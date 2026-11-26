"""
stackchan_chat.py - Voice conversation loop for StackChan using the direct Gemini Interactions API.

Usage:
    cd D:\\StackChan\\stackchan-mcp
    uv run python -u scripts/stackchan_chat.py
    uv run python -u scripts/stackchan_chat.py --lang es
    uv run python -u scripts/stackchan_chat.py --model gemini-4-hydrogen --lang auto

Requirements:
    - GEMINI_API_KEY set in environment or .env
    - google-genai >= 3.2.0 installed
    - stackchan-mcp configured with Piper / Edge TTS and Faster-Whisper ASR
"""

import argparse
import datetime
import os
import re
import sys
import threading
import time
from contextlib import suppress
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from google import genai  # noqa: E402

from mcp_server import audio_processing  # noqa: E402
from mcp_server.audio_server import AUDIO_DIR, audio_url, start_audio_server  # noqa: E402
from mcp_server.stackchan_client import StackchanClient  # noqa: E402
from mcp_server.stackchan_config import load_config  # noqa: E402

DEFAULT_MODEL = "gemini-3.5-flash-lite"
FALLBACK_MODELS = ("gemini-3.5-flash-lite", "gemini-3.1-flash-lite")
GEMINI_TIMEOUT_SEC = 15.0
# Hard output cap: keeps TTS snappy without clipping sentences (220 tokens ≈ 3-4 short sentences).
GEMINI_MAX_OUTPUT_TOKENS = 220

SYSTEM_INSTRUCTION = (
    "You are StackChan, a friendly desktop robot in casual voice-chat. "
    "Reply in 1-2 short natural sentences — no markdown, no lists. "
    "Stay strictly in the current conversation language unless the user explicitly asks to switch languages.\n"
    "If the user explicitly asks to switch/change language (e.g. 'speak English', 'hablemos en inglés', 'cambia a francés'), "
    "reply ONLY with: SET_LANG: <2-letter ISO code> | <short confirmation in the NEW language>\n\n"
    "TWO request types:\n"
    "1. GENERAL (answer yourself): stable knowledge, history, science, opinions, jokes, chit-chat "
    "that you can answer confidently without web search or PC tools.\n"
    "2. ESCALATE IMMEDIATELY (do NOT answer or apologize for cutoff): "
    "a) modifying/reading files, code, scripts, logs, configs, firmware/MCP changes, system diagnostics, camera, or shell commands; "
    "b) ANY live, real-time, or post-cutoff question you cannot verify with certainty without web search "
    "(e.g., current officeholders/presidents in 2026+, today's news, weather, live prices, recent events). "
    "If you lack up-to-date info to answer accurately, NEVER reply saying you have a knowledge cutoff — escalate instead so the engineer searches the web.\n"
    "Do NOT escalate casual testing remarks, requests to suggest example test questions, or announcements like 'I have a technical question' when no concrete question has been asked yet — invite them to ask it.\n"
    "To escalate, your ENTIRE reply must be ONLY this one line — no extra text before or after:\n"
    "ESCALATE_TO_ENGINEER: <concise English summary>\n\n"
    "NEVER add a conversational sentence before ESCALATE_TO_ENGINEER. "
    "If you add ANY text before that line, it will be spoken aloud as a voice message, which is wrong.\n\n"
    "To end chat the user says 'stop', 'bye', 'terminamos', 'chao' → reply ONLY: END_CHAT."
)

_ESCALATE_RE = re.compile(r"^\s*ESC[A-Z_]*TO_ENGINEER\s*:\s*(.+)$", re.IGNORECASE | re.MULTILINE)
_END_RE = re.compile(r"^\s*END_CHAT\b", re.IGNORECASE | re.MULTILINE)
_SET_LANG_RE = re.compile(r"^\s*SET_LANG\s*:\s*([a-z]{2}(?:-[a-z]{2})?)\s*(?:\|\s*(.*))?$", re.IGNORECASE | re.MULTILINE)

# Detect when the user mentions years >= current year (likely near/past knowledge cutoff)
_CURRENT_YEAR = datetime.date.today().year
_RECENT_YEAR_RE = re.compile(r"\b(20[2-9][0-9])\b")

