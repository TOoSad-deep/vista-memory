"""Task-scoped notes, budgeted directory views, and on-demand image reads."""

from __future__ import annotations

import json
import re
import sqlite3
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from secrets import token_urlsafe
from threading import Lock
from typing import Literal

from .core import RecordContent
from .history_store import (
    HistoryError,
    HistoryUnavailable,
    HistoryUnknownRecord,
    TaskHistory,
)

_SCOPED_SHORT_ID = re.compile(r"R[1-9][0-9]{0,18}\Z")
_MENTIONED_SHORT_ID = re.compile(r"(?<![A-Za-z0-9_])R[1-9][0-9]*(?![A-Za-z0-9_])")
_DISPLAY_CONTROLS = re.compile(r"[\x7f-\x9f\u2028-\u202e\u2066-\u2069]")
_NOTE_SOURCE = {"user": "用户评价", "agent": "Agent 观察"}


class HistoryNoteConflict(HistoryError):
    """A source event already has different note contents or a different target."""


class HistoryViewExpired(HistoryError):
    """The requested directory view does not exist in the bound task."""


class HistoryBudgetTooSmall(HistoryError):
    """The visible directory budget is too small to include its entry point."""


class HistoryReadLimit(HistoryError):
    """The current turn has exhausted its image-read allowance."""


class HistoryRepeatedRead(HistoryError):
    """The same read already failed during the current turn."""


@dataclass(frozen=True)
class NoteReceipt:
    source: Literal["user", "agent"]
    event_id: str
    target: str
    text: str
    compared_to: str | None = None
    short_text: str | None = None

    @property
    def receipt(self) -> str:
        return f"已记录 {self.target} 的{_NOTE_SOURCE[self.source]}"


@dataclass(frozen=True)
class CatalogView:
    """Only ``text`` belongs in the model request; the rest is host metadata."""

    view_id: str
    version: int
    text: str
    visible_short_ids: tuple[str, ...]
    covered_short_ids: tuple[str, ...]
    entries: dict[str, tuple[str, ...]]
    token_count: int
    token_count_kind: Literal["measured", "estimate"]


@dataclass(frozen=True)
class CatalogPage:
    text: str
    short_ids: tuple[str, ...]
    next_cursor: str | None
    notes: tuple[NoteReceipt, ...] = ()


@dataclass(frozen=True)
class _Image:
    short_id: str
    sequence: int
    label: str
    subtask_id: str | None
    subtask_label: str | None
    parent: str | None
    unavailable: bool
    notes: tuple[NoteReceipt, ...]


@dataclass(frozen=True)
class _ViewRow:
    text: str
    members: tuple[str, ...]
    visible: str
    target: str | None = None


def _literal(text: str) -> str:
    quoted = json.dumps(text, ensure_ascii=False)
    return _DISPLAY_CONTROLS.sub(lambda match: f"\\u{ord(match[0]):04x}", quoted)


def _next_cursor(view_id: str, target: str, end: int, size: int) -> str | None:
    return f"{view_id}/{target}/{end}" if end < size else None


def _offset(view_id: str, target: str, cursor: str | None) -> int:
    if cursor is None:
        return 0
    prefix = f"{view_id}/{target}/"
    if not cursor.startswith(prefix):
        raise HistoryViewExpired("视图已失效，请刷新目录")
    offset = cursor[len(prefix) :]
    if not re.fullmatch(r"[1-9][0-9]{0,18}", offset):
        raise HistoryError("无效的目录页游标，请刷新目录")
    return int(offset)


def _describe_note(note: NoteReceipt, *, original: bool = False) -> str:
    source = _NOTE_SOURCE[note.source]
    about = (
        f"{note.target} 相比 {note.compared_to}" if note.compared_to else note.target
    )
    if original:
        return f"{source} · {about} · {_literal(note.text)}"
    # Briefs may omit an important qualification. Only display verbatim text
    # in the short view and fall back to an expandable source label for long notes.
    short = note.text.strip()
    if note.short_text is not None and not note.compared_to:
        # An explicit target prefix is already rendered in `about`.
        preface = re.match(rf"{note.target}[\s:：]+", short)
        provided = note.short_text.strip()
        if preface is not None and provided == short[preface.end() :]:
            short = provided
    if len(short) <= 25:
        return f"{source} · {about} · {_literal(short)}"
    return f"{note.target} 有一条{source}，可展开"


