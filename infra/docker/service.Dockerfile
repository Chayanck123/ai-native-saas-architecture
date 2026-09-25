# syntax=docker/dockerfile:1.7
#
# One image definition for every service (containerised microservices):
#
#   docker build -f infra/docker/service.Dockerfile \
#     --build-arg PACKAGE=knowledge-service --build-arg SOURCE_DIR=services/knowledge .
#
# The same image runs the HTTP API (`api`) or the background worker (`worker`),
# so each role can be deployed and scaled independently.

ARG PYTHON_VERSION=3.12

# --------------------------------------------------------------------------- #
# Build stage: resolve and install exactly this service's dependencies.
# --------------------------------------------------------------------------- #
FROM python:${PYTHON_VERSION}-slim-bookworm AS build
COPY --from=ghcr.io/astral-sh/uv:0.8.17 /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /src
ARG PACKAGE
ARG SOURCE_DIR

# 1) Third-party dependencies only: this layer is cached until uv.lock changes.
#    uv needs every workspace member's pyproject.toml to validate the lockfile.
COPY pyproject.toml uv.lock ./
COPY packages/platform-core/pyproject.toml packages/platform-core/
COPY packages/devstack/pyproject.toml packages/devstack/
COPY services/gateway/pyproject.toml services/gateway/
COPY services/knowledge/pyproject.toml services/knowledge/
COPY services/agents/pyproject.toml services/agents/
COPY services/billing/pyproject.toml services/billing/
COPY services/growth/pyproject.toml services/growth/
COPY services/serverless/pyproject.toml services/serverless/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-workspace --package "${PACKAGE}"

# 2) Our code: the shared kernel plus this one service, installed as wheels.
COPY packages/platform-core packages/platform-core
COPY ${SOURCE_DIR} ${SOURCE_DIR}
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --package "${PACKAGE}"

# --------------------------------------------------------------------------- #
# Runtime stage: no compilers, no uv, no source tree - just the virtualenv.
# --------------------------------------------------------------------------- #
FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime
ARG PACKAGE
ARG SCRIPT=${PACKAGE}

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app

COPY --from=build /opt/venv /opt/venv
RUN ln -s "/opt/venv/bin/${SCRIPT}" /usr/local/bin/service

USER 10001:10001
EXPOSE 8000
ENTRYPOINT ["service"]
CMD ["api", "--port", "8000"]
