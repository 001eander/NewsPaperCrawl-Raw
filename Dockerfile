# syntax=docker/dockerfile:1
# =====================================================================
# 浙图报纸爬虫
# =====================================================================

# ---------- 阶段 1: build ----------
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS build

WORKDIR /app

COPY pyproject.toml uv.lock ./
ENV UV_PROJECT_ENVIRONMENT=/opt/venv
RUN uv sync --no-dev --locked --no-install-project

# ---------- 阶段 2: runtime ----------
FROM python:3.12-slim-bookworm

ENV \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /app

COPY --from=build /opt/venv /opt/venv

COPY crawler/ crawler/
COPY cli.py .

ENTRYPOINT ["/opt/venv/bin/python", "cli.py"]
CMD ["--help"]
