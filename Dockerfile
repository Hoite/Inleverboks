# syntax=docker/dockerfile:1.19
# Inleverboks container image: multi-stage build with uv, non-root, read-only root filesystem.

# Base images are pinned on tag and digest directly in FROM, so Dependabot can update them.
FROM ghcr.io/astral-sh/uv:0.13.9@sha256:d3b601f0c7b118bc1753febf7c41cc80981df678445e0254284eecf84db79a7d AS uv

# ---------------------------------------------------------------------------
FROM python:3.15.2-slim-trixie@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928 AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /src

# Dependencies first, for layer caching.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-dev --no-install-project

COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

# ---------------------------------------------------------------------------
FROM python:3.15.2-slim-trixie@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928 AS runtime

LABEL org.opencontainers.image.title="Inleverboks" \
      org.opencontainers.image.description="Digitaal examenwerk inleveren" \
      org.opencontainers.image.source="https://github.com/hoite/Inleverboks" \
      org.opencontainers.image.licenses="EUPL-1.2"

# Security updates of the base image, system libraries for WeasyPrint (Pango/HarfBuzz) and
# a basic font set. pip is not needed at runtime: removing it (and the ensurepip wheels)
# also removes the packages it vendors from the image and its vulnerability scans.
RUN apt-get update \
 && apt-get upgrade -y --no-install-recommends \
 && apt-get install -y --no-install-recommends \
      libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz-subset0 fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/* \
 && rm -rf /usr/local/lib/python3.15/site-packages/pip /usr/local/lib/python3.15/site-packages/pip-* \
      /usr/local/lib/python3.15/ensurepip/_bundled /usr/local/bin/pip* \
 && groupadd --gid 10001 inleverboks \
 && useradd --uid 10001 --gid 10001 --home-dir /tmp --no-create-home \
      --shell /usr/sbin/nologin inleverboks \
 && mkdir -p /data \
 && chown 10001:10001 /data

COPY --from=build /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp \
    XDG_CACHE_HOME=/tmp/.cache \
    PORT=8080

WORKDIR /tmp
USER 10001:10001
VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4)"]

# CMD (not ENTRYPOINT) so `docker run <image> inleverboks <command>` and the k8s
# initContainer can run CLI commands with the same image.
CMD ["python", "-m", "inleverboks.entrypoint"]
