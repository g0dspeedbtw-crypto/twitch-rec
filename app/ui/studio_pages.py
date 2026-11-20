from __future__ import annotations

import os
import subprocess
import uuid
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from app.chat_and_ai import (
    HighlightMoment,
    WhisperAIWorker,
    analyze_chat_highlights,
    detect_loud_moments,
)
from app.config import AppConfig, BlurRegion
from app.recorder import (
    ClipsBatchWorker,
    StudioToolWorker,
    format_bytes,
    format_duration,
)
from app.tools import EXPORT_PRESETS, probe_video, resolve_encoder
from app.twitch_api import ClipItem, VodItem, fetch_channel_clips, fetch_channel_vods
from app.ui.blur_editor import BlurEditorDialog
from app.ui.icons import ToggleSwitch, make_icon
from app.ui.widgets import HeatmapWidget, JobCard


def _make_subnav(labels: List[str], on_switch: Callable[[int], None]) -> tuple[QHBoxLayout, List[QPushButton]]:
    row = QHBoxLayout()
    row.setSpacing(8)
    btns: List[QPushButton] = []
    for idx, text in enumerate(labels):
        b = QPushButton(text)
        b.setCursor(Qt.PointingHandCursor)
        b.setProperty("role", "soft-primary" if idx == 0 else "")
        b.clicked.connect(lambda _, i=idx: on_switch(i))
        btns.append(b)
        row.addWidget(b)
    row.addStretch()
    return row, btns


def _set_subnav_active(btns: List[QPushButton], active_idx: int) -> None:
    for i, b in enumerate(btns):
        b.setProperty("role", "soft-primary" if i == active_idx else "")
        b.style().unpolish(b)
        b.style().polish(b)


def _field_box(label: str, w: QWidget) -> QWidget:
    wrap = QWidget()
    l = QVBoxLayout(wrap)
    l.setContentsMargins(0, 0, 0, 0)
    l.setSpacing(5)
    lbl = QLabel(label)
    lbl.setProperty("caption", "true")
    l.addWidget(lbl)
    l.addWidget(w)
    return wrap


def _parse_time_to_sec(text: str) -> float:
    s = text.strip().replace(",", ".")
    if not s:
        return 0.0
    if ":" in s:
        parts = [float(x) for x in s.split(":")]
        if len(parts) == 2:
            return parts[0] * 60.0 + parts[1]
        if len(parts) == 3:
            return parts[0] * 3600.0 + parts[1] * 60.0 + parts[2]
    return float(s)


