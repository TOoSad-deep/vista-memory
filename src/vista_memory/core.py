"""Store immutable image records independently of agents and MCP."""

from __future__ import annotations

import io
import json
import logging
import os
import stat
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from secrets import token_hex
from typing import Any, Literal
from uuid import uuid4

from PIL import Image

logger = logging.getLogger(__name__)


class VistaError(Exception):
    """A record, input, or storage operation could not be completed."""


@dataclass
class RecordContent:
    """Metadata and the requested contents of one persisted record."""

    metadata: dict[str, Any]
    image: bytes | None = None
    code: str | None = None


def _json_snapshot(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError("render must be a JSON object")

    # Serialize first to reject cycles and non-finite numbers before traversal.
    encoded = json.dumps(value, allow_nan=False, ensure_ascii=False)
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                raise TypeError("render object keys must be strings")
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
        elif item is not None and type(item) not in (str, bool, int, float):
            raise TypeError(f"render contains a non-JSON value: {type(item).__name__}")
    return json.loads(encoded)


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number {value!r}")


def _validate_png(data: bytes) -> None:
    if not isinstance(data, bytes):
        raise TypeError("png must contain PNG bytes")
    with Image.open(io.BytesIO(data)) as image:
        if image.format != "PNG":
            raise ValueError("image format must be PNG")
        image.verify()

    # Pillow's verify stops before checking IEND's CRC. Check its completeness
    # by following chunk lengths, allowing any bytes after the image's end.
    offset = 8
    while offset + 12 <= len(data):
        length = int.from_bytes(data[offset : offset + 4], "big")
        end = offset + length + 12
        if end > len(data):
            raise ValueError("PNG contains an incomplete chunk")
        if data[offset + 4 : offset + 8] == b"IEND":
            if length != 0 or data[offset + 8 : end] != b"\xaeB`\x82":
                raise ValueError("PNG has an invalid IEND chunk")
            break
        offset = end
    else:
        raise ValueError("PNG is missing its complete IEND chunk")

    with Image.open(io.BytesIO(data)) as image:
        for frame in range(getattr(image, "n_frames", 1)):
            image.seek(frame)
            image.load()


@dataclass
class _Directory:
    fd: int
    relative: Path
    logical: str


@dataclass
class _StoredFile:
    directory: _Directory
    name: str

    @property
    def relative(self) -> Path:
        return self.directory.relative / self.name


_DESCRIPTOR_IO_SUPPORTED = (
    hasattr(os, "O_DIRECTORY")
    and hasattr(os, "O_NOFOLLOW")
    and all(
        function in os.supports_dir_fd
        for function in (os.open, os.mkdir, os.link, os.unlink)
    )
    and os.listdir in os.supports_fd
)
_DIRECTORY_FLAGS = (
    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
)


class VistaStore:
    """Bind storage operations to a single persistent data directory."""

    def __init__(self, root: str | Path):
        try:
            if not _DESCRIPTOR_IO_SUPPORTED:
                raise OSError(
                    "Store requires directory-fd and no-follow file operations"
                )
            self.root = Path(root).expanduser().resolve()
            with ExitStack() as opened:
                root_fd = self._open_absolute_directory(self.root, create=True)
                opened.callback(os.close, root_fd)
                self._root_identity = self._identity(root_fd)
                for name in ("png", "glsl", "json"):
                    try:
                        os.mkdir(name, dir_fd=root_fd)
                    except FileExistsError:
                        pass
                    self._open_directory(root_fd, name, opened)
                self._assert_root_current(root_fd)
        except Exception as error:
            raise VistaError(f"Cannot initialize store {root!r}: {error}") from error

    def save_record(
        self,
        *,
        png: bytes,
        label: str,
        glsl: str | None = None,
        reuse_glsl_from: str | None = None,
        source_agent: str | None = None,
        render: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Append a record, publishing its JSON after all content is complete."""
        try:
            if not isinstance(label, str):
                raise TypeError("label must be a string")
            if source_agent is not None and not isinstance(source_agent, str):
                raise TypeError("source_agent must be a string or None")
            if glsl is not None and not isinstance(glsl, str):
                raise TypeError("glsl must be a string or None")
            if glsl is not None and reuse_glsl_from is not None:
                raise ValueError("glsl and reuse_glsl_from are mutually exclusive")
            _validate_png(png)

            png_name = f"image-{uuid4().hex}.png"
            record_name = f"record-{uuid4().hex}.json"
            record: dict[str, Any] = {
                "label": label,
                "png": f"png/{png_name}",
                "glsl": None,
                "source_agent": source_agent,
            }
            if render is not None:
                record["render"] = _json_snapshot(render)

            with ExitStack() as opened:
                root_fd = self._open_root(opened)
                code_bytes = None
                if reuse_glsl_from is not None:
                    source = self._load_record(root_fd, reuse_glsl_from)
                    if source["glsl"] is None:
                        raise ValueError(
                            f"Record {reuse_glsl_from!r} has no associated code"
                        )
                    self._read_code(root_fd, source["glsl"])
                    record["glsl"] = source["glsl"]
                elif glsl is not None:
                    code_bytes = glsl.encode("utf-8")
                    record["glsl"] = f"glsl/shader-{uuid4().hex}.glsl"

                json_bytes = json.dumps(
                    record, ensure_ascii=False, allow_nan=False, indent=2
                ).encode("utf-8")
                summary = self._summary(record_name, record)
                directories = [self._open_directory(root_fd, "png", opened)]
                if code_bytes is not None:
                    directories.append(self._open_directory(root_fd, "glsl", opened))
                json_directory = self._open_directory(root_fd, "json", opened)
                directories.append(json_directory)
                temporary_files: list[_StoredFile] = []
                published_files: list[_StoredFile] = []
                committed = False
                try:
                    png_temporary = self._write_temporary(
                        directories[0], png, temporary_files
                    )
                    contents = [(png_temporary, _StoredFile(directories[0], png_name))]
                    if code_bytes is not None:
                        code_temporary = self._write_temporary(
                            directories[1], code_bytes, temporary_files
                        )
                        contents.append(
                            (
                                code_temporary,
                                _StoredFile(directories[1], Path(record["glsl"]).name),
                            )
                        )
                    for temporary, destination in contents:
                        self._publish(temporary, destination)
                        published_files.append(destination)

                    json_temporary = self._write_temporary(
                        json_directory, json_bytes, temporary_files
                    )
                    self._assert_bindings(root_fd, directories, record)
                    json_destination = _StoredFile(json_directory, record_name)
                    self._publish(json_temporary, json_destination)
                    published_files.append(json_destination)
                    self._assert_bindings(root_fd, directories, record)
                    committed = True
                    return summary
                finally:
                    if not committed:
                        self._cleanup(published_files)
                    self._cleanup(temporary_files)
        except Exception as error:
            raise VistaError(f"Cannot save record {label!r}: {error}") from error

    def list_records(self) -> dict[str, list[dict[str, Any]]]:
        """List published records, failing explicitly on a malformed record."""
        try:
            with ExitStack() as opened:
                root_fd = self._open_root(opened)
                directory = self._open_directory(root_fd, "json", opened)
                names = sorted(
                    name for name in os.listdir(directory.fd) if name.endswith(".json")
                )
                result = {
                    "records": [
                        self._summary(name, self._load_record(root_fd, name))
                        for name in names
                    ]
                }
                self._assert_root_current(root_fd)
                return result
        except Exception as error:
            raise VistaError(f"Cannot list records in {self.root}: {error}") from error

    def read_record(
        self,
        record: str,
        content: Literal["image", "code", "both"] = "image",
    ) -> RecordContent:
        """Read exactly the requested entities and the record's metadata."""
        try:
            if content not in ("image", "code", "both"):
                raise ValueError("content must be 'image', 'code', or 'both'")
            with ExitStack() as opened:
                root_fd = self._open_root(opened)
                data = self._load_record(root_fd, record)
                metadata = {
                    "record": record,
                    "label": data["label"],
                    "source_agent": data["source_agent"],
                }
                if "render" in data:
                    metadata["render"] = data["render"]
                result = RecordContent(metadata=metadata)
                if content in ("image", "both"):
                    result.image = self._read_image(root_fd, data["png"])
                if content in ("code", "both") and data["glsl"] is not None:
                    result.code = self._read_code(root_fd, data["glsl"])
                self._assert_root_current(root_fd)
                return result
        except Exception as error:
            raise VistaError(f"Cannot read record {record!r}: {error}") from error

    def _path(self, relative: str) -> Path:
        if not isinstance(relative, str) or not relative or "\x00" in relative:
            raise ValueError(f"Invalid relative file path {relative!r}")
        if Path(relative).is_absolute() or "\\" in relative:
            raise ValueError(f"File path must be relative to the store: {relative!r}")
        resolved = (self.root / relative).resolve()
        if not resolved.is_relative_to(self.root):
            raise ValueError(f"File path escapes the store: {relative!r}")
        return resolved

    @staticmethod
    def _identity(descriptor: int) -> tuple[int, int]:
        info = os.fstat(descriptor)
        return info.st_dev, info.st_ino

    @staticmethod
    def _open_absolute_directory(path: Path, *, create: bool = False) -> int:
        descriptor = os.open(path.anchor, _DIRECTORY_FLAGS)
        try:
            for name in path.parts[1:]:
                try:
                    child = os.open(name, _DIRECTORY_FLAGS, dir_fd=descriptor)
                except FileNotFoundError:
                    if not create:
                        raise
                    try:
                        os.mkdir(name, dir_fd=descriptor)
                    except FileExistsError:
                        pass
                    child = os.open(name, _DIRECTORY_FLAGS, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def _open_root(self, opened: ExitStack) -> int:
        descriptor = self._open_absolute_directory(self.root)
        opened.callback(os.close, descriptor)
        if self._identity(descriptor) != self._root_identity:
            raise ValueError(f"Store root changed: {self.root}")
        return descriptor

    def _open_directory(
        self, root_fd: int, logical: str, opened: ExitStack
    ) -> _Directory:
        relative = self._path(logical).relative_to(self.root)
        return self._open_resolved_directory(root_fd, relative, logical, opened)

    @staticmethod
    def _open_resolved_directory(
        root_fd: int, relative: Path, logical: str, opened: ExitStack
    ) -> _Directory:
        descriptor = os.dup(root_fd)
        try:
            for name in relative.parts:
                child = os.open(name, _DIRECTORY_FLAGS, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
        except BaseException:
            os.close(descriptor)
            raise
        opened.callback(os.close, descriptor)
        return _Directory(descriptor, relative, logical)

    def _assert_root_current(self, root_fd: int) -> None:
        current = self._open_absolute_directory(self.root)
        try:
            if self._identity(current) != self._identity(root_fd):
                raise ValueError(f"Store root changed: {self.root}")
        finally:
            os.close(current)

    def _assert_bindings(
        self, root_fd: int, directories: list[_Directory], record: dict[str, Any]
    ) -> None:
        self._assert_root_current(root_fd)
        with ExitStack() as opened:
            for directory in directories:
                current = self._open_directory(root_fd, directory.logical, opened)
                if self._identity(current.fd) != self._identity(directory.fd):
                    raise ValueError(f"Store directory changed: {directory.logical}")
        self._path(record["png"])
        if record["glsl"] is not None:
            self._path(record["glsl"])

    def _read_bytes(self, root_fd: int, relative: str) -> bytes:
        resolved = self._path(relative).relative_to(self.root)
        with ExitStack() as opened:
            parent = self._open_resolved_directory(
                root_fd, resolved.parent, str(resolved.parent), opened
            )
            descriptor = os.open(
                resolved.name,
                os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0),
                dir_fd=parent.fd,
            )
            opened.callback(os.close, descriptor)
            with os.fdopen(descriptor, "rb", closefd=False) as handle:
                if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                    raise ValueError(
                        f"Store content must be a regular file: {relative}"
                    )
                return handle.read()

    def _load_record(self, root_fd: int, name: str) -> dict[str, Any]:
        try:
            if (
                not isinstance(name, str)
                or not name.endswith(".json")
                or "/" in name
                or "\\" in name
                or "\x00" in name
            ):
                raise ValueError("record must be a JSON filename without a path")
            data = json.loads(
                self._read_bytes(root_fd, f"json/{name}").decode("utf-8"),
                parse_constant=_reject_json_constant,
            )
            if not isinstance(data, dict):
                raise TypeError("record must be a JSON object")
            for field in ("label", "png", "glsl", "source_agent"):
                if field not in data:
                    raise ValueError(f"record is missing field {field!r}")
            if not isinstance(data["label"], str):
                raise TypeError("label must be a string")
            if data["source_agent"] is not None and not isinstance(
                data["source_agent"], str
            ):
                raise TypeError("source_agent must be a string or null")
            self._path(data["png"])
            if data["glsl"] is not None:
                self._path(data["glsl"])
            if "render" in data:
                data["render"] = _json_snapshot(data["render"])
            return data
        except Exception as error:
            raise ValueError(
                f"Invalid or unreadable record {name!r}: {error}"
            ) from error

    def _read_image(self, root_fd: int, relative: str) -> bytes:
        try:
            data = self._read_bytes(root_fd, relative)
            _validate_png(data)
            return data
        except Exception as error:
            raise ValueError(f"Cannot read PNG {relative!r}: {error}") from error

    def _read_code(self, root_fd: int, relative: str) -> str:
        try:
            return self._read_bytes(root_fd, relative).decode("utf-8")
        except Exception as error:
            raise ValueError(f"Cannot read GLSL {relative!r}: {error}") from error

    @staticmethod
    def _summary(name: str, record: dict[str, Any]) -> dict[str, Any]:
        return {
            "record": name,
            "label": record["label"],
            "source_agent": record["source_agent"],
            "has_glsl": record["glsl"] is not None,
        }

    @staticmethod
    def _write_temporary(
        directory: _Directory, data: bytes, files: list[_StoredFile]
    ) -> _StoredFile:
        temporary = _StoredFile(directory, f".vista-{token_hex(16)}.tmp")
        try:
            with ExitStack() as opened:
                descriptor = os.open(
                    temporary.name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=directory.fd,
                )
                opened.callback(os.close, descriptor)
                files.append(temporary)
                with os.fdopen(descriptor, "wb", closefd=False) as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
            return temporary
        except Exception as error:
            raise OSError(
                f"Cannot write temporary file {temporary.relative}: {error}"
            ) from error

    @staticmethod
    def _publish(temporary: _StoredFile, destination: _StoredFile) -> None:
        try:
            os.link(
                temporary.name,
                destination.name,
                src_dir_fd=temporary.directory.fd,
                dst_dir_fd=destination.directory.fd,
                follow_symlinks=False,
            )
        except OSError as error:
            raise OSError(
                f"Cannot publish file {destination.relative}: {error}"
            ) from error

    @staticmethod
    def _cleanup(files: list[_StoredFile]) -> None:
        for filename in reversed(files):
            try:
                os.unlink(filename.name, dir_fd=filename.directory.fd)
            except FileNotFoundError:
                pass
            except Exception:
                logger.error(
                    "Could not remove store file %s", filename.relative, exc_info=True
                )
