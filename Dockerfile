# syntax=docker/dockerfile:1
FROM python:3.12-slim-bookworm

ARG PORCHLIGHT_VERSION=dev
LABEL org.opencontainers.image.source="https://github.com/tachyonFlux/porchlight-hoa" \
      org.opencontainers.image.version="${PORCHLIGHT_VERSION}"

COPY --from=ghcr.io/astral-sh/uv:0.12.0 /uv /usr/local/bin/uv

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:$PATH" \
    DJANGO_SETTINGS_MODULE=porchlight.settings \
    PORCHLIGHT_DATA_DIR=/data \
    PORCHLIGHT_VERSION=${PORCHLIGHT_VERSION}

# WeasyPrint (PDFs) needs Pango; fonts for invoices and reports.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz-subset0 \
        fonts-dejavu-core fonts-liberation shared-mime-info gosu \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project

COPY . .
RUN PORCHLIGHT_SECRET_KEY=collectstatic-only PORCHLIGHT_DATA_DIR=/tmp/build-data \
        python manage.py collectstatic --noinput \
    && rm -rf /tmp/build-data

VOLUME /data
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
    CMD ["python", "/app/docker/healthcheck.py"]
ENTRYPOINT ["/app/docker/entrypoint.sh"]
CMD ["web"]
