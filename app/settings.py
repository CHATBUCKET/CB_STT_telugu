"""Service configuration, read once from environment variables (prefix S2T_)."""

import os
from dataclasses import dataclass
from pathlib import Path


def _env(name: str, default, cast=str):
    value = os.environ.get(f"S2T_{name}")
    return cast(default if value in (None, "") else value)


@dataclass(frozen=True)
class Settings:
    # Model
    language: str = _env("LANGUAGE", "telugu")
    model_dir: Path = _env("MODEL_DIR", "models", Path)
    # gs://bucket/prefix — models are read from gs://bucket/prefix/<language>/
    model_gcs_uri: str = _env("MODEL_GCS_URI", "")

    # Inference
    provider: str = _env("PROVIDER", "cpu")                  # cpu | cuda
    num_threads: int = _env("NUM_THREADS", 1, int)           # ONNX intra-op threads per decode call
    decode_workers: int = _env("DECODE_WORKERS", 2, int)     # parallel batched-decode loops
    max_batch: int = _env("MAX_BATCH", 32, int)              # streams per decode call
    decoding_method: str = _env("DECODING_METHOD", "greedy_search")

    # Endpointing (utterance segmentation)
    rule1_silence: float = _env("RULE1_SILENCE", 2.4, float)
    rule2_silence: float = _env("RULE2_SILENCE", 1.4, float)
    rule3_max_utterance: float = _env("RULE3_MAX_UTTERANCE", 20.0, float)

    # Capacity / limits (per container)
    max_streams: int = _env("MAX_STREAMS", 40, int)              # live websocket sessions
    max_offline: int = _env("MAX_OFFLINE", 8, int)               # files decoded concurrently
    max_offline_queue: int = _env("MAX_OFFLINE_QUEUE", 256, int) # files waiting for a slot
    max_upload_mb: int = _env("MAX_UPLOAD_MB", 100, int)
    max_files: int = _env("MAX_FILES", 50, int)                  # files per batch request
    stream_idle_timeout: float = _env("STREAM_IDLE_TIMEOUT", 30.0, float)
    max_backlog_seconds: float = _env("MAX_BACKLOG_SECONDS", 30.0, float)

    # Server
    host: str = _env("HOST", "0.0.0.0")
    port: int = _env("PORT", 8000, int)
    log_level: str = _env("LOG_LEVEL", "info")

    @property
    def model_path(self) -> Path:
        return self.model_dir / self.language


settings = Settings()
