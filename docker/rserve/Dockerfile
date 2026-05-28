# syntax=docker/dockerfile:1.7
#
# Rserve image for microbiomedb-mcp.
#
# Modeled on https://github.com/VEuPathDB/Rserve (which is itself just a
# Dockerfile + Rserv.conf, not an R package). Our additions:
#   * pre-install MicrobiomeDB + microbiomeData so curated data is available
#     without reload between sessions
#   * source the mcp_* shim at master startup
#
# The master R process loads MicrobiomeDB + microbiomeData + the shim once.
# Rserve forks per client connection so each session COW-inherits the
# loaded packages (no reload between sessions). Inside a session, long
# computations are forked again via parallel::mcparallel so they ALSO
# COW-inherit the session's user-imported data.
#
# Build tip: this image is large (Bioconductor + curated data). Use
# BuildKit cache mounts (--mount=type=cache,target=...) if you iterate.

ARG R_VERSION=4.4.2

FROM rocker/r-ver:${R_VERSION} AS base

ENV DEBIAN_FRONTEND=noninteractive \
    R_LIBS_USER=/usr/local/lib/R/site-library

# Optional GH PAT for unauthenticated rate-limit relief during `remotes::install_github`.
# Not stored in the final image layers if you build with --secret; for a
# plain ARG, do `docker build --build-arg GITHUB_PAT=...` only on dev hosts.
ARG GITHUB_PAT=""
ENV GITHUB_PAT=$GITHUB_PAT

