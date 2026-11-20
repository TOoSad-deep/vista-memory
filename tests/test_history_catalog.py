"""Task attachment, stable navigation, and agent-facing image read limits."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from PIL import Image

from vista_memory import VistaStore
from vista_memory.history_catalog import (
    HistoryBudgetTooSmall,
    HistoryCatalog,
    HistoryNoteConflict,
    HistoryReadLimit,
    HistoryRepeatedRead,
    HistoryViewExpired,
)
from vista_memory.history_store import (
    HistoryError,
    HistoryUnavailable,
    HistoryUnknownRecord,
    TaskHistory,
)


@pytest.fixture
def task(tmp_path: Path) -> TaskHistory:
    return TaskHistory(
        VistaStore(tmp_path / "records"), tmp_path / "tasks", task_id="A"
    )


@pytest.fixture
def png() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (8, 6), "orange").save(output, format="PNG")
    return output.getvalue()


def test_feedback_has_one_explicit_target_and_is_idempotent_after_restart(
    task: TaskHistory, png: bytes
) -> None:
    task.save("save-1", png=png, label="两个相似版本")
    task.save("save-2", png=png, label="两个相似版本")
    catalog = HistoryCatalog(task)
    initial = catalog.current_version()
    assert catalog.append_user_feedback("ambiguous", "刚才那张不错") is None
    assert catalog.append_user_feedback("multiple", "R1 和 R2 都不错") is None
    assert (
        catalog.append_user_feedback("mismatch", "R1 可以", reply_short_ids=("R2",))
        is None
    )
    assert catalog.current_version() == initial

    first = catalog.append_user_feedback("explicit", "R2 的宽度刚好")
    assert first is not None and first.target == "R2" and first.source == "user"
    assert (
        catalog.append_user_feedback("action", "边缘可以", target="R1").target == "R1"
    )
    assert (
        catalog.append_user_feedback(
            "reply", "边缘可以", reply_short_ids=("R1",)
        ).target
        == "R1"
    )
    comparison = catalog.append_agent_observation(
        "explicit", "R2", "R2 比 R1 平滑", compared_to="R1"
    )
    assert comparison.source == "agent"
    assert catalog.current_version() == initial + 4

    reopened = HistoryCatalog(TaskHistory(task.store, task.task_dir, task_id="A"))
    assert reopened.append_user_feedback("explicit", "R2 的宽度刚好") == first
    assert reopened.current_version() == initial + 4
    with pytest.raises(HistoryNoteConflict):
        reopened.append_user_feedback("explicit", "R1 的宽度刚好")
    with pytest.raises(HistoryNoteConflict):
        reopened.append_user_feedback("explicit", "刚才那张不错")
    view = reopened.short_view()
    assert reopened.expand(view_id=view.view_id, target="R2").notes == (
        first,
        comparison,
    )
    assert comparison in reopened.expand(view_id=view.view_id, target="R1").notes

    other = HistoryCatalog(TaskHistory(task.store, task.task_dir, task_id="B"))
    with pytest.raises(HistoryUnknownRecord):
        other.append_agent_observation("explicit", "R2", "宽度刚好")
    with pytest.raises(HistoryViewExpired, match="视图已失效"):
        other.expand(view_id=view.view_id, target="R2")


def test_short_views_keep_every_image_reachable_and_pin_old_pagination(
    task: TaskHistory, png: bytes
) -> None:
    catalog = HistoryCatalog(task, count_tokens=len)
    empty = catalog.short_view()
    assert empty.visible_short_ids == empty.covered_short_ids == ()
    assert "暂无" in empty.text
    with pytest.raises(HistoryBudgetTooSmall):
        catalog.short_view(budget=1)

    legacy = task.store.save_record(png=png, label="旧定稿")
    assert task.import_record("import", legacy["record"]).short_id == "R1"
    one = catalog.short_view()
    assert one.visible_short_ids == ("R1",)
    assert "最近保存 · R1" in one.text and "旧定稿" in one.text

    for sequence in range(2, 21):
        if sequence == 3:
            with pytest.raises(HistoryError):
                task.save("pending", png=b"incomplete", label="预留后写入失败")
            continue
        if sequence <= 4:
            metadata = {}
        elif sequence == 8 or sequence >= 14:
            metadata = {"subtask_id": f"long-{sequence}", "subtask_label": "长" * 300}
        else:
            metadata = {"subtask_id": "spacing", "subtask_label": "留白试验"}
        task.save(f"save-{sequence}", png=png, label="相似版本", **metadata)

    old_view = catalog.short_view(selected="R2", budget=180)
    assert old_view.view_id in old_view.text
    assert old_view.token_count == len(old_view.text) <= 180
    assert old_view.token_count_kind == "measured"
    assert old_view.visible_short_ids[0] == "R2"
    assert "其他图片 R1–R10 · 2 张" in old_view.text  # R3 missing and R2 standalone.
    assert '"留白试验" · 8 张' in old_view.text
    assert "R8" in old_view.entries["rest"]
    first = catalog.expand(view_id=old_view.view_id, target="rest", page_size=2)
    assert first.next_cursor and old_view.view_id in first.next_cursor

    new_view = catalog.short_view(selected="R8", budget=180)
    assert old_view.version == new_view.version and old_view.view_id != new_view.view_id
    assert new_view.visible_short_ids[0] == "R8"
    assert "R8" not in new_view.entries["rest"]
    second = catalog.expand(
        view_id=old_view.view_id, target="rest", cursor=first.next_cursor, page_size=2
    )
    assert second.short_ids == old_view.entries["rest"][2:4]
    with pytest.raises(HistoryViewExpired):
        catalog.expand(
            view_id=new_view.view_id, target="rest", cursor=first.next_cursor
        )

    for view in (old_view, new_view):
        seen: list[str] = []
        for entry, expected in view.entries.items():
            cursor = None
            collected: list[str] = []
            while True:
                page = catalog.expand(
                    view_id=view.view_id, target=entry, cursor=cursor, page_size=2
                )
                collected.extend(page.short_ids)
                cursor = page.next_cursor
                if cursor is None:
                    break
            assert tuple(collected) == expected
            seen.extend(collected)
        seen.extend(
            short_id for short_id in view.visible_short_ids if short_id not in seen
        )
        assert len(seen) == len(set(seen)) == 19
        assert set(seen) == {f"R{i}" for i in range(1, 21) if i != 3}
        assert set(view.covered_short_ids) == set(seen)
        assert catalog.get_view(view.view_id) == view
    estimate = HistoryCatalog(task).short_view()
    assert estimate.token_count_kind == "estimate"
    assert estimate.token_count == len(estimate.text.encode("utf-8")) <= 400


def test_note_rank_fallback_and_frozen_original_text(
    task: TaskHistory, png: bytes
) -> None:
    for sequence in range(1, 7):
        subtask_id = "same" if sequence in {1, 2, 6} else None
        task.save(
            f"event-{sequence}",
            png=png,
            label=f"版本 {sequence}",
            subtask_id=subtask_id,
        )
    catalog = HistoryCatalog(task, count_tokens=len)
    long_note = "外圈宽度可以。" + "高光仍需改，尤其是右下角。" * 3 + "\n要保留细边。"
    catalog.append_user_feedback("u1", long_note, target="R1", short_text="认可这版")
    catalog.append_agent_observation("a1", "R1", "较新的观察")
    catalog.append_agent_observation("a2", "R2", "留白均匀")
    catalog.append_user_feedback("u3", "边缘太钝", target="R3")
    catalog.append_agent_observation("a5", "R5", "留白均匀")

    a = catalog.short_view(selected="R6", grouped=False, include_notes=False)
    b1 = catalog.short_view(selected="R6", grouped=True, include_notes=False)
    b2 = catalog.short_view(selected="R6", grouped=False, include_notes=True)
    assert (
        a.covered_short_ids
        == b1.covered_short_ids
        == ("R6", "R2", "R1", "R5", "R4", "R3")
    )
    assert b2.covered_short_ids == ("R6", "R1", "R2", "R3", "R5", "R4")
    assert "R1 有一条用户评价" in b2.text
    assert "认可这版" not in b2.text
    assert "较新的观察" not in b2.text
    assert catalog.expand(view_id=b1.view_id, target="R1").notes == ()

    first = catalog.expand(view_id=b2.view_id, target="R1", page_size=1)
    assert first.next_cursor
    assert first.notes[0].text == long_note
    assert first.notes[0].short_text == "认可这版"
    assert json.loads(first.text.splitlines()[-2].split(" · ", 2)[2]) == long_note
    # New remarks and views do not move the old note page.
    catalog.append_user_feedback("new", "补充：扫光需要更窄", target="R1")
    second = catalog.expand(
        view_id=b2.view_id, target="R1", page_size=1, cursor=first.next_cursor
    )
    assert [note.event_id for note in second.notes] == ["a1"]
    assert second.next_cursor is None


def test_image_reads_preserve_scope_budget_and_unavailability(
    task: TaskHistory, png: bytes
) -> None:
    record = task.save(
        "save", png=png, label="草稿", glsl="void main() {}", render={"preset": 5}
    )
    data = json.loads((task.store.root / "json" / record.record).read_text())
    image_path = task.store.root / data["png"]
    catalog = HistoryCatalog(task)
    turn = catalog.new_turn(image_limit=1)
    with pytest.raises(HistoryUnknownRecord, match="刷新目录"):
        turn.read("R2")
    with pytest.raises(HistoryRepeatedRead):
        turn.read("R2")
    content = turn.read("R1", "both")
    assert content.image == png
    assert content.code == "void main() {}"
    assert content.metadata["render"] == {"preset": 5}
    with pytest.raises(HistoryReadLimit, match="停止读图"):
        turn.read("R1")
    assert turn.read("R1", "code").image is None

    image_path.unlink()
    broken = catalog.new_turn()
    with pytest.raises(HistoryUnavailable, match="原图不可用"):
        broken.read("R1")
    with pytest.raises(HistoryRepeatedRead):
        broken.read("R1")
    marked = catalog.short_view()
    assert "原图不可用" in marked.text

    image_path.write_bytes(png)
    assert catalog.new_turn().read("R1").image == png
    assert "原图不可用" not in catalog.short_view().text
    (task.store.root / data["glsl"]).unlink()
    with pytest.raises(HistoryUnavailable) as unavailable:
        catalog.new_turn().read("R1", "both")
    assert not unavailable.value.image_unavailable
    assert "原图不可用" not in catalog.short_view().text
