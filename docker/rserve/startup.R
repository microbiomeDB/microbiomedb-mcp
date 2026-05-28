# Master Rserve bootstrap.
#
# Sourced by the standalone Rserve binary (`R CMD Rserve --RS-conf
# /etc/Rserv.conf`) via the `source` directive in Rserv.conf. Runs ONCE
# in the master process before Rserve enters its accept loop. Children
# are forked on each TCP connect and COW-inherit everything attached or
# sourced here, so per-session reload cost is zero.
#
# Memory implications: anything loaded eagerly here is paid for once per
# container. Curated `microbiomeData` datasets rely on R's LazyData and are
# NOT realized until first accessed in a session, so library() alone is cheap.

suppressPackageStartupMessages({
  library(jsonlite)
  library(uuid)
  library(data.table)
  library(parallel)
  library(MicrobiomeDB)
})

# microbiomeData is OPTIONAL. It's a large data package and some deployments
# may want to ship without it (user-import-only mode). We probe for it once
# here in the master process; the shim and the Python layer key off this
# flag to gate curated-dataset tools.
.mcp_has_curated_data <- requireNamespace("microbiomeData", quietly = TRUE)
if (.mcp_has_curated_data) {
  suppressPackageStartupMessages(library(microbiomeData))
}

# Shim handlers live alongside this file.
source("/opt/microbiomedb-mcp/R/mcp_handlers.R", local = FALSE)
source("/opt/microbiomedb-mcp/R/mcp_jobs.R", local = FALSE)
source("/opt/microbiomedb-mcp/R/mcp_imports.R", local = FALSE)

# Sanity log to container stdout. Useful when diagnosing slow starts.
cat(sprintf(
  "[microbiomedb-mcp] master ready: R %s, MicrobiomeDB %s, microbiomeData %s\n",
  getRversion(),
  utils::packageVersion("MicrobiomeDB"),
  if (.mcp_has_curated_data) as.character(utils::packageVersion("microbiomeData")) else "(not installed)"
))

# NOTE: do NOT call Rserve::run.Rserve() here. The standalone Rserve
# binary that sources this file (via `source` in Rserv.conf) hands the
# master process to its own accept loop after this script returns.
