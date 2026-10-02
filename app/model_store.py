"""Verify that model files for the configured language exist locally."""

import logging
from pathlib import Path

from .settings import Settings

log = logging.getLogger(__name__)

REQUIRED_FILES = ("encoder.onnx", "decoder.onnx", "joiner.onnx", "tokens.txt")


def ensure_model(settings: Settings) -> Path:
    path = settings.model_path
    missing = [f for f in REQUIRED_FILES if not (path / f).is_file()]
    if missing:
        raise RuntimeError(
            f"Model files {missing} missing in {path}. "
            f"Mount the models directory with: -v /path/to/models:/models:ro"
        )
    log.info("Using local model files in %s", path)
    return path
