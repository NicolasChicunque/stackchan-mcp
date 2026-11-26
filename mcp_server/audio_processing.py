import asyncio
import logging
import os
import shutil
import struct
import subprocess
import threading
import time
import uuid
import wave
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from pathlib import Path
from typing import Any

import numpy as np
import requests

try:
    import edge_tts as _edge_tts_mod
except ImportError:
    _edge_tts_mod = None

try:
    from piper import PiperVoice as _PiperVoice
    from piper import SynthesisConfig as _PiperSynthesisConfig
except ImportError:
    _PiperVoice = None
    _PiperSynthesisConfig = None

try:
    from faster_whisper import WhisperModel as _WhisperModel
except ImportError:
    _WhisperModel = None

from .audio_server import AUDIO_DIR, TEMP_AUDIO_DIR
from .elevenlabs_tts import TtsSynthesisError, collect_pcm, synthesize_pcm
from .stackchan_config import (
    EDGE_VOICES,
    PCM_CHANNELS,
    PCM_CONTENT_TYPE,
    PCM_SAMPLE_RATE,
    PCM_SAMPLE_WIDTH,
    StackchanConfig,
)

_PIPER_LOCK = threading.Lock()
_PIPER_CACHE: dict[str, Any] = {}
_WHISPER_LOCK = threading.Lock()
_WHISPER_MODEL: Any = None
_WHISPER_MODEL_NAME: str | None = None

_DEFAULT_PIPER_ES = Path(__file__).resolve().parents[1] / "models" / "piper" / "es_MX-claude-high.onnx"
_AUTO_PRIMARY_LANGS = {"es", "en", "fr", "pt", "de", "it", "zh", "ja", "ko"}
_HALLUCINATIONS = {
    "gracias por ver",
    "gracias por ver el video",
    "gracias por ver el vídeo",
    "suscríbete",
    "subtítulos por la comunidad de amara.org",
    "subtítulos realizados por la comunidad de amara.org",
}

logger = logging.getLogger(__name__)


def new_tts_stem() -> str:
    return f"tts_{int(time.time() * 1000)}_{uuid.uuid4().hex}"


def raise_for_fish_status(resp: requests.Response) -> None:
    try:
        resp.raise_for_status()
        return
    except requests.HTTPError as exc:
        try:
            detail = resp.text.strip()
        except Exception:
            detail = ""
        if len(detail) > 500:
            detail = detail[:500] + "…"
        message = str(exc)
        if detail:
            message = f"{message}; Fish response: {detail}"
        raise requests.HTTPError(message, response=resp) from exc


def validate_playback_wav(wav_path: Path) -> None:
    try:
        with wave.open(str(wav_path), "rb") as wav:
            channels = wav.getnchannels()
            sample_rate = wav.getframerate()
            sample_width = wav.getsampwidth()
            compression = wav.getcomptype()
            frame_count = wav.getnframes()

            if (
                compression != "NONE"
                or channels != 1
                or sample_rate != PCM_SAMPLE_RATE
                or sample_width != PCM_SAMPLE_WIDTH
            ):
                raise ValueError(
                    "unsupported WAV format: "
                    f"compression={compression} channels={channels} "
                    f"rate={sample_rate} width={sample_width}"
                )
            if frame_count <= 0:
                raise ValueError("WAV has no audio frames")

            pcm = wav.readframes(frame_count)
            expected_bytes = frame_count * channels * sample_width
            if len(pcm) != expected_bytes:
                raise ValueError(f"truncated WAV data: got={len(pcm)} expected={expected_bytes}")
    except (EOFError, wave.Error) as exc:
        raise ValueError(f"invalid WAV file: {exc}") from exc


def publish_validated_wav(temp_wav_path: Path, final_stem: str) -> Path:
    validate_playback_wav(temp_wav_path)
    final_path = AUDIO_DIR / f"{final_stem}.wav"
    os.replace(temp_wav_path, final_path)
    return final_path


def require_executable(name: str) -> str:
    fallback = (
        Path(os.path.expanduser("~"))
        / "AppData"
        / "Local"
        / "Microsoft"
        / "WinGet"
        / "Packages"
        / "Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe"
        / "ffmpeg-9.0.2-full_build"
        / "bin"
        / "ffmpeg.exe"
    )
    executable = shutil.which(name) or (str(fallback) if name == "ffmpeg" and fallback.exists() else None)
    if not executable:
        raise RuntimeError(f"Required executable not found on PATH: {name}")
    return executable


