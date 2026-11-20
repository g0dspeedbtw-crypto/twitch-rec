from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, List, Optional, Tuple

import psutil
import requests
from PySide6.QtCore import QObject, QThread, QTimer, Signal

from app.config import BASE_DIR, CONFIG_PATH, AppConfig

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
BELOW_NORMAL_PRIORITY_CLASS = getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0x00004000)
RUN_REG_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
APP_REG_NAME = "TwitchRecorderPro"


def low_priority_popen_kwargs(low_priority: bool = True) -> dict:
    """Return cross-platform subprocess.Popen kwargs for background/low-priority rendering."""
    if os.name == "nt":
        flags = CREATE_NO_WINDOW | (BELOW_NORMAL_PRIORITY_CLASS if low_priority else 0)
        return {"creationflags": flags}
    if low_priority:
        def _set_nice() -> None:
            try:
                os.nice(10)
            except Exception:
                pass
        return {"preexec_fn": _set_nice}
    return {}


def detect_running_game(game_processes_csv: str) -> str:
    """Return name of a currently running game matching the CSV process list, or ''."""
    targets = {p.strip().lower() for p in (game_processes_csv or "").split(",") if p.strip()}
    if not targets:
        return ""
    try:
        for proc in psutil.process_iter(["name"]):
            name = (proc.info.get("name") or "").strip()
            if not name:
                continue
            low = name.lower()
            if low in targets or low.removesuffix(".exe") in targets:
                return name
    except Exception:
        pass
    return ""


# ------------------------------------------------------------------ #31 Windows & macOS Autostart
def set_windows_autostart(enabled: bool) -> Tuple[bool, str]:
    """Enable/disable autostart on login for both Windows (Registry) and macOS (LaunchAgent)."""
    if sys.platform == "darwin":
        plist_dir = Path.home() / "Library" / "LaunchAgents"
        plist_path = plist_dir / "com.twitchrecorder.pro.plist"
        try:
            if enabled:
                plist_dir.mkdir(parents=True, exist_ok=True)
                args = (
                    [sys.executable, "--minimized"]
                    if getattr(sys, "frozen", False)
                    else [sys.executable, str(BASE_DIR / "main.py"), "--minimized"]
                )
                args_xml = "\n".join(f"    <string>{a}</string>" for a in args)
                plist_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.twitchrecorder.pro</string>
  <key>ProgramArguments</key>
  <array>
{args_xml}
  </array>
  <key>RunAtLoad</key><true/>
