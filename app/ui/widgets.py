from __future__ import annotations

import os
from typing import List, Optional, Tuple

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QColor, QDragEnterEvent, QDropEvent, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from app.config import ChannelConfig
from app.recorder import format_duration
from app.twitch_api import StreamStatus
from app.ui.icons import ToggleSwitch, make_icon

QUALITY_OPTIONS = ["best", "1080p60", "1080p", "720p60", "720p", "480p", "360p", "worst"]
BLUR_MODES = [
    ("Постобработка", "postprocess"),
    ("Реалтайм (GPU)", "realtime"),
    ("Без блюра", "none"),
]

PLATFORM_COLORS = {
    "twitch": ("#9146FF", "TWITCH"),
    "youtube": ("#EF4444", "YOUTUBE"),
    "kick": ("#22C55E", "KICK"),
    "vkplay": ("#3B82F6", "VK PLAY"),
    "trovo": ("#10B981", "TROVO"),
}


class StatusPill(QLabel):
    def __init__(self, text: str = "ОФФЛАЙН", parent: Optional[QWidget] = None):
        super().__init__(text, parent)
        self.setAlignment(Qt.AlignCenter)
        self.setFixedHeight(24)
        self.set_state("offline", text)

    def set_state(self, state: str, text: str) -> None:
        self.setText(text)
        styles = {
            "recording": "background-color: rgba(239, 68, 68, 0.18); color: #F87171; border: 1px solid rgba(239, 68, 68, 0.45);",
            "live": "background-color: rgba(34, 197, 94, 0.16); color: #4ADE80; border: 1px solid rgba(34, 197, 94, 0.4);",
            "error": "background-color: rgba(245, 158, 11, 0.16); color: #FBBF24; border: 1px solid rgba(245, 158, 11, 0.4);",
            "offline": "background-color: #20232E; color: #8E92A4; border: 1px solid #2D3140;",
        }
        base = styles.get(state, styles["offline"])
        self.setStyleSheet(f"{base} border-radius: 12px; padding: 0 10px; font-size: 8.5pt; font-weight: 700;")


