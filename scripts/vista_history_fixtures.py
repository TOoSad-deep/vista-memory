"""Freeze synthetic icon tasks for the native DSH image-history experiment."""

from __future__ import annotations

import hashlib
import io
from dataclasses import asdict, dataclass
from pathlib import Path

from PIL import Image, ImageDraw

SUMMARY = "此前做过几版图标，这轮同时看当前版本。"
FEEDBACK = "这一版的留白好像又大了。"


@dataclass(frozen=True)
class Icon:
    label: str
    topic: str
    direction: str
    inset: int = 28
    stroke: int = 18


ANGLES = {"右": 0, "下": 90, "左": 180, "上": 270}


def icon_png(icon: Icon, color: tuple[int, int, int]) -> bytes:
    image = Image.new("RGB", (192, 192), "white")
    bounds = (icon.inset, icon.inset, 191 - icon.inset, 191 - icon.inset)
    angle = ANGLES[icon.direction]
    ImageDraw.Draw(image).arc(bounds, angle + 32, angle + 328, color, icon.stroke)
    png = io.BytesIO()
    image.save(png, format="PNG")
    return png.getvalue()


def freeze_task(root: Path, index: int) -> dict:
    from vista_memory import VistaStore
    from vista_memory.history_catalog import HistoryCatalog
    from vista_memory.history_store import TaskHistory

    name, fold_topic, current_color, color, target_dir, folded_dir = (
        ("潮汐", "潮位标记", "蓝", (31, 94, 184), "下", "右"),
        ("泊位", "港口标记", "绿", (21, 127, 85), "左", "上"),
    )[index]
    task_id = f"set-{index + 1}"
    root.mkdir(parents=True, exist_ok=False)
    store = VistaStore(root / "records")
    history = TaskHistory(store, root / "task", task_id=task_id)
    catalog = HistoryCatalog(history)

    detail_topic = f"{name}光影校准"
    highlight = Icon(f"{detail_topic} · 校准稿甲", detail_topic, target_dir, 31)
    edge = Icon(
        f"{detail_topic} · 校准稿乙", detail_topic, "上" if index == 0 else "右", 33
    )
    lead, follow = (highlight, edge) if index == 0 else (edge, highlight)
    icons = [
        Icon(f"{name}图标 · 初稿", f"{name}图标", "上", 30),
        lead,
        Icon(f"{name}图标 · 定稿", f"{name}图标", target_dir, 35),
        Icon(f"{fold_topic} · 细笔画初稿", fold_topic, folded_dir, 38, 9),
        follow,
        Icon(f"{fold_topic} · 粗笔画新稿", fold_topic, "下", 33, 24),
    ]
    fillers = ("方向识别", "对比度校对", "边界过渡", "缩放适配", "空白一致性")
    for number in range(35):
        topic = f"{name}{fillers[number % len(fillers)]}"
        icons.append(
            Icon(
                f"{topic} · 操作界面预览方案{number + 1}",
                topic,
                tuple(ANGLES)[number % len(ANGLES)],
                22 + number % 9,
                10 + number // 4,
            )
        )
    # A second member makes the folded target's topic visible through its newer
    # representative, even when the detailed row falls out of the flat window.
    icons.append(Icon(f"{fold_topic} · 最近修改稿", fold_topic, "左", 34, 25))
    icons.append(Icon(f"{name}图标 · 当前稿", f"{name}图标", "右", 47, 13))

    records, assets = {}, root / "assets"
    assets.mkdir()
    for sequence, icon in enumerate(icons, 1):
        png = icon_png(icon, color)
        record = history.save(
            f"fixture-{sequence}",
            png=png,
            label=icon.label,
            subtask_id=icon.topic,
            subtask_label=icon.topic,
        )
        path = assets / f"{record.short_id}.png"
        path.write_bytes(png)
        values = asdict(icon)
        values.update(record=record.record, sha256=hashlib.sha256(png).hexdigest())
        records[record.short_id] = values

    highlight_id, edge_id = ("R2", "R5") if index == 0 else ("R5", "R2")
    current = f"R{len(icons)}"
    catalog.append_user_feedback(
        "highlight-feedback", "高光这版留白刚好。", target=highlight_id
    )
    catalog.append_user_feedback("edge-feedback", "这张是边缘校准稿。", target=edge_id)
    catalog.append_agent_observation(
        "edge-observation", edge_id, "边缘宽度可作比较。", compared_to=highlight_id
    )

    cases = [
        dict(
            id="visual",
            question=f"我们保存的{name}图标定稿，缺口朝哪边？",
            target="R3",
            answer=target_dir,
        ),
        dict(
            id="text",
            question=f"当前{name}图标已定为{current_color}色，请确认颜色。",
            answer=current_color,
        ),
        dict(
            id="folded",
            question=f"{fold_topic}最初那版细笔画的缺口朝哪边？",
            target="R4",
            answer=folded_dir,
        ),
        dict(
            id="similar",
            question=f"{name}图标那轮专门讨论高光的校准稿，缺口朝哪边？",
            target=highlight_id,
            answer=target_dir,
        ),
        dict(
            id="absent-text",
            question="紫色版已经取消且没有保存。旧图里有紫色定稿吗？",
            answer="无",
        ),
        dict(
            id="absent-near",
            question=f"我们保存过中心带星形的{name}图标定稿吗？想确认有几个尖角。",
            answer="无",
            relevant=["R1", "R3"],
        ),
        dict(id="delivery", question=FEEDBACK, target=highlight_id, answer=None),
    ]
    return {
        "task": task_id,
        "current": current,
        "current_topic": records[current]["topic"],
        "records": records,
        "checkpoint": SUMMARY,
        "cases": cases,
    }
