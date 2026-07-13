"""Read-only web inspector for :mod:`unified_agent.monitoring`.

Run with::

    python -m unified_agent.monitor_web --database /path/to/agent-monitor.db --open

The web dependencies are optional; install ``unified-agent[monitor-web]``.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import ipaddress
import json
import os
import socket
import time
import uuid
import webbrowser
from contextlib import asynccontextmanager
from dataclasses import asdict
from importlib.resources import files
from pathlib import Path
from typing import Any, AsyncIterator, Sequence

from .events import (
    AssistantText,
    CommandOutputDelta,
    CommandRun,
    CommandStarted,
    FileChanged,
    SessionStarted,
    ToolCall,
    ToolProgress,
    ToolResult,
    TurnCompleted,
)
from .monitoring import MonitoredRun, RecordedEvent, SQLiteRunMonitor
from .types import UnifiedUsage

_ASSET_DIR = "monitor_web_assets"
_FINAL_RUN_STATUSES = frozenset({"succeeded", "failed", "cancelled", "incomplete"})
_SECURITY_HEADERS = {
    b"content-security-policy": (
        b"default-src 'self'; script-src 'self'; style-src 'self'; "
        b"connect-src 'self'; img-src 'self' data:; object-src 'none'; "
        b"base-uri 'none'; frame-ancestors 'none'; form-action 'none'"
    ),
    b"referrer-policy": b"no-referrer",
    b"x-content-type-options": b"nosniff",
    b"x-frame-options": b"DENY",
}


class _SecurityHeadersMiddleware:
    """Small ASGI middleware that does not buffer streaming responses."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        async def send_with_headers(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                present = {name.lower() for name, _ in headers}
                headers.extend(
                    (name, value)
                    for name, value in _SECURITY_HEADERS.items()
                    if name not in present
                )
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_headers)


def validate_bind_host(host: str) -> str:
    """Reject non-loopback bindings until authenticated remote access exists."""
    candidate = host.strip()
    normalized = candidate.removeprefix("[").removesuffix("]")
    if normalized.casefold() == "localhost":
        return host
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError as exc:
        raise ValueError("monitor web server must bind to a loopback address") from exc
    if not address.is_loopback:
        raise ValueError("monitor web server must bind to a loopback address")
    return host


def _require_web_dependencies() -> dict[str, Any]:
    try:
        from starlette.applications import Starlette
        from starlette.middleware import Middleware
        from starlette.requests import Request
        from starlette.responses import JSONResponse, Response, StreamingResponse
        from starlette.routing import Route
    except ImportError as exc:  # pragma: no cover - exercised in core-only installs
        raise RuntimeError(
            "monitor web dependencies are missing; install 'unified-agent[monitor-web]'"
        ) from exc
    return {
        "Starlette": Starlette,
        "Middleware": Middleware,
        "Request": Request,
        "JSONResponse": JSONResponse,
        "Response": Response,
        "StreamingResponse": StreamingResponse,
        "Route": Route,
    }


