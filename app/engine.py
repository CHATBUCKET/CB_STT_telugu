"""Batched streaming ASR engine on top of sherpa-onnx.

Every request (live websocket or offline file) is a Session owning one sherpa-onnx
OnlineStream. Each DecodeWorker thread owns a shard of sessions and loops:

    1. move pending audio into streams
    2. decode all ready streams in batched decode_streams() calls
    3. emit partial/final results, finish sessions whose input has ended

sherpa-onnx releases the GIL while decoding, so workers run in parallel with each
other and with the asyncio event loop. Events reach the handler's loop through
loop.call_soon_threadsafe, so request handlers never touch a stream directly.
"""

import logging
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Callable

import numpy as np
import sherpa_onnx
from prometheus_client import Counter, Gauge, Histogram

from .settings import Settings

log = logging.getLogger(__name__)

TAIL_PADDING_SECONDS = 0.66  # flushes the encoder's right context at end of input

ACTIVE = Gauge("s2t_active_sessions", "Active sessions", ["mode"])
SESSIONS = Counter("s2t_sessions_total", "Sessions opened", ["mode"])
REJECTED = Counter("s2t_rejected_total", "Requests rejected at capacity", ["mode"])
AUDIO = Counter("s2t_audio_seconds_total", "Audio seconds ingested", ["mode"])
BATCH_SIZE = Histogram("s2t_batch_size", "Streams per decode call",
                       buckets=(1, 2, 4, 8, 16, 32, 64, 128))
LOOP_SECONDS = Histogram(
    "s2t_loop_seconds",
    "Scheduler iteration time; sustained values above ~0.3s mean streams fall behind real time",
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.2, 0.3, 0.5, 1.0, 2.0),
)


class Session:
    def __init__(self, stream, mode: str, sample_rate: int, emit: Callable[[dict], None],
                 partials: bool):
        self.id = uuid.uuid4().hex[:12]
        self.stream = stream
        self.mode = mode                      # "stream" | "offline"
        self.sample_rate = sample_rate
        self.partials = partials
        self._emit = emit
        self._lock = threading.Lock()
        self._pending: deque[np.ndarray] = deque()
        self._pending_samples = 0
        self._eof = False
        self.closed = False                   # worker drops the session once set
        self.released = False                 # capacity slot returned (Engine.close)
        # Worker-owned state
        self.done = False
        self.input_finished = False
        self.last_partial = ""
        self.segments: list[dict] = []

    # -- handler side -----------------------------------------------------
    def feed(self, samples: np.ndarray) -> float:
        """Queue audio; returns the backlog in seconds not yet consumed by the decoder."""
        with self._lock:
            self._pending.append(samples)
            self._pending_samples += len(samples)
            return self._pending_samples / self.sample_rate

    def finish(self) -> None:
        with self._lock:
            self._eof = True

    # -- worker side ------------------------------------------------------
    def pop_chunk(self) -> np.ndarray | None:
        with self._lock:
            if not self._pending:
                return None
            chunk = self._pending.popleft()
            self._pending_samples -= len(chunk)
            return chunk

    @property
    def eof_reached(self) -> bool:
        with self._lock:
            return self._eof and not self._pending

    def emit(self, event: dict) -> None:
        try:
            self._emit(event)
        except RuntimeError:  # handler's event loop is gone
            self.closed = True


