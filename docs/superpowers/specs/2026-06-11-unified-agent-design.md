# Unified Agent — Design Spec

**Date:** 2026-06-11
**Status:** Approved for implementation (autonomous session — design decisions documented here for review)

## Goal

One Python package, `unified_agent`, that drives **both** OpenAI Codex and Claude Code as
interchangeable agent units:

- **Same input** — an instruction string, README/context files, or inline context.
- **Same tools** — custom Python tools defined once, callable by both agents with
  byte-identical tool names.
- **Same skills** — one skill directory in the open Agent Skills format (SKILL.md,
  agentskills.io) discovered by both agents.
- **Same output** — a normalized event stream and a `UnifiedResult` with text, usage,
  and structured output.
- A **`SuperAgent`** that holds several `UnifiedAgent` units and runs the same task
  across them concurrently.

## Verified foundations (live docs, 2026-06-11)

| Fact | Claude Code | Codex |
|---|---|---|
| Python SDK | `claude-agent-sdk` 0.2.95 (bundles Claude Code CLI) | `openai-codex` 0.1.0b3 (beta; bundles codex binary via `openai-codex-cli-bin`; drives `codex app-server` over JSON-RPC) |
| Entry point | `query(prompt, options=ClaudeAgentOptions(...))` async iterator | `AsyncCodex().thread_start(...)` → `thread.turn(input).stream()` notifications |
| Custom tools via MCP | `mcp_servers={"unified": {type:"stdio", command, args, env}}`, allow with `allowed_tools=["mcp__unified__*"]` | config override `mcp_servers.unified.{command,args,env,...}`; tools surface as `mcp__unified__<tool>` |
| MCP env | stdio server inherits parent env + per-server `env` | **no parent env inheritance** — only whitelist (`env_vars`) + explicit `env` map |
| Skills discovery | `<ws>/.claude/skills/` (needs `setting_sources` incl. `"project"`) | `<ws>/.agents/skills/` (cwd→repo root), `~/.agents/skills/` |
| Skills format | SKILL.md frontmatter `name`, `description` (+ Claude extras) | same standard; reads `name`, `description`, ignores the rest |
| Sandbox | no OS sandbox by default; permission system (`permission_mode`, `allowed_tools`, `disallowed_tools`) | OS sandbox: `read-only` / `workspace-write` / `danger-full-access` |
| Structured output | `output_format={"type":"json_schema","schema":...}` → `ResultMessage.structured_output` | `output_schema=<json schema>` → `final_response` is JSON text |
| Resume | `resume=<session_id>` | `thread_resume(<thread_id>)` |
| Usage | tokens + `total_cost_usd` | tokens only |
| Auth | `ANTHROPIC_API_KEY` or Claude subscription login (shared with CLI) | ChatGPT login cached in `~/.codex/auth.json`, or API key |

## Approaches considered

1. **Official SDK wrappers + external MCP tool server + skills installer** ← **chosen.**
   Full feature access (streaming, structured output, resume), typed APIs, the SDKs own
   process management. One identical tool path for both backends.
2. *CLI subprocess unification* (`claude -p --output-format stream-json`, `codex exec --json`):
   re-implements what the SDKs already do, weaker typing, no interrupt/steer path. Rejected.
3. *MCP-host inversion* (`codex mcp-server` as a tool inside a single host): experimental,
   asymmetric capabilities, loses native skills/sessions. Rejected.

Within (1), tools are served by **one external stdio MCP server for both backends**
(not in-process SDK MCP for Claude): a single code path is the whole point of the PoC,
and external FastMCP servers also support `structuredContent`, which the Python in-process
`@tool` path drops. Trade-off: registered tools must live in an importable module
(the server subprocess imports them by path) — same constraint as `multiprocessing`.

## Architecture

```
unified_agent/
  types.py        # SandboxPolicy, UnifiedUsage, UnifiedResult, errors
  events.py       # normalized AgentEvent union
  task.py         # Task: instruction + context_files + context + skill → render(backend)
  tools.py        # ToolRegistry + @registry.tool — name validation, import-path contract
  tool_server.py  # `python -m unified_agent.tool_server pkg.mod:REGISTRY` → FastMCP stdio
  skills.py       # Skill model (agentskills.io validation), installer → both discovery dirs
  backends/
    base.py       # AgentBackend protocol: stream(task, run_opts) -> AsyncIterator[AgentEvent]
    claude_code.py
    codex.py
  agent.py        # UnifiedAgent (run/stream/run_sync) + SuperAgent (run_all)
examples/         # importable tools module, shared skill, demo script
tests/            # unit tests (no network); live smoke gated by UA_LIVE=1
```

### Data flow

```
Task ──render(backend)──► prompt string
                              │
UnifiedAgent(backend, workspace, tools, skills, sandbox, model)
   │ 1. SkillInstaller.install(workspace)   → .claude/skills/* + .agents/skills/*  (symlinks)
   │ 2. tool server spec = [sys.executable, -m unified_agent.tool_server, "pkg.mod:REG"]
   │ 3. backend.stream(prompt, opts)        → yields normalized AgentEvents
   ▼
UnifiedResult(text, success, usage, cost_usd, session_id, tool_calls, file_changes,
              structured_output, raw events)
```

## Key contracts

### Tools

```python
# examples/demo_tools.py  (must be importable)
from unified_agent import ToolRegistry
REGISTRY = ToolRegistry("unified")          # server key → tools named mcp__unified__<name>

@REGISTRY.tool()
def add_numbers(a: float, b: float) -> str:
    """Add two numbers and return the sum."""
    return f"{a} + {b} = {a + b}"
```

