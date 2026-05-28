# microbiomedb-mcp

A Model Context Protocol (MCP) server that exposes the [MicrobiomeDB R package][mdb]
and its curated data package [microbiomeData][mdata] to MCP-capable clients
(IDE agents, chat assistants, automation scripts).

* **Heavy R packages are loaded once** in the master Rserve process. Each MCP
  client gets a forked R child that **copy-on-write shares** the loaded
  curated datasets, so subsequent calls in the same session never reload.
* **Long-running computations run async** via `parallel::mcparallel` inside
  the session's R child. The job fork also COW-inherits the session's
  user-imported data, so you don't pay for data again to start a job.
* **Per-MCP-connection sessions** mean a client's loaded datasets, result
  handles, and jobs persist across tool calls until disconnect or idle
  timeout.

[mdb]: https://github.com/microbiomeDB/MicrobiomeDB
[mdata]: https://github.com/microbiomeDB/microbiomeData

## Architecture

```
MCP client (Claude / IDE / agent)
        | MCP over HTTPS + bearer auth
        v
   Caddy (TLS, bearer token)
        | http
        v
   microbiomedb-mcp (Python, FastMCP, pyRserve)
        | session_id -> dedicated pyRserve connection (one forked R child)
        v
   rserve (R 4.4)
     master:
       library(MicrobiomeDB) + library(microbiomeData) + shim
     session child (forked on connect, COW-shares loaded packages):
       per-session state: loaded datasets, user imports, result handles
       spawns mcparallel() forks for long-running compute jobs
```

Only the `proxy` service publishes ports to the host. `rserve` is bound to
the internal compose network and is not reachable from outside the stack.

## Quick start

```bash
cp .env.example .env
# Edit .env: set MCP_BEARER_TOKEN to a long random string.

docker compose build      # ~30-60 min for the rserve image on first build
docker compose up -d

# End-to-end check (uses your bearer token):
export MCP_BASE_URL=https://localhost
export MCP_BEARER_TOKEN=$(grep ^MCP_BEARER_TOKEN .env | cut -d= -f2)
python -m pip install -e .
python scripts/smoke.py
```

For local development without TLS, point at `http://localhost:80` and accept
that Caddy is still terminating; or bypass Caddy by porting the `mcp`
service directly (uncomment the relevant lines in `docker-compose.yml`).

## Tools

All tools are scoped to the calling MCP session. Loaded datasets, result
handles, and jobs persist for the lifetime of the session.

### Discovery (sync)

| Tool                              | Purpose |
|-----------------------------------|---------|
| `list_curated_datasets`           | Names of curated `MbioDataset` objects bundled in `microbiomeData`. |
| `load_dataset(name)`              | Realize and register a curated dataset; returns a handle. |
| `get_collection_names(h)`         | List collections on a loaded dataset. |
| `get_collection(h, name, format)` | Register a collection from a dataset; returns a handle. |
| `get_metadata_variable_names(h)`  | List metadata variable names. |
| `get_metadata_variable_summary(h, variables?)` | Per-variable summaries. |
| `get_sample_metadata(h, limit)`   | Paged sample metadata table. |

### Compute (async; always)

Each `start_*` tool returns immediately with a `job_id`. Poll with
`get_job_status`, then fetch with `get_job_result`.

| Tool                          | Wraps |
|-------------------------------|-------|
| `start_alphaDiv`              | `MicrobiomeDB::alphaDiv` |
| `start_betaDiv`               | `MicrobiomeDB::betaDiv` |
| `start_rankedAbundance`       | `MicrobiomeDB::rankedAbundance` |
| `start_correlation`           | `MicrobiomeDB::correlation` |
| `start_selfCorrelation`       | `MicrobiomeDB::selfCorrelation` |
| `start_differentialAbundance` | `MicrobiomeDB::differentialAbundance` |
| `start_job(kind, args)`       | Escape hatch over the R-side whitelist. |

`args.data` (and `args.data2` for bipartite correlation) should be a
**handle stub**:

```json
{ "data": { "$handle": "<collection-uuid>" } }
```

The R shim resolves stubs back to the registered objects in this session.

For `differentialAbundance`, `groupA`/`groupB` may be R expression strings
with the variable `x`, e.g. `"x < 300"`, which the server compiles to
closures. Anything else is passed through verbatim.

