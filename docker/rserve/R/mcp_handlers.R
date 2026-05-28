# mcp_handlers.R
#
# Per-session state and JSON-friendly wrappers around MicrobiomeDB functions.
# Lives in each forked Rserve child; ".mcp_env" and ".mcp_jobs" are
# session-local because forks get a private copy on first write.
#
# Design notes
# ------------
# * We deliberately keep heavy S4 objects (MbioDataset, Collection,
#   ComputeResult, igraph) on the R side, addressed by UUID handles. The MCP
#   wire format only carries small metadata blobs; clients fetch full result
#   payloads on demand via mcp_getComputeResult / mcp_getJobResult.
# * Every public mcp_* function is wrapped by .mcp_ok() so pyRserve never
#   sees raw R errors; instead the Python client gets a structured
#   {ok: FALSE, error: "..."} object it can translate into an MCP error.
# * The handle registry and the job registry are intentionally separate
#   environments so we can sweep them independently.

# --- Session registries ---------------------------------------------------

if (!exists(".mcp_env", inherits = FALSE)) {
  .mcp_env  <- new.env(parent = emptyenv())   # handle -> list(kind=, obj=, last_access=)
}
if (!exists(".mcp_jobs", inherits = FALSE)) {
  .mcp_jobs <- new.env(parent = emptyenv())   # job_id -> list(status=, parallel=, ...)
}

# Session-wide cap on concurrent jobs. The Python side ALSO enforces a
# global cap. Keeping a local cap here protects against a single misbehaving
# client even if the Python layer is bypassed.
if (!exists(".mcp_max_jobs", inherits = FALSE)) {
  .mcp_max_jobs <- 2L
}

# --- Internal helpers -----------------------------------------------------

.mcp_ok <- function(expr) {
  # Wrap an expression so its return value is always a JSON-safe envelope.
  # Errors are caught and surfaced as {ok = FALSE, error = "..."}.
  tryCatch(
    {
      value <- expr
      list(ok = TRUE, value = value)
    },
    error = function(e) {
      list(ok = FALSE, error = conditionMessage(e),
           class = class(e)[1])
    }
  )
}

.mcp_new_handle <- function() {
  uuid::UUIDgenerate(use.time = TRUE)
}

.mcp_register <- function(obj, kind) {
  h <- .mcp_new_handle()
  .mcp_env[[h]] <- list(kind = kind, obj = obj,
                        last_access = Sys.time())
  h
}

.mcp_touch <- function(handle) {
  if (!is.null(.mcp_env[[handle]])) {
    .mcp_env[[handle]]$last_access <- Sys.time()
  }
  invisible(NULL)
}

.mcp_require <- function(handle, kind = NULL) {
  entry <- .mcp_env[[handle]]
  if (is.null(entry)) {
    stop(sprintf("unknown handle: %s", handle), call. = FALSE)
  }
  if (!is.null(kind) && !(entry$kind %in% kind)) {
    stop(sprintf("handle %s is %s, expected %s",
                 handle, entry$kind, paste(kind, collapse = "|")),
         call. = FALSE)
  }
  .mcp_touch(handle)
  entry$obj
}

.mcp_summary <- function(obj) {
  # Small, JSON-friendly summary of any object we hand out a handle for.
  # Always cheap; intended for response payloads.
  if (inherits(obj, "MbioDataset")) {
    list(
      type = "MbioDataset",
      collections = tryCatch(getCollectionNames(obj), error = function(e) character()),
      metadataVariables = tryCatch(getMetadataVariableNames(obj), error = function(e) character())
    )
  } else if (inherits(obj, "Collection")) {
    list(type = "Collection",
         name = tryCatch(obj@name, error = function(e) NA_character_))
  } else if (inherits(obj, "ComputeResult")) {
    list(
      type = "ComputeResult",
      computation = tryCatch(obj@computationDetails, error = function(e) NA_character_),
      hasStatistics = isTRUE(nrow(obj@statistics) > 0)
    )
  } else if (inherits(obj, "igraph")) {
    list(type = "igraph",
         vcount = igraph::vcount(obj),
         ecount = igraph::ecount(obj))
  } else {
    list(type = class(obj)[1])
  }
}

