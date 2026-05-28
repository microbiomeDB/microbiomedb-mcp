"""Runtime configuration, loaded from environment variables.

Kept tiny on purpose: this is a deployment artifact, not a library, so we
read env once at startup rather than threading a config object everywhere.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    rserve_host: str
    rserve_port: int
    bearer_token: str | None
    internal_port: int
    session_idle_timeout: float
    max_sessions: int
    max_jobs_per_session: int
    max_jobs_global: int
    uploads_dir: str
    log_level: str
    # MCP 1.27+ ships DNS-rebinding protection that rejects any request
    # whose Host header isn't in an allowlist (returns 421). Default
    # allowlist is empty -> every request fails. We sit behind Caddy
    # (which validates Host) + a bearer token + an internal-only
    # compose network, so the extra layer is redundant for us. Off by
    # default; set MCP_ENABLE_DNS_REBINDING_PROTECTION=true and provide
    # MCP_ALLOWED_HOSTS / MCP_ALLOWED_ORIGINS to re-enable.
    enable_dns_rebinding_protection: bool
    allowed_hosts: list[str]
    allowed_origins: list[str]

    @classmethod
    def from_env(cls) -> "Settings":
        def _csv(name: str) -> list[str]:
            raw = os.environ.get(name, "")
            return [s.strip() for s in raw.split(",") if s.strip()]

        return cls(
            rserve_host=os.environ.get("RSERVE_HOST", "rserve"),
            rserve_port=int(os.environ.get("RSERVE_PORT", "6311")),
            bearer_token=os.environ.get("MCP_BEARER_TOKEN") or None,
            internal_port=int(os.environ.get("MCP_INTERNAL_PORT", "8080")),
            session_idle_timeout=float(os.environ.get("MCP_SESSION_IDLE_TIMEOUT", "3600")),
            max_sessions=int(os.environ.get("MCP_MAX_SESSIONS", "16")),
            max_jobs_per_session=int(os.environ.get("MCP_MAX_JOBS_PER_SESSION", "2")),
            max_jobs_global=int(os.environ.get("MCP_MAX_JOBS_GLOBAL", "8")),
            uploads_dir=os.environ.get("UPLOADS_DIR", "/srv/uploads"),
            log_level=os.environ.get("MCP_LOG_LEVEL", "INFO"),
            enable_dns_rebinding_protection=(
                os.environ.get("MCP_ENABLE_DNS_REBINDING_PROTECTION", "false").lower()
                in {"1", "true", "yes"}
            ),
            allowed_hosts=_csv("MCP_ALLOWED_HOSTS"),
            allowed_origins=_csv("MCP_ALLOWED_ORIGINS"),
        )


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
