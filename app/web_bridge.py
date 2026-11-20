from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
import zipfile
from collections import deque
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Deque, Dict, List, Optional

from PIL import Image
from PySide6.QtCore import QObject, QThread, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QFileDialog, QWidget

from app.chat_and_ai import (
    HighlightMoment,
    WhisperAIWorker,
    analyze_chat_highlights,
    detect_audio_events,
    detect_loud_moments,
    fetch_twitch_vod_chat,
    generate_stream_digest,
    generate_youtube_metadata,
    save_markers_and_edl,
)
from app.config import (
    APP_VERSION,
    CONFIG_PATH,
    DATA_DIR,
    LIBRARY_CACHE_PATH,
    LOG_PATH,
    TEMPLATES_DIR,
    AppConfig,
    BlurRegion,
    ChannelConfig,
    get_file_logger,
    normalize_github_repo,
)
from app.local_media_server import LocalMediaServer
from app.updater import UpdateCheckWorker, UpdateDownloadWorker, launch_update_installer
from app.recorder import (
    BlurPostProcessWorker,
    ChannelMonitorWorker,
    ClipsBatchWorker,
    LiveRecorderWorker,
    StudioToolWorker,
    VodDownloadWorker,
    format_bytes,
    format_duration,
)
from app.services import (
    GlobalHotkeyMonitor,
    ResourceMonitorWorker,
    ResourceSnapshot,
    TelegramBotWorker,
    cleanup_old_recordings,
    send_notification_async,
    send_telegram_reply_async,
    send_telegram_video_async,
    set_windows_autostart,
    sync_profile_cloud,
)
from app.tools import (
    EXPORT_PRESETS,
    detect_available_encoders,
    detect_banner_intervals,
    extract_frame_from_file,
    generate_youtube_thumbnail,
    get_ffmpeg_path,
    get_streamlink_path,
    get_ytdlp_path,
    probe_video,
    resolve_encoder,
)
from app.twitch_api import (
    ClipItem,
    StreamStatus,
    VodItem,
    capture_live_snapshot,
    capture_vod_snapshot,
    download_preview_bytes,
    fetch_channel_clips,
    fetch_channel_vods,
    parse_vod_id,
)


class _ThumbWorker(QThread):
    ready = Signal(str, bytes)

    def __init__(self, targets: Dict[str, str], proxy_url: str, parent=None):
        super().__init__(parent)
        self.targets = dict(targets)
        self.proxy_url = proxy_url

    def run(self) -> None:
        for k, url in self.targets.items():
            raw = download_preview_bytes(url, self.proxy_url)
            if raw:
                self.ready.emit(k, raw)


class _SnapWorker(QThread):
    ready = Signal(str, bool, str, str)  # req_id, ok, jpg_path, msg

    def __init__(
        self,
        req_id: str,
        mode: str,
        target,
        ts: float,
        quality: str,
        proxy_url: str,
        oauth_token: str = "",
        parent=None,
    ):
        super().__init__(parent)
        self.req_id = req_id
        self.mode = mode
        self.target = target
        self.ts = ts
        self.quality = quality
        self.proxy_url = proxy_url
        self.oauth_token = oauth_token

    def run(self) -> None:
        out_jpg = os.path.join(tempfile.gettempdir(), f"tr_snap_{self.req_id}.jpg")
        if self.mode == "live":
            ok, msg = capture_live_snapshot(self.target, out_jpg, self.quality, self.proxy_url)
        elif self.mode == "vod":
            ok, msg = capture_vod_snapshot(self.target, out_jpg, self.ts, self.oauth_token, self.proxy_url)
        else:
            ok, msg = extract_frame_from_file(self.target, self.ts, out_jpg)
        self.ready.emit(self.req_id, ok, out_jpg if ok else "", msg)


class _CatalogWorker(QThread):
    ready = Signal(str, list, str)  # kind, items_dicts, err

    def __init__(self, kind: str, username: str, period: str, oauth: str, proxy: str, parent=None):
        super().__init__(parent)
        self.kind = kind
        self.username = username
        self.period = period
        self.oauth = oauth
        self.proxy = proxy

    def run(self) -> None:
        if self.kind == "vods":
            items, err = fetch_channel_vods(self.username, 24, self.oauth, self.proxy)
            self.ready.emit("vods", [asdict(x) for x in items], err)
        else:
            items, err = fetch_channel_clips(self.username, self.period, 24, self.oauth, self.proxy)
            self.ready.emit("clips", [asdict(x) for x in items], err)


class _HighlightsWorker(QThread):
    ready = Signal(str, list, list)  # mode, heatmap, highlights

    def __init__(self, mode: str, path: str, parent=None):
        super().__init__(parent)
        self.mode = mode
        self.path = path

    def run(self) -> None:
        if self.mode == "chat":
            hm, hl = analyze_chat_highlights(self.path)
        elif self.mode == "events":
            hm, hl = detect_audio_events(self.path)
        else:
            hm, hl = detect_loud_moments(self.path)
        self.ready.emit(self.mode, hm, [asdict(h) for h in hl])


class _StartupWorker(QThread):
    encoders_ready = Signal(list)
    cleanup_done = Signal(int, float)

    def __init__(self, output_dir: str, auto_cleanup: bool, max_age_days: int, parent=None):
        super().__init__(parent)
        self.output_dir = output_dir
        self.auto_cleanup = auto_cleanup
        self.max_age_days = max_age_days

    def run(self) -> None:
        encoders = detect_available_encoders()
        self.encoders_ready.emit(encoders)
        if self.auto_cleanup:
            cnt, gb = cleanup_old_recordings(self.output_dir, self.max_age_days)
            if cnt > 0:
                self.cleanup_done.emit(cnt, gb)


class _LibraryScanWorker(QThread):
    ready = Signal(list, dict)  # items, analytics_stats

    def __init__(self, root: Path, parent=None):
        super().__init__(parent)
        self.root = root

    def run(self) -> None:
        exts = {".mp4", ".mkv", ".ts", ".flv", ".mov"}
        items = []
        cache: Dict[str, dict] = {}
        try:
            if LIBRARY_CACHE_PATH.exists():
                cache = json.loads(LIBRARY_CACHE_PATH.read_text(encoding="utf-8"))
        except Exception:
            cache = {}

        total_bytes = 0
        folder_sizes: Dict[str, int] = {}
        redundant_raw_cnt = 0
        redundant_raw_bytes = 0
        probed_now = 0
        cache_dirty = False

        if self.root.exists():
            files = [
                p for p in self.root.rglob("*")
                if p.is_file() and p.suffix.lower() in exts and not p.name.startswith(".")
            ]
            files.sort(key=lambda x: x.stat().st_mtime, reverse=True)
            for p in files:
                try:
                    st = p.stat()
                except OSError:
                    continue
                total_bytes += st.st_size
                folder_sizes[p.parent.name] = folder_sizes.get(p.parent.name, 0) + st.st_size
                if p.stem.lower().endswith("_raw"):
                    blurred = p.with_name(f"{p.stem[:-4]}_blurred{p.suffix}")
                    if blurred.exists() and blurred.stat().st_size > 100_000:
                        redundant_raw_cnt += 1
                        redundant_raw_bytes += st.st_size

            for p in files[:150]:
                try:
                    st = p.stat()
                except OSError:
                    continue
                ckey = f"{p}:{int(st.st_mtime)}"
                cached = cache.get(ckey)
                if not cached and probed_now < 25:
                    info = probe_video(str(p))
                    cached = {
                        "duration": info.get("duration", 0.0),
                        "width": int(info.get("width", 0)),
                        "height": int(info.get("height", 0)),
                    }
                    cache[ckey] = cached
                    probed_now += 1
                    cache_dirty = True

                dur = float((cached or {}).get("duration", 0.0))
                h_px = int((cached or {}).get("height", 0))
                res_str = f"{h_px}p" if h_px >= 240 else ""

                meta_game, meta_title = "", ""
                meta_f = p.with_suffix(".meta.json")
                if meta_f.exists():
                    try:
                        md = json.loads(meta_f.read_text(encoding="utf-8"))
                        meta_game = str(md.get("game") or "")
                        meta_title = str(md.get("title") or "")
                    except Exception:
                        pass

                markers = []
                mk_f = p.with_suffix(".markers.json")
                if mk_f.exists():
                    try:
                        markers = json.loads(mk_f.read_text(encoding="utf-8"))[:40]
                    except Exception:
                        pass

                items.append({
                    "path": str(p),
                    "name": p.name,
                    "folder": p.parent.name,
                    "size_bytes": st.st_size,
                    "size_str": format_bytes(st.st_size),
                    "duration_sec": dur,
                    "duration_str": format_duration(dur) if dur > 0 else "",
                    "resolution": res_str,
                    "game": meta_game,
                    "title": meta_title,
                    "markers": markers,
                    "mtime": datetime.fromtimestamp(st.st_mtime).strftime("%d.%m.%Y %H:%M"),
                    "is_blurred": "_blurred" in p.stem.lower(),
                    "has_chat": p.with_suffix(".chat.json").exists(),
                    "has_chat_ass": p.with_suffix(".chat.ass").exists(),
                    "has_markers": mk_f.exists(),
                })

        if cache_dirty:
            try:
                # Cap cache size to 1000 entries
                if len(cache) > 1000:
                    keys = list(cache.keys())[-800:]
                    cache = {k: cache[k] for k in keys}
                LIBRARY_CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
            except Exception:
                pass

        top_folders = [
            {"name": k, "size_str": format_bytes(v), "size_bytes": v}
            for k, v in sorted(folder_sizes.items(), key=lambda kv: kv[1], reverse=True)[:5]
        ]
        stats = {
            "total_files": len(items),
            "total_size_str": format_bytes(total_bytes),
            "redundant_raw_count": redundant_raw_cnt,
            "redundant_raw_size_str": format_bytes(redundant_raw_bytes),
            "top_folders": top_folders,
        }
        self.ready.emit(items, stats)


