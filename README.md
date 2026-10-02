# S2T · Telugu (తెలుగు)

Streaming speech-to-text service for **Telugu** built on Zipformer2 (sherpa-onnx) + FastAPI.
One container, one language, two endpoints: live WebSocket streaming and offline batch transcription.

## Quick start

### Docker (recommended)

```bash
# Build
docker build -t s2t-telugu .

# Run — mount model files at runtime
docker run -d \
  -p 8000:8000 \
  -v $PWD/models:/models:ro \
  --name s2t-telugu \
  s2t-telugu
```

### Without Docker

```bash
pip install -r requirements.txt
python -m app          # reads ./models/telugu/
```

## Model files

Place the four model files in `models/telugu/`:

```
models/
  telugu/
    encoder.onnx
    decoder.onnx
    joiner.onnx
    tokens.txt
```

> Model binaries are not committed to this repo. Obtain them separately and mount or place them locally.

## API

### Live streaming — `WS /v1/stream?sample_rate=16000`

Send 16-bit little-endian mono PCM frames (~100 ms each). Send text frame `eof` to flush.

```jsonc
{"type": "ready",   "session": "…", "language": "telugu"}
{"type": "partial", "text": "…"}
{"type": "final",   "text": "…", "start": 0.0, "end": 2.1}
{"type": "done",    "text": "…", "segments": […]}
```

### Offline batch — `POST /v1/transcribe`

```bash
curl -F files=@audio.wav http://localhost:8000/v1/transcribe
```

```json
{
  "language": "telugu",
  "results": [
    {"filename": "audio.wav", "duration": 10.5, "text": "…",
     "segments": [{"text": "…", "start": 0.0, "end": 10.5}]}
  ]
}
```

Accepts WAV, FLAC, OGG, MP3 at any sample rate (mono or stereo).

### Ops

```bash
curl http://localhost:8000/health    # liveness + readiness
curl http://localhost:8000/metrics  # Prometheus
```

## Configuration

| Env var | Default | Description |
|---|---|---|
| `S2T_LANGUAGE` | `telugu` | model folder name |
| `S2T_MODEL_DIR` | `models` (`/models` in Docker) | local model root |
| `S2T_MODEL_GCS_URI` | – | `gs://bucket/prefix` — download model from GCS at startup |
| `S2T_DECODE_WORKERS` | `2` | set to container vCPU count |
| `S2T_MAX_STREAMS` | `40` | max concurrent live streams |
| `S2T_MAX_OFFLINE` | `8` | max concurrent offline files |
| `S2T_PROVIDER` | `cpu` | `cuda` requires a CUDA sherpa-onnx build |

## Health check

Docker polls `GET /health` every 15 s (60 s start grace). Check container health:

```bash
docker inspect --format='{{.State.Health.Status}}' s2t-telugu
# healthy
```
