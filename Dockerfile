# syntax=docker/dockerfile:1

# Serves the streamable-http transport. No credential is baked in or expected
# at runtime: each request carries its own Parcel key in an X-Parcel-Token or
# Authorization: Bearer header, so one container can serve several people.
#
# Both stages come from Docker Hardened Images. dhi.io refuses anonymous pulls,
# so run `docker login dhi.io` with a Docker Hub account before building.

# The package version comes from the nearest git tag, but .dockerignore keeps
# .git out of the build context on purpose, so hatchling cannot read it here.
# This ARG is the only way in, and a build without it honestly reports 0.0.0
# rather than a stale hardcoded number:
#   docker build --build-arg VERSION=$(git describe --tags --abbrev=0 | sed s/^v//) .
# Declared here so both stages share one default: the build stage feeds it to
# the install, the runtime stage to the OCI label.
ARG VERSION=0.0.0

# The -dev variant carries a shell and runs as root, which the build needs.
# The runtime image below has neither, so nothing can run there but Python.
FROM dhi.io/python:3.13-dev AS build

# uv is pinned like any other dependency, and stays in this stage.
COPY --from=ghcr.io/astral-sh/uv:0.12.9 /uv /uvx /bin/

# The venv is copied into the runtime stage, so it must point at an interpreter
# that exists there: both variants ship it at /usr/bin/python3.13, and uv must
# never download its own. Bytecode is compiled now because the runtime user
# cannot write next to a root-owned venv. Copy mode, since the cache is a mount.
ENV UV_PYTHON=/usr/bin/python3.13 \
    UV_PYTHON_DOWNLOADS=never \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

# Dependencies in their own cached layer.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project --no-dev --no-editable

ARG VERSION

# --no-editable installs the project into the venv as a regular wheel, so the
# runtime stage needs the venv alone, not the source tree.
COPY . .
RUN --mount=type=cache,target=/root/.cache/uv \
    SETUPTOOLS_SCM_PRETEND_VERSION=${VERSION} uv sync --locked --no-dev --no-editable


# Distroless-style runtime: no shell, no package manager, nonroot by default.
FROM dhi.io/python:3.13

ARG VERSION

WORKDIR /app

# Root-owned on purpose: the app runs the venv but cannot rewrite it.
COPY --from=build /app/.venv /app/.venv

# The image already defaults to nonroot; stated numerically so Kubernetes
# runAsNonRoot can verify it without resolving a name.
USER 65532:65532

ENV PATH="/app/.venv/bin:$PATH" \
    PARCEL_TRANSPORT=streamable-http \
    PARCEL_HOST=0.0.0.0 \
    PARCEL_PORT=8000 \
    PARCEL_PATH=/mcp

EXPOSE 8000

LABEL org.opencontainers.image.title="parcelapp-mcp" \
      org.opencontainers.image.description="MCP server for the Parcel delivery tracking app: read and add deliveries over stdio or HTTP" \
      org.opencontainers.image.source="https://github.com/obeone/parcelapp-mcp" \
      org.opencontainers.image.documentation="https://github.com/obeone/parcelapp-mcp#readme" \
      org.opencontainers.image.url="https://github.com/obeone/parcelapp-mcp" \
      org.opencontainers.image.authors="Grégoire Compagnon <obeone@obeone.org>" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${VERSION}"

# There is no /health endpoint, and a GET on /mcp opens an SSE stream that
# never returns, so neither would work. This performs a real MCP initialize
# handshake instead: it proves the session manager and the tool registry are
# alive, not merely that a socket is open. urlopen raises on a non-2xx, and the
# assert catches a 200 that is not a protocol response. Either one exits 1.
# Exec form is mandatory: the runtime image has no /bin/sh to run a shell form.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import json,urllib.request as u; \
r=u.Request('http://127.0.0.1:8000/mcp', \
data=json.dumps({'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-06-18','capabilities':{},'clientInfo':{'name':'healthcheck','version':'0'}}}).encode(), \
headers={'Content-Type':'application/json','Accept':'application/json, text/event-stream'}); \
assert b'\"result\"' in u.urlopen(r, timeout=4).read()"]

# Exec form, so the server is PID 1 and receives SIGTERM directly. uvicorn
# installs its own handler and drains, so no init shim is needed.
CMD ["parcelapp-mcp"]
