# syntax=docker/dockerfile:1

# One image for both the bot and the worker: they share every line of code, and
# compose picks which entry point to run. Two images would only mean two builds.

# --- build stage -------------------------------------------------------------
# The runtime stage uses the same python:3.13-slim-bookworm base on purpose: the
# venv hard-codes the interpreter path, so a venv built against a different
# Python (uv's managed one, say) would not run over there.
FROM python:3.13-slim-bookworm AS builder

COPY --from=ghcr.io/astral-sh/uv:0.12.10 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies first and without the project itself, so editing src/ rebuilds
# only the two layers below instead of re-resolving and re-downloading the tree.
# --frozen makes uv.lock authoritative: it fails rather than quietly re-resolving.
#
# No compiler toolchain is installed, and none is needed: PyMuPDF 1.28 publishes
# cp310-abi3 manylinux_2_28 wheels for both x86_64 and aarch64, and abi3 covers
# 3.13. bookworm ships glibc 2.36, comfortably over the 2.28 floor. (Alpine would
# not work as painlessly — PyMuPDF has no musl wheel for aarch64.)
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src/ ./src/
RUN uv sync --frozen --no-dev


# --- runtime stage -----------------------------------------------------------
FROM python:3.13-slim-bookworm AS runtime

RUN useradd --create-home --uid 10001 app

WORKDIR /app

COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --from=builder --chown=app:app /app/src /app/src
COPY --chown=app:app pyproject.toml ./

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER app

# Overridden per service in docker-compose.yml; the worker is the sensible
# default because it is where all the real work happens.
CMD ["pdf-to-anki-worker"]