- Name rules enforced at registration: `^[a-z][a-z0-9_]*$` and
  `len("mcp__" + server + "__" + name) <= 64` (Codex hard limit; hyphens are rewritten by
  Codex, so they are banned outright).
- Both backends spawn the same subprocess: `sys.executable -m unified_agent.tool_server pkg.mod:REGISTRY`.
- Env for the server is passed **explicitly** on both sides (Codex does not inherit parent
  env): `PYTHONPATH` for the registry module + caller-provided `extra_env`.
- Claude side: `allowed_tools=["mcp__unified__*"]`; Codex side: server `required` semantics +
  startup timeout raised to 30s.

### Skills

- Canonical source: any directory of `<skill-name>/SKILL.md` (+ `scripts/`, `references/`, ...).
- `install_skills(workspace, source)` validates per the agentskills spec
  (name `^[a-z0-9]+(-[a-z0-9]+)*$` ≤64 == dirname; description 1–1024) and links each skill into
  `workspace/.claude/skills/<name>` **and** `workspace/.agents/skills/<name>`
  (symlink default, copy fallback).
- Claude options always set `setting_sources=["project"]` so project skills load
  hermetically (no user/local config bleed). Codex discovers `.agents/skills` from cwd.
- `Task.skill="name"` renders an explicit invocation hint per backend
  (Codex: `$name` mention; Claude: "use the <name> skill").
- Shared skills must avoid Claude-only preprocessing (`!`cmd`` injection, `$ARGUMENTS`)
  — validator warns on those.

### Sandbox mapping (honest, documented asymmetry)

| `SandboxPolicy` | Claude (permissions, no OS sandbox) | Codex (OS sandbox) |
|---|---|---|
| `READ_ONLY` | `permission_mode="dontAsk"`, allow `Read/Glob/Grep` + MCP tools, disallow `Write/Edit/NotebookEdit/Bash` | `Sandbox.read_only` |
| `WORKSPACE_WRITE` | `permission_mode="acceptEdits"`, allow file tools + `Bash` + MCP | `Sandbox.workspace_write` |
| `FULL_ACCESS` | `permission_mode="bypassPermissions"` | `Sandbox.full_access` |

Note: in `READ_ONLY` Claude gets no shell at all (its Bash is unsandboxed), while Codex can
still run commands inside its read-only OS sandbox. Documented in README.

### Normalized events

`SessionStarted`, `TextDelta` (opt-in), `AssistantText`, `Reasoning`, `ToolCall`,
`ToolResult`, `CommandRun`, `FileChanged`, `TurnCompleted(usage)`, `RawEvent` (escape hatch).

Mapping highlights — Claude: `SystemMessage(init)`→SessionStarted, `TextBlock`→AssistantText,
`ThinkingBlock`→Reasoning, `ToolUseBlock`/`ToolResultBlock`→ToolCall/ToolResult (Bash calls
also→CommandRun), `ResultMessage`→TurnCompleted. Codex: `thread/started`→SessionStarted,
`item/completed{agentMessage}`→AssistantText, `{reasoning}`→Reasoning,
`{commandExecution}`→CommandRun, `{mcpToolCall}`→ToolCall+ToolResult,
`{fileChange}`→FileChanged, `item/agentMessage/delta`→TextDelta, `turn/completed`→TurnCompleted.

### Instructions / system prompt parity

`UnifiedAgent(instructions=...)` → Claude: `system_prompt={"type": "preset", "preset":
"claude_code", "append": instructions}` (keeps the agent prompt, appends);
Codex: `developer_instructions=instructions` on `thread_start` (keeps base prompt, appends).

### Result

```python
@dataclass
class UnifiedResult:
    backend: str                 # "claude" | "codex"
    success: bool
    text: str                    # final assistant message
    structured_output: Any|None  # parsed JSON when output_schema was given
    usage: UnifiedUsage          # input/cached_input/output/reasoning tokens
    cost_usd: float|None         # Claude only
    session_id: str|None         # claude session_id / codex thread_id (resumable)
    duration_ms: int|None
    tool_calls: list[ToolCall]
    file_changes: list[FileChanged]
    events: list[AgentEvent]     # full normalized trace
    error: str|None
```

## Error handling

- Missing SDK → `BackendUnavailableError` at construction with install hint.
- Auth failures surfaced as `AgentAuthError` (Claude: `AssistantMessage.error ==
  "authentication_failed"` / `ProcessError`; Codex: JSON-RPC auth errors).
- Tool-server failure: Claude init message reports `mcp_servers[].status == "failed"` →
  raise `ToolServerError`; Codex `required`/startup timeout produces a turn failure → same.
- A failed run returns `UnifiedResult(success=False, error=...)` from `run()`;
  `stream()` raises.

## Testing

- **Unit (default, no network):** task rendering per backend; registry validation
  (names, importability, fq-length); skill validation + installation into both dirs
  (tmp workspace); sandbox option mapping for both backends; event normalization from
  synthetic SDK objects; SuperAgent concurrency with a FakeBackend.
- **Live smoke (`UA_LIVE=1`):** one tiny read-only task per backend that forces the shared
  MCP tool, asserting the tool round-trip and a normalized result. Uses the developer's
  existing auth (Claude CLI login / `~/.codex/auth.json`).

## Non-goals (PoC)

Interactive permission prompts, mid-turn steering/interrupt, Windows, cross-backend session
transfer, cost normalization for Codex (tokens only), Claude OS-sandbox (`sandbox` option)
tuning, in-process SDK MCP server fast path.
