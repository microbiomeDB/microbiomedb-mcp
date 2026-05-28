"""User-data import tools.

File transfer model
-------------------
MCP itself doesn't have a first-class binary-upload primitive, so we
expose a chunked ``upload_*`` family that writes bytes into the shared
``uploads`` volume. The path returned by ``upload_finish`` is what the
``import_*`` tools accept; the R-side ``.mcp_check_path`` validates the
path lives under that volume.

Chunks are arbitrary byte ranges; clients should split files into ~1 MiB
chunks to stay well within MCP's per-message size limits. The Python side
also enforces the path lives under this session's upload directory before
even calling R, so a misbehaving client gets a clean MCP error.
"""

from __future__ import annotations

import base64
import os
import secrets
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from .._ctx import session_from_ctx
from ..config import Settings
from ..rserve_client import call
from ..sessions import Session, SessionManager

# Safe-character mask for filenames passed by clients. The user value is
# advisory; the actual path is namespaced under a session-specific dir.
_SAFE_FILENAME_CHARS = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
)


def _sanitize_filename(name: str) -> str:
    cleaned = "".join(c if c in _SAFE_FILENAME_CHARS else "_" for c in name)
    return cleaned or "upload.bin"


def _session_upload_dir(settings: Settings, sess: Session) -> Path:
    base = Path(settings.uploads_dir) / sess.session_id
    base.mkdir(parents=True, exist_ok=True)
    return base


def _verify_upload_path(settings: Settings, sess: Session, path: str) -> None:
    base = _session_upload_dir(settings, sess).resolve()
    target = Path(path).resolve()
    try:
        target.relative_to(base)
    except ValueError as e:
        raise ValueError(
            f"path {path} is not inside this session's upload directory"
        ) from e


def register(mcp: FastMCP, manager: SessionManager, settings: Settings) -> None:

    @mcp.tool(description=(
        "Begin a file upload. Returns an upload_id and the server path the "
        "file will land at. Use `upload_chunk` to append base64-encoded "
        "bytes, then `upload_finish` to seal it. The resulting path is "
        "passed verbatim to import_* tools."
    ))
    async def upload_begin(filename: str, ctx: Context) -> dict:
        sess = await session_from_ctx(manager, ctx)
        safe = _sanitize_filename(filename)
        upload_id = secrets.token_urlsafe(12)
        target = _session_upload_dir(settings, sess) / f"{upload_id}-{safe}"
        target.touch()
        return {"upload_id": upload_id, "path": str(target), "filename": safe}

    @mcp.tool(description=(
        "Append a base64-encoded chunk to an in-progress upload. Returns "
        "the new size on disk. Keep chunks <= 1 MiB to stay well under "
        "MCP message size limits."
    ))
    async def upload_chunk(path: str, data_base64: str, ctx: Context) -> dict:
        sess = await session_from_ctx(manager, ctx)
        _verify_upload_path(settings, sess, path)
        raw = base64.b64decode(data_base64, validate=True)
        with open(path, "ab") as fh:
            fh.write(raw)
        return {"path": path, "size": os.path.getsize(path)}

    @mcp.tool(description="Finalize an upload. Returns the final path + size.")
    async def upload_finish(path: str, ctx: Context) -> dict:
        sess = await session_from_ctx(manager, ctx)
        _verify_upload_path(settings, sess, path)
        return {"path": path, "size": os.path.getsize(path)}

    # --- importers ------------------------------------------------------
    # Each importer takes the path returned by upload_finish and any
    # passthrough kwargs the underlying MicrobiomeDB function accepts. The
    # R shim re-validates the path is under the uploads volume.

    def _make_import_tool(name: str, r_fn: str, description: str) -> None:
        async def _impl(path: str, ctx: Context,
                        kwargs: dict[str, Any] | None = None) -> dict:
            sess = await session_from_ctx(manager, ctx)
            # kwargs are forwarded to R as named arguments via pyRserve's
            # kwargs mapping, which our `call` helper supports.
            return await call(sess.conn, r_fn, path, **(kwargs or {}))
        _impl.__name__ = name
        mcp.tool(name=name, description=description)(_impl)

    _make_import_tool("import_biom", "mcp_importBIOM",
        "Import a BIOM file into this session as an MbioDataset handle.")
    _make_import_tool("import_qiime2", "mcp_importQIIME2",
        "Import QIIME2 output files into this session as an MbioDataset handle.")
    _make_import_tool("import_mothur", "mcp_importMothur",
        "Import Mothur output files into this session as an MbioDataset handle.")
    _make_import_tool("import_dada2", "mcp_importDADA2",
        "Import dada2 output files into this session as an MbioDataset handle.")
    _make_import_tool("import_humann", "mcp_importHUMAnN",
        "Import HUMAnN output files into this session as an MbioDataset handle.")
    _make_import_tool("import_metaphlan", "mcp_importMetaPhlAn",
        "Import MetaPhlAn output files into this session as an MbioDataset handle.")
    _make_import_tool("import_phyloseq", "mcp_importPhyloseq",
        "Import a saved phyloseq object (.rds) into this session as an MbioDataset handle.")
    _make_import_tool("import_treese", "mcp_importTreeSE",
        "Import a saved TreeSummarizedExperiment (.rds) into this session as an MbioDataset handle.")