def tts_edge(text: str, lang: str, config: StackchanConfig) -> Path:
    with open(AUDIO_DIR / "debug.txt", "a", encoding="utf-8") as _df:
        _df.write(f"{time.strftime('%X')} [tts_edge] called text={text!r} lang={lang!r}\n")
    norm_lang = (lang or "en").strip()
    base_lang = norm_lang.split("-")[0].lower()
    voice = EDGE_VOICES.get(norm_lang) or EDGE_VOICES.get(base_lang) or EDGE_VOICES["en"]
    stem = new_tts_stem()
    mp3_path = TEMP_AUDIO_DIR / f"{stem}.mp3"
    temp_wav_path = TEMP_AUDIO_DIR / f"{stem}.wav"
    ffmpeg_bin = require_executable("ffmpeg")
    rate = getattr(config, "edge_tts_rate", "+10%")
    pitch = getattr(config, "edge_tts_pitch", "+15Hz")
    try:
        if _edge_tts_mod is not None:
            edge_tts_mod = _edge_tts_mod

            async def _synth():
                comm = edge_tts_mod.Communicate(text, voice=voice, rate=rate, pitch=pitch)
                await comm.save(str(mp3_path))
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                asyncio.run(_synth())
            else:
                with ThreadPoolExecutor(max_workers=1) as _pool:
                    _pool.submit(asyncio.run, _synth()).result()
        else:
            subprocess.run(  # noqa: S603 - shell=False and executable/args are controlled.
                [
                    config.edge_tts_bin,
                    "--voice",
                    voice,
                    "--rate",
                    rate,
                    "--pitch",
                    pitch,
                    "--text",
                    text,
                    "--write-media",
                    str(mp3_path),
                ],
                stdin=subprocess.DEVNULL,
                check=True,
                capture_output=True,
            )
        with open(AUDIO_DIR / "debug.txt", "a", encoding="utf-8") as _df:
            _df.write(f"{time.strftime('%X')} [tts_edge] mp3 ready, running ffmpeg\n")
        subprocess.run(  # noqa: S603 - shell=False and executable/args are controlled.
            [
                ffmpeg_bin,
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(mp3_path),
                "-ar",
                str(PCM_SAMPLE_RATE),
                "-ac",
                "1",
                "-sample_fmt",
                "s16",
                str(temp_wav_path),
            ],
            stdin=subprocess.DEVNULL,
            check=True,
            capture_output=True,
        )
        with open(AUDIO_DIR / "debug.txt", "a", encoding="utf-8") as _df:
            _df.write(f"{time.strftime('%X')} [tts_edge] wav ready\n")
        return publish_validated_wav(temp_wav_path, stem)
    except subprocess.CalledProcessError as exc:
        with open(AUDIO_DIR / "debug.txt", "a", encoding="utf-8") as _df:
            _df.write(
                f"{time.strftime('%X')} [tts_edge CalledProcessError] rc={exc.returncode} "
                f"err={exc.stderr.decode('utf-8', errors='ignore') if exc.stderr else ''}\n"
            )
        raise
    except Exception as exc:
        with open(AUDIO_DIR / "debug.txt", "a", encoding="utf-8") as _df:
            _df.write(f"{time.strftime('%X')} [tts_edge exc] {type(exc).__name__}: {exc}\n")
        raise
    finally:
        mp3_path.unlink(missing_ok=True)
        temp_wav_path.unlink(missing_ok=True)


