"""
stackchan_live.py - Primary Gemini Live API voice bridge for StackChan (with auto-fallback).

Runs Gemini 3.8 Live (`gemini-3.8-live`) as the primary voice engine using native
audio-in / audio-out over WebSockets + live HTTP/1.1 chunked WAV streaming to ESP32-S3.
If Gemini Live fails to connect or encounters a runtime/quota error, it automatically
falls back to `scripts/stackchan_chat.py` (`FREE 3.5 LITE` — local Faster-Whisper +
Gemini 3.5 Flash-Lite + local Piper/Edge TTS) so voice chat never interrupts.

Usage:
    cd D:\\StackChan\\stackchan-mcp
    uv run python -u scripts/stackchan_live.py --lang auto
"""

import argparse
import asyncio
import io
import os
import queue
import subprocess
import sys
import time
import uuid
import wave
from contextlib import suppress
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from google import genai  # noqa: E402
from google.genai import types  # noqa: E402

from mcp_server.audio_server import (  # noqa: E402
    AUDIO_DIR,
    audio_url,
    register_live_stream,
    start_live_audio_server,
    unregister_live_stream,
)
from mcp_server.stackchan_client import StackchanClient  # noqa: E402
from mcp_server.stackchan_config import load_config  # noqa: E402

DEFAULT_LIVE_MODEL = "gemini-3.8-live"

LIVE_SYSTEM_INSTRUCTION = (
    "You are StackChan, a friendly desktop robot in real-time voice chat. "
    "Keep replies concise (1-2 short natural sentences) in the user's spoken language, "
    "staying in the active conversation language unless the user explicitly asks to switch. "
    "TWO request types:\n"
    "1. GENERAL (answer yourself): casual conversation, stable knowledge, opinions, jokes.\n"
    "2. ESCALATE IMMEDIATELY: code edits, file/log inspection, git/build/flash commands, "
    "camera/hardware diagnostics, or live/real-time/post-cutoff web facts you cannot verify. "
    "To escalate, IMMEDIATELY call `escalate_to_engineer` with a concise English summary. "
    "If the user says goodbye / stop / terminamos / chao, call `end_chat`."
)


def detect_os_language(default: str = "en") -> str:
    """Detect the operating system UI language as a 2-letter ISO code."""
    if sys.platform == "win32":
        with suppress(Exception):
            import ctypes
            import locale

            lcid = ctypes.windll.kernel32.GetUserDefaultUILanguage()
            win_loc = locale.windows_locale.get(lcid, "")
            if win_loc:
                return win_loc.split("_")[0].split("-")[0].lower()
    for env_key in ("LC_ALL", "LC_MESSAGES", "LANG"):
        val = (os.environ.get(env_key) or "").strip()
        if val and val.lower() not in ("c", "posix"):
            code = val.split(".")[0].split("_")[0].split("-")[0].lower()
            if len(code) == 2 and code.isalpha():
                return code
    return default


def wav_bytes_to_pcm16(wav_bytes: bytes) -> tuple[bytes, int]:
    """Extract raw 16-bit mono PCM bytes and sample rate from a WAV buffer."""
    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        return wf.readframes(wf.getnframes()), wf.getframerate()


def read_started_ms(client: StackchanClient) -> int | None:
    try:
        val = client.playback_status().get("started_ms")
        return int(val) if val is not None else None
    except Exception:
        return None


def wait_for_playback_end(
    client: StackchanClient,
    timeout_sec: float = 30.0,
    baseline_started_ms: int | None = None,
) -> None:
    started_wait = time.monotonic()
    deadline = started_wait + timeout_sec
    saw_playing = False
    while time.monotonic() < deadline:
        with suppress(Exception):
            pst = client.playback_status()
            if pst.get("playing"):
                saw_playing = True
            elif saw_playing or (
                baseline_started_ms is not None
                and pst.get("started_ms") not in (None, baseline_started_ms)
            ):
                time.sleep(0.03)
                return
            elif time.monotonic() - started_wait > 2.5:
                return
        time.sleep(0.04)