.mcp_df_to_list <- function(df, limit = NULL) {
  # data.frame / data.table -> column-oriented list, JSON-safe.
  if (is.null(df)) return(NULL)
  if (!is.null(limit) && nrow(df) > limit) {
    df <- df[seq_len(limit), , drop = FALSE]
    attr(df, "truncated") <- TRUE
  }
  out <- as.list(df)
  out[["__nrow__"]] <- nrow(df)
  out[["__truncated__"]] <- isTRUE(attr(df, "truncated"))
  out
}

# --- Capability probe -----------------------------------------------------
# Set in startup.R before this file is sourced. Default FALSE so the shim
# is still safe to load in environments where the master bootstrap didn't
# run (e.g. unit tests that source mcp_handlers.R directly).
if (!exists(".mcp_has_curated_data", inherits = TRUE)) {
  .mcp_has_curated_data <- FALSE
}

.mcp_require_curated_data <- function() {
  if (!isTRUE(.mcp_has_curated_data)) {
    stop("microbiomeData package is not installed in this container",
         call. = FALSE)
  }
}

mcp_capabilities <- function() {
  .mcp_ok({
    list(
      has_curated_data = isTRUE(.mcp_has_curated_data),
      microbiomedb     = as.character(utils::packageVersion("MicrobiomeDB")),
      microbiomedata   = if (isTRUE(.mcp_has_curated_data))
        as.character(utils::packageVersion("microbiomeData")) else NA_character_
    )
  })
}

# --- Ping / health --------------------------------------------------------

mcp_ping <- function() {
  .mcp_ok({
    list(
      r_version    = as.character(getRversion()),
      microbiomedb = as.character(utils::packageVersion("MicrobiomeDB")),
      microbiomedata = if (isTRUE(.mcp_has_curated_data))
        as.character(utils::packageVersion("microbiomeData")) else NA_character_,
      has_curated_data = isTRUE(.mcp_has_curated_data),
      pid          = Sys.getpid(),
      handles      = length(ls(.mcp_env)),
      jobs         = length(ls(.mcp_jobs)),
      time         = as.character(Sys.time())
    )
  })
}

# --- Discovery ------------------------------------------------------------

mcp_listCuratedDatasets <- function() {
  .mcp_ok({
    .mcp_require_curated_data()
    # NOTE: do NOT use `return()` inside .mcp_ok({...}) blocks. `return()`
    # is lexically scoped to the enclosing mcp_* function, so it bypasses
    # the envelope wrap and produces a malformed reply that the Python
    # client surfaces as "unknown R error".
    if (exists("getCuratedDatasetNames",
               where = asNamespace("microbiomeData"))) {
      sort(microbiomeData::getCuratedDatasetNames())
    } else {
      # Fall back to scanning exported data sets.
      ds <- utils::data(package = "microbiomeData")$results[, "Item"]
      sort(unique(ds))
    }
  })
}

mcp_loadDataset <- function(name) {
  .mcp_ok({
    .mcp_require_curated_data()
    # microbiomeData ships its curated datasets as LazyData; the symbols
    # are NOT directly visible in asNamespace("microbiomeData") via
    # `get(..., inherits=FALSE)`. The canonical way to materialize them
    # is `data()`, which resolves the lazy-load db and binds the object
    # into the supplied environment. Once realized, it stays in this
    # session's RAM for the lifetime of the R child (CoW from the master
    # if it had been pre-realized; otherwise it's a one-time cost here).
    if (!(name %in% utils::data(package = "microbiomeData")$results[, "Item"])) {
      stop(sprintf("'%s' is not a curated microbiomeData dataset", name),
           call. = FALSE)
    }
    env <- new.env(parent = emptyenv())
    utils::data(list = name, package = "microbiomeData", envir = env)
    obj <- env[[name]]
    if (!inherits(obj, "MbioDataset")) {
      stop(sprintf("%s is not an MbioDataset", name), call. = FALSE)
    }
    h <- .mcp_register(obj, "MbioDataset")
    list(handle = h, summary = .mcp_summary(obj))
  })
}

mcp_getCollectionNames <- function(dataset_handle) {
  .mcp_ok(getCollectionNames(.mcp_require(dataset_handle, "MbioDataset")))
}