_CUTOFF_NOTE_ES = (
    "Nota: no tienes búsqueda web y tu corte de conocimiento es anterior; "
    "si esta pregunta pide hechos actuales o posteriores a tu corte que no puedas confirmar con certeza, "
    "responde SOLO con ESCALATE_TO_ENGINEER: <resumen en inglés para buscar en la web>. "
)
_CUTOFF_NOTE_EN = (
    "Note: you have no web search and an earlier knowledge cutoff; "
    "if this asks for current or post-cutoff facts you cannot verify with certainty, "
    "reply ONLY with ESCALATE_TO_ENGINEER: <concise English web-lookup summary>. "
)

_USER_ESCALATE_PHRASES = (
    "pasa al ingeniero",
    "pasame al ingeniero",
    "pásame al ingeniero",
    "ingeniero principal",
    "escalate to engineer",
    "hand off to engineer",
)

_USER_END_PHRASES = (
    "terminamos",
    "dejémoslo hasta aquí",
    "dejemolo hasta aqui",
    "end chat",
    "stop chatting",
)

_LOCALIZED_MESSAGES = {
    "es": {
        "greeting": "¡Hola! Soy StackChan. Estoy listo para conversar en tu idioma.",
        "timeout": "¿Sigues ahí? Cuando quieras seguimos.",
        "escalate": "Paso la orden al ingeniero principal.",
        "goodbye": "Listo, cerramos la charla. ¡Hasta luego!",
        "api_error": "Tuve un problema temporal conectando con Gemini. Inténtalo de nuevo.",
    },
    "en": {
        "greeting": "Hello! I'm StackChan, ready to chat in your language.",
        "timeout": "Are you still there? Let me know when you want to continue.",
        "escalate": "Handing this over to the lead engineer.",
        "goodbye": "Alright, ending the chat. See you later!",
        "api_error": "I hit a temporary error reaching Gemini. Please try again.",
    },
}


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
    with suppress(Exception):
        import locale

        loc = (locale.getlocale()[0] or "").strip()
        if loc and "_" in loc and len(loc.split("_")[0]) == 2:
            return loc.split("_")[0].lower()
        lowered = loc.lower()
        for prefix, code in (
            ("spanish", "es"),
            ("english", "en"),
            ("french", "fr"),
            ("portuguese", "pt"),
            ("german", "de"),
            ("italian", "it"),
            ("japanese", "ja"),
            ("korean", "ko"),
            ("chinese", "zh"),
        ):
            if lowered.startswith(prefix):
                return code
    return default


def localized_text(lang: str, key: str) -> str:
    code = (lang or "en").split("-")[0].lower()
    bucket = _LOCALIZED_MESSAGES.get(code, _LOCALIZED_MESSAGES["en"])
    return bucket[key]


def read_started_ms(client: StackchanClient) -> int | None:
    try:
        val = client.playback_status().get("started_ms")
        return int(val) if val is not None else None
    except Exception:
        return None


def play_with_retry(client: StackchanClient, url: str, attempts: int = 3) -> bool:
    for attempt in range(1, attempts + 1):
        try:
            res = client.play(url)
            if res.get("success", True):
                return True
            print(f"[stackchan_chat] Play attempt {attempt}/{attempts} failed: {res}")
        except Exception as exc:
            print(f"[stackchan_chat] Play attempt {attempt}/{attempts} error: {exc}")
        if attempt < attempts:
            time.sleep(1.0)
    return False


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