</dict>
</plist>
"""
                plist_path.write_text(plist_xml, encoding="utf-8")
            else:
                if plist_path.exists():
                    plist_path.unlink()
            return True, "Автозапуск macOS обновлён"
        except Exception as exc:
            return False, str(exc)

    if os.name != "nt":
        return False, "Автозапуск не поддерживается на этой ОС"
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_REG_PATH, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                if getattr(sys, "frozen", False):
                    cmd = f'"{sys.executable}" --minimized'
                else:
                    pythonw = Path(sys.executable).with_name("pythonw.exe")
                    exe = str(pythonw if pythonw.exists() else Path(sys.executable))
                    script = str(BASE_DIR / "main.py")
                    cmd = f'"{exe}" "{script}" --minimized'
                winreg.SetValueEx(key, APP_REG_NAME, 0, winreg.REG_SZ, cmd)
            else:
                try:
                    winreg.DeleteValue(key, APP_REG_NAME)
                except FileNotFoundError:
                    pass
        return True, "Автозапуск Windows обновлён"
    except Exception as exc:
        return False, str(exc)


# ------------------------------------------------------------------ #20 Native OS Notifications (macOS & Windows)
def send_native_os_notification(title: str, message: str) -> None:
    """Display a native macOS Notification Center banner (with sound) or Windows tray notification."""
    if sys.platform == "darwin":
        def _mac_notify() -> None:
            try:
                safe_t = (title or "Twitch Recorder Pro").replace("\\", "\\\\").replace('"', '\\"')
                safe_m = (message or "").replace("\\", "\\\\").replace('"', '\\"').replace("\n", " · ")
                script = f'display notification "{safe_m}" with title "{safe_t}" sound name "Glass"'
                subprocess.run(["osascript", "-e", script], capture_output=True, timeout=5)
            except Exception:
                pass
        threading.Thread(target=_mac_notify, daemon=True).start()


# ------------------------------------------------------------------ #32 Telegram & Discord Notifier
def send_notification_async(
    config: AppConfig,
    title: str,
    message: str,
    image_path: Optional[str] = None,
    log_cb: Optional[Callable[[str], None]] = None,
) -> None:
    """Fire-and-forget notification to macOS/Windows Notification Center, Telegram Bot, and/or Discord Webhook."""
    if getattr(config, "native_os_notifications", True):
        send_native_os_notification(title, message)
    has_tg = bool(config.telegram_bot_token.strip() and config.telegram_chat_id.strip())
    has_dc = bool(config.discord_webhook_url.strip())
    if not has_tg and not has_dc:
        return

    def _worker() -> None:
        text = f"*{title}*\n{message}"
        proxies = {"http": config.proxy_url.strip(), "https": config.proxy_url.strip()} if config.proxy_url.strip() else None
        if has_tg:
            try:
                tok = config.telegram_bot_token.strip()
                cid = config.telegram_chat_id.strip()
                if image_path and os.path.exists(image_path):
                    url = f"https://api.telegram.org/bot{tok}/sendPhoto"
                    with open(image_path, "rb") as f:
                        requests.post(
                            url,
                            data={"chat_id": cid, "caption": f"{title}\n{message}"},
                            files={"photo": f},
                            timeout=12,
                            proxies=proxies,
                        )
                else:
                    url = f"https://api.telegram.org/bot{tok}/sendMessage"
                    requests.post(
                        url,
                        json={"chat_id": cid, "text": text, "parse_mode": "Markdown"},
                        timeout=10,
                        proxies=proxies,
                    )
            except Exception as exc:
                if log_cb:
                    log_cb(f"[Telegram] Ошибка отправки: {exc}")

        if has_dc:
            try:
                wh = config.discord_webhook_url.strip()
                payload = {"content": f"**{title}**\n{message}"}
                if image_path and os.path.exists(image_path):
                    with open(image_path, "rb") as f:
                        requests.post(
                            wh,
                            data={"content": payload["content"]},
                            files={"file": (os.path.basename(image_path), f, "image/jpeg")},
                            timeout=12,
                            proxies=proxies,
                        )
                else:
                    requests.post(wh, json=payload, timeout=10, proxies=proxies)
            except Exception as exc:
                if log_cb:
                    log_cb(f"[Discord] Ошибка отправки: {exc}")

    threading.Thread(target=_worker, daemon=True).start()


# ------------------------------------------------------------------ #14 Disk Guard & Cleanup
def get_disk_free_gb(path: str) -> float:
    try:
        p = Path(path)
        while not p.exists() and p.parent != p:
            p = p.parent
        usage = shutil.disk_usage(str(p))
        return usage.free / (1024 ** 3)
    except Exception:
        return 0.0


def cleanup_old_recordings(output_dir: str, max_age_days: int) -> Tuple[int, float]:
    """Delete video files older than max_age_days. Returns (deleted_count, freed_gb)."""
    root = Path(output_dir)
    if not root.exists():
        return 0, 0.0
    cutoff = time.time() - max(1, int(max_age_days)) * 86400
    deleted = 0
    freed_bytes = 0
    exts = {".mp4", ".mkv", ".ts", ".flv", ".mov"}
    for f in root.rglob("*"):
        if f.is_file() and f.suffix.lower() in exts:
            try:
                st = f.stat()
                if st.st_mtime < cutoff:
                    sz = st.st_size
                    f.unlink()
                    deleted += 1
                    freed_bytes += sz
                    # Remove companion chat/marker/meta/edl/subtitle files if present
                    for side in (
                        f.with_suffix(".chat.json"),
                        f.with_suffix(".chat.ass"),
                        f.with_suffix(".markers.json"),
                        f.with_suffix(".meta.json"),
                        f.with_suffix(".edl"),
                        f.with_suffix(".srt"),
                        f.with_suffix(".subs.ass"),
                    ):
                        if side.exists():
                            try:
                                side.unlink()
                            except Exception:
                                pass
            except Exception:
                pass
    return deleted, freed_bytes / (1024 ** 3)


# ------------------------------------------------------------------ #35 PC Resource Monitor & #43 Game Mode
@dataclass
class ResourceSnapshot:
    cpu_pct: float = 0.0
    ram_pct: float = 0.0
    disk_free_gb: float = 0.0
    gpu_pct: Optional[int] = None
    nvenc_pct: Optional[int] = None
    vram_used_mb: Optional[int] = None
    vram_total_mb: Optional[int] = None
    active_game: str = ""


class ResourceMonitorWorker(QThread):
    stats_ready = Signal(object)  # ResourceSnapshot

    def __init__(
        self,
        output_dir_fn: Callable[[], str],
        game_cfg_fn: Optional[Callable[[], Tuple[bool, str]]] = None,
        parent=None,
    ):
        super().__init__(parent)
        self._output_dir_fn = output_dir_fn
        self._game_cfg_fn = game_cfg_fn
        self._running = True
        self._has_nvidia_smi = shutil.which("nvidia-smi") is not None

    def stop(self) -> None:
        self._running = False

    def run(self) -> None:
        psutil.cpu_percent(interval=None)
        while self._running:
            active_game = ""
            if self._game_cfg_fn:
                try:
                    gm_on, gm_procs = self._game_cfg_fn()
                    if gm_on:
                        active_game = detect_running_game(gm_procs)
                except Exception:
                    pass

            snap = ResourceSnapshot(
                cpu_pct=psutil.cpu_percent(interval=None),
                ram_pct=psutil.virtual_memory().percent,
                disk_free_gb=get_disk_free_gb(self._output_dir_fn()),
                active_game=active_game,
            )
            if self._has_nvidia_smi:
                try:
                    res = subprocess.run(
                        [
                            "nvidia-smi",
                            "--query-gpu=utilization.gpu,utilization.encoder,memory.used,memory.total",
                            "--format=csv,noheader,nounits",
                        ],
                        capture_output=True,
                        text=True,
                        timeout=3,
                        creationflags=CREATE_NO_WINDOW,
                    )
                    if res.returncode == 0 and res.stdout.strip():
                        parts = [p.strip() for p in res.stdout.strip().splitlines()[0].split(",")]
                        if len(parts) >= 4:
                            snap.gpu_pct = int(parts[0])
                            snap.nvenc_pct = int(parts[1]) if parts[1].isdigit() else 0
                            snap.vram_used_mb = int(parts[2])
                            snap.vram_total_mb = int(parts[3])
                except Exception:
                    pass
            self.stats_ready.emit(snap)
            for _ in range(30):
                if not self._running:
                    break
                time.sleep(0.1)


# ------------------------------------------------------------------ #19 Global Hotkey Poller (Windows & macOS)
_VK_MAP = {
    "F1": 0x70, "F2": 0x71, "F3": 0x72, "F4": 0x73, "F5": 0x74, "F6": 0x75,
    "F7": 0x76, "F8": 0x77, "F9": 0x78, "F10": 0x79, "F11": 0x7A, "F12": 0x7B,
}
_MAC_KEYCODE_MAP = {
    "F1": 122, "F2": 120, "F3": 99, "F4": 118, "F5": 96, "F6": 97,
    "F7": 98, "F8": 100, "F9": 101, "F10": 109, "F11": 103, "F12": 111,
}


class GlobalHotkeyMonitor(QObject):
    """Lightweight global Windows & macOS hotkey detector (e.g. F9 to drop a live highlight marker)."""

    triggered = Signal()

    def __init__(self, key_name: str = "F9", parent=None):
        super().__init__(parent)
        self._vk = _VK_MAP.get(key_name.upper(), 0x78)
        self._mac_kc = _MAC_KEYCODE_MAP.get(key_name.upper(), 101)
        self._cg = None
        if sys.platform == "darwin":
            try:
                self._cg = ctypes.cdll.LoadLibrary(
                    "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
                )
                self._cg.CGEventSourceKeyState.argtypes = [ctypes.c_uint32, ctypes.c_uint16]
                self._cg.CGEventSourceKeyState.restype = ctypes.c_bool
            except Exception:
                self._cg = None
        self._was_down = False
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._poll)
        if os.name == "nt" or (sys.platform == "darwin" and self._cg is not None):
            self._timer.start(80)

    def set_key(self, key_name: str) -> None:
        self._vk = _VK_MAP.get(key_name.upper(), 0x78)
        self._mac_kc = _MAC_KEYCODE_MAP.get(key_name.upper(), 101)

    def _poll(self) -> None:
        try:
            if os.name == "nt":
                state = ctypes.windll.user32.GetAsyncKeyState(self._vk)
                is_down = bool(state & 0x8000)
            elif sys.platform == "darwin" and self._cg is not None:
                # kCGEventSourceStateCombinedSessionState = 0
                is_down = bool(self._cg.CGEventSourceKeyState(0, self._mac_kc))
            else:
                return
            if is_down and not self._was_down:
                self.triggered.emit()
            self._was_down = is_down
        except Exception:
            pass


# ------------------------------------------------------------------ #41 Interactive Telegram Remote Bot
def send_telegram_reply_async(
    token: str,
    chat_id: str,
    text: str,
    photo_path: Optional[str] = None,
    proxy_url: str = "",
) -> None:
    tok = (token or "").strip()
    cid = (chat_id or "").strip()
    if not tok or not cid:
        return
    proxies = {"http": proxy_url.strip(), "https": proxy_url.strip()} if proxy_url.strip() else None

    def _send() -> None:
        try:
            if photo_path and os.path.exists(photo_path):
                with open(photo_path, "rb") as f:
                    requests.post(
                        f"https://api.telegram.org/bot{tok}/sendPhoto",
                        data={"chat_id": cid, "caption": text[:1000]},
                        files={"photo": f},
                        timeout=15,
                        proxies=proxies,
                    )
            else:
                requests.post(
                    f"https://api.telegram.org/bot{tok}/sendMessage",
                    json={"chat_id": cid, "text": text[:4000]},
                    timeout=12,
                    proxies=proxies,
                )
        except Exception:
            pass

    threading.Thread(target=_send, daemon=True).start()


def send_telegram_video_async(
    token: str,
    chat_id: str,
    video_path: str,
    caption: str = "",
    proxy_url: str = "",
    done_cb: Optional[Callable[[bool, str], None]] = None,
) -> None:
    """#16 Upload a video/Shorts (.mp4 up to 50MB) directly to a Telegram chat or channel via Bot API."""
    tok = (token or "").strip()
    cid = (chat_id or "").strip()
    if not tok or not cid:
        if done_cb:
            done_cb(False, "Укажите Telegram Bot Token и Chat ID в Настройках")
        return
    if not video_path or not os.path.exists(video_path):
        if done_cb:
            done_cb(False, "Видеофайл не найден")
        return
    sz_mb = os.path.getsize(video_path) / (1024 * 1024)
    if sz_mb > 49.5:
        if done_cb:
            done_cb(False, f"Файл ({sz_mb:.1f} МБ) превышает лимит Telegram Bot API (50 МБ) — сначала сожмите через пресет «Telegram 50 МБ»")
        return
    proxies = {"http": proxy_url.strip(), "https": proxy_url.strip()} if proxy_url.strip() else None

    def _upload() -> None:
        try:
            with open(video_path, "rb") as f:
                r = requests.post(
                    f"https://api.telegram.org/bot{tok}/sendVideo",
                    data={"chat_id": cid, "caption": (caption or Path(video_path).name)[:1000], "supports_streaming": "true"},
                    files={"video": (Path(video_path).name, f, "video/mp4")},
                    timeout=180,
                    proxies=proxies,
                )
            if r.status_code == 200 and r.json().get("ok"):
                if done_cb:
                    done_cb(True, f"Видео опубликовано в Telegram ({sz_mb:.1f} МБ)")
            else:
                desc = (r.json().get("description") if r.headers.get("content-type", "").startswith("application/json") else r.text) or f"HTTP {r.status_code}"
                if done_cb:
                    done_cb(False, f"Ошибка Telegram API: {desc}")
        except Exception as exc:
            if done_cb:
                done_cb(False, f"Ошибка отправки видео: {exc}")

    threading.Thread(target=_upload, daemon=True).start()


