#!/usr/bin/env python3
"""Smoke test: start service, send sample.wav, verify transcription response."""

import os, sys, time, json, signal, subprocess, urllib.request, pathlib, http.client

LANG     = "telugu"
PORT     = int(os.environ.get("S2T_PORT", 8000))
AUDIO    = pathlib.Path(__file__).parent / "sample.wav"
APP_DIR  = pathlib.Path(__file__).parents[1]
PYTHON   = sys.executable
TIMEOUT  = 40

def wait_ready(port, timeout):
    url = f"http://localhost:{port}/health"
    for _ in range(timeout):
        try:
            with urllib.request.urlopen(url, timeout=1) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(1)
    return False

def run():
    print(f"[TEST] Language : {LANG}")
    print(f"[TEST] Audio    : {AUDIO} ({AUDIO.stat().st_size // 1024} KB)")

    env = os.environ.copy()
    env["S2T_LANGUAGE"]  = LANG
    env["S2T_MODEL_DIR"] = str(APP_DIR / "models")

    proc = subprocess.Popen(
        [PYTHON, "-m", "app"],
        cwd=APP_DIR, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
    )

    print(f"[TEST] Server PID {proc.pid} — waiting up to {TIMEOUT}s ...")
    if not wait_ready(PORT, TIMEOUT):
        err = proc.stderr.read(1000).decode(errors="replace")
        proc.kill()
        print(f"[FAIL] Server did not start\n{err[:400]}")
        sys.exit(1)

    print("[TEST] Server ready ✓")

    boundary   = "----TestBoundary"
    audio_bytes = AUDIO.read_bytes()
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="files"; filename="sample.wav"\r\n'
        f"Content-Type: audio/wav\r\n\r\n"
    ).encode() + audio_bytes + f"\r\n--{boundary}--\r\n".encode()

    conn = http.client.HTTPConnection("localhost", PORT, timeout=30)
    conn.request("POST", "/v1/transcribe", body,
                 {"Content-Type": f"multipart/form-data; boundary={boundary}"})
    resp = conn.getresponse()
    data = json.loads(resp.read())
    conn.close()

    proc.send_signal(signal.SIGTERM)
    proc.wait()

    result = data["results"][0]
    text   = result.get("text", "").strip()
    dur    = result.get("duration", 0)

    print(f"[TEST] Duration : {dur:.1f}s")
    print(f"[TEST] Text     : {text or '(empty)'}")

    if resp.status != 200:
        print(f"[FAIL] HTTP {resp.status}")
        sys.exit(1)
    if not text:
        print("[WARN] Empty transcription — check models/{LANG}/ has .onnx files")
        sys.exit(2)

    print("[PASS] ✅ Service responded with transcription")

if __name__ == "__main__":
    run()