def speak_and_listen(
    client: StackchanClient,
    config,
    text: str,
    tts_lang: str,
    asr_lang: str,
    listen_timeout: int = 60,
    face: str = "happy",
    gesture: str = "nod",
) -> tuple[str, str]:
    """Speak `text` in `tts_lang`, wait for playback, and listen for the user's reply."""
    if text:
        try:
            wav_path = audio_processing.generate_tts(text, tts_lang, config)
        except Exception as exc:
            print(f"[stackchan_chat] TTS error ({tts_lang}): {exc}", flush=True)
            with suppress(Exception):
                client.set_face("calm")
            time.sleep(0.5)
            return "", tts_lang
        try:
            url = audio_url(config.mac_ip, config.audio_serve_port, wav_path.name)
            with suppress(Exception):
                client.set_face(face)
            baseline_started_ms = read_started_ms(client)
            if not play_with_retry(client, url):
                time.sleep(1.0)
                return "", tts_lang
            if gesture in {"nod", "shake", "home"}:
                with suppress(Exception):
                    client.gesture(gesture)
            wait_for_playback_end(client, baseline_started_ms=baseline_started_ms)
        finally:
            wav_path.unlink(missing_ok=True)
    else:
        with suppress(Exception):
            client.resume_listen()

    deadline = time.monotonic() + listen_timeout
    while time.monotonic() < deadline:
        with suppress(Exception):
            ast = client.audio_status()
            if ast.get("ready"):
                audio_data = client.get_audio()
                if audio_data and len(audio_data) >= 6400:
                    rec_path = AUDIO_DIR / f"rec_{int(time.time() * 1000)}.wav"
                    try:
                        rec_path.write_bytes(audio_data)
                        asr = audio_processing.transcribe_audio(rec_path, asr_lang, config)
                    finally:
                        rec_path.unlink(missing_ok=True)
                    heard = (asr.get("text") or "").strip()
                    detected = (asr.get("language") or tts_lang or "en").split("-")[0].lower()
                    if heard:
                        return heard, detected
                client.resume_listen()
        time.sleep(0.035)
    return "", tts_lang


def speak_only(client: StackchanClient, config, text: str, lang: str, face: str = "calm") -> None:
    """Speak a final message without waiting for a reply."""
    with suppress(Exception):
        client.set_face(face)
    try:
        wav_path = audio_processing.generate_tts(text, lang, config)
    except Exception as exc:
        print(f"[stackchan_chat] TTS error ({lang}): {exc}", flush=True)
        return
    try:
        url = audio_url(config.mac_ip, config.audio_serve_port, wav_path.name)
        baseline_started_ms = read_started_ms(client)
        if play_with_retry(client, url):
            wait_for_playback_end(client, timeout_sec=15.0, baseline_started_ms=baseline_started_ms)
    finally:
        wav_path.unlink(missing_ok=True)


def generate_reply(
    genai_client: genai.Client,
    primary_model: str,
    prompt_text: str,
    previous_interaction_id: str | None,
    history_model: str | None,
    log_fn=print,
) -> tuple[str, str | None, str]:
    """Call Gemini Interactions API with timeout and model fallback so a stalled model never hangs."""
    models_to_try = [primary_model] + [m for m in FALLBACK_MODELS if m != primary_model]
    last_exc: Exception | None = None
    for model_name in models_to_try:
        kwargs: dict = {
            "model": model_name,
            "system_instruction": SYSTEM_INSTRUCTION,
            "input": prompt_text,
            "timeout": GEMINI_TIMEOUT_SEC,
            "generation_config": {"max_output_tokens": GEMINI_MAX_OUTPUT_TOKENS},
        }
        if previous_interaction_id and history_model == model_name:
            kwargs["previous_interaction_id"] = previous_interaction_id
        log_fn(f"[stackchan_chat] Calling Gemini ({model_name})...")
        t0 = time.monotonic()
        try:
            interaction = cast(Any, genai_client.interactions.create(**kwargs))
            reply = (interaction.output_text or "").strip()
            if not reply:
                raise RuntimeError(f"{model_name} returned empty output_text")
            elapsed = time.monotonic() - t0
            log_fn(f"[Gemini ({model_name}, {elapsed:.2f}s)] {reply}")
            return reply, interaction.id, model_name
        except Exception as exc:
            last_exc = exc
            elapsed = time.monotonic() - t0
            log_fn(f"[stackchan_chat] {model_name} failed after {elapsed:.2f}s: {type(exc).__name__}: {exc}")
    raise RuntimeError(f"All models failed: {last_exc}") from last_exc


def _clip(text: str, limit: int = 180) -> str:
    clean = " ".join((text or "").split())
    return clean if len(clean) <= limit else clean[: limit - 1] + "…"


