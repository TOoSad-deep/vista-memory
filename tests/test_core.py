"""Behavioral tests for persistent image records and publication failures."""

from __future__ import annotations

import io
import json
import multiprocessing
import threading
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path
from uuid import UUID

import pytest
from PIL import Image

from vista_memory import RecordContent, VistaError, VistaStore, core


def make_png(color: str = "red") -> bytes:
    output = io.BytesIO()
    Image.new("RGBA", (12, 10), color).save(output, format="PNG")
    return output.getvalue()


@pytest.fixture
def png() -> bytes:
    return make_png()


@pytest.fixture
def store(tmp_path: Path) -> VistaStore:
    return VistaStore(tmp_path / "vista")


def record_json(store: VistaStore, name: str) -> dict:
    return json.loads((store.root / "json" / name).read_bytes())


def write_record(store: VistaStore, name: str, data: dict) -> None:
    (store.root / "json" / name).write_text(json.dumps(data), encoding="utf-8")


def test_reference_and_empty_store(store: VistaStore, png: bytes) -> None:
    assert store.list_records() == {"records": []}
    summary = store.save_record(png=png, label="参考图")
    assert summary == {
        "record": summary["record"],
        "label": "参考图",
        "source_agent": None,
        "has_glsl": False,
    }
    assert store.list_records() == {"records": [summary]}
    persisted = record_json(store, summary["record"])
    assert set(persisted) == {"label", "png", "glsl", "source_agent"}
    assert persisted["glsl"] is None
    assert (store.root / persisted["png"]).read_bytes() == png
    assert store.read_record(summary["record"]).image == png
    code_only = store.read_record(summary["record"], "code")
    assert isinstance(code_only, RecordContent)
    assert code_only.image is None
    assert code_only.code is None
    assert store.read_record(summary["record"], "both").image == png
    assert code_only.metadata == {
        "record": summary["record"],
        "label": "参考图",
        "source_agent": None,
    }
    assert set(entry.name for entry in store.root.iterdir()) == {"png", "glsl", "json"}
    assert not list(store.root.rglob("*.tmp"))


def test_shared_code_new_version_and_metadata_snapshot(
    store: VistaStore, png: bytes
) -> None:
    code = "// 原始代码\r\nvoid main() {\r\n}\r\n"
    parameters = {"time": 0, "uniforms": {"radius": 0.2, "values": [1, None, True]}}
    first = store.save_record(
        png=png,
        label="first",
        glsl=code,
        source_agent="generation",
        render=parameters,
    )
    parameters["uniforms"]["values"].append(999)
    parameters["uniforms"]["radius"] = 5
    second_png = make_png("blue")
    second = store.save_record(
        png=second_png,
        label="second",
        reuse_glsl_from=first["record"],
        render={"time": 0, "uniforms": {"radius": 0.3}},
    )
    third = store.save_record(png=png, label="third", glsl="// changed\n", render={})
    first_data = record_json(store, first["record"])
    assert first_data["glsl"] == record_json(store, second["record"])["glsl"]
    assert first_data["glsl"] != record_json(store, third["record"])["glsl"]
    assert len(list((store.root / "glsl").iterdir())) == 2
    assert (store.root / first_data["glsl"]).read_bytes() == code.encode("utf-8")

    reopened = VistaStore(store.root)
    result = reopened.read_record(first["record"], "both")
    assert result.image == png
    assert result.code == code
    assert result.metadata["source_agent"] == "generation"
    assert result.metadata["render"]["uniforms"] == {
        "radius": 0.2,
        "values": [1, None, True],
    }
    result.metadata["render"]["uniforms"]["radius"] = 10
    assert (
        reopened.read_record(first["record"]).metadata["render"]["uniforms"]["radius"]
        == 0.2
    )
    assert reopened.read_record(second["record"], "both").image == second_png
    assert reopened.read_record(second["record"], "code").code == code
    assert reopened.read_record(third["record"], "code").code == "// changed\n"
    assert reopened.read_record(third["record"]).metadata["render"] == {}


