"""Live verification matrix: drive unified_agent's public API against REAL backends.

Each check runs on Claude Code and Codex (concurrently per check) and prints
captured evidence. Costs real usage; needs both CLIs authenticated.

    .venv/bin/python scripts/live_verify.py --only structured,context
    .venv/bin/python scripts/live_verify.py            # everything

Exit code 0 iff every selected check passes on both backends.
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from unified_agent import SandboxPolicy, Task, TextDelta, TurnCompleted, UnifiedAgent

ROOT = Path(__file__).resolve().parent.parent
WS_ROOT = ROOT / "examples" / "workspace" / "live_verify"
BACKENDS = ["claude", "codex"]
PER_RUN_TIMEOUT = 300  # seconds per agent.run


@dataclass
class Evidence:
    check: str
    backend: str
    ok: bool
    details: str


def ws_for(check: str, backend: str) -> Path:
    ws = WS_ROOT / check / backend
    if ws.exists():
        shutil.rmtree(ws)
    ws.mkdir(parents=True)
    return ws


def snippet(text: str, n: int = 160) -> str:
    return " ".join((text or "").split())[:n]


async def timed_run(agent: UnifiedAgent, task, **kw):
    t0 = time.monotonic()
    result = await asyncio.wait_for(agent.run(task, **kw), timeout=PER_RUN_TIMEOUT)
    return result, time.monotonic() - t0


# --- checks -----------------------------------------------------------------

async def check_structured(backend: str) -> Evidence:
    agent = UnifiedAgent(backend, ws_for("structured", backend), sandbox=SandboxPolicy.READ_ONLY)
    schema = {
        "type": "object",
        "properties": {"answer": {"type": "integer"}},
        "required": ["answer"],
        "additionalProperties": False,
    }
    r, dt = await timed_run(
        agent,
        "Compute 6*7. Return JSON matching the schema, nothing else.",
        output_schema=schema,
    )
    got = r.structured_output
    ok = r.success and isinstance(got, dict) and got.get("answer") == 42
    return Evidence(
        "structured", backend, ok,
        f"success={r.success} structured_output={got!r} ({dt:.0f}s)",
    )


async def check_resume(backend: str) -> Evidence:
    ws = ws_for("resume", backend)
    agent = UnifiedAgent(backend, ws, sandbox=SandboxPolicy.READ_ONLY)
    r1, dt1 = await timed_run(
        agent, "Remember this codeword: MOONSTONE-88. Reply with exactly: OK"
    )
    if not (r1.success and r1.session_id):
        return Evidence("resume", backend, False,
                        f"turn1 failed: success={r1.success} session={r1.session_id} err={r1.error}")
    r2, dt2 = await timed_run(
        agent,
        "What codeword did I give you earlier in this conversation? Reply with only the codeword.",
        resume=r1.session_id,
    )
    ok = r2.success and "MOONSTONE-88" in r2.text
    return Evidence(
        "resume", backend, ok,
        f"turn1={snippet(r1.text, 30)!r} session={r1.session_id[:14]}… "
        f"turn2={snippet(r2.text, 60)!r} ({dt1:.0f}s+{dt2:.0f}s)",
    )


async def check_read_only(backend: str) -> Evidence:
    """PROBE: a READ_ONLY agent is asked to write; the file must NOT appear."""
    ws = ws_for("read_only", backend)
    agent = UnifiedAgent(backend, ws, sandbox=SandboxPolicy.READ_ONLY)
    r, dt = await timed_run(
        agent,
        "Create a file named hack.txt containing the word pwned in the current "
        "working directory. If you are unable to, reply exactly: CANNOT-WRITE",
    )
    leaked = (ws / "hack.txt").exists()
    ok = not leaked
    return Evidence(
        "read_only(PROBE)", backend, ok,
        f"hack.txt exists={leaked} agent said: {snippet(r.text, 90)!r} ({dt:.0f}s)",
    )


async def check_workspace_write(backend: str) -> Evidence:
    ws = ws_for("workspace_write", backend)
    agent = UnifiedAgent(backend, ws, sandbox=SandboxPolicy.WORKSPACE_WRITE)
    r, dt = await timed_run(
        agent,
        "Create a file named hello.txt in the current working directory whose "
        "content is exactly: hello unified\nThen reply with exactly: DONE",
    )
    f = ws / "hello.txt"
    content = f.read_text() if f.exists() else None
    ok = r.success and f.exists() and "hello unified" in (content or "")
    return Evidence(
        "workspace_write", backend, ok,
        f"hello.txt exists={f.exists()} content={content!r} "
        f"file_changes={[c.path for c in r.file_changes]} ({dt:.0f}s)",
    )


async def check_context(backend: str) -> Evidence:
    ws = ws_for("context", backend)
    fact = ws / "facts.md"
    fact.write_text("# Internal notes\n\nThe project codename is ZEPHYR-77.\n")
    agent = UnifiedAgent(backend, ws, sandbox=SandboxPolicy.READ_ONLY)
    r, dt = await timed_run(
        agent,
        Task(
            instruction="What is the project codename? Reply with only the codename.",
            context_files=[fact],
        ),
    )
    ok = r.success and "ZEPHYR-77" in r.text
    return Evidence("context", backend, ok, f"text={snippet(r.text, 60)!r} ({dt:.0f}s)")


async def check_instructions(backend: str) -> Evidence:
    agent = UnifiedAgent(
        backend,
        ws_for("instructions", backend),
        sandbox=SandboxPolicy.READ_ONLY,
        instructions="Always end your final reply with the single word BANANA",
    )
    r, dt = await timed_run(agent, "Say hello in at most five words.")
    tail = (r.text or "").rstrip().rstrip(".!").upper()
    ok = r.success and tail.endswith("BANANA")
    return Evidence("instructions", backend, ok, f"text={snippet(r.text, 80)!r} ({dt:.0f}s)")


async def check_stream(backend: str) -> Evidence:
    agent = UnifiedAgent(
        backend, ws_for("stream", backend), sandbox=SandboxPolicy.READ_ONLY, stream_text=True
    )
    deltas: list[str] = []
    final: TurnCompleted | None = None

    async def consume():
        nonlocal final
        async for ev in agent.stream("Reply with one short sentence about the ocean."):
            if isinstance(ev, TextDelta):
                deltas.append(ev.text)
            elif isinstance(ev, TurnCompleted):
                final = ev

    t0 = time.monotonic()
    await asyncio.wait_for(consume(), timeout=PER_RUN_TIMEOUT)
    dt = time.monotonic() - t0
    joined = "".join(deltas)
    ok = bool(final and final.success and len(deltas) >= 2 and joined.strip())
    return Evidence(
        "stream", backend, ok,
        f"deltas={len(deltas)} joined={snippet(joined, 70)!r} "
        f"final_success={final.success if final else None} ({dt:.0f}s)",
    )


async def check_invalid_model(backend: str) -> Evidence:
    """PROBE: bogus model must come back as success=False with an error, not a crash."""
    agent = UnifiedAgent(
        backend,
        ws_for("invalid_model", backend),
        sandbox=SandboxPolicy.READ_ONLY,
        model="totally-bogus-model-9000",
    )
    r, dt = await timed_run(agent, "Say hi.")
    ok = (r.success is False) and bool(r.error or r.text)
    return Evidence(
        "invalid_model(PROBE)", backend, ok,
        f"success={r.success} error={snippet(r.error or '', 110)!r} ({dt:.0f}s)",
    )


CHECKS = {
    "structured": check_structured,
    "context": check_context,
    "instructions": check_instructions,
    "stream": check_stream,
    "read_only": check_read_only,
    "workspace_write": check_workspace_write,
    "resume": check_resume,
    "invalid_model": check_invalid_model,
}


async def run_backend(check_name: str, fn, backend: str) -> Evidence:
    try:
        return await fn(backend)
    except Exception as e:  # noqa: BLE001 - report, don't crash the matrix
        return Evidence(check_name, backend, False, f"EXCEPTION {type(e).__name__}: {e}")


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", default=None, help="comma-separated check names")
    parser.add_argument("--backends", default=",".join(BACKENDS))
    args = parser.parse_args()
    selected = [s.strip() for s in (args.only or ",".join(CHECKS)).split(",") if s.strip()]
    backends = [b.strip() for b in args.backends.split(",")]

    all_evidence: list[Evidence] = []
    for name in selected:
        fn = CHECKS[name]
        print(f"\n=== {name} ===", flush=True)
        results = await asyncio.gather(*(run_backend(name, fn, b) for b in backends))
        for ev in results:
            all_evidence.append(ev)
            mark = "PASS" if ev.ok else "FAIL"
            print(f"  [{mark}] {ev.backend:6s} {ev.details}", flush=True)

    print("\n" + "=" * 72)
    failed = [e for e in all_evidence if not e.ok]
    for e in all_evidence:
        print(f"{'PASS' if e.ok else 'FAIL'}  {e.check:22s} {e.backend}")
    print(f"\n{len(all_evidence) - len(failed)}/{len(all_evidence)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
