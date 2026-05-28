"""Unit tests for SessionManager that don't require Rserve.

We monkey-patch `open_connection` so the manager thinks it has a real
pyRserve connection. The goal is to verify the cap, reaper, and
active-jobs guard semantics, not the Rserve plumbing itself.
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from unittest.mock import MagicMock

import pytest

from microbiomedb_mcp.config import Settings
from microbiomedb_mcp import sessions as sessions_mod
from microbiomedb_mcp.sessions import SessionLimitExceeded, SessionManager


@dataclass
class _FakeRConn:
    closed: bool = False
    def close(self):
        self.closed = True


@pytest.fixture
def settings():
    return Settings(
        rserve_host="rserve",
        rserve_port=6311,
        bearer_token=None,
        internal_port=8080,
        session_idle_timeout=0.2,
        max_sessions=2,
        max_jobs_per_session=2,
        max_jobs_global=4,
        uploads_dir="/tmp/uploads",
        log_level="WARNING",
    )


@pytest.fixture
def patched_open(monkeypatch):
    def _fake(host, port):
        from microbiomedb_mcp.rserve_client import RConnection
        return RConnection(conn=_FakeRConn(), lock=threading.Lock())

    monkeypatch.setattr(sessions_mod, "open_connection", _fake)


@pytest.fixture
def patched_call(monkeypatch):
    async def _fake_call(*args, **kwargs):
        return {"max_jobs": 2}
    monkeypatch.setattr(sessions_mod, "call", _fake_call)


async def test_get_or_create_reuses_session(settings, patched_open, patched_call):
    mgr = SessionManager(settings)
    s1 = await mgr.get_or_create("alice")
    s2 = await mgr.get_or_create("alice")
    assert s1 is s2
    await mgr.stop()


async def test_session_cap_enforced(settings, patched_open, patched_call):
    mgr = SessionManager(settings)
    await mgr.get_or_create("a")
    await mgr.get_or_create("b")
    with pytest.raises(SessionLimitExceeded):
        await mgr.get_or_create("c")
    await mgr.stop()


async def test_reaper_skips_sessions_with_active_jobs(settings, patched_open, patched_call):
    mgr = SessionManager(settings)
    sess = await mgr.get_or_create("a")
    sess.active_jobs.add("job-1")
    # Force expiry: rewind last_access well past the idle timeout.
    sess.last_access -= settings.session_idle_timeout * 10
    await mgr._sweep_once()
    assert await mgr.session_count() == 1
    await mgr.stop()


async def test_reaper_collects_idle_sessions(settings, patched_open, patched_call):
    mgr = SessionManager(settings)
    sess = await mgr.get_or_create("a")
    sess.last_access -= settings.session_idle_timeout * 10
    await mgr._sweep_once()
    assert await mgr.session_count() == 0
    await mgr.stop()