class DecodeWorker:
    def __init__(self, recognizer, max_batch: int, index: int):
        self.rec = recognizer
        self.max_batch = max_batch
        self._sessions: list[Session] = []
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._running = True
        self._thread = threading.Thread(target=self._run, name=f"decode-{index}", daemon=True)
        self._thread.start()

    @property
    def load(self) -> int:
        return len(self._sessions)

    def add(self, session: Session) -> None:
        with self._lock:
            self._sessions.append(session)
        self._wake.set()

    def stop(self) -> None:
        self._running = False
        self._wake.set()
        self._thread.join(timeout=5)

    def _run(self) -> None:
        rec = self.rec
        while self._running:
            self._wake.wait(timeout=0.02)
            self._wake.clear()

            with self._lock:
                self._sessions = [s for s in self._sessions if not (s.closed or s.done)]
                sessions = list(self._sessions)
            if not sessions:
                continue

            started = time.perf_counter()
            for s in sessions:
                self._feed(s)

            # Live streams first so they get the earliest batches.
            ready = [s for s in sessions if rec.is_ready(s.stream)]
            ready.sort(key=lambda s: s.mode != "stream")
            for i in range(0, len(ready), self.max_batch):
                batch = ready[i:i + self.max_batch]
                rec.decode_streams([s.stream for s in batch])
                BATCH_SIZE.observe(len(batch))

            for s in ready:
                self._collect(s)
            for s in sessions:
                if s.input_finished and not s.done and not rec.is_ready(s.stream):
                    self._complete(s)

            LOOP_SECONDS.observe(time.perf_counter() - started)
            if ready:
                self._wake.set()

    def _feed(self, s: Session) -> None:
        # Feed only until the stream has a chunk to decode: bounds memory for long
        # files and gives every session one chunk per iteration (fairness).
        while not self.rec.is_ready(s.stream):
            chunk = s.pop_chunk()
            if chunk is None:
                break
            s.stream.accept_waveform(s.sample_rate, chunk)
            AUDIO.labels(s.mode).inc(len(chunk) / s.sample_rate)
        if not s.input_finished and s.eof_reached:
            tail = np.zeros(int(TAIL_PADDING_SECONDS * s.sample_rate), dtype=np.float32)
            s.stream.accept_waveform(s.sample_rate, tail)
            s.stream.input_finished()
            s.input_finished = True

    def _collect(self, s: Session) -> None:
        rec = self.rec
        text = rec.get_result(s.stream)
        if s.partials and text and text != s.last_partial:
            s.last_partial = text
            s.emit({"type": "partial", "text": text})
        if rec.is_endpoint(s.stream) and not s.input_finished:
            if text:
                self._final(s)
            rec.reset(s.stream)
            s.last_partial = ""

    def _final(self, s: Session) -> None:
        result = self.rec.get_result_all(s.stream)
        text = result.text.strip()
        start = round(result.start_time, 2)
        end = round(start + (result.timestamps[-1] if result.timestamps else 0.0), 2)
        segment = {"text": text, "start": start, "end": end}
        s.segments.append(segment)
        s.emit({"type": "final", **segment})

    def _complete(self, s: Session) -> None:
        if self.rec.get_result(s.stream):
            self._final(s)
        s.done = True
        s.emit({
            "type": "done",
            "text": " ".join(seg["text"] for seg in s.segments),
            "segments": s.segments,
        })


class Engine:
    """Owns the recognizer, the decode workers and live-stream capacity accounting.

    open()/close() are called only from the asyncio event loop thread.
    """

    def __init__(self, settings: Settings, model_path: Path):
        self.settings = settings
        started = time.perf_counter()
        self.recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(
            tokens=str(model_path / "tokens.txt"),
            encoder=str(model_path / "encoder.onnx"),
            decoder=str(model_path / "decoder.onnx"),
            joiner=str(model_path / "joiner.onnx"),
            model_type="zipformer2",
            num_threads=settings.num_threads,
            provider=settings.provider,
            decoding_method=settings.decoding_method,
            enable_endpoint_detection=True,
            rule1_min_trailing_silence=settings.rule1_silence,
            rule2_min_trailing_silence=settings.rule2_silence,
            rule3_min_utterance_length=settings.rule3_max_utterance,
        )
        log.info("Loaded '%s' model from %s in %.1fs (provider=%s)", settings.language,
                 model_path, time.perf_counter() - started, settings.provider)
        self.workers = [DecodeWorker(self.recognizer, settings.max_batch, i)
                        for i in range(settings.decode_workers)]
        self.active = {"stream": 0, "offline": 0}

    def open(self, mode: str, sample_rate: int, emit: Callable[[dict], None]) -> Session | None:
        if mode == "stream" and self.active[mode] >= self.settings.max_streams:
            REJECTED.labels(mode).inc()
            return None
        session = Session(self.recognizer.create_stream(), mode, sample_rate, emit,
                          partials=(mode == "stream"))
        min(self.workers, key=lambda w: w.load).add(session)
        self.active[mode] += 1
        ACTIVE.labels(mode).inc()
        SESSIONS.labels(mode).inc()
        return session

    def close(self, session: Session) -> None:
        if session.released:
            return
        session.released = session.closed = True
        self.active[session.mode] -= 1
        ACTIVE.labels(session.mode).dec()

    def shutdown(self) -> None:
        for worker in self.workers:
            worker.stop()
