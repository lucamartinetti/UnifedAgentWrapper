# Unified Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `unified_agent`, a Python package that drives OpenAI Codex (`openai-codex`) and Claude Code (`claude-agent-sdk`) as interchangeable agent units sharing one task format, one Python tool registry (served over stdio MCP to both), one Agent-Skills directory, and one normalized event/result model — plus a `SuperAgent` fan-out.

**Architecture:** Spec at `docs/superpowers/specs/2026-06-11-unified-agent-design.md`. Thin backend adapters over the two official SDKs; an external FastMCP stdio server (`python -m unified_agent.tool_server pkg.mod:REGISTRY`) gives both agents byte-identical tools `mcp__unified__<name>`; a skills installer symlinks one canonical skills dir into `.claude/skills/` and `.agents/skills/`; backends emit a shared `AgentEvent` union folded into `UnifiedResult`.

**Tech Stack:** Python 3.12, `claude-agent-sdk` (latest 0.2.x), `openai-codex` (0.1.0b3, pre-release), `mcp` (FastMCP 1.x server + client for tests), `pyyaml`, `pytest` + `pytest-asyncio`.

**Critical API facts (verified against live docs 2026-06-11 — re-verify against installed packages in Task 0):**
- Claude: `query(prompt=..., options=ClaudeAgentOptions(...))`; options used: `cwd`, `model`, `permission_mode` (`"dontAsk"|"acceptEdits"|"bypassPermissions"`), `allowed_tools`, `disallowed_tools`, `mcp_servers={key: {"type":"stdio","command":...,"args":[...],"env":{...}}}`, `setting_sources=["project"]`, `system_prompt={"type":"preset","preset":"claude_code","append":...}`, `output_format={"type":"json_schema","schema":...}`, `resume`, `max_turns`, `env`, `include_partial_messages`, `stderr` callback. Messages: `SystemMessage(subtype="init", data)`, `AssistantMessage(content=[TextBlock|ThinkingBlock|ToolUseBlock], error)`, `UserMessage(content may hold ToolResultBlock)`, `ResultMessage(subtype, result, structured_output, usage, total_cost_usd, session_id, num_turns, duration_ms, is_error)`, `StreamEvent(event)` raw API deltas.
- Codex: `AsyncCodex(CodexConfig(config_overrides=tuple[str,...], cwd=...))`; `thread_start(model=, cwd=, sandbox=Sandbox.read_only|workspace_write|full_access, developer_instructions=)` → `Thread`; `thread.run(input, output_schema=dict|None) -> TurnResult(final_response, items, usage, status, error)`; `thread.turn(input,...) -> TurnHandle` with `.stream()` notifications + `.run()`; `thread_resume(id)`; notifications: `thread/started`, `item/completed` (item.root.type ∈ agentMessage|reasoning|commandExecution|fileChange|mcpToolCall|webSearch|...), `item/agentMessage/delta`, `turn/completed`, `error`. Usage: `ThreadTokenUsage{last,total:TokenUsageBreakdown{input_tokens,cached_input_tokens,output_tokens,reasoning_output_tokens}}`. MCP config via `-c`-style overrides: `mcp_servers.unified.command="..."`, `.args=["-m",...]`, `.env={ "K" = "V" }`, `.startup_timeout_sec=30`, `.tool_timeout_sec=120`, `.required=true`, `.default_tools_approval_mode="auto"`. Codex does NOT inherit parent env into MCP servers.
- Tool naming both hosts: `mcp__{server_key}__{tool}`; charset `[a-z0-9_]`, fq ≤ 64 bytes.

---

### Task 0: Scaffold + verify installed SDK surfaces

**Files:** Create `pyproject.toml`; create `scripts/verify_sdk_surface.py`.

