"""
stackchan_bridge.py - Lightweight PC MCP bridge for unified ESP32-native AppGemini.

Zero Whisper, zero GEMINI_API_KEY needed on the PC:
- StackChan's AppGemini runs Gemini 3.8 Live natively on the ESP32-S3.
- This script attaches MCP mode via a heartbeat lease (`POST /mcp/attach`),
  listens for `escalate_to_engineer` tasks (`GET /mcp/escalation`), and hands
  them off to Antigravity.
- Supports `--say` (using Edge TTS / configured TTS) and `--wait-reply` (using
  Gemini Live's native inputTranscription directly from the robot).
- When this script exits cleanly or loses heartbeat, StackChan automatically
  returns to 1-avatar standalone GEMINI mode.

Usage:
    cd D:\\StackChan\\stackchan-mcp
    uv run python -u scripts/stackchan_bridge.py
    uv run python -u scripts/stackchan_bridge.py --context "Updated firmware"
    uv run python -u scripts/stackchan_bridge.py --wait-reply --say "Quieres que flashee ahora?"
"""

import argparse
import io
import signal
import sys
import threading
import time
import uuid
import wave
from contextlib import suppress
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mcp_server import audio_processing  # noqa: E402
from mcp_server.audio_server import (  # noqa: E402
    audio_url,
    register_live_stream,
    start_live_audio_server,
    unregister_live_stream,
)
from mcp_server.stackchan_client import StackchanClient  # noqa: E402
from mcp_server.stackchan_config import load_config  # noqa: E402

HEARTBEAT_INTERVAL_SEC = 6.0
LEASE_TTL_MS = 25000


def _wav_bytes_to_pcm16(wav_bytes: bytes) -> tuple[bytes, int]:
    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        return wf.readframes(wf.getnframes()), wf.getframerate()


def _wait_playback_end(client: StackchanClient, baseline_ms: int | None, timeout_sec: float = 25.0) -> None:
    t0 = time.monotonic()
    deadline = t0 + timeout_sec
    saw_playing = False
    while time.monotonic() < deadline:
        with suppress(Exception):
            pst = client.playback_status()
            if pst.get("playing"):
                saw_playing = True
            elif saw_playing or (baseline_ms is not None and pst.get("started_ms") not in (None, baseline_ms)):
                time.sleep(0.05)
                return
            elif time.monotonic() - t0 > 2.5:
                return
        time.sleep(0.06)


def speak_on_robot(
    client: StackchanClient,
    config,
    live_port: int,
    text: str,
    lang: str = "es",
    *,
    wait_reply: bool = False,
) -> None:
    if not text.strip():
        return
    wav_path = audio_processing.generate_tts(text, lang, config)
    stream_id = f"eng_{uuid.uuid4().hex[:10]}.wav"
    try:
        pcm, sr = _wav_bytes_to_pcm16(wav_path.read_bytes())
        q = register_live_stream(stream_id, sample_rate=sr)
        q.put(pcm)
        q.put(None)
        baseline = None
        with suppress(Exception):
            baseline = client.playback_status().get("started_ms")
        url = audio_url(config.mac_ip, live_port, f"live/{stream_id}")
        client.request(
            "post",
            f"{client.base_url}/play",
            json_body={"voice_url": url, "wait_reply": wait_reply, "subtitle": text.strip()},
            timeout=5.0,
        )
        _wait_playback_end(client, baseline)
    finally:
        wav_path.unlink(missing_ok=True)
        unregister_live_stream(stream_id)


def attach_lease(client: StackchanClient, *, mode: str = "", context: str = "", ttl_ms: int = LEASE_TTL_MS) -> bool:
    body: dict = {"ttl_ms": ttl_ms}
    if mode:
        body["mode"] = mode
    if context:
        body["context"] = context
    try:
        resp = client.request("post", f"{client.base_url}/mcp/attach", json_body=body, timeout=3.0)
        return 200 <= resp.status_code < 300
    except Exception:
        return False


def detach_lease(client: StackchanClient) -> None:
    with suppress(Exception):
        client.request("post", f"{client.base_url}/mcp/detach", json_body={}, timeout=2.0)


