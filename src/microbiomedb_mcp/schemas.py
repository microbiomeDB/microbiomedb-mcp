"""Pydantic schemas shared by tool modules.

Kept intentionally lenient: the R side does its own argument validation
via the underlying MicrobiomeDB functions, so we only enforce what's
necessary to construct the call safely (e.g. handle stubs are well-formed,
file paths are strings).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class HandleRef(BaseModel):
    """JSON shape for referring to a server-side object by handle."""
    model_config = ConfigDict(populate_by_name=True)
    handle: str = Field(..., alias="$handle")


def handle_stub(h: str) -> dict[str, str]:
    """Build the ``{"$handle": h}`` stub that the R shim resolves to a
    registered object. Used by compute tools so callers can pass dataset /
    collection handles inside a generic ``args`` dict."""
    return {"$handle": h}


# --- Compute argument schema -------------------------------------------------
#
# We accept a loose dict for compute args because MicrobiomeDB's compute
# functions have wide and partially-overlapping signatures. The Python
# layer does light validation; the R layer is authoritative.

ComputeKind = Literal[
    "alphaDiv",
    "betaDiv",
    "rankedAbundance",
    "correlation",
    "selfCorrelation",
    "differentialAbundance",
]


class StartJobArgs(BaseModel):
    kind: ComputeKind
    args: dict[str, Any] = Field(default_factory=dict)


JobStatus = Literal["queued", "running", "succeeded", "failed", "cancelled"]


class JobInfo(BaseModel):
    job_id: str
    kind: str | None = None
    status: JobStatus
    started_at: str | None = None
    finished_at: str | None = None
    result_handle: str | None = None
    error: str | None = None