### Job control

| Tool             | Purpose |
|------------------|---------|
| `get_job_status` | Cheap poll. Returns terminal status + result handle when done. |
| `get_job_result(job_id, format)` | `summary` (default), `data.table`, or `igraph`. |
| `cancel_job`     | SIGTERM the worker fork. |
| `list_jobs`      | All jobs known to this session, with live status. |
| `free_job`       | Forget a job; best-effort cancels first. |

### Result retrieval

| Tool                                | Purpose |
|-------------------------------------|---------|
| `get_compute_result(h, format)`     | Fetch by handle (alternative to `get_job_result`). |
| `get_compute_result_with_metadata`  | Join compute result with metadata variables. |
| `correlation_network(h)`            | Render an HTML widget; returns a path on the uploads volume. |
| `list_handles`                      | Show all live handles in this session. |
| `free_handle(h)`                    | Release an object so R can GC it. |

### User-data imports

```text
upload_begin(filename) -> upload_chunk(path, base64)* -> upload_finish(path)
                       -> import_<format>(path, kwargs)
```

Supported formats: `import_biom`, `import_qiime2`, `import_mothur`,
`import_dada2`, `import_humann`, `import_metaphlan`, `import_phyloseq`,
`import_treese`.

### Session

| Tool             | Purpose |
|------------------|---------|
| `session_info`   | PID, handle/job counts, memory hint, active jobs. |
| `close_session`  | Tear down this session's R worker. |

## Configuration

All knobs are environment variables. See `.env.example` for the full list.
The defaults are reasonable for a single-node deployment with a handful of
clients.

| Variable | Default | Meaning |
|---|---|---|
| `MCP_BEARER_TOKEN`         | (required) | Token clients must present. |
| `MCP_PUBLIC_HOST`          | `localhost` | Hostname Caddy serves. |
| `MCP_INTERNAL_PORT`        | `8080`    | Port the mcp service listens on. |
| `MCP_SESSION_IDLE_TIMEOUT` | `3600`    | Idle seconds before reaping a session (active jobs override). |
| `MCP_MAX_SESSIONS`         | `16`      | Cap on concurrent Rserve children. |
| `MCP_MAX_JOBS_PER_SESSION` | `2`       | Cap on concurrent `mcparallel` jobs per session. |
| `MCP_MAX_JOBS_GLOBAL`      | `8`       | Cap across all sessions. |

## Security notes

* `rserve`'s 6311 port is **never** published to the host. The only path in
  is via Caddy -> `mcp`, which is bearer-authed.
* Bearer auth is intentionally a single shared static token. For multi-user
  or revocable tokens, swap the Caddyfile `@authorized` matcher for
  `forward_auth` against an OIDC sidecar.
* Path traversal in `upload_*` is blocked both in Python (`_verify_upload_path`)
  and in R (`.mcp_check_path`).
* Compute is async-only, so a runaway computation cannot block the HTTP
  request thread or starve other clients on the same `mcp` service.
* Sessions with active jobs are never reaped; a client that disconnects
  mid-job can still reconnect (with the same MCP session id) and retrieve
  the result.

## Development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'

# Pure-python unit tests (no docker required):
pytest

# R unit tests (run inside the rserve container, or any R install with
# MicrobiomeDB + microbiomeData attached):
Rscript -e "testthat::test_dir('tests/testthat')"

# Full end-to-end:
docker compose up -d
python scripts/smoke.py
```

## Limitations / known caveats

* The `rserve` image is large (Bioconductor stack + curated data). First
  build can take 30-60 minutes.
* Sessions and jobs are **in-memory only**. A container restart loses
  user-imported data and running jobs. (This was an intentional choice;
  see the planning doc.)
* The R-side compute whitelist is explicit. Adding a new MicrobiomeDB
  compute function means adding it to `.mcp_compute_fns` in
  `docker/rserve/R/mcp_jobs.R` *and* a thin tool wrapper in
  `src/microbiomedb_mcp/tools/compute.py`.
* `microbiomeData`'s curated-dataset list is exposed lazily via R's
  LazyData mechanism. If you update `microbiomeData`, rebuild the rserve
  image so the new datasets show up.
