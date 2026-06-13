"""Confirm alternate models work on each backend, via the real public API.

Runs a trivial task through UnifiedAgent.run() for each (backend, model) pair,
and independently captures the AUTHORITATIVE served-model string from the raw
SDK so we're not trusting the model's self-report.

    .venv/bin/python scripts/check_models.py
"""

from __future__ import annotations

import asyncio
import inspect
import shutil
from pathlib import Path

from claude_agent_sdk import AssistantMessage, SystemMessage, query
from openai_codex import AsyncCodex, CodexConfig

from unified_agent import SandboxPolicy, UnifiedAgent
from unified_agent.backends.claude_code import build_options
from unified_agent.backends.codex import build_thread_kwargs, build_turn_kwargs
from unified_agent.types import RunOptions

WS = Path(__file__).resolve().parent.parent / "examples" / "workspace" / "models"


def ws(tag: str) -> Path:
    d = WS / tag
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True)
    return d


async def claude_served_model(model: str, workspace: Path) -> str | None:
    """Authoritative model string from Claude Code's own messages."""
    opts = RunOptions(workspace=workspace, model=model, sandbox=SandboxPolicy.READ_ONLY)
    served = None
    async for msg in query(prompt="Reply with exactly: OK", options=build_options(opts)):
        if isinstance(msg, SystemMessage) and msg.subtype == "init":
            served = (msg.data or {}).get("model") or served
        if isinstance(msg, AssistantMessage) and msg.model:
            served = msg.model
    return served


async def codex_served_model(model: str, workspace: Path) -> str | None:
    """Authoritative model string from Codex's thread metadata."""
    opts = RunOptions(workspace=workspace, model=model, sandbox=SandboxPolicy.READ_ONLY)
    config = CodexConfig(config_overrides=(), cwd=str(workspace))
    async with AsyncCodex(config) as cx:
        thread = await cx.thread_start(**build_thread_kwargs(opts))
        handle = thread.turn("Reply with exactly: OK", **build_turn_kwargs(opts))
        if inspect.isawaitable(handle):  # SDK returns a coroutine yielding the handle
            handle = await handle
        served = None
        async for note in handle.stream():
            payload = getattr(note, "payload", None)
            for obj in (payload, getattr(payload, "thread", None), getattr(payload, "turn", None)):
                m = getattr(obj, "model", None)
                if isinstance(m, str):
                    served = m
        try:
            meta = await thread.read()
            served = served or getattr(meta, "model", None)
        except Exception:  # noqa: BLE001
            pass
        return served


TIMEOUT = 120  # seconds per phase


async def run_pair(backend: str, model: str) -> None:
    tag = f"{backend}-{model}".replace(".", "_")
    print(f"... {backend} {model}: api run", flush=True)
    try:
        agent = UnifiedAgent(backend, ws(tag + "-api"), model=model, sandbox=SandboxPolicy.READ_ONLY)
        r = await asyncio.wait_for(agent.run("Reply with exactly the word: OK"), TIMEOUT)
    except asyncio.TimeoutError:
        print(f"[FAIL] {backend:6s} requested={model:20s} api run TIMED OUT after {TIMEOUT}s", flush=True)
        return
    print(f"... {backend} {model}: probe served-model", flush=True)
    probe = claude_served_model if backend == "claude" else codex_served_model
    try:
        served = await asyncio.wait_for(probe(model, ws(tag + "-probe")), TIMEOUT)
    except asyncio.TimeoutError:
        served = "<probe timed out>"
    match = isinstance(served, str) and model in served
    mark = "PASS" if (r.success and match) else "FAIL"
    print(
        f"[{mark}] {backend:6s} requested={model:20s} served={served!r} "
        f"run_success={r.success} text={(r.text or '').strip()[:24]!r}",
        flush=True,
    )


async def main() -> int:
    pairs = [
        ("claude", "claude-sonnet-4-6"),
        ("claude", "claude-opus-4-8"),
        ("codex", "gpt-5.4-mini"),
        ("codex", "gpt-5.5"),
    ]
    for backend, model in pairs:  # sequential: isolate any single-pair hang
        await run_pair(backend, model)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
