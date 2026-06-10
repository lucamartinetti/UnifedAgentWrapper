"""Verify the installed SDK surfaces match the API facts the implementation relies on.

Run: .venv/bin/python scripts/verify_sdk_surface.py
Exits non-zero with a report if any relied-upon name is missing.
"""

import dataclasses
import inspect
import sys

failures: list[str] = []


def check(cond: bool, what: str) -> None:
    print(("OK   " if cond else "FAIL ") + what)
    if not cond:
        failures.append(what)


# --- claude-agent-sdk ---------------------------------------------------
import claude_agent_sdk as cas

opt_fields = {f.name for f in dataclasses.fields(cas.ClaudeAgentOptions)}
for f in [
    "cwd", "model", "permission_mode", "allowed_tools", "disallowed_tools",
    "mcp_servers", "setting_sources", "system_prompt", "output_format",
    "resume", "max_turns", "env", "include_partial_messages", "stderr",
]:
    check(f in opt_fields, f"ClaudeAgentOptions.{f}")

for name in [
    "query", "ClaudeSDKClient", "AssistantMessage", "UserMessage",
    "SystemMessage", "ResultMessage", "TextBlock", "ThinkingBlock",
    "ToolUseBlock", "ToolResultBlock", "CLINotFoundError", "ProcessError",
]:
    check(hasattr(cas, name), f"claude_agent_sdk.{name}")

rm_fields = {f.name for f in dataclasses.fields(cas.ResultMessage)}
for f in ["subtype", "result", "usage", "total_cost_usd", "session_id",
          "num_turns", "duration_ms", "is_error"]:
    check(f in rm_fields, f"ResultMessage.{f}")
check("structured_output" in rm_fields, "ResultMessage.structured_output")
am_fields = {f.name for f in dataclasses.fields(cas.AssistantMessage)}
check("error" in am_fields, "AssistantMessage.error")
print("StreamEvent present:", hasattr(cas, "StreamEvent"))

# --- openai-codex -------------------------------------------------------
import openai_codex as oc

for name in ["Codex", "AsyncCodex", "CodexConfig", "Sandbox"]:
    check(hasattr(oc, name), f"openai_codex.{name}")

cfg_fields = {f.name for f in dataclasses.fields(oc.CodexConfig)}
for f in ["config_overrides", "cwd", "env", "codex_bin"]:
    check(f in cfg_fields, f"CodexConfig.{f}")

sandbox_members = {m.name for m in oc.Sandbox}
print("Sandbox members:", sandbox_members)
check({"read_only", "workspace_write"} <= sandbox_members, "Sandbox read_only/workspace_write")

ts_sig = inspect.signature(oc.AsyncCodex.thread_start)
print("thread_start params:", list(ts_sig.parameters))
for p in ["model", "cwd", "sandbox", "developer_instructions"]:
    check(p in ts_sig.parameters, f"thread_start({p}=)")
check("approval_mode" in ts_sig.parameters, "thread_start(approval_mode=)")
check(hasattr(oc.AsyncCodex, "thread_resume"), "AsyncCodex.thread_resume")

# Thread / TurnHandle surface
thread_cls = getattr(oc, "AsyncThread", None) or getattr(oc, "Thread", None)
print("thread class:", thread_cls)
if thread_cls is not None:
    run_sig = inspect.signature(thread_cls.run)
    print("Thread.run params:", list(run_sig.parameters))
    check("output_schema" in run_sig.parameters, "Thread.run(output_schema=)")
    check(hasattr(thread_cls, "turn"), "Thread.turn")
else:
    failures.append("no Thread/AsyncThread class exported")

print("\nexports sample:", [n for n in dir(oc) if not n.startswith("_")][:60])

# approval mode enum
am = getattr(oc, "ApprovalMode", None)
print("ApprovalMode:", am and [m.name for m in am])

# notification/turn result models
for name in ["TurnResult", "ThreadTokenUsage"]:
    has = hasattr(oc, name)
    print(f"openai_codex.{name}:", has)

# --- mcp ----------------------------------------------------------------
from mcp.server.fastmcp import FastMCP

m = FastMCP("probe")
check(hasattr(m, "add_tool"), "FastMCP.add_tool")
check(hasattr(m, "run"), "FastMCP.run")
check(hasattr(m, "list_tools"), "FastMCP.list_tools")
print("add_tool sig:", inspect.signature(m.add_tool))
from mcp.client.stdio import stdio_client, StdioServerParameters  # noqa: F401
from mcp import ClientSession  # noqa: F401
check(True, "mcp client imports")

print("\n" + ("ALL OK" if not failures else f"{len(failures)} FAILURES"))
sys.exit(1 if failures else 0)
