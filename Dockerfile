# syntax=docker/dockerfile:1

# Serves the streamable-http transport. No credential is baked in or expected
# at runtime: each request carries its own Parcel key in an X-Parcel-Token or
# Authorization: Bearer header, so one container can serve several people.

FROM python:3.13-slim

# uv is pinned like any other dependency.
COPY --from=ghcr.io/astral-sh/uv:0.12.9 /uv /uvx /bin/

WORKDIR /app

# Created first: a --chown naming a user this stage does not know yet is
# silently dropped, and the files land as root.
RUN groupadd -r -g 10001 app && useradd -r -u 10001 -g app app

# Dependencies as root, in their own cached layer. /app/.venv stays root-owned
# on purpose: the app runs it but cannot rewrite it.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project --no-dev

COPY --chown=app:app . .
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev

# Hand over the workdir, not the dependency tree, then drop privileges last.
RUN chown app:app /app
USER app

ENV PATH="/app/.venv/bin:$PATH" \
    PARCEL_TRANSPORT=streamable-http \
    PARCEL_HOST=0.0.0.0 \
    PARCEL_PORT=8000 \
    PARCEL_PATH=/mcp

EXPOSE 8000

# The one label that cannot read itself from the package metadata, so it is an
# ARG: pass --build-arg VERSION=$(uv version --short) to keep it truthful.
ARG VERSION=0.2.0

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
# assert catches a 200 that is not a protocol response.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import json,urllib.request as u; \
r=u.Request('http://127.0.0.1:8000/mcp', \
data=json.dumps({'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-06-18','capabilities':{},'clientInfo':{'name':'healthcheck','version':'0'}}}).encode(), \
headers={'Content-Type':'application/json','Accept':'application/json, text/event-stream'}); \
assert b'\"result\"' in u.urlopen(r, timeout=4).read()" || exit 1

# Exec form, so the server is PID 1 and receives SIGTERM directly. uvicorn
# installs its own handler and drains, so no init shim is needed.
CMD ["parcelapp-mcp"]