def trigger_stream_play(client: StackchanClient, url: str, face: str = "happy") -> int | None:
    """Tell StackChan to start streaming the live WAV URL immediately."""
    with suppress(Exception):
        client.set_face(face)
    baseline_started_ms = read_started_ms(client)
    for attempt in range(1, 3):
        try:
            res = client.play(url)
            if res.get("success", True):
                return baseline_started_ms
            print(f"[stackchan_live] Play attempt {attempt}/2 failed: {res}", flush=True)
        except Exception as exc:
            print(f"[stackchan_live] Play attempt {attempt}/2 error: {exc}", flush=True)
        time.sleep(0.25)
    return baseline_started_ms


def listen_for_wav(
    client: StackchanClient,
    timeout_sec: float = 0.0,
    expected_voice: str = "",
) -> tuple[bytes, str]:
    """Wait for StackChan to capture a spoken utterance, or return early if the user changed voice in MCP -> SET."""
    with suppress(Exception):
        if client.playback_status().get("playing"):
            wait_for_playback_end(client, timeout_sec=20.0)
    with suppress(Exception):
        client.resume_listen()

    deadline = None if timeout_sec <= 0 else (time.monotonic() + timeout_sec)
    while deadline is None or time.monotonic() < deadline:
        try:
            ast = client.audio_status()
            dev_voice = (ast.get("voice") or "").strip()
            if expected_voice and dev_voice and dev_voice != expected_voice:
                return b"", dev_voice
            if ast.get("ready"):
                audio_data = client.get_audio()
                if audio_data and len(audio_data) >= 6400:
                    return audio_data, dev_voice
                client.resume_listen()
        except Exception:
            time.sleep(0.4)
            continue
        time.sleep(0.05)
    return b"", expected_voice


def build_live_config(voice: str, lang: str = "es", context: str = "") -> types.LiveConnectConfig:
    instr = LIVE_SYSTEM_INSTRUCTION
    if lang:
        instr += f"\nActive conversation language: {lang}."
    if context:
        instr += f"\nRecent context from coding agent: {context}"

    escalate_tool = types.Tool(
        function_declarations=[
            types.FunctionDeclaration(
                name="escalate_to_engineer",
                description="Hand off a technical, coding, file, build, camera, or live web-search task to the main coding agent.",
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "task": types.Schema(
                            type=types.Type.STRING,
                            description="Concise English summary of the task for the coding agent.",
                        )
                    },
                    required=["task"],
                ),
            ),
            types.FunctionDeclaration(
                name="end_chat",
                description="End the voice chat session when the user says goodbye or asks to stop.",
                parameters=types.Schema(type=types.Type.OBJECT, properties={}),
            ),
        ]
    )

    return types.LiveConnectConfig(
        response_modalities=[types.Modality.AUDIO],
        realtime_input_config=types.RealtimeInputConfig(
            automatic_activity_detection=types.AutomaticActivityDetection(disabled=True)
        ),
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice)
            )
        ),
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
        system_instruction=types.Content(parts=[types.Part(text=instr)]),
        tools=[escalate_tool],
    )


async def receive_and_stream_turn(
    session,
    device: StackchanClient,
    config,
    live_port: int,
    t0: float,
) -> tuple[str, str, str | None, bool, float | None]:
    """Receive one Live API turn and stream 24kHz PCM directly to StackChan on first audio byte."""
    user_tx: list[str] = []
    bot_tx: list[str] = []
    escalated_task: str | None = None
    end_requested = False

    stream_id: str | None = None
    stream_q: queue.Queue[bytes | None] | None = None
    play_task: asyncio.Task | None = None
    ttfa: float | None = None

    try:
        async for msg in session.receive():
            sc = getattr(msg, "server_content", None)
            if sc:
                if getattr(sc, "input_transcription", None) and sc.input_transcription.text:
                    user_tx.append(sc.input_transcription.text)
                if getattr(sc, "output_transcription", None) and sc.output_transcription.text:
                    bot_tx.append(sc.output_transcription.text)
                mt = getattr(sc, "model_turn", None)
                if mt and getattr(mt, "parts", None):
                    for part in mt.parts:
                        inline = getattr(part, "inline_data", None)
                        if inline and inline.data:
                            if stream_q is None:
                                ttfa = time.monotonic() - t0
                                stream_id = f"live_{uuid.uuid4().hex[:12]}.wav"
                                stream_q = register_live_stream(stream_id, sample_rate=24000)
                                stream_q.put(inline.data)
                                url = audio_url(config.mac_ip, live_port, f"live/{stream_id}")
                                play_task = asyncio.create_task(
                                    asyncio.to_thread(trigger_stream_play, device, url, "happy")
                                )
                            else:
                                stream_q.put(inline.data)
                if getattr(sc, "turn_complete", False):
                    break

            tc = getattr(msg, "tool_call", None)
            if tc and getattr(tc, "function_calls", None):
                for fc in tc.function_calls:
                    if fc.name == "escalate_to_engineer":
                        escalated_task = (fc.args or {}).get("task", "User requested technical assistance")
                        break
                    if fc.name == "end_chat":
                        end_requested = True
                        break
                if escalated_task or end_requested:
                    break
    finally:
        if stream_q is not None:
            stream_q.put(None)

    if play_task is not None:
        baseline_started_ms = await play_task
        await asyncio.to_thread(wait_for_playback_end, device, 30.0, baseline_started_ms)
        with suppress(Exception):
            device.set_face("calm")
    if stream_id is not None:
        unregister_live_stream(stream_id)

    return "".join(user_tx).strip(), "".join(bot_tx).strip(), escalated_task, end_requested, ttfa


