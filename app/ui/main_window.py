from __future__ import annotations

import os
import subprocess
import tempfile
import uuid
from collections import deque
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Deque, Dict, List, Optional

from PySide6.QtCore import QSize, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QStackedWidget,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from app.config import PLATFORMS, AppConfig, BlurRegion, ChannelConfig
from app.recorder import (
    BlurPostProcessWorker,
    ChannelMonitorWorker,
    ClipsBatchWorker,
    LiveRecorderWorker,
    VodDownloadWorker,
    format_duration,
)
from app.services import (
    GlobalHotkeyMonitor,
    ResourceMonitorWorker,
    ResourceSnapshot,
    cleanup_old_recordings,
    get_disk_free_gb,
    send_notification_async,
    set_windows_autostart,
)
from app.tools import (
    detect_available_encoders,
    get_ffmpeg_path,
    get_streamlink_path,
    get_ytdlp_path,
    resolve_encoder,
)
from app.twitch_api import (
    ClipItem,
    StreamStatus,
    VodItem,
    download_preview_bytes,
    parse_vod_id,
)
from app.ui.blur_editor import BlurEditorDialog
from app.ui.channel_settings_dialog import ChannelSettingsDialog
from app.ui.icons import (
    ToggleSwitch,
    apply_windows_dark_titlebar,
    make_app_icon,
    make_icon,
)
from app.ui.studio_pages import (
    CatalogFetchWorker,
    LibraryPage,
    StudioAIPage,
    _field_box,
    _make_subnav,
    _set_subnav_active,
)
from app.ui.widgets import ChannelCard, DropFileCard, JobCard


class ThumbnailBatchWorker(QThread):
    """Fetches live preview thumbnails for online channels in background (#11)."""

    thumb_ready = Signal(str, bytes)  # channel_key, jpeg_bytes

    def __init__(self, targets: Dict[str, str], proxy_url: str = "", parent=None):
        super().__init__(parent)
        self.targets = dict(targets)
        self.proxy_url = proxy_url

    def run(self) -> None:
        for key, url in self.targets.items():
            raw = download_preview_bytes(url, self.proxy_url)
            if raw:
                self.thumb_ready.emit(key, raw)


