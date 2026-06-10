"""Shared tools both agents can call — defined exactly once.

Both backends launch `python -m unified_agent.tool_server examples.demo_tools:REGISTRY`
and the model sees identical names: mcp__unified__add_numbers, mcp__unified__count_files.
"""

from pathlib import Path

from unified_agent import ToolRegistry

REGISTRY = ToolRegistry("unified")


@REGISTRY.tool()
def add_numbers(a: float, b: float) -> str:
    """Add two numbers and return the exact sum. Use for any arithmetic the user requests."""
    return f"{a} + {b} = {a + b}"


@REGISTRY.tool()
def count_files(directory: str = ".") -> str:
    """Count files in a directory (non-recursive). Use when asked how many files exist."""
    p = Path(directory)
    if not p.is_dir():
        return f"error: {directory} is not a directory"
    n = sum(1 for c in p.iterdir() if c.is_file())
    return f"{n} file(s) in {p.resolve()}"