# ------------------------------------------------------------------ #21 Cloud Config & Blur Presets Sync (GitHub Gist + Cloud Folder)
def sync_profile_cloud(
    config: AppConfig,
    direction: str = "push",  # "push" | "pull"
) -> Tuple[bool, str]:
    """
    Synchronize config.json (channels + blur scenes) via:
      1. Shared Cloud Folder (iCloud Drive / OneDrive / Dropbox path in config.cloud_sync_path), and/or
      2. Private GitHub Gist (via config.cloud_gist_token & config.cloud_gist_id).
    """
    config.save()
    proxies = {"http": config.proxy_url.strip(), "https": config.proxy_url.strip()} if config.proxy_url.strip() else None
    msgs: List[str] = []

    # 1. Local Cloud Folder sync (iCloud / Dropbox / OneDrive)
    sync_p_str = (config.cloud_sync_path or "").strip()
    if sync_p_str:
        target = Path(sync_p_str)
        if target.is_dir() or not target.suffix:
            target.mkdir(parents=True, exist_ok=True)
            target = target / "TwitchRecorderPro_sync_config.json"
        try:
            if direction == "push":
                target.write_text(CONFIG_PATH.read_text(encoding="utf-8"), encoding="utf-8")
                msgs.append(f"Сохранено в облачную папку ({target.name})")
            else:
                if not target.exists():
                    return False, f"Файл синхронизации не найден: {target}"
                CONFIG_PATH.write_text(target.read_text(encoding="utf-8"), encoding="utf-8")
                msgs.append(f"Загружено из облачной папки ({target.name})")
        except Exception as exc:
            return False, f"Ошибка облачной папки: {exc}"

    # 2. GitHub Gist Sync
    tok = (config.cloud_gist_token or "").strip()
    gist_id = (config.cloud_gist_id or "").strip()
    if tok:
        headers = {
            "Authorization": f"Bearer {tok}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "TwitchRecorderPro",
        }
        try:
            if direction == "push":
                raw_json = CONFIG_PATH.read_text(encoding="utf-8")
                payload = {
                    "description": "Twitch Recorder Pro — Cloud Profile & Blur Presets Sync",
                    "public": False,
                    "files": {"twitch_recorder_pro_config.json": {"content": raw_json}},
                }
                if gist_id:
                    r = requests.patch(f"https://api.github.com/gists/{gist_id}", json=payload, headers=headers, timeout=15, proxies=proxies)
                else:
                    r = requests.post("https://api.github.com/gists", json=payload, headers=headers, timeout=15, proxies=proxies)
                r.raise_for_status()
                new_id = str(r.json().get("id") or "")
                if new_id and new_id != gist_id:
                    config.cloud_gist_id = new_id
                    config.save()
                msgs.append(f"Синхронизировано с GitHub Gist ({new_id[:8]})")
            else:
                if not gist_id:
                    # Try finding an existing Gist automatically
                    r_list = requests.get("https://api.github.com/gists", headers=headers, timeout=15, proxies=proxies)
                    r_list.raise_for_status()
                    for g in r_list.json() or []:
                        if "twitch_recorder_pro_config.json" in (g.get("files") or {}):
                            gist_id = str(g.get("id") or "")
                            break
                if not gist_id:
                    return False, "Не найден Gist с профилем — сначала нажмите «Выгрузить в облако»"
                r = requests.get(f"https://api.github.com/gists/{gist_id}", headers=headers, timeout=15, proxies=proxies)
                r.raise_for_status()
                files = r.json().get("files") or {}
                f_obj = files.get("twitch_recorder_pro_config.json") or (next(iter(files.values())) if files else None)
                if not f_obj or not f_obj.get("content"):
                    return False, "В Gist отсутствует конфигурация"
                CONFIG_PATH.write_text(f_obj["content"], encoding="utf-8")
                config.cloud_gist_id = gist_id
                msgs.append(f"Загружено из GitHub Gist ({gist_id[:8]})")
        except Exception as exc:
            return False, f"Ошибка GitHub Gist: {exc}"

    if not msgs:
        return False, "Укажите папку облака (iCloud/Dropbox) или токен GitHub Gist в Настройках → Система"
    return True, " · ".join(msgs)


