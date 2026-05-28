# mcp_jobs.R
#
# Async job engine for long-running MicrobiomeDB computations.
#
# Why parallel::mcparallel?
# -------------------------
# Each MCP session is already a forked Rserve child. When that child calls
# mcparallel(expr), the resulting job process is a fork of the SESSION
# process and therefore COW-inherits:
#   - the master's loaded packages (MicrobiomeDB, microbiomeData, ...)
#   - any datasets the session previously realized (loaded curated data
#     and user-imported MbioDataset objects)
#   - any result handles already in .mcp_env
# So launching a job costs ~zero RAM beyond what the job itself writes.
#
# We do NOT rely on the `parallel` package's higher-level helpers (e.g.
# mclapply) because those block. Instead we use mcparallel + mccollect with
# wait=FALSE for true async polling.
#
# Cancellation uses SIGTERM via tools::pskill, followed by a non-blocking
# mccollect to reap the zombie. We mark cancellation explicitly so a slow
# kill can't be confused with normal failure.

# A whitelist of compute functions exposed to start_job. Keeping this
# explicit means clients can't ask the engine to execute arbitrary R.
.mcp_compute_fns <- list(
  alphaDiv               = function(...) MicrobiomeDB::alphaDiv(...),
  betaDiv                = function(...) MicrobiomeDB::betaDiv(...),
  rankedAbundance        = function(...) MicrobiomeDB::rankedAbundance(...),
  correlation            = function(...) MicrobiomeDB::correlation(...),
  selfCorrelation        = function(...) MicrobiomeDB::selfCorrelation(...),
  differentialAbundance  = function(...) MicrobiomeDB::differentialAbundance(...)
)

.mcp_resolve_handles <- function(args) {
  # Walk an argument list and swap any {"$handle": "..."} stub for the
  # corresponding registered object. Lets the Python side pass collection /
  # dataset handles as JSON without leaking R object lifecycle to clients.
  if (is.list(args)) {
    if (length(args) == 1 && !is.null(names(args)) &&
        identical(names(args), "$handle") && is.character(args[[1]])) {
      return(.mcp_require(args[[1]]))
    }
    return(lapply(args, .mcp_resolve_handles))
  }
  args
}

.mcp_build_fun <- function(kind, args_json) {
  fn <- .mcp_compute_fns[[kind]]
  if (is.null(fn)) {
    stop(sprintf("unsupported compute kind: %s", kind), call. = FALSE)
  }
  args <- if (is.null(args_json) || identical(args_json, "")) list()
          else jsonlite::fromJSON(args_json, simplifyVector = FALSE)
  args <- .mcp_resolve_handles(args)

  # Special-case: differentialAbundance group selectors arrive as serialized
  # expressions like "x < 300". We compile them to closures here.
  # The Python side documents this contract.
  for (k in c("groupA", "groupB")) {
    if (!is.null(args[[k]]) && is.character(args[[k]])) {
      expr <- args[[k]]
      args[[k]] <- local({
        body_text <- expr
        eval(parse(text = sprintf("function(x) { %s }", body_text)))
      })
    }
  }

  list(fn = fn, args = args)
}

mcp_startJob <- function(kind, args_json = "{}") {
  .mcp_ok({
    # Enforce the per-session concurrency cap.
    running <- vapply(ls(.mcp_jobs), function(j) {
      identical(.mcp_jobs[[j]]$status, "running")
    }, logical(1))
    if (sum(running) >= .mcp_max_jobs) {
      stop(sprintf("session job cap reached (%d)", .mcp_max_jobs), call. = FALSE)
    }

    spec <- .mcp_build_fun(kind, args_json)
    job_id <- .mcp_new_handle()

    pj <- parallel::mcparallel({
      do.call(spec$fn, spec$args)
    }, name = job_id, mc.set.seed = TRUE)

    .mcp_jobs[[job_id]] <- list(
      status      = "running",
      kind        = kind,
      parallel    = pj,
      pid         = pj$pid,
      started_at  = Sys.time(),
      finished_at = NA,
      result_handle = NA_character_,
      error       = NA_character_
    )

    list(job_id = job_id, kind = kind, status = "running",
         started_at = as.character(.mcp_jobs[[job_id]]$started_at))
  })
}

