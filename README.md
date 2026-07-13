# unified_agent

Drive **OpenAI Codex** and **Claude Code** as interchangeable agent units from Python:
the same task input, the same custom tools, the same skills, the same normalized
events and results — on either backend, or both at once via `SuperAgent`.

Built on the official SDKs:
[`claude-agent-sdk`](https://pypi.org/project/claude-agent-sdk/) (bundles the Claude Code CLI)
and [`openai-codex`](https://pypi.org/project/openai-codex/) (bundles the codex binary,
drives `codex app-server`).

Verified live (2026-06-11): both backends called the **byte-identical** custom tool
`mcp__unified__add_numbers({'a': 20.25, 'b': 21.75})` and both discovered and executed the
**same SKILL.md** installed by one installer.

## How the unification works

| Concern | Mechanism | Claude Code | Codex |
|---|---|---|---|
| Input | `Task(instruction, context_files, context, skill)` rendered per backend | prompt string | prompt string (`$skill` mention syntax) |
| Tools | one `ToolRegistry`, served by `python -m unified_agent.tool_server pkg.mod:REGISTRY` over **stdio MCP** | `mcp_servers` option + `mcp__unified__*` allow-list | `-c mcp_servers.unified.*` config overrides |
| Skills | one canonical dir in the open [Agent Skills](https://agentskills.io) format, installed by symlink into both discovery dirs | `<ws>/.claude/skills/` | `<ws>/.agents/skills/` |
| Sandbox | `SandboxPolicy` | permission system (`dontAsk`/`acceptEdits`/`bypassPermissions`) | OS sandbox (`read-only`/`workspace-write`/`danger-full-access`) |
| Output | normalized `AgentEvent` stream → `UnifiedResult` | messages from `query()` | app-server notifications |
| Structured output | `output_schema=` (JSON Schema) | native `output_format` | native `output_schema` |
| Sessions | `resume=result.session_id` | `resume` option | `thread_resume` |

```
            ┌────────────────┐        ┌─────────────────────────┐
 Task ─────►│  UnifiedAgent  │───────►│ ClaudeCodeBackend       │──► claude code CLI
            │  (or SuperAgent│        │ CodexBackend            │──► codex app-server
            │   fan-out)     │        └───────────┬─────────────┘
            └──────┬─────────┘                    │ both spawn the SAME
                   │ installs                     ▼
                   │ skills ──► ws/.claude/skills + ws/.agents/skills (symlinks)
                   └─ tools ──► python -m unified_agent.tool_server pkg.mod:REGISTRY
                                (stdio MCP; tools named mcp__unified__<tool> in BOTH)
```

## Install

Backends are opt-in extras — each SDK bundles a ~200MB vendor runtime, so
install only what you use:

```bash
pip install -e '.[dev]'        # this repo: both backends + pytest

# as a dependency in another project (pick one):
pip install 'unified-agent[all] @ git+https://<repo-url>'     # both backends
pip install 'unified-agent[claude] @ git+https://<repo-url>'  # Claude Code only
pip install 'unified-agent[codex] @ git+https://<repo-url>'   # Codex only
```

Both SDKs are pre-1.0 and move fast, so they are pinned with upper bounds
(`<0.3` / `<0.2`). Treat every pin bump as a behavior change — most
claude-agent-sdk releases swap the bundled CLI — and re-verify (see Tests).

Auth (uses your existing logins):
- **Claude:** Claude Code login (`claude /login`) or `ANTHROPIC_API_KEY`.
- **Codex:** `codex login` (ChatGPT plan) or API-key auth; cached in `~/.codex/auth.json`.

## Quickstart

```python
# mytools.py — tools must live in an importable module (the MCP server imports them)
from unified_agent import ToolRegistry

REGISTRY = ToolRegistry("unified")

@REGISTRY.tool()
def add_numbers(a: float, b: float) -> str:
    """Add two numbers and return the exact sum."""
    return f"{a} + {b} = {a + b}"
```

```python
import asyncio
from unified_agent import SuperAgent, Task, UnifiedAgent, SandboxPolicy

def make(backend: str) -> UnifiedAgent:
    return UnifiedAgent(
        backend=backend,                      # "claude" | "codex"
        workspace="./workspace",
        sandbox=SandboxPolicy.WORKSPACE_WRITE,
        tools="mytools:REGISTRY",             # same tools on both
        skills_dir="./skills",                # same SKILL.md dirs on both
        instructions="Be terse.",             # appended to each native system prompt
        # model="claude-opus-4-8" / "gpt-5.5" (backend-native names; None = default)
    )

async def main():
    squad = SuperAgent({"claude": make("claude"), "codex": make("codex")})
    task = Task(
        instruction="Use the add_numbers tool to add 20.25 and 21.75.",
        context_files=["README.md"],          # same context for both
        # skill="word-count-report",          # explicit skill invocation on both
    )
    results = await squad.run_all(task)       # concurrent
    for name, r in results.items():
        print(name, r.success, r.text, r.usage, r.cost_usd)

asyncio.run(main())
```

Single unit, streaming, structured output, resume:

```python
agent = make("codex")

async for event in agent.stream(task):        # SessionStarted / AssistantText / ToolCall / ...
    print(event)

r = await agent.run(task, output_schema={
    "type": "object",
    "properties": {"sum": {"type": "number"}},
    "required": ["sum"], "additionalProperties": False,
})
print(r.structured_output)                    # parsed + validated on both backends

r2 = await agent.run("now double it", resume=r.session_id)
```

## Live process monitoring

Opt into a durable, cross-process event timeline without changing caller code:

```bash
export UNIFIED_AGENT_MONITOR_DB=/absolute/path/outside-agent-workspaces/agent-monitor.db
python -m your_project
```

Or configure it explicitly and attach project metadata:

```python
from unified_agent import SQLiteRunMonitor

monitor = SQLiteRunMonitor("/var/lib/unified-agent/monitor.db")
agent = UnifiedAgent("codex", workspace="./workspace", monitor=monitor)
result = await agent.run(
    "Inspect the authorized target",
    monitor_metadata={"project": "pentestgpt", "role": "executor"},
)
print(result.monitor_run_id)
```

`SQLiteRunMonitor` commits each normalized event with a per-run sequence number while the provider
is active, so a separate process can call `list_runs()` and `list_events(after_sequence=...)` to
tail it. Codex command starts/output deltas and MCP progress are normalized; Claude tool calls and
results already arrive as live events. Reasoning and raw provider payloads are omitted by default.

The database includes prompts, commands, tool inputs/results, and target output. Store it outside
agent-writable workspaces, never commit it, and protect it as a sensitive pentest artifact. See the
[monitor/debugger architecture and staged web plan](docs/process-monitor-design.md).

### Graphical inspector

Install the optional local web UI:

```bash
pip install 'unified-agent[monitor-web] @ git+https://<repo-url>'
```

From this checkout, evaluate the complete interface without provider credentials or token spend:

```bash
PYTHONPATH=. .venv/bin/python -m unified_agent.monitor_web \
  --database /tmp/unified-agent-inspector-demo.db --demo --open
```

For real runs, point the agent and inspector at the same database:

```bash
export UNIFIED_AGENT_MONITOR_DB=/secure/path/agent-monitor.db

# Terminal 1: read-only browser inspector
python -m unified_agent.monitor_web --database "$UNIFIED_AGENT_MONITOR_DB" --open

# Terminal 2: any project that constructs UnifiedAgent
python -m your_project
```

The inspector provides a searchable run list, live status, sequence-grounded SSE updates, grouped
command/tool timelines, streaming output, file changes, terminal results, usage/cost, prompts, and
project metadata. It serves only local packaged assets, exposes no mutation endpoints, and refuses
non-loopback network bindings.

## Demo

```bash
python -m examples.demo --backend both                          # shared MCP tool on both
python -m examples.demo --backend both --skill word-count-report \
    --task "Produce the word-count report for the text files in this workspace."
```

## Sandbox mapping (read the fine print)

| `SandboxPolicy` | Claude Code | Codex |
|---|---|---|
| `READ_ONLY` | `dontAsk` + only `Read/Glob/Grep` + MCP tools (no shell at all) | OS-sandboxed, may still run read-only commands |
| `WORKSPACE_WRITE` (default) | `acceptEdits` + file tools + `Bash` | `workspace-write` (network off by default) |
| `FULL_ACCESS` | `bypassPermissions` | `danger-full-access` |

**Honest asymmetry:** Claude Code has no OS sandbox by default — `WORKSPACE_WRITE`
auto-approves `Bash`, which is not OS-confined to the workspace. Codex enforces its
policy at the OS level. Treat `FULL_ACCESS` (and Claude `WORKSPACE_WRITE`) as trusted-
environment settings.

## Writing cross-agent tools and skills

- **Tool names:** `[a-z][a-z0-9_]*`, fully-qualified `mcp__<server>__<tool>` ≤ 64 bytes
  (Codex hard limit; hyphens banned because Codex rewrites them). Enforced at registration.
- **Tool env:** Codex does *not* pass your environment to MCP servers; `unified_agent`
  passes `PYTHONPATH` + your `extra_env` explicitly to both, so behavior is identical.
- **Skills:** keep to the agentskills.io baseline (frontmatter `name` + `description`).
  `lint_skill` warns about Claude-only syntax (`$ARGUMENTS`, `` !`cmd` ``, `${CLAUDE_*}`)
  that Codex would ignore.
- Claude runs hermetically with `setting_sources=["project"]`: only the workspace's
  `.claude/` (skills, CLAUDE.md) is loaded — your personal `~/.claude` config never
  leaks into runs.

## Result model

`UnifiedResult`: `success`, `text`, `structured_output`, `usage`
(input/cached/output/reasoning tokens — provider counting differs, values passed through),
`cost_usd` (Claude only; Codex reports tokens, not dollars), `session_id` (resumable),
`tool_calls`, `file_changes`, `events` (full normalized trace), `error`, and
`monitor_run_id` when monitoring is enabled.

## Tests

```bash
pytest -q                      # offline unit tests, no network or auth needed
python -m examples.demo ...    # live runs (cost money, need auth)
```

**On every SDK pin bump** (an SDK upgrade is an agent-behavior change, even at
patch level): `python scripts/verify_sdk_surface.py` (free — asserts every SDK
name/shape the code relies on) → `pytest -q` (free, offline) →
`python -m examples.demo --backend both` (cents — live behavior).

## Limitations (PoC)

- No mid-turn steering/interrupt; no interactive permission prompts (headless policies only).
- Thinking-display knobs not surfaced (reasoning `effort` is: `UnifiedAgent(effort="xhigh")`).
- Sessions resume per backend; they do not transfer across backends.
- Windows untested (symlink installer has a copy fallback: `skills_install_mode="copy"`).

## Layout

```
unified_agent/        package (agent, backends, events, monitoring, monitor_web, tools, skills)
unified_agent/monitor_web_assets/  local graphical inspector (HTML/CSS/JS)
unified_agent/backends/  claude_code.py, codex.py, base.py
examples/             demo_tools.py, skills/word-count-report/, demo.py
tests/                offline unit tests incl. a real stdio MCP round-trip
docs/superpowers/     design spec + implementation plan
scripts/              verify_sdk_surface.py (re-run after SDK upgrades)
```