# ==============================================================================
# 1. RECORDINGS LIBRARY PAGE (#28)
# ==============================================================================
class LibraryPage(QWidget):
    send_to_blur = Signal(str)
    send_to_studio = Signal(str)

    def __init__(self, config_fn: Callable[[], AppConfig], reveal_fn: Callable[[str], None], parent=None):
        super().__init__(parent)
        self._config_fn = config_fn
        self._reveal_fn = reveal_fn
        self._all_files: List[Path] = []
        self._build_ui()

    def _build_ui(self) -> None:
        lay = QVBoxLayout(self)
        lay.setContentsMargins(28, 24, 28, 20)
        lay.setSpacing(14)

        hdr = QHBoxLayout()
        t_col = QVBoxLayout()
        t_col.setSpacing(2)
        h1 = QLabel("Библиотека записей")
        h1.setProperty("h1", "true")
        sub = QLabel("Все ваши записанные стримы, VOD и клипы — быстрый запуск, блюр и монтаж в один клик")
        sub.setProperty("muted", "true")
        t_col.addWidget(h1)
        t_col.addWidget(sub)
        hdr.addLayout(t_col)
        hdr.addStretch()

        btn_ref = QPushButton("Обновить список")
        btn_ref.setIcon(make_icon("refresh", "#E8E9EE", 15))
        btn_ref.setCursor(Qt.PointingHandCursor)
        btn_ref.clicked.connect(self.refresh)
        hdr.addWidget(btn_ref)
        lay.addLayout(hdr)

        # Search & filter bar
        bar = QHBoxLayout()
        bar.setSpacing(10)
        self.edit_search = QLineEdit()
        self.edit_search.setPlaceholderText("Поиск по каналу или названию файла...")
        self.edit_search.textChanged.connect(self._render_filtered)

        self.combo_filter = QComboBox()
        self.combo_filter.addItems(["Все файлы", "Только с блюром", "Только исходники", "Есть чат / маркеры"])
        self.combo_filter.setFixedWidth(185)
        self.combo_filter.currentIndexChanged.connect(self._render_filtered)

        self.lbl_count = QLabel("Файлов: 0")
        self.lbl_count.setProperty("muted", "true")

        bar.addWidget(self.edit_search, 1)
        bar.addWidget(self.combo_filter)
        bar.addWidget(self.lbl_count)
        lay.addLayout(bar)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.container = QWidget()
        self.container.setObjectName("ScrollContent")
        self.list_layout = QVBoxLayout(self.container)
        self.list_layout.setContentsMargins(0, 0, 6, 0)
        self.list_layout.setSpacing(10)
        self.list_layout.addStretch()
        scroll.setWidget(self.container)
        lay.addWidget(scroll, 1)

    def refresh(self) -> None:
        root = self._config_fn().ensure_output_dir()
        exts = {".mp4", ".mkv", ".ts", ".flv", ".mov"}
        found: List[Path] = []
        if root.exists():
            for p in root.rglob("*"):
                if p.is_file() and p.suffix.lower() in exts and not p.name.startswith("."):
                    found.append(p)
        found.sort(key=lambda x: x.stat().st_mtime, reverse=True)
        self._all_files = found
        self._render_filtered()

    def _render_filtered(self) -> None:
        while self.list_layout.count() > 0:
            item = self.list_layout.takeAt(0)
            w = item.widget()
            if w:
                w.hide()
                w.setParent(None)
                w.deleteLater()

        q = self.edit_search.text().strip().lower()
        flt = self.combo_filter.currentIndex()

        shown = []
        for p in self._all_files:
            if q and q not in str(p).lower():
                continue
            is_blur = "_blurred" in p.stem.lower()
            has_chat = p.with_suffix(".chat.json").exists()
            has_mark = p.with_suffix(".markers.json").exists()
            if flt == 1 and not is_blur:
                continue
            if flt == 2 and is_blur:
                continue
            if flt == 3 and not (has_chat or has_mark):
                continue
            shown.append((p, is_blur, has_chat, has_mark))

        self.lbl_count.setText(f"Файлов: {len(shown)}")

        if not shown:
            empty = QFrame()
            empty.setProperty("surface", "subtle")
            el = QVBoxLayout(empty)
            el.setContentsMargins(24, 42, 24, 42)
            lbl = QLabel("Записанных видео пока нет")
            lbl.setProperty("h2", "true")
            lbl.setAlignment(Qt.AlignCenter)
            sub = QLabel("Запишите прямой эфир или скачайте VOD — файлы появятся здесь автоматически.")
            sub.setProperty("muted", "true")
            sub.setAlignment(Qt.AlignCenter)
            el.addWidget(lbl)
            el.addWidget(sub)
            self.list_layout.addWidget(empty)
        else:
            for p, is_blur, has_chat, has_mark in shown[:120]:
                self.list_layout.addWidget(self._build_file_card(p, is_blur, has_chat, has_mark))

        self.list_layout.addStretch()

    def _build_file_card(self, p: Path, is_blur: bool, has_chat: bool, has_mark: bool) -> QFrame:
        card = QFrame()
        card.setProperty("card", "true")
        card.setProperty("hoverable", "true")
        row = QHBoxLayout(card)
        row.setContentsMargins(16, 12, 16, 12)
        row.setSpacing(12)

        ico = QLabel()
        ico.setPixmap(make_icon("video", "#8B5CF6" if is_blur else "#9CA0B0", 22).pixmap(22, 22))
        row.addWidget(ico)

        info = QVBoxLayout()
        info.setSpacing(3)
        name_lbl = QLabel(p.name)
        name_lbl.setStyleSheet("font-size: 10.5pt; font-weight: 600; color: #F4F5F8;")

        st = p.stat()
        dt_str = datetime.fromtimestamp(st.st_mtime).strftime("%d.%m.%Y %H:%M")
        badges = [p.parent.name, format_bytes(st.st_size), dt_str]
        if is_blur:
            badges.append("Заблюрено")
        if has_chat:
            badges.append("Есть чат")
        if has_mark:
            badges.append("Есть маркеры")
        meta_lbl = QLabel("  ·  ".join(badges))
        meta_lbl.setProperty("caption", "true")
        info.addWidget(name_lbl)
        info.addWidget(meta_lbl)
        row.addLayout(info, 1)

        btn_play = QPushButton("Смотреть")
        btn_play.setIcon(make_icon("play", "#FFFFFF", 13))
        btn_play.setCursor(Qt.PointingHandCursor)
        btn_play.clicked.connect(lambda _, fp=str(p): os.startfile(fp) if os.name == "nt" else None)

        btn_blur = QPushButton("В блюр")
        btn_blur.setIcon(make_icon("blur", "#C4B5FD", 14))
        btn_blur.setProperty("role", "soft-primary")
        btn_blur.setCursor(Qt.PointingHandCursor)
        btn_blur.clicked.connect(lambda _, fp=str(p): self.send_to_blur.emit(fp))

        btn_edit = QPushButton("В монтаж")
        btn_edit.setIcon(make_icon("scissors", "#E8E9EE", 14))
        btn_edit.setCursor(Qt.PointingHandCursor)
        btn_edit.clicked.connect(lambda _, fp=str(p): self.send_to_studio.emit(fp))

        btn_dir = QPushButton()
        btn_dir.setIcon(make_icon("folder", "#9CA0B0", 15))
        btn_dir.setProperty("role", "ghost")
        btn_dir.setToolTip("Показать в проводнике")
        btn_dir.setFixedSize(34, 34)
        btn_dir.clicked.connect(lambda _, fp=str(p): self._reveal_fn(fp))

        btn_del = QPushButton()
        btn_del.setIcon(make_icon("trash", "#8E92A4", 15))
        btn_del.setProperty("role", "ghost-danger")
        btn_del.setToolTip("Удалить файл")
        btn_del.setFixedSize(34, 34)
        btn_del.clicked.connect(lambda _, fp=p: self._delete_file(fp))

        row.addWidget(btn_play)
        row.addWidget(btn_blur)
        row.addWidget(btn_edit)
        row.addWidget(btn_dir)
        row.addWidget(btn_del)
        return card

    def _delete_file(self, p: Path) -> None:
        ans = QMessageBox.question(self, "Удаление", f"Удалить файл «{p.name}» с диска?", QMessageBox.Yes | QMessageBox.No)
        if ans == QMessageBox.Yes:
            try:
                p.unlink()
            except Exception:
                pass
            self.refresh()


