from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import uuid
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests
from PySide6.QtCore import QThread, Signal

from app.config import APP_VERSION, normalize_github_repo
from app.tools import CREATE_NO_WINDOW


@dataclass
class UpdateInfo:
    available: bool = False
    current_version: str = APP_VERSION
    latest_version: str = APP_VERSION
    tag_name: str = ""
    release_name: str = ""
    notes: str = ""
    html_url: str = ""
    published_at: str = ""
    asset_name: str = ""
    asset_url: str = ""
    asset_size: int = 0
    can_self_update: bool = False
    error: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def parse_version(raw: str) -> Tuple[int, ...]:
    cleaned = (raw or "").strip().lstrip("vV")
    nums = re.findall(r"\d+", cleaned)
    if not nums:
        return (0,)
    return tuple(int(n) for n in nums[:4])


def is_newer_version(latest: str, current: str) -> bool:
    lv = parse_version(latest)
    cv = parse_version(current)
    max_len = max(len(lv), len(cv))
    lv = lv + (0,) * (max_len - len(lv))
    cv = cv + (0,) * (max_len - len(cv))
    return lv > cv


def get_mac_app_bundle_path() -> Optional[Path]:
    """Return path to enclosing TwitchRecorderPro.app when running as a frozen macOS bundle."""
    if sys.platform != "darwin" or not getattr(sys, "frozen", False):
        return None
    p = Path(sys.executable).resolve()
    for parent in p.parents:
        if parent.suffix == ".app":
            return parent
    return None


def _pick_release_asset(assets: List[dict], platform: str = sys.platform) -> Tuple[str, str, int]:
    """Pick the best release asset for the current OS: prefers self-updatable .zip, falls back to installer."""
    scored: List[Tuple[int, str, str, int]] = []
    for a in assets or []:
        name = str(a.get("name") or "")
        url = str(a.get("browser_download_url") or "")
        size = int(a.get("size") or 0)
        if not name or not url:
            continue
        low = name.lower()
        score = 0
        if platform == "darwin":
            is_mac = any(k in low for k in ("mac", "osx", "darwin"))
            if is_mac and low.endswith(".zip"):
                score = 100
            elif is_mac and low.endswith(".dmg"):
                score = 80
            elif low.endswith(".dmg"):
                score = 60
        elif platform.startswith("win") or os.name == "nt":
            is_win = any(k in low for k in ("win", "windows"))
            if is_win and low.endswith(".zip"):
                score = 100
            elif is_win and low.endswith(".exe"):
                score = 80
            elif low.endswith(".zip") and not any(k in low for k in ("mac", "linux")):
                score = 70
        else:
            if "linux" in low and (low.endswith(".tar.gz") or low.endswith(".zip") or low.endswith(".appimage")):
                score = 90
        if score > 0:
            scored.append((score, name, url, size))

    if not scored:
        return "", "", 0
    scored.sort(key=lambda x: x[0], reverse=True)
    _, name, url, size = scored[0]
    return name, url, size


def _proxies(proxy_url: str) -> Optional[Dict[str, str]]:
    p = (proxy_url or "").strip()
    return {"http": p, "https": p} if p else None