async def run_live_loop(args: argparse.Namespace, active_lang: str) -> int:
    config = load_config()
    api_key = (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY not set in environment or .env")

    device = StackchanClient(config)
    live_port = start_live_audio_server()
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)

    with suppress(Exception):
        if device.audio_status().get("ready"):
            device.get_audio()
            device.resume_listen()

    genai_client = genai.Client(api_key=api_key, http_options={"api_version": "v1beta"})

    if args.no_greeting:
        with suppress(Exception):
            if device.playback_status().get("playing"):
                wait_for_playback_end(device, timeout_sec=20.0)

    follow_device_voice = args.voice.strip().lower() in ("", "auto")
    active_voice = "Puck"
    if follow_device_voice:
        with suppress(Exception):
            v = (device.audio_status().get("voice") or "").strip()
            if v:
                active_voice = v
    else:
        active_voice = args.voice.strip()

    first_connect = True
    turn = 0
    while turn < args.max_turns:
        live_cfg = build_live_config(active_voice, lang=active_lang, context=args.context if first_connect else "")

        with suppress(Exception):
            device.set_agent_mode("live", label=f"LIVE {active_voice}")
            device.set_face("calm")

        print(
            f"[stackchan_live] Connecting to {args.model} (voice={active_voice}, lang={active_lang}, port={live_port})...",
            flush=True,
        )

        voice_changed_to = ""
        async with genai_client.aio.live.connect(model=args.model, config=live_cfg) as session:
            print(f"[stackchan_live] Connected! LIVE 3.8 ({active_voice}) streaming session active.", flush=True)

            if not args.no_greeting or not first_connect:
                if first_connect:
                    greet_prompt = (
                        f"Saluda en una frase muy corta en español diciendo que ya estás listo en modo Gemini Live con la voz {active_voice}."
                        if active_lang == "es"
                        else f"Greet the user in one very short sentence in '{active_lang}' saying Gemini Live is ready with voice {active_voice}."
                    )
                else:
                    greet_prompt = (
                        f"Di en una frase muy corta en español: Listo, cambié a la voz {active_voice}."
                        if active_lang == "es"
                        else f"Say in one very short sentence in '{active_lang}': Switched to voice {active_voice}."
                    )
                t0 = time.monotonic()
                await session.send_client_content(
                    turns=types.Content(role="user", parts=[types.Part(text=greet_prompt)]),
                    turn_complete=True,
                )
                _heard, spoken, _esc, _end, ttfa = await asyncio.wait_for(
                    receive_and_stream_turn(session, device, config, live_port, t0), timeout=25.0
                )
                if spoken:
                    ttfa_s = f"{ttfa:.2f}s" if ttfa is not None else "n/a"
                    print(f"[Greeting (TTFA={ttfa_s})] {spoken}", flush=True)
            first_connect = False

            while turn < args.max_turns:
                wav_bytes, dev_voice = await asyncio.to_thread(
                    listen_for_wav,
                    device,
                    args.listen_timeout,
                    active_voice if follow_device_voice else "",
                )
                if follow_device_voice and dev_voice and dev_voice != active_voice:
                    print(f"[stackchan_live] Voice changed on device: {active_voice} -> {dev_voice}", flush=True)
                    voice_changed_to = dev_voice
                    break
                if not wav_bytes:
                    continue

                pcm_in, sample_rate = wav_bytes_to_pcm16(wav_bytes)
                if not pcm_in:
                    with suppress(Exception):
                        device.resume_listen()
                    continue

                turn += 1
                with suppress(Exception):
                    device.set_face("thinking")

                t0 = time.monotonic()
                await session.send_realtime_input(activity_start=types.ActivityStart())
                chunk_bytes = max(3200, int(sample_rate * 2 * 0.1))  # 100ms PCM16 chunks
                for offset in range(0, len(pcm_in), chunk_bytes):
                    await session.send_realtime_input(
                        audio=types.Blob(data=pcm_in[offset : offset + chunk_bytes], mime_type=f"audio/pcm;rate={sample_rate}")
                    )
                await session.send_realtime_input(activity_end=types.ActivityEnd())

                try:
                    heard, spoken, escalated_task, end_requested, ttfa = await asyncio.wait_for(
                        receive_and_stream_turn(session, device, config, live_port, t0), timeout=30.0
                    )
                except asyncio.TimeoutError:
                    print(f"[stackchan_live] Turn {turn} timed out waiting for Gemini Live; resuming listen.", flush=True)
                    with suppress(Exception):
                        device.set_face("calm")
                        device.resume_listen()
                    continue

                total = time.monotonic() - t0
                ttfa_s = f"{ttfa:.2f}s" if ttfa is not None else "n/a"
                if heard:
                    print(f"[Turn {turn} User] {heard}", flush=True)
                if spoken:
                    print(f"[Turn {turn} Bot (TTFA={ttfa_s}, total={total:.2f}s)] {spoken}", flush=True)

                if escalated_task:
                    with suppress(Exception):
                        device.set_agent_mode("engineer")
                        device.set_face("thinking")
                    print(f"[Last User ({active_lang})] {heard}", flush=True)
                    print(f"[ESCALATED] Task: {escalated_task}", flush=True)
                    return 0

                if end_requested:
                    with suppress(Exception):
                        device.set_agent_mode("idle")
                        device.set_face("calm")
                    print("[stackchan_live] Session ended by user.", flush=True)
                    return 0

        if voice_changed_to:
            active_voice = voice_changed_to
            continue
        break

    return 0


