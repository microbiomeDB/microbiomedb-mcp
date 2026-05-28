"""Async job control tools (status / result / cancel / list / free)."""

from __future__ import annotations

from mcp.server.fastmcp import Context, FastMCP

from .._ctx import session_from_ctx
from ..rserve_client import call
from ..sessions import Session, SessionManager

TERMINAL = frozenset({"succeeded", "failed", "cancelled"})


def _untrack_if_terminal(sess: Session, info: dict | None) -> None:
    if not isinstance(info, dict):
        return
    if info.get("status") in TERMINAL and (jid := info.get("job_id")):
        sess.active_jobs.discard(jid)


def register(mcp: FastMCP, manager: SessionManager) -> None:

    @mcp.tool(description=(
        "Poll an async compute job. Cheap; safe to call frequently. "
        "Returns the job's current status and (if succeeded) the handle "
        "of the registered ComputeResult."
    ))
    async def get_job_status(job_id: str, ctx: Context) -> dict:
        sess = await session_from_ctx(manager, ctx)
        info = await call(sess.conn, "mcp_jobStatus", job_id)
        _untrack_if_terminal(sess, info if isinstance(info, dict) else None)
        return info

    @mcp.tool(description=(
        "Fetch the result of a finished job. `format` may be 'summary' "
        "(handle + small summary), 'data.table' (full result table), or "
        "'igraph' (graph view, for correlation results). Errors if the "
        "job has not yet succeeded."
    ))
    async def get_job_result(job_id: str, ctx: Context,
                             format: str = "summary",
                             limit: int = 100000) -> dict | list:
        sess = await session_from_ctx(manager, ctx)
        result = await call(sess.conn, "mcp_jobResult", job_id, format, limit)
        # mcp_jobResult auto-polls, so by the time we get here the job is
        # terminal; ensure we untrack it from the session.
        sess.active_jobs.discard(job_id)
        return result

    @mcp.tool(description="Cancel a running job (SIGTERM the worker fork).")
    async def cancel_job(job_id: str, ctx: Context) -> dict:
        sess = await session_from_ctx(manager, ctx)
        info = await call(sess.conn, "mcp_cancelJob", job_id)
        sess.active_jobs.discard(job_id)
        return info

    @mcp.tool(description="List all jobs known to this session, polling each for current state.")
    async def list_jobs(ctx: Context) -> list[dict]:
        sess = await session_from_ctx(manager, ctx)
        jobs = await call(sess.conn, "mcp_listJobs")
        # Resync active_jobs with reality; cheap, corrects drift if a job
        # finished between two status polls.
        sess.active_jobs = {
            j["job_id"] for j in (jobs or [])
            if isinstance(j, dict) and j.get("status") not in TERMINAL
        }
        return jobs

    @mcp.tool(description=(
        "Forget a job from the session registry. Best-effort cancels it if "
        "still running. Does NOT free the registered result handle - use "
        "`free_handle` for that."
    ))
    async def free_job(job_id: str, ctx: Context) -> bool:
        sess = await session_from_ctx(manager, ctx)
        ok = await call(sess.conn, "mcp_freeJob", job_id)
        sess.active_jobs.discard(job_id)
        return bool(ok)
