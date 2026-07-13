import asyncio
import os
import socket
import sqlite3
import stat
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from unified_agent import (
    AgentEvent,
    AssistantText,
    RawEvent,
    Reasoning,
    RunOptions,
    SessionStarted,
    SQLiteRunMonitor,
    ToolCall,
    ToolResult,
    TurnCompleted,
    UnifiedAgent,
)


class PausingBackend:
    name = "fake"

    def __init__(self) -> None:
        self.action_visible = asyncio.Event()
        self.release = asyncio.Event()

    async def stream(self, prompt: str, opts: RunOptions) -> AsyncIterator[AgentEvent]:
        yield SessionStarted(session_id="provider-session")
        yield ToolCall(name="Bash", input={"command": "printf ready"}, call_id="call-1")
        self.action_visible.set()
        await self.release.wait()
        yield ToolResult(call_id="call-1", output="ready", is_error=False)
        yield AssistantText(text="done")
        yield TurnCompleted(
            success=True, final_text="done", session_id="provider-session"
        )


class FailingBackend:
    name = "fake"

    async def stream(self, prompt: str, opts: RunOptions) -> AsyncIterator[AgentEvent]:
        yield AssistantText(text="partial")
        raise RuntimeError("provider transport broke")


@pytest.mark.asyncio
async def test_sqlite_monitor_exposes_events_before_run_completes(
    tmp_path: Path,
) -> None:
    """Red-capable acceptance loop for the real-time monitoring gap."""
    backend = PausingBackend()
    monitor = SQLiteRunMonitor(tmp_path / "monitor.db")
    agent = UnifiedAgent(
        backend,
        workspace=tmp_path / "workspace",
        monitor=monitor,
    )

    pending = asyncio.create_task(
        agent.run(
            "run one observable command",
            monitor_metadata={"project": "monitor-test", "role": "executor"},
        )
    )
    await asyncio.wait_for(backend.action_visible.wait(), timeout=1)

    [live_run] = monitor.list_runs(status="running")
    live_events = monitor.list_events(live_run.run_id)
    assert live_run.backend == "fake"
    assert live_run.owner_host == socket.gethostname()
    assert live_run.owner_pid == os.getpid()
    assert live_run.last_event_at >= live_run.started_at
    assert live_run.metadata == {"project": "monitor-test", "role": "executor"}
    assert live_run.prompt == "run one observable command"
    assert [event.sequence for event in live_events] == [1, 2]
    assert [event.event_type for event in live_events] == [
        "session_started",
        "tool_call",
    ]
    assert live_events[-1].payload["input"] == {"command": "printf ready"}

    backend.release.set()
    result = await asyncio.wait_for(pending, timeout=1)

    completed = monitor.get_run(live_run.run_id)
    assert result.success is True
    assert result.monitor_run_id == live_run.run_id
    assert completed is not None and completed.status == "succeeded"
    assert completed.completed_at is not None
    assert [event.event_type for event in monitor.list_events(live_run.run_id)] == [
        "session_started",
        "tool_call",
        "tool_result",
        "assistant_text",
        "turn_completed",
    ]


@pytest.mark.asyncio
async def test_sqlite_monitor_closes_failed_run_with_partial_events(
    tmp_path: Path,
) -> None:
    monitor = SQLiteRunMonitor(tmp_path / "monitor.db")
    agent = UnifiedAgent(
        FailingBackend(),
        workspace=tmp_path / "workspace",
        monitor=monitor,
    )

    result = await agent.run("fail after producing output")

    assert result.success is False
    assert result.monitor_run_id is not None
    failed = monitor.get_run(result.monitor_run_id)
    assert failed is not None
    assert failed.status == "failed"
    assert failed.error == "RuntimeError: provider transport broke"
    assert [event.event_type for event in monitor.list_events(failed.run_id)] == [
        "assistant_text"
    ]