def _describe_image(
    image: _Image, *, brief: bool = False, include_topic: bool = False
) -> str:
    parts = [image.short_id, _literal(image.label) if image.label else "已保存"]
    topic = image.subtask_label or image.subtask_id
    if include_topic and topic and topic not in image.label:
        parts.append(f"主题 {_literal(topic)}")
    if image.parent is not None and not brief:
        parts.append(f"上一版 {image.parent}")
    if image.unavailable:
        parts.append("原图不可用")
    if image.notes and not brief:
        parts.append(_describe_note(image.notes[0]))
    return " · ".join(parts)


def _rank(image: _Image, subtask_id: str | None) -> tuple[bool, int, int]:
    primary = {note.source for note in image.notes if note.target == image.short_id}
    note_rank = 0 if "user" in primary else 1 if "agent" in primary else 2
    return (
        subtask_id is None or image.subtask_id != subtask_id,
        note_rank,
        -image.sequence,
    )


def _rank_notes(notes: list[NoteReceipt], short_id: str) -> tuple[NoteReceipt, ...]:
    return tuple(
        sorted(notes, key=lambda note: (note.target != short_id, note.source != "user"))
    )


def _group_rows(images: list[_Image]) -> list[_ViewRow]:
    groups: dict[tuple[str, str | int], list[_Image]] = defaultdict(list)
    for image in images:
        key = (
            ("topic", image.subtask_id)
            if image.subtask_id is not None
            else ("range", (image.sequence - 1) // 10)
        )
        groups[key].append(image)

    rows = []
    for index, ((kind, key), members) in enumerate(groups.items(), start=1):
        entry = f"G{index}"
        representative = members[0]
        if kind == "topic":
            group_name = _literal(representative.subtask_label or str(key))
        else:
            start = int(key) * 10 + 1
            group_name = f"其他图片 R{start}–R{start + 9}"
        rows.append(
            _ViewRow(
                f"{entry} · {group_name} · {len(members)} 张 · "
                f"{_describe_image(representative)}",
                tuple(member.short_id for member in members),
                representative.short_id,
                entry,
            )
        )
    return rows


class HistoryCatalog:
    """Build and resolve views for an already bound TaskHistory."""

    def __init__(
        self,
        history: TaskHistory,
        *,
        count_tokens: Callable[[str], int] | None = None,
    ) -> None:
        self.history = history
        self._count_tokens = count_tokens
        with history.connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS history_notes (
                    note_id INTEGER PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    source TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    target TEXT NOT NULL,
                    text TEXT NOT NULL,
                    compared_to TEXT,
                    short_text TEXT,
                    UNIQUE (task_id, source, event_id)
                );
                CREATE TABLE IF NOT EXISTS history_views (
                    task_id TEXT NOT NULL,
                    view_id TEXT NOT NULL,
                    snapshot TEXT NOT NULL,
                    PRIMARY KEY (task_id, view_id)
                );
                CREATE TABLE IF NOT EXISTS history_unavailable_images (
                    task_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    PRIMARY KEY (task_id, sequence)
                );
                """)

    def append_user_feedback(
        self,
        event_id: str,
        text: str,
        *,
        target: str | None = None,
        reply_short_ids: Sequence[str] = (),
        compared_to: str | None = None,
        short_text: str | None = None,
    ) -> NoteReceipt | None:
        """Accept a trusted feedback event with a single explicit image target."""
        self._validate_note(event_id, text, short_text)
        mentioned = set(_MENTIONED_SHORT_ID.findall(text))
        replies = set(reply_short_ids)
        candidates = mentioned - {compared_to}
        if target is None:
            if len(replies) == 1:
                target = next(iter(replies))
            elif len(candidates) == 1:
                target = next(iter(candidates))
        if (
            target is None
            or (replies and target not in replies)
            or candidates - {target}
        ):
            # An altered replay must not silently bypass the original binding.
            with self.history.connect() as connection:
                if self._saved_note(connection, "user", event_id) is not None:
                    raise HistoryNoteConflict("反馈事件已绑定其他内容或图片")
            return None
        return self._append(
            NoteReceipt("user", event_id, target, text, compared_to, short_text)
        )

    def append_agent_observation(
        self,
        event_id: str,
        target: str,
        text: str,
        *,
        compared_to: str | None = None,
        short_text: str | None = None,
    ) -> NoteReceipt:
        """Store a host-attributed observation for an image the agent has viewed."""
        self._validate_note(event_id, text, short_text)
        return self._append(
            NoteReceipt("agent", event_id, target, text, compared_to, short_text)
        )

    @staticmethod
    def _validate_note(event_id: str, text: str, short_text: str | None) -> None:
        if not isinstance(event_id, str) or not event_id.strip():
            raise HistoryError("备注需要有效的来源事件 ID")
        if not isinstance(text, str) or not text.strip():
            raise HistoryError("备注原文不能为空")
        if short_text is not None and not isinstance(short_text, str):
            raise HistoryError("备注短句必须是文字")

    def _saved_note(
        self, connection: sqlite3.Connection, source: str, event_id: str
    ) -> NoteReceipt | None:
        row = connection.execute(
            """SELECT source, event_id, target, text, compared_to, short_text
               FROM history_notes WHERE task_id = ? AND source = ? AND event_id = ?""",
            (self.history.task_id, source, event_id),
        ).fetchone()
        return NoteReceipt(**dict(row)) if row is not None else None

    def _append(self, note: NoteReceipt) -> NoteReceipt:
        with self.history.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            saved = self._saved_note(connection, note.source, note.event_id)
            if saved is not None:
                if saved != note:
                    raise HistoryNoteConflict("反馈事件已绑定其他内容或图片")
                return saved
            self.history._lookup(connection, note.target)
            if note.compared_to is not None:
                if note.compared_to == note.target:
                    raise HistoryError("比较备注需要两个不同的图片短号")
                self.history._lookup(connection, note.compared_to)
            connection.execute(
                """INSERT INTO history_notes
                   (task_id, source, event_id, target, text, compared_to, short_text)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    self.history.task_id,
                    note.source,
                    note.event_id,
                    note.target,
                    note.text,
                    note.compared_to,
                    note.short_text,
                ),
            )
            self._bump_version(connection)
        return note

    def _bump_version(self, connection: sqlite3.Connection) -> None:
        connection.execute(
            "UPDATE history_tasks SET version = version + 1 WHERE task_id = ?",
            (self.history.task_id,),
        )

    def current_version(self) -> int:
        with self.history.connect() as connection:
            return self._version(connection)

    def _version(self, connection: sqlite3.Connection) -> int:
        row = connection.execute(
            "SELECT version FROM history_tasks WHERE task_id = ?",
            (self.history.task_id,),
        ).fetchone()
        return row["version"]

    def _snapshot(self, include_notes: bool) -> tuple[int, list[_Image]]:
        task_id = self.history.task_id
        with self.history.connect() as connection:
            connection.execute("BEGIN")
            version = self._version(connection)
            rows = connection.execute(
                """SELECT r.sequence, r.record, r.label, r.subtask_id, r.subtask_label,
                          r.parent_record, u.sequence IS NOT NULL AS unavailable
                   FROM history_records AS r
                   LEFT JOIN history_unavailable_images AS u
                       ON r.task_id = u.task_id AND r.sequence = u.sequence
                   WHERE r.task_id = ? AND r.confirmed = 1
                   ORDER BY r.sequence""",
                (task_id,),
            ).fetchall()
            notes: dict[str, list[NoteReceipt]] = defaultdict(list)
            if include_notes:
                for row in connection.execute(
                    """SELECT source, event_id, target, text, compared_to, short_text
                       FROM history_notes WHERE task_id = ? ORDER BY note_id DESC""",
                    (task_id,),
                ):
                    note = NoteReceipt(**dict(row))
                    notes[note.target].append(note)
                    if note.compared_to is not None:
                        notes[note.compared_to].append(note)

        short_ids = {row["record"]: f"R{row['sequence']}" for row in rows}
        images = []
        for row in rows:
            short_id = short_ids[row["record"]]
            images.append(
                _Image(
                    short_id=short_id,
                    sequence=row["sequence"],
                    label=row["label"],
                    subtask_id=row["subtask_id"],
                    subtask_label=row["subtask_label"],
                    parent=short_ids.get(row["parent_record"]),
                    unavailable=bool(row["unavailable"]),
                    notes=_rank_notes(notes[short_id], short_id),
                )
            )
        return version, images

    def _count(self, text: str) -> int:
        if self._count_tokens is None:
            return len(text.encode("utf-8"))
        count = self._count_tokens(text)
        if type(count) is not int or count < 0:
            raise HistoryError("目录计数器必须返回非负整数")
        return count

    def short_view(
        self,
        *,
        selected: str | None = None,
        subtask_id: str | None = None,
        budget: int = 400,
        grouped: bool = True,
        include_notes: bool = True,
    ) -> CatalogView:
        """Save a frozen view whose complete model-visible text fits the budget."""
        if type(budget) is not int or budget <= 0:
            raise HistoryBudgetTooSmall("目录预算必须为正整数")
        version, images = self._snapshot(include_notes)
        by_id = {image.short_id: image for image in images}
        if selected is not None and selected not in by_id:
            raise HistoryUnknownRecord(f"短号 {selected!r} 未知，请刷新目录核对编号")
        focus = (
            by_id[selected]
            if selected is not None
            else (images[-1] if images else None)
        )
        if subtask_id is None and selected is not None and focus is not None:
            subtask_id = focus.subtask_id
        others = sorted(
            (image for image in images if image is not focus),
            key=lambda image: _rank(image, subtask_id),
        )
        ordered = ([focus] if focus is not None else []) + others
        view_id = token_urlsafe(9)
        header = f"VISTA 目录 · view_id={view_id}\n展开时提供 view_id 和 target。"
        if not ordered:
            header += "\n当前任务暂无已登记图片。"
        rows = (
            _group_rows(others)
            if grouped
            else [
                _ViewRow(
                    _describe_image(image, include_topic=True),
                    (image.short_id,),
                    image.short_id,
                )
                for image in others
            ]
        )
        if focus is not None:
            marker = "当前图" if selected is not None else "最近保存"
            rows.insert(
                0,
                _ViewRow(
                    f"{marker} · {_describe_image(focus, include_topic=True)}",
                    (focus.short_id,),
                    focus.short_id,
                ),
            )
        view = self._fit_view(view_id, version, header, ordered, rows, budget, selected)
        snapshot = {
            "view": asdict(view),
            "selected": selected,
            "subtask_id": subtask_id,
            "budget": budget,
            "grouped": grouped,
            "include_notes": include_notes,
            "images": {key: asdict(image) for key, image in by_id.items()},
        }
        with self.history.connect() as connection:
            connection.execute(
                """INSERT INTO history_views (task_id, view_id, snapshot)
                   VALUES (?, ?, ?)""",
                (
                    self.history.task_id,
                    view_id,
                    json.dumps(snapshot, ensure_ascii=False),
                ),
            )
        return view

    def _fit_view(
        self,
        view_id: str,
        version: int,
        header: str,
        ordered: list[_Image],
        rows: list[_ViewRow],
        budget: int,
        selected: str | None,
    ) -> CatalogView:
        short_ids = tuple(image.short_id for image in ordered)
        remaining = dict.fromkeys(short_ids)
        chosen: list[_ViewRow] = []
        lines = [header]
        for index, row in enumerate(rows):
            # Reserve the remainder entry before admitting each complete row.
            tail = len(remaining) - len(row.members)
            suffix = [f"rest · 其余图片 · {tail} 张"] if tail else []
            if index == 0:
                marker = "当前图" if selected is not None else "最近保存"
                compact = f"{marker} · {row.visible} · 已保存"
                if ordered[0].unavailable:
                    compact += " · 原图不可用"
                choices = (row.text, compact)
            else:
                choices = (row.text,)
            for choice in choices:
                if self._count("\n".join([*lines, choice, *suffix])) <= budget:
                    lines.append(choice)
                    chosen.append(row)
                    for short_id in row.members:
                        del remaining[short_id]
                    break

        entries = {row.target: row.members for row in chosen if row.target is not None}
        if remaining:
            lines.append(f"rest · 其余图片 · {len(remaining)} 张")
            entries["rest"] = tuple(remaining)
        text = "\n".join(lines)
        count = self._count(text)
        if count > budget:
            raise HistoryBudgetTooSmall(f"目录入口至少需要 {count} token，请提高预算")
        return CatalogView(
            view_id=view_id,
            version=version,
            text=text,
            visible_short_ids=tuple(row.visible for row in chosen),
            covered_short_ids=short_ids,
            entries=entries,
            token_count=count,
            token_count_kind="measured"
            if self._count_tokens is not None
            else "estimate",
        )

    def _view_snapshot(self, view_id: str) -> dict:
        with self.history.connect() as connection:
            row = connection.execute(
                "SELECT snapshot FROM history_views WHERE task_id = ? AND view_id = ?",
                (self.history.task_id, view_id),
            ).fetchone()
        if row is None:
            raise HistoryViewExpired("视图已失效，请刷新目录")
        return json.loads(row["snapshot"])

    def get_view(self, view_id: str) -> CatalogView:
        """Resolve a model-visible view identifier in this task."""
        stored = self._view_snapshot(view_id)["view"]
        stored["visible_short_ids"] = tuple(stored["visible_short_ids"])
        stored["covered_short_ids"] = tuple(stored["covered_short_ids"])
        stored["entries"] = {key: tuple(ids) for key, ids in stored["entries"].items()}
        return CatalogView(**stored)

    def expand(
        self,
        *,
        view_id: str,
        target: str,
        cursor: str | None = None,
        page_size: int = 10,
    ) -> CatalogPage:
        """Expand a group, the remaining images, or a single image's original notes."""
        if type(page_size) is not int or page_size <= 0:
            raise HistoryError("目录分页大小必须为正整数")
        snapshot = self._view_snapshot(view_id)
        images = snapshot["images"]
        offset = _offset(view_id, target, cursor)
        notes = ()
        if target in images:
            image = _decode_image(images[target])
            total = len(image.notes)
            notes = image.notes[offset : offset + page_size]
            short_ids = (target,)
            lines = [f"{_describe_image(image, brief=True)} · {total} 条备注"]
            lines.extend(_describe_note(note, original=True) for note in notes)
        elif target in snapshot["view"]["entries"]:
            members = snapshot["view"]["entries"][target]
            total = len(members)
            short_ids = tuple(members[offset : offset + page_size])
            lines = [f"VISTA 目录 · view_id={view_id} · {target} · {total} 张"]
            lines.extend(
                _describe_image(_decode_image(images[key]), include_topic=True)
                for key in short_ids
            )
        elif _SCOPED_SHORT_ID.fullmatch(target):
            raise HistoryUnknownRecord(f"短号 {target!r} 未知，请刷新目录核对编号")
        else:
            raise HistoryError("目录入口未知，请刷新目录")
        if cursor is not None and offset >= total:
            raise HistoryError("目录页游标超出范围，请刷新目录")
        next_cursor = _next_cursor(view_id, target, offset + page_size, total)
        if next_cursor is not None:
            lines.append(f"下一页 cursor={next_cursor}")
        return CatalogPage("\n".join(lines), short_ids, next_cursor, notes)

    def new_turn(self, image_limit: int = 3) -> HistoryReadTurn:
        return HistoryReadTurn(self, image_limit)

    def _set_image_unavailable(self, short_id: str, unavailable: bool) -> None:
        key = self.history.task_id, int(short_id[1:])
        with self.history.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if unavailable:
                result = connection.execute(
                    "INSERT OR IGNORE INTO history_unavailable_images VALUES (?, ?)",
                    key,
                )
            else:
                result = connection.execute(
                    """DELETE FROM history_unavailable_images
                       WHERE task_id = ? AND sequence = ?""",
                    key,
                )
            if result.rowcount:
                self._bump_version(connection)