# --- System deps ----------------------------------------------------------
# A superset of the VEuPathDB/Rserve image plus the libraries needed by
# phyloseq / dada2 / mia / biomformat (the importer dependencies).
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        git \
        wget \
        curl \
        pkg-config \
        libglpk-dev \
        libxml2-dev \
        libssl-dev \
        libcurl4-openssl-dev \
        libgsl-dev \
        libfontconfig1-dev \
        libharfbuzz-dev \
        libfribidi-dev \
        libfreetype6-dev \
        libpng-dev \
        libtiff5-dev \
        libjpeg-dev \
        libbz2-dev \
        liblzma-dev \
        libicu-dev \
        zlib1g-dev \
        libsodium-dev \
    && rm -rf /var/lib/apt/lists/*

# --- S4Vectors / Bioconductor CFLAGS workaround --------------------------
# Bioconductor S4Vectors calls error(variable) instead of error("%s", variable),
# which fails to compile on Ubuntu 24.04 (rocker/r-ver:4.4.x) under
# -Werror=format-security. Drop just that flag; keep the rest.
# Lifted verbatim from VEuPathDB/Rserve's Dockerfile.
# TODO: remove once Bioconductor fixes the upstream issue.
RUN mkdir -p /root/.R && \
    echo "CFLAGS = -g -O2 -fstack-protector-strong -Wformat -Wdate-time -D_FORTIFY_SOURCE=2" > /root/.R/Makevars

# --- Core R toolchain ----------------------------------------------------
# Pin Rserve to the same rforge.net build VEuPathDB uses; pull the rest
# from rocker's bundled PPM mirror.
RUN R -e "install.packages('Rserve', version='1.8-9', repos='http://rforge.net')"

RUN R -e "install.packages(c('BiocManager','remotes','jsonlite','uuid','data.table','igraph','purrr','Rcpp','digest','bit64','dplyr','readr','Hmisc'))"

# --- Bioconductor deps ---------------------------------------------------
RUN R -e "BiocManager::install(c('SummarizedExperiment','DESeq2','TreeSummarizedExperiment','mia','dada2','biomformat','phyloseq'), ask=FALSE, update=FALSE)"

# --- MicrobiomeDB + curated data + their transitive deps ------------------
# Pinned to known-good releases. Bump these tags deliberately rather than
# tracking main, so reproducible image builds are possible.
# `remotes::install_github` resolves the dep graph (pulls mbioUtils,
# corGraph, microbiomeComputations, Maaslin2, ...). upgrade_dependencies=
# FALSE keeps already-installed CRAN/Bioconductor pkgs at their existing
# versions so this step doesn't reshuffle the Bioconductor matrix above.
#
# IMPORTANT: each install is followed by a `requireNamespace` check that
# stop()s on failure. Without it, install_github occasionally exits with
# status 0 even on network/rate-limit failures (its errors are sometimes
# `message()` rather than `stop()`). A clean exit caches a broken layer in
# Docker, so subsequent rebuilds reuse the cached "success" and the
# package silently never gets installed. The explicit check makes failure
# loud and cache-busting.
#
# GITHUB_PAT is declared as an ARG and forwarded into the RUN env so
# remotes::install_github can authenticate (avoids the 60 req/hr
# unauthenticated GitHub rate limit on large rebuilds). Using ARG +
# inline env (not ENV) keeps the token out of the final image layers.
# R's default download timeout is 60s. The microbiomeData tarball
# (curated datasets are vendored into /data) routinely takes longer than
# that to fetch from GitHub, hitting `download from ... failed`. Bump it
# to 10 minutes for any github tarball download in this image.
ARG GITHUB_PAT=
ARG MICROBIOMEDB_REF=v1.1.2
RUN GITHUB_PAT="$GITHUB_PAT" R -e "\
  options(timeout = 600); \
  remotes::install_github('microbiomeDB/MicrobiomeDB@${MICROBIOMEDB_REF}', dependencies=TRUE, upgrade_dependencies=FALSE); \
  if (!requireNamespace('MicrobiomeDB', quietly=TRUE)) stop('MicrobiomeDB install failed')"

# microbiomeData is OPTIONAL. Set INSTALL_MICROBIOMEDATA=false to skip it
# (useful for slimmer images, or when the GitHub tarball download is
# rate-limited and you only need user-import workflows). The runtime shim
# detects its absence and the Python layer hides curated-dataset tools
# accordingly. The package is large; dropping it saves ~hundreds of MB.
#
# Pinned to a specific post-v1.0.7 commit because v1.0.7 (the latest
# release tag) still references the now-removed veupathUtils package,
# which makes MicrobiomeDB::getCollection() fail at call-time with
# "unable to find required package 'veupathUtils'". The fork of
# veupathUtils into mbioUtils was never tagged in microbiomeData; this
# commit is the first one that compiles and runs cleanly against the
# mbioUtils-based stack. Bump to a release tag once one exists upstream.
ARG MICROBIOMEDATA_REF=ced9da6f012842596ffa411b473e2781f296f384
ARG INSTALL_MICROBIOMEDATA=true
RUN if [ "$INSTALL_MICROBIOMEDATA" = "true" ]; then \
      GITHUB_PAT="$GITHUB_PAT" R -e "\
        options(timeout = 600); \
        remotes::install_github('microbiomeDB/microbiomeData@${MICROBIOMEDATA_REF}', dependencies=TRUE, upgrade_dependencies=FALSE); \
        if (!requireNamespace('microbiomeData', quietly=TRUE)) stop('microbiomeData install failed')"; \
    else \
      echo "[microbiomedb-mcp] Skipping microbiomeData install (INSTALL_MICROBIOMEDATA=$INSTALL_MICROBIOMEDATA)"; \
    fi

# --- App layout ----------------------------------------------------------
RUN useradd rserve \
 && mkdir -p /home/rserve /srv/uploads /opt/microbiomedb-mcp \
 && chown -R rserve:rserve /home/rserve /srv/uploads /opt/microbiomedb-mcp

COPY Rserv.conf /etc/Rserv.conf
COPY startup.R /opt/microbiomedb-mcp/startup.R
COPY R /opt/microbiomedb-mcp/R

USER rserve
WORKDIR /home/rserve

EXPOSE 6311

# Standalone Rserve binary so the full Rserv.conf is honored (run.Rserve
# silently ignores most directives). `daemon disable` in Rserv.conf keeps
# the master in the foreground so docker can manage the lifecycle and
# stdout/stderr land in container logs. The conf file's `source`
# directive pulls in startup.R, which preloads MicrobiomeDB + the shim
# in the master before any client connects.
CMD ["R", "CMD", "Rserve", "--no-save", "--RS-conf", "/etc/Rserv.conf"]