mcp_getCollection <- function(dataset_handle, name, format = "default") {
  .mcp_ok({
    ds <- .mcp_require(dataset_handle, "MbioDataset")
    coll <- if (identical(format, "default")) {
      getCollection(ds, name)
    } else {
      getCollection(ds, name, format = format)
    }
    h <- .mcp_register(coll, "Collection")
    list(handle = h, summary = .mcp_summary(coll))
  })
}

mcp_getMetadataVariableNames <- function(dataset_handle) {
  .mcp_ok(getMetadataVariableNames(.mcp_require(dataset_handle, "MbioDataset")))
}

mcp_getMetadataVariableSummary <- function(dataset_handle, variables = NULL) {
  .mcp_ok({
    ds <- .mcp_require(dataset_handle, "MbioDataset")
    if (is.null(variables) || identical(variables, "")) {
      getMetadataVariableSummary(ds)
    } else {
      getMetadataVariableSummary(ds, variables)
    }
  })
}

mcp_getSampleMetadata <- function(dataset_handle, limit = 1000L) {
  .mcp_ok({
    ds <- .mcp_require(dataset_handle, "MbioDataset")
    sm <- getSampleMetadata(ds)
    # `getSampleMetadata` returns a SampleMetadata S4 object containing a
    # data.table. Coerce to a plain data.frame so jsonlite serializes cleanly.
    dt <- tryCatch(sm@data, error = function(e) as.data.frame(sm))
    .mcp_df_to_list(as.data.frame(dt), limit = limit)
  })
}

# --- Result retrieval -----------------------------------------------------

mcp_getComputeResult <- function(result_handle, format = "data.table",
                                 limit = 100000L) {
  .mcp_ok({
    res <- .mcp_require(result_handle, "ComputeResult")
    if (identical(format, "igraph")) {
      g <- getComputeResult(res, format = "igraph")
      h <- .mcp_register(g, "igraph")
      list(handle = h, summary = .mcp_summary(g))
    } else {
      dt <- getComputeResult(res)
      .mcp_df_to_list(as.data.frame(dt), limit = limit)
    }
  })
}

mcp_getComputeResultWithMetadata <- function(result_handle, dataset_handle,
                                             variables, limit = 100000L) {
  .mcp_ok({
    res <- .mcp_require(result_handle, "ComputeResult")
    ds  <- .mcp_require(dataset_handle, "MbioDataset")
    dt  <- getComputeResultWithMetadata(res, ds, variables)
    .mcp_df_to_list(as.data.frame(dt), limit = limit)
  })
}

mcp_correlationNetwork <- function(result_handle, uploads_dir = "/srv/uploads") {
  # Render a correlation network widget to disk and return the path. The
  # path is on the shared uploads volume so the mcp service can stream it
  # back to the client (or expose a download URL).
  .mcp_ok({
    res <- .mcp_require(result_handle, "ComputeResult")
    dt  <- getComputeResult(res)
    out <- file.path(uploads_dir,
                     paste0("network-", .mcp_new_handle(), ".html"))
    htmlwidgets::saveWidget(correlationNetwork(dt), out, selfcontained = TRUE)
    list(path = out)
  })
}

# --- Handle lifecycle -----------------------------------------------------

mcp_listHandles <- function() {
  .mcp_ok({
    keys <- ls(.mcp_env, all.names = TRUE)
    if (!length(keys)) {
      list()
    } else lapply(keys, function(k) {
      e <- .mcp_env[[k]]
      list(handle = k, kind = e$kind,
           last_access = as.character(e$last_access))
    })
  })
}

mcp_free <- function(handle) {
  .mcp_ok({
    if (exists(handle, envir = .mcp_env, inherits = FALSE)) {
      rm(list = handle, envir = .mcp_env)
      TRUE
    } else {
      FALSE
    }
  })
}

mcp_setMaxJobs <- function(n) {
  .mcp_ok({
    n <- as.integer(n)
    if (is.na(n) || n < 1L) stop("n must be a positive integer", call. = FALSE)
    .mcp_max_jobs <<- n
    list(max_jobs = n)
  })
}

mcp_sessionInfo <- function() {
  .mcp_ok({
    list(
      pid       = Sys.getpid(),
      handles   = length(ls(.mcp_env)),
      jobs      = length(ls(.mcp_jobs)),
      memory_mb = as.numeric(gc(reset = FALSE)[2, 2])
    )
  })
}