def _owner_alive(run: MonitoredRun) -> bool | None:
    if run.status != "running" or run.owner_host != socket.gethostname():
        return None
    try:
        os.kill(run.owner_pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _run_payload(run: MonitoredRun) -> dict[str, Any]:
    payload = asdict(run)
    owner_alive = _owner_alive(run)
    payload["owner_alive"] = owner_alive
    payload["stale"] = run.status == "running" and owner_alive is False
    return payload


def _event_payload(event: RecordedEvent) -> dict[str, Any]:
    return asdict(event)


def _asset(name: str) -> bytes:
    return files("unified_agent").joinpath(_ASSET_DIR, name).read_bytes()


def _integer_query(
    raw: str | None,
    *,
    default: int,
    minimum: int,
    maximum: int,
    label: str,
) -> int:
    try:
        value = default if raw is None else int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{label} must be between {minimum} and {maximum}")
    return value


def _sse_message(
    event: str, data: dict[str, Any], *, event_id: int | None = None
) -> bytes:
    lines = []
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.append(f"event: {event}")
    lines.append("data: " + json.dumps(data, ensure_ascii=False, separators=(",", ":")))
    return ("\n".join(lines) + "\n\n").encode()


def create_app(
    database: str | Path,
    *,
    poll_interval: float = 0.25,
    demo: bool = False,
) -> Any:
    """Build the read-only inspector ASGI application."""
    if poll_interval <= 0:
        raise ValueError("poll_interval must be positive")
    web = _require_web_dependencies()
    JSONResponse = web["JSONResponse"]
    Response = web["Response"]
    StreamingResponse = web["StreamingResponse"]
    Route = web["Route"]

    @asynccontextmanager
    async def lifespan(app: Any) -> AsyncIterator[None]:
        monitor = SQLiteRunMonitor(database)
        app.state.monitor = monitor
        demo_task = asyncio.create_task(_demo_feed(monitor)) if demo else None
        try:
            yield
        finally:
            if demo_task is not None:
                demo_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await demo_task
            monitor.close()

    def monitor_for(request: Any) -> SQLiteRunMonitor:
        return request.app.state.monitor

    def error(message: str, status_code: int) -> Any:
        return JSONResponse(
            {"error": message},
            status_code=status_code,
            headers={"Cache-Control": "no-store"},
        )

    async def index(request: Any) -> Any:
        return Response(
            _asset("index.html"),
            media_type="text/html",
            headers={"Cache-Control": "no-store"},
        )

    async def asset(request: Any) -> Any:
        name = request.path_params["name"]
        media_types = {
            "app.js": "text/javascript",
            "style.css": "text/css",
        }
        if name not in media_types:
            return error("asset not found", 404)
        return Response(
            _asset(name),
            media_type=media_types[name],
            headers={"Cache-Control": "public, max-age=3600"},
        )

    async def health(request: Any) -> Any:
        return JSONResponse(
            {"ok": True, "schema_version": monitor_for(request).schema_version},
            headers={"Cache-Control": "no-store"},
        )

    async def runs(request: Any) -> Any:
        try:
            limit = _integer_query(
                request.query_params.get("limit"),
                default=100,
                minimum=1,
                maximum=1000,
                label="limit",
            )
        except ValueError as exc:
            return error(str(exc), 400)
        status = request.query_params.get("status")
        valid_statuses = {"running", *_FINAL_RUN_STATUSES}
        if status in (None, "", "all"):
            status = None
        elif status not in valid_statuses:
            return error("invalid run status", 400)
        records = monitor_for(request).list_runs(status=status, limit=limit)
        return JSONResponse(
            {"runs": [_run_payload(record) for record in records]},
            headers={"Cache-Control": "no-store"},
        )

    async def run_detail(request: Any) -> Any:
        record = monitor_for(request).get_run(request.path_params["run_id"])
        if record is None:
            return error("run not found", 404)
        return JSONResponse(
            {"run": _run_payload(record)}, headers={"Cache-Control": "no-store"}
        )

    async def run_events(request: Any) -> Any:
        monitor = monitor_for(request)
        run_id = request.path_params["run_id"]
        if monitor.get_run(run_id) is None:
            return error("run not found", 404)
        try:
            after_sequence = _integer_query(
                request.query_params.get("after_sequence"),
                default=0,
                minimum=0,
                maximum=2**63 - 1,
                label="after_sequence",
            )
            limit = _integer_query(
                request.query_params.get("limit"),
                default=10_000,
                minimum=1,
                maximum=10_000,
                label="limit",
            )
        except ValueError as exc:
            return error(str(exc), 400)
        records = monitor.list_events(
            run_id, after_sequence=after_sequence, limit=limit
        )
        next_sequence = records[-1].sequence if records else after_sequence
        return JSONResponse(
            {
                "events": [_event_payload(record) for record in records],
                "next_sequence": next_sequence,
            },
            headers={"Cache-Control": "no-store"},
        )

    async def run_stream(request: Any) -> Any:
        monitor = monitor_for(request)
        run_id = request.path_params["run_id"]
        if monitor.get_run(run_id) is None:
            return error("run not found", 404)
        try:
            query_after = _integer_query(
                request.query_params.get("after_sequence"),
                default=0,
                minimum=0,
                maximum=2**63 - 1,
                label="after_sequence",
            )
        except ValueError as exc:
            return error(str(exc), 400)
        header_after = 0
        with contextlib.suppress(ValueError):
            header_after = int(request.headers.get("last-event-id", "0"))
        after_sequence = max(query_after, header_after)

        async def generate() -> AsyncIterator[bytes]:
            sequence = after_sequence
            last_state: tuple[Any, ...] | None = None
            heartbeat_at = time.monotonic() + 10
            while True:
                if await request.is_disconnected():
                    return
                record = monitor.get_run(run_id)
                if record is None:
                    yield _sse_message("stream_end", {"reason": "run_missing"})
                    return
                state = (
                    record.status,
                    record.last_sequence,
                    record.last_event_at,
                    record.completed_at,
                    record.error,
                )
                if state != last_state:
                    yield _sse_message("run_state", {"run": _run_payload(record)})
                    last_state = state

                events = monitor.list_events(
                    run_id, after_sequence=sequence, limit=1000
                )
                for item in events:
                    sequence = item.sequence
                    yield _sse_message(
                        "agent_event",
                        {"event": _event_payload(item)},
                        event_id=sequence,
                    )

                if (
                    record.status in _FINAL_RUN_STATUSES
                    and sequence >= record.last_sequence
                ):
                    yield _sse_message(
                        "stream_end",
                        {"reason": "run_finished", "status": record.status},
                    )
                    return
                if time.monotonic() >= heartbeat_at:
                    yield b": keepalive\n\n"
                    heartbeat_at = time.monotonic() + 10
                await asyncio.sleep(poll_interval)

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-store",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    routes = [
        Route("/", index),
        Route("/assets/{name:str}", asset),
        Route("/api/health", health),
        Route("/api/runs", runs),
        Route("/api/runs/{run_id:str}/events", run_events),
        Route("/api/runs/{run_id:str}/stream", run_stream),
        Route("/api/runs/{run_id:str}", run_detail),
    ]
    return web["Starlette"](
        routes=routes,
        lifespan=lifespan,
        middleware=[web["Middleware"](_SecurityHeadersMiddleware)],
    )


