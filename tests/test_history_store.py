"""Task-local registration, exact retries and publication recovery."""

from __future__ import annotations

import io
import json
import multiprocessing
import os
import signal
from concurrent.futures import ProcessPoolExecutor
from multiprocessing.connection import Connection
from pathlib import Path
from unittest.mock import patch

import pytest
from PIL import Image

from vista_memory import VistaStore
from vista_memory.history_store import (
    HistoryConflict,
    HistoryError,
    HistoryUnavailable,
    HistoryUnknownRecord,
    TaskHistory,
)


def png(color: str) -> bytes:
    target = io.BytesIO()
    Image.new("RGB", (4, 5), color).save(target, format="PNG")
    return target.getvalue()


def file_names(root: Path) -> set[str]:
    return {
        path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()
    }


def test_task_binding_import_and_exact_replay(tmp_path: Path) -> None:
    store = VistaStore(tmp_path / "vista")
    db = tmp_path / "tasks"
    history = TaskHistory(store, db, task_id="one")
    image = png("red")
    old = store.save_record(png=image, label="old")
    assert history.list_records() == []
    parent = history.import_record("old-request", old["record"], subtask_id="border")
    assert (
        history.import_record("old-request", old["record"], subtask_id="border")
        == parent
    )
    with pytest.raises(HistoryConflict, match="already registered"):
        history.import_record("other-import", old["record"])

    options = {
        "png": png("blue"),
        "label": "new\nlabel",
        "glsl": "// gradient\r\n",
        "source_agent": "native",
        "subtask_id": "border",
        "subtask_label": "边缘",
        "parent_short_id": parent.short_id,
    }
    new = history.save("save", render={"z": 1, "a": 2}, **options)
    assert new.short_id == "R2"
    assert new.receipt == "已存 R2 · new label"
    assert new.parent_record == parent.record
    reopened = TaskHistory(store, db, task_id="one")
    assert reopened.save("save", render={"a": 2, "z": 1}, **options) == new
    assert reopened.list_records() == [parent, new]
    result = reopened.read(new.short_id, "both")
    assert result.image == options["png"]
    assert result.code == options["glsl"]
    assert result.metadata["render"] == {"a": 2, "z": 1}
    with pytest.raises(HistoryConflict, match="Different input"):
        reopened.save("save", render={"a": 3}, **options)
    assert len(store.list_records()["records"]) == 2

    other_task = TaskHistory(store, db, task_id="two")
    with pytest.raises(HistoryUnknownRecord, match="请刷新目录"):
        other_task.read(new.short_id)
    with pytest.raises(HistoryUnknownRecord):
        other_task.save("save", **options)
    assert other_task.save("save", png=image, label="other").short_id == "R1"
    assert other_task.read("R1").metadata["label"] == "other"
    assert reopened.get("R1") == parent
    assert store.read_record(old["record"]).image == image
    with pytest.raises(HistoryConflict, match="bound to another store"):
        TaskHistory(VistaStore(tmp_path / "different"), db, task_id="one")

    persisted = json.loads((store.root / "json" / new.record).read_text())
    (store.root / persisted["png"]).unlink()
    with pytest.raises(HistoryUnavailable, match="原图不可用") as error:
        reopened.read(new.short_id, "both")
    assert error.value.image_unavailable
    with pytest.raises(HistoryConflict, match="Published content is missing"):
        reopened.save("save", render={"z": 1, "a": 2}, **options)


def _concurrent_save(root: Path, task_dir: Path, request: str, image: bytes) -> str:
    history = TaskHistory(VistaStore(root), task_dir, task_id="task")
    return history.save(request, png=image, label=request).short_id


def test_simultaneous_retries_from_separate_processes(tmp_path: Path) -> None:
    root = tmp_path / "vista"
    task_dir = tmp_path / "task"
    image = png("red")
    requests = ("one", "one", "two", "one", "two")
    with ProcessPoolExecutor(
        max_workers=3, mp_context=multiprocessing.get_context("spawn")
    ) as executor:
        results = list(
            executor.map(
                _concurrent_save, [root] * 5, [task_dir] * 5, requests, [image] * 5
            )
        )
    assert results[0] == results[1] == results[3]
    assert results[2] == results[4]
    assert {results[0], results[2]} == {"R1", "R2"}
    history = TaskHistory(VistaStore(root), task_dir, task_id="task")
    assert len(file_names(root)) == 4
    assert [record.short_id for record in history.list_records()] == ["R1", "R2"]
    assert all(history.read(item).image == image for item in results)
    with history.connect() as connection:
        assert (
            connection.execute("SELECT version FROM history_tasks").fetchone()[0] == 2
        )


