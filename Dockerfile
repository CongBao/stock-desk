# syntax=docker/dockerfile:1.7

ARG NODE_VERSION=22.22.1
ARG PYTHON_VERSION=3.12.13
ARG UV_VERSION=0.11.8

FROM node:${NODE_VERSION}-bookworm-slim AS web-builder

ENV PNPM_HOME=/pnpm
ENV PATH="${PNPM_HOME}:${PATH}"

WORKDIR /build
RUN corepack enable && corepack prepare pnpm@11.7.0 --activate
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
COPY web/package.json ./web/package.json
RUN --mount=type=cache,id=pnpm,target=/pnpm/store \
    pnpm install --frozen-lockfile
COPY web ./web
RUN pnpm build

FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv-bin

FROM python:${PYTHON_VERSION}-slim-bookworm AS python-builder

ENV UV_COMPILE_BYTECODE=1
ENV UV_LINK_MODE=copy

WORKDIR /app
COPY --from=uv-bin /uv /uvx /bin/
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,id=uv,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project
COPY alembic.ini ./alembic.ini
COPY migrations ./migrations
COPY src ./src
RUN --mount=type=cache,id=uv,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime

RUN groupadd --gid 10001 stockdesk \
    && useradd --uid 10001 --gid 10001 --no-create-home \
        --home-dir /nonexistent --shell /usr/sbin/nologin stockdesk \
    && mkdir -p /app/data \
    && chown 10001:10001 /app/data

WORKDIR /app
COPY --from=python-builder --chown=10001:10001 /app/.venv /app/.venv
COPY --from=web-builder --chown=10001:10001 /build/web/dist /app/web-dist

ENV PATH="/app/.venv/bin:${PATH}"
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV STOCK_DESK_DATA_DIR=/app/data
ENV STOCK_DESK_DATABASE_URL=sqlite:////app/data/stock-desk.db
ENV STOCK_DESK_WEB_DIST_DIR=/app/web-dist

USER 10001:10001
EXPOSE 8000

CMD ["uvicorn", "stock_desk.main:app", "--host", "0.0.0.0", "--port", "8000"]