def run_fallback_chat(args: argparse.Namespace, active_lang: str, reason: str) -> int:
    """Seamlessly hand off to stackchan_chat.py (FREE 3.5 LITE) if Gemini Live fails."""
    print(f"[stackchan_live] Falling back to stackchan_chat.py (FREE 3.5 LITE): {reason}", flush=True)
    fallback_script = Path(__file__).resolve().with_name("stackchan_chat.py")
    cmd = [sys.executable, "-u", str(fallback_script), "--lang", active_lang]
    if args.no_greeting:
        cmd.append("--no-greeting")
    if args.context:
        cmd.extend(["--context", args.context])
    if args.listen_timeout > 0:
        cmd.extend(["--listen-timeout", str(int(args.listen_timeout))])
    return subprocess.call(cmd)


def main() -> None:
    parser = argparse.ArgumentParser(description="StackChan primary Gemini Live voice bridge (with FREE 3.5 LITE fallback)")
    parser.add_argument("--model", default=DEFAULT_LIVE_MODEL)
    parser.add_argument("--voice", default="auto")
    parser.add_argument("--lang", default="auto")
    parser.add_argument("--context", default="")
    parser.add_argument("--no-greeting", action="store_true")
    parser.add_argument("--no-fallback", action="store_true", help="Disable automatic fallback to stackchan_chat.py")
    parser.add_argument("--max-turns", type=int, default=50)
    parser.add_argument("--listen-timeout", type=float, default=0.0)
    args = parser.parse_args()

    raw_lang = args.lang.strip().lower()
    active_lang = detect_os_language() if raw_lang in ("", "auto", "os") else raw_lang.split("-")[0]

    try:
        rc = asyncio.run(run_live_loop(args, active_lang))
    except Exception as exc:
        if args.no_fallback:
            print(f"[stackchan_live] Live session error ({exc}).", flush=True)
            rc = 1
        else:
            rc = run_fallback_chat(args, active_lang, str(exc))
    sys.exit(rc)


if __name__ == "__main__":
    main()