def _decode_image(stored: dict) -> _Image:
    return _Image(
        **{**stored, "notes": tuple(NoteReceipt(**note) for note in stored["notes"])}
    )


class HistoryReadTurn:
    """Budget and memoize failures for one user turn across tool calls."""

    def __init__(self, catalog: HistoryCatalog, image_limit: int) -> None:
        if type(image_limit) is not int or image_limit < 0:
            raise HistoryError("读图额度必须为非负整数")
        self._catalog = catalog
        self._image_limit = image_limit
        self._images_attempted = 0
        self._failed: set[tuple[str, str]] = set()
        self._lock = Lock()

    def read(
        self,
        short_id: str,
        content: Literal["image", "code", "both"] = "image",
    ) -> RecordContent:
        if content not in ("image", "code", "both"):
            raise HistoryError("读取内容必须是 image、code 或 both")
        image_requested = content in ("image", "both")
        key = short_id, content
        with self._lock:
            if key in self._failed:
                raise HistoryRepeatedRead(f"本轮读取 {short_id} 已失败，请勿重复请求")
            if image_requested and self._images_attempted >= self._image_limit:
                raise HistoryReadLimit("本轮读图额度已用完，请停止读图")
            try:
                self._catalog.history.get(short_id)
                if image_requested:
                    self._images_attempted += 1
                result = self._catalog.history.read(short_id, content)
            except HistoryUnavailable as error:
                if error.image_unavailable:
                    self._catalog._set_image_unavailable(short_id, True)
                self._failed.add(key)
                raise
            except HistoryError:
                self._failed.add(key)
                raise
            if image_requested:
                self._catalog._set_image_unavailable(short_id, False)
            return result
