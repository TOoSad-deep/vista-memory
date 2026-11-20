"""Task-local short IDs and recoverable registration of VISTA records."""

from __future__ import annotations

import fcntl
import json
import os
import re
import sqlite3
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid4

from vista_memory import _publication
from vista_memory.core import (
    RecordContent,
    VistaError,
    VistaStore,
    _ImageUnavailable,
    _json_snapshot,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS history_tasks (
    task_id TEXT PRIMARY KEY,
    store_root TEXT NOT NULL,
    next_sequence INTEGER NOT NULL DEFAULT 1,
    version INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS history_records (
    task_id TEXT NOT NULL REFERENCES history_tasks (task_id),
    sequence INTEGER NOT NULL,
    request_id TEXT NOT NULL,
    record TEXT NOT NULL,
    label TEXT NOT NULL,
    subtask_id TEXT,
    subtask_label TEXT,
    parent_record TEXT,
    fingerprint TEXT NOT NULL,
    manifest TEXT,
    confirmed INTEGER NOT NULL,
    PRIMARY KEY (task_id, sequence),
    UNIQUE (task_id, request_id),
    UNIQUE (task_id, record)
);
"""


class HistoryError(VistaError):
    """A task history request could not be completed."""


class HistoryConflict(HistoryError):
    """A registration request or its files conflict with saved state."""


class HistoryUnknownRecord(HistoryError):
    """The short ID has no confirmed mapping in this task."""


class HistoryUnavailable(HistoryError):
    """The mapped record or requested content is unavailable."""

    def __init__(self, short_id: str, *, image_unavailable: bool):
        self.short_id = short_id
        self.image_unavailable = image_unavailable
        item = "原图" if image_unavailable else "记录或关联内容"
        super().__init__(f"{short_id} 的{item}不可用")


@dataclass(frozen=True)
class HistoryRecord:
    short_id: str
    sequence: int
    request_id: str
    record: str
    label: str
    subtask_id: str | None
    subtask_label: str | None
    parent_record: str | None

    @property
    def receipt(self) -> str:
        label = " ".join(self.label.splitlines()).strip()
        return f"已存 {self.short_id} · {label}" if label else f"已存 {self.short_id}"


def _history_record(row: sqlite3.Row) -> HistoryRecord:
    sequence = row["sequence"]
    return HistoryRecord(
        f"R{sequence}",
        sequence,
        row["request_id"],
        row["record"],
        row["label"],
        row["subtask_id"],
        row["subtask_label"],
        row["parent_record"],
    )


def _encode(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _fingerprint(value: dict[str, Any]) -> str:
    return sha256(_encode(value).encode("utf-8")).hexdigest()


def _required_text(name: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise HistoryError(f"{name} must be a non-empty string")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise HistoryError(f"{name} must be valid UTF-8 text") from error


def _validate_registration(
    request_id: str, subtask_id: str | None, subtask_label: str | None
) -> None:
    _required_text("request_id", request_id)
    if subtask_id is not None:
        _required_text("subtask_id", subtask_id)
    if subtask_label is not None:
        _required_text("subtask_label", subtask_label)


class TaskHistory:
    """Bind short IDs to an explicit task and a VISTA store.

    The caller controls the task directory and reuses the request ID on retries.
    The directory keeps the SQLite catalog and per-request advisory lock files.
    """

    def __init__(self, store: VistaStore, task_dir: str | Path, *, task_id: str):
        _required_text("task_id", task_id)
        self.store = store
        self.task_id = task_id
        self.task_dir = Path(task_dir).expanduser().resolve()
        self._database = self.task_dir / "history.sqlite3"
        self._locks = self.task_dir / "history-locks"
        try:
            self._locks.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise HistoryError(
                f"Cannot create task directory {self.task_dir}"
            ) from error
        with self.connect() as connection:
            connection.executescript(_SCHEMA)
            connection.execute(
                "INSERT OR IGNORE INTO history_tasks (task_id, store_root) "
                "VALUES (?, ?)",
                (task_id, str(store.root)),
            )
            task = connection.execute(
                "SELECT store_root FROM history_tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if task["store_root"] != str(store.root):
                raise HistoryConflict(f"Task {task_id!r} is bound to another store")

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """Open a connection for an explicit task history transaction."""
        try:
            connection = sqlite3.connect(
                self._database, timeout=30, isolation_level=None
            )
            try:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA foreign_keys = ON")
                with connection:
                    yield connection
            finally:
                connection.close()
        except sqlite3.Error as error:
            raise HistoryError(f"Task history database error: {error}") from error

    @contextmanager
    def _request_lock(self, request_id: str) -> Iterator[None]:
        lock_name = _fingerprint({"task_id": self.task_id, "request_id": request_id})
        try:
            descriptor = os.open(
                self._locks / lock_name,
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0),
                0o600,
            )
        except OSError as error:
            raise HistoryError(f"Cannot lock history request {request_id!r}") from error
        try:
            try:
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    raise OSError("Request lock is not a regular file")
                fcntl.flock(descriptor, fcntl.LOCK_EX)
            except OSError as error:
                raise HistoryError(
                    f"Cannot lock history request {request_id!r}"
                ) from error
            yield
        finally:
            os.close(descriptor)

    def _lookup(self, connection: sqlite3.Connection, short_id: str) -> sqlite3.Row:
        if isinstance(short_id, str) and re.fullmatch(r"R[1-9][0-9]{0,18}", short_id):
            sequence = int(short_id[1:])
            if sequence < 2**63:
                row = connection.execute(
                    """SELECT * FROM history_records
                       WHERE task_id = ? AND sequence = ? AND confirmed = 1""",
                    (self.task_id, sequence),
                ).fetchone()
                if row is not None:
                    return row
        raise HistoryUnknownRecord(f"{short_id!r} 不在当前任务，请刷新目录核对编号")

    def _by_request(
        self, connection: sqlite3.Connection, request_id: str, fingerprint: str
    ) -> sqlite3.Row | None:
        row = connection.execute(
            "SELECT * FROM history_records WHERE task_id = ? AND request_id = ?",
            (self.task_id, request_id),
        ).fetchone()
        if row is not None and row["fingerprint"] != fingerprint:
            raise HistoryConflict(f"Different input for history request {request_id!r}")
        return row

    def _register(
        self,
        request_id: str,
        fingerprint: str,
        record: str,
        label: str,
        subtask_id: str | None,
        subtask_label: str | None,
        parent_short_id: str | None,
        *,
        confirmed: bool,
    ) -> sqlite3.Row:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._by_request(connection, request_id, fingerprint)
            parent_record = None
            if parent_short_id is not None:
                parent_record = self._lookup(connection, parent_short_id)["record"]
            if row is not None:
                if (
                    row["label"],
                    row["subtask_id"],
                    row["subtask_label"],
                    row["parent_record"],
                ) != (label, subtask_id, subtask_label, parent_record):
                    raise HistoryConflict(
                        f"Registration differs for history request {request_id!r}"
                    )
                return row
            previous = connection.execute(
                "SELECT request_id FROM history_records "
                "WHERE task_id = ? AND record = ?",
                (self.task_id, record),
            ).fetchone()
            if previous is not None:
                raise HistoryConflict(f"Record {record!r} is already registered")
            sequence = connection.execute(
                "SELECT next_sequence FROM history_tasks WHERE task_id = ?",
                (self.task_id,),
            ).fetchone()["next_sequence"]
            connection.execute(
                """
                INSERT INTO history_records
                    (task_id, sequence, request_id, record, label, subtask_id,
                     subtask_label, parent_record, fingerprint, confirmed)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self.task_id,
                    sequence,
                    request_id,
                    record,
                    label,
                    subtask_id,
                    subtask_label,
                    parent_record,
                    fingerprint,
                    int(confirmed),
                ),
            )
            connection.execute(
                """UPDATE history_tasks SET next_sequence = next_sequence + 1,
                          version = version + ? WHERE task_id = ?""",
                (int(confirmed), self.task_id),
            )
            return connection.execute(
                "SELECT * FROM history_records WHERE task_id = ? AND sequence = ?",
                (self.task_id, sequence),
            ).fetchone()

    def _persist_manifest(
        self, request_id: str, manifest: _publication.Manifest
    ) -> None:
        with self.connect() as connection:
            updated = connection.execute(
                """UPDATE history_records SET manifest = ?
                   WHERE task_id = ? AND request_id = ? AND confirmed = 0""",
                (manifest.encode(), self.task_id, request_id),
            ).rowcount
            if updated != 1:
                raise HistoryConflict(f"Reservation changed for request {request_id!r}")

    def _confirm(self, request_id: str) -> None:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                """UPDATE history_records SET confirmed = 1
                   WHERE task_id = ? AND request_id = ? AND confirmed = 0""",
                (self.task_id, request_id),
            ).rowcount
            if updated:
                connection.execute(
                    "UPDATE history_tasks SET version = version + 1 WHERE task_id = ?",
                    (self.task_id,),
                )
            else:
                confirmed = connection.execute(
                    """SELECT confirmed FROM history_records
                       WHERE task_id = ? AND request_id = ?""",
                    (self.task_id, request_id),
                ).fetchone()
                if confirmed is None or not confirmed["confirmed"]:
                    raise HistoryConflict(
                        f"Reservation changed for request {request_id!r}"
                    )

    def save(
        self,
        request_id: str,
        *,
        png: bytes,
        label: str,
        glsl: str | None = None,
        reuse_glsl_from: str | None = None,
        source_agent: str | None = None,
        render: dict[str, Any] | None = None,
        subtask_id: str | None = None,
        subtask_label: str | None = None,
        parent_short_id: str | None = None,
    ) -> HistoryRecord:
        """Create or complete a registration using a stable request ID."""
        _validate_registration(request_id, subtask_id, subtask_label)
        if not isinstance(png, bytes):
            raise HistoryError("png must contain PNG bytes")
        if not isinstance(label, str):
            raise HistoryError("label must be a string")
        for name, value in (
            ("glsl", glsl),
            ("reuse_glsl_from", reuse_glsl_from),
            ("source_agent", source_agent),
            ("parent_short_id", parent_short_id),
        ):
            if value is not None and not isinstance(value, str):
                raise HistoryError(f"{name} must be a string or None")
        if glsl is not None and reuse_glsl_from is not None:
            raise HistoryError("glsl and reuse_glsl_from are mutually exclusive")
        try:
            snapshot = (
                json.loads(_encode(_json_snapshot(render)))
                if render is not None
                else None
            )
            fingerprint = _fingerprint(
                {
                    "action": "save",
                    "png": sha256(png).hexdigest(),
                    "label": label,
                    "glsl": sha256(glsl.encode("utf-8")).hexdigest()
                    if glsl is not None
                    else None,
                    "reuse_glsl_from": reuse_glsl_from,
                    "source_agent": source_agent,
                    "render": snapshot,
                    "subtask_id": subtask_id,
                    "subtask_label": subtask_label,
                    "parent_short_id": parent_short_id,
                }
            )
        except (TypeError, ValueError) as error:
            raise HistoryError(f"Invalid history input: {error}") from error

        with self._request_lock(request_id):
            row = self._register(
                request_id,
                fingerprint,
                f"record-{uuid4().hex}.json",
                label,
                subtask_id,
                subtask_label,
                parent_short_id,
                confirmed=False,
            )
            try:
                manifest = (
                    _publication.Manifest.decode(row["manifest"])
                    if row["manifest"]
                    else None
                )
            except _publication.PublicationConflict as error:
                raise HistoryConflict(str(error)) from error
            self._publish(
                row, manifest, png, glsl, reuse_glsl_from, source_agent, snapshot
            )
            self._confirm(request_id)
            return _history_record(row)

    def _publish(
        self,
        row: sqlite3.Row,
        manifest: _publication.Manifest | None,
        png: bytes,
        glsl: str | None,
        reuse_glsl_from: str | None,
        source_agent: str | None,
        render: dict[str, Any] | None,
    ) -> None:
        def persist(updated: _publication.Manifest) -> None:
            self._persist_manifest(row["request_id"], updated)

        try:
            self.store.save_record(
                png=png,
                label=row["label"],
                glsl=glsl,
                reuse_glsl_from=reuse_glsl_from,
                source_agent=source_agent,
                render=render,
                record_id=UUID(
                    row["record"].removeprefix("record-").removesuffix(".json")
                ),
                _pending=_publication.PendingPublication(
                    manifest, persist, bool(row["confirmed"])
                ),
            )
        except VistaError as error:
            if isinstance(error.__cause__, _publication.PublicationConflict):
                raise HistoryConflict(str(error)) from error
            if isinstance(error.__cause__, HistoryError):
                raise error.__cause__
            raise HistoryError(str(error)) from error

    def import_record(
        self,
        request_id: str,
        record: str,
        *,
        subtask_id: str | None = None,
        subtask_label: str | None = None,
        parent_short_id: str | None = None,
    ) -> HistoryRecord:
        """Register an existing record under this task without copying its contents."""
        _validate_registration(request_id, subtask_id, subtask_label)
        _required_text("record", record)
        fingerprint = _fingerprint(
            {
                "action": "import",
                "record": record,
                "subtask_id": subtask_id,
                "subtask_label": subtask_label,
                "parent_short_id": parent_short_id,
            }
        )
        with self._request_lock(request_id):
            with self.connect() as connection:
                prior = self._by_request(connection, request_id, fingerprint)
            if prior is not None:
                item = _history_record(prior)
                saved = self.read(item.short_id, "both")
                if item.record != record or item.label != saved.metadata["label"]:
                    raise HistoryConflict(
                        f"Imported record differs for history request {request_id!r}"
                    )
                return item
            try:
                content = self.store.read_record(record, "both")
            except VistaError as error:
                raise HistoryError(
                    f"Cannot import record {record!r}: {error}"
                ) from error
            row = self._register(
                request_id,
                fingerprint,
                record,
                content.metadata["label"],
                subtask_id,
                subtask_label,
                parent_short_id,
                confirmed=True,
            )
            return _history_record(row)

    def get(self, short_id: str) -> HistoryRecord:
        with self.connect() as connection:
            return _history_record(self._lookup(connection, short_id))

    def list_records(self) -> list[HistoryRecord]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT * FROM history_records WHERE task_id = ? AND confirmed = 1
                   ORDER BY sequence""",
                (self.task_id,),
            )
            return [_history_record(row) for row in rows]

    def read(
        self, short_id: str, content: Literal["image", "code", "both"] = "image"
    ) -> RecordContent:
        record = self.get(short_id)
        try:
            return self.store.read_record(record.record, content)
        except VistaError as error:
            raise HistoryUnavailable(
                short_id,
                image_unavailable=isinstance(error.__cause__, _ImageUnavailable),
            ) from error
