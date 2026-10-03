"""HTTP/WebSocket API.

  WS   /v1/stream       live streaming recognition
  POST /v1/transcribe   offline batch transcription (one or more files)
  GET  /health          liveness/readiness + load
  GET  /metrics         Prometheus
"""

import asyncio
import base64
import hashlib
import hmac
import io
import json
import logging
import re
import time
from contextlib import asynccontextmanager

import numpy as np
import soundfile as sf
from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from prometheus_client import make_asgi_app

from .engine import REJECTED, Engine, Session
from .model_store import ensure_model
from .settings import settings

log = logging.getLogger(__name__)

OFFLINE_CHUNK_SECONDS = 0.5


@asynccontextmanager
async def lifespan(app: FastAPI):
    model_path = await asyncio.to_thread(ensure_model, settings)
    app.state.engine = Engine(settings, model_path)
    app.state.offline_slots = asyncio.Semaphore(settings.max_offline)
    app.state.offline_waiting = 0
    yield
    app.state.engine.shutdown()


# No interactive docs or OpenAPI schema: the API is documented in README.md and
# there is no reason to advertise its surface to the internet.
app = FastAPI(title="Speech-to-Text", version="1.0.0", lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)
# Prometheus scrape endpoint. Not reachable through the public load balancer
# (its Cloud Armor policy only admits /v1/stream, /v1/transcribe and /health).
app.mount("/metrics", make_asgi_app())


# Checked before the upload body is read, so an unauthenticated client can't
# make the server receive a 30 MB file. Registered before CORS, so CORS stays
# the outer layer and browsers can read the 401.
@app.middleware("http")
async def require_token(request: Request, call_next):
    if request.url.path == "/v1/transcribe" and request.method == "POST":
        if not _token_valid(request.headers.get("authorization", "").removeprefix("Bearer ")):
            return JSONResponse({"detail": "missing or invalid token"}, status_code=401,
                                headers={"WWW-Authenticate": "Bearer"})
    return await call_next(request)


_allowed_origin = re.compile(settings.cors_origin_regex) if settings.cors_origin_regex else None
if _allowed_origin:
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=settings.cors_origin_regex,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
        max_age=600,
    )


def _origin_allowed(ws: WebSocket) -> bool:
    """Browsers always send Origin on a WebSocket handshake and CORS does not
    apply to WebSockets, so a cross-site page could otherwise open a stream.
    Non-browser clients (no Origin header) are allowed."""
    origin = ws.headers.get("origin")
    return _allowed_origin is None or origin is None or bool(_allowed_origin.fullmatch(origin))