def test_render_without_code_and_empty_code_are_supported(
    store: VistaStore, png: bytes
) -> None:
    no_code = store.save_record(png=png, label="generated", render={"time": 2})
    empty_code = store.save_record(png=png, label="empty code", glsl="")
    assert store.read_record(no_code["record"]).metadata["render"] == {"time": 2}
    assert no_code["has_glsl"] is False
    assert empty_code["has_glsl"] is True
    assert store.read_record(empty_code["record"], "code").code == ""


def test_design_json_examples_and_custom_internal_paths(
    store: VistaStore, png: bytes
) -> None:
    assets = store.root / "custom assets"
    assets.mkdir()
    (assets / "reference.png").write_bytes(png)
    (store.root / "png" / "render-002.png").write_bytes(png)
    (store.root / "glsl" / "shader-001.glsl").write_bytes(b"// source\r\n")
    reference = {
        "label": "参考图",
        "png": "custom assets/reference.png",
        "glsl": None,
        "source_agent": None,
    }
    render = {
        "label": "第 2 次渲染",
        "png": "png/render-002.png",
        "glsl": "glsl/shader-001.glsl",
        "source_agent": "generation",
        "render": {"time": 0, "uniforms": {"radius": 0.3}},
    }
    write_record(store, "render-002.json", render)
    write_record(store, "reference-001.json", reference)
    (store.root / "json" / ".unfinished.tmp").write_text("{")
    (store.root / "png" / "orphan.png").write_bytes(png)
    assert [item["record"] for item in store.list_records()["records"]] == [
        "reference-001.json",
        "render-002.json",
    ]
    assert store.read_record("reference-001.json").image == png
    result = store.read_record("render-002.json", "both")
    assert result.image == png
    assert result.code == "// source\r\n"
    assert result.metadata["render"] == render["render"]


@pytest.mark.parametrize("missing", ["png", "glsl"])
def test_read_only_accesses_requested_entity(
    store: VistaStore, png: bytes, missing: str
) -> None:
    summary = store.save_record(png=png, label="selective", glsl="// code")
    data = record_json(store, summary["record"])
    (store.root / data[missing]).unlink()
    available_mode = "code" if missing == "png" else "image"
    result = store.read_record(summary["record"], available_mode)
    assert result.code == ("// code" if missing == "png" else None)
    assert result.image == (png if missing == "glsl" else None)
    assert store.list_records() == {"records": [summary]}
    with pytest.raises(VistaError, match=Path(data[missing]).name) as error:
        store.read_record(summary["record"], "both")
    assert summary["record"] in str(error.value)
    assert error.value.__cause__ is not None


@pytest.mark.parametrize("entity", ["png", "glsl"])
def test_corrupt_requested_entities_fail(store: VistaStore, png: bytes, entity: str):
    summary = store.save_record(png=png, label="corrupted", glsl="// code")
    data = record_json(store, summary["record"])
    (store.root / data[entity]).write_bytes(b"\xff\xfe broken data")
    other_mode = "code" if entity == "png" else "image"
    store.read_record(summary["record"], other_mode)
    with pytest.raises(VistaError, match=Path(data[entity]).name):
        store.read_record(summary["record"], "both")


@pytest.mark.parametrize(
    "changes",
    [
        {"label": 123},
        {"source_agent": []},
        {"glsl": b"code"},
        {"glsl": "code", "reuse_glsl_from": "source.json"},
        {"render": []},
        {"render": {"nested": {1: "value"}}},
        {"render": {"value": (1, 2)}},
        {"render": {"value": {1, 2}}},
        {"render": {"value": float("nan")}},
        {"render": {"value": float("inf")}},
        {"render": {"value": float("-inf")}},
    ],
)
def test_invalid_save_input_leaves_no_files(
    store: VistaStore, png: bytes, changes: dict
) -> None:
    arguments = {"png": png, "label": "invalid", **changes}
    with pytest.raises(VistaError) as error:
        store.save_record(**arguments)
    assert error.value.__cause__ is not None
    assert store.list_records() == {"records": []}
    assert not [entry for entry in store.root.rglob("*") if entry.is_file()]