# ==============================================================================
# 2. VOD & CLIPS BROWSER WORKER + HELPER (#26, #27)
# ==============================================================================
class CatalogFetchWorker(QThread):
    vods_ready = Signal(list, str)
    clips_ready = Signal(list, str)

    def __init__(self, kind: str, username: str, period: str, oauth: str, proxy: str, parent=None):
        super().__init__(parent)
        self.kind = kind
        self.username = username
        self.period = period
        self.oauth = oauth
        self.proxy = proxy

    def run(self) -> None:
        if self.kind == "vods":
            items, err = fetch_channel_vods(self.username, 20, self.oauth, self.proxy)
            self.vods_ready.emit(items, err)
        else:
            items, err = fetch_channel_clips(self.username, self.period, 24, self.oauth, self.proxy)
            self.clips_ready.emit(items, err)


# ==============================================================================
# 3. STUDIO & AI PAGE (#6, #8, #16, #17, #18, #20, #21, #22, #23, #24, #25)
# ==============================================================================
class HighlightAnalyzerWorker(QThread):
    ready = Signal(list, list, str)  # heatmap, highlights, mode

    def __init__(self, mode: str, path: str, parent=None):
        super().__init__(parent)
        self.mode = mode
        self.path = path

    def run(self) -> None:
        if self.mode == "chat":
            hm, hl = analyze_chat_highlights(self.path)
            self.ready.emit(hm, hl, "chat")
        else:
            hm, hl = detect_loud_moments(self.path)
            self.ready.emit(hm, hl, "audio")