.mcp_poll_job <- function(job_id) {
  # Internal: advance a job's state by polling mccollect once. Idempotent
  # for terminal states.
  entry <- .mcp_jobs[[job_id]]
  if (is.null(entry)) stop(sprintf("unknown job: %s", job_id), call. = FALSE)
  if (!identical(entry$status, "running")) return(entry)

  result <- parallel::mccollect(entry$parallel, wait = FALSE)
  if (is.null(result)) {
    return(entry)   # still running
  }

  val <- result[[1]]
  entry$finished_at <- Sys.time()

  if (inherits(val, "try-error")) {
    entry$status <- "failed"
    entry$error  <- as.character(val)
  } else {
    entry$status <- "succeeded"
    # Register the result as a handle so subsequent get_job_result calls
    # don't reserialize a potentially huge object across the wire.
    kind <- if (inherits(val, "ComputeResult")) "ComputeResult" else class(val)[1]
    entry$result_handle <- .mcp_register(val, kind)
  }
  .mcp_jobs[[job_id]] <- entry
  entry
}

mcp_jobStatus <- function(job_id) {
  .mcp_ok({
    e <- .mcp_poll_job(job_id)
    list(
      job_id        = job_id,
      kind          = e$kind,
      status        = e$status,
      started_at    = as.character(e$started_at),
      finished_at   = if (is.na(e$finished_at)) NA_character_ else as.character(e$finished_at),
      result_handle = e$result_handle,
      error         = e$error
    )
  })
}

mcp_jobResult <- function(job_id, format = "summary", limit = 100000L) {
  # `format = "summary"` returns the handle + .mcp_summary() of the result.
  # `format = "data.table"` materializes the result via getComputeResult.
  # `format = "igraph"` returns a new handle for the igraph view.
  #
  # NOTE: do NOT use `return()` inside .mcp_ok({...}). It returns from
  # this function and bypasses the envelope wrap, surfacing on the
  # client as "unknown R error".
  .mcp_ok({
    e <- .mcp_poll_job(job_id)
    if (!identical(e$status, "succeeded")) {
      stop(sprintf("job %s is %s, not succeeded", job_id, e$status), call. = FALSE)
    }
    h <- e$result_handle
    obj <- .mcp_require(h)
    if (identical(format, "summary")) {
      list(result_handle = h, summary = .mcp_summary(obj))
    } else if (identical(format, "igraph")) {
      if (!inherits(obj, "ComputeResult")) {
        stop("result is not a ComputeResult; only `summary` format is available",
             call. = FALSE)
      }
      g <- getComputeResult(obj, format = "igraph")
      gh <- .mcp_register(g, "igraph")
      list(handle = gh, summary = .mcp_summary(g))
    } else {
      if (!inherits(obj, "ComputeResult")) {
        stop("result is not a ComputeResult; only `summary` format is available",
             call. = FALSE)
      }
      dt <- getComputeResult(obj)
      .mcp_df_to_list(as.data.frame(dt), limit = limit)
    }
  })
}

mcp_cancelJob <- function(job_id) {
  .mcp_ok({
    entry <- .mcp_jobs[[job_id]]
    if (is.null(entry)) stop(sprintf("unknown job: %s", job_id), call. = FALSE)
    if (!identical(entry$status, "running")) {
      list(job_id = job_id, status = entry$status, cancelled = FALSE)
    } else {
      tools::pskill(entry$pid, tools::SIGTERM)
      # Reap so we don't leak a zombie. wait=TRUE here is safe: the child
      # has been signalled and should exit promptly.
      invisible(parallel::mccollect(entry$parallel, wait = TRUE))
      entry$status      <- "cancelled"
      entry$finished_at <- Sys.time()
      .mcp_jobs[[job_id]] <- entry
      list(job_id = job_id, status = "cancelled", cancelled = TRUE)
    }
  })
}

mcp_listJobs <- function() {
  .mcp_ok({
    keys <- ls(.mcp_jobs, all.names = TRUE)
    if (!length(keys)) {
      list()
    } else lapply(keys, function(k) {
      # Poll first so callers see current state without an extra round-trip.
      e <- .mcp_poll_job(k)
      list(job_id = k, kind = e$kind, status = e$status,
           started_at = as.character(e$started_at),
           finished_at = if (is.na(e$finished_at)) NA_character_ else as.character(e$finished_at),
           result_handle = e$result_handle)
    })
  })
}

mcp_freeJob <- function(job_id) {
  .mcp_ok({
    if (exists(job_id, envir = .mcp_jobs, inherits = FALSE)) {
      entry <- .mcp_jobs[[job_id]]
      # If still running, best-effort cancel before forgetting.
      if (identical(entry$status, "running")) {
        try(tools::pskill(entry$pid, tools::SIGTERM), silent = TRUE)
        try(parallel::mccollect(entry$parallel, wait = TRUE), silent = TRUE)
      }
      rm(list = job_id, envir = .mcp_jobs)
      TRUE
    } else {
      FALSE
    }
  })
}
