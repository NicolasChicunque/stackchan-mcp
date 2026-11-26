import io
import json
import os
import sys
import time
import urllib.request
from contextlib import suppress
from pathlib import Path

import speech_recognition as sr

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mcp_server.stackchan_config import load_dotenv  # noqa: E402

load_dotenv()

IP = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("STACKCHAN_IP", "127.0.0.1")
WAIT_SPEAK = "--after-speak" in sys.argv
LANG = "es-ES"
MAX_WAIT = 600


def get_json(path):
    with urllib.request.urlopen(f"http://{IP}{path}", timeout=2.0) as resp:  # noqa: S310 - LAN device HTTP endpoint
        return json.loads(resp.read().decode("utf-8", "ignore"))


def fetch_audio():
    with urllib.request.urlopen(f"http://{IP}/audio", timeout=8.0) as resp:  # noqa: S310 - LAN device HTTP endpoint
        return resp.read()


if WAIT_SPEAK:
    t0 = time.monotonic()
    saw_playing = False
    while time.monotonic() - t0 < 20.0:
        with suppress(Exception):
            st = get_json("/playback/status")
            if st.get("playing"):
                saw_playing = True
            elif saw_playing:
                time.sleep(0.55)
                break
            elif time.monotonic() - t0 > 9.0:
                break
        time.sleep(0.25)

recognizer = sr.Recognizer()
deadline = time.monotonic() + MAX_WAIT

while time.monotonic() < deadline:
    with suppress(Exception):
        data = get_json("/audio/status")
        if data.get("ready"):
            wav_bytes = fetch_audio()
            if len(wav_bytes) < 6400:
                time.sleep(0.2)
                continue
            with sr.AudioFile(io.BytesIO(wav_bytes)) as src:
                audio = recognizer.record(src)
            try:
                text = recognizer.recognize_google(audio, language=LANG).strip()  # type: ignore[attr-defined]
            except (sr.UnknownValueError, sr.RequestError):
                text = ""
            if text:
                print(f"USER_SAID: {text}", flush=True)
                sys.exit(0)
    time.sleep(0.2)

print("TIMEOUT")
sys.exit(2)
