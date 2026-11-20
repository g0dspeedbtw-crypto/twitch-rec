from __future__ import annotations

import hashlib
import mimetypes
import os
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Dict, Optional
from urllib.parse import parse_qs, unquote, urlparse

from app.config import get_resource_dir
from app.tools import CREATE_NO_WINDOW, extract_frame_from_file, get_ffmpeg_path, get_streamlink_cmd

WEB_DIR = get_resource_dir() / "web"
THUMB_CACHE_DIR = Path(tempfile.gettempdir()) / "twitch_recorder_thumbs"
THUMB_CACHE_DIR.mkdir(parents=True, exist_ok=True)


class LocalMediaServer:
    """
    Lightweight local HTTP server bound strictly to 127.0.0.1 on an ephemeral port.
    Serves:
      - Static web UI assets from web/
      - Local video/image files with HTTP Range (206 Partial Content) for instant 60fps <video> seeking
      - Cached video thumbnails for the Library grid
      - In-memory live stream preview thumbnails
    """

    def __init__(
        self,
        allowed_roots_fn: Callable[[], list[Path]],
        allowed_files_fn: Optional[Callable[[], set[Path]]] = None,
    ):
        self._allowed_roots_fn = allowed_roots_fn
        self._allowed_files_fn = allowed_files_fn
        self.live_previews: Dict[str, bytes] = {}
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self.port: int = 0

    def is_path_allowed(self, file_path: str) -> bool:
        try:
            target = Path(file_path).resolve(strict=True)
        except Exception:
            return False
        if not target.is_file():
            return False
        if self._allowed_files_fn:
            try:
                allowed_files = self._allowed_files_fn()
                if target in allowed_files:
                    return True
            except Exception:
                pass
        try:
            roots = self._allowed_roots_fn()
        except Exception:
            roots = []
        for root in roots:
            try:
                if target.is_relative_to(Path(root).resolve()):
                    return True
            except Exception:
                continue
        return False

    def set_live_preview(self, key: str, data: bytes) -> None:
        self.live_previews[key] = data

    def start(self) -> int:
        parent = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format, *args) -> None:
                pass  # Silence console spam

            def _send_bytes(self, code: int, content_type: str, data: bytes, cache: str = "no-cache") -> None:
                self.send_response(code)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", cache)
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:
                parsed = urlparse(self.path)
                path = parsed.path
                qs = parse_qs(parsed.query)

                # 0. Official Qt QWebChannel JS served over same-origin HTTP
                if path == "/qwebchannel.js":
                    from PySide6.QtCore import QFile, QIODevice
                    qf = QFile(":/qtwebchannel/qwebchannel.js")
                    if qf.open(QIODevice.ReadOnly):
                        data = bytes(qf.readAll())
                        qf.close()
                        self._send_bytes(200, "application/javascript; charset=utf-8", data, "max-age=3600")
                    else:
                        self._send_bytes(404, "text/plain", b"qwebchannel.js not found")
                    return

                # 1. Live channel preview image
                if path.startswith("/media/live/"):
                    key = unquote(path[len("/media/live/") :])
                    raw = parent.live_previews.get(key)
                    if raw:
                        self._send_bytes(200, "image/jpeg", raw, "no-store")
                    else:
                        self._send_bytes(404, "text/plain", b"No preview")
                    return

                # 1b. Live ad-free stream video/audio preview proxy (#8 Live Stream Mini-Player)
                if path == "/media/stream":
                    stream_url = qs.get("url", [""])[0].strip()
                    qual = qs.get("q", ["720p60,720p,best"])[0].strip()
                    if not stream_url.startswith(("http://", "https://")):
                        self._send_bytes(400, "text/plain", b"Invalid stream URL")
                        return
                    self._serve_live_stream_proxy(stream_url, qual)
                    return

                # 2. Cached thumbnail for a recorded video file
                if path == "/media/thumb":
                    file_path = qs.get("path", [""])[0]
                    if not file_path or not parent.is_path_allowed(file_path):
                        self._send_bytes(403 if file_path and os.path.isfile(file_path) else 404, "text/plain", b"Not found")
                        return
                    h = hashlib.md5(f"{file_path}:{os.path.getmtime(file_path)}".encode("utf-8")).hexdigest()
                    thumb = THUMB_CACHE_DIR / f"{h}.jpg"
                    if not thumb.exists():
                        ok, _ = extract_frame_from_file(file_path, 5.0, str(thumb))
                        if not ok:
                            extract_frame_from_file(file_path, 0.2, str(thumb))
                    if thumb.exists():
                        self._send_bytes(200, "image/jpeg", thumb.read_bytes(), "max-age=3600")
                    else:
                        self._send_bytes(404, "text/plain", b"Thumb failed")
                    return

                # 3. Local file streaming with HTTP 206 Range support (for <video> and images)
                if path == "/media/file":
                    file_path = qs.get("path", [""])[0]
                    if not file_path or not parent.is_path_allowed(file_path):
                        self._send_bytes(403 if file_path and os.path.isfile(file_path) else 404, "text/plain", b"File not found")
                        return
                    self._serve_file_ranged(file_path)
                    return

                # 4. Static web frontend assets
                rel = "index.html" if path in ("/", "") else path.lstrip("/")
                web_root = WEB_DIR.resolve()
                target = (WEB_DIR / rel).resolve()
                if not target.is_relative_to(web_root) or not target.is_file():
                    self._send_bytes(404, "text/plain", b"Not found")
                    return
                ctype, _ = mimetypes.guess_type(str(target))
                if target.suffix == ".js":
                    ctype = "application/javascript; charset=utf-8"
                elif target.suffix == ".css":
                    ctype = "text/css; charset=utf-8"
                elif target.suffix == ".html":
                    ctype = "text/html; charset=utf-8"
                elif target.suffix == ".svg":
                    ctype = "image/svg+xml"
                self._send_bytes(200, ctype or "application/octet-stream", target.read_bytes(), "no-cache")

            def _serve_file_ranged(self, file_path: str) -> None:
                size = os.path.getsize(file_path)
                ctype, _ = mimetypes.guess_type(file_path)
                if file_path.lower().endswith(".mkv"):
                    ctype = "video/x-matroska"
                ctype = ctype or "application/octet-stream"

                if size == 0:
                    self._send_bytes(200, ctype, b"", "no-store")
                    return

                range_hdr = self.headers.get("Range")
                start, end = 0, size - 1
                status = 200
                if range_hdr and range_hdr.startswith("bytes="):
                    try:
                        spec = range_hdr[6:].split(",")[0].strip()
                        s_str, e_str = spec.split("-")
                        start = int(s_str) if s_str else 0
                        end = int(e_str) if e_str else min(size - 1, start + 4 * 1024 * 1024)
                        end = min(end, size - 1)
                        if start > end or start >= size:
                            self.send_response(416)
                            self.send_header("Content-Range", f"bytes */{size}")
                            self.end_headers()
                            return
                        status = 206
                    except Exception:
                        start, end, status = 0, size - 1, 200

                length = end - start + 1
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(length))
                if status == 206:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()

                try:
                    with open(file_path, "rb") as f:
                        f.seek(start)
                        remaining = length
                        while remaining > 0:
                            chunk = f.read(min(262144, remaining))
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                            remaining -= len(chunk)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass

            def _serve_live_stream_proxy(self, stream_url: str, quality: str) -> None:
                import subprocess
                sl_cmd = get_streamlink_cmd()
                ffmpeg = get_ffmpeg_path()
                if not sl_cmd or not ffmpeg:
                    self._send_bytes(503, "text/plain", b"Streamlink/FFmpeg unavailable")
                    return
                sl_args = [*sl_cmd, "--stdout", "--quiet", "--twitch-disable-ads", stream_url, quality]
                ff_args = [
                    ffmpeg, "-hide_banner", "-loglevel", "error",
                    "-fflags", "+genpts+discardcorrupt", "-i", "pipe:0",
                    "-c", "copy", "-f", "mp4",
                    "-movflags", "+frag_keyframe+empty_moov+default_base_moof",
                    "pipe:1",
                ]
                sl_proc = None
                ff_proc = None
                try:
                    sl_proc = subprocess.Popen(
                        sl_args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW
                    )
                    ff_proc = subprocess.Popen(
                        ff_args, stdin=sl_proc.stdout, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                        creationflags=CREATE_NO_WINDOW,
                    )
                    if sl_proc.stdout:
                        sl_proc.stdout.close()
                    self.send_response(200)
                    self.send_header("Content-Type", "video/mp4")
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    assert ff_proc.stdout is not None
                    while True:
                        chunk = ff_proc.stdout.read(65536)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        self.wfile.flush()
                except Exception:
                    pass
                finally:
                    for p in (ff_proc, sl_proc):
                        if p and p.poll() is None:
                            try:
                                p.kill()
                            except Exception:
                                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self.port

    def stop(self) -> None:
        if self._server:
            try:
                self._server.shutdown()
                self._server.server_close()
            except Exception:
                pass