def test_cyclic_render_is_reported(store: VistaStore, png: bytes) -> None:
    parameters = {}
    parameters["self"] = parameters
    with pytest.raises(VistaError, match="Circular reference"):
        store.save_record(png=png, label="cycle", render=parameters)
    assert store.list_records() == {"records": []}


@pytest.mark.parametrize("damage", ["wrong_format", "truncated", "crc", "not_bytes"])
def test_png_must_be_complete_and_valid(store: VistaStore, png: bytes, damage: str):
    if damage == "wrong_format":
        output = io.BytesIO()
        Image.new("RGB", (3, 3)).save(output, "JPEG")
        data = output.getvalue()
    elif damage == "truncated":
        data = png[:-1]
    elif damage == "crc":
        data = png[:-1] + bytes([png[-1] ^ 1])
    else:
        data = bytearray(png)
    with pytest.raises(VistaError):
        store.save_record(png=data, label=damage)
    assert store.list_records() == {"records": []}


def test_code_reuse_requires_existing_code(store: VistaStore, png: bytes) -> None:
    reference = store.save_record(png=png, label="reference")
    with pytest.raises(VistaError, match="no associated code"):
        store.save_record(png=png, label="new", reuse_glsl_from=reference["record"])
    with pytest.raises(VistaError, match="missing.json"):
        store.save_record(png=png, label="new", reuse_glsl_from="missing.json")
    source = store.save_record(png=png, label="source", glsl="// code")
    source_data = record_json(store, source["record"])
    (store.root / source_data["png"]).unlink()
    reused = store.save_record(
        png=png, label="reused", reuse_glsl_from=source["record"]
    )
    assert store.read_record(reused["record"], "code").code == "// code"
    (store.root / source_data["glsl"]).unlink()
    with pytest.raises(VistaError, match=Path(source_data["glsl"]).name):
        store.save_record(png=png, label="missing", reuse_glsl_from=source["record"])


@pytest.mark.parametrize(
    "name",
    ["../record.json", "json/record.json", "/record.json", "..\\record.json", "x"],
)
def test_record_name_must_be_filename(store: VistaStore, name: str) -> None:
    with pytest.raises(VistaError, match="filename"):
        store.read_record(name)


@pytest.mark.parametrize("content", ["all", "", None, [], {}])
def test_invalid_read_mode(store: VistaStore, content) -> None:
    with pytest.raises(VistaError, match="content must be"):
        store.read_record("any.json", content)


@pytest.mark.parametrize(
    "bad_json",
    [
        "{",
        "[]",
        "{}",
        '{"label": 1, "png": "png/a.png", "glsl": null, "source_agent": null}',
        '{"label": "a", "png": 2, "glsl": null, "source_agent": null}',
        '{"label": "a", "png": "png/a.png", "glsl": [], "source_agent": null}',
        '{"label": "a", "png": "png/a.png", "glsl": null, "source_agent": 2}',
        '{"label":"a","png":"png/a.png","glsl":null,"source_agent":null,'
        '"render":{"value":NaN}}',
        '{"label":"a","png":"png/a.png","glsl":null,"source_agent":null,"render":null}',
    ],
)
def test_malformed_json_makes_listing_fail(
    store: VistaStore, png: bytes, bad_json: str
):
    store.save_record(png=png, label="valid")
    (store.root / "json" / "bad.json").write_text(bad_json, encoding="utf-8")
    with pytest.raises(VistaError, match="bad.json"):
        store.list_records()
    with pytest.raises(VistaError, match="bad.json"):
        store.read_record("bad.json")


@pytest.mark.parametrize("reference", ["../outside.png", "/tmp/outside.png"])
def test_record_cannot_reference_paths_outside_store(store: VistaStore, reference: str):
    write_record(
        store,
        "escape.json",
        {"label": "escape", "png": reference, "glsl": None, "source_agent": None},
    )
    with pytest.raises(VistaError, match="escape.json"):
        store.read_record("escape.json", "code")
    with pytest.raises(VistaError, match="escape.json"):
        store.list_records()