class ChannelCard(QFrame):
    """Spacious channel card with live preview thumbnail, scene switcher, marker button, and settings gear."""

    quality_changed = Signal(str, str)       # key, quality
    blur_mode_changed = Signal(str, str)     # key, mode
    preset_changed = Signal(str, str)        # key, preset_name
    auto_toggled = Signal(str, bool)         # key, checked
    edit_blur_clicked = Signal(str)          # key
    settings_clicked = Signal(str)           # key
    marker_clicked = Signal(str)             # key
    record_clicked = Signal(str)             # key
    stop_clicked = Signal(str)               # key
    delete_clicked = Signal(str)             # key

    def __init__(self, channel: ChannelConfig, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.key = channel.key
        self._is_recording = False

        self.setProperty("card", "true")
        self.setProperty("hoverable", "true")
        self.setProperty("recording", "false")

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 14, 16, 14)
        outer.setSpacing(10)

        # ---- Top Section: Thumbnail (when live) + Info + Action Buttons ----
        top_wrap = QHBoxLayout()
        top_wrap.setSpacing(14)

        self.lbl_thumb = QLabel()
        self.lbl_thumb.setFixedSize(116, 65)
        self.lbl_thumb.setStyleSheet(
            "background-color: #111218; border: 1px solid #262936; border-radius: 8px;"
        )
        self.lbl_thumb.setAlignment(Qt.AlignCenter)
        self.lbl_thumb.setVisible(False)
        top_wrap.addWidget(self.lbl_thumb)

        info_col = QVBoxLayout()
        info_col.setSpacing(5)

        row_head = QHBoxLayout()
        row_head.setSpacing(8)

        self.pill = StatusPill("ОФФЛАЙН")
        p_col, p_txt = PLATFORM_COLORS.get(channel.platform, ("#9146FF", channel.platform.upper()))
        self.lbl_plat = QLabel(p_txt)
        self.lbl_plat.setStyleSheet(
            f"background-color: #12131A; color: {p_col}; border: 1px solid {p_col}55; "
            "border-radius: 5px; padding: 2px 6px; font-size: 7.5pt; font-weight: 700;"
        )

        self.lbl_name = QLabel(channel.username)
        self.lbl_name.setStyleSheet("font-size: 12pt; font-weight: 700; color: #F5F6FA;")

        self.lbl_meta = QLabel("")
        self.lbl_meta.setProperty("muted", "true")

        row_head.addWidget(self.pill)
        row_head.addWidget(self.lbl_plat)
        row_head.addWidget(self.lbl_name)
        row_head.addWidget(self.lbl_meta)
        row_head.addStretch()
        info_col.addLayout(row_head)

        self.lbl_title = QLabel("")
        self.lbl_title.setProperty("muted", "true")
        self.lbl_title.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.lbl_title.setVisible(False)
        info_col.addWidget(self.lbl_title)

        top_wrap.addLayout(info_col, 1)

        # Right side telemetry & action buttons
        act_row = QHBoxLayout()
        act_row.setSpacing(8)

        self.lbl_telemetry = QLabel("")
        self.lbl_telemetry.setProperty("mono", "true")
        self.lbl_telemetry.setStyleSheet(
            "background-color: #12131A; color: #F87171; border: 1px solid rgba(239, 68, 68, 0.35); "
            "border-radius: 6px; padding: 4px 10px; font-size: 9pt;"
        )
        self.lbl_telemetry.setVisible(False)
        act_row.addWidget(self.lbl_telemetry)

        self.btn_marker = QPushButton("Маркер")
        self.btn_marker.setIcon(make_icon("bookmark", "#C4B5FD", 14))
        self.btn_marker.setProperty("role", "soft-primary")
        self.btn_marker.setToolTip("Поставить таймкод хайлайта (также клавиша F9)")
        self.btn_marker.setCursor(Qt.PointingHandCursor)
        self.btn_marker.setVisible(False)
        self.btn_marker.clicked.connect(lambda: self.marker_clicked.emit(self.key))
        act_row.addWidget(self.btn_marker)

        self.btn_action = QPushButton("Записать")
        self.btn_action.setCursor(Qt.PointingHandCursor)
        self.btn_action.setMinimumWidth(116)
        self.btn_action.clicked.connect(self._on_action_clicked)
        act_row.addWidget(self.btn_action)

        self.btn_settings = QPushButton()
        self.btn_settings.setIcon(make_icon("gear", "#9CA0B0", 16))
        self.btn_settings.setProperty("role", "ghost")
        self.btn_settings.setToolTip("Расписание, фильтры, чат, переподключение, разбивка")
        self.btn_settings.setFixedSize(34, 34)
        self.btn_settings.setCursor(Qt.PointingHandCursor)
        self.btn_settings.clicked.connect(lambda: self.settings_clicked.emit(self.key))
        act_row.addWidget(self.btn_settings)

        self.btn_delete = QPushButton()
        self.btn_delete.setIcon(make_icon("trash", "#8E92A4", 16))
        self.btn_delete.setProperty("role", "ghost-danger")
        self.btn_delete.setToolTip("Удалить канал")
        self.btn_delete.setFixedSize(34, 34)
        self.btn_delete.setCursor(Qt.PointingHandCursor)
        self.btn_delete.clicked.connect(lambda: self.delete_clicked.emit(self.key))
        act_row.addWidget(self.btn_delete)

        top_wrap.addLayout(act_row)
        outer.addLayout(top_wrap)

        # ---- Separator ----
        sep = QFrame()
        sep.setFixedHeight(1)
        sep.setStyleSheet("background-color: #232634; border: none;")
        outer.addWidget(sep)

        # ---- Bottom Controls Row ----
        bot = QHBoxLayout()
        bot.setSpacing(12)

        lbl_q = QLabel("Качество")
        lbl_q.setProperty("caption", "true")
        self.combo_quality = QComboBox()
        self.combo_quality.addItems(QUALITY_OPTIONS)
        self.combo_quality.setCurrentText(channel.quality)
        self.combo_quality.setFixedWidth(126)
        self.combo_quality.currentTextChanged.connect(lambda val: self.quality_changed.emit(self.key, val))

        lbl_b = QLabel("Блюр")
        lbl_b.setProperty("caption", "true")
        self.combo_blur = QComboBox()
        for label, val in BLUR_MODES:
            self.combo_blur.addItem(label, val)
        self.combo_blur.setFixedWidth(174)
        self.combo_blur.currentIndexChanged.connect(
            lambda _: self.blur_mode_changed.emit(self.key, self.combo_blur.currentData())
        )

        # Scene preset switcher (#4) — visible when channel has >1 presets
        self.combo_preset = QComboBox()
        self.combo_preset.setFixedWidth(152)
        self.combo_preset.setToolTip("Активный пресет сцены блюра")
        self.combo_preset.currentTextChanged.connect(lambda name: self.preset_changed.emit(self.key, name))

        self.btn_zones = QPushButton()
        self.btn_zones.setCursor(Qt.PointingHandCursor)
        self.btn_zones.clicked.connect(lambda: self.edit_blur_clicked.emit(self.key))

        self.lbl_badges = QLabel("")
        self.lbl_badges.setProperty("caption", "true")

        auto_box = QHBoxLayout()
        auto_box.setSpacing(8)
        lbl_auto = QLabel("Автозапись")
        lbl_auto.setProperty("muted", "true")
        self.sw_auto = ToggleSwitch(channel.auto_record)
        self.sw_auto.toggled.connect(lambda checked: self.auto_toggled.emit(self.key, checked))
        auto_box.addWidget(lbl_auto)
        auto_box.addWidget(self.sw_auto)

        bot.addWidget(lbl_q)
        bot.addWidget(self.combo_quality)
        bot.addWidget(lbl_b)
        bot.addWidget(self.combo_blur)
        bot.addWidget(self.combo_preset)
        bot.addWidget(self.btn_zones)
        bot.addWidget(self.lbl_badges)
        bot.addStretch()
        bot.addLayout(auto_box)
        outer.addLayout(bot)

        self.sync_channel(channel, None, False)

    def _on_action_clicked(self) -> None:
        if self._is_recording:
            self.stop_clicked.emit(self.key)
        else:
            self.record_clicked.emit(self.key)

    def set_preview_pixmap(self, raw_bytes: Optional[bytes]) -> None:
        if not raw_bytes:
            self.lbl_thumb.setVisible(False)
            return
        pm = QPixmap()
        if pm.loadFromData(raw_bytes):
            scaled = pm.scaled(116, 65, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
            # Round corners cleanly
            out = QPixmap(116, 65)
            out.fill(Qt.transparent)
            p = QPainter(out)
            p.setRenderHint(QPainter.Antialiasing)
            path = QPainterPath()
            path.addRoundedRect(QRectF(0, 0, 116, 65), 8, 8)
            p.setClipPath(path)
            dx = (116 - scaled.width()) // 2
            dy = (65 - scaled.height()) // 2
            p.drawPixmap(dx, dy, scaled)
            p.end()
            self.lbl_thumb.setPixmap(out)
            self.lbl_thumb.setVisible(True)

    def sync_channel(
        self,
        channel: ChannelConfig,
        status: Optional[StreamStatus],
        is_recording: bool,
    ) -> None:
        self._is_recording = is_recording

        rec_flag = "true" if is_recording else "false"
        if self.property("recording") != rec_flag:
            self.setProperty("recording", rec_flag)
            self.style().unpolish(self)
            self.style().polish(self)

        if is_recording:
            self.pill.set_state("recording", "ЗАПИСЬ")
            self.btn_action.setText("Остановить")
            self.btn_action.setIcon(make_icon("stop", "#FFFFFF", 14))
            self.btn_action.setProperty("role", "danger")
            self.lbl_telemetry.setVisible(True)
            self.btn_marker.setVisible(True)
            if not self.lbl_telemetry.text():
                self.lbl_telemetry.setText("Подключение...")
        else:
            self.lbl_telemetry.setVisible(False)
            self.lbl_telemetry.setText("")
            self.btn_marker.setVisible(False)
            self.btn_action.setText("Записать")
            self.btn_action.setIcon(make_icon("record", "#FFFFFF", 13))
            self.btn_action.setProperty("role", "success" if (status and status.is_live) else "")
            if status and status.is_live:
                self.pill.set_state("live", "В ЭФИРЕ")
            elif status and status.error:
                self.pill.set_state("error", "ОШИБКА")
            else:
                self.pill.set_state("offline", "ОФФЛАЙН")
                self.lbl_thumb.setVisible(False)

        self.btn_action.style().unpolish(self.btn_action)
        self.btn_action.style().polish(self.btn_action)

        if status and status.is_live:
            meta_parts = []
            if status.game:
                meta_parts.append(status.game)
            if status.viewers:
                meta_parts.append(f"{status.viewers:,} зрит.".replace(",", " "))
            self.lbl_meta.setText("  ·  ".join(meta_parts))
            if status.title:
                t = status.title.strip()
                self.lbl_title.setText(t[:130] + ("…" if len(t) > 130 else ""))
                self.lbl_title.setToolTip(t)
                self.lbl_title.setVisible(True)
            else:
                self.lbl_title.setVisible(False)
        elif status and status.error:
            self.lbl_meta.setText(status.error)
            self.lbl_title.setVisible(False)
        else:
            self.lbl_meta.setText("Не в сети")
            self.lbl_title.setVisible(False)

        # Scene preset selector (#4)
        self.combo_preset.blockSignals(True)
        self.combo_preset.clear()
        for pname in channel.blur_presets.keys():
            self.combo_preset.addItem(pname)
        pidx = self.combo_preset.findText(channel.active_preset)
        if pidx >= 0:
            self.combo_preset.setCurrentIndex(pidx)
        self.combo_preset.setVisible(len(channel.blur_presets) > 1)
        self.combo_preset.blockSignals(False)

        z_count = len(channel.blur_regions)
        if z_count > 0:
            self.btn_zones.setText(f"Зоны блюра: {z_count}")
            self.btn_zones.setIcon(make_icon("crop", "#C4B5FD", 15))
            self.btn_zones.setProperty("role", "soft-primary")
        else:
            self.btn_zones.setText("Настроить блюр")
            self.btn_zones.setIcon(make_icon("crop", "#E8E9EE", 15))
            self.btn_zones.setProperty("role", "")
        self.btn_zones.style().unpolish(self.btn_zones)
        self.btn_zones.style().polish(self.btn_zones)

        self.combo_blur.blockSignals(True)
        idx = self.combo_blur.findData(channel.blur_mode)
        if idx >= 0:
            self.combo_blur.setCurrentIndex(idx)
        self.combo_blur.blockSignals(False)

        # Compact status badges for advanced features
        b_tags = []
        if channel.schedule_enabled:
            b_tags.append(f"Расписание {channel.schedule_start}–{channel.schedule_end}")
        if channel.category_filter.strip():
            b_tags.append("Фильтр игр")
        if channel.segment_minutes > 0:
            b_tags.append(f"Части {channel.segment_minutes}м")
        self.lbl_badges.setText("  ·  ".join(b_tags))

    def set_recording_status(self, text: str) -> None:
        self.pill.set_state("recording", text.upper())

    def set_telemetry(self, dur: str, size: str, speed: str) -> None:
        self.lbl_telemetry.setVisible(True)
        self.lbl_telemetry.setText(f"{dur}  ·  {size}  ·  {speed}")


class JobCard(QFrame):
    cancel_clicked = Signal(str)
    reveal_clicked = Signal(str)

    def __init__(self, job_id: str, title: str, subtitle: str, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.job_id = job_id
        self._output_path = ""
        self.setProperty("card", "true")

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(10)

        top = QHBoxLayout()
        top.setSpacing(10)

        t_col = QVBoxLayout()
        t_col.setSpacing(2)
        self.lbl_title = QLabel(title)
        self.lbl_title.setStyleSheet("font-size: 10.5pt; font-weight: 600; color: #F4F5F8;")
        self.lbl_sub = QLabel(subtitle)
        self.lbl_sub.setProperty("caption", "true")
        t_col.addWidget(self.lbl_title)
        t_col.addWidget(self.lbl_sub)
        top.addLayout(t_col, 1)

        self.lbl_status = QLabel("В очереди...")
        self.lbl_status.setProperty("mono", "true")
        self.lbl_status.setStyleSheet("color: #9CA0B0; font-size: 9pt;")
        top.addWidget(self.lbl_status)

        self.btn_cancel = QPushButton("Отмена")
        self.btn_cancel.setIcon(make_icon("x", "#F87171", 14))
        self.btn_cancel.setProperty("role", "ghost-danger")
        self.btn_cancel.setCursor(Qt.PointingHandCursor)
        self.btn_cancel.clicked.connect(lambda: self.cancel_clicked.emit(self.job_id))
        top.addWidget(self.btn_cancel)

        self.btn_reveal = QPushButton("Показать в папке")
        self.btn_reveal.setIcon(make_icon("folder", "#E8E9EE", 15))
        self.btn_reveal.setCursor(Qt.PointingHandCursor)
        self.btn_reveal.setVisible(False)
        self.btn_reveal.clicked.connect(lambda: self.reveal_clicked.emit(self._output_path))
        top.addWidget(self.btn_reveal)

        root.addLayout(top)

        self.pbar = QProgressBar()
        self.pbar.setRange(0, 100)
        self.pbar.setValue(0)
        root.addWidget(self.pbar)

    def update_progress(self, pct: float, status: str) -> None:
        self.pbar.setValue(int(min(100, max(0, pct))))
        self.lbl_status.setText(status)

    def mark_finished(self, success: bool, output_path: str) -> None:
        self.btn_cancel.setVisible(False)
        if success and output_path:
            self._output_path = output_path
            self.pbar.setValue(100)
            self.pbar.setStyleSheet("QProgressBar::chunk { background-color: #16A34A; border-radius: 5px; }")
            self.btn_reveal.setVisible(True)
            self.lbl_status.setStyleSheet("color: #4ADE80; font-size: 9pt;")
        else:
            self.pbar.setStyleSheet("QProgressBar::chunk { background-color: #DC2626; border-radius: 5px; }")
            self.lbl_status.setStyleSheet("color: #F87171; font-size: 9pt;")


class DropFileCard(QFrame):
    file_dropped = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setProperty("card", "true")

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls():
            for u in event.mimeData().urls():
                if u.isLocalFile() and os.path.splitext(u.toLocalFile())[1].lower() in (
                    ".mp4", ".mkv", ".ts", ".flv", ".mov", ".avi", ".webm"
                ):
                    event.acceptProposedAction()
                    return
        event.ignore()

    def dropEvent(self, event: QDropEvent) -> None:
        for u in event.mimeData().urls():
            if u.isLocalFile():
                self.file_dropped.emit(u.toLocalFile())
                break


class HeatmapWidget(QWidget):
    """Interactive activity heatmap bar chart for Chat & Audio highlights (#16, #18)."""

    timestamp_clicked = Signal(float)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setMinimumHeight(130)
        self.setCursor(Qt.PointingHandCursor)
        self._data: List[Tuple[float, float]] = []
        self._accent = QColor("#8B5CF6")
        self._hot = QColor("#F97316")

    def set_data(self, data: List[Tuple[float, float]], accent: str = "#8B5CF6") -> None:
        self._data = list(data)
        self._accent = QColor(accent)
        self.update()

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), QColor("#111218"))
        p.setPen(QColor("#262936"))
        p.drawRoundedRect(self.rect().adjusted(0, 0, -1, -1), 8, 8)

        if not self._data:
            p.setPen(QColor("#7D8194"))
            p.drawText(self.rect(), Qt.AlignCenter, "Загрузите видео или .chat.json для построения тепловой карты")
            return

        w, h = self.width() - 16, self.height() - 28
        max_v = max(v for _, v in self._data) or 1.0
        n = len(self._data)
        bar_w = max(2.0, w / n)
        for i, (t, val) in enumerate(self._data):
            frac = val / max_v
            bh = max(2.0, frac * h)
            x = 8.0 + i * (w / n)
            y = 10.0 + (h - bh)
            col = self._hot if frac >= 0.72 else self._accent
            p.fillRect(QRectF(x, y, max(1.5, bar_w - 1.0), bh), col)

        p.setPen(QColor("#8E92A4"))
        p.drawText(10, self.height() - 6, "00:00:00")
        end_lbl = format_duration(self._data[-1][0])
        p.drawText(self.width() - 68, self.height() - 6, end_lbl)

    def mousePressEvent(self, event) -> None:
        if not self._data:
            return
        w = max(1.0, float(self.width() - 16))
        frac = max(0.0, min(1.0, (event.position().x() - 8.0) / w))
        idx = min(len(self._data) - 1, int(frac * len(self._data)))
        self.timestamp_clicked.emit(self._data[idx][0])