- [ ] Write `pyproject.toml`: package `unified-agent` 0.1.0, `requires-python = ">=3.11"`, deps `claude-agent-sdk`, `openai-codex`, `mcp`, `pyyaml`; dev extra `pytest`, `pytest-asyncio`; `[tool.pytest.ini_options] asyncio_mode = "auto"`, `testpaths=["tests"]`; setuptools packages `unified_agent`, `unified_agent.backends`.
- [ ] Install: `.venv/bin/python -m pip install -e '.[dev]' --pre` (pre needed for `openai-codex` beta).
- [ ] Write and run `scripts/verify_sdk_surface.py` that imports both SDKs and prints: `ClaudeAgentOptions` dataclass field names; presence of `query`, message classes, `output_format`/`setting_sources`/`skills` fields; `openai_codex` exports (`Codex`, `AsyncCodex`, `CodexConfig`, `Sandbox`, enum values), `inspect.signature` of `thread_start`/`Thread.run`/`Thread.turn`, notification method registry sample, `TurnResult`/`ThreadTokenUsage` model fields; `mcp.server.fastmcp.FastMCP` has `add_tool`/`tool`/`run` and async `list_tools`. Expected: all names resolve; any mismatch → update plan/spec before continuing.
- [ ] Commit `chore: scaffold package, pin SDKs, verify API surfaces`.

### Task 1: Core types + events (`unified_agent/types.py`, `unified_agent/events.py`)

No tests (pure dataclasses); exercised by every later test.

- [ ] `types.py`: `class SandboxPolicy(str, Enum): READ_ONLY="read_only"; WORKSPACE_WRITE="workspace_write"; FULL_ACCESS="full_access"`. `@dataclass UnifiedUsage: input_tokens=0; cached_input_tokens=0; output_tokens=0; reasoning_output_tokens=0` with `__add__`. Errors: `UnifiedAgentError(Exception)`, `BackendUnavailableError`, `ToolRegistryError`, `SkillError`, `ToolServerError`, `AgentRunError`, `AgentAuthError(AgentRunError)`.
- [ ] `events.py`: frozen dataclasses `SessionStarted(session_id)`, `TextDelta(text)`, `AssistantText(text)`, `Reasoning(text)`, `ToolCall(name, input, call_id)`, `ToolResult(call_id, output, is_error)`, `CommandRun(command, exit_code, output)`, `FileChanged(path, kind)`, `TurnCompleted(success, final_text, usage: UnifiedUsage, cost_usd, session_id, duration_ms, structured_output, stop_reason, error)`, `RawEvent(backend, kind, data)`; `AgentEvent = Union[...]`.
- [ ] `types.py`: `@dataclass UnifiedResult: backend, success, text, structured_output, usage, cost_usd, session_id, duration_ms, tool_calls: list[ToolCall], file_changes: list[FileChanged], events: list[AgentEvent], error: str|None` + `@dataclass RunOptions: workspace: Path; model: str|None; sandbox: SandboxPolicy; instructions: str|None; tool_server: ToolServerSpec|None; output_schema: dict|None; resume: str|None; max_turns: int|None; extra_env: dict[str,str]; stream_text: bool` + `@dataclass ToolServerSpec: server_name: str; command: list[str]; env: dict[str,str]`.
- [ ] Commit `feat: core types and normalized event model`.

### Task 2: Task input (`unified_agent/task.py`, `tests/test_task.py`)

- [ ] Failing tests: instruction-only render returns instruction verbatim; `context_files=[readme]` embeds `## Context file: README.md` header + file body; `context="extra"` appended under `## Additional context`; `skill="deploy-check"` renders `$deploy-check` mention for backend `"codex"` and `the 'deploy-check' skill` + Skill-tool phrasing for `"claude"`; missing file → `FileNotFoundError`; >200_000-char file truncated with `[... truncated ...]` marker. Run `pytest tests/test_task.py -v` → FAIL (module missing).
- [ ] Implement `@dataclass Task: instruction: str; context_files: Sequence[str|Path] = (); context: str|None = None; skill: str|None = None` with `render(backend: str) -> str`; `Task.coerce(task: str|Task)` classmethod. Run tests → PASS.
- [ ] Commit `feat: unified task input with per-backend rendering`.

### Task 3: Tool registry (`unified_agent/tools.py`, `tests/test_tools.py`, fixture `tests/fixture_registry.py`)

