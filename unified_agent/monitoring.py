"""Durable, provider-neutral monitoring for UnifiedAgent runs.

The public seam is deliberately small: a :class:`RunMonitor` starts one
recorder, and that recorder accepts normalized ``AgentEvent`` objects until it
is finished.  Storage schemas, ordering, redaction, and cross-process access
stay behind the monitor implementation.
"""

from __future__ import annotations

import json
import os
import re
import socket
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Protocol, runtime_checkable

from .events import AgentEvent, RawEvent, Reasoning

_SCHEMA_VERSION = 1
_FINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled", "incomplete"})
_CAMEL_BOUNDARY = re.compile(r"(?<!^)(?=[A-Z])")


@dataclass(frozen=True)
class MonitoredRun:
    """One wrapper invocation as seen by a monitor reader."""

    run_id: str
    backend: str
    workspace: str
    status: str
    prompt: str | None
    metadata: dict[str, Any]
    owner_host: str
    owner_pid: int
    started_at: float
    last_event_at: float
    completed_at: float | None
    error: str | None
    last_sequence: int


@dataclass(frozen=True)
class RecordedEvent:
    """One ordered normalized event persisted for a monitored run."""

    run_id: str
    sequence: int
    recorded_at: float
    event_type: str
    payload: dict[str, Any]


@runtime_checkable
class RunRecorder(Protocol):
    """Run-scoped event recorder returned by a :class:`RunMonitor`."""

    @property
    def run_id(self) -> str: ...

    def record(self, event: AgentEvent) -> None: ...

    def finish(self, *, status: str, error: str | None = None) -> None: ...