@pytest.mark.parametrize("entity", ["png", "glsl", "json"])
def test_external_file_symlinks_are_rejected(
    store: VistaStore, png: bytes, tmp_path: Path, entity: str
) -> None:
    summary = store.save_record(png=png, label="linked", glsl="// code")
    data = record_json(store, summary["record"])
    filename = (
        store.root / "json" / summary["record"]
        if entity == "json"
        else store.root / data[entity]
    )
    outside = tmp_path / f"outside-{entity}"
    outside.write_bytes(filename.read_bytes())
    filename.unlink()
    filename.symlink_to(outside)
    with pytest.raises(VistaError, match="escapes"):
        store.read_record(summary["record"], "both")
    with pytest.raises(VistaError, match="escapes"):
        store.list_records()


@pytest.mark.parametrize("directory", ["png", "glsl", "json"])
def test_external_directory_symlinks_are_rejected(
    store: VistaStore, png: bytes, tmp_path: Path, directory: str
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (store.root / directory).rmdir()
    (store.root / directory).symlink_to(outside, target_is_directory=True)
    with pytest.raises(VistaError, match="escapes"):
        VistaStore(store.root)
    with pytest.raises(VistaError, match="escapes"):
        store.save_record(png=png, label="escape", glsl="// code")
    assert list(outside.iterdir()) == []


def test_internal_symlink_directory_is_usable(store: VistaStore, png: bytes) -> None:
    assets = store.root / "assets"
    assets.mkdir()
    (store.root / "png").rmdir()
    (store.root / "png").symlink_to(assets, target_is_directory=True)
    summary = store.save_record(png=png, label="inside")
    assert store.read_record(summary["record"]).image == png
    assert len(list(assets.iterdir())) == 1


def test_unusable_root_has_context(tmp_path: Path) -> None:
    target = tmp_path / "file"
    target.write_text("existing")
    with pytest.raises(VistaError, match="file") as error:
        VistaStore(target)
    assert error.value.__cause__ is not None
    assert target.read_text() == "existing"


@pytest.mark.parametrize("failure", ["sync", "sync_json", ".png", ".glsl", ".json"])
def test_failed_publication_cleans_own_files_and_preserves_existing(
    store: VistaStore, png: bytes, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    old = store.save_record(png=png, label="old", glsl="// old")
    before = {
        entry.relative_to(store.root): entry.read_bytes()
        for entry in store.root.rglob("*")
        if entry.is_file()
    }
    real_link = core.os.link
    real_sync = core.os.fsync
    sync_calls = 0

    def fail_link(source, destination, *args, **kwargs):
        if Path(destination).suffix == failure:
            raise OSError("injected publication failure")
        return real_link(source, destination, *args, **kwargs)

    def fail_sync(descriptor):
        nonlocal sync_calls
        sync_calls += 1
        if failure == "sync" or sync_calls == 3:
            raise OSError("injected sync failure")
        return real_sync(descriptor)

    if failure in ("sync", "sync_json"):
        monkeypatch.setattr(core.os, "fsync", fail_sync)
    else:
        monkeypatch.setattr(core.os, "link", fail_link)
    with pytest.raises(VistaError, match="injected") as error:
        store.save_record(png=png, label="new", glsl="// new")
    assert error.value.__cause__ is not None
    after = {
        entry.relative_to(store.root): entry.read_bytes()
        for entry in store.root.rglob("*")
        if entry.is_file()
    }
    assert after == before
    assert store.read_record(old["record"], "both").code == "// old"


def test_failed_record_keeps_reused_code(
    store: VistaStore, png: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = store.save_record(png=png, label="old", glsl="// shared")
    real_link = core.os.link

    def fail_json(source, destination, *args, **kwargs):
        if Path(destination).suffix == ".json":
            raise OSError("injected JSON failure")
        return real_link(source, destination, *args, **kwargs)

    monkeypatch.setattr(core.os, "link", fail_json)
    with pytest.raises(VistaError, match="JSON failure"):
        store.save_record(png=png, label="new", reuse_glsl_from=old["record"])
    assert store.list_records() == {"records": [old]}
    assert store.read_record(old["record"], "code").code == "// shared"
    assert len(list((store.root / "png").iterdir())) == 1
    assert not list(store.root.rglob("*.tmp"))


@pytest.mark.parametrize("collision", ["png", "glsl", "json"])
def test_name_collision_never_overwrites_existing_file(
    store: VistaStore, png: bytes, monkeypatch: pytest.MonkeyPatch, collision: str
) -> None:
    identifiers = iter(UUID(int=value) for value in (1, 2, 3))
    monkeypatch.setattr(core, "uuid4", lambda: next(identifiers))
    first = store.save_record(png=png, label="first", glsl="// first")
    values = {"png": (1, 5, 6), "glsl": (4, 5, 3), "json": (4, 2, 6)}
    identifiers = iter(UUID(int=value) for value in values[collision])
    with pytest.raises(VistaError, match="File exists"):
        store.save_record(png=make_png("blue"), label="second", glsl="// second")
    assert store.list_records() == {"records": [first]}
    assert store.read_record(first["record"], "both").image == png
    assert store.read_record(first["record"], "code").code == "// first"
    assert not list(store.root.rglob("*.tmp"))
    assert len(list((store.root / "png").iterdir())) == 1
    assert len(list((store.root / "glsl").iterdir())) == 1


def test_post_commit_cleanup_failure_keeps_successful_record(
    store: VistaStore, png: bytes, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    real_unlink = core.os.unlink

    def fail_temporary_unlink(filename, *args, **kwargs):
        if Path(filename).suffix == ".tmp":
            raise PermissionError("injected cleanup failure")
        return real_unlink(filename, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(core.os, "unlink", fail_temporary_unlink)
        summary = store.save_record(png=png, label="committed", glsl="// code")
    assert "Could not remove store file" in caplog.text
    assert store.list_records() == {"records": [summary]}
    assert store.read_record(summary["record"], "both").image == png
    assert store.read_record(summary["record"], "both").code == "// code"


def test_catalog_cannot_see_record_before_json_publication(
    store: VistaStore, png: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    json_ready = threading.Event()
    release_json = threading.Event()
    real_link = core.os.link

    def pause_json(source, destination, *args, **kwargs):
        if Path(destination).suffix == ".json":
            json_ready.set()
            if not release_json.wait(timeout=10):
                raise TimeoutError("JSON publication test timed out")
        return real_link(source, destination, *args, **kwargs)

    monkeypatch.setattr(core.os, "link", pause_json)
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(
            store.save_record, png=png, label="atomic", glsl="code"
        )
        try:
            assert json_ready.wait(timeout=10)
            assert list((store.root / "png").glob("*.png"))
            assert list((store.root / "glsl").glob("*.glsl"))
            assert store.list_records() == {"records": []}
        finally:
            release_json.set()
        summary = pending.result(timeout=10)
    assert store.list_records() == {"records": [summary]}
    assert store.read_record(summary["record"], "both").code == "code"


def save_from_process(root: str, number: int, png: bytes) -> dict:
    return VistaStore(root).save_record(
        png=png,
        label=f"record-{number}",
        glsl=f"// code {number}",
        render={"n": number},
    )


def test_multiple_processes_append_without_overwriting(
    store: VistaStore, png: bytes
) -> None:
    with ProcessPoolExecutor(
        max_workers=3, mp_context=multiprocessing.get_context("spawn")
    ) as executor:
        results = list(
            executor.map(save_from_process, [str(store.root)] * 8, range(8), [png] * 8)
        )
    assert len({item["record"] for item in results}) == 8
    assert len(store.list_records()["records"]) == 8
    for number, summary in enumerate(results):
        result = store.read_record(summary["record"], "both")
        assert result.image == png
        assert result.code == f"// code {number}"
        assert result.metadata["render"] == {"n": number}
    assert not list(store.root.rglob("*.tmp"))