def emit_escalation(
    task: str,
    heard: str,
    lang: str,
    log_path: Path,
    prev_user: str = "",
    prev_bot: str = "",
) -> None:
    """Print only the essential context for the Antigravity engineer on escalation."""
    lines: list[str] = []
    if prev_user or prev_bot:
        parts = []
        if prev_user:
            parts.append(f"User: {_clip(prev_user)}")
        if prev_bot:
            parts.append(f"StackChan: {_clip(prev_bot)}")
        lines.append(f"[Prev Turn] {' | '.join(parts)}")
    lines.append(f"[Last User ({lang})] {heard}")
    lines.append(f"[ESCALATED] Task: {task}")
    lines.append(f"[Chat Log] {log_path}")
    print("\n" + "\n".join(lines), flush=True)


def cleanup_stale_temp_files(max_age_sec: float = 3600.0) -> None:
    """Remove leftover TTS/ASR WAVs and old chat logs so %TEMP% never accumulates."""
    cutoff = time.time() - max_age_sec
    for pattern in ("tts_*.wav", "rec_*.wav", "chat_*.log"):
        for path in AUDIO_DIR.glob(pattern):
            try:
                if path.name != "chat_latest.log" and path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)
            except OSError:
                pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="StackChan voice chat loop using Gemini Interactions API")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Gemini model name")
    parser.add_argument("--lang", default="auto", help="Language code ('auto', 'es', 'en', etc.)")
    parser.add_argument("--turns", type=int, default=100, help="Maximum conversation turns")
    parser.add_argument("--listen-timeout", type=int, default=60, help="Seconds to wait for user speech")
    parser.add_argument("--verbose", action="store_true", help="Also stream full per-turn transcript to stdout")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config()

    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        print("[stackchan_chat] ERROR: GEMINI_API_KEY is not set in environment or .env.", file=sys.stderr)
        sys.exit(1)

    genai_client = genai.Client(
        api_key=api_key,
        http_options={
            "timeout": int(GEMINI_TIMEOUT_SEC * 1000),
            "retry_options": {"attempts": 1},
        },
    )
    device_client = StackchanClient(config)
    start_audio_server(config.audio_serve_port)
    print("[stackchan_chat] Warming up local models...", flush=True)
    # Warmup runs in a background thread so the greeting can be synthesized in parallel.
    warmup_thread = threading.Thread(target=audio_processing.warmup_local_models, daemon=True)
    warmup_thread.start()
    with suppress(Exception):
        if device_client.audio_status().get("ready"):
            device_client.get_audio()
            device_client.resume_listen()

    cleanup_stale_temp_files()
    log_path = AUDIO_DIR / "chat_latest.log"
    with log_path.open("w", encoding="utf-8", buffering=1) as log_file:
        def log_line(msg: str) -> None:
            stamp = time.strftime("%H:%M:%S")
            log_file.write(f"{stamp} {msg}\n")
            if args.verbose:
                print(msg, flush=True)

        raw_lang = args.lang.strip().lower()
        active_lang = detect_os_language() if raw_lang in ("", "auto", "os") else raw_lang.split("-")[0]
        next_say = localized_text(active_lang, "greeting")
        previous_interaction_id: str | None = None
        history_model: str | None = None
        last_user = ""
        last_bot = ""
        completed_turns = 0

        start_msg = f"[stackchan_chat] Started (model={args.model}, lang={active_lang}, requested={args.lang}, log={log_path})"
        log_file.write(f"{time.strftime('%H:%M:%S')} {start_msg}\n")
        print(start_msg, flush=True)

        for _turn in range(args.turns):
            log_line(f"[StackChan ({active_lang})] {next_say}")
            heard, _detected_lang = speak_and_listen(
                device_client,
                config,
                next_say,
                tts_lang=active_lang,
                asr_lang=active_lang,
                listen_timeout=args.listen_timeout,
            )

            if not heard:
                log_line("[stackchan_chat] No user response (timeout).")
                next_say = localized_text(active_lang, "timeout")
                continue

            completed_turns += 1
            log_line(f"[User ({active_lang})] {heard}")
            lowered = heard.lower()

            if any(phrase in lowered for phrase in _USER_ESCALATE_PHRASES):
                speak_only(device_client, config, localized_text(active_lang, "escalate"), active_lang, face="thinking")
                log_line(f"[ESCALATED] Task: {heard}")
                emit_escalation(heard, heard, active_lang, log_path, prev_user=last_user, prev_bot=last_bot)
                sys.exit(0)

            if any(phrase == lowered.strip(" .!?,¡¿") for phrase in _USER_END_PHRASES):
                speak_only(device_client, config, localized_text(active_lang, "goodbye"), active_lang, face="calm")
                log_line("[stackchan_chat] Session ended by user.")
                print(f"[stackchan_chat] Session ended by user ({completed_turns} turns). Log: {log_path}", flush=True)
                sys.exit(0)

            # Deterministic cutoff guard: if the user mentions a year >= current year,
            # inject a cutoff reminder so the model flags uncertainty before answering.
            year_matches = _RECENT_YEAR_RE.findall(heard)
            if any(int(y) >= _CURRENT_YEAR for y in year_matches):
                cutoff_note = _CUTOFF_NOTE_ES if active_lang == "es" else _CUTOFF_NOTE_EN
                prompt_text = cutoff_note + heard
                log_line("[stackchan_chat] Recent year detected, injecting cutoff note.")
            else:
                prompt_text = heard

            with suppress(Exception):
                device_client.set_face("thinking")

            try:
                reply, previous_interaction_id, history_model = generate_reply(
                    genai_client,
                    args.model,
                    prompt_text,
                    previous_interaction_id,
                    history_model,
                    log_fn=log_line,
                )
            except Exception as exc:
                log_line(f"[stackchan_chat] ERROR calling Gemini: {exc}")
                next_say = localized_text(active_lang, "api_error")
                continue

            escalate_match = _ESCALATE_RE.search(reply)
            if escalate_match:
                task = escalate_match.group(1).strip()
                # Only speak a prefix if it comes BEFORE the escalate marker and contains no escalation text.
                raw_prefix = reply[: escalate_match.start()].strip()
                # Strip any residual ESCALATE or markup the model may have leaked into the prefix.
                clean_prefix = re.sub(r"ESCALATE\S*", "", raw_prefix, flags=re.IGNORECASE).strip()
                # Only use a clean, short prefix (≤ 80 chars) so the TTS never reads weird content.
                spoken_prefix = clean_prefix if 0 < len(clean_prefix) <= 80 else ""
                spoken = f"{spoken_prefix} {localized_text(active_lang, 'escalate')}".strip() if spoken_prefix else localized_text(active_lang, "escalate")
                speak_only(device_client, config, spoken, active_lang, face="thinking")
                log_line(f"[ESCALATED] Task: {task}")
                emit_escalation(task, heard, active_lang, log_path, prev_user=last_user, prev_bot=last_bot)
                sys.exit(0)

            if _END_RE.search(reply):
                speak_only(device_client, config, localized_text(active_lang, "goodbye"), active_lang, face="calm")
                log_line("[stackchan_chat] Session ended by user.")
                print(f"[stackchan_chat] Session ended by user ({completed_turns} turns). Log: {log_path}", flush=True)
                sys.exit(0)

            set_lang_match = _SET_LANG_RE.search(reply)
            if set_lang_match:
                new_lang = set_lang_match.group(1).split("-")[0].lower()
                confirm_text = (set_lang_match.group(2) or "").strip()
                if not confirm_text:
                    confirm_text = reply[set_lang_match.end():].strip() or localized_text(new_lang, "greeting")
                active_lang = new_lang
                log_line(f"[stackchan_chat] Language switched to '{active_lang}' by user request.")
                last_user = heard
                last_bot = confirm_text
                next_say = confirm_text
                continue

            last_user = heard
            last_bot = reply
            next_say = reply

        speak_only(device_client, config, localized_text(active_lang, "goodbye"), active_lang, face="calm")
        log_line("[stackchan_chat] Turn limit reached.")
        print(f"[stackchan_chat] Turn limit reached ({completed_turns} turns). Log: {log_path}", flush=True)


if __name__ == "__main__":
    main()
