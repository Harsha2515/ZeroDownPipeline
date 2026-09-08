# ---------------------------------------------------------------------------
# Multi-stage build.
#
# Stage 1 installs dependencies into a throwaway layer that carries the build
# toolchain. Stage 2 copies only the installed packages, so the shipped image
# has no compilers in it - smaller, and a smaller attack surface.
# ---------------------------------------------------------------------------
FROM python:3.11-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build
COPY app/requirements.txt .
RUN pip install --prefix=/install -r requirements.txt


FROM python:3.11-slim AS runtime

# APP_VERSION is the git short SHA, injected at build time. It is what /health
# reports, and it is how the pipeline proves which build is actually live.
ARG APP_VERSION=dev
ENV APP_VERSION=${APP_VERSION} \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    APP_PORT=8000

RUN groupadd --system --gid 1001 appuser \
 && useradd --system --uid 1001 --gid appuser --create-home appuser \
 && apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

COPY --from=builder /install /usr/local

WORKDIR /srv
COPY --chown=appuser:appuser app/ ./app/

USER appuser
EXPOSE 8000

# Docker's own health status. The pipeline does not rely on this - it polls
# /health over HTTP itself - but it makes `docker ps` tell the truth.
HEALTHCHECK --interval=10s --timeout=3s --start-period=15s --retries=3 \
    CMD curl -fsS "http://localhost:${APP_PORT}/health" || exit 1

CMD ["sh", "-c", "exec gunicorn --bind 0.0.0.0:${APP_PORT} --workers 2 --threads 4 --timeout 30 --access-logfile - --error-logfile - 'app.main:create_app()'"]
