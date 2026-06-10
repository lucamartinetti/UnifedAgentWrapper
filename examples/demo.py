"""Run the SAME task — same tools, same skills — on Codex and/or Claude Code.

Usage:
    python -m examples.demo --backend both
    python -m examples.demo --backend codex --task "Use the add_numbers tool to add 2 and 3."
    python -m examples.demo --backend claude --skill word-count-report

Auth: Claude uses your Claude Code login or ANTHROPIC_API_KEY; Codex uses your
`codex login` (ChatGPT) or API-key auth. Runs cost normal usage.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from unified_agent import SandboxPolicy, SuperAgent, Task, ToolCall, UnifiedAgent

HERE = Path(__file__).resolve().parent
DEFAULT_TASK = (
    "Use the add_numbers tool to add 20.25 and 21.75, then state the result "
    "in one short sentence."
)


def build_agents(backends: list[str], workspace: Path, model: dict[str, str | None]) -> SuperAgent:
    agents = {
        name: UnifiedAgent(
            backend=name,
            workspace=workspace,
            model=model.get(name),
            sandbox=SandboxPolicy.WORKSPACE_WRITE,
            tools="examples.demo_tools:REGISTRY",
            skills_dir=HERE / "skills",
        )
        for name in backends
    }
    return SuperAgent(agents)


def print_result(name: str, result) -> None:
    print(f"\n{'=' * 70}\n[{name}] success={result.success} session={result.session_id}")
    if result.error:
        print(f"[{name}] error: {result.error}")
    tool_calls = [t for t in result.tool_calls if isinstance(t, ToolCall)]
    for t in tool_calls:
        print(f"[{name}] tool call: {t.name}({t.input})")
    u = result.usage
    print(
        f"[{name}] usage: in={u.input_tokens} cached={u.cached_input_tokens} "
        f"out={u.output_tokens} reasoning={u.reasoning_output_tokens}"
        + (f" cost=${result.cost_usd:.4f}" if result.cost_usd is not None else "")
    )
    print(f"[{name}] text:\n{result.text}")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", choices=["claude", "codex", "both"], default="both")
    parser.add_argument("--task", default=DEFAULT_TASK)
    parser.add_argument("--skill", default=None, help="explicitly invoke an installed skill")
    parser.add_argument("--workspace", default=str(HERE / "workspace"))
    parser.add_argument("--claude-model", default=None)
    parser.add_argument("--codex-model", default=None)
    args = parser.parse_args()

    backends = ["claude", "codex"] if args.backend == "both" else [args.backend]
    workspace = Path(args.workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "notes.md").write_text("alpha beta gamma\n")
    (workspace / "story.txt").write_text("one two three four five\n")

    squad = build_agents(
        backends, workspace, {"claude": args.claude_model, "codex": args.codex_model}
    )
    task = Task(instruction=args.task, skill=args.skill)

    results = await squad.run_all(task)
    for name, result in results.items():
        print_result(name, result)
    return 0 if all(r.success for r in results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