- [ ] Failing tests: register via `@REG.tool()` keeps fn callable and listed in `REG.tools`; explicit `name=`/`description=` honored, default name = fn name and description = first docstring line; duplicate name → `ToolRegistryError`; invalid names (`Add`, `add-nums`, `9x`) → error; fq-length >64 (long server+tool) → error; `registry_spec(REG)` on the fixture module returns `"tests.fixture_registry:REG"`; registry defined in `__main__` → error mentioning importable module; `resolve_registry("tests.fixture_registry:REG")` returns same object. Run → FAIL.
- [ ] Implement:

```python
NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
@dataclass class ToolEntry: fn: Callable; name: str; description: str
class ToolRegistry:
    def __init__(self, server_name: str = "unified"):
        # validate server_name with NAME_RE; record defining module:
        self._module = sys._getframe(1).f_globals.get("__name__")
    def tool(self, name=None, description=None): ...  # decorator; validates name, fq len, dupes
def resolve_registry(spec: str) -> ToolRegistry  # "mod.path:ATTR" import + getattr + type check
def registry_spec(reg: ToolRegistry) -> str      # import reg._module, scan vars() for identity, reject __main__
```

- [ ] Run → PASS. Commit `feat: tool registry with cross-host name validation`.

### Task 4: MCP tool server (`unified_agent/tool_server.py`, `tests/test_tool_server.py`)

- [ ] Failing tests: `build_server(REG)` returns FastMCP whose `await server.list_tools()` contains registered names/descriptions; **stdio round-trip** (the core promise): spawn `[sys.executable, "-m", "unified_agent.tool_server", "tests.fixture_registry:REG"]` with `mcp.client.stdio.stdio_client(StdioServerParameters(command=sys.executable, args=[...], env={**os.environ, "PYTHONPATH": repo_root}))` + `ClientSession`; `initialize()`, `list_tools()` shows `add_numbers`, `call_tool("add_numbers", {"a":2,"b":3})` returns text containing `"5"`. Run → FAIL.
- [ ] Implement: `build_server(registry) -> FastMCP` (`FastMCP(registry.server_name)` + `add_tool(entry.fn, name=..., description=...)` — fall back to `mcp.tool(name=...)(fn)` if `add_tool` signature differs); `main(argv)` parses spec, `build_server(resolve_registry(spec)).run(transport="stdio")`; `if __name__ == "__main__": main(sys.argv[1:])`; all logging to stderr.
- [ ] Implement `build_tool_server_spec(tools: ToolRegistry|str, extra_env) -> ToolServerSpec` in `tools.py`: command `[sys.executable, "-m", "unified_agent.tool_server", spec]`; env = `{"PYTHONPATH": os.pathsep.join(dirs)}` where dirs = package root of `unified_agent` + root dir of the registry module (walk up `module.__name__.count('.')` levels from `module.__file__`) + existing `sys.path` entries NOT included — keep just those two + `extra_env`. Unit test the env computation.
- [ ] Run → PASS (round-trip test may take ~2s). Commit `feat: stdio MCP tool server serving a registry by import path`.

### Task 5: Skills (`unified_agent/skills.py`, `tests/test_skills.py`)

- [ ] Failing tests (tmp_path fixtures writing SKILL.md): `load_skill(dir)` parses frontmatter name/description/body; name≠dirname → `SkillError`; invalid names (`Deploy`, `a--b`, `-x`, 65 chars) → `SkillError`; description empty or >1024 → `SkillError`; `lint_skill` returns warnings for `$ARGUMENTS` and `` !`cmd` `` in body; `install_skills(workspace, source_dir)` creates symlinks `ws/.claude/skills/<n>` and `ws/.agents/skills/<n>` → canonical, returns installed list; idempotent on second call; replaces stale symlink; `mode="copy"` copies tree; existing real dir without `force` → `SkillError`.
- [ ] Implement: `NAME_RE = ^[a-z0-9]+(-[a-z0-9]+)*$` (≤64); frontmatter via `yaml.safe_load` of the `--- ... ---` block; `@dataclass Skill: name, description, path, body, warnings`; `discover_skills(source_dir)`; `install_skills(workspace, source, mode="symlink", force=False)` writing both discovery dirs; `make_skill(dir, name, description, body)` helper for examples/tests.
- [ ] Run → PASS. Commit `feat: agent-skills loader and dual-host installer`.

