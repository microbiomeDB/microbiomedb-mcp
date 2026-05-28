"""Result retrieval + handle lifecycle tools (synchronous)."""

from __future__ import annotations

from mcp.server.fastmcp import Context, FastMCP

from .._ctx import session_from_ctx
from ..rserve_client import call
from ..sessions import SessionManager


def register(mcp: FastMCP, manager: SessionManager) -> None:

    @mcp.tool(description=(
        "Fetch a ComputeResult by handle. `format` is one of 'data.table' "
        "(default; column-oriented rows) or 'igraph' (returns a new handle "
        "for the graph view, e.g. for correlation results)."
    ))
    async def get_compute_result(result_handle: str, ctx: Context,
                                 format: str = "data.table",
                                 limit: int = 100000) -> dict:
        sess = await session_from_ctx(manager, ctx)
        return await call(sess.conn, "mcp_getComputeResult",
                          result_handle, format, limit)

    @mcp.tool(description=(
        "Fetch a ComputeResult joined with sample metadata variables from "
        "the given dataset handle. `variables` is a list of metadata "
        "variable names."
    ))
    async def get_compute_result_with_metadata(result_handle: str,
                                               dataset_handle: str,
                                               variables: list[str],
                                               ctx: Context,
                                               limit: int = 100000) -> dict:
        sess = await session_from_ctx(manager, ctx)
        return await call(sess.conn, "mcp_getComputeResultWithMetadata",
                          result_handle, dataset_handle, variables, limit)

    @mcp.tool(description=(
        "Render a correlation-network HTML widget from a correlation "
        "ComputeResult handle. Writes to the shared uploads volume and "
        "returns the file path (also accessible via the `download_file` tool)."
    ))
    async def correlation_network(result_handle: str, ctx: Context) -> dict:
        sess = await session_from_ctx(manager, ctx)
        return await call(sess.conn, "mcp_correlationNetwork", result_handle)

    @mcp.tool(description="List all live handles in this session (datasets, collections, results, networks).")
    async def list_handles(ctx: Context) -> list[dict]:
        sess = await session_from_ctx(manager, ctx)
        return await call(sess.conn, "mcp_listHandles")

    @mcp.tool(description="Release a handle so its underlying R object can be garbage-collected.")
    async def free_handle(handle: str, ctx: Context) -> bool:
        sess = await session_from_ctx(manager, ctx)
        return bool(await call(sess.conn, "mcp_free", handle))
