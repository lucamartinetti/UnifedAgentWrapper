# UnifiedAgent process monitor and debugger

Status: wrapper telemetry and the read-only graphical inspector are implemented; PentestGPT
integration and authenticated remote deployment are staged below.

## Goal

Make a long-running `UnifiedAgent` invocation observable while it is still active, regardless of
whether Claude Code or Codex is underneath it and regardless of which higher-level project invoked
the wrapper. The first user-facing product is a read-only web debugger: run list, live action
timeline, command/tool output, terminal status, usage, and searchable history.

"Process monitor" here primarily means the logical provider and action lifecycle. Codex exposes a
PTY process identifier for many commands, but it is not an operating-system PID, and Claude's
public SDK does not expose its CLI PID. Host CPU, memory, descendant PIDs, and container metrics are
a separate runtime adapter in a later phase; they must not be inferred from unstable SDK internals.

## Evidence from the current projects

- `UnifiedAgent.stream()` is already the correct provider-neutral seam, but `run()` consumes it
  without any observer, run identity, durable state, or external subscription path.
- The Codex SDK already sends `item/started`, `item/commandExecution/outputDelta`, and
  `item/mcpToolCall/progress`. The wrapper previously discarded or ignored them and emitted a
  command only after completion.
- PentestGPT's `EpisodeRunner` fsyncs normalized events to per-episode JSONL files. This is durable
  and useful for recovery, but it is application-specific, has no run-discovery index, and cannot
  monitor other wrapper consumers.
