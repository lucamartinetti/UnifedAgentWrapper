"""Pi backend: drives the `pi` coding-agent CLI (pi.dev) as one agent unit.

Pi is a self-contained terminal coding harness that talks to any provider Pi is
configured for (here: DeepSeek via OpenRouter). Unlike the Claude Code / Codex
backends it needs no provider SDK and no nested OS sandbox — we run `pi` in
non-interactive mode (`-p`) with `--mode json`, which streams newline-delimited
JSON events that we normalize onto the shared AgentEvent model.

Pi runs its own tool loop (read/bash/edit/write) to completion within a single
`stream()` call, so one call == one full Pi turn-to-settle, ending in
TurnCompleted. Session state is kept per-workspace so `opts.resume` can continue
a prior session.

Not yet supported (raises): an injected MCP tool server (`opts.tool_server`) and
output schemas (`opts.output_schema`). The PentestGPT supervisor/executor roles
set neither, relying on Pi's native tools.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

from ..events import (
    AgentEvent,
    AssistantText,
    FileChanged,
    Reasoning,
    SessionStarted,
    TextDelta,
    ToolCall,
    ToolResult,
    TurnCompleted,
)
from ..types import (
    AgentAuthError,
    AgentRunError,
    BackendUnavailableError,
    RunOptions,
    SandboxPolicy,
    ToolServerError,
    UnifiedUsage,
)

# Pi's read-only tool set — used to honor SandboxPolicy.READ_ONLY. Pi has no OS
# sandbox (like Claude Code), so the policy is enforced by tool allow-listing.
_READ_ONLY_TOOLS = "read,grep,find,ls"
_WRITE_TOOLNAMES = {"write", "edit"}
_VALID_THINKING = {"off", "minimal", "low", "medium", "high", "xhigh", "max"}
# Pi exposes OpenRouter/DeepSeek by default in this image; callers normally pass
# an explicit --model, but fall back to the cage default when they don't. The
# :nitro variant is OpenRouter throughput routing (declared in the cage's
# models.json, so Pi matches the id rather than parsing :nitro as a thinking level).
_DEFAULT_MODEL = "deepseek/deepseek-v4.1-flash:nitro"


def build_argv(prompt: str, opts: RunOptions, session_id: str) -> list[str]:
    """Construct the `pi` command line for one non-interactive JSON run."""
    if opts.tool_server is not None:
        raise ToolServerError(
            "pi backend does not support an injected MCP tool server yet "
            "(opts.tool_server must be None)"
        )

    argv: list[str] = ["pi", "-p", "--mode", "json"]

    model = opts.model or _DEFAULT_MODEL
    argv += ["--model", model]

    # Pi has no CLI flag for final-message structured output, so when a schema
    # is requested we instruct the model to end with a single schema-conforming
    # JSON object and parse it out of the final assistant text (see stream()).
    # The framework retries if the parsed object is missing/invalid.
    instructions = opts.instructions or ""
    if opts.output_schema is not None:
        schema_json = json.dumps(opts.output_schema, separators=(",", ":"))
        instructions = (
            instructions
            + "\n\n## Structured output contract\n"
            "After any tool work, your FINAL message MUST be exactly one JSON object that "
            "validates against this JSON Schema — and nothing else: no prose, no explanation, "
            "no markdown, no code fences, just the raw JSON object.\n"
            "JSON Schema:\n" + schema_json
        ).strip()
    if instructions:
        argv += ["--append-system-prompt", instructions]

    if opts.sandbox is SandboxPolicy.READ_ONLY:
        argv += ["--tools", _READ_ONLY_TOOLS]
    # WORKSPACE_WRITE / FULL_ACCESS keep Pi's default tool set (read/bash/edit/write).

    if opts.effort and opts.effort in _VALID_THINKING:
        argv += ["--thinking", opts.effort]

    # Keep session state inside the workspace so it is hermetic per role and
    # resumable across turns.
    session_dir = str((opts.workspace / ".pi-sessions").resolve())
    argv += ["--session-id", session_id, "--session-dir", session_dir]

    # End option parsing; pass the prompt as the initial user message.
    argv += ["--", prompt]
    return argv


def _text_blocks(content: Any) -> tuple[list[str], list[str]]:
    """Return (text_parts, thinking_parts) from a Pi assistant message content."""
    texts: list[str] = []
    thinks: list[str] = []
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text" and block.get("text"):
                texts.append(block["text"])
            elif btype == "thinking" and block.get("thinking"):
                thinks.append(block["thinking"])
    return texts, thinks


def _extract_json(text: str | None) -> Any | None:
    """Best-effort parse of a JSON object from the model's final message.

    Tolerates ```json code fences and leading/trailing prose by falling back to
    the outermost balanced {...} span.
    """
    if not text:
        return None
    s = text.strip()
    if s.startswith("```"):
        s = s.strip("`")
        if s[:4].lower() == "json":
            s = s[4:]
        s = s.strip()
    try:
        return json.loads(s)
    except (json.JSONDecodeError, ValueError):
        pass
    start = s.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(s)):
        c = s[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(s[start : i + 1])
                except (json.JSONDecodeError, ValueError):
                    return None
    return None


def _result_text(result: Any) -> str:
    """Flatten a tool_execution_end result.content into plain text."""
    if isinstance(result, dict):
        content = result.get("content")
        if isinstance(content, list):
            parts = [
                b.get("text", "")
                for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            ]
            if parts:
                return "\n".join(parts)
        sc = result.get("structuredContent")
        if isinstance(sc, dict) and "output" in sc:
            return str(sc["output"])
    return "" if result is None else str(result)


class PiBackend:
    name = "pi"

    async def stream(self, prompt: str, opts: RunOptions) -> AsyncIterator[AgentEvent]:
        session_id = opts.resume or str(uuid.uuid4())
        opts.workspace.mkdir(parents=True, exist_ok=True)
        argv = build_argv(prompt, opts, session_id)

        env = {**os.environ, **opts.extra_env}

        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(opts.workspace),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                # Pi emits one JSON event per line; some (large tool outputs, the
                # agent_end message dump) far exceed asyncio's default 64 KiB
                # readline limit, which would raise LimitOverrunError. Give the
                # stream a generous per-line buffer.
                limit=512 * 1024 * 1024,
            )
        except FileNotFoundError as e:
            raise BackendUnavailableError(f"pi CLI not found on PATH: {e}") from e

        usage = UnifiedUsage()
        last_text: str | None = None
        saw_agent_end = False
        emitted_session = False

        assert proc.stdout is not None
        async for raw in proc.stdout:
            line = raw.decode("utf-8", "replace").strip()
            if not line or not line.startswith("{"):
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            etype = evt.get("type")

            if etype == "session":
                sid = evt.get("id") or session_id
                if not emitted_session:
                    emitted_session = True
                    yield SessionStarted(session_id=sid)

            elif etype == "text_delta" and opts.stream_text:
                if evt.get("text"):
                    yield TextDelta(text=evt["text"])

            elif etype == "tool_execution_start":
                yield ToolCall(
                    name=evt.get("toolName", ""),
                    input=evt.get("args"),
                    call_id=evt.get("toolCallId"),
                )

            elif etype == "tool_execution_end":
                tool_name = evt.get("toolName", "")
                yield ToolResult(
                    call_id=evt.get("toolCallId"),
                    output=_result_text(evt.get("result")),
                    is_error=bool(evt.get("isError")),
                )
                if tool_name in _WRITE_TOOLNAMES and not evt.get("isError"):
                    args = evt.get("args") or {}
                    path = args.get("path") or args.get("file") or args.get("filePath")
                    if path:
                        yield FileChanged(
                            path=str(path),
                            kind="update" if tool_name == "edit" else "add",
                        )

            elif etype == "turn_end":
                message = evt.get("message") or {}
                texts, thinks = _text_blocks(message.get("content"))
                for t in thinks:
                    yield Reasoning(text=t)
                for t in texts:
                    yield AssistantText(text=t)
                    last_text = t
                u = message.get("usage") or {}
                usage = usage + UnifiedUsage(
                    input_tokens=int(u.get("input", 0) or 0),
                    cached_input_tokens=int(u.get("cacheRead", 0) or 0),
                    output_tokens=int(u.get("output", 0) or 0),
                )

            elif etype == "agent_end":
                saw_agent_end = True

        stderr = (await proc.stderr.read()).decode("utf-8", "replace").strip() if proc.stderr else ""
        rc = await proc.wait()

        if rc != 0:
            low = stderr.lower()
            if any(k in low for k in ("api key", "api_key", "auth", "credential", "unauthorized")):
                raise AgentAuthError(f"pi not authenticated (exit {rc}): {stderr[-2000:]}")
            raise AgentRunError(f"pi process failed (exit {rc}): {stderr[-2000:]}")

        structured = _extract_json(last_text) if opts.output_schema is not None else None

        yield TurnCompleted(
            success=saw_agent_end,
            final_text=last_text,
            usage=usage,
            cost_usd=None,
            session_id=session_id,
            structured_output=structured,
            stop_reason="agent_end" if saw_agent_end else "incomplete",
            error=None if saw_agent_end else (stderr[-2000:] or "pi ended without agent_end"),
        )