def tts_fish(text: str, lang: str, config: StackchanConfig) -> Path:
    model_id = config.fish_audio_model_zh if lang == "zh" else config.fish_audio_model_en
    stem = new_tts_stem()
    raw_path = TEMP_AUDIO_DIR / f"{stem}_raw.wav"
    temp_wav_path = TEMP_AUDIO_DIR / f"{stem}.wav"
    ffmpeg_bin = require_executable("ffmpeg")
    try:
        resp = requests.post(
            "https://api.fish.audio/v1/tts",
            headers={
                "Authorization": f"Bearer {config.fish_audio_key}",
                "Content-Type": "application/json",
            },
            json={
                "text": text,
                "reference_id": model_id,
                "format": "wav",
                "sample_rate": PCM_SAMPLE_RATE,
            },
            timeout=config.fish_tts_timeout,
        )
        raise_for_fish_status(resp)
        raw_path.write_bytes(resp.content)
        subprocess.run(  # noqa: S603 - shell=False and executable/args are controlled.
            [
                ffmpeg_bin,
                "-y",
                "-i",
                str(raw_path),
                "-af",
                "loudnorm=I=-16:TP=-3:LRA=11,alimiter=limit=0.9:attack=0.1:release=50",
                "-ar",
                str(PCM_SAMPLE_RATE),
                "-ac",
                "1",
                "-sample_fmt",
                "s16",
                str(temp_wav_path),
            ],
            stdin=subprocess.DEVNULL,
            check=True,
            capture_output=True,
        )
        return publish_validated_wav(temp_wav_path, stem)
    except subprocess.CalledProcessError as exc:
        with open(AUDIO_DIR / "debug.txt", "a", encoding="utf-8") as _df:
            _df.write(
                f"{time.strftime('%X')} [tts_fish CalledProcessError] rc={exc.returncode} "
                f"err={exc.stderr.decode('utf-8', errors='ignore') if exc.stderr else ''}\n"
            )
        raise
    except Exception as exc:
        with open(AUDIO_DIR / "debug.txt", "a", encoding="utf-8") as _df:
            _df.write(f"{time.strftime('%X')} [tts_fish exc] {type(exc).__name__}: {exc}\n")
        raise
    finally:
        raw_path.unlink(missing_ok=True)
        temp_wav_path.unlink(missing_ok=True)


def _get_piper_voice(model_path: Path) -> Any:
    key = str(model_path)
    voice = _PIPER_CACHE.get(key)
    if voice is not None:
        return voice
    with _PIPER_LOCK:
        voice = _PIPER_CACHE.get(key)
        if voice is None:
            if _PiperVoice is None:
                raise RuntimeError("piper-tts is not installed")
            voice = _PiperVoice.load(key)
            # 1-word warmup compiles ONNX execution graph once
            list(voice.synthesize("ok"))
            _PIPER_CACHE[key] = voice
        return voice


def tts_piper(text: str, lang: str, config: StackchanConfig) -> Path:
    t0 = time.perf_counter()
    model_str = os.environ.get("STACKCHAN_PIPER_MODEL", str(_DEFAULT_PIPER_ES))
    model_path = Path(model_str)
    if _PiperVoice is None or not model_path.exists() or not lang.lower().startswith("es"):
        return tts_edge(text, lang, config)

    voice = _get_piper_voice(model_path)
    length_scale = float(os.environ.get("STACKCHAN_PIPER_LENGTH_SCALE", "0.90"))
    syn_cfg = _PiperSynthesisConfig(length_scale=length_scale) if _PiperSynthesisConfig else None
    pcm_bytes = b"".join(c.audio_int16_bytes for c in voice.synthesize(text, syn_config=syn_cfg))
    if not pcm_bytes:
        return tts_edge(text, lang, config)

    src = np.frombuffer(pcm_bytes, dtype=np.int16)
    src_rate = int(getattr(voice.config, "sample_rate", 22050))
    if src_rate != PCM_SAMPLE_RATE and len(src) > 1:
        n_out = int(round(len(src) * PCM_SAMPLE_RATE / src_rate))
        xp = np.linspace(0.0, float(len(src) - 1), len(src), dtype=np.float32)
        x_new = np.linspace(0.0, float(len(src) - 1), n_out, dtype=np.float32)
        dst = np.interp(x_new, xp, src).astype(np.int16)
    else:
        dst = src

    stem = new_tts_stem()
    temp_wav_path = TEMP_AUDIO_DIR / f"{stem}.wav"
    final_path = AUDIO_DIR / f"{stem}.wav"
    try:
        with wave.open(str(temp_wav_path), "wb") as wav:
            wav.setnchannels(PCM_CHANNELS)
            wav.setsampwidth(PCM_SAMPLE_WIDTH)
            wav.setframerate(PCM_SAMPLE_RATE)
            wav.writeframes(dst.tobytes())
        os.replace(temp_wav_path, final_path)
    finally:
        temp_wav_path.unlink(missing_ok=True)

    with open(AUDIO_DIR / "debug.txt", "a", encoding="utf-8") as _df:
        _df.write(f"{time.strftime('%X')} [tts_piper] {time.perf_counter() - t0:.3f}s text={text!r}\n")
    return final_path