class _VodChatWorker(QThread):
    ready = Signal(bool, str, str, int)  # ok, json_path, msg, count

    def __init__(self, vod_url: str, base_path: str, cfg: AppConfig, parent=None):
        super().__init__(parent)
        self.vod_url = vod_url
        self.base_path = base_path
        self.cfg = cfg

    def run(self) -> None:
        ok, jpath, msg, cnt = fetch_twitch_vod_chat(
            self.vod_url,
            self.base_path,
            oauth_token=self.cfg.twitch_oauth_token,
            proxy_url=self.cfg.proxy_url,
            chat_position=self.cfg.chat_position,
            chat_font_size=self.cfg.chat_font_size,
            chat_hide_bots=self.cfg.chat_hide_bots,
        )
        self.ready.emit(ok, jpath, msg, cnt)


class _YtMetaWorker(QThread):
    ready = Signal(bool, dict)

    def __init__(self, video_path: str, cfg: AppConfig, parent=None):
        super().__init__(parent)
        self.video_path = video_path
        self.cfg = cfg

    def run(self) -> None:
        ok, data = generate_youtube_metadata(
            self.video_path,
            llm_api_url=self.cfg.llm_api_url,
            llm_api_key=self.cfg.llm_api_key,
            llm_model=self.cfg.llm_model,
            proxy_url=self.cfg.proxy_url,
        )
        self.ready.emit(ok, data)


class _BannerTestWorker(QThread):
    ready = Signal(list, str)  # intervals, label

    def __init__(self, video_path: str, region: BlurRegion, parent=None):
        super().__init__(parent)
        self.video_path = video_path
        self.region = region

    def run(self) -> None:
        ivs = detect_banner_intervals(self.video_path, self.region, sample_fps=0.5)
        self.ready.emit(ivs, self.region.label)


class _DigestWorker(QThread):
    ready = Signal(bool, str)

    def __init__(self, video_path: str, cfg: AppConfig, parent=None):
        super().__init__(parent)
        self.video_path = video_path
        self.cfg = cfg

    def run(self) -> None:
        ok, text = generate_stream_digest(
            self.video_path,
            llm_api_url=self.cfg.llm_api_url,
            llm_api_key=self.cfg.llm_api_key,
            llm_model=self.cfg.llm_model,
            proxy_url=self.cfg.proxy_url,
        )
        self.ready.emit(ok, text)


