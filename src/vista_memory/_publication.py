"""Ownership and recovery for a single immutable record publication."""

from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Callable
from dataclasses import asdict, dataclass
from secrets import token_hex

_DESTINATIONS = {
    "png": r"image-[0-9a-f]{32}\.png",
    "glsl": r"shader-[0-9a-f]{32}\.glsl",
    "json": r"record-[0-9a-f]{32}\.json",
}


class PublicationConflict(ValueError):
    """A reserved path or its contents no longer match the write manifest."""


@dataclass
class Target:
    name: str
    temporary: str
    device: int | None = None
    inode: int | None = None

    @classmethod
    def reserve(cls, name: str) -> Target:
        return cls(name, f".vista-{token_hex(16)}.tmp")

    def owns(self, info: os.stat_result) -> bool:
        return stat.S_ISREG(info.st_mode) and (info.st_dev, info.st_ino) == (
            self.device,
            self.inode,
        )


@dataclass
class Manifest:
    files: dict[str, Target]
    shared_glsl: str | None = None
    shared_glsl_digest: str | None = None

    def encode(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"))

    @classmethod
    def decode(cls, encoded: str) -> Manifest:
        try:
            data = json.loads(encoded)
            files = {key: Target(**value) for key, value in data["files"].items()}
            if not {"png", "json"} <= files.keys() <= _DESTINATIONS.keys():
                raise ValueError("Unexpected directory in write manifest")
            for key, target in files.items():
                if not re.fullmatch(_DESTINATIONS[key], target.name):
                    raise ValueError("Invalid destination in write manifest")
                if not re.fullmatch(r"\.vista-[0-9a-f]{32}\.tmp", target.temporary):
                    raise ValueError("Invalid temporary name in write manifest")
            data["files"] = files
            return cls(**data)
        except (AttributeError, KeyError, TypeError, ValueError) as error:
            raise PublicationConflict(f"Invalid write manifest: {error}") from error


@dataclass
class PendingPublication:
    manifest: Manifest | None
    persist: Callable[[Manifest], None]
    confirmed: bool


def _stat(directory: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _checked_fd(directory: int, name: str, target: Target, flags: int) -> int:
    descriptor = os.open(
        name,
        flags | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0),
        dir_fd=directory,
    )
    try:
        if not target.owns(os.fstat(descriptor)):
            raise PublicationConflict(f"File identity changed: {name}")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def state(
    directory: int, target: Target, payload: bytes, *, require_published: bool
) -> tuple[bool, bytes | None]:
    """Verify the planned names and return publication status and staged bytes."""
    published = _stat(directory, target.name)
    staged = _stat(directory, target.temporary)
    if require_published and published is None:
        raise PublicationConflict(f"Published content is missing: {target.name}")
    for name, info in ((target.name, published), (target.temporary, staged)):
        if info is not None and not target.owns(info):
            raise PublicationConflict(f"Reserved file has uncertain ownership: {name}")
    if published is not None:
        with os.fdopen(
            _checked_fd(directory, target.name, target, os.O_RDONLY), "rb"
        ) as handle:
            if handle.read() != payload:
                raise PublicationConflict(f"Published content differs: {target.name}")
        return True, None
    if staged is None:
        return False, None
    if staged.st_nlink != 1:
        raise PublicationConflict(f"Staged file has another link: {target.temporary}")
    with os.fdopen(
        _checked_fd(directory, target.temporary, target, os.O_RDONLY), "rb"
    ) as handle:
        partial = handle.read()
    if not payload.startswith(partial):
        raise PublicationConflict(f"Staged content differs: {target.temporary}")
    return False, partial


def write_temporary(
    directory: int,
    target: Target,
    payload: bytes,
    partial: bytes | None,
    save_identity: Callable[[], None],
) -> None:
    if partial is None:
        try:
            descriptor = os.open(
                target.temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory,
            )
        except FileExistsError as error:
            raise PublicationConflict(
                f"File appeared at reserved staging path: {target.temporary}"
            ) from error
    else:
        descriptor = _checked_fd(directory, target.temporary, target, os.O_WRONLY)
    with os.fdopen(descriptor, "wb") as handle:
        if partial is None:
            info = os.fstat(descriptor)
            target.device, target.inode = info.st_dev, info.st_ino
            save_identity()
        else:
            handle.seek(len(partial))
        handle.write(payload[len(partial or b"") :])
        handle.flush()
        os.fsync(descriptor)


def remove_owned_link(directory: int, target: Target, *, name: str) -> None:
    info = _stat(directory, name)
    if info is not None:
        if not target.owns(info):
            raise PublicationConflict(f"File has uncertain ownership: {name}")
        os.unlink(name, dir_fd=directory)
        os.fsync(directory)


def verify_publication(directory: int, target: Target) -> None:
    info = _stat(directory, target.name)
    if info is None or not target.owns(info):
        raise PublicationConflict(f"Published file identity changed: {target.name}")