@runtime_checkable
class RunMonitor(Protocol):
    """External seam used by ``UnifiedAgent``; adapters own persistence/transport."""

    def start_run(
        self,
        *,
        run_id: str,
        backend: str,
        workspace: str | Path,
        prompt: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> RunRecorder: ...


class SQLiteRunMonitor:
    """SQLite/WAL monitor suitable for a separate local web process.

    Every event is committed before control returns to the provider stream, so
    another process can observe it while the agent is still running. Reasoning
    and backend-native raw payloads are omitted by default; normalized command,
    tool, file, text, usage, and terminal events are retained.
    """

    def __init__(
        self,
        database: str | Path,
        *,
        capture_prompt: bool = True,
        capture_reasoning: bool = False,
        capture_raw_events: bool = False,
        max_text_chars: int | None = 200_000,
    ) -> None:
        if max_text_chars is not None and max_text_chars < 1:
            raise ValueError("max_text_chars must be positive or None")
        self.database = str(database)
        self.capture_prompt = capture_prompt
        self.capture_reasoning = capture_reasoning
        self.capture_raw_events = capture_raw_events
        self.max_text_chars = max_text_chars
        self._lock = threading.RLock()

        if self.database != ":memory:":
            path = Path(self.database).expanduser().resolve()
            path.parent.mkdir(parents=True, exist_ok=True)
            self.database = str(path)

        self._connection = sqlite3.connect(
            self.database,
            timeout=30,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        try:
            with self._lock:
                self._connection.execute("PRAGMA busy_timeout=30000")
                self._connection.execute("PRAGMA foreign_keys=ON")
                if self.database != ":memory:":
                    self._connection.execute("PRAGMA journal_mode=WAL")
                self._initialize_schema()
        except BaseException:
            self._protect_database_files()
            self._connection.close()
            raise

        self._protect_database_files()

    def _initialize_schema(self) -> None:
        current_version = int(
            self._connection.execute("PRAGMA user_version").fetchone()[0]
        )
        if current_version > _SCHEMA_VERSION:
            raise RuntimeError(
                f"monitor database schema {current_version} is newer than supported "
                f"schema {_SCHEMA_VERSION}"
            )
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS monitor_runs (
                run_id TEXT PRIMARY KEY,
                schema_version INTEGER NOT NULL,
                backend TEXT NOT NULL,
                workspace TEXT NOT NULL,
                status TEXT NOT NULL,
                prompt TEXT,
                metadata_json TEXT NOT NULL,
                owner_host TEXT NOT NULL,
                owner_pid INTEGER NOT NULL,
                started_at REAL NOT NULL,
                last_event_at REAL NOT NULL,
                completed_at REAL,
                error TEXT,
                last_sequence INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS monitor_events (
                run_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                recorded_at REAL NOT NULL,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                PRIMARY KEY (run_id, sequence),
                FOREIGN KEY (run_id) REFERENCES monitor_runs(run_id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS monitor_runs_status_started
                ON monitor_runs(status, started_at DESC);
            CREATE INDEX IF NOT EXISTS monitor_events_type
                ON monitor_events(event_type);
            """
        )
        if current_version < _SCHEMA_VERSION:
            self._connection.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
        self._connection.commit()

    @property
    def schema_version(self) -> int:
        return _SCHEMA_VERSION

    def start_run(
        self,
        *,
        run_id: str,
        backend: str,
        workspace: str | Path,
        prompt: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> RunRecorder:
        if not run_id or not run_id.strip():
            raise ValueError("run_id must not be empty")
        started_at = time.time()
        safe_metadata = _json_safe(dict(metadata or {}), self.max_text_chars)
        recorded_prompt = (
            _truncate_text(prompt, self.max_text_chars) if self.capture_prompt else None
        )
        try:
            with self._lock, self._connection:
                self._connection.execute(
                    """
                    INSERT INTO monitor_runs (
                        run_id, schema_version, backend, workspace, status, prompt,
                        metadata_json, owner_host, owner_pid, started_at, last_event_at
                    ) VALUES (?, ?, ?, ?, 'running', ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        _SCHEMA_VERSION,
                        backend,
                        str(Path(workspace).resolve()),
                        recorded_prompt,
                        json.dumps(
                            safe_metadata, sort_keys=True, separators=(",", ":")
                        ),
                        socket.gethostname(),
                        os.getpid(),
                        started_at,
                        started_at,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"monitor run_id already exists: {run_id!r}") from exc
        self._protect_database_files()
        return _SQLiteRunRecorder(self, run_id)

    def get_run(self, run_id: str) -> MonitoredRun | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM monitor_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        return _run_from_row(row) if row is not None else None

    def list_runs(
        self,
        *,
        status: str | None = None,
        limit: int = 100,
    ) -> list[MonitoredRun]:
        if limit < 1:
            raise ValueError("limit must be positive")
        sql = "SELECT * FROM monitor_runs"
        params: list[Any] = []
        if status is not None:
            sql += " WHERE status = ?"
            params.append(status)
        sql += " ORDER BY started_at DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._connection.execute(sql, params).fetchall()
        return [_run_from_row(row) for row in rows]

    def list_events(
        self,
        run_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 10_000,
    ) -> list[RecordedEvent]:
        if after_sequence < 0:
            raise ValueError("after_sequence must not be negative")
        if limit < 1:
            raise ValueError("limit must be positive")
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT * FROM monitor_events
                WHERE run_id = ? AND sequence > ?
                ORDER BY sequence
                LIMIT ?
                """,
                (run_id, after_sequence, limit),
            ).fetchall()
        return [
            RecordedEvent(
                run_id=str(row["run_id"]),
                sequence=int(row["sequence"]),
                recorded_at=float(row["recorded_at"]),
                event_type=str(row["event_type"]),
                payload=json.loads(row["payload_json"]),
            )
            for row in rows
        ]

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def _protect_database_files(self) -> None:
        if self.database == ":memory:":
            return
        for candidate in (
            Path(self.database),
            Path(f"{self.database}-wal"),
            Path(f"{self.database}-shm"),
        ):
            try:
                candidate.chmod(0o600)
            except FileNotFoundError:
                pass

    def _append(self, run_id: str, sequence: int, event: AgentEvent) -> None:
        event_type, payload = self._serialize_event(event)
        recorded_at = time.time()
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                UPDATE monitor_runs SET last_sequence = ?, last_event_at = ?
                WHERE run_id = ? AND status = 'running' AND last_sequence = ?
                """,
                (sequence, recorded_at, run_id, sequence - 1),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(
                    f"monitor run {run_id!r} is closed or has an invalid event sequence"
                )
            self._connection.execute(
                """
                INSERT INTO monitor_events (
                    run_id, sequence, recorded_at, event_type, payload_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    sequence,
                    recorded_at,
                    event_type,
                    json.dumps(payload, sort_keys=True, separators=(",", ":")),
                ),
            )
        self._protect_database_files()

    def _finish(self, run_id: str, *, status: str, error: str | None) -> None:
        if status not in _FINAL_STATUSES:
            raise ValueError(
                f"invalid final monitor status {status!r}; expected one of "
                f"{', '.join(sorted(_FINAL_STATUSES))}"
            )
        with self._lock, self._connection:
            completed_at = time.time()
            cursor = self._connection.execute(
                """
                UPDATE monitor_runs
                SET status = ?, completed_at = ?, last_event_at = ?, error = ?
                WHERE run_id = ? AND status = 'running'
                """,
                (status, completed_at, completed_at, error, run_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(
                    f"monitor run {run_id!r} is already closed or missing"
                )
        self._protect_database_files()

    def _serialize_event(self, event: AgentEvent) -> tuple[str, dict[str, Any]]:
        event_type = _CAMEL_BOUNDARY.sub("_", type(event).__name__).lower()
        if isinstance(event, Reasoning) and not self.capture_reasoning:
            return event_type, {"content_omitted": True}
        if isinstance(event, RawEvent) and not self.capture_raw_events:
            return event_type, {
                "backend": event.backend,
                "kind": event.kind,
                "data_omitted": True,
            }
        raw = asdict(event) if is_dataclass(event) else {"value": event}
        safe = _json_safe(raw, self.max_text_chars)
        if not isinstance(safe, dict):
            safe = {"value": safe}
        return event_type, safe


class _SQLiteRunRecorder:
    def __init__(self, monitor: SQLiteRunMonitor, run_id: str) -> None:
        self._monitor = monitor
        self._run_id = run_id
        self._sequence = 0
        self._finished = False
        self._lock = threading.Lock()

    @property
    def run_id(self) -> str:
        return self._run_id

    def record(self, event: AgentEvent) -> None:
        with self._lock:
            if self._finished:
                raise RuntimeError(f"monitor run {self._run_id!r} is already closed")
            next_sequence = self._sequence + 1
            self._monitor._append(self._run_id, next_sequence, event)
            self._sequence = next_sequence

    def finish(self, *, status: str, error: str | None = None) -> None:
        with self._lock:
            if self._finished:
                raise RuntimeError(f"monitor run {self._run_id!r} is already closed")
            self._monitor._finish(self._run_id, status=status, error=error)
            self._finished = True


def _run_from_row(row: sqlite3.Row) -> MonitoredRun:
    return MonitoredRun(
        run_id=str(row["run_id"]),
        backend=str(row["backend"]),
        workspace=str(row["workspace"]),
        status=str(row["status"]),
        prompt=row["prompt"],
        metadata=json.loads(row["metadata_json"]),
        owner_host=str(row["owner_host"]),
        owner_pid=int(row["owner_pid"]),
        started_at=float(row["started_at"]),
        last_event_at=float(row["last_event_at"]),
        completed_at=float(row["completed_at"])
        if row["completed_at"] is not None
        else None,
        error=row["error"],
        last_sequence=int(row["last_sequence"]),
    )


def _truncate_text(value: str, limit: int | None) -> str:
    if limit is None or len(value) <= limit:
        return value
    omitted = len(value) - limit
    return f"{value[:limit]}\n...[monitor truncated {omitted} characters]"


def _json_safe(value: Any, max_text_chars: int | None) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _truncate_text(value, max_text_chars)
    if isinstance(value, bytes):
        return _truncate_text(value.decode("utf-8", "replace"), max_text_chars)
    if isinstance(value, Enum):
        return _json_safe(value.value, max_text_chars)
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value) and not isinstance(value, type):
        return _json_safe(asdict(value), max_text_chars)
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return _json_safe(model_dump(mode="json", by_alias=True), max_text_chars)
    if isinstance(value, Mapping):
        return {
            str(key): _json_safe(item, max_text_chars) for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(item, max_text_chars) for item in value]
    return _truncate_text(repr(value), max_text_chars)


__all__ = [
    "MonitoredRun",
    "RecordedEvent",
    "RunMonitor",
    "RunRecorder",
    "SQLiteRunMonitor",
]
