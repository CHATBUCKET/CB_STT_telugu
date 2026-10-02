"""Entrypoint: `python -m app`. Models are fetched during app startup (see api.lifespan),
which uvicorn completes before it binds the port."""

import logging

import uvicorn

from .settings import settings

logging.basicConfig(level=settings.log_level.upper(),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")

uvicorn.run(
    "app.api:app",
    host=settings.host,
    port=settings.port,
    log_level=settings.log_level,
    ws_ping_interval=20,
    ws_ping_timeout=20,
    timeout_graceful_shutdown=30,
    access_log=False,
)