def check_github_update(
    repo: str,
    current_version: str = APP_VERSION,
    proxy_url: str = "",
    timeout: int = 12,
) -> UpdateInfo:
    norm = normalize_github_repo(repo)
    if not norm or "/" not in norm:
        return UpdateInfo(
            current_version=current_version,
            error="Укажите репозиторий GitHub в формате owner/repo",
        )

    url = f"https://api.github.com/repos/{norm}/releases/latest"
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": f"TwitchRecorderPro/{current_version}",
    }
    try:
        resp = requests.get(url, headers=headers, timeout=timeout, proxies=_proxies(proxy_url))
        if resp.status_code == 404:
            return UpdateInfo(
                current_version=current_version,
                error="Релизы в репозитории пока не опубликованы (404)",
            )
        if resp.status_code == 403:
            return UpdateInfo(
                current_version=current_version,
                error="Лимит запросов GitHub API (попробуйте позже)",
            )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        return UpdateInfo(
            current_version=current_version,
            error=f"Не удалось проверить обновления: {exc}",
        )

    tag = str(data.get("tag_name") or "").strip()
    latest = tag.lstrip("vV") or current_version
    asset_name, asset_url, asset_size = _pick_release_asset(data.get("assets") or [])
    available = is_newer_version(latest, current_version)
    frozen = getattr(sys, "frozen", False)
    can_self = bool(
        frozen
        and asset_url
        and (
            (sys.platform == "darwin" and asset_name.lower().endswith(".zip") and get_mac_app_bundle_path() is not None)
            or (os.name == "nt" and asset_name.lower().endswith(".zip"))
        )
    )

    return UpdateInfo(
        available=available,
        current_version=current_version,
        latest_version=latest,
        tag_name=tag,
        release_name=str(data.get("name") or tag),
        notes=str(data.get("body") or "").strip()[:3000],
        html_url=str(data.get("html_url") or f"https://github.com/{norm}/releases"),
        published_at=str(data.get("published_at") or "")[:10],
        asset_name=asset_name,
        asset_url=asset_url,
        asset_size=asset_size,
        can_self_update=can_self,
        error="",
    )


class UpdateCheckWorker(QThread):
    result_ready = Signal(dict, bool)  # update_info_dict, is_manual

    def __init__(self, repo: str, proxy_url: str = "", manual: bool = False, parent=None):
        super().__init__(parent)
        self.repo = repo
        self.proxy_url = proxy_url
        self.manual = manual

    def run(self) -> None:
        info = check_github_update(self.repo, APP_VERSION, self.proxy_url)
        self.result_ready.emit(info.to_dict(), self.manual)


class UpdateDownloadWorker(QThread):
    progress_updated = Signal(float, str)   # pct, status_text
    finished_download = Signal(bool, str, str)  # ok, local_path, message

    def __init__(self, asset_url: str, asset_name: str, proxy_url: str = "", parent=None):
        super().__init__(parent)
        self.asset_url = asset_url
        self.asset_name = asset_name or "TwitchRecorderPro-update.zip"
        self.proxy_url = proxy_url
        self._stop_requested = False

    def request_stop(self) -> None:
        self._stop_requested = True

    def run(self) -> None:
        if not self.asset_url:
            self.finished_download.emit(False, "", "Нет ссылки на файл обновления")
            return

        upd_dir = Path(tempfile.gettempdir()) / "twitch_recorder_updates"
        upd_dir.mkdir(parents=True, exist_ok=True)
        safe_name = re.sub(r"[^a-zA-Z0-9._\-]", "_", self.asset_name)
        dest = upd_dir / f"{uuid.uuid4().hex[:6]}_{safe_name}"

        headers = {"User-Agent": f"TwitchRecorderPro/{APP_VERSION}"}
        try:
            self.progress_updated.emit(0.0, "Подключение к GitHub...")
            with requests.get(
                self.asset_url,
                headers=headers,
                stream=True,
                timeout=(15, 60),
                proxies=_proxies(self.proxy_url),
            ) as resp:
                resp.raise_for_status()
                total = int(resp.headers.get("Content-Length") or 0)
                downloaded = 0
                last_pct = -1.0

                with open(dest, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=262144):
                        if self._stop_requested:
                            break
                        if not chunk:
                            continue
                        f.write(chunk)
                        downloaded += len(chunk)
                        if total > 0:
                            pct = min(99.5, (downloaded / total) * 100.0)
                            if pct - last_pct >= 0.8:
                                last_pct = pct
                                mb_done = downloaded / (1024 * 1024)
                                mb_tot = total / (1024 * 1024)
                                self.progress_updated.emit(pct, f"Загрузка: {pct:.0f}% ({mb_done:.1f} / {mb_tot:.1f} МБ)")
                        else:
                            mb_done = downloaded / (1024 * 1024)
                            self.progress_updated.emit(50.0, f"Загрузка: {mb_done:.1f} МБ...")

            if self._stop_requested:
                if dest.exists():
                    dest.unlink(missing_ok=True)
                self.finished_download.emit(False, "", "Загрузка отменена")
                return

            if not dest.exists() or dest.stat().st_size < 10_000:
                dest.unlink(missing_ok=True)
                self.finished_download.emit(False, "", "Файл обновления повреждён или пуст")
                return

            self.progress_updated.emit(100.0, "Загрузка завершена")
            self.finished_download.emit(True, str(dest), "Обновление готово к установке")
        except Exception as exc:
            if dest.exists():
                try:
                    dest.unlink()
                except Exception:
                    pass
            self.finished_download.emit(False, "", f"Ошибка загрузки: {exc}")


