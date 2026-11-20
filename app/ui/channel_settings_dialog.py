from __future__ import annotations

from copy import deepcopy
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from app.config import ChannelConfig
from app.ui.icons import ToggleSwitch, apply_windows_dark_titlebar, make_icon

DAY_NAMES = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


class ChannelSettingsDialog(QDialog):
    """Per-channel advanced settings: reconnect, concat, split, filters, schedule, chat recording."""

    def __init__(self, channel: ChannelConfig, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle(f"Параметры канала — {channel.username}")
        self.resize(560, 620)
        apply_windows_dark_titlebar(self)
        self.channel = deepcopy(channel)
        self._day_checks = []
        self._build_ui()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(14)

        # Card 1: Recording & Reliability
        c1 = QFrame()
        c1.setProperty("card", "true")
        l1 = QVBoxLayout(c1)
        l1.setContentsMargins(16, 14, 16, 14)
        l1.setSpacing(10)

        h1 = QLabel("Запись и надёжность")
        h1.setProperty("h2", "true")
        l1.addWidget(h1)

        self.sw_reconnect = self._add_switch_row(
            l1, "Авто-переподключение при обрыве", "Продолжит запись, если стрим или интернет моргнёт", self.channel.auto_reconnect
        )
        self.sw_concat = self._add_switch_row(
            l1, "Авто-склейка частей после обрыва", "Склеит разорванные куски стрима в один файл", self.channel.auto_concat_parts
        )
        self.sw_chat = self._add_switch_row(
            l1, "Параллельная запись чата Twitch", "Сохраняет .chat.json и .chat.ass для хайлайтов и наложения", self.channel.record_chat
        )
        self.sw_ads = self._add_switch_row(
            l1, "Пропуск рекламы Twitch", "Использовать --twitch-disable-ads", self.channel.disable_ads
        )
        self.sw_keep = self._add_switch_row(
            l1, "Сохранять исходник после авто-блюра", "Не удалять _raw файл после постобработки", self.channel.keep_original
        )

        seg_row = QHBoxLayout()
        seg_lbl = QLabel("Делить длинный стрим на части (минут, 0 = одним файлом):")
        seg_lbl.setProperty("muted", "true")
        self.spin_seg = QSpinBox()
        self.spin_seg.setRange(0, 720)
        self.spin_seg.setValue(self.channel.segment_minutes)
        self.spin_seg.setSuffix(" мин")
        self.spin_seg.setFixedWidth(120)
        seg_row.addWidget(seg_lbl, 1)
        seg_row.addWidget(self.spin_seg)
        l1.addLayout(seg_row)
        root.addWidget(c1)

        # Card 2: Category & Title filters (#10)
        c2 = QFrame()
        c2.setProperty("card", "true")
        l2 = QVBoxLayout(c2)
        l2.setContentsMargins(16, 14, 16, 14)
        l2.setSpacing(8)
        h2 = QLabel("Фильтры автозаписи (пусто = записывать всё)")
        h2.setProperty("h2", "true")
        l2.addWidget(h2)

        lbl_cat = QLabel("Разрешённые категории / игры (через запятую):")
        lbl_cat.setProperty("caption", "true")
        self.edit_cat = QLineEdit(self.channel.category_filter)
        self.edit_cat.setPlaceholderText("Напр.: Just Chatting, Counter-Strike 2, Dota 2")
        l2.addWidget(lbl_cat)
        l2.addWidget(self.edit_cat)

        lbl_tit = QLabel("Обязательные слова в названии стрима (через запятую):")
        lbl_tit.setProperty("caption", "true")
        self.edit_title = QLineEdit(self.channel.title_filter)
        self.edit_title.setPlaceholderText("Напр.: турнир, марафон, аукцион")
        l2.addWidget(lbl_tit)
        l2.addWidget(self.edit_title)
        root.addWidget(c2)

        # Card 3: Schedule (#13)
        c3 = QFrame()
        c3.setProperty("card", "true")
        l3 = QVBoxLayout(c3)
        l3.setContentsMargins(16, 14, 16, 14)
        l3.setSpacing(10)

        self.sw_sched = self._add_switch_row(
            l3, "Запись по расписанию", "Автомониторинг сработает только в указанные дни и часы", self.channel.schedule_enabled
        )

        time_row = QHBoxLayout()
        time_row.setSpacing(10)
        self.edit_s_start = QLineEdit(self.channel.schedule_start)
        self.edit_s_start.setFixedWidth(90)
        self.edit_s_start.setPlaceholderText("18:00")
        self.edit_s_end = QLineEdit(self.channel.schedule_end)
        self.edit_s_end.setFixedWidth(90)
        self.edit_s_end.setPlaceholderText("03:00")
        time_row.addWidget(QLabel("С:"))
        time_row.addWidget(self.edit_s_start)
        time_row.addWidget(QLabel("До:"))
        time_row.addWidget(self.edit_s_end)
        time_row.addStretch()
        l3.addLayout(time_row)

        days_row = QHBoxLayout()
        days_row.setSpacing(8)
        for i, dname in enumerate(DAY_NAMES):
            cb = QCheckBox(dname)
            cb.setChecked(i in self.channel.schedule_days)
            self._day_checks.append(cb)
            days_row.addWidget(cb)
        l3.addLayout(days_row)
        root.addWidget(c3)

        # Buttons
        btns = QHBoxLayout()
        btns.addStretch()
        btn_cancel = QPushButton("Отмена")
        btn_cancel.clicked.connect(self.reject)
        btn_save = QPushButton("Сохранить")
        btn_save.setIcon(make_icon("check", "#FFFFFF", 15))
        btn_save.setProperty("role", "primary")
        btn_save.clicked.connect(self._on_save)
        btns.addWidget(btn_cancel)
        btns.addWidget(btn_save)
        root.addLayout(btns)

    def _add_switch_row(self, parent_lay: QVBoxLayout, title: str, desc: str, checked: bool) -> ToggleSwitch:
        row = QHBoxLayout()
        col = QVBoxLayout()
        col.setSpacing(1)
        t = QLabel(title)
        t.setStyleSheet("font-size: 9.5pt; font-weight: 600;")
        d = QLabel(desc)
        d.setProperty("caption", "true")
        col.addWidget(t)
        col.addWidget(d)
        sw = ToggleSwitch(checked)
        row.addLayout(col, 1)
        row.addWidget(sw)
        parent_lay.addLayout(row)
        return sw

    def _on_save(self) -> None:
        self.channel.auto_reconnect = self.sw_reconnect.isChecked()
        self.channel.auto_concat_parts = self.sw_concat.isChecked()
        self.channel.record_chat = self.sw_chat.isChecked()
        self.channel.disable_ads = self.sw_ads.isChecked()
        self.channel.keep_original = self.sw_keep.isChecked()
        self.channel.segment_minutes = self.spin_seg.value()
        self.channel.category_filter = self.edit_cat.text().strip()
        self.channel.title_filter = self.edit_title.text().strip()
        self.channel.schedule_enabled = self.sw_sched.isChecked()
        self.channel.schedule_start = self.edit_s_start.text().strip() or "00:00"
        self.channel.schedule_end = self.edit_s_end.text().strip() or "23:59"
        self.channel.schedule_days = [i for i, cb in enumerate(self._day_checks) if cb.isChecked()]
        self.accept()

    def get_channel(self) -> ChannelConfig:
        return self.channel
