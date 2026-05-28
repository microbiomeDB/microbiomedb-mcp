"""Session lifecycle / introspection tools.

The session itself is created lazily on first tool call, so a client never
*needs* to call ``create_session`` explicitly. These tools exist mainly to
let a client report memory usage or proactively release resources.
"""

from __future__ import annotations

from mcp.server.fastmcp import Context, FastMCP

from .._ctx import session_from_ctx
from ..rserve_client import call
from ..sessions import SessionManager


def register(mcp: FastMCP, manager: SessionManager) -> None:

    @mcp.tool(description=(
        "Report this session's R worker info (pid, handle/job counts, "
        "approximate memory)."
    ))
    async def session_info(ctx: Context) -> dict:
        sess = await session_from_ctx(manager, ctx)
        info = await call(sess.conn, "mcp_sessionInfo")
        if isinstance(info, dict):
            info["session_id"] = sess.session_id
            info["active_jobs_python_view"] = sorted(sess.active_jobs)
        return info

    @mcp.tool(description=(
        "Explicitly close this session and free its R worker. Subsequent "
        "tool calls on the same MCP connection will lazily create a fresh "
        "session (with NO retained data)."
    ))
    async def close_session(ctx: Context) -> bool:
        sess = await session_from_ctx(manager, ctx)
        return await manager.close(sess.session_id)
