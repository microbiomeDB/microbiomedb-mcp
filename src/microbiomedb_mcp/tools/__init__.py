"""Tool registration entrypoints.

Each submodule defines a ``register(mcp, get_session)`` function that adds
its tools to the FastMCP instance. ``get_session`` is an async callable
that returns the current Session for the active MCP request.
"""

from . import compute, discovery, imports, jobs, results, session

__all__ = ["compute", "discovery", "imports", "jobs", "results", "session"]
