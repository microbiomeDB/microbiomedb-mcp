# Lightweight smoke tests for the R shim. These run inside the rserve
# container (or any R session with MicrobiomeDB + microbiomeData loaded)
# and don't require a live Rserve socket.
#
# Run with:
#   Rscript -e "testthat::test_dir('tests/testthat')"

library(testthat)

# Source the shim. Paths assume the tests run from the repo root.
source("docker/rserve/R/mcp_handlers.R")
source("docker/rserve/R/mcp_jobs.R")
source("docker/rserve/R/mcp_imports.R")

# Reset session registries between tests so leftover handles don't bleed.
reset_state <- function() {
  rm(list = ls(.mcp_env),  envir = .mcp_env)
  rm(list = ls(.mcp_jobs), envir = .mcp_jobs)
}

test_that("mcp_ping returns ok with structural fields", {
  reset_state()
  r <- mcp_ping()
  expect_true(r$ok)
  expect_true("r_version" %in% names(r$value))
  expect_true(is.numeric(r$value$pid))
})

test_that(".mcp_ok wraps errors into envelopes", {
  r <- .mcp_ok(stop("nope"))
  expect_false(r$ok)
  expect_equal(r$error, "nope")
})

test_that("handle registry round-trips", {
  reset_state()
  h <- .mcp_register(list(a = 1), "TestKind")
  expect_true(exists(h, envir = .mcp_env))
  expect_equal(.mcp_require(h, "TestKind")$a, 1)
  expect_error(.mcp_require(h, "OtherKind"), "expected")
  r <- mcp_free(h)
  expect_true(r$value)
  expect_false(exists(h, envir = .mcp_env))
})

test_that("mcp_setMaxJobs enforces sanity", {
  r_bad <- mcp_setMaxJobs(0)
  expect_false(r_bad$ok)
  r <- mcp_setMaxJobs(3)
  expect_true(r$ok)
  expect_equal(.mcp_max_jobs, 3L)
})

test_that("startJob runs and completes for a trivial expression", {
  reset_state()
  # Register a fake compute fn that returns immediately, so we don't need
  # MicrobiomeDB curated data realized to test the engine itself.
  .mcp_compute_fns[["__noop"]] <<- function(x = 1) list(value = x * 2)

  r <- mcp_startJob("__noop", '{"x": 21}')
  expect_true(r$ok)
  job_id <- r$value$job_id

  # Poll up to a few seconds; mcparallel may need a tick.
  ok <- FALSE
  for (i in 1:50) {
    s <- mcp_jobStatus(job_id)
    if (identical(s$value$status, "succeeded")) { ok <- TRUE; break }
    Sys.sleep(0.05)
  }
  expect_true(ok)

  res <- mcp_jobResult(job_id, "summary")
  expect_true(res$ok)
  # The noop function returned a non-ComputeResult list, so we get a
  # handle + summary with a generic type.
  expect_true(!is.null(res$value$result_handle))
})

test_that("startJob respects per-session cap", {
  reset_state()
  .mcp_compute_fns[["__sleep"]] <<- function() Sys.sleep(2)
  mcp_setMaxJobs(1)

  a <- mcp_startJob("__sleep", "{}")
  expect_true(a$ok)
  b <- mcp_startJob("__sleep", "{}")
  expect_false(b$ok)
  expect_match(b$error, "job cap")

  # Clean up so the test process exits promptly.
  mcp_cancelJob(a$value$job_id)
})
