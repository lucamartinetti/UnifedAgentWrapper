import json
import re
from contextlib import asynccontextmanager
from importlib.resources import files
from pathlib import Path
from typing import AsyncIterator

import httpx
import pytest

from unified_agent import (
    AssistantText,
    CommandRun,
    CommandStarted,
    SessionStarted,
    SQLiteRunMonitor,
    TurnCompleted,
    UnifiedUsage,
)
from unified_agent.monitor_web import create_app, validate_bind_host


@asynccontextmanager
async def app_client(app: object) -> AsyncIterator[httpx.AsyncClient]:
    async with app.router.lifespan_context(app):  # type: ignore[attr-defined]
        transport = httpx.ASGITransport(app=app)  # type: ignore[arg-type]
        async with httpx.AsyncClient(
            transport=transport, base_url="http://inspector.test"
        ) as client:
            yield client


def populated_database(tmp_path: Path) -> Path:
    path = tmp_path / "monitor.db"
    monitor = SQLiteRunMonitor(path)
    recorder = monitor.start_run(
        run_id="run-1",
        backend="codex",
        workspace=tmp_path / "workspace",
        prompt="Inspect the local demo target",
        metadata={"project": "manual-review", "role": "executor"},
    )
    recorder.record(SessionStarted(session_id="session-1"))
    recorder.record(
        CommandStarted(
            command="printf monitor-ok",
            call_id="command-1",
            cwd=str(tmp_path / "workspace"),
            process_id="pty-1",
        )
    )
    recorder.record(
        CommandRun(
            command="printf monitor-ok",
            exit_code=0,
            output="monitor-ok",
            call_id="command-1",
            process_id="pty-1",
            duration_ms=12,
        )
    )
    recorder.record(AssistantText(text="The command returned monitor-ok."))
    recorder.record(
        TurnCompleted(
            success=True,
            final_text="The command returned monitor-ok.",
            session_id="session-1",
            usage=UnifiedUsage(input_tokens=21, output_tokens=7),
        )
    )
    recorder.finish(status="succeeded")
    monitor.close()
    return path


@pytest.mark.asyncio
async def test_monitor_web_serves_read_only_run_and_event_snapshots(
    tmp_path: Path,
) -> None:
    path = populated_database(tmp_path)

    async with app_client(create_app(path, poll_interval=0.001)) as client:
        health = await client.get("/api/health")
        assert health.status_code == 200
        assert health.json() == {"ok": True, "schema_version": 1}

        response = await client.get("/api/runs")
        assert response.status_code == 200
        [run] = response.json()["runs"]
        assert run["run_id"] == "run-1"
        assert run["status"] == "succeeded"
        assert run["metadata"] == {"project": "manual-review", "role": "executor"}

        detail = (await client.get("/api/runs/run-1")).json()["run"]
        assert detail["prompt"] == "Inspect the local demo target"
        assert detail["last_sequence"] == 5

        events = (await client.get("/api/runs/run-1/events?after_sequence=2")).json()
        assert [event["sequence"] for event in events["events"]] == [3, 4, 5]
        assert events["next_sequence"] == 5


@pytest.mark.asyncio
async def test_monitor_web_stream_resumes_by_sequence_and_closes_when_final(
    tmp_path: Path,
) -> None:
    path = populated_database(tmp_path)

    async with app_client(create_app(path, poll_interval=0.001)) as client:
        response = await client.get(
            "/api/runs/run-1/stream?after_sequence=1",
            headers={"Last-Event-ID": "2"},
        )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    blocks = [block for block in response.text.split("\n\n") if block.strip()]
    agent_events = [block for block in blocks if "event: agent_event" in block]
    ids = [
        int(next(line[4:] for line in block.splitlines() if line.startswith("id: ")))
        for block in agent_events
    ]
    assert ids == [3, 4, 5]
    assert any("event: run_state" in block for block in blocks)
    assert any("event: stream_end" in block for block in blocks)


@pytest.mark.asyncio
async def test_monitor_web_validates_queries_and_missing_runs(tmp_path: Path) -> None:
    path = populated_database(tmp_path)
    async with app_client(create_app(path)) as client:
        assert (await client.get("/api/runs/missing")).status_code == 404
        assert (
            await client.get("/api/runs/run-1/events?after_sequence=-1")
        ).status_code == 400
        assert (await client.get("/api/runs?limit=0")).status_code == 400
        assert (await client.post("/api/runs")).status_code == 405


@pytest.mark.asyncio
async def test_monitor_web_assets_are_local_and_security_hardened(
    tmp_path: Path,
) -> None:
    path = populated_database(tmp_path)
    async with app_client(create_app(path)) as client:
        page = await client.get("/")
        assert page.status_code == 200
        assert "UnifiedAgent Inspector" in page.text
        assert 'src="/assets/app.js"' in page.text
        assert "https://" not in page.text
        assert "default-src 'self'" in page.headers["content-security-policy"]
        assert page.headers["x-content-type-options"] == "nosniff"
        assert "access-control-allow-origin" not in page.headers

        script = await client.get("/assets/app.js")
        style = await client.get("/assets/style.css")
        assert script.status_code == style.status_code == 200
        assert "EventSource" in script.text
        assert "innerHTML" not in script.text
        assert "eval(" not in script.text
        assert "--accent" in style.text


@pytest.mark.asyncio
async def test_monitor_web_event_payload_is_json_serializable(tmp_path: Path) -> None:
    path = populated_database(tmp_path)
    async with app_client(create_app(path)) as client:
        payload = (await client.get("/api/runs/run-1/events")).json()
    assert json.loads(json.dumps(payload)) == payload


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_monitor_web_accepts_loopback_bindings(host: str) -> None:
    assert validate_bind_host(host) == host


def test_monitor_web_rejects_remote_binding() -> None:
    with pytest.raises(ValueError, match="loopback"):
        validate_bind_host("0.0.0.0")


def test_monitor_web_script_references_existing_dom_ids() -> None:
    html = (
        files("unified_agent").joinpath("monitor_web_assets", "index.html").read_text()
    )
    script = files("unified_agent").joinpath("monitor_web_assets", "app.js").read_text()
    html_ids = set(re.findall(r'id="([^"]+)"', html))
    referenced_ids = set(re.findall(r'byId\("([^"]+)"\)', script))
    assert referenced_ids <= html_ids
