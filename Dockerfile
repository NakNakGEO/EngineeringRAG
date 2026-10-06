# syntax=docker/dockerfile:1
# One parameterised image recipe for the three Engineering OS processes.
#   docker build --build-arg APP=eios-api -t eios-api .
# APP is the uv workspace package to install (eios-api | eios-worker | eios-mcp).

ARG PYTHON_VERSION=3.12

FROM python:${PYTHON_VERSION}-slim AS builder
ARG APP
ARG UV_VERSION=0.11.32
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never PIP_NO_CACHE_DIR=1
# Optional build secret `extra_ca`: a full PEM CA bundle for networks that inspect TLS (corporate
# proxies). Empty/absent by default, in which case this is a no-op. It is mounted only for the
# duration of the RUN step and is never written into an image layer. See docs/development.md.
RUN --mount=type=secret,id=extra_ca \
    if [ -s /run/secrets/extra_ca ]; then \
      export SSL_CERT_FILE=/run/secrets/extra_ca PIP_CERT=/run/secrets/extra_ca; \
    fi \
 && pip install "uv==${UV_VERSION}"
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY packages ./packages
COPY apps ./apps
# Non-editable, locked, no dev tools: the runtime venv contains only what the service needs.
RUN --mount=type=secret,id=extra_ca \
    if [ -s /run/secrets/extra_ca ]; then \
      export SSL_CERT_FILE=/run/secrets/extra_ca REQUESTS_CA_BUNDLE=/run/secrets/extra_ca; \
    fi \
 && uv sync --frozen --no-dev --no-editable --package "${APP}"

FROM python:${PYTHON_VERSION}-slim AS runtime
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH"
RUN groupadd --system --gid 10001 eios \
 && useradd --system --uid 10001 --gid eios --no-create-home --shell /usr/sbin/nologin eios
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
# Migration files ship in every image so the one-shot `migrate` service can reuse the API image.
COPY alembic.ini ./
COPY migrations ./migrations
USER eios
