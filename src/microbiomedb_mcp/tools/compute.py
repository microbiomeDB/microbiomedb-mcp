"""Async compute tools.

All compute operations on MicrobiomeDB are routed through the same
``start_*`` -> ``get_job_status`` -> ``get_job_result`` lifecycle so the
client never has to discover which ones might exceed the MCP request
deadline. The R-side mcparallel fork shares all session data via COW, so
starting a job does not duplicate the user's loaded datasets.

Arguments to the underlying R functions are passed as a generic ``args``
dict. Dataset / collection handles should be supplied as ``{"$handle":
"<uuid>"}`` stubs; the R shim resolves them to the registered S4 objects.

For ``differentialAbundance``, ``groupA`` / ``groupB`` may be either:
* an R expression string with ``x`` as the variable (e.g. ``"x < 300"``),
  which the R shim compiles to a closure, or
* any value MicrobiomeDB's underlying function accepts (a list/vector of
  values, etc.), passed through verbatim.
"""

from __future__ import annotations

import json
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from .._ctx import session_from_ctx
from ..rserve_client import call
from ..schemas import ComputeKind, StartJobArgs
from ..sessions import SessionManager


def _encode_args(args: dict[str, Any]) -> str:
    """JSON-encode an args dict for the R shim.

    The R side calls ``jsonlite::fromJSON(args_json, simplifyVector = FALSE)``
    so we dump as-is. Centralized so the wire contract is obvious.
    """
    return json.dumps(args)


def _kind_tool_factory(mcp: FastMCP, manager: SessionManager,
                       kind: ComputeKind, description: str) -> None:
    """Register a ``start_<kind>`` tool. Each is a thin wrapper around the
    generic ``mcp_startJob`` plumbing so clients get autocompletable, typed
    entrypoints in their MCP UI."""

    tool_name = f"start_{kind}"

    async def _impl(ctx: Context, args: dict[str, Any] | None = None) -> dict:
        sess = await session_from_ctx(manager, ctx)
        result = await call(sess.conn, "mcp_startJob", kind, _encode_args(args or {}))
        # Track the running job so the idle-session reaper won't kill us
        # mid-computation.
        job_id = result.get("job_id") if isinstance(result, dict) else None
        if job_id:
            sess.active_jobs.add(job_id)
        return result

    _impl.__name__ = tool_name
    mcp.tool(name=tool_name, description=description)(_impl)


def register(mcp: FastMCP, manager: SessionManager) -> None:

    _kind_tool_factory(mcp, manager, "alphaDiv",
        "Start an alpha-diversity computation. `args` must include `data` "
        "(a collection handle stub, e.g. {\"$handle\": \"<uuid>\"}) plus any "
        "options accepted by MicrobiomeDB::alphaDiv.")

    _kind_tool_factory(mcp, manager, "betaDiv",
        "Start a beta-diversity computation. `args.data` should be a "
        "collection handle stub; see MicrobiomeDB::betaDiv for options.")

    _kind_tool_factory(mcp, manager, "rankedAbundance",
        "Start a ranked-abundance computation against a collection handle.")

    _kind_tool_factory(mcp, manager, "correlation",
        "Start a correlation computation. Provide `data` and (for "
        "bipartite networks) `data2` as collection handle stubs.")

    _kind_tool_factory(mcp, manager, "selfCorrelation",
        "Start a self-correlation computation against a collection handle.")

    _kind_tool_factory(mcp, manager, "differentialAbundance",
        "Start a differential-abundance computation. `args.data` is a "
        "collection handle stub; `groupA`/`groupB` may be R expression "
        "strings (variable `x`) which the server compiles to closures.")

    # Generic escape hatch so future MicrobiomeDB compute methods can be
    # exercised without a new tool deployment. The R-side whitelist still
    # bounds what kinds are allowed.
    @mcp.tool(description=(
        "Generic compute job launcher. Prefer the typed `start_*` tools; "
        "use this only when the R-side handler whitelist has been extended."
    ))
    async def start_job(args: StartJobArgs, ctx: Context) -> dict:
        sess = await session_from_ctx(manager, ctx)
        result = await call(sess.conn, "mcp_startJob",
                            args.kind, _encode_args(args.args))
        job_id = result.get("job_id") if isinstance(result, dict) else None
        if job_id:
            sess.active_jobs.add(job_id)
        return result