async def _demo_feed(monitor: SQLiteRunMonitor) -> None:
    """Continuously create safe fake runs for visual evaluation."""
    if not monitor.list_runs(limit=1):
        _seed_completed_demo(monitor)
    active_recorder: Any | None = None
    try:
        while True:
            run_id = f"demo-live-{uuid.uuid4().hex[:8]}"
            active_recorder = monitor.start_run(
                run_id=run_id,
                backend="codex",
                workspace="/tmp/unified-agent-inspector-demo",
                prompt=(
                    "Inspect the authorized local demo service, record the response, "
                    "and summarize the result."
                ),
                metadata={
                    "project": "inspector-demo",
                    "role": "executor",
                    "episode_id": f"episode-{uuid.uuid4().hex[:6]}",
                },
            )
            active_recorder.record(
                SessionStarted(session_id=f"thread-{uuid.uuid4().hex[:8]}")
            )
            await asyncio.sleep(0.7)
            active_recorder.record(
                CommandStarted(
                    command="curl --max-time 3 http://127.0.0.1:8080/health",
                    call_id="command-health",
                    cwd="/tmp/unified-agent-inspector-demo",
                    process_id="demo-pty-1",
                )
            )
            for chunk in (
                "* Connected to 127.0.0.1\n",
                "< HTTP/1.1 200 OK\n",
                '{"status":"healthy","service":"demo"}\n',
            ):
                await asyncio.sleep(0.85)
                active_recorder.record(
                    CommandOutputDelta(call_id="command-health", delta=chunk)
                )
            active_recorder.record(
                CommandRun(
                    command="curl --max-time 3 http://127.0.0.1:8080/health",
                    exit_code=0,
                    output='{"status":"healthy","service":"demo"}',
                    call_id="command-health",
                    process_id="demo-pty-1",
                    duration_ms=184,
                )
            )
            await asyncio.sleep(0.6)
            active_recorder.record(
                ToolCall(
                    name="mcp__unified__record_finding",
                    input={"title": "Local service healthy", "severity": "info"},
                    call_id="tool-record",
                )
            )
            await asyncio.sleep(0.7)
            active_recorder.record(
                ToolProgress(
                    call_id="tool-record", message="Writing structured observation"
                )
            )
            await asyncio.sleep(0.7)
            active_recorder.record(
                ToolResult(
                    call_id="tool-record",
                    output='{"observation_id":"obs-demo-1"}',
                    is_error=False,
                )
            )
            active_recorder.record(FileChanged(path="notes/demo-health.md", kind="add"))
            await asyncio.sleep(0.6)
            final_text = (
                "The local demo service returned HTTP 200 and reported healthy status."
            )
            active_recorder.record(AssistantText(text=final_text))
            active_recorder.record(
                TurnCompleted(
                    success=True,
                    final_text=final_text,
                    usage=UnifiedUsage(
                        input_tokens=1240,
                        cached_input_tokens=820,
                        output_tokens=186,
                        reasoning_output_tokens=74,
                    ),
                    session_id=f"thread-{uuid.uuid4().hex[:8]}",
                    duration_ms=6380,
                )
            )
            active_recorder.finish(status="succeeded")
            active_recorder = None
            await asyncio.sleep(4)
    except asyncio.CancelledError:
        if active_recorder is not None:
            with contextlib.suppress(RuntimeError):
                active_recorder.finish(
                    status="cancelled", error="inspector demo server stopped"
                )
        raise


