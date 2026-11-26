import logging
import os
import queue
import struct
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .stackchan_config import DEFAULT_AUDIO_DIR

logger = logging.getLogger(__name__)

AUDIO_DIR = Path(os.environ.get("STACKCHAN_AUDIO_DIR", DEFAULT_AUDIO_DIR))
AUDIO_DIR.mkdir(parents=True, exist_ok=True)
AUDIO_DIR.chmod(0o700)
TEMP_AUDIO_DIR = AUDIO_DIR / ".tmp"
TEMP_AUDIO_DIR.mkdir(parents=True, exist_ok=True)
TEMP_AUDIO_DIR.chmod(0o700)

_http_server = None
_http_thread = None
_live_http_server = None
_live_http_thread = None
_live_http_port = 0

_LIVE_STREAMS_LOCK = threading.Lock()
_LIVE_STREAMS: dict[str, tuple[queue.Queue[bytes | None], int]] = {}


def make_streaming_wav_header(sample_rate: int = 24000, channels: int = 1, bits_per_sample: int = 16) -> bytes:
    """Build a 44-byte WAV header for unknown-length live PCM streaming."""
    byte_rate = sample_rate * channels * (bits_per_sample // 8)
    block_align = channels * (bits_per_sample // 8)
    return struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        0xFFFFFFFF,
        b"WAVE",
        b"fmt ",
        16,
        1,  # PCM
        channels,
        sample_rate,
        byte_rate,
        block_align,
        bits_per_sample,
        b"data",
        0xFFFFFFFF,
    )


def register_live_stream(stream_id: str, sample_rate: int = 24000) -> queue.Queue[bytes | None]:
    q: queue.Queue[bytes | None] = queue.Queue()
    with _LIVE_STREAMS_LOCK:
        _LIVE_STREAMS[stream_id] = (q, sample_rate)
    return q


def unregister_live_stream(stream_id: str) -> None:
    with _LIVE_STREAMS_LOCK:
        _LIVE_STREAMS.pop(stream_id, None)


class QuietHandler(SimpleHTTPRequestHandler):
    # Abandoned sockets (firmware reboot / WiFi drop mid-download) self-terminate
    # instead of blocking a handler thread forever.
    timeout = 20

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(AUDIO_DIR), **kwargs)

    def log_message(self, format, *args):
        pass

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        self.send_header("Pragma", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()

    def do_GET(self):
        if self.path.startswith("/live/"):
            stream_id = self.path[len("/live/") :].split("?", 1)[0]
            with _LIVE_STREAMS_LOCK:
                entry = _LIVE_STREAMS.get(stream_id)
            if entry is None:
                self.send_error(404, "Live stream not found")
                return
            q, sample_rate = entry
            self.protocol_version = "HTTP/1.1"
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("Connection", "close")
            self.end_headers()

            def write_chunk(data: bytes) -> None:
                if not data:
                    return
                self.wfile.write(f"{len(data):x}\r\n".encode("ascii"))
                self.wfile.write(data)
                self.wfile.write(b"\r\n")
                self.wfile.flush()

            try:
                write_chunk(make_streaming_wav_header(sample_rate=sample_rate))
                while True:
                    try:
                        chunk = q.get(timeout=15.0)
                    except queue.Empty:
                        break
                    if chunk is None:
                        break
                    write_chunk(chunk)
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                unregister_live_stream(stream_id)
            return
        super().do_GET()


def start_audio_server(port: int) -> None:
    global _http_server, _http_thread
    if _http_server is not None:
        return
    try:
        # ThreadingHTTPServer: one hung client connection must not starve the
        # rest — the single-threaded HTTPServer deadlocked playback whenever a
        # download was interrupted (root cause of the "one bullet" silence bug).
        _http_server = ThreadingHTTPServer(("0.0.0.0", port), QuietHandler)  # noqa: S104 - device must fetch host audio over LAN.
        _http_thread = threading.Thread(target=_http_server.serve_forever, daemon=True)
        _http_thread.start()
    except OSError as exc:
        logger.warning("Audio server not started on port %d: %s", port, exc)


def start_live_audio_server() -> int:
    """Bind a dedicated ephemeral HTTP port in this process for live chunked WAV streams."""
    global _live_http_server, _live_http_thread, _live_http_port
    if _live_http_server is not None and _live_http_port > 0:
        return _live_http_port
    _live_http_server = ThreadingHTTPServer(("0.0.0.0", 0), QuietHandler)  # noqa: S104 - device must fetch host audio over LAN.
    _live_http_port = int(_live_http_server.server_address[1])
    _live_http_thread = threading.Thread(target=_live_http_server.serve_forever, daemon=True)
    _live_http_thread.start()
    return _live_http_port


def audio_url(host: str, port: int, filename: str) -> str:
    return f"http://{host}:{port}/{filename}"
