from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import socket
import struct
import subprocess
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

import requests

from app.config import AppConfig, CONFIG_PATH, DATA_DIR
from app.tools import CREATE_NO_WINDOW, get_ffmpeg_path, get_ffprobe_path, probe_video


# ------------------------------------------------------------------ #7 MP4 / MKV Doctor
def diagnose_and_repair_video(input_path: str) -> Tuple[bool, str, str]:
    """
    Losslessly remux & repair broken/truncated MP4/MKV/TS recordings so they open
    cleanly in Premiere Pro, DaVinci Resolve, and CapCut.
    Returns (ok, output_path, summary_message).
    """
    ffmpeg = get_ffmpeg_path()
    if not ffmpeg:
        return False, "", "FFmpeg не найден"
    if not input_path or not os.path.exists(input_path):
        return False, "", "Файл не найден"

    src = Path(input_path)
    out = str(src.with_name(f"{src.stem}_repaired.mp4"))
    cmd = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "warning",
        "-err_detect", "ignore_err",
        "-fflags", "+genpts+igndts+discardcorrupt",
        "-i", input_path,
        "-map", "0:v:0", "-map", "0:a:0?",
        "-c", "copy",
        "-movflags", "+faststart",
        out,
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=1800, creationflags=CREATE_NO_WINDOW)
        if res.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 10_000:
            info = probe_video(out)
            w, h = int(info.get("width", 0)), int(info.get("height", 0))
            dur = info.get("duration", 0.0)
            return True, out, f"Файл восстановлен: {Path(out).name} ({w}×{h}, {int(dur)}с)"
        err = (res.stderr or "Не удалось восстановить файл").strip().splitlines()[-1]
        return False, "", err[:180]
    except Exception as exc:
        return False, "", str(exc)


# ------------------------------------------------------------------ #3 Auto Cloud Upload (WebDAV / Yandex.Disk / NAS / Folder)
def upload_file_to_cloud_storage(file_path: str, cfg: AppConfig) -> Tuple[bool, str]:
    """Upload a finished recording to WebDAV (Yandex.Disk/Nextcloud/NAS) or mounted cloud folder."""
    if not file_path or not os.path.exists(file_path):
        return False, "Файл не найден"
    fname = Path(file_path).name

    webdav_url = (getattr(cfg, "cloud_webdav_url", "") or "").strip()
    if webdav_url.startswith(("http://", "https://")):
        user = (getattr(cfg, "cloud_webdav_user", "") or "").strip()
        pwd = (getattr(cfg, "cloud_webdav_pass", "") or "").strip()
        dest_url = f"{webdav_url.rstrip('/')}/{fname}"
        try:
            with open(file_path, "rb") as f:
                r = requests.put(
                    dest_url,
                    data=f,
                    auth=(user, pwd) if user else None,
                    timeout=1800,
                )
            if r.status_code in (200, 201, 204):
                if getattr(cfg, "cloud_delete_local_after_upload", False):
                    try:
                        os.remove(file_path)
                    except Exception:
                        pass
                return True, f"Загружено в облако (WebDAV): {fname}"
            return False, f"WebDAV ошибка ({r.status_code}): {r.text[:140]}"
        except Exception as exc:
            return False, f"WebDAV: {exc}"

    folder = (getattr(cfg, "cloud_upload_dir", "") or "").strip()
    if folder:
        try:
            dest_dir = Path(folder)
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest_file = dest_dir / fname
            shutil.copy2(file_path, dest_file)
            if getattr(cfg, "cloud_delete_local_after_upload", False) and dest_file.exists():
                try:
                    os.remove(file_path)
                except Exception:
                    pass
            return True, f"Скопировано в облачную папку: {dest_file}"
        except Exception as exc:
            return False, f"Облачная папка: {exc}"

    return False, "Не указан WebDAV URL или облачная папка в Настройках"


# ------------------------------------------------------------------ #2 Scheduled Posts (Content Calendar)
SCHEDULED_POSTS_PATH = DATA_DIR / "scheduled_posts.json"