### Task 6: UnifiedAgent core + SuperAgent with FakeBackend (`unified_agent/backends/base.py`, `unified_agent/agent.py`, `tests/test_agent.py`)

- [ ] Failing tests with `FakeBackend` (scripted event list, records received prompt/opts): `run()` returns `UnifiedResult` with `text` = TurnCompleted.final_text (fallback: last AssistantText), success/usage/cost/session_id propagated, `tool_calls`/`file_changes` collected, `events` complete; `run()` on backend that raises → `success=False`, `error` set (no exception); `stream()` propagates exception; prompt seen by backend equals `task.render(backend.name)`; `SuperAgent({"a":…,"b":…}).run_all(task)` returns both keys and runs concurrently (two FakeBackends sleeping 0.2s finish in <0.35s total); `run_sync` works.
- [ ] Implement `backends/base.py`:

```python
class AgentBackend(Protocol):
    name: str
    def stream(self, prompt: str, opts: RunOptions) -> AsyncIterator[AgentEvent]: ...
```

- [ ] Implement `agent.py`: `UnifiedAgent(backend="claude"|"codex"|AgentBackend, workspace=".", model=None, sandbox=WORKSPACE_WRITE, tools=None, skills_dir=None, instructions=None, extra_env=None, stream_text=False)`; lazy backend factory (`claude`→`ClaudeCodeBackend()`, `codex`→`CodexBackend()`, import errors → `BackendUnavailableError`); `_prepare()` installs skills (once) + builds `ToolServerSpec`; `async run(task, output_schema=None, resume=None, max_turns=None) -> UnifiedResult`; `async stream(task, ...)`; `run_sync`; module-level `collect(events) -> UnifiedResult` folding logic; `class SuperAgent: __init__(dict[str, UnifiedAgent]); async run_all(task, **kw) -> dict[str, UnifiedResult]` via `asyncio.gather`; `agents` property.
- [ ] Run → PASS. Commit `feat: UnifiedAgent orchestration core and SuperAgent fan-out`.

### Task 7: Claude backend (`unified_agent/backends/claude_code.py`, `tests/test_claude_backend.py`)

- [ ] Failing tests: `build_options(opts)` for each `SandboxPolicy` yields the spec's `permission_mode`/`allowed_tools`/`disallowed_tools`; always `setting_sources=["project"]`, `cwd=str(workspace)`; tool server present → `mcp_servers={"unified": {"type":"stdio","command":py,"args":["-m","unified_agent.tool_server",spec],"env":...}}` + `"mcp__unified__*"` in allowed_tools; `instructions` → system_prompt preset dict with `append`; `output_schema` → `output_format` dict; `resume`/`max_turns` passthrough; `stream_text` → `include_partial_messages=True`. Normalizer tests using real SDK classes: `SystemMessage(subtype="init", data={"session_id":"s1","mcp_servers":[{"name":"unified","status":"connected"}]})` → `SessionStarted("s1")`; failed MCP status → raises `ToolServerError`; `AssistantMessage([TextBlock, ThinkingBlock, ToolUseBlock(name="Bash", input={"command":"ls"})])` → AssistantText/Reasoning/ToolCall; `UserMessage` w/ ToolResultBlock → ToolResult; `ResultMessage(...)` → TurnCompleted with usage/cost mapped (`usage{input_tokens,cache_read_input_tokens→cached, output_tokens}`); auth error message (`AssistantMessage.error="authentication_failed"`) → `AgentAuthError`; StreamEvent text delta → TextDelta when stream_text.
- [ ] Implement `ClaudeCodeBackend.stream()`: `async for message in query(prompt=prompt, options=build_options(opts)): yield from normalize_message(message, opts)`; wrap `CLINotFoundError/ProcessError` → `AgentRunError`/`AgentAuthError`.
- [ ] Run → PASS. Commit `feat: Claude Code backend (claude-agent-sdk)`.

### Task 8: Codex backend (`unified_agent/backends/codex.py`, `tests/test_codex_backend.py`)

