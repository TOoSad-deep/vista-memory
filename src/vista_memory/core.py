"""Store immutable image records independently of agents and MCP."""

from __future__ import annotations

import io
import json
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
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


class VistaStore:
    """Bind storage operations to a single persistent data directory."""

    def __init__(self, root: str | Path):
        try:
            self.root = Path(root).expanduser().resolve()
            self.root.mkdir(parents=True, exist_ok=True)
            for name in ("png", "glsl", "json"):
                directory = self._path(name)
                directory.mkdir(parents=True, exist_ok=True)
                self._directory(name)
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
        temporary_files: list[Path] = []
        published_files: list[Path] = []
        committed = False
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

            code_bytes = None
            if reuse_glsl_from is not None:
                source = self._load_record(reuse_glsl_from)
                if source["glsl"] is None:
                    raise ValueError(
                        f"Record {reuse_glsl_from!r} has no associated code"
                    )
                self._read_code(source["glsl"])
                record["glsl"] = source["glsl"]
            elif glsl is not None:
                code_bytes = glsl.encode("utf-8")
                record["glsl"] = f"glsl/shader-{uuid4().hex}.glsl"

            json_bytes = json.dumps(
                record, ensure_ascii=False, allow_nan=False, indent=2
            ).encode("utf-8")
            summary = self._summary(record_name, record)

            png_destination = self._directory("png") / png_name
            png_temporary = self._write_temporary(
                png_destination.parent, png, temporary_files
            )
            contents = [(png_temporary, png_destination)]
            if code_bytes is not None:
                code_destination = self._directory("glsl") / Path(record["glsl"]).name
                code_temporary = self._write_temporary(
                    code_destination.parent, code_bytes, temporary_files
                )
                contents.append((code_temporary, code_destination))

            for temporary, destination in contents:
                self._publish(temporary, destination)
                published_files.append(destination)

            json_destination = self._directory("json") / record_name
            json_temporary = self._write_temporary(
                json_destination.parent, json_bytes, temporary_files
            )
            self._publish(json_temporary, json_destination)
            committed = True
            return summary
        except Exception as error:
            if not committed:
                self._cleanup(published_files)
            raise VistaError(f"Cannot save record {label!r}: {error}") from error
        finally:
            self._cleanup(temporary_files)

    def list_records(self) -> dict[str, list[dict[str, Any]]]:
        """List published records, failing explicitly on a malformed record."""
        try:
            names = sorted(
                entry.name
                for entry in self._directory("json").iterdir()
                if entry.name.endswith(".json")
            )
            return {
                "records": [
                    self._summary(name, self._load_record(name)) for name in names
                ]
            }
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
            data = self._load_record(record)
            metadata = {
                "record": record,
                "label": data["label"],
                "source_agent": data["source_agent"],
            }
            if "render" in data:
                metadata["render"] = data["render"]
            result = RecordContent(metadata=metadata)
            if content in ("image", "both"):
                result.image = self._read_image(data["png"])
            if content in ("code", "both") and data["glsl"] is not None:
                result.code = self._read_code(data["glsl"])
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

    def _directory(self, name: str) -> Path:
        directory = self._path(name)
        if not directory.is_dir():
            raise NotADirectoryError(f"Store directory is unavailable: {directory}")
        return directory

    def _load_record(self, name: str) -> dict[str, Any]:
        try:
            if (
                not isinstance(name, str)
                or not name.endswith(".json")
                or "/" in name
                or "\\" in name
                or "\x00" in name
            ):
                raise ValueError("record must be a JSON filename without a path")
            record_path = self._path(f"json/{name}")
            data = json.loads(
                record_path.read_bytes().decode("utf-8"),
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

    def _read_image(self, relative: str) -> bytes:
        try:
            data = self._path(relative).read_bytes()
            _validate_png(data)
            return data
        except Exception as error:
            raise ValueError(f"Cannot read PNG {relative!r}: {error}") from error

    def _read_code(self, relative: str) -> str:
        try:
            return self._path(relative).read_bytes().decode("utf-8")
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
    def _write_temporary(directory: Path, data: bytes, files: list[Path]) -> Path:
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=directory, prefix=".vista-", suffix=".tmp", delete=False
            ) as handle:
                temporary = Path(handle.name)
                files.append(temporary)
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            return temporary
        except Exception as error:
            raise OSError(
                f"Cannot write temporary file {temporary or directory}: {error}"
            ) from error

    @staticmethod
    def _publish(temporary: Path, destination: Path) -> None:
        try:
            os.link(temporary, destination)
        except OSError as error:
            raise OSError(f"Cannot publish file {destination}: {error}") from error

    def _cleanup(self, files: list[Path]) -> None:
        for filename in reversed(files):
            try:
                if not filename.parent.resolve().is_relative_to(self.root):
                    raise ValueError(f"Cleanup directory escapes store: {filename}")
                filename.unlink(missing_ok=True)
            except Exception:
                logger.error("Could not remove store file %s", filename, exc_info=True)
