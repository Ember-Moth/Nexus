# syntax=docker/dockerfile:1.12
# Management API and Worker: `nexus api`, `nexus worker`, `nexus migrate` and the other
# `nexus` commands. Build from the repository root:
#   docker build -f deploy/images/control-plane.Dockerfile -t nexus-control-plane .
# Base images are pinned by tag and multi-arch index digest; update both together.
ARG PYTHON_IMAGE=python:3.14.7-slim-trixie@sha256:51dafde81dbdb6ebde285137a295cf18a47ca95234fe388a343719cb97305b3d
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.13.6@sha256:af22a52f5eafef3d78e551599d405c02df61919d18028f0cea8eda93bc9986d0

FROM ${UV_IMAGE} AS uv

FROM ${PYTHON_IMAGE} AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/nexus
WORKDIR /src
# Dependencies first, from the lock file only, so source edits reuse this layer.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project
COPY README.md alembic.ini ./
COPY migrations migrations
COPY src/nexus src/nexus
# A regular (non-editable) install: the wheel carries the migrations and alembic.ini.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

FROM ${PYTHON_IMAGE}
LABEL org.opencontainers.image.title="nexus-control-plane" \
      org.opencontainers.image.description="Nexus management API, Worker and migrations" \
      org.opencontainers.image.source="https://github.com/herta-network/Nexus"
COPY --from=build /opt/nexus /opt/nexus
ENV PATH=/opt/nexus/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
# Bytecode is compiled at build time, so the root filesystem can be read-only.
USER 65532:65532
WORKDIR /tmp
EXPOSE 8000
ENTRYPOINT ["nexus"]
CMD ["api"]