class AppBridge(QObject):
    """Bi-directional RPC + push-event bridge between Chromium Web UI and Python engines."""

    event = Signal(str)           # Emits JSON {"type": ..., "data": ...} to JS
    recordingCountChanged = Signal(int)
    _mainThreadEmit = Signal(str, object)

    def __init__(self, parent_window: QWidget, media_server: LocalMediaServer):
        super().__init__(parent_window)
        self._win = parent_window
        self._media = media_server
        self._web_page = None
        self._mainThreadEmit.connect(self._emit_on_main_thread)

        self.config = AppConfig.load()
        self._encoders: List[str] = ["libx264"]
        self._allowed_files: set[Path] = set()
        self._register_configured_media_files()

        self.statuses: Dict[str, StreamStatus] = {}
        self.live_recorders: Dict[str, LiveRecorderWorker] = {}
        self.live_telemetry: Dict[str, dict] = {}
        self.manually_stopped: set[str] = set()

        self.jobs: Dict[str, dict] = {}  # job_id -> metadata dict (vod / blur / studio / ai)
        self.vod_workers: Dict[str, QThread] = {}
        self.vod_blur_regions: Dict[str, List[BlurRegion]] = {}
        self.blur_workers: Dict[str, BlurPostProcessWorker] = {}
        self.pending_blur_queue: Deque[dict] = deque()
        self.studio_workers: Dict[str, QThread] = {}
        self.logs: Deque[str] = deque(maxlen=500)
        self._file_logger = get_file_logger()
        self._disk_warned = False
        self._disk_free_gb = 999.0
        self._active_game = ""

        self._monitor_worker: Optional[ChannelMonitorWorker] = None
        self._thumb_worker: Optional[_ThumbWorker] = None
        self._lib_worker: Optional[_LibraryScanWorker] = None
        self._tg_bot: Optional[TelegramBotWorker] = None
        self._update_check_worker: Optional[UpdateCheckWorker] = None
        self._update_dl_worker: Optional[UpdateDownloadWorker] = None
        self._update_state: dict = {
            "status": "idle",        # idle | checking | available | up_to_date | downloading | ready | error
            "info": None,
            "progress": 0.0,
            "progress_text": "",
            "downloaded_path": "",
        }
        self._aux_workers: List[QThread] = []

        # Timers — always poll online statuses so channel cards stay live even when auto-record is paused
        self.monitor_timer = QTimer(self)
        self.monitor_timer.timeout.connect(self.checkChannels)
        self.monitor_timer.start(max(10, self.config.check_interval_sec) * 1000)

        self.queue_timer = QTimer(self)
        self.queue_timer.timeout.connect(lambda: self._pump_blur_queue(force=False))
        self.queue_timer.start(30_000)

        # PC Resource Monitor (#35) + Game Mode (#43)
        self._res_monitor = ResourceMonitorWorker(
            lambda: self.config.output_dir,
            game_cfg_fn=lambda: (self.config.game_mode_enabled, self.config.game_processes),
            parent=self,
        )
        self._res_monitor.stats_ready.connect(self._on_resource_stats)
        self._res_monitor.start()

        # Global F9 Hotkey (#19)
        self._hotkey = GlobalHotkeyMonitor(self.config.marker_hotkey, self)
        self._hotkey.triggered.connect(self._on_global_marker)

        # Interactive Telegram Bot (#41)
        self._sync_telegram_bot()

        # Probe hardware encoders & run optional startup cleanup asynchronously
        self._startup_worker = _StartupWorker(
            self.config.output_dir,
            self.config.auto_cleanup_old,
            self.config.max_age_days,
            self,
        )
        self._startup_worker.encoders_ready.connect(self._on_encoders_ready)
        self._startup_worker.cleanup_done.connect(self._on_startup_cleanup_done)
        self._startup_worker.finished.connect(self._startup_worker.deleteLater)
        self._aux_workers.append(self._startup_worker)
        self._startup_worker.start()

        self._log("Ядро запущено. Определение аппаратных видеокодеков...")
        self._monitor_pending = False
        QTimer.singleShot(900, self.checkChannels)
        QTimer.singleShot(4000, self._auto_check_updates)

    def bind_web_page(self, page) -> None:
        self._web_page = page

    @Slot(int, float)
    def _on_startup_cleanup_done(self, cnt: int, gb: float) -> None:
        self._log(f"[Авто-очистка] Удалено старых файлов: {cnt} ({gb:.2f} ГБ)")

    def _register_configured_media_files(self) -> None:
        for ch in self.config.channels:
            for rlist in (ch.blur_presets or {}).values():
                for r in rlist:
                    if r.image_path:
                        self.allow_media_file(r.image_path)
                    if r.template_path:
                        self.allow_media_file(r.template_path)

    def allow_media_file(self, path: str) -> None:
        if not path:
            return
        try:
            p = Path(path).resolve()
            if p.is_file():
                self._allowed_files.add(p)
        except Exception:
            pass

    def get_allowed_media_roots(self) -> list[Path]:
        return [self.config.ensure_output_dir(), TEMPLATES_DIR]

    def get_allowed_media_files(self) -> set[Path]:
        return set(self._allowed_files)

    def _build_diagnostics(self) -> dict:
        active = self.config.encoder if (self.config.encoder != "auto" and self.config.encoder in self._encoders) else (self._encoders[0] if self._encoders else "libx264")
        return {
            "platform": sys.platform,
            "app_version": APP_VERSION,
            "ffmpeg": bool(get_ffmpeg_path()),
            "streamlink": bool(get_streamlink_path()),
            "ytdlp": bool(get_ytdlp_path()),
            "encoders": list(self._encoders),
            "active_encoder": active,
        }

    def _on_encoders_ready(self, encoders: list) -> None:
        self._encoders = list(encoders) if encoders else ["libx264"]
        diag = self._build_diagnostics()
        self._log(f"Кодеки готовы: {', '.join(self._encoders)} (активный: {diag['active_encoder']})")
        self._emit("diagnostics", diag)

    @Slot(str, object)
    def _emit_on_main_thread(self, ev_type: str, data) -> None:
        self._emit(ev_type, data)

    def _emit(self, ev_type: str, data) -> None:
        if QThread.currentThread() != self.thread():
            self._mainThreadEmit.emit(ev_type, data)
            return
        try:
            payload = json.dumps({"type": ev_type, "data": data}, ensure_ascii=False)
            self.event.emit(payload)
            if self._web_page is not None:
                js_arg = json.dumps(payload, ensure_ascii=False)
                self._web_page.runJavaScript(
                    f"window.__trDispatch && window.__trDispatch({js_arg});"
                )
        except Exception:
            pass

    def _toast(self, title: str, msg: str = "", level: str = "info") -> None:
        self._emit("toast", {"title": title, "message": msg, "level": level})

    def _log(self, msg: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        self.logs.append(line)
        try:
            self._file_logger.info(msg)
        except Exception:
            pass
        self._emit("log", {"line": line})

    def handleNativeFileDrop(self, paths: List[str]) -> None:
        """Called by DropWebEngineView when files are dropped from Finder/Explorer (#49)."""
        clean = []
        for p in paths:
            if p and os.path.isfile(p):
                self.allow_media_file(p)
                clean.append(p)
        if clean:
            self._emit("files_dropped", clean)

    def _serialize_channels(self) -> list:
        out = []
        for ch in self.config.channels:
            d = asdict(ch)
            d["key"] = ch.key
            d["blur_regions"] = [asdict(r) for r in ch.blur_regions]
            st = self.statuses.get(ch.key)
            d["status"] = asdict(st) if st else {
                "key": ch.key, "username": ch.username, "platform": ch.platform,
                "is_live": False, "title": "", "game": "", "viewers": 0, "preview_url": "", "error": ""
            }
            d["is_recording"] = ch.key in self.live_recorders
            d["telemetry"] = self.live_telemetry.get(ch.key, {"dur": "", "size": "", "speed": "", "state": ""})
            d["has_preview"] = ch.key in self._media.live_previews
            out.append(d)
        return out

    def _push_channels(self) -> None:
        self._emit("channels", self._serialize_channels())
        self.recordingCountChanged.emit(len(self.live_recorders))

    def _push_job(self, job_id: str) -> None:
        j = self.jobs.get(job_id)
        if j:
            self._emit("job", j)

    # ------------------------------------------------------------------ Bootstrap & State
    @Slot(result=str)
    def getBootstrap(self) -> str:
        return json.dumps({
            "config": asdict(self.config),
            "channels": self._serialize_channels(),
            "jobs": list(self.jobs.values()),
            "logs": list(self.logs),
            "export_presets": EXPORT_PRESETS,
            "diagnostics": self._build_diagnostics(),
            "update": self._update_state,
        }, ensure_ascii=False)

    # ------------------------------------------------------------------ Native Pickers & OS Helpers
    @Slot(str, result=str)
    def pickFile(self, filter_str: str) -> str:
        flt = filter_str or "Видео (*.mp4 *.mkv *.ts *.flv *.mov);;Все файлы (*)"
        p, _ = QFileDialog.getOpenFileName(self._win, "Выберите файл", self.config.output_dir, flt)
        if p:
            self.allow_media_file(p)
        return p or ""

    @Slot(result=str)
    def pickVideoFiles(self) -> str:
        files, _ = QFileDialog.getOpenFileNames(
            self._win, "Выберите 2+ видеофайлов", self.config.output_dir, "Видео (*.mp4 *.mkv *.ts *.flv *.mov)"
        )
        for f in files:
            self.allow_media_file(f)
        return json.dumps(files, ensure_ascii=False)

    @Slot(result=str)
    def pickFolder(self) -> str:
        d = QFileDialog.getExistingDirectory(self._win, "Выберите папку", self.config.output_dir)
        return d or ""

    @Slot(str)
    def openInExplorer(self, path: str) -> None:
        if path and os.path.exists(path):
            if os.name == "nt":
                subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
            elif sys.platform == "darwin":
                subprocess.Popen(["open", "-R", path])
            else:
                subprocess.Popen(["xdg-open", str(Path(path).parent)])
        else:
            p = str(self.config.ensure_output_dir())
            if os.name == "nt":
                os.startfile(p)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", p])
            else:
                subprocess.Popen(["xdg-open", p])

    @Slot(str)
    def openExternalPlayer(self, path: str) -> None:
        if path and os.path.exists(path):
            if os.name == "nt":
                os.startfile(path)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])

    # ------------------------------------------------------------------ Channels CRUD & Recording
    @Slot(str, str, result=str)
    def addChannels(self, raw_input: str, default_platform: str) -> str:
        tokens = [t.strip() for t in raw_input.replace(",", " ").replace("\n", " ").split() if t.strip()]
        added = []
        for tok in tokens:
            plat, uname = ChannelConfig.parse_input(tok, default_platform or "twitch")
            if not uname:
                continue
            key = f"{plat}:{uname.lower()}"
            if self.config.get_channel(key):
                continue
            self.config.channels.append(
                ChannelConfig(
                    username=uname,
                    platform=plat,
                    quality="best",
                    auto_record=True,
                    blur_mode="postprocess",
                    segment_minutes=int(getattr(self.config, "default_segment_minutes", 0) or 0),
                )
            )
            added.append(uname)
        if added:
            self.config.save()
            self._push_channels()
            self._log(f"Добавлены каналы: {', '.join(added)}")
            self._toast("Каналы добавлены", ", ".join(added), "success")
            self.checkChannels()
        return json.dumps({"added": added, "channels": self._serialize_channels()}, ensure_ascii=False)

    @Slot(str, result=str)
    def deleteChannel(self, key: str) -> str:
        if key in self.live_recorders:
            self.stopRecording(key)
        self.config.channels = [c for c in self.config.channels if c.key != key]
        self.config.save()
        self._push_channels()
        self._toast("Канал удалён", key, "info")
        return json.dumps({"channels": self._serialize_channels()}, ensure_ascii=False)

    @Slot(str, str, result=str)
    def updateChannelField(self, key: str, patch_json: str) -> str:
        ch = self.config.get_channel(key)
        if not ch:
            return json.dumps({"channels": self._serialize_channels()}, ensure_ascii=False)
        patch = json.loads(patch_json)
        for field_name in (
            "quality", "blur_mode", "auto_record", "active_preset",
            "auto_reconnect", "auto_concat_parts", "record_chat", "disable_ads",
            "keep_original", "segment_minutes", "category_filter", "title_filter",
            "schedule_enabled", "schedule_start", "schedule_end", "schedule_days",
            "auto_scene_by_game", "live_auto_clip", "auto_clip_threshold",
            "chat_trigger_words", "separate_audio_track",
        ):
            if field_name in patch:
                setattr(ch, field_name, patch[field_name])
        if patch.get("auto_record") is True:
            self.manually_stopped.discard(key)
        self.config.save()
        self._push_channels()
        return json.dumps({"channels": self._serialize_channels()}, ensure_ascii=False)

    @Slot(str, str, str, result=str)
    def saveChannelBlurPresets(self, key: str, presets_json: str, active_preset: str) -> str:
        ch = self.config.get_channel(key)
        if not ch:
            return json.dumps({"channels": self._serialize_channels()}, ensure_ascii=False)
        raw_presets = json.loads(presets_json)
        parsed: Dict[str, List[BlurRegion]] = {}
        for pname, rlist in raw_presets.items():
            parsed[str(pname)] = [AppConfig._parse_region(r) for r in (rlist or [])]
        if not parsed:
            parsed = {"Основной": []}
        ch.blur_presets = parsed
        ch.active_preset = active_preset if active_preset in parsed else next(iter(parsed))
        if ch.blur_regions and ch.blur_mode == "none":
            ch.blur_mode = "postprocess"
        self._register_configured_media_files()
        self.config.save()
        self._push_channels()
        self._toast("Зоны блюра сохранены", f"{ch.username} · сцена «{ch.active_preset}» ({len(ch.blur_regions)} зон)", "success")
        return json.dumps({"channels": self._serialize_channels()}, ensure_ascii=False)

    @Slot()
    def checkChannels(self) -> None:
        if not self.config.channels:
            return
        if self._monitor_worker and self._monitor_worker.isRunning():
            self._monitor_pending = True
            return
        self._monitor_pending = False
        self._monitor_worker = ChannelMonitorWorker(deepcopy(self.config.channels), deepcopy(self.config), self)
        self._monitor_worker.statuses_ready.connect(self._on_statuses_ready)
        self._monitor_worker.log_message.connect(self._log)
        self._monitor_worker.start()

    @Slot(dict)
    def _on_statuses_ready(self, statuses: Dict[str, StreamStatus]) -> None:
        if statuses:
            self.statuses.update(statuses)
            thumb_targets: Dict[str, str] = {}
            for ch in self.config.channels:
                st = statuses.get(ch.key)
                if not st:
                    continue
                if st.error:
                    self._log(f"[Статус {ch.username}] {st.error}")
                if not st.is_live:
                    self.manually_stopped.discard(ch.key)
                else:
                    if st.preview_url:
                        thumb_targets[ch.key] = st.preview_url
                    # #2 Auto-switch blur scene preset by current stream game/category
                    if getattr(ch, "auto_scene_by_game", True) and st.game and len(ch.blur_presets) > 1:
                        g_low = st.game.strip().lower()
                        for pname in ch.blur_presets.keys():
                            p_low = pname.strip().lower()
                            if p_low and (p_low == g_low or p_low in g_low or g_low in p_low):
                                if ch.active_preset != pname:
                                    ch.active_preset = pname
                                    self.config.save()
                                    self._log(f"[Авто-сцена] {ch.username}: игра «{st.game}» -> сцена блюра «{pname}»")
                                break

                if (
                    self.config.auto_monitor_enabled
                    and ch.auto_record
                    and st.is_live
                    and ch.key not in self.live_recorders
                    and ch.key not in self.manually_stopped
                    and ch.is_within_schedule()
                    and ch.matches_filters(st.game, st.title)
                ):
                    self._log(f"[Автомониторинг] {ch.username} вышел в эфир! Запуск записи...")
                    self.startRecording(ch.key)

            self._push_channels()
            if thumb_targets and not (self._thumb_worker and self._thumb_worker.isRunning()):
                self._thumb_worker = _ThumbWorker(thumb_targets, self.config.proxy_url, self)
                self._thumb_worker.ready.connect(self._on_thumb_ready)
                self._thumb_worker.start()
        if getattr(self, "_monitor_pending", False):
            self._monitor_pending = False
            QTimer.singleShot(250, self.checkChannels)

    @Slot(str, bytes)
    def _on_thumb_ready(self, key: str, raw: bytes) -> None:
        self._media.set_live_preview(key, raw)
        self._emit("preview_updated", {"key": key, "ts": int(datetime.now().timestamp() * 1000)})

    @Slot(bool, result=str)
    def setAutoMonitor(self, enabled: bool) -> str:
        self.config.auto_monitor_enabled = bool(enabled)
        self.config.save()
        self.monitor_timer.start(max(10, self.config.check_interval_sec) * 1000)
        if enabled:
            self.checkChannels()
        self._emit("config", asdict(self.config))
        self._toast("Автомониторинг", "Включён" if enabled else "Приостановлен", "info")
        return json.dumps(asdict(self.config), ensure_ascii=False)

    @Slot(str)
    def startRecording(self, key: str) -> None:
        if key in self.live_recorders:
            return
        ch = self.config.get_channel(key)
        if not ch:
            return
        if self.config.hard_disk_guard and self._disk_free_gb < self.config.min_free_gb:
            self._log(f"[Диск] Запись {ch.username} заблокирована: свободно {self._disk_free_gb:.1f} ГБ < {self.config.min_free_gb} ГБ")
            self._toast("Недостаточно места на диске", f"Свободно {self._disk_free_gb:.1f} ГБ (лимит {self.config.min_free_gb} ГБ)", "error")
            return
        self.manually_stopped.discard(key)
        self.live_telemetry[key] = {"dur": "00:00:00", "size": "0 КБ", "speed": "1.0x", "state": "Подключение..."}

        st = self.statuses.get(key)
        s_meta = asdict(st) if st else {}
        w = LiveRecorderWorker(deepcopy(ch), deepcopy(self.config), stream_meta=s_meta, parent=self)
        w.progress_updated.connect(self._on_rec_progress)
        w.status_changed.connect(self._on_rec_status)
        w.log_message.connect(self._log)
        w.finished_recording.connect(self._on_rec_finished)
        self.live_recorders[key] = w
        w.start()
        self._push_channels()

        if self.config.notify_on_live:
            info = f"Канал: {ch.username} ({ch.platform})\nКачество: {ch.quality}"
            if st and st.title:
                info += f"\nСтрим: {st.title[:100]}"
            send_notification_async(self.config, "Началась запись стрима", info, log_cb=self._log)

    @Slot(str)
    def stopRecording(self, key: str) -> None:
        self.manually_stopped.add(key)
        w = self.live_recorders.get(key)
        if w:
            w.request_stop()
            self.live_telemetry.setdefault(key, {})["state"] = "Сохранение..."
            self._emit("rec_progress", {"key": key, **self.live_telemetry[key]})

    @Slot()
    def recordAllOnline(self) -> None:
        cnt = 0
        for ch in self.config.channels:
            st = self.statuses.get(ch.key)
            if st and st.is_live and ch.key not in self.live_recorders:
                self.startRecording(ch.key)
                cnt += 1
        self._toast("Массовая запись", f"Запущено потоков: {cnt}" if cnt else "Нет новых онлайн-каналов", "success" if cnt else "info")

    @Slot()
    def stopAllRecordings(self) -> None:
        for k in list(self.live_recorders.keys()):
            self.stopRecording(k)

    @Slot(str)
    def dropMarker(self, key: str) -> None:
        w = self.live_recorders.get(key)
        if w:
            t = w.add_marker("Момент")
            self._log(f"[Маркер] {w.channel.username} -> {format_duration(t)}")
            self._toast("Маркер поставлен", f"{w.channel.username} · {format_duration(t)}", "success")

    @Slot(str, int)
    def clipLastSeconds(self, key: str, seconds: int = 60) -> None:
        """#6 DVR Retrospective Clip: cut last N seconds from an active live recording without interrupting it."""
        w = self.live_recorders.get(key)
        if not w:
            self._toast("Откат эфира (DVR)", "Сначала запустите запись канала", "error")
            return
        out = w.clip_retrospective(max(10, int(seconds or 60)), tag="dvr")
        if out:
            self._toast("DVR-клип сохранён", f"{w.channel.username} · последние {seconds} сек -> AutoClips/", "success")
        else:
            self._toast("Откат эфира (DVR)", "Подождите хотя бы 5 секунд после старта записи", "info")

    def _on_global_marker(self) -> None:
        if not self.live_recorders:
            return
        for k, w in self.live_recorders.items():
            t = w.add_marker(f"Хоткей {self.config.marker_hotkey}")
            self._log(f"[Маркер {self.config.marker_hotkey}] {w.channel.username} -> {format_duration(t)}")
        self._toast(f"Маркер ({self.config.marker_hotkey})", f"Сохранён для {len(self.live_recorders)} активных записей", "success")

    def _on_rec_status(self, key: str, state: str) -> None:
        self.live_telemetry.setdefault(key, {"dur": "00:00:00", "size": "0 КБ", "speed": "1.0x"})["state"] = state
        self._emit("rec_progress", {"key": key, **self.live_telemetry[key]})

    def _on_rec_progress(self, key: str, dur: str, size: str, speed: str) -> None:
        t = self.live_telemetry.setdefault(key, {"state": "Запись"})
        t.update({"dur": dur, "size": size, "speed": speed})
        self._emit("rec_progress", {"key": key, **t})

    @Slot(str, list, bool, bool)
    def _on_rec_finished(self, key: str, files: list, ok: bool, needs_post_blur: bool) -> None:
        w = self.live_recorders.pop(key, None)
        if w:
            w.deleteLater()
        self.live_telemetry.pop(key, None)
        self._push_channels()
        ch = self.config.get_channel(key)
        if ok and files:
            self._toast("Запись завершена", f"{ch.username if ch else key} · {Path(files[0]).name}", "success")
            if self.config.notify_on_finish:
                send_notification_async(self.config, "Запись завершена", f"{ch.username if ch else key}: {Path(files[0]).name}", log_cb=self._log)
            if needs_post_blur and ch and ch.blur_regions:
                for fp in files:
                    self._enqueue_blur_job(fp, ch.blur_regions, delete_original=not ch.keep_original, normalize_audio=self.config.normalize_audio_default, force_now=False)
        elif key not in self.manually_stopped:
            self._toast("Не удалось записать эфир", f"{ch.username if ch else key} — поток оффлайн или недоступен", "error")

    # ------------------------------------------------------------------ Snapshots & AI Template Capture
    @Slot(str, bool, str, str)
    def _on_snapshot_ready(self, rid: str, ok: bool, path: str, msg: str) -> None:
        if ok and path:
            self.allow_media_file(path)
        self._emit("snapshot", {"req_id": rid, "ok": ok, "path": path, "message": msg})

    @Slot(str, str, float, result=str)
    def requestSnapshot(self, mode: str, target_str: str, ts_sec: float) -> str:
        req_id = uuid.uuid4().hex[:8]
        target = self.config.get_channel(target_str) if mode == "live" else target_str
        quality = target.quality if isinstance(target, ChannelConfig) else "best"
        w = _SnapWorker(
            req_id, mode, target or target_str, ts_sec, quality,
            self.config.proxy_url, self.config.twitch_oauth_token, self
        )
        w.ready.connect(self._on_snapshot_ready)
        w.finished.connect(w.deleteLater)
        self._aux_workers.append(w)
        w.start()
        return req_id

    @Slot(str, str, float, result=str)
    def captureBannerTemplate(self, image_or_video_path: str, region_json: str, timestamp_sec: float = 5.0) -> str:
        """Crop banner template PNG for OpenCV smart auto-detector (#3) at the exact frame timestamp."""
        try:
            r = AppConfig._parse_region(json.loads(region_json))
            src = image_or_video_path
            if not src or not os.path.exists(src):
                return json.dumps({"ok": False, "error": "Сначала загрузите кадр"})
            ext = os.path.splitext(src)[1].lower()
            tmp_frame = None
            if ext not in (".jpg", ".jpeg", ".png", ".webp", ".bmp"):
                ts = max(0.0, float(timestamp_sec or 0.0))
                tmp_frame = os.path.join(tempfile.gettempdir(), f"tr_tpl_src_{uuid.uuid4().hex[:6]}.jpg")
                ok, _ = extract_frame_from_file(src, ts, tmp_frame)
                if not ok and ts > 0.2:
                    ok, _ = extract_frame_from_file(src, 0.2, tmp_frame)
                if not ok:
                    return json.dumps({"ok": False, "error": "Не удалось извлечь кадр"})
                src = tmp_frame
            img = Image.open(src).convert("RGB")
            w_img, h_img = img.size
            x1 = max(0, min(w_img - 2, int(r.x * w_img)))
            y1 = max(0, min(h_img - 2, int(r.y * h_img)))
            x2 = max(x1 + 4, min(w_img, int((r.x + r.w) * w_img)))
            y2 = max(y1 + 4, min(h_img, int((r.y + r.h) * h_img)))
            TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
            out = str(TEMPLATES_DIR / f"banner_{uuid.uuid4().hex[:10]}.png")
            img.crop((x1, y1, x2, y2)).save(out)
            self.allow_media_file(out)
            if tmp_frame and os.path.exists(tmp_frame):
                os.remove(tmp_frame)
            return json.dumps({"ok": True, "template_path": out, "name": Path(out).name})
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc)})

    def _cleanup_aux(self, w: QThread) -> None:
        if w in self._aux_workers:
            self._aux_workers.remove(w)
        w.deleteLater()

    # ------------------------------------------------------------------ VOD & Clips Browser
    @Slot(str, list, str)
    def _on_catalog_ready(self, k: str, items: list, err: str) -> None:
        self._emit("catalog", {"kind": k, "items": items, "error": err})

    @Slot(str, str, str)
    def fetchCatalog(self, kind: str, username: str, period: str) -> None:
        w = _CatalogWorker(kind, username, period or "LAST_WEEK", self.config.twitch_oauth_token, self.config.proxy_url, self)
        w.ready.connect(self._on_catalog_ready)
        w.finished.connect(w.deleteLater)
        self._aux_workers.append(w)
        w.start()

    @Slot(str)
    def startVodDownload(self, payload_json: str) -> None:
        p = json.loads(payload_json)
        url = (p.get("url") or "").strip()
        if not url:
            return
        if url.isdigit():
            url = f"https://www.twitch.tv/videos/{url}"
        jid = uuid.uuid4().hex[:8]
        quality = p.get("quality", "best")
        start_t = p.get("start_time", "")
        end_t = p.get("end_time", "")
        title = p.get("title") or f"Twitch VOD #{parse_vod_id(url) or url}"

        regions: List[BlurRegion] = []
        if p.get("regions"):
            regions = [AppConfig._parse_region(r) for r in p["regions"]]
        elif p.get("preset_channel_key"):
            ch = self.config.get_channel(p["preset_channel_key"])
            if ch:
                regions = deepcopy(ch.blur_regions)
        if regions:
            self.vod_blur_regions[jid] = regions

        self.jobs[jid] = {
            "id": jid, "category": "vod", "title": title,
            "subtitle": f"{quality}" + (f" · Авто-блюр: {len(regions)} зон" if regions else " · Без блюра"),
            "progress": 0.0, "status": "Подготовка...", "state": "running", "output_path": "",
        }
        self._push_job(jid)

        w = VodDownloadWorker(jid, url, quality, deepcopy(self.config), start_t, end_t, parent=self)
        w.progress_updated.connect(self._on_job_progress)
        w.log_message.connect(self._log)
        w.finished_download.connect(self._on_vod_finished)
        self.vod_workers[jid] = w
        w.start()

    @Slot(str)
    def downloadClipsBatch(self, payload_json: str) -> None:
        p = json.loads(payload_json)
        urls = p.get("urls") or []
        if not urls:
            return
        ch = p.get("channel") or "twitch"
        merge = bool(p.get("merge", False))
        jid = uuid.uuid4().hex[:8]
        self.jobs[jid] = {
            "id": jid, "category": "vod",
            "title": f"{'Подборка' if merge else 'Пакет'} клипов: {ch} ({len(urls)} шт.)",
            "subtitle": "Склейка в 1080p60" if merge else "Отдельные файлы",
            "progress": 0.0, "status": "Запуск...", "state": "running", "output_path": "",
        }
        self._push_job(jid)
        w = ClipsBatchWorker(jid, ch, urls, merge, deepcopy(self.config), self)
        w.progress_updated.connect(self._on_job_progress)
        w.log_message.connect(self._log)
        w.finished_batch.connect(self._on_vod_finished)
        self.vod_workers[jid] = w
        w.start()

    @Slot(str, float, str)
    def _on_job_progress(self, jid: str, pct: float, status: str) -> None:
        j = self.jobs.get(jid)
        if j:
            j["progress"] = round(pct, 1)
            j["status"] = status
            self._push_job(jid)

    @Slot(str, str, bool)
    def _on_vod_finished(self, jid: str, out_path: str, ok: bool) -> None:
        w = self.vod_workers.pop(jid, None)
        if w:
            w.deleteLater()
        j = self.jobs.get(jid)
        if j:
            j["state"] = "done" if ok else "error"
            j["output_path"] = out_path
            if ok:
                j["progress"] = 100.0
            self._push_job(jid)
        if ok and out_path:
            self._toast("Загрузка VOD готова", Path(out_path).name, "success")
        elif not ok:
            st_txt = (j or {}).get("status") or "Ошибка загрузки"
            if st_txt != "Отменено":
                self._toast("Ошибка загрузки VOD", st_txt, "error")
        regs = self.vod_blur_regions.pop(jid, None)
        if ok and out_path and regs:
            self._enqueue_blur_job(out_path, regs, delete_original=False, normalize_audio=self.config.normalize_audio_default, force_now=False)

    # ------------------------------------------------------------------ Smart GPU Blur Queue (#33, #34)
    @Slot(str)
    def enqueueBlur(self, payload_json: str) -> None:
        p = json.loads(payload_json)
        inp = (p.get("input_path") or "").strip()
        if not inp or not os.path.exists(inp):
            self._toast("Ошибка", "Выберите существующий видеофайл", "error")
            return
        regions = [AppConfig._parse_region(r) for r in (p.get("regions") or [])]
        if not regions and p.get("preset_channel_key"):
            ch = self.config.get_channel(p["preset_channel_key"])
            if ch:
                regions = deepcopy(ch.blur_regions)
        if not regions:
            self._toast("Нет зон блюра", "Выделите хотя бы одну область баннера", "error")
            return
        self._enqueue_blur_job(
            inp,
            regions,
            delete_original=bool(p.get("delete_original", False)),
            normalize_audio=bool(p.get("normalize_audio", False)),
            force_now=bool(p.get("force_now", True)),
        )

    @Slot(str)
    def batchEnqueueBlur(self, payload_json: str) -> None:
        """Enqueue multiple video files into the GPU blur queue with one preset (#15)."""
        p = json.loads(payload_json)
        paths = [f for f in (p.get("input_paths") or []) if f and os.path.isfile(f)]
        if not paths:
            self._toast("Пакетный блюр", "Выберите хотя бы один видеофайл", "error")
            return
        regions = [AppConfig._parse_region(r) for r in (p.get("regions") or [])]
        if not regions and p.get("preset_channel_key"):
            ch = self.config.get_channel(p["preset_channel_key"])
            if ch:
                regions = deepcopy(ch.blur_regions)
        if not regions:
            self._toast("Нет зон блюра", "Выберите пресет или задайте зоны блюра", "error")
            return
        for fp in paths:
            self.allow_media_file(fp)
            self._enqueue_blur_job(
                fp,
                regions,
                delete_original=bool(p.get("delete_original", False)),
                normalize_audio=bool(p.get("normalize_audio", False)),
                force_now=bool(p.get("force_now", True)),
            )
        self._toast("Пакетный блюр", f"Добавлено в очередь GPU: {len(paths)} файлов", "success")

    @Slot(list, str)
    def _on_banner_test_ready(self, ivs: list, lbl: str) -> None:
        self._emit("banner_test", {"intervals": ivs, "label": lbl})

    @Slot(str, str)
    def testBannerDetect(self, video_path: str, region_json: str) -> None:
        """Preview OpenCV banner auto-detection intervals directly inside Blur Studio (#13)."""
        if not video_path or not os.path.exists(video_path):
            self._toast("Проверка детекции", "Откройте видеофайл в Студии блюра", "error")
            return
        r = AppConfig._parse_region(json.loads(region_json))
        if not r.template_path or not os.path.exists(r.template_path):
            self._toast("Нет образца", "Сначала нажмите «Запомнить образец баннера»", "error")
            return
        self._toast("Тест AI-детектора", f"Сканируем видео для «{r.label}»...", "info")
        w = _BannerTestWorker(video_path, r, self)
        w.ready.connect(self._on_banner_test_ready)
        w.finished.connect(w.deleteLater)
        self._aux_workers.append(w)
        w.start()

    def _enqueue_blur_job(
        self,
        input_path: str,
        regions: List[BlurRegion],
        delete_original: bool = False,
        normalize_audio: bool = False,
        force_now: bool = False,
    ) -> None:
        jid = uuid.uuid4().hex[:8]
        enc = resolve_encoder(self.config.encoder)
        ai_cnt = sum(1 for r in regions if r.auto_detect)
        sub = f"Зон: {len(regions)}" + (f" (AI: {ai_cnt})" if ai_cnt else "") + f" · {enc}"
        if normalize_audio:
            sub += " · EBU R128"
        deferred_night = self.config.night_render_enabled and not self.config.is_night_render_window() and not force_now
        deferred_game = self.config.game_mode_enabled and bool(self._active_game) and not force_now
        status_txt = (
            f"Пауза (Игра: {self._active_game})" if deferred_game
            else (f"Ночной рендер ({self.config.night_render_start})" if deferred_night else "В очереди GPU...")
        )
        self.jobs[jid] = {
            "id": jid, "category": "blur", "title": Path(input_path).name, "subtitle": sub,
            "progress": 0.0,
            "status": status_txt,
            "state": "queued", "output_path": "",
        }
        self._push_job(jid)
        self.pending_blur_queue.append({
            "job_id": jid, "input_path": input_path, "regions": deepcopy(regions),
            "delete_original": delete_original, "normalize_audio": normalize_audio,
        })
        self._pump_blur_queue(force=force_now)

    @Slot()
    def processBlurQueueNow(self) -> None:
        self._pump_blur_queue(force=True)
        self._toast("Очередь GPU", "Обработка очереди запущена", "info")

    def _pump_blur_queue(self, force: bool = False) -> None:
        if not self.pending_blur_queue:
            return
        if not force and self.config.night_render_enabled and not self.config.is_night_render_window():
            return
        if not force and self.config.game_mode_enabled and self._active_game:
            return
        while self.pending_blur_queue and len(self.blur_workers) < self.config.max_concurrent_blurs:
            job = self.pending_blur_queue.popleft()
            jid = job["job_id"]
            if jid in self.jobs:
                self.jobs[jid]["state"] = "running"
                self.jobs[jid]["status"] = "Запуск кодирования..."
                self._push_job(jid)
            w = BlurPostProcessWorker(
                job_id=jid,
                input_path=job["input_path"],
                regions=job["regions"],
                app_config=deepcopy(self.config),
                delete_original=job["delete_original"],
                normalize_audio=job["normalize_audio"],
                parent=self,
            )
            w.progress_updated.connect(self._on_job_progress)
            w.log_message.connect(self._log)
            w.finished_blur.connect(self._on_blur_finished)
            self.blur_workers[jid] = w
            w.start()

    def _on_blur_finished(self, jid: str, out_path: str, ok: bool) -> None:
        w = self.blur_workers.pop(jid, None)
        if w:
            w.deleteLater()
        j = self.jobs.get(jid)
        if j:
            j["state"] = "done" if ok else "error"
            j["output_path"] = out_path
            if ok:
                j["progress"] = 100.0
            self._push_job(jid)
        if ok and out_path:
            self._toast("Блюр готов", Path(out_path).name, "success")
            if self.config.notify_on_blur:
                send_notification_async(self.config, "Блюр видео готов", Path(out_path).name, log_cb=self._log)
        elif not ok:
            j = self.jobs.get(jid) or {}
            st_txt = j.get("status") or "Ошибка FFmpeg"
            if st_txt != "Отменено":
                self._toast("Ошибка блюра", st_txt, "error")
        self._pump_blur_queue(force=False)

    @Slot(str)
    def cancelJob(self, jid: str) -> None:
        for item in list(self.pending_blur_queue):
            if item["job_id"] == jid:
                self.pending_blur_queue.remove(item)
                if jid in self.jobs:
                    self.jobs[jid]["state"] = "error"
                    self.jobs[jid]["status"] = "Отменено"
                    self._push_job(jid)
                return
        for pool in (self.vod_workers, self.blur_workers, self.studio_workers):
            w = pool.get(jid)
            if w and hasattr(w, "request_stop"):
                w.request_stop()

    # ------------------------------------------------------------------ Library (#28, #47, #51)
    @Slot(list, dict)
    def _on_library_ready(self, items: list, stats: dict) -> None:
        self._emit("library", {"items": items, "stats": stats})

    @Slot()
    def scanLibrary(self) -> None:
        if self._lib_worker and self._lib_worker.isRunning():
            return
        root = self.config.ensure_output_dir()
        self._lib_worker = _LibraryScanWorker(root, self)
        self._lib_worker.ready.connect(self._on_library_ready)
        self._lib_worker.start()

    @Slot()
    def cleanRedundantRawFiles(self) -> None:
        """Delete *_raw files that already have a valid *_blurred counterpart (>100KB) (#51)."""
        root = self.config.ensure_output_dir()
        exts = {".mp4", ".mkv", ".ts", ".flv", ".mov"}
        deleted = 0
        freed = 0
        if root.exists():
            for p in root.rglob("*"):
                if p.is_file() and p.suffix.lower() in exts and p.stem.lower().endswith("_raw"):
                    blurred = p.with_name(f"{p.stem[:-4]}_blurred{p.suffix}")
                    try:
                        if blurred.exists() and blurred.stat().st_size > 100_000:
                            freed += p.stat().st_size
                            p.unlink()
                            deleted += 1
                    except Exception:
                        pass
        if deleted > 0:
            self._log(f"[Очистка _raw] Удалено {deleted} исходников, освобождено {format_bytes(freed)}")
            self._toast("Очистка исходников", f"Удалено {deleted} _raw файлов ({format_bytes(freed)})", "success")
            self.scanLibrary()
        else:
            self._toast("Очистка исходников", "Лишних _raw файлов с готовым блюром не найдено", "info")

    @Slot(str, result=bool)
    def deleteRecordingFile(self, path: str) -> bool:
        try:
            if not path:
                return False
            target = Path(path).resolve(strict=True)
            root = self.config.ensure_output_dir().resolve()
            if not target.is_file() or not target.is_relative_to(root):
                return False
            target.unlink()
            for side in (
                target.with_suffix(".chat.json"),
                target.with_suffix(".chat.ass"),
                target.with_suffix(".markers.json"),
                target.with_suffix(".meta.json"),
                target.with_suffix(".edl"),
                target.with_suffix(".srt"),
                target.with_suffix(".subs.ass"),
            ):
                if side.exists():
                    try:
                        side.unlink()
                    except Exception:
                        pass
            self.scanLibrary()
            return True
        except Exception:
            pass
        return False

    # ------------------------------------------------------------------ Studio & AI (#6, #8, #16-#26, #30, #33)
    @Slot(str, list, list)
    def _on_highlights_ready(self, m: str, hm: list, hl: list) -> None:
        self._emit("highlights", {"mode": m, "heatmap": hm, "highlights": hl})

    @Slot(str, str)
    def analyzeHighlights(self, mode: str, target_path: str) -> None:
        if not target_path or not os.path.exists(target_path):
            self._toast("Файл не найден", "Укажите видео или .chat.json", "error")
            return
        w = _HighlightsWorker(mode, target_path, self)
        w.ready.connect(self._on_highlights_ready)
        w.finished.connect(w.deleteLater)
        self._aux_workers.append(w)
        w.start()

    @Slot(bool, str, str, int)
    def _on_vod_chat_done(self, ok: bool, jpath: str, msg: str, cnt: int) -> None:
        if ok and jpath:
            self.allow_media_file(jpath)
            self._log(f"[Чат VOD] {msg}")
            self._toast("Чат VOD загружен", msg, "success")
            self._emit("vod_chat_ready", {"ok": True, "json_path": jpath, "count": cnt, "message": msg})
        else:
            self._toast("Чат VOD", msg, "error")
            self._emit("vod_chat_ready", {"ok": False, "json_path": "", "count": 0, "message": msg})

    @Slot(str, str)
    def downloadVodChat(self, vod_url: str, video_path: str) -> None:
        """Download past Twitch VOD chat via GQL into .chat.json + .chat.ass (#33)."""
        if not vod_url.strip():
            self._toast("Чат VOD", "Укажите ссылку или ID Twitch VOD", "error")
            return
        base = video_path.strip() if video_path.strip() else str(self.config.ensure_output_dir() / "VODs" / f"vod_chat_{uuid.uuid4().hex[:6]}.mp4")
        Path(base).parent.mkdir(parents=True, exist_ok=True)
        self._toast("Загрузка чата VOD", "Скачиваем сообщения из Twitch GQL...", "info")
        w = _VodChatWorker(vod_url, base, deepcopy(self.config), self)
        w.ready.connect(self._on_vod_chat_done)
        w.finished.connect(w.deleteLater)
        self._aux_workers.append(w)
        w.start()

    @Slot(str, str)
    def exportHighlightsEdl(self, video_path: str, highlights_json: str) -> None:
        """Export detected highlights to Premiere Pro / DaVinci Resolve .edl (#19)."""
        if not video_path:
            self._toast("Экспорт EDL", "Сначала выберите видеофайл", "error")
            return
        try:
            hls = json.loads(highlights_json or "[]")
            markers = [{"t": float(h.get("timestamp_sec", 0)), "note": str(h.get("reason", "Хайлайт"))} for h in hls]
            if not markers:
                self._toast("Экспорт EDL", "Нет найденных хайлайтов для экспорта", "error")
                return
            _, edl_path = save_markers_and_edl(video_path, markers)
            self._log(f"[Экспорт EDL] Сохранено {len(markers)} маркеров -> {Path(edl_path).name}")
            self._toast("EDL экспортирован", f"{Path(edl_path).name} (Premiere / DaVinci)", "success")
        except Exception as exc:
            self._toast("Ошибка EDL", str(exc), "error")

    @Slot(bool, dict)
    def _on_yt_meta_ready(self, ok: bool, data: dict) -> None:
        self._emit("yt_meta", {"ok": ok, **data})

    @Slot(str)
    def generateYouTubeMeta(self, video_path: str) -> None:
        """Generate YouTube titles, description, tags, and chapters via LLM or local heuristics (#26)."""
        if not video_path or not os.path.exists(video_path):
            self._toast("AI YouTube", "Выберите видеофайл", "error")
            return
        self._toast("AI YouTube", "Генерируем заголовки, описание и таймкоды...", "info")
        w = _YtMetaWorker(video_path, deepcopy(self.config), self)
        w.ready.connect(self._on_yt_meta_ready)
        w.finished.connect(w.deleteLater)
        self._aux_workers.append(w)
        w.start()

    @Slot(bool, str)
    def _on_digest_ready(self, ok: bool, text: str) -> None:
        self._emit("stream_digest", {"ok": ok, "text": text})

    @Slot(str)
    def generateStreamDigest(self, video_path: str) -> None:
        """#18 Generate a structured Stream Summary / Digest from chat, markers, and Whisper subtitles."""
        if not video_path or not os.path.exists(video_path):
            self._toast("AI Конспект", "Выберите видеофайл", "error")
            return
        self._toast("AI Конспект", "Анализируем чат, маркеры и субтитры...", "info")
        w = _DigestWorker(video_path, deepcopy(self.config), self)
        w.ready.connect(self._on_digest_ready)
        w.finished.connect(w.deleteLater)
        self._aux_workers.append(w)
        w.start()

    @Slot(str, result=str)
    def createThumbnail(self, payload_json: str) -> str:
        """#11 Generate a 1280x720 YouTube / Shorts thumbnail JPG with custom headline, subtitle, and neon border."""
        try:
            p = json.loads(payload_json or "{}")
            src = (p.get("source_path") or "").strip()
            if not src or not os.path.exists(src):
                return json.dumps({"ok": False, "error": "Выберите видео или кадр"})
            ts = float(p.get("timestamp_sec") or 10.0)
            headline = str(p.get("headline") or "").strip()
            subtitle = str(p.get("subtitle") or "").strip()
            accent = str(p.get("accent_color") or "#8B5CF6").strip()
            border = bool(p.get("add_border", True))
            out_path = (p.get("output_path") or "").strip()
            if not out_path:
                sp = Path(src)
                out_path = str(sp.with_name(f"{sp.stem}_thumbnail.jpg"))
            ok, res = generate_youtube_thumbnail(
                src, out_path, timestamp_sec=ts, headline=headline,
                subtitle=subtitle, accent_color=accent, add_border=border
            )
            if ok:
                self.allow_media_file(res)
                self._log(f"[Обложка YouTube] Сохранена: {Path(res).name}")
                self._toast("Обложка создана", Path(res).name, "success")
                return json.dumps({"ok": True, "path": res})
            self._toast("Ошибка обложки", res, "error")
            return json.dumps({"ok": False, "error": res})
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc)})

    @Slot(str, str)
    def publishVideoToTelegram(self, video_path: str, caption: str = "") -> None:
        """#16 Direct 1-click upload of a video/Shorts (<50MB) to the user's Telegram chat/channel."""
        if not video_path or not os.path.exists(video_path):
            self._toast("Публикация в Telegram", "Видеофайл не найден", "error")
            return
        self._toast("Telegram", f"Отправка {Path(video_path).name} в Telegram...", "info")

        def _done(ok: bool, msg: str) -> None:
            self._log(f"[Telegram Publish] {msg}")
            self._toast("Telegram", msg, "success" if ok else "error")

        send_telegram_video_async(
            self.config.telegram_bot_token,
            self.config.telegram_chat_id,
            video_path,
            caption=caption or f"🎬 {Path(video_path).stem}",
            proxy_url=self.config.proxy_url,
            done_cb=_done,
        )

    @Slot(str, result=str)
    def syncCloudProfile(self, direction: str = "push") -> str:
        """#21 Sync channels & blur presets via Cloud Folder (iCloud/Dropbox) or GitHub Gist."""
        ok, msg = sync_profile_cloud(self.config, direction=direction)
        if ok:
            if direction == "pull":
                self.config = AppConfig.load()
                self._register_configured_media_files()
                self._emit("config", asdict(self.config))
                self._push_channels()
            self._log(f"[Облачная синхронизация] {msg}")
            self._toast("Облачная синхронизация", msg, "success")
        else:
            self._toast("Облачная синхронизация", msg, "error")
        return json.dumps({"ok": ok, "message": msg, "config": asdict(self.config)}, ensure_ascii=False)

    @Slot(str)
    def runStudioAction(self, payload_json: str) -> None:
        p = json.loads(payload_json)
        action = p.get("action", "")
        jid = uuid.uuid4().hex[:8]
        title = p.get("title") or action
        subtitle = p.get("subtitle") or ""

        if action in ("subtitles", "censor"):
            inp = p.get("input_path", "")
            if not inp or not os.path.exists(inp):
                self._toast("AI Whisper", "Выберите существующий видеофайл", "error")
                return
            self.jobs[jid] = {
                "id": jid, "category": "studio", "title": title, "subtitle": subtitle,
                "progress": 0.0, "status": "Загрузка Whisper...", "state": "running", "output_path": "",
            }
            self._push_job(jid)
            w = WhisperAIWorker(
                job_id=jid,
                mode=action,
                video_path=inp,
                model_size=p.get("model", self.config.whisper_model),
                language=p.get("language", "ru"),
                burn_subtitles=bool(p.get("burn_subtitles", True)),
                shorts_style=bool(p.get("shorts_style", True)),
                banned_words=p.get("banned_words", self.config.banned_words),
                censor_beep=bool(p.get("censor_beep", True)),
                encoder=resolve_encoder(self.config.encoder),
                quality=self.config.video_quality,
                initial_prompt=p.get("whisper_prompt", self.config.whisper_prompt),
                task=p.get("task", "transcribe"),
                parent=self,
            )
            w.progress_updated.connect(self._on_job_progress)
            w.log_message.connect(self._log)
            w.finished_ai.connect(self._on_studio_finished)
            self.studio_workers[jid] = w
            w.start()
            return

        # Convert dict regions to BlurRegion dataclasses for Shorts 9:16
        params = dict(p.get("params") or {})
        if action in ("shorts", "batch_shorts"):
            if params.get("cam_region"):
                params["cam_region"] = AppConfig._parse_region(params["cam_region"])
            if params.get("game_region"):
                params["game_region"] = AppConfig._parse_region(params["game_region"])
            if params.get("blur_channel_key"):
                ch = self.config.get_channel(params["blur_channel_key"])
                params["blur_regions"] = deepcopy(ch.blur_regions) if ch else []
            else:
                params["blur_regions"] = [AppConfig._parse_region(r) for r in (params.get("blur_regions") or [])]

        self.jobs[jid] = {
            "id": jid, "category": "studio", "title": title, "subtitle": subtitle,
            "progress": 0.0, "status": "Запуск...", "state": "running", "output_path": "",
        }
        self._push_job(jid)
        w = StudioToolWorker(jid, action, params, deepcopy(self.config), self)
        w.progress_updated.connect(self._on_job_progress)
        w.log_message.connect(self._log)
        w.finished_task.connect(self._on_studio_finished)
        self.studio_workers[jid] = w
        w.start()

    def _on_studio_finished(self, jid: str, out_path: str, ok: bool) -> None:
        w = self.studio_workers.pop(jid, None)
        if w:
            w.deleteLater()
        j = self.jobs.get(jid)
        if j:
            j["state"] = "done" if ok else "error"
            j["output_path"] = out_path
            if ok:
                j["progress"] = 100.0
            self._push_job(jid)
        if ok and out_path:
            self._toast("Задача студии готова", Path(out_path).name, "success")
            if getattr(self.config, "auto_publish_telegram", False) and os.path.exists(out_path):
                sz_mb = os.path.getsize(out_path) / (1024 * 1024)
                if sz_mb <= 49.0 and out_path.lower().endswith(".mp4"):
                    self.publishVideoToTelegram(out_path, caption=f"🎬 {Path(out_path).stem}")
        else:
            j = self.jobs.get(jid) or {}
            self._toast("Ошибка задачи студии", j.get("status") or "Не удалось выполнить операцию", "error")

    # ------------------------------------------------------------------ Settings & Notifications
    @Slot(str)
    def saveSettings(self, cfg_json: str) -> None:
        d = json.loads(cfg_json)
        for k in (
            "output_dir", "check_interval_sec", "encoder", "video_quality", "container",
            "default_segment_minutes", "vod_concurrent_fragments", "twitch_oauth_token", "proxy_url",
            "min_free_gb", "hard_disk_guard", "auto_cleanup_old", "max_age_days", "minimize_to_tray",
            "start_with_windows", "marker_hotkey", "telegram_bot_token", "telegram_chat_id",
            "telegram_bot_enabled", "discord_webhook_url", "notify_on_live", "notify_on_finish", "notify_on_blur",
            "max_concurrent_blurs", "night_render_enabled", "night_render_start",
            "night_render_end", "normalize_audio_default", "low_priority_render",
            "game_mode_enabled", "game_processes", "whisper_model", "whisper_prompt", "banned_words",
            "llm_api_url", "llm_api_key", "llm_model",
            "chat_position", "chat_font_size", "chat_hide_bots", "watermark_path",
            "theme", "compact_cards", "github_repo", "auto_check_updates",
            "smart_segment_blur", "native_os_notifications", "auto_publish_telegram",
            "cloud_sync_path", "cloud_gist_token", "cloud_gist_id",
        ):
            if k in d:
                val = normalize_github_repo(str(d[k])) if k == "github_repo" else d[k]
                setattr(self.config, k, val)
        self.config.ensure_output_dir()
        self.config.save()
        set_windows_autostart(self.config.start_with_windows)
        self._hotkey.set_key(self.config.marker_hotkey)
        self.monitor_timer.start(max(10, self.config.check_interval_sec) * 1000)
        self._sync_telegram_bot()
        self._emit("config", asdict(self.config))
        self._emit("diagnostics", self._build_diagnostics())
        self._toast("Настройки сохранены", "Все параметры применены", "success")

    # ------------------------------------------------------------------ #35 Profile Backup & #55 Logs/Diagnostics
    @Slot()
    def exportProfileZip(self) -> None:
        default_name = str(DATA_DIR / f"TwitchRecorderPro_Profile_{datetime.now().strftime('%Y%m%d')}.zip")
        dest, _ = QFileDialog.getSaveFileName(self._win, "Экспорт профиля (каналы + блюр + шаблоны)", default_name, "ZIP архив (*.zip)")
        if not dest:
            return
        try:
            self.config.save()
            with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
                if CONFIG_PATH.exists():
                    zf.write(CONFIG_PATH, "config.json")
                if TEMPLATES_DIR.exists():
                    for f in TEMPLATES_DIR.rglob("*"):
                        if f.is_file():
                            zf.write(f, f"templates/{f.relative_to(TEMPLATES_DIR)}")
            self._log(f"[Профиль] Экспортирован в {dest}")
            self._toast("Профиль экспортирован", Path(dest).name, "success")
        except Exception as exc:
            self._toast("Ошибка экспорта профиля", str(exc), "error")

    @Slot()
    def importProfileZip(self) -> None:
        src, _ = QFileDialog.getOpenFileName(self._win, "Импорт профиля (.zip)", str(DATA_DIR), "ZIP архив (*.zip)")
        if not src:
            return
        try:
            with zipfile.ZipFile(src, "r") as zf:
                names = zf.namelist()
                if "config.json" not in names:
                    self._toast("Ошибка импорта", "В архиве нет config.json", "error")
                    return
                CONFIG_PATH.write_bytes(zf.read("config.json"))
                TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
                for name in names:
                    if name.startswith("templates/") and not name.endswith("/"):
                        rel = Path(name[len("templates/") :]).name
                        if rel:
                            (TEMPLATES_DIR / rel).write_bytes(zf.read(name))
            self.config = AppConfig.load()
            self._register_configured_media_files()
            self._sync_telegram_bot()
            self._emit("config", asdict(self.config))
            self._push_channels()
            self._log(f"[Профиль] Импортирован из {Path(src).name}")
            self._toast("Профиль импортирован", "Каналы, настройки и шаблоны восстановлены", "success")
        except Exception as exc:
            self._toast("Ошибка импорта профиля", str(exc), "error")

    @Slot()
    def openLogFile(self) -> None:
        self.openInExplorer(str(LOG_PATH) if LOG_PATH.exists() else str(DATA_DIR))

    @Slot(result=str)
    def getChangelog(self) -> str:
        from app.config import get_resource_dir
        for cand in (get_resource_dir() / "CHANGELOG.md", Path(__file__).resolve().parent.parent / "CHANGELOG.md"):
            if cand.is_file():
                try:
                    return cand.read_text(encoding="utf-8")
                except Exception:
                    pass
        return f"# Twitch Recorder Pro v{APP_VERSION}\n\nФайл CHANGELOG.md не найден."

    # ------------------------------------------------------------------ #41 Interactive Telegram Bot
    def _sync_telegram_bot(self) -> None:
        should_run = bool(
            self.config.telegram_bot_enabled
            and self.config.telegram_bot_token.strip()
            and self.config.telegram_chat_id.strip()
        )
        if self._tg_bot and self._tg_bot.isRunning():
            if (
                should_run
                and self._tg_bot.token == self.config.telegram_bot_token.strip()
                and self._tg_bot.allowed_chat_id == self.config.telegram_chat_id.strip()
            ):
                return
            self._tg_bot.stop()
            self._tg_bot.wait(1200)
            self._tg_bot = None
        if should_run:
            self._tg_bot = TelegramBotWorker(
                self.config.telegram_bot_token,
                self.config.telegram_chat_id,
                self.config.proxy_url,
                self,
            )
            self._tg_bot.command_received.connect(self._on_telegram_command)
            self._tg_bot.log_message.connect(self._log)
            self._tg_bot.start()

    def _tg_reply(self, text: str, photo_path: Optional[str] = None) -> None:
        send_telegram_reply_async(
            self.config.telegram_bot_token,
            self.config.telegram_chat_id,
            text,
            photo_path=photo_path,
            proxy_url=self.config.proxy_url,
        )

    def _on_telegram_command(self, cmd: str, arg: str) -> None:
        self._log(f"[Telegram Bot] Команда: {cmd} {arg}".strip())
        if cmd in ("/start", "/help"):
            self._tg_reply(
                "Twitch Recorder Pro — удалённое управление:\n"
                "/status — статус эфиров, записей и диска\n"
                "/rec <ник> — начать запись канала\n"
                "/stop <ник> — остановить запись канала\n"
                "/stopall — остановить все записи\n"
                "/marker [текст] — поставить маркер хайлайта\n"
                "/vod <ссылка> — скачать VOD\n"
                "/snap <ник> — прислать скриншот эфира"
            )
        elif cmd == "/status":
            live_chs = [c.username for c in self.config.channels if (self.statuses.get(c.key) and self.statuses[c.key].is_live)]
            rec_chs = [w.channel.username for w in self.live_recorders.values()]
            gm = f"\nИгровой режим: активен ({self._active_game})" if self._active_game else ""
            self._tg_reply(
                f"Статус Twitch Recorder Pro v{APP_VERSION}\n"
                f"В эфире ({len(live_chs)}): {', '.join(live_chs) or 'нет'}\n"
                f"Пишутся ({len(rec_chs)}): {', '.join(rec_chs) or 'нет'}\n"
                f"Очередь блюра: {len(self.pending_blur_queue)} (активно: {len(self.blur_workers)})\n"
                f"Свободно на диске: {self._disk_free_gb:.1f} ГБ{gm}"
            )
        elif cmd == "/rec" and arg:
            ch = self.config.get_channel(arg)
            if not ch:
                self._tg_reply(f"Канал «{arg}» не найден в списке.")
            else:
                self.startRecording(ch.key)
                self._tg_reply(f"Запуск записи канала {ch.username}...")
        elif cmd == "/stop" and arg:
            ch = self.config.get_channel(arg)
            if not ch or ch.key not in self.live_recorders:
                self._tg_reply(f"Запись канала «{arg}» сейчас не идёт.")
            else:
                self.stopRecording(ch.key)
                self._tg_reply(f"Остановка записи {ch.username}...")
        elif cmd == "/stopall":
            cnt = len(self.live_recorders)
            self.stopAllRecordings()
            self._tg_reply(f"Остановлено записей: {cnt}")
        elif cmd == "/marker":
            if not self.live_recorders:
                self._tg_reply("Нет активных записей для маркера.")
            else:
                note = arg or "Telegram маркер"
                lines = []
                for w in self.live_recorders.values():
                    t = w.add_marker(note)
                    lines.append(f"{w.channel.username}: {format_duration(t)}")
                self._tg_reply("Маркер поставлен:\n" + "\n".join(lines))
        elif cmd == "/vod" and arg:
            self.startVodDownload(json.dumps({"url": arg, "quality": "best"}))
            self._tg_reply(f"Загрузка VOD поставлена в очередь:\n{arg}")
        elif cmd == "/snap" and arg:
            ch = self.config.get_channel(arg) or ChannelConfig(username=arg.strip().lower(), platform="twitch")
            self._start_tg_snap(ch)
        else:
            self._tg_reply("Неизвестная команда. Отправьте /help")

    @Slot(str, bool, str, str)
    def _on_tg_snap_ready(self, _rid: str, ok: bool, path: str, msg: str) -> None:
        if ok and path and os.path.exists(path):
            self._tg_reply("Кадр прямого эфира", photo_path=path)
        else:
            self._tg_reply(f"Не удалось снять кадр: {msg}")

    def _start_tg_snap(self, ch: ChannelConfig) -> None:
        w = _SnapWorker(
            uuid.uuid4().hex[:6], "live", ch, 0.0, ch.quality,
            self.config.proxy_url, self.config.twitch_oauth_token, self
        )
        w.ready.connect(self._on_tg_snap_ready)
        w.finished.connect(w.deleteLater)
        self._aux_workers.append(w)
        w.start()

    # ------------------------------------------------------------------ Cloud Updates (GitHub Releases)
    def _push_update_state(self) -> None:
        self._emit("update", self._update_state)

    def _auto_check_updates(self) -> None:
        if self.config.auto_check_updates and self.config.github_repo.strip():
            self.checkForUpdates(False)

    @Slot(str)
    def openExternalUrl(self, url: str) -> None:
        u = (url or "").strip()
        if u.startswith(("http://", "https://")):
            QDesktopServices.openUrl(QUrl(u))

    @Slot(bool)
    def checkForUpdates(self, manual: bool = True) -> None:
        repo = normalize_github_repo(self.config.github_repo)
        if not repo:
            if manual:
                self._toast("Обновления", "Укажите репозиторий GitHub в Настройки → Система и Трей", "info")
            return
        if self._update_check_worker and self._update_check_worker.isRunning():
            return
        self._update_state["status"] = "checking"
        self._update_state["progress_text"] = "Проверка GitHub Releases..."
        self._push_update_state()

        self._update_check_worker = UpdateCheckWorker(repo, self.config.proxy_url, manual=manual, parent=self)
        self._update_check_worker.result_ready.connect(self._on_update_checked)
        self._update_check_worker.start()

    def _on_update_checked(self, info: dict, manual: bool) -> None:
        self._update_state["info"] = info
        err = info.get("error") or ""
        if err:
            self._update_state["status"] = "error"
            self._update_state["progress_text"] = err
            self._push_update_state()
            if manual:
                self._toast("Проверка обновлений", err, "error")
            return

        if info.get("available"):
            self._update_state["status"] = "available"
            self._update_state["progress"] = 0.0
            self._update_state["progress_text"] = f"Доступна версия v{info.get('latest_version')}"
            self._push_update_state()
            self._log(f"[Обновление] Найдена новая версия v{info.get('latest_version')} (текущая v{APP_VERSION})")
            self._toast(
                f"Доступно обновление v{info.get('latest_version')}",
                "Нажмите «Обновить» в меню слева или в Настройках",
                "success",
            )
        else:
            self._update_state["status"] = "up_to_date"
            self._update_state["progress_text"] = f"Установлена актуальная версия v{APP_VERSION}"
            self._push_update_state()
            if manual:
                self._toast("Обновления", f"У вас последняя версия v{APP_VERSION}", "success")

    @Slot()
    def startUpdateDownload(self) -> None:
        info = self._update_state.get("info") or {}
        asset_url = info.get("asset_url") or ""
        asset_name = info.get("asset_name") or ""
        if not asset_url:
            html_url = info.get("html_url") or ""
            if html_url:
                self.openExternalUrl(html_url)
                self._toast("Открыт релиз GitHub", "Подходящий архив не прикреплён — открыта страница релиза", "info")
            return
        if self._update_dl_worker and self._update_dl_worker.isRunning():
            return

        self._update_state["status"] = "downloading"
        self._update_state["progress"] = 0.0
        self._update_state["progress_text"] = "Подготовка загрузки..."
        self._update_state["downloaded_path"] = ""
        self._push_update_state()

        checksum_url = info.get("checksum_url") or ""
        self._update_dl_worker = UpdateDownloadWorker(
            asset_url, asset_name, self.config.proxy_url, checksum_url=checksum_url, parent=self
        )
        self._update_dl_worker.progress_updated.connect(self._on_update_dl_progress)
        self._update_dl_worker.finished_download.connect(self._on_update_dl_finished)
        self._update_dl_worker.start()

    def _on_update_dl_progress(self, pct: float, text: str) -> None:
        self._update_state["progress"] = round(pct, 1)
        self._update_state["progress_text"] = text
        self._push_update_state()

    def _on_update_dl_finished(self, ok: bool, local_path: str, msg: str) -> None:
        if ok and local_path:
            self._update_state["status"] = "ready"
            self._update_state["progress"] = 100.0
            self._update_state["downloaded_path"] = local_path
            self._update_state["progress_text"] = msg
            self._push_update_state()
            self._log(f"[Обновление] Загружено: {Path(local_path).name}")
            self._toast("Обновление загружено", "Нажмите «Установить сейчас» для применения", "success")
        else:
            self._update_state["status"] = "available" if (self._update_state.get("info") or {}).get("available") else "error"
            self._update_state["progress"] = 0.0
            self._update_state["progress_text"] = msg
            self._push_update_state()
            self._toast("Обновление", msg, "error")

    @Slot()
    def cancelUpdateDownload(self) -> None:
        if self._update_dl_worker and self._update_dl_worker.isRunning():
            self._update_dl_worker.request_stop()

    @Slot()
    def applyDownloadedUpdate(self) -> None:
        dl_path = self._update_state.get("downloaded_path") or ""
        if not dl_path or not os.path.exists(dl_path):
            self._toast("Обновление", "Сначала загрузите пакет обновления", "error")
            return
        if self.active_count() > 0:
            self._toast(
                "Есть активные задачи",
                "Остановите записи/рендер перед перезапуском для обновления",
                "error",
            )
            return

        ok, should_quit, msg = launch_update_installer(dl_path)
        self._log(f"[Обновление] {msg}")
        if not ok:
            self._toast("Ошибка обновления", msg, "error")
            return
        self._toast("Установка обновления", msg, "success")
        if should_quit and hasattr(self._win, "quit_for_update"):
            QTimer.singleShot(400, self._win.quit_for_update)

    @Slot()
    def sendTestNotification(self) -> None:
        send_notification_async(
            self.config,
            "Twitch Recorder Pro — Тест",
            "Уведомления успешно настроены и работают!",
            log_cb=self._log,
        )
        self._toast("Тест отправлен", "Проверьте Telegram / Discord", "info")

    @Slot()
    def clearLogs(self) -> None:
        self.logs.clear()

    # ------------------------------------------------------------------ Resource Stats, Game Mode, Hard Disk Guard & Shutdown
    def _on_resource_stats(self, snap: ResourceSnapshot) -> None:
        self._disk_free_gb = float(snap.disk_free_gb or 0.0)
        prev_game = self._active_game
        self._active_game = snap.active_game or ""
        if self._active_game and not prev_game:
            self._log(f"[Игровой режим] Обнаружена игра ({self._active_game}) — очередь GPU-рендера на паузе.")
        elif prev_game and not self._active_game:
            self._log(f"[Игровой режим] Игра ({prev_game}) закрыта — возобновление очереди GPU.")
            self._pump_blur_queue(force=False)

        self._emit("resources", asdict(snap))
        low_disk = snap.disk_free_gb < self.config.min_free_gb
        if low_disk and not self._disk_warned:
            self._disk_warned = True
            self._log(f"[Диск] ВНИМАНИЕ: осталось {snap.disk_free_gb:.1f} ГБ!")
            send_notification_async(self.config, "Мало места на диске!", f"Осталось {snap.disk_free_gb:.1f} ГБ")
            if self.config.hard_disk_guard and self.live_recorders:
                self._log("[Диск] Hard Disk Guard: аварийная остановка активных записей!")
                self._toast("Hard Disk Guard", f"Записи остановлены — осталось {snap.disk_free_gb:.1f} ГБ", "error")
                self.stopAllRecordings()
        elif not low_disk:
            self._disk_warned = False

    def active_count(self) -> int:
        return len(self.live_recorders) + len(self.vod_workers) + len(self.blur_workers) + len(self.studio_workers)

    def shutdown(self) -> None:
        self.monitor_timer.stop()
        self.queue_timer.stop()
        self._res_monitor.stop()
        self._res_monitor.wait(1500)
        if self._tg_bot and self._tg_bot.isRunning():
            self._tg_bot.stop()
            self._tg_bot.wait(1000)
        for pool in (self.live_recorders, self.vod_workers, self.blur_workers, self.studio_workers):
            for w in list(pool.values()):
                if hasattr(w, "request_stop"):
                    w.request_stop()
        for pool in (self.live_recorders, self.vod_workers, self.blur_workers, self.studio_workers):
            for w in list(pool.values()):
                if w is not None:
                    w.wait(3500)
        if self._lib_worker and self._lib_worker.isRunning():
            self._lib_worker.wait(1000)
        if self._update_dl_worker and self._update_dl_worker.isRunning():
            self._update_dl_worker.request_stop()
            self._update_dl_worker.wait(1500)
        if self._update_check_worker and self._update_check_worker.isRunning():
            self._update_check_worker.wait(1000)
        for w in list(self._aux_workers):
            if w.isRunning():
                w.terminate()
                w.wait(500)
        self.config.save()
