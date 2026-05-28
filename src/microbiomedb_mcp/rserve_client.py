"""Thin async-friendly wrapper around pyRserve.

Two responsibilities:

1. Translate Python call sites into Rserve invocations of the ``mcp_*``
   helper functions defined in ``docker/rserve/R``. The R side wraps every
   handler in ``.mcp_ok()`` and returns a ``{ok, value|error}`` envelope;
   we unwrap that envelope here and raise :class:`RserveCallError` on
   ``ok == FALSE`` so MCP tools can surface the message cleanly.

2. Convert Python arguments to JSON before crossing the wire. We
   deliberately pass complex arguments as a single JSON string (parsed by
   ``jsonlite::fromJSON`` on the R side) rather than relying on pyRserve's
   ad-hoc S-expression mapping, which struggles with nested dicts and
   handle stubs of the form ``{"$handle": "<uuid>"}``.

pyRserve itself is synchronous. We run blocking calls in a worker thread
via ``anyio.to_thread.run_sync`` so an MCP request handler doesn't block
the event loop while R is doing real work. For long-running compute we
don't even need this; ``start_job`` returns in milliseconds. But ``import*``
and ``getComputeResult`` for big tables can take seconds, hence the thread
offload.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any

import anyio
import pyRserve

log = logging.getLogger(__name__)


class RserveCallError(RuntimeError):
    """The R-side handler returned ``ok = FALSE``."""

    def __init__(self, fn: str, message: str, r_class: str | None = None):
        super().__init__(f"R {fn}: {message}")
        self.fn = fn
        self.message = message
        self.r_class = r_class


@dataclass
class RConnection:
    """A single pyRserve connection bound to one forked R child.

    pyRserve connections are NOT thread-safe; we serialize access with a
    per-connection lock so the connection can be shared between the MCP
    tool handlers that touch the same session (e.g. one handler polling a
    job status while another is mid-call).
    """

    conn: pyRserve.RConnector
    lock: threading.Lock

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:  # pragma: no cover - best effort on shutdown
            log.exception("error closing Rserve connection")


def open_connection(host: str, port: int) -> RConnection:
    log.debug("opening Rserve connection to %s:%d", host, port)
    conn = pyRserve.connect(host=host, port=port)
    return RConnection(conn=conn, lock=threading.Lock())


def _unwrap(fn_name: str, envelope: Any) -> Any:
    """Validate and unwrap the ``.mcp_ok`` envelope from the R side."""
    # pyRserve maps a named R list to a TaggedList; coerce to a plain dict.
    if hasattr(envelope, "astuples"):
        env = dict(envelope.astuples())
    elif isinstance(envelope, dict):
        env = envelope
    else:
        raise RserveCallError(
            fn_name, f"unexpected R return type: {type(envelope).__name__}"
        )

    ok = env.get("ok")
    # pyRserve sometimes returns a length-1 array for scalars.
    if hasattr(ok, "__len__") and not isinstance(ok, (str, bytes)):
        ok = ok[0] if len(ok) else False

    if not ok:
        msg = env.get("error", "unknown R error")
        if hasattr(msg, "__len__") and not isinstance(msg, (str, bytes)):
            msg = msg[0] if len(msg) else "unknown R error"
        r_class = env.get("class")
        if hasattr(r_class, "__len__") and not isinstance(r_class, (str, bytes)):
            r_class = r_class[0] if len(r_class) else None
        raise RserveCallError(fn_name, str(msg), r_class=str(r_class) if r_class else None)

    return env.get("value")


def _to_py(value: Any) -> Any:
    """Recursively convert pyRserve return values to plain Python types.

    Named R lists become dicts; un-named (or partially-named) R lists
    become Python lists, since otherwise we'd collapse all None-keyed
    entries onto one another. R-side handlers that need dict semantics
    are responsible for ensuring every element is named.
    """
    if hasattr(value, "astuples"):
        items = list(value.astuples())
        if items and all(k is not None for k, _ in items):
            return {k: _to_py(v) for k, v in items}
        return [_to_py(v) for _, v in items]
    if isinstance(value, dict):
        return {k: _to_py(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_py(v) for v in value]
    # numpy arrays and pyRserve's own array types quack like sequences.
    # tolist() handles them. Fall through for scalars.
    if hasattr(value, "tolist"):
        try:
            return _to_py(value.tolist())
        except Exception:
            pass
    return value


def _call_sync(rc: RConnection, fn: str, args: tuple, kwargs: dict) -> Any:
    """Synchronous call to ``fn(*args, **kwargs)`` on the R side, returning
    the unwrapped + python-ified value. ``kwargs`` are mapped to R named
    arguments and used to forward callers' ``...`` style options (e.g.
    importer extras).
    """
    with rc.lock:
        r_fn = getattr(rc.conn.r, fn)
        envelope = r_fn(*args, **kwargs)
    value = _unwrap(fn, envelope)
    return _to_py(value)


async def call(rc: RConnection, fn: str, *args: Any, **kwargs: Any) -> Any:
    """Async wrapper that offloads the blocking pyRserve call to a worker
    thread. Use this from MCP tool handlers."""
    return await anyio.to_thread.run_sync(_call_sync, rc, fn, args, kwargs)


async def ping(rc: RConnection) -> dict:
    """Cheap healthcheck of the R worker."""
    return await call(rc, "mcp_ping")


def probe_capabilities(host: str, port: int) -> dict:
    """Synchronously probe the Rserve worker for installed-package
    capabilities.

    Used at server startup (before the asyncio loop is running) to decide
    which tools to register. We use a short-lived dedicated connection so
    the probe doesn't pollute any session bookkeeping.
    """
    rc = open_connection(host, port)
    try:
        return _to_py(_unwrap("mcp_capabilities",
                              rc.conn.r.mcp_capabilities()))
    finally:
        rc.close()