def _make_empty_state(icon_name: str, title: str, subtitle: str) -> QFrame:
    card = QFrame()
    card.setProperty("surface", "subtle")
    lay = QVBoxLayout(card)
    lay.setContentsMargins(32, 40, 32, 40)
    lay.setSpacing(10)
    lay.setAlignment(Qt.AlignCenter)

    ico = QLabel()
    ico.setPixmap(make_icon(icon_name, "#5D6278", 36).pixmap(36, 36))
    ico.setAlignment(Qt.AlignCenter)

    t = QLabel(title)
    t.setProperty("h2", "true")
    t.setAlignment(Qt.AlignCenter)

    sub = QLabel(subtitle)
    sub.setProperty("muted", "true")
    sub.setAlignment(Qt.AlignCenter)
    sub.setWordWrap(True)
    sub.setFixedWidth(540)
    sub.setMinimumHeight(44)

    lay.addWidget(ico)
    lay.addWidget(t)
    lay.addWidget(sub)
    return card


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Twitch Recorder Pro")
        self.setWindowIcon(make_app_icon(64))
        self.resize(1340, 840)
        self.setMinimumSize(1080, 680)
        apply_windows_dark_titlebar(self)

        self.config = AppConfig.load()
        self.stream_statuses: Dict[str, StreamStatus] = {}
        self.active_recorders: Dict[str, LiveRecorderWorker] = {}
        self.channel_cards: Dict[str, ChannelCard] = {}
        self.manually_stopped: set[str] = set()
        self._force_quit = False
        self._disk_warned = False

        # VOD & Clips
        self.vod_workers: Dict[str, QThread] = {}
        self.vod_cards: Dict[str, JobCard] = {}
        self.vod_blur_regions: Dict[str, List[BlurRegion]] = {}
        self._draft_vod_regions: List[BlurRegion] = []
        self._catalog_worker: Optional[CatalogFetchWorker] = None
        self._fetched_clips: List[ClipItem] = []
        self._clip_checks: List[QCheckBox] = []

        # Smart GPU Blur Render Queue (#33, #34)
        self.blur_workers: Dict[str, BlurPostProcessWorker] = {}
        self.blur_cards: Dict[str, JobCard] = {}
        self.pending_blur_queue: Deque[dict] = deque()
        self._manual_blur_regions: List[BlurRegion] = []

        self._monitor_worker: Optional[ChannelMonitorWorker] = None
        self._thumb_worker: Optional[ThumbnailBatchWorker] = None
        self._available_encoders = detect_available_encoders()
        self._nav_buttons: List[QPushButton] = []

        self._build_ui()
        self._setup_tray()
        self._rebuild_channel_cards()
        self._refresh_preset_combos()
        self._update_sidebar_badges()

        # Timers & Background Services
        self.monitor_timer = QTimer(self)
        self.monitor_timer.timeout.connect(self._check_all_channels)
        if self.config.auto_monitor_enabled:
            self.monitor_timer.start(self.config.check_interval_sec * 1000)

        # Night render queue checker (every 30s)
        self.queue_timer = QTimer(self)
        self.queue_timer.timeout.connect(lambda: self._pump_blur_queue(force=False))
        self.queue_timer.start(30_000)

        # PC Resource Monitor (#35)
        self._res_monitor = ResourceMonitorWorker(lambda: self.config.output_dir, self)
        self._res_monitor.stats_ready.connect(self._on_resource_stats)
        self._res_monitor.start()

        # Global Hotkey F9 for Live Markers (#19)
        self._hotkey = GlobalHotkeyMonitor(self.config.marker_hotkey, self)
        self._hotkey.triggered.connect(self._on_global_marker_hotkey)

        # Startup disk cleanup if enabled (#14)
        if self.config.auto_cleanup_old:
            cnt, gb = cleanup_old_recordings(self.config.output_dir, self.config.max_age_days)
            if cnt > 0:
                self._log(f"[Авто-очистка] Удалено старых записей: {cnt} (освобождено {gb:.2f} ГБ)")

        QTimer.singleShot(600, self._check_all_channels)
        self._log(
            f"Приложение запущено. Кодеки: {', '.join(self._available_encoders)} "
            f"(активный: {resolve_encoder(self.config.encoder)})."
        )

    # ------------------------------------------------------------------ #30 System Tray
    def _setup_tray(self) -> None:
        self.tray = QSystemTrayIcon(make_app_icon(64, recording_dot=False), self)
        self.tray.setToolTip("Twitch Recorder Pro")
        menu = QMenu()
        act_open = QAction("Открыть окно", self)
        act_open.triggered.connect(self._restore_from_tray)
        act_stop = QAction("Остановить все записи", self)
        act_stop.triggered.connect(self._stop_all_recordings)
        act_quit = QAction("Выход", self)
        act_quit.triggered.connect(self._quit_completely)
        menu.addAction(act_open)
        menu.addAction(act_stop)
        menu.addSeparator()
        menu.addAction(act_quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(
            lambda reason: self._restore_from_tray() if reason == QSystemTrayIcon.DoubleClick else None
        )
        self.tray.show()

    def _restore_from_tray(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _quit_completely(self) -> None:
        self._force_quit = True
        self.close()

    # ------------------------------------------------------------------ Layout Shell
    def _build_ui(self) -> None:
        root_widget = QWidget()
        self.setCentralWidget(root_widget)
        shell = QHBoxLayout(root_widget)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(0)

        # ---- Left Sidebar ----
        sidebar = QFrame()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(244)
        sb = QVBoxLayout(sidebar)
        sb.setContentsMargins(14, 18, 14, 14)
        sb.setSpacing(4)

        brand_row = QHBoxLayout()
        brand_row.setSpacing(10)
        logo = QLabel()
        logo.setPixmap(make_app_icon(34).pixmap(34, 34))
        brand_txt = QVBoxLayout()
        brand_txt.setSpacing(0)
        b_title = QLabel("Twitch Recorder")
        b_title.setStyleSheet("font-size: 11.5pt; font-weight: 700; color: #F5F6FA;")
        b_sub = QLabel("Pro Studio Suite")
        b_sub.setProperty("caption", "true")
        brand_txt.addWidget(b_title)
        brand_txt.addWidget(b_sub)
        brand_row.addWidget(logo)
        brand_row.addLayout(brand_txt)
        brand_row.addStretch()
        sb.addLayout(brand_row)
        sb.addSpacing(14)

        self._nav_icons = ["radio", "film", "grid", "blur", "scissors", "sliders", "terminal"]
        nav_labels = [
            "Прямые эфиры",
            "VOD и Клипы",
            "Библиотека",
            "Блюр и Очередь",
            "Монтаж и AI",
            "Настройки",
            "Журнал",
        ]
        for idx, (label, ico_name) in enumerate(zip(nav_labels, self._nav_icons)):
            btn = QPushButton(f"  {label}")
            btn.setProperty("nav", "true")
            btn.setProperty("active", "true" if idx == 0 else "false")
            btn.setIcon(make_icon(ico_name, "#C4B5FD" if idx == 0 else "#9CA0B0", 17))
            btn.setIconSize(QSize(17, 17))
            btn.setCursor(Qt.PointingHandCursor)
            btn.setFixedHeight(40)
            btn.clicked.connect(lambda _, i=idx: self._switch_page(i))
            self._nav_buttons.append(btn)
            sb.addWidget(btn)

        sb.addStretch()

        # Sidebar bottom PC Resource Monitor Card (#35)
        status_card = QFrame()
        status_card.setProperty("surface", "subtle")
        sc_l = QVBoxLayout(status_card)
        sc_l.setContentsMargins(12, 10, 12, 10)
        sc_l.setSpacing(6)

        enc_name = resolve_encoder(self.config.encoder)
        gpu_ok = enc_name in ("h264_nvenc", "h264_qsv", "h264_amf")
        gpu_row = QHBoxLayout()
        gpu_row.setSpacing(6)
        gpu_ico = QLabel()
        gpu_ico.setPixmap(make_icon("cpu", "#4ADE80" if gpu_ok else "#9CA0B0", 15).pixmap(15, 15))
        self.lbl_gpu_status = QLabel("GPU NVENC готов" if enc_name == "h264_nvenc" else f"Кодек: {enc_name}")
        self.lbl_gpu_status.setStyleSheet(
            f"font-size: 9pt; font-weight: 600; color: {'#4ADE80' if gpu_ok else '#9CA0B0'};"
        )
        gpu_row.addWidget(gpu_ico)
        gpu_row.addWidget(self.lbl_gpu_status)
        gpu_row.addStretch()
        sc_l.addLayout(gpu_row)

        self.lbl_res_cpu_gpu = QLabel("CPU: —  ·  GPU: —")
        self.lbl_res_cpu_gpu.setProperty("caption", "true")
        self.lbl_res_disk = QLabel("Диск: проверка...")
        self.lbl_res_disk.setProperty("caption", "true")
        self.lbl_sb_counters = QLabel("Записей: 0  ·  Очередь: 0")
        self.lbl_sb_counters.setProperty("caption", "true")

        sc_l.addWidget(self.lbl_res_cpu_gpu)
        sc_l.addWidget(self.lbl_res_disk)
        sc_l.addWidget(self.lbl_sb_counters)

        btn_folder = QPushButton("Папка записей")
        btn_folder.setIcon(make_icon("folder", "#E8E9EE", 15))
        btn_folder.setCursor(Qt.PointingHandCursor)
        btn_folder.clicked.connect(self._open_recordings_folder)
        sc_l.addWidget(btn_folder)

        sb.addWidget(status_card)
        shell.addWidget(sidebar)

        # ---- Main Stacked Pages ----
        self.pages = QStackedWidget()
        self.pages.addWidget(self._build_live_page())          # 0
        self.pages.addWidget(self._build_vod_clips_page())     # 1
        self.library_page = LibraryPage(lambda: self.config, self._reveal_file)
        self.library_page.send_to_blur.connect(self._open_file_in_blur)
        self.library_page.send_to_studio.connect(self._open_file_in_studio)
        self.pages.addWidget(self.library_page)                # 2
        self.pages.addWidget(self._build_blur_page())          # 3
        self.studio_page = StudioAIPage(lambda: self.config, self._reveal_file)
        self.studio_page.log_message.connect(self._log)
        self.pages.addWidget(self.studio_page)                 # 4
        self.pages.addWidget(self._build_settings_page())      # 5
        self.pages.addWidget(self._build_logs_page())          # 6
        shell.addWidget(self.pages, 1)

    def _switch_page(self, index: int) -> None:
        self.pages.setCurrentIndex(index)
        for i, btn in enumerate(self._nav_buttons):
            active = i == index
            btn.setProperty("active", "true" if active else "false")
            btn.setIcon(make_icon(self._nav_icons[i], "#C4B5FD" if active else "#9CA0B0", 17))
            btn.style().unpolish(btn)
            btn.style().polish(btn)
        if index == 2:
            self.library_page.refresh()

    def _open_file_in_blur(self, path: str) -> None:
        self.edit_blur_file.setText(path)
        self._switch_page(3)

    def _open_file_in_studio(self, path: str) -> None:
        self.studio_page.load_video_path(path)
        self._switch_page(4)

    # ------------------------------------------------------------------ Page 0: Live Channels
    def _build_live_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(28, 22, 28, 18)
        lay.setSpacing(14)

        t_col = QVBoxLayout()
        t_col.setSpacing(2)
        h1 = QLabel("Прямые эфиры")
        h1.setProperty("h1", "true")
        sub = QLabel("Одновременная запись Twitch, YouTube, Kick, VK Play и Trovo — с живыми превью, чатом и блюром")
        sub.setProperty("muted", "true")
        t_col.addWidget(h1)
        t_col.addWidget(sub)
        lay.addLayout(t_col)

        # Action toolbar
        hdr = QHBoxLayout()
        hdr.setSpacing(10)

        mon_wrap = QFrame()
        mon_wrap.setProperty("surface", "subtle")
        mw_l = QHBoxLayout(mon_wrap)
        mw_l.setContentsMargins(12, 6, 12, 6)
        mw_l.setSpacing(10)
        lbl_mon = QLabel("Автомониторинг")
        lbl_mon.setStyleSheet("font-size: 9.5pt; font-weight: 600;")
        self.sw_monitor = ToggleSwitch(self.config.auto_monitor_enabled)
        self.sw_monitor.toggled.connect(self._on_auto_monitor_toggled)
        mw_l.addWidget(lbl_mon)
        mw_l.addWidget(self.sw_monitor)
        hdr.addWidget(mon_wrap)

        btn_refresh = QPushButton("Обновить статусы")
        btn_refresh.setIcon(make_icon("refresh", "#E8E9EE", 15))
        btn_refresh.setCursor(Qt.PointingHandCursor)
        btn_refresh.clicked.connect(self._check_all_channels)
        hdr.addWidget(btn_refresh)
        hdr.addStretch()

        btn_rec_all = QPushButton("Записать всех онлайн")
        btn_rec_all.setIcon(make_icon("record", "#FFFFFF", 13))
        btn_rec_all.setProperty("role", "success")
        btn_rec_all.setCursor(Qt.PointingHandCursor)
        btn_rec_all.clicked.connect(self._record_all_online)
        hdr.addWidget(btn_rec_all)

        btn_stop_all = QPushButton("Остановить все")
        btn_stop_all.setIcon(make_icon("stop", "#FFFFFF", 13))
        btn_stop_all.setProperty("role", "danger")
        btn_stop_all.setCursor(Qt.PointingHandCursor)
        btn_stop_all.clicked.connect(self._stop_all_recordings)
        hdr.addWidget(btn_stop_all)
        lay.addLayout(hdr)

        # Add Channel Card with Multi-Platform Selector (#29)
        add_card = QFrame()
        add_card.setProperty("card", "true")
        ac_l = QHBoxLayout(add_card)
        ac_l.setContentsMargins(14, 12, 14, 12)
        ac_l.setSpacing(10)

        self.combo_platform = QComboBox()
        for label, pid in PLATFORMS:
            self.combo_platform.addItem(label, pid)
        self.combo_platform.setFixedWidth(145)

        self.edit_channel = QLineEdit()
        self.edit_channel.setPlaceholderText(
            "Ник стримера или ссылка (twitch.tv, kick.com, youtube.com, live.vkvideo.ru, trovo.live)..."
        )
        self.edit_channel.returnPressed.connect(self._add_channels)

        btn_add = QPushButton("Добавить канал")
        btn_add.setIcon(make_icon("plus", "#FFFFFF", 16))
        btn_add.setProperty("role", "primary")
        btn_add.setCursor(Qt.PointingHandCursor)
        btn_add.clicked.connect(self._add_channels)

        ac_l.addWidget(self.combo_platform)
        ac_l.addWidget(self.edit_channel, 1)
        ac_l.addWidget(btn_add)
        lay.addWidget(add_card)

        # Scrollable Channel Cards
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.channels_container = QWidget()
        self.channels_container.setObjectName("ScrollContent")
        self.channels_layout = QVBoxLayout(self.channels_container)
        self.channels_layout.setContentsMargins(0, 0, 6, 0)
        self.channels_layout.setSpacing(12)
        self.channels_layout.addStretch()
        scroll.setWidget(self.channels_container)
        lay.addWidget(scroll, 1)
        return page

    # ------------------------------------------------------------------ Page 1: VOD & Clips Browser (#26, #27)
    def _build_vod_clips_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(28, 22, 28, 18)
        lay.setSpacing(14)

        h1 = QLabel("VOD и Клипы")
        h1.setProperty("h1", "true")
        sub = QLabel("Скачивание прошлых записей по ссылке, просмотр архива VOD и пакетная загрузка топ-клипов канала")
        sub.setProperty("muted", "true")
        lay.addWidget(h1)
        lay.addWidget(sub)

        nav_row, self._vod_sub_btns = _make_subnav(
            ["Загрузка по ссылке", "Браузер VOD канала", "Топ-клипы и Подборки"],
            self._switch_vod_sub,
        )
        lay.addLayout(nav_row)

        self.vod_stack = QStackedWidget()

        # Sub-tab 0: Direct VOD URL form
        direct_card = QFrame()
        direct_card.setProperty("card", "true")
        cl = QVBoxLayout(direct_card)
        cl.setContentsMargins(18, 16, 18, 16)
        cl.setSpacing(12)

        r1 = QHBoxLayout()
        r1.setSpacing(12)
        self.edit_vod_url = QLineEdit()
        self.edit_vod_url.setPlaceholderText("https://www.twitch.tv/videos/1234567890 или ссылка на клип/YouTube")
        self.combo_vod_quality = QComboBox()
        self.combo_vod_quality.addItems(["best", "1080p", "720p", "480p", "360p"])
        self.combo_vod_quality.setFixedWidth(125)
        r1.addWidget(_field_box("Ссылка на VOD / видео", self.edit_vod_url), 1)
        r1.addWidget(_field_box("Качество", self.combo_vod_quality))
        cl.addLayout(r1)

        r2 = QHBoxLayout()
        r2.setSpacing(12)
        self.edit_vod_start = QLineEdit()
        self.edit_vod_start.setPlaceholderText("00:00:00")
        self.edit_vod_start.setFixedWidth(115)
        self.edit_vod_end = QLineEdit()
        self.edit_vod_end.setPlaceholderText("01:00:00")
        self.edit_vod_end.setFixedWidth(115)
        self.combo_vod_blur_preset = QComboBox()
        self.combo_vod_blur_preset.setMinimumWidth(220)

        self.btn_vod_custom_blur = QPushButton("Зоны на кадре")
        self.btn_vod_custom_blur.setIcon(make_icon("crop", "#E8E9EE", 15))
        self.btn_vod_custom_blur.clicked.connect(self._open_vod_blur_editor)

        btn_dl = QPushButton("Скачать VOD")
        btn_dl.setIcon(make_icon("download", "#FFFFFF", 16))
        btn_dl.setProperty("role", "primary")
        btn_dl.clicked.connect(self._start_vod_download)

        r2.addWidget(_field_box("От (опц.)", self.edit_vod_start))
        r2.addWidget(_field_box("До (опц.)", self.edit_vod_end))
        r2.addWidget(_field_box("Авто-блюр после загрузки", self.combo_vod_blur_preset))
        r2.addStretch()
        r2.addWidget(self.btn_vod_custom_blur)
        r2.addWidget(btn_dl)
        cl.addLayout(r2)
        self.vod_stack.addWidget(direct_card)

        # Sub-tab 1: In-App Channel VOD Browser (#26)
        vod_br = QFrame()
        vod_br.setProperty("card", "true")
        vb_l = QVBoxLayout(vod_br)
        vb_l.setContentsMargins(16, 14, 16, 14)
        vb_l.setSpacing(10)

        vb_top = QHBoxLayout()
        self.edit_br_vod_ch = QLineEdit()
        self.edit_br_vod_ch.setPlaceholderText("Ник канала Twitch (напр. shroud)...")
        self.edit_br_vod_ch.returnPressed.connect(self._fetch_vods_catalog)
        btn_load_vods = QPushButton("Загрузить архив VOD")
        btn_load_vods.setIcon(make_icon("search", "#FFFFFF", 15))
        btn_load_vods.setProperty("role", "primary")
        btn_load_vods.clicked.connect(self._fetch_vods_catalog)
        vb_top.addWidget(self.edit_br_vod_ch, 1)
        vb_top.addWidget(btn_load_vods)
        vb_l.addLayout(vb_top)

        vb_scroll = QScrollArea()
        vb_scroll.setWidgetResizable(True)
        vb_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        vb_scroll.setMinimumHeight(210)
        self.vod_catalog_box = QWidget()
        self.vod_catalog_box.setObjectName("ScrollContent")
        self.vod_catalog_layout = QVBoxLayout(self.vod_catalog_box)
        self.vod_catalog_layout.setContentsMargins(0, 0, 4, 0)
        self.vod_catalog_layout.setSpacing(8)
        self.vod_catalog_layout.addStretch()
        vb_scroll.setWidget(self.vod_catalog_box)
        vb_l.addWidget(vb_scroll)
        self.vod_stack.addWidget(vod_br)

        # Sub-tab 2: Top Clips Browser & Batch Compilation (#26, #27)
        clips_br = QFrame()
        clips_br.setProperty("card", "true")
        cb_l = QVBoxLayout(clips_br)
        cb_l.setContentsMargins(16, 14, 16, 14)
        cb_l.setSpacing(10)

        cb_top = QHBoxLayout()
        self.edit_br_clip_ch = QLineEdit()
        self.edit_br_clip_ch.setPlaceholderText("Ник канала Twitch...")
        self.edit_br_clip_ch.returnPressed.connect(self._fetch_clips_catalog)
        self.combo_clip_period = QComboBox()
        self.combo_clip_period.addItem("За день", "LAST_DAY")
        self.combo_clip_period.addItem("За неделю", "LAST_WEEK")
        self.combo_clip_period.addItem("За месяц", "LAST_MONTH")
        self.combo_clip_period.addItem("За всё время", "ALL_TIME")
        self.combo_clip_period.setCurrentIndex(1)
        self.combo_clip_period.setFixedWidth(145)

        btn_load_clips = QPushButton("Найти клипы")
        btn_load_clips.setIcon(make_icon("search", "#FFFFFF", 15))
        btn_load_clips.setProperty("role", "primary")
        btn_load_clips.clicked.connect(self._fetch_clips_catalog)

        btn_dl_clips = QPushButton("Скачать выбранные")
        btn_dl_clips.setIcon(make_icon("download", "#E8E9EE", 15))
        btn_dl_clips.clicked.connect(lambda: self._download_selected_clips(merge=False))

        btn_merge_clips = QPushButton("Склеить в подборку")
        btn_merge_clips.setIcon(make_icon("sparkles", "#C4B5FD", 15))
        btn_merge_clips.setProperty("role", "soft-primary")
        btn_merge_clips.clicked.connect(lambda: self._download_selected_clips(merge=True))

        cb_top.addWidget(self.edit_br_clip_ch, 1)
        cb_top.addWidget(self.combo_clip_period)
        cb_top.addWidget(btn_load_clips)
        cb_top.addWidget(btn_dl_clips)
        cb_top.addWidget(btn_merge_clips)
        cb_l.addLayout(cb_top)

        cb_scroll = QScrollArea()
        cb_scroll.setWidgetResizable(True)
        cb_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        cb_scroll.setMinimumHeight(210)
        self.clips_catalog_box = QWidget()
        self.clips_catalog_box.setObjectName("ScrollContent")
        self.clips_catalog_layout = QVBoxLayout(self.clips_catalog_box)
        self.clips_catalog_layout.setContentsMargins(0, 0, 4, 0)
        self.clips_catalog_layout.setSpacing(6)
        self.clips_catalog_layout.addStretch()
        cb_scroll.setWidget(self.clips_catalog_box)
        cb_l.addWidget(cb_scroll)
        self.vod_stack.addWidget(clips_br)

        lay.addWidget(self.vod_stack)

        # Downloads Queue
        lbl_q = QLabel("Активные и завершённые загрузки")
        lbl_q.setProperty("h2", "true")
        lay.addWidget(lbl_q)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.vods_container = QWidget()
        self.vods_container.setObjectName("ScrollContent")
        self.vods_layout = QVBoxLayout(self.vods_container)
        self.vods_layout.setContentsMargins(0, 0, 6, 0)
        self.vods_layout.setSpacing(10)
        self.vod_empty_widget = _make_empty_state(
            "film",
            "Загрузок VOD и клипов пока нет",
            "Вставьте ссылку выше или откройте вкладку «Браузер VOD канала» / «Топ-клипы».",
        )
        self.vods_layout.addWidget(self.vod_empty_widget)
        self.vods_layout.addStretch()
        scroll.setWidget(self.vods_container)
        lay.addWidget(scroll, 1)
        return page

    def _switch_vod_sub(self, idx: int) -> None:
        self.vod_stack.setCurrentIndex(idx)
        _set_subnav_active(self._vod_sub_btns, idx)

    # ------------------------------------------------------------------ Page 3: Blur & Smart GPU Render Queue (#33, #34)
    def _build_blur_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(28, 22, 28, 18)
        lay.setSpacing(14)

        hdr = QHBoxLayout()
        t_col = QVBoxLayout()
        t_col.setSpacing(2)
        h1 = QLabel("Блюр видео и Умная очередь GPU")
        h1.setProperty("h1", "true")
        sub = QLabel("Перетащите видеофайл сюда, настройте мягкий блюр / логотип / AI-детектор и управляйте очередью рендера")
        sub.setProperty("muted", "true")
        t_col.addWidget(h1)
        t_col.addWidget(sub)
        hdr.addLayout(t_col)
        hdr.addStretch()

        # Night Render quick status & override button (#34)
        self.btn_run_queue_now = QPushButton("Обработать очередь сейчас")
        self.btn_run_queue_now.setIcon(make_icon("play", "#FFFFFF", 14))
        self.btn_run_queue_now.setProperty("role", "success")
        self.btn_run_queue_now.setCursor(Qt.PointingHandCursor)
        self.btn_run_queue_now.clicked.connect(lambda: self._pump_blur_queue(force=True))
        hdr.addWidget(self.btn_run_queue_now)
        lay.addLayout(hdr)

        card = DropFileCard()
        card.file_dropped.connect(lambda p: self.edit_blur_file.setText(p))
        cl = QVBoxLayout(card)
        cl.setContentsMargins(18, 16, 18, 16)
        cl.setSpacing(12)

        r1 = QHBoxLayout()
        r1.setSpacing(10)
        self.edit_blur_file = QLineEdit()
        self.edit_blur_file.setPlaceholderText("Перетащите видеофайл (.mp4 / .mkv) или нажмите «Выбрать файл»...")
        btn_browse = QPushButton("Выбрать файл")
        btn_browse.setIcon(make_icon("folder", "#E8E9EE", 15))
        btn_browse.clicked.connect(self._browse_blur_video)
        r1.addWidget(self.edit_blur_file, 1)
        r1.addWidget(btn_browse)
        cl.addLayout(r1)

        r2 = QHBoxLayout()
        r2.setSpacing(12)
        self.combo_manual_preset = QComboBox()
        self.combo_manual_preset.setMinimumWidth(220)
        self.combo_manual_preset.currentIndexChanged.connect(self._on_manual_preset_changed)

        self.btn_manual_edit_zones = QPushButton("Выделить баннер на кадре")
        self.btn_manual_edit_zones.setIcon(make_icon("crop", "#C4B5FD", 15))
        self.btn_manual_edit_zones.setProperty("role", "soft-primary")
        self.btn_manual_edit_zones.clicked.connect(self._open_manual_blur_editor)

        self.sw_delete_orig = ToggleSwitch(False)
        lbl_del = QLabel("Удалить исходник")
        lbl_del.setProperty("caption", "true")

        self.sw_blur_norm = ToggleSwitch(self.config.normalize_audio_default)
        lbl_bn = QLabel("Выровнять звук")
        lbl_bn.setProperty("caption", "true")

        btn_run = QPushButton("В очередь блюра")
        btn_run.setIcon(make_icon("play", "#FFFFFF", 14))
        btn_run.setProperty("role", "primary")
        btn_run.clicked.connect(self._start_manual_blur)

        r2.addWidget(_field_box("Пресет зон блюра", self.combo_manual_preset))
        r2.addWidget(self.btn_manual_edit_zones)
        r2.addWidget(self.sw_delete_orig)
        r2.addWidget(lbl_del)
        r2.addWidget(self.sw_blur_norm)
        r2.addWidget(lbl_bn)
        r2.addStretch()
        r2.addWidget(btn_run)
        cl.addLayout(r2)
        lay.addWidget(card)

        self.lbl_queue_info = QLabel("Очередь рендера")
        self.lbl_queue_info.setProperty("h2", "true")
        lay.addWidget(self.lbl_queue_info)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.blurs_container = QWidget()
        self.blurs_container.setObjectName("ScrollContent")
        self.blurs_layout = QVBoxLayout(self.blurs_container)
        self.blurs_layout.setContentsMargins(0, 0, 6, 0)
        self.blurs_layout.setSpacing(10)
        self.blur_empty_widget = _make_empty_state(
            "blur",
            "Очередь блюра пуста",
            "Сюда попадают автоматические задачи после эфиров и VOD, а также ручная обработка. "
            "Лимит одновременных рендеров GPU и ночной режим настраиваются в разделе «Настройки».",
        )
        self.blurs_layout.addWidget(self.blur_empty_widget)
        self.blurs_layout.addStretch()
        scroll.setWidget(self.blurs_container)
        lay.addWidget(scroll, 1)
        return page

    # ------------------------------------------------------------------ Page 5: Settings (All System / Network / Notify / Queue)
    def _build_settings_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(28, 22, 28, 18)
        lay.setSpacing(14)

        h1 = QLabel("Настройки")
        h1.setProperty("h1", "true")
        sub = QLabel("Хранилище, лимит GPU-очереди, ночной рендер, Telegram/Discord, OAuth, прокси, трей и автозапуск")
        sub.setProperty("muted", "true")
        lay.addWidget(h1)
        lay.addWidget(sub)

        nav_row, self._set_sub_btns = _make_subnav(
            ["Файлы и GPU-очередь", "Сеть и Twitch OAuth", "Telegram / Discord", "Система и Трей"],
            self._switch_settings_sub,
        )
        lay.addLayout(nav_row)

        self.settings_stack = QStackedWidget()

        # ---- Tab 0: Storage, Disk Guard, GPU & Night Render ----
        s0 = QWidget()
        s0_l = QVBoxLayout(s0)
        s0_l.setContentsMargins(0, 0, 0, 0)
        s0_l.setSpacing(12)

        c1 = QFrame()
        c1.setProperty("card", "true")
        c1_l = QVBoxLayout(c1)
        c1_l.setContentsMargins(18, 16, 18, 16)
        c1_l.setSpacing(12)

        dir_row = QHBoxLayout()
        self.edit_out_dir = QLineEdit(self.config.output_dir)
        btn_dir = QPushButton("Выбрать папку")
        btn_dir.setIcon(make_icon("folder", "#E8E9EE", 15))
        btn_dir.clicked.connect(self._pick_output_dir)
        dir_row.addWidget(self.edit_out_dir, 1)
        dir_row.addWidget(btn_dir)
        c1_l.addLayout(dir_row)

        r_fmt = QHBoxLayout()
        r_fmt.setSpacing(14)
        self.combo_container = QComboBox()
        self.combo_container.addItems(["mp4", "mkv"])
        self.combo_container.setCurrentText(self.config.container)
        self.combo_container.setFixedWidth(115)

        self.spin_interval = QSpinBox()
        self.spin_interval.setRange(10, 600)
        self.spin_interval.setValue(self.config.check_interval_sec)
        self.spin_interval.setSuffix(" сек")
        self.spin_interval.setFixedWidth(130)

        self.spin_vod_frag = QSpinBox()
        self.spin_vod_frag.setRange(1, 16)
        self.spin_vod_frag.setValue(self.config.vod_concurrent_fragments)
        self.spin_vod_frag.setFixedWidth(115)

        self.spin_min_gb = QSpinBox()
        self.spin_min_gb.setRange(1, 500)
        self.spin_min_gb.setValue(self.config.min_free_gb)
        self.spin_min_gb.setSuffix(" ГБ")
        self.spin_min_gb.setFixedWidth(120)

        self.sw_auto_clean = ToggleSwitch(self.config.auto_cleanup_old)
        self.spin_max_days = QSpinBox()
        self.spin_max_days.setRange(1, 365)
        self.spin_max_days.setValue(self.config.max_age_days)
        self.spin_max_days.setSuffix(" дн.")
        self.spin_max_days.setFixedWidth(110)

        r_fmt.addWidget(_field_box("Формат", self.combo_container))
        r_fmt.addWidget(_field_box("Проверка эфиров", self.spin_interval))
        r_fmt.addWidget(_field_box("Потоков VOD", self.spin_vod_frag))
        r_fmt.addWidget(_field_box("Предупр. диска <", self.spin_min_gb))
        r_fmt.addWidget(_field_box("Авто-очистка", self.sw_auto_clean))
        r_fmt.addWidget(_field_box("Старше дней", self.spin_max_days))
        r_fmt.addStretch()
        c1_l.addLayout(r_fmt)
        s0_l.addWidget(c1)

        c2 = QFrame()
        c2.setProperty("card", "true")
        c2_l = QVBoxLayout(c2)
        c2_l.setContentsMargins(18, 16, 18, 16)
        c2_l.setSpacing(12)

        r_enc = QHBoxLayout()
        r_enc.setSpacing(14)
        self.combo_encoder = QComboBox()
        self.combo_encoder.setMinimumWidth(260)
        self.combo_encoder.addItem(f"Авто ({resolve_encoder('auto')})", "auto")
        for enc in self._available_encoders:
            lbl = {
                "h264_nvenc": "NVIDIA NVENC (GPU — быстро)",
                "h264_qsv": "Intel QuickSync (GPU)",
                "h264_amf": "AMD AMF (GPU)",
                "libx264": "CPU (libx264)",
            }.get(enc, enc)
            self.combo_encoder.addItem(lbl, enc)
        eidx = self.combo_encoder.findData(self.config.encoder)
        if eidx >= 0:
            self.combo_encoder.setCurrentIndex(eidx)

        self.spin_quality = QSpinBox()
        self.spin_quality.setRange(14, 35)
        self.spin_quality.setValue(self.config.video_quality)
        self.spin_quality.setFixedWidth(110)

        self.spin_max_blurs = QSpinBox()
        self.spin_max_blurs.setRange(1, 6)
        self.spin_max_blurs.setValue(self.config.max_concurrent_blurs)
        self.spin_max_blurs.setFixedWidth(120)

        r_enc.addWidget(_field_box("Видеокодек (блюр / монтаж)", self.combo_encoder))
        r_enc.addWidget(_field_box("Качество CQ/CRF", self.spin_quality))
        r_enc.addWidget(_field_box("Лимит рендеров GPU", self.spin_max_blurs))
        r_enc.addStretch()
        c2_l.addLayout(r_enc)

        # Night Render Mode (#34)
        r_night = QHBoxLayout()
        r_night.setSpacing(12)
        self.sw_night = ToggleSwitch(self.config.night_render_enabled)
        self.edit_night_s = QLineEdit(self.config.night_render_start)
        self.edit_night_s.setFixedWidth(90)
        self.edit_night_e = QLineEdit(self.config.night_render_end)
        self.edit_night_e.setFixedWidth(90)
        lbl_nr = QLabel("Ночной рендер: днём только запись, а блюр стартует автоматически в указанное окно")
        lbl_nr.setProperty("muted", "true")
        r_night.addWidget(self.sw_night)
        r_night.addWidget(lbl_nr)
        r_night.addWidget(QLabel("С:"))
        r_night.addWidget(self.edit_night_s)
        r_night.addWidget(QLabel("До:"))
        r_night.addWidget(self.edit_night_e)
        r_night.addStretch()
        c2_l.addLayout(r_night)

        # Diagnostics badges
        diag = QHBoxLayout()
        for name, ok in [
            ("FFmpeg", bool(get_ffmpeg_path())),
            ("Streamlink", bool(get_streamlink_path())),
            ("yt-dlp", bool(get_ytdlp_path())),
            ("NVIDIA NVENC", "h264_nvenc" in self._available_encoders),
        ]:
            pill = QLabel(f"{name}: {'OK' if ok else 'Нет'}")
            pill.setStyleSheet(
                f"background-color: {'rgba(34,197,94,0.14)' if ok else '#20232E'}; "
                f"color: {'#4ADE80' if ok else '#8E92A4'}; border: 1px solid {'rgba(34,197,94,0.35)' if ok else '#2D3140'}; "
                "border-radius: 6px; padding: 4px 10px; font-size: 9pt; font-weight: 600;"
            )
            diag.addWidget(pill)
        diag.addStretch()
        c2_l.addLayout(diag)
        s0_l.addWidget(c2)
        s0_l.addStretch()
        self.settings_stack.addWidget(s0)

        # ---- Tab 1: Network, OAuth & Proxy (#12) ----
        s1 = QFrame()
        s1.setProperty("card", "true")
        s1_l = QVBoxLayout(s1)
        s1_l.setContentsMargins(20, 18, 20, 18)
        s1_l.setSpacing(14)

        self.edit_oauth = QLineEdit(self.config.twitch_oauth_token)
        self.edit_oauth.setEchoMode(QLineEdit.Password)
        self.edit_oauth.setPlaceholderText("auth-token из cookies Twitch (для саб-онли VOD и отключения рекламы)")
        self.edit_proxy = QLineEdit(self.config.proxy_url)
        self.edit_proxy.setPlaceholderText("http://user:pass@host:port или socks5://127.0.0.1:1080 (опционально)")

        s1_l.addWidget(_field_box("Twitch OAuth / auth-token (опционально)", self.edit_oauth))
        s1_l.addWidget(_field_box("HTTP / SOCKS5 прокси (опционально)", self.edit_proxy))
        s1_l.addStretch()
        self.settings_stack.addWidget(s1)

        # ---- Tab 2: Telegram & Discord Notifications (#32) ----
        s2 = QFrame()
        s2.setProperty("card", "true")
        s2_l = QVBoxLayout(s2)
        s2_l.setContentsMargins(20, 18, 20, 18)
        s2_l.setSpacing(12)

        tg_row = QHBoxLayout()
        tg_row.setSpacing(12)
        self.edit_tg_tok = QLineEdit(self.config.telegram_bot_token)
        self.edit_tg_tok.setPlaceholderText("123456789:AAH...")
        self.edit_tg_chat = QLineEdit(self.config.telegram_chat_id)
        self.edit_tg_chat.setPlaceholderText("Ваш Chat ID (напр. 123456789)")
        tg_row.addWidget(_field_box("Telegram Bot Token (@BotFather)", self.edit_tg_tok), 2)
        tg_row.addWidget(_field_box("Telegram Chat ID", self.edit_tg_chat), 1)
        s2_l.addLayout(tg_row)

        self.edit_dc_wh = QLineEdit(self.config.discord_webhook_url)
        self.edit_dc_wh.setPlaceholderText("https://discord.com/api/webhooks/...")
        s2_l.addWidget(_field_box("Discord Webhook URL", self.edit_dc_wh))

        ev_row = QHBoxLayout()
        ev_row.setSpacing(16)
        self.sw_n_live = ToggleSwitch(self.config.notify_on_live)
        self.sw_n_fin = ToggleSwitch(self.config.notify_on_finish)
        self.sw_n_blur = ToggleSwitch(self.config.notify_on_blur)
        btn_test_n = QPushButton("Отправить тест")
        btn_test_n.setIcon(make_icon("bell", "#C4B5FD", 15))
        btn_test_n.setProperty("role", "soft-primary")
        btn_test_n.clicked.connect(self._send_test_notification)

        ev_row.addWidget(_field_box("Старт эфира", self.sw_n_live))
        ev_row.addWidget(_field_box("Конец записи", self.sw_n_fin))
        ev_row.addWidget(_field_box("Блюр готов", self.sw_n_blur))
        ev_row.addStretch()
        ev_row.addWidget(btn_test_n)
        s2_l.addLayout(ev_row)
        s2_l.addStretch()
        self.settings_stack.addWidget(s2)

        # ---- Tab 3: System, Tray, Autostart & Hotkey (#19, #30, #31) ----
        s3 = QFrame()
        s3.setProperty("card", "true")
        s3_l = QVBoxLayout(s3)
        s3_l.setContentsMargins(20, 18, 20, 18)
        s3_l.setSpacing(14)

        r_tr = QHBoxLayout()
        self.sw_tray = ToggleSwitch(self.config.minimize_to_tray)
        r_tr.addWidget(QLabel("Сворачивать в системный трей Windows при закрытии окна (запись продолжится в фоне)"), 1)
        r_tr.addWidget(self.sw_tray)
        s3_l.addLayout(r_tr)

        r_as = QHBoxLayout()
        self.sw_autostart = ToggleSwitch(self.config.start_with_windows)
        r_as.addWidget(QLabel("Автозапуск вместе с Windows (тихий запуск свёрнутым в трей)"), 1)
        r_as.addWidget(self.sw_autostart)
        s3_l.addLayout(r_as)

        r_hk = QHBoxLayout()
        self.combo_hotkey = QComboBox()
        self.combo_hotkey.addItems([f"F{i}" for i in range(1, 13)])
        self.combo_hotkey.setCurrentText(self.config.marker_hotkey)
        self.combo_hotkey.setFixedWidth(110)
        r_hk.addWidget(_field_box("Глобальная клавиша маркера хайлайта во время записи", self.combo_hotkey))
        r_hk.addStretch()
        s3_l.addLayout(r_hk)
        s3_l.addStretch()
        self.settings_stack.addWidget(s3)

        lay.addWidget(self.settings_stack, 1)

        save_row = QHBoxLayout()
        btn_save = QPushButton("Сохранить все настройки")
        btn_save.setIcon(make_icon("save", "#FFFFFF", 16))
        btn_save.setProperty("role", "primary")
        btn_save.setCursor(Qt.PointingHandCursor)
        btn_save.clicked.connect(self._save_settings)
        self.lbl_saved_hint = QLabel("")
        self.lbl_saved_hint.setStyleSheet("color: #4ADE80; font-size: 9.5pt; font-weight: 600;")
        save_row.addWidget(btn_save)
        save_row.addSpacing(12)
        save_row.addWidget(self.lbl_saved_hint)
        save_row.addStretch()
        lay.addLayout(save_row)
        return page

    def _switch_settings_sub(self, idx: int) -> None:
        self.settings_stack.setCurrentIndex(idx)
        _set_subnav_active(self._set_sub_btns, idx)

    # ------------------------------------------------------------------ Page 6: Logs
    def _build_logs_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(28, 22, 28, 18)
        lay.setSpacing(14)

        hdr = QHBoxLayout()
        t_col = QVBoxLayout()
        t_col.setSpacing(2)
        h1 = QLabel("Журнал событий")
        h1.setProperty("h1", "true")
        sub = QLabel("События записи, чата, мониторинга, уведомлений, AI и обработки FFmpeg")
        sub.setProperty("muted", "true")
        t_col.addWidget(h1)
        t_col.addWidget(sub)
        hdr.addLayout(t_col)
        hdr.addStretch()

        btn_copy = QPushButton("Копировать")
        btn_copy.setIcon(make_icon("copy", "#E8E9EE", 15))
        btn_copy.clicked.connect(lambda: QApplication.clipboard().setText(self.txt_logs.toPlainText()))
        btn_clear = QPushButton("Очистить")
        btn_clear.setIcon(make_icon("trash", "#F87171", 15))
        btn_clear.setProperty("role", "ghost-danger")
        btn_clear.clicked.connect(lambda: self.txt_logs.clear())
        hdr.addWidget(btn_copy)
        hdr.addWidget(btn_clear)
        lay.addLayout(hdr)

        self.txt_logs = QPlainTextEdit()
        self.txt_logs.setObjectName("LogConsole")
        self.txt_logs.setReadOnly(True)
        self.txt_logs.setMaximumBlockCount(2000)
        lay.addWidget(self.txt_logs, 1)
        return page

    # ------------------------------------------------------------------ Channels Management
    def _add_channels(self) -> None:
        raw = self.edit_channel.text().strip()
        if not raw:
            return
        def_plat = self.combo_platform.currentData() or "twitch"
        tokens = [t.strip() for t in raw.replace(",", " ").replace("\n", " ").split() if t.strip()]
        added = []
        for tok in tokens:
            plat, uname = ChannelConfig.parse_input(tok, def_plat)
            if not uname:
                continue
            key = f"{plat}:{uname.lower()}"
            if self.config.get_channel(key):
                continue
            ch = ChannelConfig(username=uname, platform=plat, quality="best", auto_record=True, blur_mode="postprocess")
            self.config.channels.append(ch)
            added.append(f"{uname} ({plat})")

        if added:
            self.config.save()
            self.edit_channel.clear()
            self._rebuild_channel_cards()
            self._refresh_preset_combos()
            self._log(f"Добавлены каналы: {', '.join(added)}")
            self._check_all_channels()
        else:
            QMessageBox.information(self, "Каналы", "Этот канал уже есть в списке или указано неверное имя.")

    def _rebuild_channel_cards(self) -> None:
        while self.channels_layout.count() > 0:
            item = self.channels_layout.takeAt(0)
            w = item.widget()
            if w:
                w.hide()
                w.setParent(None)
                w.deleteLater()
        self.channel_cards.clear()

        if not self.config.channels:
            empty = _make_empty_state(
                "radio",
                "Добавьте каналы для записи",
                "Поддерживаются Twitch, YouTube Live, Kick, VK Play Live и Trovo. "
                "Для каждого канала можно настроить зоны блюра, сцены, расписание и запись чата.",
            )
            self.channels_layout.addWidget(empty)
        else:
            for ch in self.config.channels:
                card = ChannelCard(ch)
                card.quality_changed.connect(self._on_channel_quality_changed)
                card.blur_mode_changed.connect(self._on_channel_blur_mode_changed)
                card.preset_changed.connect(self._on_channel_preset_changed)
                card.auto_toggled.connect(self._on_channel_auto_toggled)
                card.edit_blur_clicked.connect(self._open_channel_blur_editor)
                card.settings_clicked.connect(self._open_channel_settings)
                card.marker_clicked.connect(self._drop_marker_for_channel)
                card.record_clicked.connect(self._start_recording)
                card.stop_clicked.connect(lambda k: self._stop_recording(k, manual=True))
                card.delete_clicked.connect(self._delete_channel)
                card.sync_channel(ch, self.stream_statuses.get(ch.key), ch.key in self.active_recorders)
                self.channel_cards[ch.key] = card
                self.channels_layout.addWidget(card)

        self.channels_layout.addStretch()
        self._update_sidebar_badges()

    def _sync_single_card(self, key: str) -> None:
        ch = self.config.get_channel(key)
        card = self.channel_cards.get(key)
        if ch and card:
            card.sync_channel(ch, self.stream_statuses.get(key), key in self.active_recorders)

    def _on_channel_quality_changed(self, key: str, quality: str) -> None:
        ch = self.config.get_channel(key)
        if ch:
            ch.quality = quality
            self.config.save()

    def _on_channel_blur_mode_changed(self, key: str, mode: str) -> None:
        ch = self.config.get_channel(key)
        if ch:
            ch.blur_mode = mode
            self.config.save()

    def _on_channel_preset_changed(self, key: str, preset_name: str) -> None:
        ch = self.config.get_channel(key)
        if ch and preset_name in ch.blur_presets:
            ch.active_preset = preset_name
            self.config.save()
            self._sync_single_card(key)

    def _on_channel_auto_toggled(self, key: str, checked: bool) -> None:
        ch = self.config.get_channel(key)
        if ch:
            ch.auto_record = checked
            if checked:
                self.manually_stopped.discard(key)
            self.config.save()

    def _open_channel_blur_editor(self, key: str) -> None:
        ch = self.config.get_channel(key)
        if not ch:
            return
        dlg = BlurEditorDialog(
            regions=ch.blur_regions,
            channel=ch,
            quality=ch.quality,
            proxy_url=self.config.proxy_url,
            parent=self,
        )
        if dlg.exec():
            presets, active = dlg.get_presets_result()
            ch.blur_presets = presets
            ch.active_preset = active
            if ch.blur_regions and ch.blur_mode == "none":
                ch.blur_mode = "postprocess"
            self.config.save()
            self._sync_single_card(key)
            self._refresh_preset_combos()
            self._log(f"[{ch.username}] Сохранено сцен: {len(presets)}, активная «{active}» ({len(ch.blur_regions)} зон)")

    def _open_channel_settings(self, key: str) -> None:
        ch = self.config.get_channel(key)
        if not ch:
            return
        dlg = ChannelSettingsDialog(ch, self)
        if dlg.exec():
            updated = dlg.get_channel()
            idx = self.config.channels.index(ch)
            self.config.channels[idx] = updated
            self.config.save()
            self._sync_single_card(key)
            self._log(f"[{ch.username}] Параметры канала обновлены.")

    def _delete_channel(self, key: str) -> None:
        if key in self.active_recorders:
            self._stop_recording(key, manual=True)
        self.config.channels = [c for c in self.config.channels if c.key != key]
        self.config.save()
        self._rebuild_channel_cards()
        self._refresh_preset_combos()

    # ------------------------------------------------------------------ Monitoring, Live Previews & Recording
    def _on_auto_monitor_toggled(self, checked: bool) -> None:
        self.config.auto_monitor_enabled = checked
        self.config.save()
        if checked:
            self.monitor_timer.start(self.config.check_interval_sec * 1000)
            self._check_all_channels()
            self._log("Автомониторинг включён.")
        else:
            self.monitor_timer.stop()
            self._log("Автомониторинг приостановлен.")

    def _check_all_channels(self) -> None:
        if not self.config.channels:
            return
        if self._monitor_worker and self._monitor_worker.isRunning():
            return
        self._monitor_worker = ChannelMonitorWorker(deepcopy(self.config.channels), deepcopy(self.config), self)
        self._monitor_worker.statuses_ready.connect(self._on_statuses_ready)
        self._monitor_worker.log_message.connect(self._log)
        self._monitor_worker.start()

    def _on_statuses_ready(self, statuses: Dict[str, StreamStatus]) -> None:
        if not statuses:
            return
        self.stream_statuses.update(statuses)
        thumb_targets: Dict[str, str] = {}

        for ch in self.config.channels:
            st = statuses.get(ch.key)
            if not st:
                continue
            if not st.is_live:
                self.manually_stopped.discard(ch.key)
            elif st.preview_url:
                thumb_targets[ch.key] = st.preview_url

            is_rec = ch.key in self.active_recorders
            self._sync_single_card(ch.key)

            if (
                self.config.auto_monitor_enabled
                and ch.auto_record
                and st.is_live
                and not is_rec
                and ch.key not in self.manually_stopped
            ):
                if not ch.is_within_schedule():
                    continue
                if not ch.matches_filters(st.game, st.title):
                    continue
                self._log(f"[Автомониторинг] {ch.username} в эфире ({st.game})! Старт записи...")
                self._start_recording(ch.key)

        # Fetch live preview thumbnails in background (#11)
        if thumb_targets and not (self._thumb_worker and self._thumb_worker.isRunning()):
            self._thumb_worker = ThumbnailBatchWorker(thumb_targets, self.config.proxy_url, self)
            self._thumb_worker.thumb_ready.connect(self._on_thumb_ready)
            self._thumb_worker.start()

        self._update_sidebar_badges()

    def _on_thumb_ready(self, key: str, raw_bytes: bytes) -> None:
        card = self.channel_cards.get(key)
        if card:
            card.set_preview_pixmap(raw_bytes)

    def _start_recording(self, key: str) -> None:
        if key in self.active_recorders:
            return
        ch = self.config.get_channel(key)
        if not ch:
            return
        self.manually_stopped.discard(key)

        worker = LiveRecorderWorker(deepcopy(ch), deepcopy(self.config), self)
        worker.progress_updated.connect(self._on_rec_progress)
        worker.status_changed.connect(self._on_rec_status)
        worker.log_message.connect(self._log)
        worker.finished_recording.connect(self._on_rec_finished)
        self.active_recorders[key] = worker
        worker.start()

        self._sync_single_card(key)
        self._update_sidebar_badges()

        if self.config.notify_on_live:
            st = self.stream_statuses.get(key)
            info = f"Канал: {ch.username} ({ch.platform})\nКачество: {ch.quality}"
            if st and st.title:
                info += f"\nСтрим: {st.title[:100]}"
            send_notification_async(self.config, "Началась запись стрима", info, log_cb=self._log)

    def _stop_recording(self, key: str, manual: bool = False) -> None:
        if manual:
            self.manually_stopped.add(key)
        w = self.active_recorders.get(key)
        if w:
            self._log(f"[{w.channel.username}] Остановка записи и сохранение...")
            w.request_stop()
            card = self.channel_cards.get(key)
            if card:
                card.set_recording_status("Сохранение...")

    def _record_all_online(self) -> None:
        cnt = 0
        for ch in self.config.channels:
            st = self.stream_statuses.get(ch.key)
            if st and st.is_live and ch.key not in self.active_recorders:
                self._start_recording(ch.key)
                cnt += 1
        if cnt == 0:
            QMessageBox.information(self, "Запись", "Нет новых онлайн-каналов для запуска.")

    def _stop_all_recordings(self) -> None:
        for k in list(self.active_recorders.keys()):
            self._stop_recording(k, manual=True)

    def _drop_marker_for_channel(self, key: str) -> None:
        w = self.active_recorders.get(key)
        if w:
            t = w.add_marker("Момент")
            self._log(f"[Маркер] {w.channel.username} -> {format_duration(t)}")

    def _on_global_marker_hotkey(self) -> None:
        if not self.active_recorders:
            return
        for key, w in self.active_recorders.items():
            t = w.add_marker(f"Хоткей {self.config.marker_hotkey}")
            self._log(f"[Маркер {self.config.marker_hotkey}] {w.channel.username} -> {format_duration(t)}")

    def _on_rec_status(self, key: str, text: str) -> None:
        card = self.channel_cards.get(key)
        if card:
            card.set_recording_status(text)

    def _on_rec_progress(self, key: str, dur: str, size: str, speed: str) -> None:
        card = self.channel_cards.get(key)
        if card:
            card.set_telemetry(dur, size, speed)

    def _on_rec_finished(self, key: str, files: list, success: bool, needs_post_blur: bool) -> None:
        w = self.active_recorders.pop(key, None)
        if w:
            w.deleteLater()

        self._sync_single_card(key)
        self._update_sidebar_badges()

        ch = self.config.get_channel(key)
        if success and files:
            if self.config.notify_on_finish:
                send_notification_async(
                    self.config,
                    "Запись завершена",
                    f"Канал: {ch.username if ch else key}\nФайлов: {len(files)} ({Path(files[0]).name})",
                    log_cb=self._log,
                )
            if needs_post_blur and ch and ch.blur_regions:
                for fp in files:
                    self._enqueue_blur_job(
                        fp,
                        ch.blur_regions,
                        delete_original=not ch.keep_original,
                        normalize_audio=self.config.normalize_audio_default,
                    )

    # ------------------------------------------------------------------ VOD & Clips Browser Actions
    def _open_vod_blur_editor(self) -> None:
        vod_url = self.edit_vod_url.text().strip()
        dlg = BlurEditorDialog(regions=self._draft_vod_regions, vod_url=vod_url, parent=self)
        if dlg.exec():
            self._draft_vod_regions = dlg.get_regions()
            cnt = len(self._draft_vod_regions)
            self.btn_vod_custom_blur.setText(f"Зоны на VOD ({cnt})" if cnt else "Зоны на кадре")
            if cnt:
                idx = self.combo_vod_blur_preset.findData("__custom__")
                if idx >= 0:
                    self.combo_vod_blur_preset.setCurrentIndex(idx)

    def _start_vod_download(self, url_override: str = "", title_override: str = "") -> None:
        url = (url_override or self.edit_vod_url.text()).strip()
        if not url:
            QMessageBox.warning(self, "Запись VOD", "Введите ссылку на VOD или ID видео.")
            return
        if url.isdigit():
            url = f"https://www.twitch.tv/videos/{url}"

        preset_key = self.combo_vod_blur_preset.currentData()
        regions: List[BlurRegion] = []
        if preset_key == "__custom__":
            regions = deepcopy(self._draft_vod_regions)
        elif preset_key and preset_key != "__none__":
            ch = self.config.get_channel(preset_key)
            if ch:
                regions = deepcopy(ch.blur_regions)

        job_id = uuid.uuid4().hex[:8]
        quality = self.combo_vod_quality.currentText()
        start_t = "" if url_override else self.edit_vod_start.text().strip()
        end_t = "" if url_override else self.edit_vod_end.text().strip()
        if regions:
            self.vod_blur_regions[job_id] = regions

        vod_id = parse_vod_id(url) or url
        title = title_override or f"Twitch VOD #{vod_id}"
        subtitle = f"Качество: {quality}" + (f" · Авто-блюр: {len(regions)} зон" if regions else " · Без блюра")

        self.vod_empty_widget.setVisible(False)
        card = JobCard(job_id, title, subtitle)
        card.cancel_clicked.connect(self._cancel_vod)
        card.reveal_clicked.connect(self._reveal_file)
        self.vod_cards[job_id] = card
        self.vods_layout.insertWidget(0, card)

        worker = VodDownloadWorker(job_id, url, quality, deepcopy(self.config), start_t, end_t, parent=self)
        worker.progress_updated.connect(self._on_vod_progress)
        worker.log_message.connect(self._log)
        worker.finished_download.connect(self._on_vod_finished)
        self.vod_workers[job_id] = worker
        worker.start()
        if not url_override:
            self.edit_vod_url.clear()
        self._update_sidebar_badges()

    def _fetch_vods_catalog(self) -> None:
        ch = self.edit_br_vod_ch.text().strip()
        if not ch:
            return
        self._clear_layout(self.vod_catalog_layout)
        self.vod_catalog_layout.addWidget(QLabel("Загрузка списка VOD..."))
        self.vod_catalog_layout.addStretch()
        self._catalog_worker = CatalogFetchWorker("vods", ch, "", self.config.twitch_oauth_token, self.config.proxy_url, self)
        self._catalog_worker.vods_ready.connect(self._on_vods_catalog_ready)
        self._catalog_worker.start()

    def _on_vods_catalog_ready(self, items: List[VodItem], err: str) -> None:
        self._clear_layout(self.vod_catalog_layout)
        if err or not items:
            self.vod_catalog_layout.addWidget(QLabel(err or "Архивных записей не найдено."))
            self.vod_catalog_layout.addStretch()
            return
        for v in items:
            row_f = QFrame()
            row_f.setProperty("surface", "subtle")
            rl = QHBoxLayout(row_f)
            rl.setContentsMargins(12, 8, 12, 8)
            info = QVBoxLayout()
            t = QLabel(v.title[:95])
            t.setStyleSheet("font-weight: 600;")
            m = QLabel(f"{v.created_at}  ·  {format_duration(v.duration_sec)}  ·  {v.views:,} просм.  ·  {v.game}".replace(",", " "))
            m.setProperty("caption", "true")
            info.addWidget(t)
            info.addWidget(m)
            btn = QPushButton("Скачать")
            btn.setIcon(make_icon("download", "#FFFFFF", 14))
            btn.setProperty("role", "primary")
            btn.clicked.connect(lambda _, u=v.url, tit=v.title[:60]: self._start_vod_download(u, tit))
            rl.addLayout(info, 1)
            rl.addWidget(btn)
            self.vod_catalog_layout.addWidget(row_f)
        self.vod_catalog_layout.addStretch()

    def _fetch_clips_catalog(self) -> None:
        ch = self.edit_br_clip_ch.text().strip()
        if not ch:
            return
        period = self.combo_clip_period.currentData() or "LAST_WEEK"
        self._clear_layout(self.clips_catalog_layout)
        self.clips_catalog_layout.addWidget(QLabel("Загрузка топ-клипов..."))
        self.clips_catalog_layout.addStretch()
        self._catalog_worker = CatalogFetchWorker("clips", ch, period, self.config.twitch_oauth_token, self.config.proxy_url, self)
        self._catalog_worker.clips_ready.connect(self._on_clips_catalog_ready)
        self._catalog_worker.start()

    def _on_clips_catalog_ready(self, items: List[ClipItem], err: str) -> None:
        self._clear_layout(self.clips_catalog_layout)
        self._fetched_clips = items
        self._clip_checks = []
        if err or not items:
            self.clips_catalog_layout.addWidget(QLabel(err or "Клипов за этот период не найдено."))
            self.clips_catalog_layout.addStretch()
            return
        for idx, c in enumerate(items):
            row_f = QFrame()
            row_f.setProperty("surface", "subtle")
            rl = QHBoxLayout(row_f)
            rl.setContentsMargins(10, 6, 10, 6)
            cb = QCheckBox()
            cb.setChecked(idx < 5)
            self._clip_checks.append(cb)
            lbl = QLabel(f"{c.title[:75]}  —  {int(c.duration_sec)}с · {c.views:,} просм. ({c.created_at})".replace(",", " "))
            rl.addWidget(cb)
            rl.addWidget(lbl, 1)
            self.clips_catalog_layout.addWidget(row_f)
        self.clips_catalog_layout.addStretch()

    def _download_selected_clips(self, merge: bool) -> None:
        selected = [c.url for c, cb in zip(self._fetched_clips, self._clip_checks) if cb.isChecked()]
        if not selected:
            QMessageBox.information(self, "Клипы", "Отметьте галочками нужные клипы.")
            return
        ch = self.edit_br_clip_ch.text().strip() or "twitch"
        jid = uuid.uuid4().hex[:8]
        title = f"{'Подборка' if merge else 'Пакет'} клипов: {ch} ({len(selected)} шт.)"
        self.vod_empty_widget.setVisible(False)
        card = JobCard(jid, title, "Склейка в один ролик" if merge else "Отдельные файлы")
        card.cancel_clicked.connect(self._cancel_vod)
        card.reveal_clicked.connect(self._reveal_file)
        self.vod_cards[jid] = card
        self.vods_layout.insertWidget(0, card)

        w = ClipsBatchWorker(jid, ch, selected, merge, deepcopy(self.config), self)
        w.progress_updated.connect(self._on_vod_progress)
        w.log_message.connect(self._log)
        w.finished_batch.connect(self._on_vod_finished)
        self.vod_workers[jid] = w
        w.start()
        self._update_sidebar_badges()

    def _cancel_vod(self, job_id: str) -> None:
        w = self.vod_workers.get(job_id)
        if w and hasattr(w, "request_stop"):
            w.request_stop()

    def _on_vod_progress(self, job_id: str, pct: float, status: str) -> None:
        card = self.vod_cards.get(job_id)
        if card:
            card.update_progress(pct, status)

    def _on_vod_finished(self, job_id: str, output_path: str, success: bool) -> None:
        w = self.vod_workers.pop(job_id, None)
        if w:
            w.deleteLater()
        regions = self.vod_blur_regions.pop(job_id, None)
        card = self.vod_cards.get(job_id)
        if card:
            card.mark_finished(success, output_path)
        self._update_sidebar_badges()
        if success and output_path and regions:
            self._enqueue_blur_job(output_path, regions, delete_original=False)

    # ------------------------------------------------------------------ Smart GPU Blur Queue (#33, #34)
    def _browse_blur_video(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Выберите видео", self.config.output_dir, "Видео (*.mp4 *.mkv *.ts *.flv *.mov)"
        )
        if path:
            self.edit_blur_file.setText(path)

    def _on_manual_preset_changed(self) -> None:
        key = self.combo_manual_preset.currentData()
        if key and key not in ("__custom__", "__none__"):
            ch = self.config.get_channel(key)
            if ch:
                self._manual_blur_regions = deepcopy(ch.blur_regions)
                self.btn_manual_edit_zones.setText(f"Выделить баннер ({len(self._manual_blur_regions)})")

    def _open_manual_blur_editor(self) -> None:
        dlg = BlurEditorDialog(regions=self._manual_blur_regions, video_path=self.edit_blur_file.text().strip(), parent=self)
        if dlg.exec():
            self._manual_blur_regions = dlg.get_regions()
            cnt = len(self._manual_blur_regions)
            self.btn_manual_edit_zones.setText(f"Выделить баннер ({cnt})" if cnt else "Выделить баннер на кадре")
            idx = self.combo_manual_preset.findData("__custom__")
            if idx >= 0:
                self.combo_manual_preset.setCurrentIndex(idx)

    def _start_manual_blur(self) -> None:
        vp = self.edit_blur_file.text().strip()
        if not vp or not os.path.exists(vp):
            QMessageBox.warning(self, "Блюр", "Выберите существующий видеофайл.")
            return
        if not self._manual_blur_regions:
            QMessageBox.warning(self, "Блюр", "Задайте хотя бы одну зону блюра.")
            return
        self._enqueue_blur_job(
            vp,
            deepcopy(self._manual_blur_regions),
            delete_original=self.sw_delete_orig.isChecked(),
            normalize_audio=self.sw_blur_norm.isChecked(),
            force_start_now=True,
        )

    def _enqueue_blur_job(
        self,
        input_path: str,
        regions: List[BlurRegion],
        delete_original: bool = False,
        normalize_audio: bool = False,
        force_start_now: bool = False,
    ) -> None:
        job_id = uuid.uuid4().hex[:8]
        enc = resolve_encoder(self.config.encoder)
        ai_cnt = sum(1 for r in regions if r.auto_detect)
        sub = f"Зон: {len(regions)}" + (f" (AI: {ai_cnt})" if ai_cnt else "") + f" · {enc}"
        if normalize_audio:
            sub += " · Звук EBU R128"

        self.blur_empty_widget.setVisible(False)
        card = JobCard(job_id, Path(input_path).name, sub)
        card.cancel_clicked.connect(self._cancel_blur)
        card.reveal_clicked.connect(self._reveal_file)
        self.blur_cards[job_id] = card
        self.blurs_layout.insertWidget(0, card)

        self.pending_blur_queue.append({
            "job_id": job_id,
            "input_path": input_path,
            "regions": deepcopy(regions),
            "delete_original": delete_original,
            "normalize_audio": normalize_audio,
        })
        if self.config.night_render_enabled and not self.config.is_night_render_window() and not force_start_now:
            card.update_progress(0.0, f"Ночной рендер (ожидание {self.config.night_render_start})")
            self._log(f"[Очередь] {Path(input_path).name} отложен до ночного окна ({self.config.night_render_start}).")
        self._pump_blur_queue(force=force_start_now)
        self._update_sidebar_badges()

    def _pump_blur_queue(self, force: bool = False) -> None:
        if not self.pending_blur_queue:
            return
        if not force and self.config.night_render_enabled and not self.config.is_night_render_window():
            return
        while self.pending_blur_queue and len(self.blur_workers) < self.config.max_concurrent_blurs:
            job = self.pending_blur_queue.popleft()
            jid = job["job_id"]
            w = BlurPostProcessWorker(
                job_id=jid,
                input_path=job["input_path"],
                regions=job["regions"],
                app_config=deepcopy(self.config),
                delete_original=job["delete_original"],
                normalize_audio=job["normalize_audio"],
                parent=self,
            )
            w.progress_updated.connect(self._on_blur_progress)
            w.log_message.connect(self._log)
            w.finished_blur.connect(self._on_blur_finished)
            self.blur_workers[jid] = w
            w.start()
        self._update_sidebar_badges()

    def _cancel_blur(self, job_id: str) -> None:
        # Remove from pending queue if not started yet
        for item in list(self.pending_blur_queue):
            if item["job_id"] == job_id:
                self.pending_blur_queue.remove(item)
                card = self.blur_cards.get(job_id)
                if card:
                    card.update_progress(0.0, "Отменено")
                    card.mark_finished(False, "")
                self._update_sidebar_badges()
                return
        w = self.blur_workers.get(job_id)
        if w:
            w.request_stop()

    def _on_blur_progress(self, job_id: str, pct: float, status: str) -> None:
        card = self.blur_cards.get(job_id)
        if card:
            card.update_progress(pct, status)

    def _on_blur_finished(self, job_id: str, output_path: str, success: bool) -> None:
        w = self.blur_workers.pop(job_id, None)
        if w:
            w.deleteLater()
        card = self.blur_cards.get(job_id)
        if card:
            card.mark_finished(success, output_path)
        if success and output_path and self.config.notify_on_blur:
            send_notification_async(
                self.config, "Блюр видео готов", f"Файл: {Path(output_path).name}", log_cb=self._log
            )
        self._pump_blur_queue(force=False)
        self._update_sidebar_badges()

    # ------------------------------------------------------------------ Resources, Settings & Helpers
    def _on_resource_stats(self, snap: ResourceSnapshot) -> None:
        if snap.gpu_pct is not None:
            self.lbl_res_cpu_gpu.setText(f"CPU: {snap.cpu_pct:.0f}%  ·  GPU: {snap.gpu_pct}% (NVENC {snap.nvenc_pct}%)")
        else:
            self.lbl_res_cpu_gpu.setText(f"CPU: {snap.cpu_pct:.0f}%  ·  RAM: {snap.ram_pct:.0f}%")

        low_disk = snap.disk_free_gb < self.config.min_free_gb
        self.lbl_res_disk.setText(f"Свободно на диске: {snap.disk_free_gb:.1f} ГБ")
        self.lbl_res_disk.setStyleSheet(
            f"font-size: 8.5pt; color: {'#F87171' if low_disk else '#7D8194'}; font-weight: {'700' if low_disk else '400'};"
        )
        if low_disk and not self._disk_warned:
            self._disk_warned = True
            self._log(f"[Диск] ВНИМАНИЕ: осталось мало места ({snap.disk_free_gb:.1f} ГБ < {self.config.min_free_gb} ГБ)!")
            send_notification_async(
                self.config,
                "Мало места на диске!",
                f"Осталось всего {snap.disk_free_gb:.1f} ГБ в папке {self.config.output_dir}",
            )
        elif not low_disk:
            self._disk_warned = False

    def _refresh_preset_combos(self) -> None:
        self.combo_vod_blur_preset.blockSignals(True)
        self.combo_vod_blur_preset.clear()
        self.combo_vod_blur_preset.addItem("Без блюра", "__none__")
        self.combo_vod_blur_preset.addItem("Свои зоны (настроенные для VOD)", "__custom__")
        for ch in self.config.channels:
            if ch.blur_regions:
                self.combo_vod_blur_preset.addItem(f"Канал {ch.username} ({len(ch.blur_regions)} зон)", ch.key)
        self.combo_vod_blur_preset.blockSignals(False)

        self.combo_manual_preset.blockSignals(True)
        self.combo_manual_preset.clear()
        self.combo_manual_preset.addItem("Свои зоны (выделить вручную)", "__custom__")
        for ch in self.config.channels:
            if ch.blur_regions:
                self.combo_manual_preset.addItem(f"Пресет {ch.username} ({len(ch.blur_regions)} зон)", ch.key)
        self.combo_manual_preset.blockSignals(False)
        self.studio_page.refresh_channel_presets()

    def _pick_output_dir(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Папка для сохранения записей", self.config.output_dir)
        if d:
            self.edit_out_dir.setText(d)

    def _send_test_notification(self) -> None:
        self._apply_settings_to_config(save_autostart=False)
        send_notification_async(
            self.config,
            "Twitch Recorder Pro — Тест",
            "Уведомления успешно настроены и работают!",
            log_cb=self._log,
        )
        QMessageBox.information(self, "Уведомления", "Тестовое сообщение отправлено!")

    def _apply_settings_to_config(self, save_autostart: bool = True) -> None:
        self.config.output_dir = self.edit_out_dir.text().strip() or self.config.output_dir
        self.config.container = self.combo_container.currentText()
        self.config.check_interval_sec = self.spin_interval.value()
        self.config.vod_concurrent_fragments = self.spin_vod_frag.value()
        self.config.min_free_gb = self.spin_min_gb.value()
        self.config.auto_cleanup_old = self.sw_auto_clean.isChecked()
        self.config.max_age_days = self.spin_max_days.value()
        self.config.encoder = self.combo_encoder.currentData() or "auto"
        self.config.video_quality = self.spin_quality.value()
        self.config.max_concurrent_blurs = self.spin_max_blurs.value()
        self.config.night_render_enabled = self.sw_night.isChecked()
        self.config.night_render_start = self.edit_night_s.text().strip() or "02:00"
        self.config.night_render_end = self.edit_night_e.text().strip() or "08:00"
        self.config.twitch_oauth_token = self.edit_oauth.text().strip()
        self.config.proxy_url = self.edit_proxy.text().strip()
        self.config.telegram_bot_token = self.edit_tg_tok.text().strip()
        self.config.telegram_chat_id = self.edit_tg_chat.text().strip()
        self.config.discord_webhook_url = self.edit_dc_wh.text().strip()
        self.config.notify_on_live = self.sw_n_live.isChecked()
        self.config.notify_on_finish = self.sw_n_fin.isChecked()
        self.config.notify_on_blur = self.sw_n_blur.isChecked()
        self.config.minimize_to_tray = self.sw_tray.isChecked()
        self.config.start_with_windows = self.sw_autostart.isChecked()
        self.config.marker_hotkey = self.combo_hotkey.currentText()
        if save_autostart:
            set_windows_autostart(self.config.start_with_windows)

    def _save_settings(self) -> None:
        self._apply_settings_to_config(save_autostart=True)
        self.config.ensure_output_dir()
        self.config.save()
        self._hotkey.set_key(self.config.marker_hotkey)
        if self.config.auto_monitor_enabled:
            self.monitor_timer.start(self.config.check_interval_sec * 1000)
        enc_name = resolve_encoder(self.config.encoder)
        self.lbl_gpu_status.setText("GPU NVENC готов" if enc_name == "h264_nvenc" else f"Кодек: {enc_name}")
        self._log("Настройки сохранены.")
        self.lbl_saved_hint.setText("Сохранено")
        QTimer.singleShot(2500, lambda: self.lbl_saved_hint.setText(""))

    def _open_recordings_folder(self) -> None:
        p = self.config.ensure_output_dir()
        if os.name == "nt":
            os.startfile(str(p))

    def _reveal_file(self, path: str) -> None:
        if os.name == "nt" and os.path.exists(path):
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        else:
            self._open_recordings_folder()

    def _clear_layout(self, lay: QVBoxLayout) -> None:
        while lay.count() > 0:
            item = lay.takeAt(0)
            w = item.widget()
            if w:
                w.hide()
                w.setParent(None)
                w.deleteLater()

    def _log(self, msg: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        self.txt_logs.appendPlainText(f"[{ts}] {msg}")

    def _update_sidebar_badges(self) -> None:
        n_rec = len(self.active_recorders)
        n_vod = len(self.vod_workers)
        n_blur = len(self.blur_workers) + len(self.pending_blur_queue)

        self._nav_buttons[0].setText(f"  Прямые эфиры  ({n_rec})" if n_rec else "  Прямые эфиры")
        self._nav_buttons[1].setText(f"  VOD и Клипы  ({n_vod})" if n_vod else "  VOD и Клипы")
        self._nav_buttons[3].setText(f"  Блюр и Очередь  ({n_blur})" if n_blur else "  Блюр и Очередь")
        self.lbl_sb_counters.setText(f"Записей: {n_rec}  ·  VOD: {n_vod}  ·  Блюр: {n_blur}")

        # Update tray icon live recording dot (#30)
        self.tray.setIcon(make_app_icon(64, recording_dot=(n_rec > 0)))
        self.tray.setToolTip(f"Twitch Recorder Pro — активных записей: {n_rec}")

    def closeEvent(self, event) -> None:
        if self.config.minimize_to_tray and not self._force_quit:
            self.hide()
            self.tray.showMessage(
                "Twitch Recorder Pro",
                "Приложение свернуто в трей и продолжает работу в фоне.",
                QSystemTrayIcon.Information,
                2500,
            )
            event.ignore()
            return

        active_total = len(self.active_recorders) + len(self.vod_workers) + len(self.blur_workers)
        if active_total > 0:
            reply = QMessageBox.question(
                self,
                "Выход из программы",
                f"Сейчас выполняются активные задачи ({active_total}).\nОстановить их корректно и выйти?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                self._force_quit = False
                event.ignore()
                return

        self.monitor_timer.stop()
        self.queue_timer.stop()
        self._res_monitor.stop()
        self._res_monitor.wait(1500)

        for w in list(self.active_recorders.values()):
            w.request_stop()
        for w in list(self.vod_workers.values()):
            if hasattr(w, "request_stop"):
                w.request_stop()
        for w in list(self.blur_workers.values()):
            w.request_stop()

        for w in (
            list(self.active_recorders.values())
            + list(self.vod_workers.values())
            + list(self.blur_workers.values())
        ):
            w.wait(4000)

        self.tray.hide()
        self.config.save()
        event.accept()