- [LuaN1aoAgent](https://github.com/SanMuzZzZz/LuaN1aoAgent) uses a separate worker and dashboard,
  SQLite/WAL persistence, an event log, and SSE. Those are useful patterns. Its in-process broker,
  swallowed persistence errors, one-second database polling, timestamp-based frontend de-duplication,
  and duplicated history/SSE loading should not become the wrapper contract.

## Module and interface

```text
Claude SDK ─┐
            ├─> provider adapter ─> normalized AgentEvent ─> caller/PentestGPT
Codex SDK ──┘                              │
                                          └─> RunRecorder ─> RunMonitor adapter
                                                                  │
                                                       SQLite/WAL (first adapter)
                                                                  │
                                               REST + resumable SSE (next slice)
                                                                  │
                                                           read-only web UI
```

The external monitoring seam is intentionally small:

```python
recorder = monitor.start_run(
    run_id=run_id,
    backend=backend,
    workspace=workspace,
    prompt=rendered_prompt,
    metadata=project_metadata,
)
recorder.record(normalized_event)
recorder.finish(status="succeeded" | "failed" | "cancelled" | "incomplete")
```

The monitor module owns event ordering, timestamps, serialization, redaction, truncation, storage,
and query behavior. Provider adapters know only how to normalize provider events. PentestGPT policy
such as Supervisor/Executor roles, task leases, evidence, and completion remains outside the
wrapper and is supplied only as opaque monitor metadata.

## First slice: wrapper telemetry foundation

Implemented in `unified_agent.monitoring`:

- `RunMonitor` / `RunRecorder` protocols;
- `SQLiteRunMonitor` using WAL, a 30-second busy timeout, one transaction per visible event, and
  monotonically increasing per-run sequence numbers, with forward-schema rejection;
- durable `running`, `succeeded`, `failed`, `cancelled`, and `incomplete` states;
- wrapper-process host/PID ownership and `last_event_at` for local crash/staleness diagnosis;
- cross-process queries through `get_run`, `list_runs`, and incremental `list_events(after_sequence=)`;
- caller-provided `run_id` and opaque `monitor_metadata`, with the effective ID returned as
  `UnifiedResult.monitor_run_id`;
- explicit construction or zero-call-site-change activation through `UNIFIED_AGENT_MONITOR_DB`;
- reasoning and backend-native raw payloads omitted by default, text bounded to 200,000 characters,
  and the database file set to mode `0600`;
- Codex command-start, output-delta, MCP-progress, process correlation, and duration events.

The monitor is deliberately fail-closed once enabled: a persistence/order failure terminates the
wrapper invocation instead of letting an unattended penetration test continue invisibly. A future
configuration may add an explicitly named best-effort mode, but it must never be the safety default.

### Activation

No caller change:

```bash
export UNIFIED_AGENT_MONITOR_DB=/absolute/path/outside-agent-workspaces/agent-monitor.db
python -m your_project
```

Explicit configuration and project tags:

```python
from unified_agent import SQLiteRunMonitor, UnifiedAgent

monitor = SQLiteRunMonitor("/var/lib/unified-agent/monitor.db")
agent = UnifiedAgent("codex", workspace="./workspace", monitor=monitor)
result = await agent.run(
    "Inspect the authorized target",
    monitor_metadata={"project": "pentestgpt", "role": "executor", "episode_id": "ep-7"},
)
print(result.monitor_run_id)
```

The database contains prompts, assistant text, commands, tool inputs/results, and potentially target
secrets. Keep it outside every agent-writable workspace, do not commit it, and treat it as a
sensitive pentest artifact. Reasoning/raw omission is data minimization, not a complete secret
redaction system.

## Event contract

Every stored event has `(run_id, sequence, recorded_at, event_type, payload)`. Ordering is by
`sequence`; timestamps are display metadata and are never used for identity or de-duplication.

The action timeline correlates by `call_id`:

- Claude shell/tool action: `tool_call` -> zero or more provider messages -> `tool_result`;
- Codex command: `command_started` -> zero or more `command_output_delta` -> `command_run`;
- Codex MCP tool: `tool_call` -> zero or more `tool_progress` -> `tool_result`;
- provider turn: `session_started` -> actions/text/files -> `turn_completed`.

`command_run.output` is the provider's terminal aggregate. A UI that already rendered deltas should
replace/reconcile the command body on completion rather than append the aggregate a second time.

## Staged delivery plan

### Phase 1 — finish and release the wrapper foundation

1. Add explicit migrations when the first schema change is needed; current code rejects unknown
   future schemas and tests cancellation, truncation, permissions, and simultaneous writers.
2. Verify both SDK surfaces and run the complete offline suite.
3. Run one cheap authenticated no-op/tool call on Claude and Codex to capture the real notification
   order; turn those captures into replay fixtures.
4. Publish as a new pre-1.0 minor version because the normalized event union grows.

Exit criterion: both providers produce a durable live run whose ordered timeline is visible from a
second process before `TurnCompleted`.

### Phase 2 — generic read-only monitor server and web debugger (implemented)

1. Add an optional `monitor-web` extra (Starlette/uvicorn), leaving core wrapper installs dependency-free.
2. Implement:
   - `GET /api/runs?status=&limit=`;
   - `GET /api/runs/{run_id}`;
   - `GET /api/runs/{run_id}/events?after_sequence=&limit=`;
   - `GET /api/runs/{run_id}/stream`, resumable SSE with `id: sequence` and `Last-Event-ID`.
3. Build a small timeline UI: active/history list, provider/session metadata, grouped actions, live
   command output, file changes, usage/cost, terminal errors, and raw JSON inspection.
4. Use one history path: initial REST snapshot followed by SSE strictly after the last sequence.
5. Bind to loopback by default, disable permissive CORS, escape all target output, and require a
   bearer token before allowing non-loopback binding.
6. Mark a run visually stale when its local owner PID is gone; settle it as orphaned only through
   an explicit collector recovery operation, never from an age timeout alone.

Exit criterion: refresh/reconnect neither loses nor duplicates events, and a completed history is
identical to the live timeline.

Implementation notes: `python -m unified_agent.monitor_web --demo --open` generates credential-free
Claude- and Codex-shaped activity. The server uses one REST history path followed by SSE strictly
after the last sequence; final streams close explicitly. The UI is entirely local, renders
untrusted values through DOM text nodes, carries a restrictive Content Security Policy, has no
mutation routes, and rejects non-loopback bindings.

### Phase 3 — PentestGPT integration without domain leakage

Current red gate: with wrapper 0.3.0 forced onto PentestGPT, its existing
`_event_record(CommandStarted(...))` raises `TypeError: unsupported UnifiedAgent event`; one trace
test also intentionally pins the recorded wrapper version to `0.2.0`. Do not bump the dependency
until both expectations are updated together.

1. Bump the pinned wrapper commit/version in `pentestgpt_agent`.
2. Have `EpisodeRunner` pass stable metadata: project run ID, episode ID, role, state revision,
   task ID, and attempt ID. Keep wrapper monitor IDs unique per actual invocation.
3. Extend PentestGPT's exhaustive trace serializer for the new progress events. Progress/deltas are
   diagnostic only; only terminal `CommandRun`/`ToolResult` events remain eligible evidence.
4. Keep PentestGPT's JSONL trace and SQLite Memory Kernel as canonical recovery/evidence stores.
   The generic monitor database is a read model, not canonical pentest memory.
5. Add a Pentest-specific UI overlay that reads canonical task/attempt state separately and joins it
   to wrapper runs by metadata. Do not put task graphs or pentest policy into `unified_agent`.

Exit criterion: Supervisor and Executor episodes appear under one PentestGPT run, while recovery,
receipt grounding, and retry decisions remain unchanged with monitoring on or off.

### Phase 4 — XBOW smoke and regression qualification

The current `xbow-benchmark` Makefile labels its single-target framework smoke as "pending rewire";
the first slice therefore runs only its offline harness tests. Do not interpret the naive bare-CLI
variant as validation of wrapper monitoring.

1. Unit smoke: run the wrapper suite and the XBOW harness parser/command tests.
2. Integration smoke: run one local fake backend through the framework while a second process tails
   the monitor database.
3. Docker smoke: run one explicitly authorized, inexpensive XBOW challenge with one provider; mount
   the monitor database outside the agent workspace and retain the event DB with benchmark logs.
4. Repeat with the other provider and compare event-shape conformance, not model success rate.
5. Only then run a broader benchmark subset. Monitoring acceptance is zero missing/duplicate
   terminal actions and bounded overhead, not flags captured.

Suggested budgets: event visibility p95 under 500 ms, monitor write overhead under 5% wall time,
bounded DB growth, and exact terminal action counts against the native JSONL transcript.

### Phase 5 — control plane and hardened deployment

Read-only observation comes first. Abort, steer, approval, and task injection require a separate
authorization and control design:

- a managed worker registry instead of guessing/killing PIDs;
- provider-specific cancellation behind one explicit control interface;
- idempotent commands with an audit trail and optimistic run-state checks;
- authentication, CSRF protection, authorization by project/run, and default-deny remote access.

For tamper resistance, add a second `RunMonitor` adapter that sends events over a Unix socket to a
host/sidecar collector. The collector owns SQLite outside the agent container and can optionally add
Docker/cgroup CPU, memory, and descendant-process telemetry. Direct SQLite remains the simple local
development adapter, not the strongest trust boundary.

## Immediate verification commands

```bash
cd UnifedAgentWrapper
PYTHONPATH=. .venv/bin/python -m pytest -q tests/test_monitoring.py
PYTHONPATH=. .venv/bin/python -m pytest -q tests/test_monitor_web.py
PYTHONPATH=. .venv/bin/python -m pytest -q
PYTHONPATH=. .venv/bin/python scripts/verify_sdk_surface.py
ruff check unified_agent tests/test_monitoring.py tests/test_codex_backend.py
```