def _get_whisper_model():
    global _WHISPER_MODEL, _WHISPER_MODEL_NAME
    size = os.environ.get("STACKCHAN_WHISPER_MODEL", "base").strip() or "base"
    if _WHISPER_MODEL is not None and size == _WHISPER_MODEL_NAME:
        return _WHISPER_MODEL
    with _WHISPER_LOCK:
        if _WHISPER_MODEL is None or size != _WHISPER_MODEL_NAME:
            if _WhisperModel is None:
                raise RuntimeError("faster-whisper is not installed")
            threads = int(os.environ.get("STACKCHAN_WHISPER_THREADS", "8"))
            model = _WhisperModel(size, device="cpu", compute_type="int8", cpu_threads=threads)
            # Short warmup so first real turn (including auto language detect) has zero graph compile lag
            list(
                model.transcribe(
                    np.zeros(8000, dtype=np.float32),
                    language=None,
                    beam_size=1,
                    temperature=0.0,
                    without_timestamps=True,
                    condition_on_previous_text=False,
                )[0]
            )
            _WHISPER_MODEL = model
            _WHISPER_MODEL_NAME = size
        return _WHISPER_MODEL


def warmup_local_models() -> None:
    """Preload Piper TTS and Faster-Whisper into RAM in the background at server boot."""
    try:
        model_path = Path(os.environ.get("STACKCHAN_PIPER_MODEL", str(_DEFAULT_PIPER_ES)))
        if _PiperVoice is not None and model_path.exists():
            _get_piper_voice(model_path)
    except Exception as exc:
        logger.warning("Piper warmup failed: %s", exc)
    try:
        if _WhisperModel is not None and os.environ.get("STACKCHAN_ASR_ENGINE", "whisper").lower() != "google":
            _get_whisper_model()
    except Exception as exc:
        logger.warning("Whisper warmup failed: %s", exc)


def generate_tts(
    text: str, lang: str, config: StackchanConfig, *, details: dict[str, str] | None = None
) -> Path:
    if config.tts_engine == "piper":
        if details is not None:
            details["engine"] = "piper"
        return tts_piper(text, lang, config)
    if config.tts_engine == "elevenlabs":
        pcm, engine = prepare_elevenlabs_pcm(text, lang, config)
        if details is not None:
            details["engine"] = engine
        stem = new_tts_stem()
        temp_wav_path = TEMP_AUDIO_DIR / f"{stem}.wav"
        try:
            with wave.open(str(temp_wav_path), "wb") as wav:
                wav.setnchannels(PCM_CHANNELS)
                wav.setsampwidth(PCM_SAMPLE_WIDTH)
                wav.setframerate(PCM_SAMPLE_RATE)
                wav.writeframes(pcm)
            return publish_validated_wav(temp_wav_path, stem)
        finally:
            temp_wav_path.unlink(missing_ok=True)
    if config.tts_engine == "fish-audio" and config.fish_audio_key:
        return tts_fish(text, lang, config)
    return tts_edge(text, lang, config)


def prepare_elevenlabs_pcm(text: str, lang: str, config: StackchanConfig) -> tuple[bytes, str]:
    try:
        return synthesize_pcm(text, config), "elevenlabs"
    except TtsSynthesisError as exc:
        if not config.fish_audio_key:
            raise TtsSynthesisError(f"{exc}; Fish fallback is not configured") from None
        logger.warning("ElevenLabs unavailable; synthesizing once with Fish: %s", exc)

    chunks = iter_fish_pcm_stream(text, lang, config)
    try:
        pcm = collect_pcm(
            chunks,
            max_bytes=config.max_pcm_payload_bytes,
            deadline=time.monotonic() + config.fish_tts_timeout,
        )
    except (requests.RequestException, ValueError) as exc:
        raise TtsSynthesisError(f"Fish fallback failed ({type(exc).__name__})") from None
    finally:
        chunks.close()
    return pcm, "fish-audio"


def iter_wav_pcm(wav_path: Path):
    """Read validated TTS audio without forwarding the WAV container to the device."""
    validate_playback_wav(wav_path)
    with wave.open(str(wav_path), "rb") as wav:
        while chunk := wav.readframes(24 * 1024):
            yield chunk


def validate_pcm_contract(sample_rate: int, channels: int, sample_width: int) -> None:
    if sample_rate != PCM_SAMPLE_RATE or channels != PCM_CHANNELS or sample_width != PCM_SAMPLE_WIDTH:
        raise ValueError(
            f"unsupported PCM format: rate={sample_rate} channels={channels} width={sample_width}"
        )