def load_scheduled_posts() -> List[dict]:
    if not SCHEDULED_POSTS_PATH.exists():
        return []
    try:
        data = json.loads(SCHEDULED_POSTS_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_scheduled_posts(posts: List[dict]) -> None:
    try:
        SCHEDULED_POSTS_PATH.write_text(json.dumps(posts, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


# ------------------------------------------------------------------ #9 Workspaces / Project Profiles
WORKSPACES_DIR = DATA_DIR / "workspaces"
ACTIVE_WS_FILE = DATA_DIR / "active_workspace.txt"


def list_workspaces() -> Tuple[str, List[str]]:
    WORKSPACES_DIR.mkdir(parents=True, exist_ok=True)
    active = "Основной"
    if ACTIVE_WS_FILE.exists():
        try:
            active = ACTIVE_WS_FILE.read_text(encoding="utf-8").strip() or "Основной"
        except Exception:
            pass
    names = ["Основной"]
    for p in sorted(WORKSPACES_DIR.glob("*.json")):
        if p.stem not in names:
            names.append(p.stem)
    if active not in names:
        names.append(active)
    return active, names


def save_current_to_workspace(name: str, cfg: AppConfig) -> None:
    WORKSPACES_DIR.mkdir(parents=True, exist_ok=True)
    cfg.save()
    if name != "Основной":
        safe = "".join(c for c in name if c.isalnum() or c in " _-").strip() or "profile"
        shutil.copy2(CONFIG_PATH, WORKSPACES_DIR / f"{safe}.json")
    else:
        shutil.copy2(CONFIG_PATH, WORKSPACES_DIR / "Основной.json")


def switch_workspace_config(current_name: str, target_name: str, current_cfg: AppConfig) -> AppConfig:
    WORKSPACES_DIR.mkdir(parents=True, exist_ok=True)
    # Persist current workspace snapshot first
    save_current_to_workspace(current_name, current_cfg)

    safe_target = "".join(c for c in target_name if c.isalnum() or c in " _-").strip() or "Основной"
    target_file = WORKSPACES_DIR / f"{safe_target}.json"
    if target_file.exists():
        shutil.copy2(target_file, CONFIG_PATH)
        new_cfg = AppConfig.load()
    else:
        # New workspace starts with clean channels but inherits system/encoder/output settings
        new_cfg = AppConfig.load()
        new_cfg.channels = []
        new_cfg.save()
        shutil.copy2(CONFIG_PATH, target_file)
    ACTIVE_WS_FILE.write_text(safe_target, encoding="utf-8")
    return new_cfg


# ------------------------------------------------------------------ #6 OBS Studio WebSocket v5 Client (Pure Python!)
class OBSWebSocketClient:
    """Zero-dependency pure-Python RFC6455 WebSocket client for OBS Studio 28+ (obs-websocket v5)."""

    def __init__(self, host: str = "127.0.0.1", port: int = 4455, password: str = ""):
        self.host = host or "127.0.0.1"
        self.port = int(port or 4455)
        self.password = password or ""

    def _ws_send(self, sock: socket.socket, text: str) -> None:
        payload = text.encode("utf-8")
        mask = os.urandom(4)
        header = bytearray([0x81])
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", n))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", n))
        header.extend(mask)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        sock.sendall(bytes(header) + masked)

    def _ws_recv(self, sock: socket.socket) -> str:
        def _exact(k: int) -> bytes:
            buf = b""
            while len(buf) < k:
                ch = sock.recv(k - len(buf))
                if not ch:
                    raise EOFError("OBS WebSocket closed")
                buf += ch
            return buf

        hdr = _exact(2)
        length = hdr[1] & 0x7F
        if length == 126:
            length = struct.unpack("!H", _exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", _exact(8))[0]
        if hdr[1] & 0x80:
            mask = _exact(4)
            data = _exact(length)
            data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        else:
            data = _exact(length)
        return data.decode("utf-8", errors="ignore")

    def send_request(self, request_type: str, request_data: Optional[dict] = None) -> Tuple[bool, dict, str]:
        """Connect, authenticate (obs-websocket v5), execute one request, and close cleanly."""
        sock = socket.create_connection((self.host, self.port), timeout=4.0)
        try:
            key = base64.b64encode(os.urandom(16)).decode("ascii")
            handshake = (
                f"GET / HTTP/1.1\r\n"
                f"Host: {self.host}:{self.port}\r\n"
                f"Upgrade: websocket\r\n"
                f"Connection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\n"
                f"Sec-WebSocket-Version: 13\r\n\r\n"
            )
            sock.sendall(handshake.encode("ascii"))
            resp = b""
            while b"\r\n\r\n" not in resp:
                resp += sock.recv(4096)
            if b"101" not in resp.splitlines()[0]:
                return False, {}, "OBS отклонил WebSocket handshake"

            hello = json.loads(self._ws_recv(sock))
            auth_info = (hello.get("d") or {}).get("authentication")
            ident_d: Dict[str, Any] = {"rpcVersion": 1}
            if auth_info:
                salt = auth_info.get("salt", "")
                challenge = auth_info.get("challenge", "")
                secret = base64.b64encode(hashlib.sha256((self.password + salt).encode("utf-8")).digest()).decode("ascii")
                auth_str = base64.b64encode(hashlib.sha256((secret + challenge).encode("utf-8")).digest()).decode("ascii")
                ident_d["authentication"] = auth_str

            self._ws_send(sock, json.dumps({"op": 1, "d": ident_d}))
            ident_resp = json.loads(self._ws_recv(sock))
            if ident_resp.get("op") != 2:
                return False, {}, "Неверный пароль OBS WebSocket"

            req_id = f"tr_{int(time.time() * 1000)}"
            req_d: Dict[str, Any] = {"requestType": request_type, "requestId": req_id}
            if request_data:
                req_d["requestData"] = request_data
            self._ws_send(sock, json.dumps({"op": 6, "d": req_d}))

            for _ in range(10):
                msg = json.loads(self._ws_recv(sock))
                if msg.get("op") == 7 and (msg.get("d") or {}).get("requestId") == req_id:
                    st = (msg["d"].get("requestStatus") or {})
                    ok = bool(st.get("result"))
                    return ok, msg["d"].get("responseData") or {}, st.get("comment") or ("OK" if ok else "Ошибка OBS")
            return False, {}, "Нет ответа от OBS"
        except Exception as exc:
            return False, {}, f"Не удалось подключиться к OBS ({self.host}:{self.port}): {exc}"
        finally:
            try:
                sock.close()
            except Exception:
                pass


# ------------------------------------------------------------------ #11 Stream & Clip Analytics Dashboard
def compute_analytics_dashboard(output_dir: str) -> dict:
    """Aggregate recordings, hours, storage, top streamers, top games, and daily activity."""
    root = Path(output_dir) if output_dir else Path(".")
    total_files = 0
    total_bytes = 0
    total_sec = 0.0
    total_markers = 0
    total_chat_msgs = 0
    by_channel: Dict[str, dict] = {}
    by_game: Dict[str, float] = {}
    by_day: Dict[str, float] = {}

    video_exts = {".mp4", ".mkv", ".ts", ".mov", ".webm"}
    if root.exists():
        for p in root.rglob("*"):
            if not p.is_file() or p.suffix.lower() not in video_exts:
                continue
            total_files += 1
            try:
                st = p.stat()
                total_bytes += st.st_size
                day = datetime.fromtimestamp(st.st_mtime).strftime("%d.%m")
            except Exception:
                day = "—"

            dur = 0.0
            ch_name = p.parent.name if p.parent != root else "Общее"
            game = ""
            meta_p = p.with_suffix(".meta.json")
            if meta_p.exists():
                try:
                    md = json.loads(meta_p.read_text(encoding="utf-8"))
                    dur = float(md.get("duration_sec") or 0.0)
                    ch_name = str(md.get("channel") or md.get("username") or ch_name)
                    game = str(md.get("game") or "").strip()
                    total_markers += int(md.get("markers_count") or 0)
                except Exception:
                    pass
            if dur <= 0:
                # Fast bitrate estimate (~5 Mbps) if no meta.json so analytics stays instant
                dur = max(10.0, (st.st_size / (625_000.0)))

            total_sec += dur
            by_day[day] = round(by_day.get(day, 0.0) + dur / 3600.0, 2)
            ch_entry = by_channel.setdefault(ch_name, {"name": ch_name, "hours": 0.0, "files": 0, "gb": 0.0})
            ch_entry["hours"] = round(ch_entry["hours"] + dur / 3600.0, 2)
            ch_entry["files"] += 1
            ch_entry["gb"] = round(ch_entry["gb"] + st.st_size / (1024 ** 3), 2)
            if game:
                by_game[game] = round(by_game.get(game, 0.0) + dur / 3600.0, 2)

            chat_p = p.with_suffix(".chat.json")
            if chat_p.exists():
                try:
                    c_sz = chat_p.stat().st_size
                    total_chat_msgs += max(1, int(c_sz / 95))
                except Exception:
                    pass

    top_channels = sorted(by_channel.values(), key=lambda x: x["hours"], reverse=True)[:8]
    top_games = [{"name": k, "hours": v} for k, v in sorted(by_game.items(), key=lambda x: x[1], reverse=True)[:8]]
    daily = [{"day": k, "hours": v} for k, v in list(by_day.items())[-14:]]

    return {
        "total_files": total_files,
        "total_hours": round(total_sec / 3600.0, 1),
        "total_gb": round(total_bytes / (1024 ** 3), 2),
        "total_markers": total_markers,
        "total_chat_msgs": total_chat_msgs,
        "top_channels": top_channels,
        "top_games": top_games,
        "daily": daily,
    }


# ------------------------------------------------------------------ #8 Mobile Wi-Fi Remote Control Server
def get_local_lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


MOBILE_REMOTE_HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1" />
<title>Twitch Recorder Pro — Пульт</title>
<style>
  body{margin:0;background:#07080d;color:#f4f5f9;font-family:-apple-system,BlinkMacSystemFont,sans-serif;padding:16px;}
  h1{font-size:20px;margin:0 0 4px;} .sub{color:#989db3;font-size:13px;margin-bottom:16px;}
  .card{background:#12151f;border:1px solid rgba(255,255,255,.09);border-radius:14px;padding:14px;margin-bottom:12px;}
  .row{display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap;}
  .btn{border:none;border-radius:10px;padding:10px 14px;font-weight:700;font-size:13px;cursor:pointer;color:#fff;background:#8b5cf6;}
  .btn.red{background:#ef4444;} .btn.green{background:#22c55e;} .btn.dark{background:#1f2436;}
  .pill{font-size:11px;font-weight:700;padding:3px 8px;border-radius:6px;background:rgba(255,255,255,.08);}
  .pill.live{background:rgba(34,197,94,.2);color:#4ade80;} .pill.rec{background:rgba(239,68,68,.25);color:#fca5a5;}
</style>
</head>
<body>
  <h1>📱 Twitch Recorder Pro</h1>
  <div class="sub" id="meta">Подключение к ПК...</div>
  <div class="row" style="margin-bottom:14px;">
    <button class="btn green" onclick="act('rec_all')">● Записать всех</button>
    <button class="btn red" onclick="act('stop_all')">■ Стоп всем</button>
    <button class="btn dark" onclick="load()">↻ Обновить</button>
  </div>
  <div id="list"></div>
<script>
const pin = new URLSearchParams(location.search).get('pin') || '';
async function act(cmd, key=''){
  await fetch(`/api/action?pin=${encodeURIComponent(pin)}&cmd=${encodeURIComponent(cmd)}&key=${encodeURIComponent(key)}`, {method:'POST'});
  setTimeout(load, 400);
}
async function load(){
  try{
    const r = await fetch(`/api/state?pin=${encodeURIComponent(pin)}`);
    if(!r.ok){ document.getElementById('meta').textContent = 'Неверный PIN-код'; return; }
    const d = await r.json();
    document.getElementById('meta').textContent = `Каналов: ${d.channels.length} · Активных задач: ${d.active_count}`;
    document.getElementById('list').innerHTML = d.channels.map(c => `
      <div class="card">
        <div class="row" style="margin-bottom:10px;">
          <div><strong>${c.username}</strong> <span class="pill ${c.is_recording?'rec':(c.is_live?'live':'')}">${c.is_recording?'ЗАПИСЬ':(c.is_live?'В ЭФИРЕ':'ОФФЛАЙН')}</span></div>
          <span style="font-size:12px;color:#989db3;">${c.game||''}</span>
        </div>
        <div class="row">
          ${c.is_recording
            ? `<button class="btn red" onclick="act('stop','${c.key}')">■ Стоп</button>
               <button class="btn" onclick="act('marker','${c.key}')">🔖 Маркер</button>
               <button class="btn dark" onclick="act('dvr','${c.key}')">✂ Откат 60с</button>`
            : `<button class="btn green" onclick="act('rec','${c.key}')">● Запись</button>`}
        </div>
      </div>
    `).join('');
  }catch(e){ document.getElementById('meta').textContent = 'Нет связи с приложением'; }
}
load(); setInterval(load, 4000);
</script>
</body>
</html>"""


class MobileRemoteServer:
    """Optional LAN Wi-Fi Remote Control Server (PIN-protected) for iPhone/Android."""

    def __init__(self, get_state_fn: Callable[[], dict], action_fn: Callable[[str, str], None]):
        self._get_state_fn = get_state_fn
        self._action_fn = action_fn
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self.port: int = 8765
        self.pin: str = "1234"
        self.running: bool = False

    def start(self, port: int = 8765, pin: str = "1234") -> Tuple[bool, str]:
        self.stop()
        self.port = int(port or 8765)
        self.pin = (pin or "1234").strip()
        parent = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def _ok(self, ctype: str, data: bytes, code: int = 200):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                u = urlparse(self.path)
                qs = parse_qs(u.query)
                if u.path in ("/", "/index.html"):
                    self._ok("text/html; charset=utf-8", MOBILE_REMOTE_HTML.encode("utf-8"))
                    return
                if u.path == "/api/state":
                    if parent.pin and qs.get("pin", [""])[0] != parent.pin:
                        self._ok("application/json", b'{"error":"bad pin"}', 403)
                        return
                    data = json.dumps(parent._get_state_fn(), ensure_ascii=False).encode("utf-8")
                    self._ok("application/json; charset=utf-8", data)
                    return
                self._ok("text/plain", b"Not found", 404)

            def do_POST(self):
                u = urlparse(self.path)
                qs = parse_qs(u.query)
                if u.path == "/api/action":
                    if parent.pin and qs.get("pin", [""])[0] != parent.pin:
                        self._ok("application/json", b'{"error":"bad pin"}', 403)
                        return
                    cmd = qs.get("cmd", [""])[0]
                    key = qs.get("key", [""])[0]
                    parent._action_fn(cmd, key)
                    self._ok("application/json", b'{"ok":true}')
                    return
                self._ok("text/plain", b"Not found", 404)

        try:
            self._server = ThreadingHTTPServer(("0.0.0.0", self.port), Handler)
            self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
            self._thread.start()
            self.running = True
            ip = get_local_lan_ip()
            return True, f"http://{ip}:{self.port}/?pin={self.pin}"
        except Exception as exc:
            self.running = False
            return False, str(exc)

    def stop(self) -> None:
        self.running = False
        if self._server:
            try:
                self._server.shutdown()
                self._server.server_close()
            except Exception:
                pass
            self._server = None
