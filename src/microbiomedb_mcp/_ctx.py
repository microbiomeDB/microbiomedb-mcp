"""Helpers for resolving an MCP Context to a Session.

Pulled into its own module so every tool can do the same one-liner
lookup without duplicating the (mildly defensive) session-id extraction.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import Context

from .sessions import Session, SessionLimitExceeded, SessionManager

# Sentinel for callers that don't have a real MCP context (e.g. the
# /healthz route, smoke tests). Kept obvious so it's easy to spot in logs.
DEFAULT_SESSION_ID = "default-session"


def session_id_from_ctx(ctx: Context | None) -> str:
    """Best-effort extraction of the MCP session id from a Context.

    Different SDK versions expose it under different names; we try the
    common ones and fall back to a default. The default is fine when the
    server is reached over stdio or a tool is invoked outside a real
    request - the session is still scoped per-process but won't be split
    by client.
    """
    if ctx is None:
        return DEFAULT_SESSION_ID
    sid: Any = getattr(ctx, "session_id", None)
    if not sid:
        rc = getattr(ctx, "request_context", None)
        sid = getattr(rc, "session_id", None) if rc is not None else None
    if not sid:
        # streamable-http transport names it "client_id" in some versions.
        sid = getattr(ctx, "client_id", None)
    return sid or DEFAULT_SESSION_ID


async def session_from_ctx(manager: SessionManager,
                           ctx: Context | None) -> Session:
    sid = session_id_from_ctx(ctx)
    try:
        return await manager.get_or_create(sid)
    except SessionLimitExceeded as e:
        # Surface as a normal RuntimeError so FastMCP turns it into an MCP
        # error response rather than a 500.
        raise RuntimeError(str(e)) from e
