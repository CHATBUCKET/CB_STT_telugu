"""Ensure model files for the configured language exist locally, pulling from GCS if needed.

Bucket layout:  gs://<bucket>/<prefix>/<language>/{encoder.onnx,decoder.onnx,joiner.onnx,tokens.txt}
Local layout:   <S2T_MODEL_DIR>/<language>/...
"""

import logging
from pathlib import Path

from .settings import Settings

log = logging.getLogger(__name__)

REQUIRED_FILES = ("encoder.onnx", "decoder.onnx", "joiner.onnx", "tokens.txt")


def _missing(path: Path) -> list[str]:
    return [f for f in REQUIRED_FILES if not (path / f).is_file()]


def _download(gcs_uri: str, language: str, dest: Path) -> None:
    from google.auth.exceptions import DefaultCredentialsError
    from google.cloud import storage

    bucket, _, prefix = gcs_uri.removeprefix("gs://").partition("/")
    prefix = "/".join(p for p in (prefix.strip("/"), language) if p) + "/"

    try:
        client = storage.Client()
    except DefaultCredentialsError:
        log.warning("No GCP credentials found; trying anonymous access to gs://%s", bucket)
        client = storage.Client.create_anonymous_client()

    blobs = [b for b in client.list_blobs(bucket, prefix=prefix) if not b.name.endswith("/")]
    if not blobs:
        raise RuntimeError(f"No model files found at gs://{bucket}/{prefix}")

    for blob in blobs:
        target = dest / blob.name[len(prefix):]
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(target.name + ".part")
        log.info("Downloading gs://%s/%s (%.1f MB)", bucket, blob.name, (blob.size or 0) / 1e6)
        blob.download_to_filename(partial)  # checksum-verified by the client
        partial.replace(target)             # atomic: never leave a half-written model


def ensure_model(settings: Settings) -> Path:
    path = settings.model_path
    if not _missing(path):
        log.info("Using local model files in %s", path)
        return path

    if not settings.model_gcs_uri:
        raise RuntimeError(
            f"Model files {_missing(path)} missing in {path} and S2T_MODEL_GCS_URI is not set"
        )

    _download(settings.model_gcs_uri, settings.language, path)
    if missing := _missing(path):
        raise RuntimeError(f"Downloaded model for '{settings.language}' is missing {missing}")
    return path
