"""Exercise read failure results at the Python boundary used by the DSH tools."""

import io

import pytest
from PIL import Image

from scripts.vista_history_dsh_bridge import Bridge
from vista_memory.history_catalog import HistoryReadLimit, HistoryRepeatedRead
from vista_memory.history_store import HistoryUnavailable, HistoryUnknownRecord


def test_native_bridge_returns_short_id_failures_and_limits_per_turn(tmp_path):
    bridge = Bridge(
        {
            "vistaRoot": str(tmp_path / "records"),
            "taskDir": str(tmp_path / "task"),
            "taskId": "native-dsh",
        }
    )
    image = io.BytesIO()
    Image.new("RGB", (4, 4), color="steelblue").save(image, format="PNG")
    bridge.history.save("one", png=image.getvalue(), label="留白初稿")

    def read(short_id, turn=1):
        return bridge.run("read", {"short_id": short_id, "turn": turn, "limit": 1})

    with pytest.raises(HistoryUnknownRecord, match="刷新目录"):
        read("R9")
    with pytest.raises(HistoryRepeatedRead):
        read("R9")
    assert read("R1")["image"]
    with pytest.raises(HistoryReadLimit, match="停止读图"):
        read("R1")

    next((tmp_path / "records" / "png").glob("*.png")).unlink()
    with pytest.raises(HistoryUnavailable, match="原图不可用"):
        read("R1", turn=2)


def test_native_bridge_uses_named_short_id_for_identical_reply_images(tmp_path):
    bridge = Bridge(
        {
            "vistaRoot": str(tmp_path / "records"),
            "taskDir": str(tmp_path / "task"),
            "taskId": "native-dsh",
        }
    )
    image = io.BytesIO()
    Image.new("RGB", (4, 4), color="steelblue").save(image, format="PNG")
    bridge.history.save("one", png=image.getvalue(), label="留白初稿")
    bridge.history.save("two", png=image.getvalue(), label="留白定稿")
    candidates = {"reply_short_ids": ["R1", "R2"]}
    assert not bridge.run(
        "user_feedback", {"event_id": "implicit", "text": "这张不错", **candidates}
    )
    assert bridge.run(
        "user_feedback", {"event_id": "named", "text": "R2 的留白刚好", **candidates}
    )
    view = bridge.catalog.short_view()
    assert not bridge.catalog.expand(view_id=view.view_id, target="R1").notes
    notes = bridge.catalog.expand(view_id=view.view_id, target="R2").notes
    assert [(note.event_id, note.target) for note in notes] == [("named", "R2")]