- [ ] Failing tests: `build_config_overrides(tool_server_spec)` returns exactly (order-stable): `mcp_servers.unified.command="<sys.executable>"`, `.args=["-m","unified_agent.tool_server","tests.fixture_registry:REG"]` (json-encoded array = valid TOML), `.env={ "PYTHONPATH" = "..." }` inline table (quoted keys), `.startup_timeout_sec=30`, `.tool_timeout_sec=120`, `.required=true`, `.default_tools_approval_mode="auto"`; `build_thread_kwargs(opts)` maps sandbox→`Sandbox.*`, model/cwd/developer_instructions passthrough. Normalizers with `SimpleNamespace` fakes: `("thread/started", payload.thread_id)` → SessionStarted; `item/completed` with root `type="agentMessage"`→AssistantText, `reasoning`→Reasoning(text from `.text` or summary), `commandExecution`→CommandRun+ToolCall/ToolResult pair, `mcpToolCall`(server,tool,arguments,result,error,status)→ToolCall(name=`mcp__unified__add_numbers`)+ToolResult, `fileChange(changes=[{path,kind}])`→FileChanged per change; `item/agentMessage/delta`→TextDelta (only when stream_text); `turn/completed`→TurnCompleted (usage.total mapped into UnifiedUsage incl. cached + reasoning; success from turn.status; final_text from accumulated agentMessage; structured_output json-parsed when output_schema set; `error` notification → AgentRunError mapping).
- [ ] Implement `CodexBackend.stream()`: lazily construct `AsyncCodex(CodexConfig(config_overrides=build_config_overrides(...), cwd=str(workspace)))` per run (async context manager); `thread = await codex.thread_start(**kwargs)` or `thread_resume(resume)`; iterate `thread.turn(prompt, output_schema=...)` handle `.stream()` if available else `await thread.run(...)` + post-hoc item normalization; ensure final TurnCompleted carries thread.id as session_id; close client in `finally`.
- [ ] Run → PASS. Commit `feat: Codex backend (openai-codex app-server SDK)`.

### Task 9: Examples + README

**Files:** Create `examples/demo_tools.py` (REGISTRY with `add_numbers`, `repo_file_count`), `examples/skills/word-count-report/SKILL.md` (portable, no Claude-only syntax), `examples/demo.py` (builds workspace dir, SuperAgent over both backends, same task forcing the MCP tool + skill, prints normalized results side by side), `README.md` (what/why, architecture diagram, quickstart, auth requirements, sandbox asymmetry note, examples).

- [ ] Write files; `python -m examples.demo --help` parses (argparse: `--backend claude|codex|both`, `--task`, `--workspace`).
- [ ] Commit `docs: README and runnable demo with shared tools and skills`.

### Task 10: Live smoke (developer auth, costs cents)

- [ ] `examples/demo.py --backend codex` then `--backend claude` with task: *"Use the add_numbers tool to add 20.25 and 21.75, then state the result."* Assert in output: ToolCall `mcp__unified__add_numbers` observed; result text contains `42`. Skill listing check: task *"Which skills are available to you?"* mentions `word-count-report` on both.
- [ ] Fix whatever reality disagrees with (likely codex config-override quoting or claude permission mode), re-run unit tests.
- [ ] Commit `test: live smoke verified on both backends` (+ fixes).

### Task 11: Final verification

- [ ] `pytest -q` → all pass; `python -m examples.demo --backend both` (or documented partial if one auth unavailable); update task list; final commit; summary with evidence per superpowers:verification-before-completion.

## Self-review notes

- Spec coverage: every spec contract (task render, tool naming, env passing, skills dual-install, sandbox table, events, result, errors, SuperAgent, tests, live smoke) maps to Tasks 1–10.
- Types referenced across tasks are defined in Task 1/3 (`RunOptions`, `ToolServerSpec`, events) and used consistently (`build_options`, `build_config_overrides`, `normalize_*`).
- Deliberate deviation from plan-skill letter: full final code lives in the implementation commits; plan carries interfaces, behaviors, exact assertions, and the verified SDK touchpoints (the non-guessable parts), since executor == author with research in context.
