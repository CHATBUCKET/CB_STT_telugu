# ── Stage 1: build ───────────────────────────────────────────────────────────
FROM python:3.12-slim AS build

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build
COPY requirements.txt .
RUN pip install --prefix=/deps -r requirements.txt

# ── Stage 2: runtime ─────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Pull only installed packages from build stage
COPY --from=build /deps /usr/local

WORKDIR /srv
COPY app ./app

# Non-root user; /models is the mount point for model files
RUN useradd --uid 10001 --create-home --no-log-init s2t \
 && mkdir /models && chown s2t /models

USER s2t

ENV S2T_LANGUAGE=telugu \
    S2T_MODEL_DIR=/models \
    S2T_PORT=6008

EXPOSE 6008

# Health check hits GET /health (liveness + readiness)
HEALTHCHECK --interval=15s --timeout=3s --start-period=60s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:6008/health', timeout=2)"

CMD ["python", "-m", "app"]
