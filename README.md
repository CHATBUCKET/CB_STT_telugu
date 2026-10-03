# S2T · Telugu (తెలుగు)

Streaming speech-to-text service for **Telugu** built on Zipformer2 (sherpa-onnx) + FastAPI.
One container, one language, two endpoints: live WebSocket streaming and offline batch transcription.

## Quick start

### Docker (recommended)

```bash
# Build
docker build -t s2t-telugu .

# Run — the model is baked into the image; same hardening as production
docker run -d \
  -p 6008:6008 \
  --read-only --cap-drop=ALL --security-opt no-new-privileges \
  --name s2t-telugu \
  s2t-telugu
```

The image is a two-stage build: packages are installed on `python:3.13-slim-trixie`
and copied onto distroless `python3-debian13` (no shell, no package manager, no
pip, non-root uid 65532). About 210 MB plus the 72 MB model, versus 309 MB
without the model for the previous `python:3.12-slim` image.

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

The Docker build copies `models/telugu/` into the image at `/models/telugu`.

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
curl -F files=@audio.wav http://localhost:6008/v1/transcribe
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
curl http://localhost:6008/health    # liveness + readiness
curl http://localhost:6008/metrics  # Prometheus
```

## Configuration

| Env var | Default | Description |
|---|---|---|
| `S2T_LANGUAGE` | `telugu` | model folder name |
| `S2T_MODEL_DIR` | `models` (`/models` in Docker) | local model root |
| `S2T_DECODE_WORKERS` | `2` | set to container vCPU count |
| `S2T_MAX_STREAMS` | `40` | max concurrent live streams |
| `S2T_MAX_OFFLINE` | `8` | max concurrent offline files |
| `S2T_PROVIDER` | `cpu` | `cuda` requires a CUDA sherpa-onnx build |
| `S2T_PORT` | `8000` (`6008` in Docker) | listen port (Cloud Run sets `8080`) |
| `S2T_MAX_UPLOAD_MB` | `100` | per file; Cloud Run caps a whole request at 32 MB, so production uses `30` |
| `S2T_CORS_ORIGIN_REGEX` | empty | browser origins allowed (CORS + WebSocket `Origin` check), full-match regex; empty disables both |
| `S2T_TOKEN_KEY` | empty | HMAC key of the user tokens; set = every API call needs a token (production: Secret Manager `prod-stt-token-key`) |

`/docs`, `/redoc` and `/openapi.json` are disabled.

## Authentication

When `S2T_TOKEN_KEY` is set, `/v1/transcribe` and `/v1/stream` require a user
token; `/health` stays open. The token is an HS256 JWT that **cb-backend-nest**
issues to a logged-in user, signed with the same key:

```json
{"alg": "HS256", "typ": "JWT"}
{"sub": "<user id>", "aud": "stt", "exp": <now + 300>}
```

- Upload: `Authorization: Bearer <token>` (otherwise `401`).
- WebSocket: `wss://…/stt-telugu/v1/stream?token=<token>` (browsers can't set
  headers on a WebSocket; otherwise the handshake gets `403`).

Keep tokens short-lived (5 minutes): the WebSocket token ends up in URLs.

## Deployment

Production runs on Cloud Run as `stt-telugu`, behind
`https://stt-agent.chatbucket.chat/stt-telugu` (global HTTPS load balancer +
Cloud Armor WAF, Google-managed certificate). The infrastructure is Terraform in
[gke-infra-terraform](https://github.com/nandak99-coin/gke-infra-terraform)
(`modules/stt-cloudrun`, `envs/prod/stt.tf`); only `/stt-telugu/v1/stream`,
`/stt-telugu/v1/transcribe` and `/stt-telugu/health` are reachable, and only with a
user token (see Authentication).

**GCP Build, Push & Deploy** (one workflow, `develop` only - nothing is
deployed from `main`):

- every push and pull request: build, smoke test (read-only container,
  transcribes `tests/sample.wav`), SBOM with Syft (kept as a run artifact) and
  a Grype scan of it (fails on fixable CRITICAL vulnerabilities);
- push to `develop`: push the image as `develop-<short sha>`, then deploy it
  (`production` environment). Traffic moves only once the new revision passes
  its startup probe, and goes back to the previous revision if the public
  health check fails.

```
wss://stt-agent.chatbucket.chat/stt-telugu/v1/stream?sample_rate=16000&token=$TOKEN
curl -H "Authorization: Bearer $TOKEN" -F files=@audio.wav https://stt-agent.chatbucket.chat/stt-telugu/v1/transcribe
```

## Health check

`GET /health` (liveness + readiness). Cloud Run probes it; locally use `make health`.
