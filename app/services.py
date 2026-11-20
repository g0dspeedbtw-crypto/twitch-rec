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

from app.config import BASE_DIR, AppConfig

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
RUN_REG_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
APP_REG_NAME = "TwitchRecorderPro"


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


# ------------------------------------------------------------------ #32 Telegram & Discord Notifier
def send_notification_async(
    config: AppConfig,
    title: str,
    message: str,
    image_path: Optional[str] = None,
    log_cb: Optional[Callable[[str], None]] = None,
) -> None:
    """Fire-and-forget notification to Telegram Bot and/or Discord Webhook."""
    has_tg = bool(config.telegram_bot_token.strip() and config.telegram_chat_id.strip())
    has_dc = bool(config.discord_webhook_url.strip())
    if not has_tg and not has_dc:
        return

    def _worker() -> None:
        text = f"*{title}*\n{message}"
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
                        )
                else:
                    url = f"https://api.telegram.org/bot{tok}/sendMessage"
                    requests.post(
                        url,
                        json={"chat_id": cid, "text": text, "parse_mode": "Markdown"},
                        timeout=10,
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
                        )
                else:
                    requests.post(wh, json=payload, timeout=10)
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
                    # Remove companion chat/marker files if present
                    for side in (f.with_suffix(".chat.json"), f.with_suffix(".chat.ass"), f.with_suffix(".markers.json")):
                        if side.exists():
                            try:
                                side.unlink()
                            except Exception:
                                pass
            except Exception:
                pass
    return deleted, freed_bytes / (1024 ** 3)


# ------------------------------------------------------------------ #35 PC Resource Monitor
@dataclass
class ResourceSnapshot:
    cpu_pct: float = 0.0
    ram_pct: float = 0.0
    disk_free_gb: float = 0.0
    gpu_pct: Optional[int] = None
    nvenc_pct: Optional[int] = None
    vram_used_mb: Optional[int] = None
    vram_total_mb: Optional[int] = None


class ResourceMonitorWorker(QThread):
    stats_ready = Signal(object)  # ResourceSnapshot

    def __init__(self, output_dir_fn: Callable[[], str], parent=None):
        super().__init__(parent)
        self._output_dir_fn = output_dir_fn
        self._running = True
        self._has_nvidia_smi = shutil.which("nvidia-smi") is not None

    def stop(self) -> None:
        self._running = False

    def run(self) -> None:
        psutil.cpu_percent(interval=None)
        while self._running:
            snap = ResourceSnapshot(
                cpu_pct=psutil.cpu_percent(interval=None),
                ram_pct=psutil.virtual_memory().percent,
                disk_free_gb=get_disk_free_gb(self._output_dir_fn()),
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


# ------------------------------------------------------------------ #19 Global Hotkey Poller (Windows)
_VK_MAP = {
    "F1": 0x70, "F2": 0x71, "F3": 0x72, "F4": 0x73, "F5": 0x74, "F6": 0x75,
    "F7": 0x76, "F8": 0x77, "F9": 0x78, "F10": 0x79, "F11": 0x7A, "F12": 0x7B,
}


class GlobalHotkeyMonitor(QObject):
    """Lightweight global Windows hotkey detector (e.g. F9 to drop a live highlight marker)."""

    triggered = Signal()

    def __init__(self, key_name: str = "F9", parent=None):
        super().__init__(parent)
        self._vk = _VK_MAP.get(key_name.upper(), 0x78)
        self._was_down = False
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._poll)
        if os.name == "nt":
            self._timer.start(80)

    def set_key(self, key_name: str) -> None:
        self._vk = _VK_MAP.get(key_name.upper(), 0x78)

    def _poll(self) -> None:
        try:
            state = ctypes.windll.user32.GetAsyncKeyState(self._vk)
            is_down = bool(state & 0x8000)
            if is_down and not self._was_down:
                self.triggered.emit()
            self._was_down = is_down
        except Exception:
            pass
