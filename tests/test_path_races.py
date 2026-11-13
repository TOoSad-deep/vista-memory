"""Deterministic filesystem replacements between validation and actual I/O."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path

import pytest
from PIL import Image

from vista_memory import VistaError, VistaStore, core


def _png(color: str = "red") -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (4, 3), color).save(output, format="PNG")
    return output.getvalue()


def _files(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


@pytest.mark.parametrize("directory_name", ["png", "glsl", "json"])
@pytest.mark.parametrize("moment", ["create", "publish"])
def test_directory_replacement_cannot_redirect_writes_or_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    directory_name: str,
    moment: str,
) -> None:
    store = VistaStore(tmp_path / "vista")
    old = store.save_record(png=_png(), label="existing", glsl="// existing\r\n")
    before = _files(store.root)
    directory = store.root / directory_name
    identity = directory.stat()
    moved = store.root / f"{directory_name}-original"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "sentinel.bin").write_bytes(b"keep this external file")
    outside_before = _files(outside)
    swapped = False

    def targets_directory(path, descriptor) -> bool:
        if descriptor is not None:
            current = os.fstat(descriptor)
            return (current.st_dev, current.st_ino) == (
                identity.st_dev,
                identity.st_ino,
            )
        return Path(os.fsdecode(path)).parent == directory

    def replace_directory() -> None:
        nonlocal swapped
        directory.rename(moved)
        directory.symlink_to(outside, target_is_directory=True)
        swapped = True

    real_open = core.os.open
    real_link = core.os.link

    def open_after_replacement(path, flags, *args, **kwargs):
        if (
            not swapped
            and flags & os.O_CREAT
            and targets_directory(path, kwargs.get("dir_fd"))
        ):
            replace_directory()
        return real_open(path, flags, *args, **kwargs)

    def link_after_replacement(source, destination, *args, **kwargs):
        if not swapped and targets_directory(destination, kwargs.get("dst_dir_fd")):
            replace_directory()
        return real_link(source, destination, *args, **kwargs)

    failure = None
    try:
        with monkeypatch.context() as patch:
            if moment == "create":
                patch.setattr(core.os, "open", open_after_replacement)
            else:
                patch.setattr(core.os, "link", link_after_replacement)
            try:
                store.save_record(png=_png("blue"), label="racing", glsl="// new")
            except VistaError as error:
                failure = error
    finally:
        if swapped:
            directory.unlink()
            moved.rename(directory)

    assert swapped, "The filesystem replacement must actually occur"
    assert isinstance(failure, VistaError), "A detached directory must fail saving"
    assert _files(outside) == outside_before
    assert _files(store.root) == before
    assert store.list_records() == {"records": [old]}
    assert store.read_record(old["record"], "both").code == "// existing\r\n"


@pytest.mark.parametrize("replacement", ["file", "parent"])
def test_resolved_code_path_cannot_be_replaced_with_external_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, replacement: str
) -> None:
    store = VistaStore(tmp_path / "vista")
    saved = store.save_record(png=_png(), label="existing", glsl="// original")
    record = json.loads(
        (store.root / "json" / saved["record"]).read_text(encoding="utf-8")
    )
    relative = record["glsl"]
    code_path = store.root / relative
    target = code_path if replacement == "file" else code_path.parent
    moved = target.with_name(target.name + "-original")
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_code = outside / code_path.name
    outside_code.write_text("// outside secret", encoding="utf-8")
    real_path = store._path
    swapped = False
    resolutions = 0

    def resolve_then_replace(path):
        nonlocal swapped, resolutions
        result = real_path(path)
        if path == relative:
            resolutions += 1
        if path == relative and resolutions == 2 and not swapped:
            target.rename(moved)
            target.symlink_to(
                outside_code if replacement == "file" else outside,
                target_is_directory=replacement == "parent",
            )
            swapped = True
        return result

    failure = None
    result = None
    try:
        with monkeypatch.context() as patch:
            patch.setattr(store, "_path", resolve_then_replace)
            try:
                result = store.read_record(saved["record"], "code")
            except VistaError as error:
                failure = error
    finally:
        if swapped:
            target.unlink()
            moved.rename(target)

    assert swapped
    assert isinstance(failure, VistaError) or result.code == "// original"
    assert outside_code.read_text(encoding="utf-8") == "// outside secret"
    assert store.read_record(saved["record"], "code").code == "// original"
