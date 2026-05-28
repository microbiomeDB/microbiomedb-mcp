"""Session manager: maps an MCP client connection to a dedicated Rserve
connection (= one forked R child).

Why per-MCP-connection sessions?
--------------------------------
Rserve forks a fresh R process per TCP connection. Each fork COW-inherits
the master's preloaded packages (MicrobiomeDB, microbiomeData). Keeping one
Rserve connection alive for the duration of an MCP client's session means:

* Curated and user-imported data stay in RAM between tool calls (no reload).
* Result handles and job handles live in the same R child, so a client can
  start a job and later fetch its result via the same plumbing.

Lifecycle
---------
Sessions are lazy: the first tool call from an MCP client creates the
Rserve connection. They are torn down on either:

* explicit ``close_session`` tool call,
* MCP client disconnect (FastMCP lifespan/teardown hooks),
* idle-timeout reaper (sessions with active jobs are skipped).

The global cap on concurrent sessions translates 1:1 to a cap on concurrent
Rserve children, which is the real OS-level resource we need to bound.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from .config import Settings
from .rserve_client import RConnection, call, open_connection

log = logging.getLogger(__name__)


@dataclass
class Session:
    session_id: str
    conn: RConnection
    created_at: float
    last_access: float = field(default_factory=time.monotonic)
    active_jobs: set[str] = field(default_factory=set)

    def touch(self) -> None:
        self.last_access = time.monotonic()


class SessionLimitExceeded(RuntimeError):
    """Raised when ``max_sessions`` is already in use."""


class SessionManager:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._sessions: dict[str, Session] = {}
        self._lock = asyncio.Lock()
        self._reaper_task: asyncio.Task[None] | None = None

    # --- public API ------------------------------------------------------

    async def start(self) -> None:
        if self._reaper_task is None:
            self._reaper_task = asyncio.create_task(self._reaper_loop())

    async def stop(self) -> None:
        if self._reaper_task is not None:
            self._reaper_task.cancel()
            try:
                await self._reaper_task
            except asyncio.CancelledError:
                pass
            self._reaper_task = None
        async with self._lock:
            for sess in list(self._sessions.values()):
                sess.conn.close()
            self._sessions.clear()

    async def get_or_create(self, session_id: str) -> Session:
        async with self._lock:
            sess = self._sessions.get(session_id)
            if sess is not None:
                sess.touch()
                return sess
            if len(self._sessions) >= self.settings.max_sessions:
                raise SessionLimitExceeded(
                    f"max sessions ({self.settings.max_sessions}) reached"
                )
            log.info("creating R session for %s", session_id)
            conn = open_connection(self.settings.rserve_host, self.settings.rserve_port)
            sess = Session(
                session_id=session_id,
                conn=conn,
                created_at=time.monotonic(),
            )
            # Apply per-session job cap on the R side so the cap survives
            # even if the Python layer is bypassed.
            try:
                await call(conn, "mcp_setMaxJobs",
                           self.settings.max_jobs_per_session)
            except Exception:
                # Don't fail session creation on a cosmetic cap-setting
                # error; the R-side default still bounds us.
                log.debug("could not set .mcp_max_jobs over Rserve", exc_info=True)
            self._sessions[session_id] = sess
            return sess

    async def close(self, session_id: str) -> bool:
        async with self._lock:
            sess = self._sessions.pop(session_id, None)
        if sess is None:
            return False
        log.info("closing R session for %s", session_id)
        sess.conn.close()
        return True

    async def session_count(self) -> int:
        async with self._lock:
            return len(self._sessions)

    # --- internals -------------------------------------------------------

    async def _reaper_loop(self) -> None:
        # Sweep at 1/10th of the idle timeout (min 30s, max 5min). Cheap.
        interval = max(30.0, min(self.settings.session_idle_timeout / 10.0, 300.0))
        log.debug("session reaper interval=%.1fs", interval)
        while True:
            try:
                await asyncio.sleep(interval)
                await self._sweep_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - reaper must keep running
                log.exception("session reaper iteration failed")

    async def _sweep_once(self) -> None:
        now = time.monotonic()
        timeout = self.settings.session_idle_timeout
        to_close: list[Session] = []
        async with self._lock:
            for sid, sess in list(self._sessions.items()):
                # Never reap a session with active jobs; we'd lose results
                # the client may not have retrieved yet.
                if sess.active_jobs:
                    continue
                if (now - sess.last_access) > timeout:
                    to_close.append(self._sessions.pop(sid))
        for sess in to_close:
            log.info("reaping idle session %s", sess.session_id)
            sess.conn.close()