def launch_update_installer(downloaded_path: str) -> Tuple[bool, bool, str]:
    """
    Apply a downloaded GitHub Release archive.
    Returns (ok, should_quit_app, message).
    - On frozen macOS .app with .zip: extracts via ditto, atomically swaps .app after exit, relaunches.
    - On frozen Windows .exe with .zip: extracts via zipfile, swaps files preserving config.json & recordings/, relaunches.
    - Otherwise (DMG or running from source): opens/reveals the downloaded installer safely.
    """
    src = Path(downloaded_path)
    if not src.is_file():
        return False, False, "Скачанный файл обновления не найден"

    frozen = getattr(sys, "frozen", False)

    # 1. Frozen macOS .app self-update from .zip
    if sys.platform == "darwin" and frozen and src.suffix.lower() == ".zip":
        target_app = get_mac_app_bundle_path()
        if target_app and target_app.exists():
            stage_dir = Path(tempfile.mkdtemp(prefix="trp_upd_stage_"))
            try:
                res = subprocess.run(
                    ["ditto", "-x", "-k", str(src), str(stage_dir)],
                    capture_output=True, text=True, timeout=180,
                )
                if res.returncode != 0:
                    shutil.rmtree(stage_dir, ignore_errors=True)
                    return False, False, f"Ошибка распаковки: {res.stderr.strip()}"

                new_apps = list(stage_dir.rglob("*.app"))
                if not new_apps:
                    shutil.rmtree(stage_dir, ignore_errors=True)
                    return False, False, "В архиве не найден пакет .app"
                new_app = new_apps[0]

                pid = os.getpid()
                script_path = Path(tempfile.gettempdir()) / f"trp_apply_update_{pid}.sh"
                backup_app = target_app.with_name(f"{target_app.name}.bak")
                script = f"""#!/usr/bin/env bash
set -e
PID={pid}
TARGET={shlex.quote(str(target_app))}
BACKUP={shlex.quote(str(backup_app))}
NEW_APP={shlex.quote(str(new_app))}
STAGE={shlex.quote(str(stage_dir))}
ARCHIVE={shlex.quote(str(src))}

for _ in $(seq 1 120); do
  if ! kill -0 "$PID" 2>/dev/null; then
    break
  fi
  sleep 0.25
done

rm -rf "$BACKUP"
mv "$TARGET" "$BACKUP"
if mv "$NEW_APP" "$TARGET"; then
  xattr -dr com.apple.quarantine "$TARGET" 2>/dev/null || true
  rm -rf "$BACKUP" "$STAGE" "$ARCHIVE"
  open -n "$TARGET"
else
  mv "$BACKUP" "$TARGET" || true
fi
rm -f "$0"
"""
                script_path.write_text(script, encoding="utf-8")
                os.chmod(script_path, 0o755)
                subprocess.Popen(
                    ["/bin/bash", str(script_path)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                return True, True, "Перезапуск для установки обновления..."
            except Exception as exc:
                shutil.rmtree(stage_dir, ignore_errors=True)
                return False, False, f"Ошибка подготовки обновления: {exc}"

    # 2. Frozen Windows onedir .exe self-update from .zip
    if os.name == "nt" and frozen and src.suffix.lower() == ".zip":
        install_dir = Path(sys.executable).resolve().parent
        stage_dir = Path(tempfile.mkdtemp(prefix="trp_upd_stage_"))
        try:
            with zipfile.ZipFile(src, "r") as zf:
                zf.extractall(stage_dir)

            # Locate folder containing TwitchRecorderPro.exe
            exe_candidates = list(stage_dir.rglob("TwitchRecorderPro.exe"))
            if not exe_candidates:
                shutil.rmtree(stage_dir, ignore_errors=True)
                return False, False, "В архиве не найден TwitchRecorderPro.exe"
            new_root = exe_candidates[0].parent

            pid = os.getpid()
            ps_path = Path(tempfile.gettempdir()) / f"trp_apply_update_{pid}.ps1"
            ps_script = f"""$ErrorActionPreference = 'SilentlyContinue'
$pidToWait = {pid}
for ($i = 0; $i -lt 120; $i++) {{
    if (-not (Get-Process -Id $pidToWait -ErrorAction SilentlyContinue)) {{ break }}
    Start-Sleep -Milliseconds 250
}}
$srcDir = '{str(new_root).replace("'", "''")}'
$dstDir = '{str(install_dir).replace("'", "''")}'
$stageDir = '{str(stage_dir).replace("'", "''")}'
$archivePath = '{str(src).replace("'", "''")}'

# Preserve user data: config.json, recordings/, templates/
$skipNames = @('config.json', 'recordings', 'templates')
Get-ChildItem -Path $srcDir -Force | ForEach-Object {{
    if ($skipNames -contains $_.Name) {{ return }}
    $dest = Join-Path $dstDir $_.Name
    if ($_.PSIsContainer) {{
        if (Test-Path $dest) {{ Remove-Item -Recurse -Force $dest }}
        Copy-Item -Path $_.FullName -Destination $dest -Recurse -Force
    }} else {{
        Copy-Item -Path $_.FullName -Destination $dest -Force
    }}
}}

Remove-Item -Recurse -Force $stageDir
Remove-Item -Force $archivePath
Start-Process -FilePath (Join-Path $dstDir 'TwitchRecorderPro.exe') -WorkingDirectory $dstDir
Remove-Item -Force $MyInvocation.MyCommand.Path
"""
            ps_path.write_text(ps_script, encoding="utf-8")
            creation = CREATE_NO_WINDOW | getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            subprocess.Popen(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-File", str(ps_path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creation,
            )
            return True, True, "Перезапуск для установки обновления..."
        except Exception as exc:
            shutil.rmtree(stage_dir, ignore_errors=True)
            return False, False, f"Ошибка подготовки обновления: {exc}"

    # 3. Fallback (DMG on macOS, EXE installer, or running from Python source): open/reveal file
    try:
        if sys.platform == "darwin":
            if src.suffix.lower() == ".dmg":
                subprocess.Popen(["open", str(src)])
                return True, False, f"Образ {src.name} открыт — перетащите приложение в Applications"
            subprocess.Popen(["open", "-R", str(src)])
            return True, False, f"Архив обновления показан в Finder: {src.name}"
        if os.name == "nt":
            subprocess.Popen(["explorer", "/select,", os.path.normpath(str(src))])
            return True, False, f"Архив обновления показан в Проводнике: {src.name}"
        subprocess.Popen(["xdg-open", str(src.parent)])
        return True, False, f"Файл обновления загружен: {src.name}"
    except Exception as exc:
        return False, False, f"Не удалось открыть файл обновления: {exc}"