def main() -> None:
    parser = argparse.ArgumentParser(description="Lightweight PC MCP bridge for unified ESP32 AppGemini")
    parser.add_argument("--lang", default="es", help="TTS language for --say (default: es)")
    parser.add_argument("--context", default="", help="1-line handoff summary injected into ESP32 Gemini Live")
    parser.add_argument("--say", default="", help="Speak a message on StackChan using configured TTS (e.g. GonzaloNeural)")
    parser.add_argument("--say-only", action="store_true", help="Speak --say and exit without waiting")
    parser.add_argument("--wait-reply", action="store_true", help="Speak --say, then wait for one robot-transcribed reply")
    args = parser.parse_args()

    config = load_config()
    client = StackchanClient(config)
    live_port = start_live_audio_server()

    if args.say_only:
        attach_lease(client, mode="engineer", ttl_ms=30000)
        speak_on_robot(client, config, live_port, args.say, lang=args.lang, wait_reply=False)
        return

    stop_evt = threading.Event()
    keep_lease_on_exit = False

    def _heartbeat() -> None:
        while not stop_evt.wait(HEARTBEAT_INTERVAL_SEC):
            attach_lease(client, ttl_ms=LEASE_TTL_MS)

    def _on_signal(_sig, _frame) -> None:
        stop_evt.set()

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    hb_thread = threading.Thread(target=_heartbeat, daemon=True)
    hb_thread.start()

    try:
        if args.wait_reply:
            attach_lease(client, mode="engineer", ttl_ms=LEASE_TTL_MS)
            if args.say.strip():
                speak_on_robot(client, config, live_port, args.say, lang=args.lang, wait_reply=True)
            else:
                client.request("post", f"{client.base_url}/mcp/wait_reply", json_body={}, timeout=2.0)
            while not stop_evt.is_set():
                try:
                    res = client.request("get", f"{client.base_url}/mcp/reply", timeout=2.5).json()
                    if res.get("ready"):
                        reply = (res.get("text") or "").strip()
                        keep_lease_on_exit = True
                        print(f"\n[USER_REPLY ({args.lang})] {reply}", flush=True)
                        return
                except Exception:
                    time.sleep(0.4)
                time.sleep(0.2)
            return

        if args.say.strip():
            attach_lease(client, mode="engineer", ttl_ms=LEASE_TTL_MS)
            speak_on_robot(client, config, live_port, args.say, lang=args.lang, wait_reply=False)

        if not attach_lease(client, mode="live", context=args.context.strip(), ttl_ms=LEASE_TTL_MS):
            print("[stackchan_bridge] ERROR: Could not reach unified AppGemini at /mcp/attach.", flush=True)
            sys.exit(2)

        print(f"[stackchan_bridge] Attached to AppGemini at {client.base_url} (native ESP32 Gemini Live + MCP).", flush=True)

        while not stop_evt.is_set():
            try:
                esc = client.request("get", f"{client.base_url}/mcp/escalation", timeout=2.5).json()
                if esc.get("ready"):
                    task = (esc.get("task") or "User requested technical assistance").strip()
                    user_txt = (esc.get("user_text") or "").strip()
                    prev_u = (esc.get("prev_user") or "").strip()
                    prev_b = (esc.get("prev_bot") or "").strip()
                    keep_lease_on_exit = True
                    # Extend lease so robot stays in AGENT $ while Antigravity processes the task
                    attach_lease(client, mode="engineer", ttl_ms=60000)
                    if prev_u or prev_b:
                        print(f"\n[Prev Turn] User: {prev_u} | StackChan: {prev_b}", flush=True)
                    if user_txt:
                        print(f"[Last User ({args.lang})] {user_txt}", flush=True)
                    print(f"[ESCALATED] Task: {task}", flush=True)
                    return
            except Exception:
                time.sleep(0.5)
            time.sleep(0.25)
    finally:
        stop_evt.set()
        if not keep_lease_on_exit:
            detach_lease(client)
            print("[stackchan_bridge] Detached cleanly; StackChan returned to standalone GEMINI.", flush=True)


if __name__ == "__main__":
    main()