def condition_pcm_chunk(chunk: bytes, *, gain: float, limit: float) -> tuple[bytes, int]:
    if not chunk:
        return chunk, 0
    if len(chunk) % PCM_SAMPLE_WIDTH != 0:
        raise ValueError(f"invalid PCM payload size: {len(chunk)}")

    peak = int(32767 * limit)
    out = bytearray(len(chunk))
    limited = 0
    for offset in range(0, len(chunk), PCM_SAMPLE_WIDTH):
        sample = struct.unpack_from("<h", chunk, offset)[0]
        scaled = int(sample * gain)
        if scaled > peak:
            scaled = peak
            limited += 1
        elif scaled < -peak:
            scaled = -peak
            limited += 1
        struct.pack_into("<h", out, offset, scaled)
    return bytes(out), limited


def declick_pcm_segment(segment: bytes, previous_tail_sample: int | None, samples: int) -> tuple[bytes, int]:
    if previous_tail_sample is None or samples == 0:
        return segment, 0
    if len(segment) % PCM_SAMPLE_WIDTH != 0:
        raise ValueError(f"invalid PCM payload size: {len(segment)}")

    sample_count = len(segment) // PCM_SAMPLE_WIDTH
    ramp_samples = min(samples, sample_count)
    out = bytearray(segment)
    for index in range(ramp_samples):
        current = struct.unpack_from("<h", segment, index * PCM_SAMPLE_WIDTH)[0]
        weight = (index + 1) / (ramp_samples + 1)
        smoothed = round(previous_tail_sample + (current - previous_tail_sample) * weight)
        struct.pack_into("<h", out, index * PCM_SAMPLE_WIDTH, smoothed)
    return bytes(out), ramp_samples


def choose_pcm_segment_cut(buffer: bytearray, target_bytes: int, zero_cross_window: int) -> int:
    target_bytes -= target_bytes % PCM_SAMPLE_WIDTH
    if len(buffer) <= target_bytes or zero_cross_window == 0:
        return target_bytes

    target_sample = target_bytes // PCM_SAMPLE_WIDTH
    start_sample = max(1, target_sample - zero_cross_window)
    best_sample = target_sample
    best_score = abs(struct.unpack_from("<h", buffer, target_bytes - PCM_SAMPLE_WIDTH)[0])

    for sample_index in range(start_sample, target_sample):
        prev_sample = struct.unpack_from("<h", buffer, (sample_index - 1) * PCM_SAMPLE_WIDTH)[0]
        sample = struct.unpack_from("<h", buffer, sample_index * PCM_SAMPLE_WIDTH)[0]
        score = abs(sample)
        if (prev_sample <= 0 <= sample) or (prev_sample >= 0 >= sample):
            score = -1
        if score < best_score:
            best_score = score
            best_sample = sample_index
            if score == -1:
                break

    return max(PCM_SAMPLE_WIDTH, best_sample * PCM_SAMPLE_WIDTH)


def iter_fish_pcm_stream(text: str, lang: str, config: StackchanConfig):
    validate_pcm_contract(PCM_SAMPLE_RATE, PCM_CHANNELS, PCM_SAMPLE_WIDTH)
    model_id = config.fish_audio_model_zh if lang == "zh" else config.fish_audio_model_en
    resp = requests.post(
        "https://api.fish.audio/v1/tts",
        headers={
            "Authorization": f"Bearer {config.fish_audio_key}",
            "Content-Type": "application/json",
            "Accept": PCM_CONTENT_TYPE,
        },
        json={
            "text": text,
            "reference_id": model_id,
            "format": "pcm",
            "sample_rate": PCM_SAMPLE_RATE,
        },
        stream=True,
        timeout=config.fish_tts_timeout,
    )
    try:
        raise_for_fish_status(resp)
        for chunk in resp.iter_content(chunk_size=config.fish_stream_chunk_bytes):
            if chunk:
                yield chunk
    finally:
        resp.close()


