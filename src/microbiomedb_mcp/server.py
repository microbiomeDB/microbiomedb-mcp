"""FastMCP entrypoint.

Wires together configuration, the session manager, and the tool modules.
Exposes a streamable-HTTP transport (so multiple clients can connect via
Caddy concurrently) plus a tiny ``/healthz`` route that round-trips a
ping through to Rserve.

Each tool resolves the active Session by inspecting the FastMCP
``Context`` it's handed; session affinity therefore matches the MCP
connection's session-id, which is exactly what we want (one R child per
MCP client).
"""

from __future__ import annotations

import contextlib
import logging

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from .config import Settings, configure_logging
from .rserve_client import ping, probe_capabilities
from .sessions import SessionManager
from .tools import compute, discovery, imports, jobs, results, session as session_tools

log = logging.getLogger("microbiomedb_mcp")


def build_app(settings: Settings,
              capabilities: dict | None = None) -> tuple[FastMCP, SessionManager]:
    """Construct the FastMCP app and SessionManager.

    Split out from :func:`main` so tests can build the app without
    starting uvicorn. ``capabilities`` is the dict returned by
    :func:`rserve_client.probe_capabilities`; it's used to gate tool
    registration so we don't advertise curated-dataset tools when the
    ``microbiomeData`` package isn't installed in the worker. When
    omitted (e.g. in tests), all tools are registered.
    """
    caps = capabilities or {}
    has_curated_data = bool(caps.get("has_curated_data", True))

    mcp = FastMCP(
        name="microbiomedb-mcp",
        instructions=(
            "Tools to explore "
            + ("curated MicrobiomeDB datasets and " if has_curated_data else "")
            + "user-imported microbiome data and run analyses via a backing "
            "R/Rserve worker. Long-running compute is async: use start_* "
            "tools to kick off, then get_job_status / get_job_result. State "
            "(loaded datasets, result handles, jobs) persists per MCP "
            "session - call close_session to release it."
        ),
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=settings.enable_dns_rebinding_protection,
            allowed_hosts=settings.allowed_hosts,
            allowed_origins=settings.allowed_origins,
        ),
    )
    manager = SessionManager(settings)

    discovery.register(mcp, manager, has_curated_data=has_curated_data)
    compute.register(mcp, manager)
    jobs.register(mcp, manager)
    results.register(mcp, manager)
    imports.register(mcp, manager, settings)
    session_tools.register(mcp, manager)

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(request):  # type: ignore[no-untyped-def]
        # End-to-end check: HTTP -> mcp service -> Rserve.
        # Uses a dedicated `_healthz` session id so it doesn't perturb
        # real clients' session state.
        from starlette.responses import JSONResponse

        try:
            sess = await manager.get_or_create("_healthz")
            info = await ping(sess.conn)
            return JSONResponse({"ok": True, "r": info})
        except Exception as e:
            log.exception("healthz failed")
            return JSONResponse({"ok": False, "error": str(e)}, status_code=503)

    return mcp, manager


def main() -> None:
    settings = Settings.from_env()
    configure_logging(settings.log_level)
    log.info(
        "starting microbiomedb-mcp on :%d -> Rserve %s:%d",
        settings.internal_port, settings.rserve_host, settings.rserve_port,
    )

    # Probe Rserve once at startup so we know which tools to advertise.
    # Failure here is fatal: without Rserve we can't serve any tools, and
    # crashing fast lets docker-compose/k8s restart us cleanly.
    capabilities = probe_capabilities(settings.rserve_host, settings.rserve_port)
    log.info("rserve capabilities: %s", capabilities)

    mcp, manager = build_app(settings, capabilities=capabilities)

    # FastMCP exposes a Starlette app for streamable-HTTP transport. It
    # already attaches its OWN lifespan, which initializes the streamable
    # session manager's anyio task group; clobbering that breaks every
    # request with "Task group is not initialized". So we compose: run the
    # original lifespan, and inside it run ours (the SessionManager's
    # idle-reaper).
    app = mcp.streamable_http_app()
    inner_lifespan = app.router.lifespan_context  # type: ignore[attr-defined]

    @contextlib.asynccontextmanager
    async def lifespan(app):  # type: ignore[no-untyped-def]
        async with inner_lifespan(app):
            await manager.start()
            try:
                yield
            finally:
                await manager.stop()

    app.router.lifespan_context = lifespan  # type: ignore[attr-defined]

    import uvicorn
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=settings.internal_port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