def _seed_completed_demo(monitor: SQLiteRunMonitor) -> None:
    run_id = f"demo-history-{uuid.uuid4().hex[:8]}"
    recorder = monitor.start_run(
        run_id=run_id,
        backend="claude",
        workspace="/tmp/unified-agent-inspector-demo",
        prompt="Summarize the local workspace files without changing them.",
        metadata={"project": "inspector-demo", "role": "supervisor"},
    )
    recorder.record(SessionStarted(session_id=f"claude-{uuid.uuid4().hex[:8]}"))
    recorder.record(
        ToolCall(
            name="Read",
            input={"file_path": "/tmp/unified-agent-inspector-demo/README.md"},
            call_id="read-1",
        )
    )
    recorder.record(
        ToolResult(
            call_id="read-1",
            output="# Inspector demo\nA safe local fixture.",
            is_error=False,
        )
    )
    recorder.record(
        AssistantText(text="The workspace contains a safe inspector demo fixture.")
    )
    recorder.record(
        TurnCompleted(
            success=True,
            final_text="The workspace contains a safe inspector demo fixture.",
            usage=UnifiedUsage(input_tokens=642, output_tokens=83),
            cost_usd=0.0031,
            duration_ms=2180,
        )
    )
    recorder.finish(status="succeeded")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database",
        default=os.environ.get("UNIFIED_AGENT_MONITOR_DB", "agent-monitor.db"),
        help="SQLite monitor database (default: UNIFIED_AGENT_MONITOR_DB or ./agent-monitor.db)",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--poll-interval", type=float, default=0.25)
    parser.add_argument(
        "--demo", action="store_true", help="generate safe fake live runs"
    )
    parser.add_argument(
        "--open", action="store_true", help="open the inspector in a browser"
    )
    parser.add_argument("--log-level", default="info")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        host = validate_bind_host(args.host)
    except ValueError as exc:
        parser.error(str(exc))
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    if args.poll_interval <= 0:
        parser.error("poll interval must be positive")
    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover - core-only install
        parser.error(
            "monitor web dependencies are missing; install 'unified-agent[monitor-web]'"
        )
        raise AssertionError from exc

    database = Path(args.database).expanduser().resolve()
    url_host = f"[{host}]" if ":" in host else host
    url = f"http://{url_host}:{args.port}"
    print(f"UnifiedAgent Inspector: {url}")
    print(f"Monitor database: {database}")
    if args.demo:
        print("Demo mode: safe fake agent activity is enabled")
    if args.open:
        webbrowser.open(url)
    uvicorn.run(
        create_app(database, poll_interval=args.poll_interval, demo=args.demo),
        host=host,
        port=args.port,
        log_level=args.log_level,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = ["create_app", "main", "validate_bind_host"]