def test_sqlite_monitor_can_be_opened_by_a_second_process_facade(
    tmp_path: Path,
) -> None:
    """A separate web process can query the same durable store."""
    path = tmp_path / "monitor.db"
    writer = SQLiteRunMonitor(path)
    reader = SQLiteRunMonitor(path)

    recorder = writer.start_run(
        run_id="run-shared",
        backend="fake",
        workspace=tmp_path,
        prompt="observe me",
        metadata={"suite": "cross-process"},
    )
    recorder.record(SessionStarted(session_id="s-1"))

    observed = reader.get_run("run-shared")
    assert observed is not None and observed.status == "running"
    assert [event.event_type for event in reader.list_events("run-shared")] == [
        "session_started"
    ]
    recorder.record(AssistantText(text="visible incrementally"))
    assert [
        event.event_type for event in reader.list_events("run-shared", after_sequence=1)
    ] == ["assistant_text"]

    recorder.finish(status="succeeded")
    assert reader.get_run("run-shared").status == "succeeded"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_monitor_can_be_enabled_without_changing_caller_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "environment-monitor.db"
    monkeypatch.setenv("UNIFIED_AGENT_MONITOR_DB", str(path))
    agent = UnifiedAgent(
        PausingBackend(),
        workspace=tmp_path / "workspace",
    )
    backend = agent.backend
    assert isinstance(backend, PausingBackend)

    pending = asyncio.create_task(agent.run("environment opt-in"))
    await asyncio.wait_for(backend.action_visible.wait(), timeout=1)
    backend.release.set()
    result = await asyncio.wait_for(pending, timeout=1)

    reader = SQLiteRunMonitor(path)
    assert result.monitor_run_id is not None
    assert reader.get_run(result.monitor_run_id).status == "succeeded"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_closing_a_stream_marks_the_monitor_run_cancelled(tmp_path: Path) -> None:
    monitor = SQLiteRunMonitor(tmp_path / "monitor.db")
    backend = PausingBackend()
    agent = UnifiedAgent(
        backend,
        workspace=tmp_path / "workspace",
        monitor=monitor,
    )
    stream = agent.stream("stop observing", run_id="cancelled-run")

    assert isinstance(await anext(stream), SessionStarted)
    await stream.aclose()

    cancelled = monitor.get_run("cancelled-run")
    assert cancelled is not None
    assert cancelled.status == "cancelled"
    assert cancelled.error == "event consumer closed the stream"


@pytest.mark.asyncio
async def test_closing_stream_after_terminal_event_preserves_success(
    tmp_path: Path,
) -> None:
    monitor = SQLiteRunMonitor(tmp_path / "monitor.db")
    backend = PausingBackend()
    backend.release.set()
    agent = UnifiedAgent(
        backend,
        workspace=tmp_path / "workspace",
        monitor=monitor,
    )
    stream = agent.stream("stop after terminal", run_id="terminal-run")

    async for event in stream:
        if isinstance(event, TurnCompleted):
            break
    await stream.aclose()

    completed = monitor.get_run("terminal-run")
    assert completed is not None
    assert completed.status == "succeeded"
    assert completed.error is None


def test_sensitive_provider_payloads_are_omitted_by_default(tmp_path: Path) -> None:
    monitor = SQLiteRunMonitor(tmp_path / "monitor.db", max_text_chars=8)
    recorder = monitor.start_run(
        run_id="sensitive-run",
        backend="fake",
        workspace=tmp_path,
        prompt="prompt-secret",
    )
    recorder.record(Reasoning(text="reasoning-secret"))
    recorder.record(
        RawEvent(backend="fake", kind="native", data={"secret": "raw-secret"})
    )
    recorder.record(AssistantText(text="1234567890"))
    recorder.finish(status="succeeded")

    run = monitor.get_run("sensitive-run")
    events = monitor.list_events("sensitive-run")
    assert run is not None and "prompt-secret" not in (run.prompt or "")
    assert events[0].payload == {"content_omitted": True}
    assert events[1].payload == {
        "backend": "fake",
        "kind": "native",
        "data_omitted": True,
    }
    assert events[2].payload["text"].startswith("12345678")
    assert "truncated 2 characters" in events[2].payload["text"]


def test_separate_monitor_instances_can_write_concurrently(tmp_path: Path) -> None:
    path = tmp_path / "monitor.db"
    monitors = [SQLiteRunMonitor(path), SQLiteRunMonitor(path)]

    def write(index: int) -> None:
        recorder = monitors[index].start_run(
            run_id=f"parallel-{index}",
            backend="fake",
            workspace=tmp_path,
            prompt=f"parallel run {index}",
        )
        for sequence in range(10):
            recorder.record(AssistantText(text=f"{index}:{sequence}"))
        recorder.finish(status="succeeded")

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(write, range(2)))

    reader = SQLiteRunMonitor(path)
    for index in range(2):
        run_id = f"parallel-{index}"
        assert reader.get_run(run_id).last_sequence == 10  # type: ignore[union-attr]
        assert [event.sequence for event in reader.list_events(run_id)] == list(
            range(1, 11)
        )


def test_database_and_wal_sidecars_are_private(tmp_path: Path) -> None:
    path = tmp_path / "monitor.db"
    monitor = SQLiteRunMonitor(path)
    recorder = monitor.start_run(
        run_id="private-run",
        backend="fake",
        workspace=tmp_path,
        prompt="sensitive prompt",
    )
    recorder.record(AssistantText(text="sensitive output"))

    for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        assert candidate.exists()
        assert stat.S_IMODE(candidate.stat().st_mode) == 0o600


def test_newer_database_schema_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "future.db"
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version=999")

    with pytest.raises(RuntimeError, match="newer than supported"):
        SQLiteRunMonitor(path)
