"""Same instruction to both agents: write a simple snake game.

Claude Code runs claude-opus-4-8 at xhigh effort; Codex runs gpt-5.5 at xhigh.
Each writes into its own workspace under examples/workspace/snake/<backend>/.
The instruction pins a tiny logic API so one conformance suite can grade both.
"""

from __future__ import annotations

import asyncio
import shutil
import time
from pathlib import Path

from unified_agent import SandboxPolicy, SuperAgent, Task, UnifiedAgent

ROOT = Path(__file__).resolve().parent.parent
SNAKE_ROOT = ROOT / "examples" / "workspace" / "snake"

INSTRUCTION = """\
Write a simple, playable snake game in Python.

Requirements:
1. Python 3.11+, standard library only (no third-party dependencies anywhere).
2. Layout (inside the current working directory):
   - snake/logic.py    pure game logic, importable with no UI/curses dependency
   - snake/__main__.py curses terminal UI so `python -m snake` plays the game
   - tests/            your own unit tests for the logic, all passing via
                       `python3 -m unittest discover -s tests`
3. snake/logic.py must expose exactly this public API (an external grader drives it):
   - class SnakeGame(width: int, height: int, seed: int | None = None)
   - attributes/properties:
       snake: list[tuple[int, int]]  # cells, HEAD FIRST; (x, y), origin (0,0) top-left,
                                     # x grows right, y grows down
       food: tuple[int, int]
       score: int                    # starts 0, +1 per food eaten
       game_over: bool
   - method step(direction: str) -> None  # one tick; direction in
       {"up", "down", "left", "right"}
   Game rules:
   - The snake starts with length 3, horizontal, head at (width//2, height//2),
     moving right; the body extends to the left of the head.
   - Requesting the opposite of the current movement direction is ignored
     (the snake keeps moving in its current direction).
   - Eating food grows the snake by 1 and increments score by 1; food then
     respawns on a uniformly random free cell using a deterministic RNG seeded
     with `seed` (same seed => same food sequence). Food never spawns on the snake.
   - Moving outside the width x height grid, or into the snake's own body,
     sets game_over = True (further step() calls are no-ops).
4. Keep it simple and readable. When done, reply with a one-paragraph summary
   of the design and how to play.
"""


def fresh_ws(backend: str) -> Path:
    ws = SNAKE_ROOT / backend
    if ws.exists():
        shutil.rmtree(ws)
    ws.mkdir(parents=True)
    return ws


async def main() -> int:
    squad = SuperAgent(
        {
            "claude": UnifiedAgent(
                "claude",
                fresh_ws("claude"),
                model="claude-opus-4-8",
                effort="xhigh",
                sandbox=SandboxPolicy.WORKSPACE_WRITE,
            ),
            "codex": UnifiedAgent(
                "codex",
                fresh_ws("codex"),
                model="gpt-5.5",
                effort="xhigh",
                sandbox=SandboxPolicy.WORKSPACE_WRITE,
            ),
        }
    )

    t0 = time.monotonic()
    results = await squad.run_all(Task(instruction=INSTRUCTION))
    wall = time.monotonic() - t0

    print(f"\nwall time: {wall:.0f}s")
    for name, r in results.items():
        ws = SNAKE_ROOT / name
        files = sorted(str(p.relative_to(ws)) for p in ws.rglob("*") if p.is_file())
        print(f"\n{'=' * 70}\n[{name}] success={r.success} duration_ms={r.duration_ms}")
        u = r.usage
        print(
            f"[{name}] usage: in={u.input_tokens} cached={u.cached_input_tokens} "
            f"out={u.output_tokens} reasoning={u.reasoning_output_tokens}"
            + (f" cost=${r.cost_usd:.4f}" if r.cost_usd is not None else "")
        )
        if r.error:
            print(f"[{name}] error: {r.error}")
        print(f"[{name}] files: {files}")
        print(f"[{name}] summary:\n{r.text}\n")
        (ws / "AGENT_SUMMARY.md").write_text(r.text or "")
    return 0 if all(r.success for r in results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