def _b64decode(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def _token_valid(token: str | None) -> bool:
    """User token issued by cb-backend-nest: an HS256 JWT signed with
    S2T_TOKEN_KEY, with aud "stt" and an exp in the future (issued for a few
    minutes). Always valid when no key is configured (local use)."""
    if not settings.token_key:
        return True
    try:
        header, payload, signature = token.split(".")
        expected = hmac.new(settings.token_key.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(_b64decode(signature), expected):
            return False
        if json.loads(_b64decode(header)).get("alg") != "HS256":
            return False
        claims = json.loads(_b64decode(payload))
        return claims.get("aud") == "stt" and float(claims["exp"]) > time.time()
    except Exception:  # missing, malformed or wrongly typed token
        return False


def _emitter(queue: asyncio.Queue):
    loop = asyncio.get_running_loop()
    return lambda event: loop.call_soon_threadsafe(queue.put_nowait, event)


@app.get("/health")
async def health():
    engine: Engine = app.state.engine
    return {
        "status": "ok",
        "language": settings.language,
        "active_streams": engine.active["stream"],
        "max_streams": settings.max_streams,
        "active_offline": engine.active["offline"],
        "queued_offline": app.state.offline_waiting,
    }


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------
# Client -> server: binary frames of 16-bit little-endian mono PCM at `sample_rate`,
#                   then the text frame "eof" to flush (or just disconnect).
# Server -> client: {"type":"ready"} | {"type":"partial","text"}
#                   | {"type":"final","text","start","end"} | {"type":"done","text","segments"}
#                   | {"type":"error","error"}

# Browsers can't set headers on a WebSocket, so the token is a query parameter.
@app.websocket("/v1/stream")
async def stream(ws: WebSocket, sample_rate: int = Query(16000, ge=8000, le=48000),
                 token: str | None = Query(None)):
    if not _origin_allowed(ws) or not _token_valid(token):
        await ws.close(code=1008)  # policy violation; rejects the handshake with 403
        return
    await ws.accept()
    engine: Engine = app.state.engine
    events: asyncio.Queue = asyncio.Queue()
    session = engine.open("stream", sample_rate, _emitter(events))
    if session is None:
        await ws.send_json({"type": "error", "error": "server at capacity, retry later"})
        await ws.close(code=1013)
        return

    async def send_events():
        while True:
            event = await events.get()
            await ws.send_text(json.dumps(event, ensure_ascii=False))
            if event["type"] == "done":
                return

    sender = asyncio.create_task(send_events())
    try:
        await ws.send_json({"type": "ready", "session": session.id, "language": settings.language})
        while True:
            msg = await asyncio.wait_for(ws.receive(), timeout=settings.stream_idle_timeout)
            if msg["type"] == "websocket.disconnect":
                break
            if data := msg.get("bytes"):
                pcm = np.frombuffer(data, dtype="<i2", count=len(data) // 2)
                backlog = session.feed(pcm.astype(np.float32) / 32768.0)
                if backlog > settings.max_backlog_seconds:
                    await ws.send_json({"type": "error", "error": "audio sent faster than it can be decoded"})
                    await ws.close(code=1013)
                    break
            elif msg.get("text") == "eof":
                session.finish()
                await sender
                await ws.close()
                break
    except asyncio.TimeoutError:
        await ws.close(code=1000, reason="idle timeout")
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("stream %s failed", session.id)
    finally:
        sender.cancel()
        engine.close(session)


# ---------------------------------------------------------------------------
# Offline batch
# ---------------------------------------------------------------------------

def _decode_audio(raw: bytes) -> tuple[np.ndarray, int]:
    samples, sample_rate = sf.read(io.BytesIO(raw), dtype="float32", always_2d=True)
    return samples.mean(axis=1), sample_rate


async def _transcribe(samples: np.ndarray, sample_rate: int) -> dict:
    engine: Engine = app.state.engine
    state = app.state
    state.offline_waiting += 1
    try:
        await state.offline_slots.acquire()
    finally:
        state.offline_waiting -= 1

    events: asyncio.Queue = asyncio.Queue()
    session: Session = engine.open("offline", sample_rate, _emitter(events))
    try:
        step = int(OFFLINE_CHUNK_SECONDS * sample_rate)
        for i in range(0, len(samples), step):
            session.feed(samples[i:i + step])
        session.finish()
        while (event := await events.get())["type"] != "done":
            pass
        return {"text": event["text"], "segments": event["segments"]}
    finally:
        engine.close(session)
        state.offline_slots.release()


async def _transcribe_file(file: UploadFile, max_bytes: int) -> dict:
    result = {"filename": file.filename}
    raw = await file.read(max_bytes + 1)
    if len(raw) > max_bytes:
        return result | {"error": f"file exceeds {settings.max_upload_mb} MB"}
    try:
        samples, sample_rate = await asyncio.to_thread(_decode_audio, raw)
    except Exception as exc:
        return result | {"error": f"could not decode audio: {exc}"}
    del raw
    result["duration"] = round(len(samples) / sample_rate, 2)
    return result | await _transcribe(samples, sample_rate)


@app.post("/v1/transcribe")
async def transcribe(files: list[UploadFile] = File(..., description="One or more audio files")):
    """Transcribe audio files (WAV/FLAC/OGG/MP3, any sample rate, mono or stereo).

    Files in a request are decoded concurrently; each gets its own result or error.
    """
    if len(files) > settings.max_files:
        raise HTTPException(413, f"at most {settings.max_files} files per request")
    queued = app.state.offline_waiting + app.state.engine.active["offline"]
    if queued + len(files) > settings.max_offline + settings.max_offline_queue:
        REJECTED.labels("offline").inc()
        raise HTTPException(429, "offline queue is full, retry later", headers={"Retry-After": "5"})

    max_bytes = settings.max_upload_mb * 1024 * 1024
    results = await asyncio.gather(*(_transcribe_file(f, max_bytes) for f in files))
    return {"language": settings.language, "results": results}