class StudioAIPage(QWidget):
    """Complete editing & AI toolkit: Trimmer, Concat, Silence/AFK cut, Shorts 9:16, Highlights, Whisper AI, Export."""

    log_message = Signal(str)

    def __init__(
        self,
        config_fn: Callable[[], AppConfig],
        reveal_fn: Callable[[str], None],
        parent=None,
    ):
        super().__init__(parent)
        self._config_fn = config_fn
        self._reveal_fn = reveal_fn
        self._workers: Dict[str, QThread] = {}
        self._cards: Dict[str, JobCard] = {}
        self._shorts_cam: Optional[BlurRegion] = None
        self._shorts_game: Optional[BlurRegion] = None
        self._hl_worker: Optional[HighlightAnalyzerWorker] = None
        self._current_highlights: List[HighlightMoment] = []
        self._build_ui()

    def load_video_path(self, path: str) -> None:
        """Populate selected video path across all studio sub-tabs."""
        self.edit_trim_file.setText(path)
        self.edit_shorts_file.setText(path)
        self.edit_hl_video.setText(path)
        self.edit_ai_file.setText(path)
        self.edit_exp_file.setText(path)
        # Auto-detect companion chat file if exists
        chat_json = Path(path).with_suffix(".chat.json")
        if chat_json.exists():
            self.edit_hl_chat.setText(str(chat_json))
        meta = probe_video(path)
        dur = meta.get("duration", 0.0)
        if dur > 0:
            self.edit_trim_end.setText(format_duration(dur))

    def _build_ui(self) -> None:
        lay = QVBoxLayout(self)
        lay.setContentsMargins(28, 24, 28, 20)
        lay.setSpacing(14)

        h1 = QLabel("Монтаж и AI-студия")
        h1.setProperty("h1", "true")
        sub = QLabel("Быстрая обрезка, склейка, Shorts 9:16, поиск хайлайтов по чату/звуку, Whisper-субтитры, цензура и экспорт")
        sub.setProperty("muted", "true")
        lay.addWidget(h1)
        lay.addWidget(sub)

        nav_row, self._sub_btns = _make_subnav(
            [
                "Обрезка / Склейка / AFK",
                "Shorts / TikTok 9:16",
                "Хайлайты и Чат",
                "AI Субтитры и Цензура",
                "Экспорт и Звук",
            ],
            self._switch_sub,
        )
        lay.addLayout(nav_row)

        self.stack = QStackedWidget()
        self.stack.addWidget(self._build_trim_concat_tab())
        self.stack.addWidget(self._build_shorts_tab())
        self.stack.addWidget(self._build_highlights_tab())
        self.stack.addWidget(self._build_ai_tab())
        self.stack.addWidget(self._build_export_tab())
        lay.addWidget(self.stack)

        # Shared studio tasks queue at bottom
        lbl_q = QLabel("Задачи студии")
        lbl_q.setProperty("h2", "true")
        lay.addWidget(lbl_q)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setMinimumHeight(140)
        self.jobs_container = QWidget()
        self.jobs_container.setObjectName("ScrollContent")
        self.jobs_layout = QVBoxLayout(self.jobs_container)
        self.jobs_layout.setContentsMargins(0, 0, 6, 0)
        self.jobs_layout.setSpacing(8)
        self.jobs_layout.addStretch()
        scroll.setWidget(self.jobs_container)
        lay.addWidget(scroll, 1)

    def _switch_sub(self, idx: int) -> None:
        self.stack.setCurrentIndex(idx)
        _set_subnav_active(self._sub_btns, idx)

    def _pick_video_into(self, line_edit: QLineEdit) -> None:
        p, _ = QFileDialog.getOpenFileName(
            self, "Выберите видеофайл", self._config_fn().output_dir, "Видео (*.mp4 *.mkv *.ts *.flv *.mov)"
        )
        if p:
            line_edit.setText(p)
            if line_edit is self.edit_trim_file:
                meta = probe_video(p)
                if meta.get("duration", 0) > 0:
                    self.edit_trim_end.setText(format_duration(meta["duration"]))
            if line_edit is self.edit_hl_video:
                cj = Path(p).with_suffix(".chat.json")
                if cj.exists():
                    self.edit_hl_chat.setText(str(cj))

    # ---- Sub-tab 1: Trim + Concat + Remove Silence/AFK (#20, #8, #24) ----
    def _build_trim_concat_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)

        # Card A: Trimmer + Auto-cut silence/AFK
        c1 = QFrame()
        c1.setProperty("card", "true")
        l1 = QVBoxLayout(c1)
        l1.setContentsMargins(16, 14, 16, 14)
        l1.setSpacing(10)

        r1 = QHBoxLayout()
        self.edit_trim_file = QLineEdit()
        self.edit_trim_file.setPlaceholderText("Выберите видео для обрезки или удаления тишины/AFK...")
        btn_b = QPushButton("Выбрать")
        btn_b.clicked.connect(lambda: self._pick_video_into(self.edit_trim_file))
        r1.addWidget(self.edit_trim_file, 1)
        r1.addWidget(btn_b)
        l1.addLayout(r1)

        r2 = QHBoxLayout()
        r2.setSpacing(12)
        self.edit_trim_start = QLineEdit("00:00:00")
        self.edit_trim_start.setFixedWidth(110)
        self.edit_trim_end = QLineEdit("00:05:00")
        self.edit_trim_end.setFixedWidth(110)
        self.sw_trim_acc = ToggleSwitch(False)
        lbl_acc = QLabel("Точный кадр (перекодировка)")
        lbl_acc.setProperty("caption", "true")

        btn_trim = QPushButton("Обрезать видео")
        btn_trim.setIcon(make_icon("scissors", "#FFFFFF", 15))
        btn_trim.setProperty("role", "primary")
        btn_trim.clicked.connect(self._run_trim)

        btn_afk = QPushButton("Вырезать тишину и AFK")
        btn_afk.setIcon(make_icon("sparkles", "#C4B5FD", 15))
        btn_afk.setProperty("role", "soft-primary")
        btn_afk.clicked.connect(self._run_remove_silence)

        r2.addWidget(_field_box("От (ЧЧ:ММ:СС)", self.edit_trim_start))
        r2.addWidget(_field_box("До (ЧЧ:ММ:СС)", self.edit_trim_end))
        r2.addWidget(self.sw_trim_acc)
        r2.addWidget(lbl_acc)
        r2.addStretch()
        r2.addWidget(btn_afk)
        r2.addWidget(btn_trim)
        l1.addLayout(r2)
        lay.addWidget(c1)

        # Card B: Multi-file Concat (#8)
        c2 = QFrame()
        c2.setProperty("card", "true")
        l2 = QHBoxLayout(c2)
        l2.setContentsMargins(16, 12, 16, 12)
        l2.setSpacing(12)
        t_lbl = QLabel("Склейка нескольких видеофайлов в один (части стрима или клипы)")
        t_lbl.setProperty("muted", "true")
        self.sw_cat_norm = ToggleSwitch(False)
        lbl_cn = QLabel("Выровнять разрешение (1080p60)")
        lbl_cn.setProperty("caption", "true")
        btn_cat = QPushButton("Выбрать файлы и склеить")
        btn_cat.setIcon(make_icon("plus", "#E8E9EE", 15))
        btn_cat.clicked.connect(self._run_concat_files)
        l2.addWidget(t_lbl, 1)
        l2.addWidget(self.sw_cat_norm)
        l2.addWidget(lbl_cn)
        l2.addWidget(btn_cat)
        lay.addWidget(c2)
        return w

    # ---- Sub-tab 2: Vertical Shorts / TikTok 9:16 (#21) ----
    def _build_shorts_tab(self) -> QWidget:
        card = QFrame()
        card.setProperty("card", "true")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(12)

        r1 = QHBoxLayout()
        self.edit_shorts_file = QLineEdit()
        self.edit_shorts_file.setPlaceholderText("Выберите видео для конвертации в вертикальный формат 9:16 (1080×1920)...")
        btn_b = QPushButton("Выбрать")
        btn_b.clicked.connect(lambda: self._pick_video_into(self.edit_shorts_file))
        r1.addWidget(self.edit_shorts_file, 1)
        r1.addWidget(btn_b)
        lay.addLayout(r1)

        r2 = QHBoxLayout()
        r2.setSpacing(12)
        self.combo_shorts_mode = QComboBox()
        self.combo_shorts_mode.addItem("Вебка сверху + Геймплей снизу (выделить на кадре)", "split")
        self.combo_shorts_mode.addItem("Видео по центру + размытый фон (универсально)", "blur_bg")
        self.combo_shorts_mode.setMinimumWidth(340)

        self.btn_shorts_zones = QPushButton("Выделить вебку и игру")
        self.btn_shorts_zones.setIcon(make_icon("crop", "#C4B5FD", 15))
        self.btn_shorts_zones.setProperty("role", "soft-primary")
        self.btn_shorts_zones.clicked.connect(self._pick_shorts_zones)

        self.combo_shorts_blur = QComboBox()
        self.combo_shorts_blur.addItem("Без блюра баннера", "")
        self.combo_shorts_blur.setMinimumWidth(180)

        btn_render = QPushButton("Создать Shorts 9:16")
        btn_render.setIcon(make_icon("smartphone", "#FFFFFF", 15))
        btn_render.setProperty("role", "primary")
        btn_render.clicked.connect(self._run_shorts)

        r2.addWidget(_field_box("Компоновка 9:16", self.combo_shorts_mode))
        r2.addWidget(_field_box("Блюр баннера (пресет)", self.combo_shorts_blur))
        r2.addStretch()
        r2.addWidget(self.btn_shorts_zones)
        r2.addWidget(btn_render)
        lay.addLayout(r2)

        self.lbl_shorts_info = QLabel("Совет: в редакторе выделите 1-й прямоугольник — Вебка (сверху), 2-й — Геймплей (снизу).")
        self.lbl_shorts_info.setProperty("caption", "true")
        lay.addWidget(self.lbl_shorts_info)
        return card

    # ---- Sub-tab 3: Highlights (Chat Heatmap + Audio Loudness + Burn Chat) (#16, #17, #18) ----
    def _build_highlights_tab(self) -> QWidget:
        card = QFrame()
        card.setProperty("card", "true")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(10)

        r1 = QHBoxLayout()
        self.edit_hl_video = QLineEdit()
        self.edit_hl_video.setPlaceholderText("Видеофайл (.mp4 / .mkv)...")
        btn_v = QPushButton("Видео")
        btn_v.clicked.connect(lambda: self._pick_video_into(self.edit_hl_video))

        self.edit_hl_chat = QLineEdit()
        self.edit_hl_chat.setPlaceholderText("Файл чата (.chat.json — подхватывается сам)...")
        btn_c = QPushButton("Чат JSON")
        btn_c.clicked.connect(self._pick_chat_json)

        r1.addWidget(self.edit_hl_video, 1)
        r1.addWidget(btn_v)
        r1.addWidget(self.edit_hl_chat, 1)
        r1.addWidget(btn_c)
        lay.addLayout(r1)

        r2 = QHBoxLayout()
        btn_scan_chat = QPushButton("Анализ чата (тепловая карта)")
        btn_scan_chat.setIcon(make_icon("flame", "#FFFFFF", 15))
        btn_scan_chat.setProperty("role", "primary")
        btn_scan_chat.clicked.connect(lambda: self._run_highlight_scan("chat"))

        btn_scan_audio = QPushButton("Найти громкие моменты (звук)")
        btn_scan_audio.setIcon(make_icon("volume", "#C4B5FD", 15))
        btn_scan_audio.setProperty("role", "soft-primary")
        btn_scan_audio.clicked.connect(lambda: self._run_highlight_scan("audio"))

        btn_burn_chat = QPushButton("Вшить чат в видео")
        btn_burn_chat.setIcon(make_icon("video", "#E8E9EE", 15))
        btn_burn_chat.clicked.connect(self._run_burn_chat)

        r2.addWidget(btn_scan_chat)
        r2.addWidget(btn_scan_audio)
        r2.addWidget(btn_burn_chat)
        r2.addStretch()
        lay.addLayout(r2)

        body = QHBoxLayout()
        body.setSpacing(12)
        self.heatmap = HeatmapWidget()
        self.heatmap.timestamp_clicked.connect(self._on_heatmap_clicked)
        body.addWidget(self.heatmap, 2)

        hl_col = QVBoxLayout()
        self.list_highlights = QListWidget()
        self.list_highlights.setMaximumHeight(130)
        btn_cut_hl = QPushButton("Вырезать выбранный хайлайт (±20 сек)")
        btn_cut_hl.setIcon(make_icon("scissors", "#FFFFFF", 14))
        btn_cut_hl.setProperty("role", "success")
        btn_cut_hl.clicked.connect(self._cut_selected_highlight)
        hl_col.addWidget(self.list_highlights)
        hl_col.addWidget(btn_cut_hl)
        body.addLayout(hl_col, 1)
        lay.addLayout(body)
        return card

    # ---- Sub-tab 4: AI Subtitles & Censorship (#6, #22) ----
    def _build_ai_tab(self) -> QWidget:
        card = QFrame()
        card.setProperty("card", "true")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(12)

        r1 = QHBoxLayout()
        self.edit_ai_file = QLineEdit()
        self.edit_ai_file.setPlaceholderText("Выберите видео для генерации субтитров или аудио-цензуры (Whisper AI)...")
        btn_b = QPushButton("Выбрать")
        btn_b.clicked.connect(lambda: self._pick_video_into(self.edit_ai_file))
        r1.addWidget(self.edit_ai_file, 1)
        r1.addWidget(btn_b)
        lay.addLayout(r1)

        r2 = QHBoxLayout()
        r2.setSpacing(14)
        self.combo_ai_model = QComboBox()
        self.combo_ai_model.addItems(["tiny", "base", "small", "medium"])
        self.combo_ai_model.setCurrentText(self._config_fn().whisper_model)
        self.combo_ai_model.setFixedWidth(120)

        self.combo_ai_lang = QComboBox()
        self.combo_ai_lang.addItem("Русский (ru)", "ru")
        self.combo_ai_lang.addItem("English (en)", "en")
        self.combo_ai_lang.addItem("Автоопределение", "auto")
        self.combo_ai_lang.setFixedWidth(170)

        self.sw_burn_subs = ToggleSwitch(True)
        self.sw_shorts_subs = ToggleSwitch(True)
        self.sw_beep_mode = ToggleSwitch(True)

        r2.addWidget(_field_box("Модель Whisper", self.combo_ai_model))
        r2.addWidget(_field_box("Язык речи", self.combo_ai_lang))
        r2.addWidget(_field_box("Вшить в видео", self.sw_burn_subs))
        r2.addWidget(_field_box("Стиль Shorts", self.sw_shorts_subs))
        r2.addWidget(_field_box("Писк вместо тишины", self.sw_beep_mode))
        r2.addStretch()
        lay.addLayout(r2)

        r3 = QHBoxLayout()
        self.edit_banned = QLineEdit(self._config_fn().banned_words)
        self.edit_banned.setPlaceholderText("Запрещённые слова через запятую...")
        btn_subs = QPushButton("Сгенерировать субтитры")
        btn_subs.setIcon(make_icon("sparkles", "#FFFFFF", 15))
        btn_subs.setProperty("role", "primary")
        btn_subs.clicked.connect(lambda: self._run_whisper("subtitles"))

        btn_censor = QPushButton("Запикать запрещённые слова")
        btn_censor.setIcon(make_icon("volume", "#F87171", 15))
        btn_censor.setProperty("role", "danger")
        btn_censor.clicked.connect(lambda: self._run_whisper("censor"))

        r3.addWidget(_field_box("Список слов для авто-цензуры (через запятую)", self.edit_banned), 1)
        r3.addWidget(btn_subs)
        r3.addWidget(btn_censor)
        lay.addLayout(r3)
        return card

    # ---- Sub-tab 5: Export Presets & Loudness Normalization (#23, #25) ----
    def _build_export_tab(self) -> QWidget:
        card = QFrame()
        card.setProperty("card", "true")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(12)

        r1 = QHBoxLayout()
        self.edit_exp_file = QLineEdit()
        self.edit_exp_file.setPlaceholderText("Выберите видео для сжатия / нормализации звука...")
        btn_b = QPushButton("Выбрать")
        btn_b.clicked.connect(lambda: self._pick_video_into(self.edit_exp_file))
        r1.addWidget(self.edit_exp_file, 1)
        r1.addWidget(btn_b)
        lay.addLayout(r1)

        r2 = QHBoxLayout()
        r2.setSpacing(14)
        self.combo_exp_preset = QComboBox()
        self.combo_exp_preset.setMinimumWidth(290)
        for pid, pinfo in EXPORT_PRESETS.items():
            self.combo_exp_preset.addItem(pinfo["label"], pid)

        self.sw_exp_norm = ToggleSwitch(True)
        lbl_n = QLabel("Нормализация громкости (EBU R128)")
        lbl_n.setProperty("muted", "true")

        btn_exp = QPushButton("Экспортировать")
        btn_exp.setIcon(make_icon("download", "#FFFFFF", 15))
        btn_exp.setProperty("role", "primary")
        btn_exp.clicked.connect(self._run_export)

        r2.addWidget(_field_box("Целевой пресет экспорта", self.combo_exp_preset))
        r2.addWidget(self.sw_exp_norm)
        r2.addWidget(lbl_n)
        r2.addStretch()
        r2.addWidget(btn_exp)
        lay.addLayout(r2)
        return card

    # ------------------------------------------------------------------ Actions
    def refresh_channel_presets(self) -> None:
        self.combo_shorts_blur.blockSignals(True)
        self.combo_shorts_blur.clear()
        self.combo_shorts_blur.addItem("Без блюра баннера", "")
        for ch in self._config_fn().channels:
            if ch.blur_regions:
                self.combo_shorts_blur.addItem(f"Канал {ch.username} ({len(ch.blur_regions)} зон)", ch.key)
        self.combo_shorts_blur.blockSignals(False)

    def _spawn_studio_job(self, title: str, subtitle: str, action: str, params: dict) -> None:
        jid = uuid.uuid4().hex[:8]
        card = JobCard(jid, title, subtitle)
        card.reveal_clicked.connect(self._reveal_fn)
        self._cards[jid] = card
        self.jobs_layout.insertWidget(0, card)

        w = StudioToolWorker(jid, action, params, deepcopy(self._config_fn()), self)
        w.progress_updated.connect(self._on_job_progress)
        w.log_message.connect(self.log_message.emit)
        w.finished_task.connect(self._on_job_done)
        self._workers[jid] = w
        w.start()

    def _on_job_progress(self, jid: str, pct: float, status: str) -> None:
        c = self._cards.get(jid)
        if c:
            c.update_progress(pct, status)

    def _on_job_done(self, jid: str, out_path: str, ok: bool) -> None:
        w = self._workers.pop(jid, None)
        if w:
            w.deleteLater()
        c = self._cards.get(jid)
        if c:
            c.mark_finished(ok, out_path)

    def _run_trim(self) -> None:
        inp = self.edit_trim_file.text().strip()
        if not inp or not os.path.exists(inp):
            QMessageBox.warning(self, "Обрезка", "Выберите существующий видеофайл.")
            return
        try:
            s = _parse_time_to_sec(self.edit_trim_start.text())
            e = _parse_time_to_sec(self.edit_trim_end.text())
        except Exception:
            QMessageBox.warning(self, "Обрезка", "Неверный формат времени. Используйте ЧЧ:ММ:СС или секунды.")
            return
        if e <= s:
            QMessageBox.warning(self, "Обрезка", "Конечное время должно быть больше начального.")
            return
        p = Path(inp)
        out = str(p.with_name(f"{p.stem}_cut_{int(s)}-{int(e)}{p.suffix}"))
        self._spawn_studio_job(
            f"Обрезка: {p.name}",
            f"{format_duration(s)} – {format_duration(e)}",
            "trim",
            {"input_path": inp, "start_sec": s, "end_sec": e, "output_path": out, "accurate": self.sw_trim_acc.isChecked()},
        )

    def _run_remove_silence(self) -> None:
        inp = self.edit_trim_file.text().strip()
        if not inp or not os.path.exists(inp):
            QMessageBox.warning(self, "Авто-удаление пауз", "Выберите видеофайл.")
            return
        p = Path(inp)
        out = str(p.with_name(f"{p.stem}_no_afk{p.suffix}"))
        self._spawn_studio_job(
            f"Удаление тишины и AFK: {p.name}",
            "silencedetect + freezedetect",
            "remove_silence_afk",
            {"input_path": inp, "output_path": out},
        )

    def _run_concat_files(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(
            self, "Выберите 2 или более видеофайлов для склейки", self._config_fn().output_dir, "Видео (*.mp4 *.mkv *.ts *.flv *.mov)"
        )
        if len(files) < 2:
            return
        first = Path(files[0])
        out = str(first.with_name(f"{first.stem}_merged_{datetime.now().strftime('%H%M%S')}.mp4"))
        self._spawn_studio_job(
            f"Склейка ({len(files)} файлов)",
            Path(out).name,
            "concat",
            {"input_paths": files, "output_path": out, "normalize": self.sw_cat_norm.isChecked()},
        )

    def _pick_shorts_zones(self) -> None:
        inp = self.edit_shorts_file.text().strip()
        init = []
        if self._shorts_cam:
            init.append(self._shorts_cam)
        else:
            init.append(BlurRegion(0.02, 0.55, 0.28, 0.35, label="1. Вебка (верх)"))
        if self._shorts_game:
            init.append(self._shorts_game)
        else:
            init.append(BlurRegion(0.25, 0.05, 0.50, 0.90, label="2. Геймплей (низ)"))

        dlg = BlurEditorDialog(
            regions=init,
            video_path=inp,
            title_override="Выбор областей для Shorts 9:16 (1 = Вебка, 2 = Геймплей)",
            parent=self,
        )
        dlg.sw_preview.setChecked(False)
        dlg.canvas.set_preview_enabled(False)
        if dlg.exec():
            regs = dlg.get_regions()
            if len(regs) >= 1:
                self._shorts_cam = regs[0]
            if len(regs) >= 2:
                self._shorts_game = regs[1]
            self.lbl_shorts_info.setText(f"Выбрано областей для Shorts: {len(regs)} (Вебка + Геймплей готовы)")

    def _run_shorts(self) -> None:
        inp = self.edit_shorts_file.text().strip()
        if not inp or not os.path.exists(inp):
            QMessageBox.warning(self, "Shorts 9:16", "Выберите видеофайл.")
            return
        mode = self.combo_shorts_mode.currentData()
        cam = self._shorts_cam if mode == "split" else None
        game = self._shorts_game if mode == "split" else None
        if mode == "split" and cam is None:
            cam = BlurRegion(0.02, 0.55, 0.28, 0.35)
            game = BlurRegion(0.22, 0.05, 0.56, 0.90)

        blur_regs: List[BlurRegion] = []
        ch_key = self.combo_shorts_blur.currentData()
        if ch_key:
            ch = self._config_fn().get_channel(ch_key)
            if ch:
                blur_regs = deepcopy(ch.blur_regions)

        p = Path(inp)
        out = str(p.with_name(f"{p.stem}_shorts_9x16.mp4"))
        self._spawn_studio_job(
            f"Shorts 9:16: {p.name}",
            f"1080×1920 · {'Вебка + Игра' if mode == 'split' else 'Размытый фон'}",
            "shorts",
            {"input_path": inp, "output_path": out, "cam_region": cam, "game_region": game, "blur_regions": blur_regs},
        )

    def _pick_chat_json(self) -> None:
        p, _ = QFileDialog.getOpenFileName(
            self, "Выберите файл чата (.chat.json)", self._config_fn().output_dir, "Чат JSON (*.json)"
        )
        if p:
            self.edit_hl_chat.setText(p)

    def _run_highlight_scan(self, mode: str) -> None:
        target = self.edit_hl_chat.text().strip() if mode == "chat" else self.edit_hl_video.text().strip()
        if not target or not os.path.exists(target):
            QMessageBox.warning(
                self,
                "Хайлайты",
                "Выберите файл чата (.chat.json)" if mode == "chat" else "Выберите видеофайл для анализа звука.",
            )
            return
        self.list_highlights.clear()
        self.list_highlights.addItem("Анализ... подождите пару секунд")
        self._hl_worker = HighlightAnalyzerWorker(mode, target, self)
        self._hl_worker.ready.connect(self._on_highlights_ready)
        self._hl_worker.start()

    def _on_highlights_ready(self, heatmap: list, highlights: List[HighlightMoment], mode: str) -> None:
        self._current_highlights = highlights
        self.heatmap.set_data(heatmap, "#8B5CF6" if mode == "chat" else "#22C55E")
        self.list_highlights.clear()
        if not highlights:
            self.list_highlights.addItem("Явных пиков активности не найдено")
            return
        for h in highlights:
            self.list_highlights.addItem(f"[{format_duration(h.timestamp_sec)}]  {h.reason}")

    def _on_heatmap_clicked(self, sec: float) -> None:
        self.edit_trim_start.setText(format_duration(max(0.0, sec - 15.0)))
        self.edit_trim_end.setText(format_duration(sec + 25.0))
        self.log_message.emit(f"[Хайлайты] Выбран таймкод {format_duration(sec)} (подставлен в обрезчик).")

    def _cut_selected_highlight(self) -> None:
        inp = self.edit_hl_video.text().strip()
        row = self.list_highlights.currentRow()
        if not inp or not os.path.exists(inp) or not (0 <= row < len(self._current_highlights)):
            QMessageBox.warning(self, "Вырезка хайлайта", "Выберите видео и нужный момент из списка хайлайтов.")
            return
        t = self._current_highlights[row].timestamp_sec
        s = max(0.0, t - 20.0)
        e = t + 20.0
        p = Path(inp)
        out = str(p.with_name(f"{p.stem}_highlight_{int(t)}s{p.suffix}"))
        self._spawn_studio_job(
            f"Хайлайт {format_duration(t)}: {p.name}",
            f"{format_duration(s)} – {format_duration(e)}",
            "trim",
            {"input_path": inp, "start_sec": s, "end_sec": e, "output_path": out, "accurate": False},
        )

    def _run_burn_chat(self) -> None:
        inp = self.edit_hl_video.text().strip()
        if not inp or not os.path.exists(inp):
            QMessageBox.warning(self, "Наложение чата", "Выберите видеофайл.")
            return
        ass_p = Path(inp).with_suffix(".chat.ass")
        if not ass_p.exists() and self.edit_hl_chat.text().strip():
            ass_p = Path(self.edit_hl_chat.text().strip()).with_suffix(".ass")
        if not ass_p.exists():
            QMessageBox.warning(self, "Наложение чата", "Не найден файл субтитров чата (.chat.ass). Включите запись чата в настройках канала.")
            return
        p = Path(inp)
        out = str(p.with_name(f"{p.stem}_with_chat{p.suffix}"))
        self._spawn_studio_job(
            f"Вшивание чата: {p.name}",
            ass_p.name,
            "burn_chat",
            {"input_path": inp, "ass_path": str(ass_p), "output_path": out},
        )

    def _run_whisper(self, mode: str) -> None:
        inp = self.edit_ai_file.text().strip()
        if not inp or not os.path.exists(inp):
            QMessageBox.warning(self, "AI Whisper", "Выберите видеофайл.")
            return
        jid = uuid.uuid4().hex[:8]
        title = f"{'AI Субтитры' if mode == 'subtitles' else 'AI Цензура'}: {Path(inp).name}"
        sub = f"Whisper {self.combo_ai_model.currentText()} · {self.combo_ai_lang.currentText()}"
        card = JobCard(jid, title, sub)
        card.reveal_clicked.connect(self._reveal_fn)
        self._cards[jid] = card
        self.jobs_layout.insertWidget(0, card)

        cfg = self._config_fn()
        w = WhisperAIWorker(
            job_id=jid,
            mode=mode,
            video_path=inp,
            model_size=self.combo_ai_model.currentText(),
            language=self.combo_ai_lang.currentData(),
            burn_subtitles=self.sw_burn_subs.isChecked(),
            shorts_style=self.sw_shorts_subs.isChecked(),
            banned_words=self.edit_banned.text(),
            censor_beep=self.sw_beep_mode.isChecked(),
            encoder=resolve_encoder(cfg.encoder),
            quality=cfg.video_quality,
            parent=self,
        )
        w.progress_updated.connect(self._on_job_progress)
        w.log_message.connect(self.log_message.emit)
        w.finished_ai.connect(self._on_job_done)
        self._workers[jid] = w
        w.start()

    def _run_export(self) -> None:
        inp = self.edit_exp_file.text().strip()
        if not inp or not os.path.exists(inp):
            QMessageBox.warning(self, "Экспорт", "Выберите видеофайл.")
            return
        pid = self.combo_exp_preset.currentData()
        p = Path(inp)
        out = str(p.with_name(f"{p.stem}_{pid}.mp4"))
        self._spawn_studio_job(
            f"Экспорт: {p.name}",
            EXPORT_PRESETS[pid]["label"],
            "export_preset",
            {
                "input_path": inp,
                "output_path": out,
                "preset_id": pid,
                "normalize_audio": self.sw_exp_norm.isChecked(),
            },
        )