def transcribe_audio(wav_path: Path, lang: str, config: StackchanConfig) -> dict:
    if not config.fish_audio_key or config.fish_audio_key.startswith("replace-"):
        asr_engine = os.environ.get("STACKCHAN_ASR_ENGINE", "whisper").strip().lower()
        if asr_engine != "google" and _WhisperModel is not None:
            try:
                t0 = time.perf_counter()
                with wave.open(str(wav_path), "rb") as w:
                    sr_hz = w.getframerate()
                    n_frames = w.getnframes()
                    pcm = np.frombuffer(w.readframes(n_frames), dtype=np.int16)
                duration = n_frames / float(sr_hz) if sr_hz else 0.0
                if len(pcm) < 1600:
                    return {"text": "", "language": lang, "duration": duration}
                rms = float(np.sqrt(np.mean(pcm.astype(np.float32) ** 2)))
                if rms < 110.0:
                    return {"text": "", "language": lang, "duration": duration}
                audio_f32 = pcm.astype(np.float32) / 32768.0
                if sr_hz and sr_hz != 16000:
                    n_out = int(round(len(audio_f32) * 16000 / sr_hz))
                    xp = np.linspace(0.0, float(len(audio_f32) - 1), len(audio_f32), dtype=np.float32)
                    x_new = np.linspace(0.0, float(len(audio_f32) - 1), n_out, dtype=np.float32)
                    audio_f32 = np.interp(x_new, xp, audio_f32).astype(np.float32)
                model = _get_whisper_model()
                normalized_lang = (lang or "").strip().lower()
                whisper_lang = None if normalized_lang in ("", "auto") else normalized_lang.split("-")[0]

                def _run_whisper(target_lang: str | None):
                    prompt = (
                        "Hola Nicolás, StackChan, Antigravity, MCP, firmware."
                        if target_lang == "es"
                        else "StackChan, Antigravity, MCP, firmware."
                    )
                    segs_iter, tr_info = model.transcribe(
                        audio_f32,
                        language=target_lang,
                        beam_size=1,
                        temperature=0.0,
                        vad_filter=True,
                        vad_parameters={"min_silence_duration_ms": 250},
                        without_timestamps=True,
                        condition_on_previous_text=False,
                        initial_prompt=prompt,
                    )
                    dur_vad = getattr(tr_info, "duration_after_vad", None)
                    if dur_vad is not None and dur_vad < 0.18:
                        return [], tr_info
                    return list(segs_iter), tr_info

                segs_list, info = _run_whisper(whisper_lang)
                detected_lang = whisper_lang or (getattr(info, "language", None) or "es")
                lang_prob = float(getattr(info, "language_probability", 1.0) or 0.0)

                if whisper_lang is None and segs_list and detected_lang not in _AUTO_PRIMARY_LANGS and lang_prob < 0.85:
                    all_probs = getattr(info, "all_language_probs", None) or []
                    fallback_lang = next(
                        (code for code, _prob in all_probs if code in _AUTO_PRIMARY_LANGS),
                        "es",
                    )
                    segs_list, info = _run_whisper(fallback_lang)
                    detected_lang = fallback_lang
                    lang_prob = float(getattr(info, "language_probability", 1.0) or 0.0)

                valid_segs = [
                    s
                    for s in segs_list
                    if getattr(s, "no_speech_prob", 0.0) <= 0.75
                    and getattr(s, "avg_logprob", 0.0) >= -1.25
                ]
                if whisper_lang is None and lang_prob < 0.35 and all(
                    getattr(s, "avg_logprob", 0.0) < -0.9 for s in valid_segs
                ):
                    valid_segs = []

                text = " ".join(s.text.strip() for s in valid_segs).strip()
                norm = text.lower().strip(" .!?,¡¿")
                if norm in _HALLUCINATIONS:
                    text = ""
                with open(AUDIO_DIR / "debug.txt", "a", encoding="utf-8") as _df:
                    _df.write(
                        f"{time.strftime('%X')} [asr_whisper] {time.perf_counter() - t0:.3f}s "
                        f"lang={detected_lang} prob={lang_prob:.2f} dur={duration:.1f}s text={text!r}\n"
                    )
                return {"text": text, "language": detected_lang, "duration": duration}
            except Exception as exc:
                logger.warning("Local Whisper failed, falling back to Google STT: %s", exc)
        try:
            import speech_recognition as sr

            r = sr.Recognizer()
            with sr.AudioFile(str(wav_path)) as source:
                audio = r.record(source)
            stt_lang = "es-ES" if lang.startswith("es") else ("zh-CN" if lang == "zh" else "en-US")
            text = r.recognize_google(audio, language=stt_lang)  # type: ignore[attr-defined]
            duration = 0.0
            with suppress(Exception), wave.open(str(wav_path), "rb") as w:
                duration = w.getnframes() / float(w.getframerate())
            return {"text": text, "language": stt_lang, "duration": duration}
        except Exception as exc:
            logger.warning("Speech recognition failed: %s", exc)
            return {"text": "", "error": str(exc)}

    with open(wav_path, "rb") as f:
        resp = requests.post(
            "https://api.fish.audio/v1/asr",
            headers={"Authorization": f"Bearer {config.fish_audio_key}"},
            files={"audio": f},
            data={"language": lang},
            timeout=config.fish_asr_timeout,
        )
    resp.raise_for_status()
    return resp.json()