class TelegramBotWorker(QThread):
    """
    Lightweight interactive Telegram Bot long-polling worker (#41).
    Strictly accepts commands ONLY from the configured telegram_chat_id.
    Supported commands: /status, /rec <nick>, /stop <nick>, /stopall, /marker, /vod <url>, /snap <nick>, /help
    """

    command_received = Signal(str, str)  # cmd, arg
    log_message = Signal(str)

    def __init__(self, token: str, allowed_chat_id: str, proxy_url: str = "", parent=None):
        super().__init__(parent)
        self.token = token.strip()
        self.allowed_chat_id = str(allowed_chat_id).strip()
        self.proxy_url = proxy_url.strip()
        self._running = True
        self._offset = 0

    def stop(self) -> None:
        self._running = False

    def run(self) -> None:
        if not self.token or not self.allowed_chat_id:
            return
        proxies = {"http": self.proxy_url, "https": self.proxy_url} if self.proxy_url else None
        base_url = f"https://api.telegram.org/bot{self.token}/getUpdates"
        self.log_message.emit("[Telegram Bot] Удалённое управление запущено.")

        # Flush old queued updates on startup so stale commands aren't replayed
        try:
            r0 = requests.get(base_url, params={"timeout": 0, "offset": -1}, timeout=8, proxies=proxies)
            if r0.status_code == 200:
                res0 = r0.json().get("result") or []
                if res0:
                    self._offset = int(res0[-1]["update_id"]) + 1
        except Exception:
            pass

        while self._running:
            try:
                r = requests.get(
                    base_url,
                    params={"timeout": 20, "offset": self._offset, "allowed_updates": '["message"]'},
                    timeout=28,
                    proxies=proxies,
                )
                if r.status_code != 200:
                    time.sleep(5.0)
                    continue
                updates = r.json().get("result") or []
                for upd in updates:
                    self._offset = max(self._offset, int(upd.get("update_id", 0)) + 1)
                    msg = upd.get("message") or {}
                    cid = str((msg.get("chat") or {}).get("id", "")).strip()
                    if cid != self.allowed_chat_id:
                        continue
                    text = str(msg.get("text") or "").strip()
                    if not text.startswith("/"):
                        continue
                    parts = text.split(None, 1)
                    cmd = parts[0].split("@")[0].lower()
                    arg = parts[1].strip() if len(parts) > 1 else ""
                    self.command_received.emit(cmd, arg)
            except Exception:
                for _ in range(50):
                    if not self._running:
                        break
                    time.sleep(0.1)