def _save_until_killed(
    root: Path,
    task_dir: Path,
    stage: str,
    kwargs: dict,
    ready: Connection,
) -> None:
    store = VistaStore(root)
    history = TaskHistory(store, task_dir, task_id="task")
    publish = store._publish

    def pause() -> None:
        ready.send(stage)
        signal.pause()

    def after_publish(temporary, destination) -> None:
        publish(temporary, destination)
        if stage == destination.directory.logical:
            pause()

    def during_write(directory, target, payload, partial, save_identity) -> None:
        assert partial is None
        descriptor = os.open(
            target.temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
            dir_fd=directory,
        )
        try:
            info = os.fstat(descriptor)
            target.device, target.inode = info.st_dev, info.st_ino
            save_identity()
            prefix = payload[: len(payload) // 2]
            assert os.write(descriptor, prefix) == len(prefix)
            pause()
        finally:
            os.close(descriptor)

    if stage == "reserved":
        patch_target = patch.object(history, "_publish", lambda *args: pause())
    elif stage == "writing":
        patch_target = patch("vista_memory._publication.write_temporary", during_write)
    else:
        patch_target = patch.object(store, "_publish", after_publish)
    try:
        with patch_target:
            history.save("stable-request", **kwargs)
    finally:
        ready.close()


def kill_save(root: Path, task_dir: Path, stage: str, options: dict) -> None:
    context = multiprocessing.get_context("spawn")
    receive, send = context.Pipe(duplex=False)
    child = context.Process(
        target=_save_until_killed, args=(root, task_dir, stage, options, send)
    )
    child.start()
    send.close()
    try:
        assert receive.poll(10), f"child did not reach {stage}"
        assert receive.recv() == stage
    finally:
        if child.is_alive():
            child.kill()
        child.join(10)
        receive.close()
        assert child.exitcode == -signal.SIGKILL
        child.close()


@pytest.mark.parametrize(
    ("stage", "shared"),
    [
        ("reserved", False),
        ("writing", False),
        ("png", False),
        ("glsl", False),
        ("json", False),
        ("png", True),
    ],
)
def test_recover_killed_publication(tmp_path: Path, stage: str, shared: bool) -> None:
    store = VistaStore(tmp_path / "vista")
    task_dir = tmp_path / "task"
    options = {"png": png("blue"), "label": "new", "render": {"scale": 3}}
    existing = file_names(store.root)
    if shared:
        old = store.save_record(png=png("red"), label="old", glsl="// code\r\n")
        options["reuse_glsl_from"] = old["record"]
        existing = file_names(store.root)
    else:
        options["glsl"] = "// code\r\n"
    kill_save(store.root, task_dir, stage, options)

    history = TaskHistory(store, task_dir, task_id="task")
    assert history.list_records() == []
    with history.connect() as connection:
        reservation = connection.execute(
            "SELECT sequence, record, manifest FROM history_records"
        ).fetchone()
    assert reservation["sequence"] == 1
    new = history.save("stable-request", **options)
    assert new.short_id == "R1"
    assert new.record == reservation["record"]
    assert history.list_records() == [new]
    assert history.save("stable-request", **options) == new
    actual = history.read(new.short_id, "both")
    assert actual.image == options["png"]
    assert actual.code == "// code\r\n"
    assert actual.metadata["render"] == {"scale": 3}
    json_rel = f"json/{new.record}"
    record = json.loads((store.root / json_rel).read_text())
    assert file_names(store.root) == existing | {
        json_rel,
        record["png"],
        record["glsl"],
    }
    if reservation["manifest"]:
        manifest = json.loads(reservation["manifest"])
        assert record["png"] == f"png/{manifest['files']['png']['name']}"
        if not shared:
            assert record["glsl"] == f"glsl/{manifest['files']['glsl']['name']}"
    if shared:
        original = store.read_record(old["record"], "both")
        assert original.code == "// code\r\n"
        assert original.image == png("red")
        assert len(list((store.root / "glsl").iterdir())) == 1


def test_foreign_file_at_reserved_name_is_preserved(tmp_path: Path) -> None:
    store = VistaStore(tmp_path / "vista")
    task_dir = tmp_path / "task"
    options = {"png": png("blue"), "label": "new"}
    kill_save(store.root, task_dir, "png", options)
    published = next((store.root / "png").glob("*.png"))
    moved = published.with_suffix(".saved")
    published.rename(moved)
    published.write_bytes(moved.read_bytes())
    before = file_names(store.root)

    history = TaskHistory(store, task_dir, task_id="task")
    with pytest.raises(HistoryConflict, match="uncertain ownership"):
        history.save("stable-request", **options)
    assert file_names(store.root) == before
    assert published.read_bytes() == moved.read_bytes() == options["png"]
    assert history.list_records() == []

    published.unlink()
    moved.rename(published)
    assert history.save("stable-request", **options).short_id == "R1"
    assert not list(store.root.rglob("*.tmp"))


def test_json_is_unpublished_if_png_directory_changes_at_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = VistaStore(tmp_path / "vista")
    history = TaskHistory(store, tmp_path / "task", task_id="task")
    existing = store.save_record(png=png("red"), label="existing")
    options = {"png": png("blue"), "label": "new", "glsl": "// new shader"}
    png_dir = store.root / "png"
    original = store.root / "png-original"
    publish = store._publish

    def publish_after_replacement(temporary, destination):
        if destination.directory.logical == "json":
            png_dir.rename(original)
            png_dir.mkdir()
        publish(temporary, destination)

    with monkeypatch.context() as patch:
        patch.setattr(store, "_publish", publish_after_replacement)
        with pytest.raises(HistoryError, match="Store directory changed: png"):
            history.save("new-request", **options)

    assert original.is_dir()
    assert store.list_records() == {"records": [existing]}
    assert history.list_records() == []
    assert not list(png_dir.iterdir())

    png_dir.rmdir()
    original.rename(png_dir)
    reopened = TaskHistory(VistaStore(store.root), history.task_dir, task_id="task")
    saved = reopened.save("new-request", **options)
    material = reopened.read(saved.short_id, "both")
    assert saved.short_id == "R1"
    assert material.image == options["png"]
    assert material.code == options["glsl"]
    assert len(file_names(store.root)) == 5
    assert not list(store.root.rglob("*.tmp"))
