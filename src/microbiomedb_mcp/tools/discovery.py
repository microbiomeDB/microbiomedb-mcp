"""Discovery + metadata tools (all synchronous; fast on the R side).

The two curated-dataset tools (``list_curated_datasets`` /
``load_dataset``) are only registered when the Rserve worker reports
``has_curated_data`` in :func:`rserve_client.probe_capabilities`. The
other tools here operate on already-loaded ``MbioDataset`` handles and
are useful regardless of how the dataset was produced (curated load OR
user import), so they're always exposed.
"""

from __future__ import annotations

from mcp.server.fastmcp import Context, FastMCP

from .._ctx import session_from_ctx
from ..rserve_client import call
from ..sessions import SessionManager


def register(mcp: FastMCP, manager: SessionManager,
             has_curated_data: bool = True) -> None:

    if has_curated_data:
        @mcp.tool(description="List the names of curated MicrobiomeDB datasets available in this server.")
        async def list_curated_datasets(ctx: Context) -> list[str]:
            sess = await session_from_ctx(manager, ctx)
            return await call(sess.conn, "mcp_listCuratedDatasets")

        @mcp.tool(description=(
            "Load a curated dataset into this session by name. Returns a handle "
            "you can pass to compute and collection tools. Subsequent calls in "
            "the same session reuse the loaded data without reloading."
        ))
        async def load_dataset(name: str, ctx: Context) -> dict:
            sess = await session_from_ctx(manager, ctx)
            return await call(sess.conn, "mcp_loadDataset", name)

    @mcp.tool(description="Get the collection names available on a loaded MbioDataset.")
    async def get_collection_names(dataset_handle: str, ctx: Context) -> list[str]:
        sess = await session_from_ctx(manager, ctx)
        return await call(sess.conn, "mcp_getCollectionNames", dataset_handle)

    @mcp.tool(description=(
        "Get a Collection from a loaded MbioDataset, returning a new handle. "
        "Optional `format` matches MicrobiomeDB::getCollection (e.g. 'phyloseq')."
    ))
    async def get_collection(dataset_handle: str, name: str, ctx: Context,
                             format: str = "default") -> dict:
        sess = await session_from_ctx(manager, ctx)
        return await call(sess.conn, "mcp_getCollection", dataset_handle, name, format)

    @mcp.tool(description="List metadata variable names on a loaded MbioDataset.")
    async def get_metadata_variable_names(dataset_handle: str, ctx: Context) -> list[str]:
        sess = await session_from_ctx(manager, ctx)
        return await call(sess.conn, "mcp_getMetadataVariableNames", dataset_handle)

    @mcp.tool(description=(
        "Summarize metadata variables on a loaded MbioDataset. `variables` "
        "is an optional list of variable names; omit to summarize all."
    ))
    async def get_metadata_variable_summary(dataset_handle: str, ctx: Context,
                                            variables: list[str] | None = None) -> dict:
        sess = await session_from_ctx(manager, ctx)
        return await call(sess.conn, "mcp_getMetadataVariableSummary",
                          dataset_handle, variables or "")

    @mcp.tool(description=(
        "Fetch the sample metadata table for a loaded MbioDataset. Rows are "
        "auto-truncated to `limit` (default 1000); use the returned "
        "`__truncated__` flag to detect this."
    ))
    async def get_sample_metadata(dataset_handle: str, ctx: Context,
                                  limit: int = 1000) -> dict:
        sess = await session_from_ctx(manager, ctx)
        return await call(sess.conn, "mcp_getSampleMetadata", dataset_handle, limit)
