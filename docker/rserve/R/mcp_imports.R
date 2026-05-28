# mcp_imports.R
#
# Wrappers around the MicrobiomeDB import* functions. All paths must point
# under the shared /srv/uploads volume; the mcp service writes uploaded
# bytes there before calling these. Resulting MbioDataset objects are
# registered in this session's .mcp_env so subsequent compute jobs reuse
# them with no reload (consistent with the curated-data path).
#
# Each importer follows the same pattern: validate inputs, call the
# underlying MicrobiomeDB function, register, return handle + summary.

.mcp_check_path <- function(path, uploads_dir = "/srv/uploads") {
  if (!is.character(path) || length(path) != 1L || !nzchar(path)) {
    stop("path must be a non-empty string", call. = FALSE)
  }
  abs <- normalizePath(path, mustWork = FALSE)
  if (!startsWith(abs, normalizePath(uploads_dir, mustWork = FALSE))) {
    stop(sprintf("path must live under %s", uploads_dir), call. = FALSE)
  }
  if (!file.exists(abs)) {
    stop(sprintf("file not found: %s", abs), call. = FALSE)
  }
  abs
}

.mcp_register_import <- function(obj) {
  if (!inherits(obj, "MbioDataset")) {
    stop("importer did not return an MbioDataset", call. = FALSE)
  }
  h <- .mcp_register(obj, "MbioDataset")
  list(handle = h, summary = .mcp_summary(obj))
}

mcp_importBIOM <- function(path, ...) {
  .mcp_ok(.mcp_register_import(importBIOM(.mcp_check_path(path), ...)))
}

mcp_importQIIME2 <- function(path, ...) {
  .mcp_ok(.mcp_register_import(importQIIME2(.mcp_check_path(path), ...)))
}

mcp_importMothur <- function(path, ...) {
  .mcp_ok(.mcp_register_import(importMothur(.mcp_check_path(path), ...)))
}

mcp_importDADA2 <- function(path, ...) {
  .mcp_ok(.mcp_register_import(importDADA2(.mcp_check_path(path), ...)))
}

mcp_importHUMAnN <- function(path, ...) {
  .mcp_ok(.mcp_register_import(importHUMAnN(.mcp_check_path(path), ...)))
}

mcp_importMetaPhlAn <- function(path, ...) {
  .mcp_ok(.mcp_register_import(importMetaPhlAn(.mcp_check_path(path), ...)))
}

mcp_importPhyloseq <- function(path, ...) {
  # importPhyloseq accepts an in-memory phyloseq object, but over Rserve we
  # only ship paths. We readRDS from a path under uploads.
  .mcp_ok({
    p <- .mcp_check_path(path)
    obj <- readRDS(p)
    .mcp_register_import(importPhyloseq(obj, ...))
  })
}

mcp_importTreeSE <- function(path, ...) {
  .mcp_ok({
    p <- .mcp_check_path(path)
    obj <- readRDS(p)
    .mcp_register_import(importTreeSummarizedExperiment(obj, ...))
  })
}
