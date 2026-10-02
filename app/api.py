"""HTTP/WebSocket API.

  WS   /v1/stream       live streaming recognition
  POST /v1/transcribe   offline batch transcription (one or more files)
  GET  /health          liveness/readiness + load
  GET  /metrics         Prometheus
"""

import asyncio
import io
import json
import logging
from contextlib import asynccontextmanager

import numpy as np
import soundfile as sf
from fastapi import FastAPI, File, HTTPException, Query, UploadFile, WebSocket, WebSocketDisconnect
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


app = FastAPI(title="Speech-to-Text", version="1.0.0", lifespan=lifespan)
app.mount("/metrics", make_asgi_app())


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

@app.websocket("/v1/stream")
async def stream(ws: WebSocket, sample_rate: int = Query(16000, ge=8000, le=48000)):
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
