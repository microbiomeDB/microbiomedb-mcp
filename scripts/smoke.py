"""End-to-end smoke test.

Brings up the compose stack out-of-band, then exercises the full happy
path against the running MCP server over HTTP:

  1. list_curated_datasets
  2. load_dataset DiabImmune
  3. get_collection '16S Species'
  4. start_alphaDiv on that collection
  5. poll get_job_status until terminal
  6. get_job_result (summary, then data.table)
  7. close_session

Run:

  export MCP_BASE_URL=https://localhost
  export MCP_BEARER_TOKEN=...
  python scripts/smoke.py

Uses the official `mcp` Python SDK as a client so the wire protocol
exactly matches what a real agent would send.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


def _payload(result: Any) -> Any:
    """Return the python payload of a tool call regardless of how the SDK
    surfaced it.

    FastMCP only auto-fills ``structuredContent`` for return types whose
    schema it can infer (``list[str]``, Pydantic models, etc). For tools
    that return a plain ``dict``, the payload is JSON-serialized into
    ``content[0].text`` and ``structuredContent`` is None. This helper
    smooths over that asymmetry so the smoke test doesn't have to.
    """
    if result.isError:
        msg = result.content[0].text if result.content else "<no content>"
        raise RuntimeError(f"tool error: {msg}")
    if result.structuredContent is not None:
        sc = result.structuredContent
        # FastMCP wraps non-dict return types under a "result" key; unwrap.
        if isinstance(sc, dict) and set(sc.keys()) == {"result"}:
            return sc["result"]
        return sc
    if result.content:
        text = result.content[0].text
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text
    return None


def _env_required(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        sys.exit(f"missing required env var: {name}")
    return v


async def _run() -> None:
    base = os.environ.get("MCP_BASE_URL", "http://localhost:8080")
    token = _env_required("MCP_BEARER_TOKEN")
    headers = {"Authorization": f"Bearer {token}"}
    mcp_url = base.rstrip("/") + "/mcp"

    print(f"-> connecting to {mcp_url}")
    async with streamablehttp_client(mcp_url, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()

            # Gate the curated-data path on the server actually advertising
            # those tools. When the rserve image was built without
            # microbiomeData (INSTALL_MICROBIOMEDATA=false), they're absent
            # and we exit cleanly so this smoke test still passes in
            # user-import-only deployments.
            tool_names = {t.name for t in (await session.list_tools()).tools}
            if "list_curated_datasets" not in tool_names:
                print("   [skip] curated-data tools not exposed by this server "
                      "(microbiomeData not installed). Server is reachable; "
                      "smoke test stopping here.")
                print("OK")
                return

            print("-> list_curated_datasets")
            names = _payload(await session.call_tool("list_curated_datasets"))
            print(f"   {len(names)} datasets available")

            print("-> load_dataset DiabImmune")
            loaded = _payload(await session.call_tool("load_dataset",
                {"name": "DiabImmune"}))
            dataset_handle = loaded["handle"]
            print(f"   handle={dataset_handle}")

            print("-> get_collection '16S (V4) Genus (Relative taxonomic abundance analysis)'")
            coll = _payload(await session.call_tool("get_collection", {
                "dataset_handle": dataset_handle,
                "name": "16S (V4) Genus (Relative taxonomic abundance analysis)",
            }))
            coll_handle = coll["handle"]
            print(f"   handle={coll_handle}")

            print("-> start_alphaDiv")
            start = _payload(await session.call_tool("start_alphaDiv",
                {"args": {"data": {"$handle": coll_handle}}}))
            job_id = start["job_id"]
            print(f"   job_id={job_id}")

            deadline = time.monotonic() + 600  # generous; first run is slow
            while True:
                status = _payload(await session.call_tool("get_job_status",
                    {"job_id": job_id}))
                print(f"   status={status['status']}")
                if status["status"] in {"succeeded", "failed", "cancelled"}:
                    break
                if time.monotonic() > deadline:
                    sys.exit("job did not finish within 10 minutes")
                await asyncio.sleep(2.0)

            if status["status"] != "succeeded":
                sys.exit(f"job did not succeed: {status}")

            print("-> get_job_result (summary)")
            summary = _payload(await session.call_tool("get_job_result",
                {"job_id": job_id, "format": "summary"}))
            print(f"   {summary}")

            print("-> get_job_result (data.table, limit=10)")
            dt = _payload(await session.call_tool("get_job_result",
                {"job_id": job_id, "format": "data.table", "limit": 10}))
            print(f"   nrow={dt.get('__nrow__')} truncated={dt.get('__truncated__')}")

            print("-> close_session")
            await session.call_tool("close_session", {})

    print("OK")


if __name__ == "__main__":
    asyncio.run(_run())
