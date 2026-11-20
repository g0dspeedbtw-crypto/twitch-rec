from __future__ import annotations

import os
import tempfile
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Dict, List, Optional

from PIL import Image, ImageDraw, ImageFilter
from PySide6.QtCore import QPointF, QRectF, Qt, QThread, Signal
from PySide6.QtGui import QColor, QFont, QImage, QKeyEvent, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from app.config import TEMPLATES_DIR, BlurRegion, ChannelConfig
from app.recorder import format_duration
from app.tools import extract_frame_from_file, probe_video
from app.twitch_api import capture_live_snapshot, capture_vod_snapshot
from app.ui.icons import ToggleSwitch, apply_windows_dark_titlebar, make_icon
from app.ui.theme import pick_ui_font_family


class FrameLoaderWorker(QThread):
    loaded = Signal(bool, str, str)

    def __init__(
        self,
        mode: str,
        target,
        out_jpg: str,
        timestamp_sec: float = 10.0,
        quality: str = "best",
        proxy_url: str = "",
        parent=None,
    ):
        super().__init__(parent)
        self.mode = mode
        self.target = target
        self.out_jpg = out_jpg
        self.timestamp_sec = timestamp_sec
        self.quality = quality
        self.proxy_url = proxy_url

    def run(self) -> None:
        if self.mode == "live":
            ok, msg = capture_live_snapshot(self.target, self.out_jpg, self.quality, self.proxy_url)
            self.loaded.emit(ok, self.out_jpg if ok else "", msg)
        elif self.mode == "vod":
            ok, msg = capture_vod_snapshot(self.target, self.out_jpg, self.timestamp_sec)
            self.loaded.emit(ok, self.out_jpg if ok else "", msg)
        elif self.mode == "file":
            ok, msg = extract_frame_from_file(self.target, self.timestamp_sec, self.out_jpg)
            self.loaded.emit(ok, self.out_jpg if ok else "", msg or "Кадр извлечён")
        else:
            self.loaded.emit(False, "", "Неизвестный источник кадра")


def _make_feather_mask(w: int, h: int, feather: int) -> Image.Image:
    """Create a smooth feathered alpha mask for PIL preview."""
    mask = Image.new("L", (w, h), 255)
    f = max(0, min(feather, w // 2, h // 2))
    if f <= 0:
        return mask
    # Inset inner white rectangle and blur the mask edges
    mask = Image.new("L", (w, h), 0)
    draw = ImageDraw.Draw(mask)
    draw.rectangle((f, f, max(f, w - f - 1), max(f, h - f - 1)), fill=255)
    return mask.filter(ImageFilter.GaussianBlur(radius=max(1, f * 0.6)))


class BlurCanvas(QWidget):
    regions_changed = Signal()
    selection_changed = Signal(int)

    HANDLE_SIZE = 10.0

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(700, 410)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)

        self._font_family = pick_ui_font_family()
        self._orig_pil: Optional[Image.Image] = None
        self._display_pixmap: Optional[QPixmap] = None
        self._regions: List[BlurRegion] = []
        self._selected_idx: int = -1
        self._preview_enabled: bool = True

        self._action: Optional[str] = None
        self._drag_start_norm: Optional[QPointF] = None
        self._orig_rect: Optional[BlurRegion] = None
        self._draft_rect: Optional[QRectF] = None
        self._default_mode: str = "blur"
        self._default_strength: int = 25

    def has_image(self) -> bool:
        return self._orig_pil is not None

    def save_region_template(self, idx: int) -> Optional[str]:
        """Crop the current frame inside region idx and save as PNG template for OpenCV auto-detect."""
        if self._orig_pil is None or not (0 <= idx < len(self._regions)):
            return None
        r = self._regions[idx].clamped()
        w_img, h_img = self._orig_pil.size
        x1 = max(0, min(w_img - 2, int(r.x * w_img)))
        y1 = max(0, min(h_img - 2, int(r.y * h_img)))
        x2 = max(x1 + 4, min(w_img, int((r.x + r.w) * w_img)))
        y2 = max(y1 + 4, min(h_img, int((r.y + r.h) * h_img)))
        patch = self._orig_pil.crop((x1, y1, x2, y2))
        TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
        out = str(TEMPLATES_DIR / f"banner_{uuid.uuid4().hex[:10]}.png")
        patch.save(out)
        self._regions[idx].template_path = out
        return out

    def set_regions(self, regions: List[BlurRegion]) -> None:
        self._regions = [deepcopy(r).clamped() for r in regions]
        self._selected_idx = 0 if self._regions else -1
        self._rebuild_preview_pixmap()
        self.update()
        self.selection_changed.emit(self._selected_idx)
        self.regions_changed.emit()

    def get_regions(self) -> List[BlurRegion]:
        return [deepcopy(r).clamped() for r in self._regions]

    def selected_index(self) -> int:
        return self._selected_idx

    def select_index(self, idx: int) -> None:
        if -1 <= idx < len(self._regions):
            self._selected_idx = idx
            self.update()
            self.selection_changed.emit(idx)

    def set_default_style(self, mode: str, strength: int) -> None:
        self._default_mode = mode
        self._default_strength = strength

    def set_preview_enabled(self, enabled: bool) -> None:
        self._preview_enabled = enabled
        self._rebuild_preview_pixmap()
        self.update()

    def load_image(self, image_path: str) -> bool:
        try:
            img = Image.open(image_path).convert("RGB")
            if img.width > 1280:
                ratio = 1280 / img.width
                img = img.resize((1280, max(1, int(img.height * ratio))), Image.Resampling.LANCZOS)
            self._orig_pil = img
            self._rebuild_preview_pixmap()
            self.update()
            return True
        except Exception:
            return False

    def update_region_props(self, idx: int, updated: BlurRegion) -> None:
        if 0 <= idx < len(self._regions):
            self._regions[idx] = updated.clamped()
            self._rebuild_preview_pixmap()
            self.update()
            self.regions_changed.emit()

    def delete_selected(self) -> None:
        if 0 <= self._selected_idx < len(self._regions):
            self._regions.pop(self._selected_idx)
            self._selected_idx = min(self._selected_idx, len(self._regions) - 1)
            self._rebuild_preview_pixmap()
            self.update()
            self.selection_changed.emit(self._selected_idx)
            self.regions_changed.emit()

    def clear_regions(self) -> None:
        self._regions.clear()
        self._selected_idx = -1
        self._rebuild_preview_pixmap()
        self.update()
        self.selection_changed.emit(-1)
        self.regions_changed.emit()

    def _rebuild_preview_pixmap(self) -> None:
        if self._orig_pil is None:
            self._display_pixmap = None
            return

        img = self._orig_pil.copy()
        if self._preview_enabled and self._regions:
            w_img, h_img = img.size
            for r in self._regions:
                x1 = max(0, min(w_img - 1, int(r.x * w_img)))
                y1 = max(0, min(h_img - 1, int(r.y * h_img)))
                x2 = max(x1 + 2, min(w_img, int((r.x + r.w) * w_img)))
                y2 = max(y1 + 2, min(h_img, int((r.y + r.h) * h_img)))
                pw, ph = x2 - x1, y2 - y1

                if r.mode == "image" and r.image_path and os.path.exists(r.image_path):
                    try:
                        ov = Image.open(r.image_path).convert("RGBA").resize((pw, ph), Image.Resampling.LANCZOS)
                        img.paste(ov, (x1, y1), ov)
                        continue
                    except Exception:
                        pass

                patch = img.crop((x1, y1, x2, y2))
                if r.mode == "solid":
                    patch = Image.new("RGB", patch.size, (0, 0, 0))
                elif r.mode == "pixelate":
                    factor = max(4, min(48, int(r.strength)))
                    sw = max(2, patch.width // factor)
                    sh = max(2, patch.height // factor)
                    patch = patch.resize((sw, sh), Image.Resampling.BOX).resize(
                        patch.size, Image.Resampling.NEAREST
                    )
                else:
                    radius = max(1.5, r.strength * (w_img / 1920.0))
                    patch = patch.filter(ImageFilter.GaussianBlur(radius=radius))

                if r.feather > 0:
                    f_scaled = max(2, int(r.feather * (w_img / 1920.0)))
                    mask = _make_feather_mask(pw, ph, f_scaled)
                    img.paste(patch, (x1, y1), mask)
                else:
                    img.paste(patch, (x1, y1))

        data = img.tobytes("raw", "RGB")
        qimg = QImage(data, img.width, img.height, img.width * 3, QImage.Format_RGB888)
        self._display_pixmap = QPixmap.fromImage(qimg.copy())

    def _image_rect(self) -> QRectF:
        w, h = float(self.width()), float(self.height())
        if self._display_pixmap and not self._display_pixmap.isNull():
            ar = self._display_pixmap.width() / max(1.0, float(self._display_pixmap.height()))
        else:
            ar = 16.0 / 9.0
        margin = 6.0
        avail_w = max(10.0, w - margin * 2)
        avail_h = max(10.0, h - margin * 2)
        if avail_w / avail_h > ar:
            draw_h = avail_h
            draw_w = draw_h * ar
        else:
            draw_w = avail_w
            draw_h = draw_w / ar
        return QRectF((w - draw_w) / 2.0, (h - draw_h) / 2.0, draw_w, draw_h)

    def _norm_to_widget(self, r: BlurRegion, img_rect: QRectF) -> QRectF:
        return QRectF(
            img_rect.left() + r.x * img_rect.width(),
            img_rect.top() + r.y * img_rect.height(),
            r.w * img_rect.width(),
            r.h * img_rect.height(),
        )

    def _widget_to_norm(self, pos: QPointF, img_rect: QRectF) -> QPointF:
        nx = (pos.x() - img_rect.left()) / max(1.0, img_rect.width())
        ny = (pos.y() - img_rect.top()) / max(1.0, img_rect.height())
        return QPointF(max(0.0, min(1.0, nx)), max(0.0, min(1.0, ny)))

    def _handle_rects(self, rect: QRectF) -> dict[str, QRectF]:
        hs = self.HANDLE_SIZE
        half = hs / 2.0
        return {
            "resize_tl": QRectF(rect.left() - half, rect.top() - half, hs, hs),
            "resize_tr": QRectF(rect.right() - half, rect.top() - half, hs, hs),
            "resize_bl": QRectF(rect.left() - half, rect.bottom() - half, hs, hs),
            "resize_br": QRectF(rect.right() - half, rect.bottom() - half, hs, hs),
        }

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(self.rect(), QColor("#0B0C10"))

        img_rect = self._image_rect()
        if self._display_pixmap:
            painter.drawPixmap(img_rect.toRect(), self._display_pixmap)
        else:
            painter.fillRect(img_rect, QColor("#14161D"))
            painter.setPen(QPen(QColor("#242736"), 1, Qt.DashLine))
            for frac in (0.25, 0.5, 0.75):
                gx = img_rect.left() + img_rect.width() * frac
                gy = img_rect.top() + img_rect.height() * frac
                painter.drawLine(QPointF(gx, img_rect.top()), QPointF(gx, img_rect.bottom()))
                painter.drawLine(QPointF(img_rect.left(), gy), QPointF(img_rect.right(), gy))
            painter.setPen(QColor("#8E92A4"))
            painter.setFont(QFont(self._font_family, 11))
            painter.drawText(
                img_rect,
                Qt.AlignCenter,
                "Нажмите «Снять кадр со стрима» или «Открыть видео / картинку»\n"
                "Либо выделите область прямо на сетке 16:9",
            )

        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(QColor("#2E3242"), 1.5))
        painter.drawRect(img_rect)

        for idx, r in enumerate(self._regions):
            wrect = self._norm_to_widget(r, img_rect)
            is_sel = idx == self._selected_idx

            if not self._display_pixmap or not self._preview_enabled:
                painter.fillRect(wrect, QColor(139, 92, 246, 75 if is_sel else 42))

            border_color = QColor("#C4B5FD") if is_sel else QColor("#8B5CF6")
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(border_color, 2.2 if is_sel else 1.6))
            painter.drawRect(wrect)

            tag_text = f"#{idx + 1}  {r.label}"
            if r.auto_detect:
                tag_text += " [AI]"
            painter.setFont(QFont(self._font_family, 9, QFont.DemiBold))
            fm = painter.fontMetrics()
            tw = fm.horizontalAdvance(tag_text) + 14
            th = fm.height() + 6
            tag_y = wrect.top() - th - 3 if wrect.top() - th - 3 >= img_rect.top() else wrect.top() + 3
            tag_rect = QRectF(wrect.left(), tag_y, tw, th)
            painter.setBrush(QColor("#8B5CF6" if is_sel else "#232633"))
            painter.setPen(Qt.NoPen)
            painter.drawRoundedRect(tag_rect, 4, 4)
            painter.setPen(QColor("#FFFFFF"))
            painter.drawText(tag_rect, Qt.AlignCenter, tag_text)

            if is_sel:
                for hrect in self._handle_rects(wrect).values():
                    painter.setBrush(QColor("#FFFFFF"))
                    painter.setPen(QPen(QColor("#8B5CF6"), 2))
                    painter.drawRoundedRect(hrect, 2, 2)

        if self._draft_rect:
            painter.fillRect(self._draft_rect, QColor(139, 92, 246, 65))
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(QColor("#FFFFFF"), 1.8, Qt.DashLine))
            painter.drawRect(self._draft_rect)

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.LeftButton:
            return
        pos = event.position()
        img_rect = self._image_rect()
        if not img_rect.contains(pos):
            return

        if 0 <= self._selected_idx < len(self._regions):
            sel_wrect = self._norm_to_widget(self._regions[self._selected_idx], img_rect)
            for name, hrect in self._handle_rects(sel_wrect).items():
                if hrect.adjusted(-4, -4, 4, 4).contains(pos):
                    self._action = name
                    self._drag_start_norm = self._widget_to_norm(pos, img_rect)
                    self._orig_rect = deepcopy(self._regions[self._selected_idx])
                    return

        for idx in range(len(self._regions) - 1, -1, -1):
            wrect = self._norm_to_widget(self._regions[idx], img_rect)
            if wrect.contains(pos):
                self._selected_idx = idx
                self._action = "move"
                self._drag_start_norm = self._widget_to_norm(pos, img_rect)
                self._orig_rect = deepcopy(self._regions[idx])
                self.selection_changed.emit(idx)
                self.update()
                return

        self._action = "draw"
        self._drag_start_norm = self._widget_to_norm(pos, img_rect)
        self._draft_rect = QRectF(pos, pos)
        self.update()

    def mouseMoveEvent(self, event) -> None:
        pos = event.position()
        img_rect = self._image_rect()
        norm = self._widget_to_norm(pos, img_rect)

        if self._action == "draw" and self._drag_start_norm is not None:
            p1 = QPointF(
                img_rect.left() + self._drag_start_norm.x() * img_rect.width(),
                img_rect.top() + self._drag_start_norm.y() * img_rect.height(),
            )
            p2 = QPointF(
                img_rect.left() + norm.x() * img_rect.width(),
                img_rect.top() + norm.y() * img_rect.height(),
            )
            self._draft_rect = QRectF(p1, p2).normalized()
            self.update()
            return

        if (
            self._action == "move"
            and self._orig_rect
            and self._drag_start_norm
            and 0 <= self._selected_idx < len(self._regions)
        ):
            dx = norm.x() - self._drag_start_norm.x()
            dy = norm.y() - self._drag_start_norm.y()
            r = self._regions[self._selected_idx]
            r.x = max(0.0, min(1.0 - self._orig_rect.w, self._orig_rect.x + dx))
            r.y = max(0.0, min(1.0 - self._orig_rect.h, self._orig_rect.y + dy))
            self.update()
            return

        if (
            self._action
            and self._action.startswith("resize_")
            and self._orig_rect
            and 0 <= self._selected_idx < len(self._regions)
        ):
            o = self._orig_rect
            left, top = o.x, o.y
            right, bottom = o.x + o.w, o.y + o.h
            min_sz = 0.015
            if self._action in ("resize_tl", "resize_bl"):
                left = max(0.0, min(right - min_sz, norm.x()))
            if self._action in ("resize_tr", "resize_br"):
                right = min(1.0, max(left + min_sz, norm.x()))
            if self._action in ("resize_tl", "resize_tr"):
                top = max(0.0, min(bottom - min_sz, norm.y()))
            if self._action in ("resize_bl", "resize_br"):
                bottom = min(1.0, max(top + min_sz, norm.y()))
            r = self._regions[self._selected_idx]
            r.x, r.y, r.w, r.h = left, top, right - left, bottom - top
            self.update()
            return

        cursor = Qt.CrossCursor
        if 0 <= self._selected_idx < len(self._regions):
            sel_wrect = self._norm_to_widget(self._regions[self._selected_idx], img_rect)
            for name, hrect in self._handle_rects(sel_wrect).items():
                if hrect.adjusted(-4, -4, 4, 4).contains(pos):
                    cursor = Qt.SizeFDiagCursor if name in ("resize_tl", "resize_br") else Qt.SizeBDiagCursor
                    self.setCursor(cursor)
                    return
        for r in self._regions:
            if self._norm_to_widget(r, img_rect).contains(pos):
                cursor = Qt.SizeAllCursor
                break
        self.setCursor(cursor)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() != Qt.LeftButton or not self._action:
            return
        img_rect = self._image_rect()
        if self._action == "draw" and self._draft_rect is not None:
            dr = self._draft_rect
            nw = dr.width() / max(1.0, img_rect.width())
            nh = dr.height() / max(1.0, img_rect.height())
            if nw >= 0.015 and nh >= 0.015:
                nx = (dr.left() - img_rect.left()) / max(1.0, img_rect.width())
                ny = (dr.top() - img_rect.top()) / max(1.0, img_rect.height())
                new_region = BlurRegion(
                    x=nx,
                    y=ny,
                    w=nw,
                    h=nh,
                    mode=self._default_mode,
                    strength=self._default_strength,
                    label=f"Баннер {len(self._regions) + 1}",
                ).clamped()
                self._regions.append(new_region)
                self._selected_idx = len(self._regions) - 1
                self.selection_changed.emit(self._selected_idx)
                self.regions_changed.emit()
            self._draft_rect = None
        else:
            self.regions_changed.emit()

        self._action = None
        self._drag_start_norm = None
        self._orig_rect = None
        self._rebuild_preview_pixmap()
        self.update()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() in (Qt.Key_Delete, Qt.Key_Backspace):
            self.delete_selected()
        else:
            super().keyPressEvent(event)


class BlurEditorDialog(QDialog):
    """
    Studio visual blur editor supporting:
      - #1 Soft feathered blur edges
      - #2 Custom logo / image overlay
      - #3 Smart OpenCV auto-banner detector (saves cropped template)
      - #4 Multiple scene presets per channel
      - #5 Timed blur intervals (start_sec..end_sec)
    """

    def __init__(
        self,
        regions: List[BlurRegion],
        channel: Optional[ChannelConfig] = None,
        video_path: str = "",
        vod_url: str = "",
        quality: str = "best",
        proxy_url: str = "",
        title_override: str = "",
        parent=None,
    ):
        super().__init__(parent)
        self.channel = deepcopy(channel) if channel else None
        self.channel_username = channel.username if channel else ""
        self.video_path = video_path
        self.vod_url = vod_url
        self.quality = quality
        self.proxy_url = proxy_url

        if title_override:
            self.setWindowTitle(title_override)
        elif self.channel_username:
            self.setWindowTitle(f"Редактор зон блюра — {self.channel_username}")
        else:
            self.setWindowTitle("Редактор зон блюра")

        self.resize(1240, 760)
        apply_windows_dark_titlebar(self)

        self._video_duration: float = 0.0
        self._temp_jpg = os.path.join(tempfile.gettempdir(), f"twitch_rec_snap_{os.getpid()}.jpg")
        self._loader: Optional[FrameLoaderWorker] = None
        self._updating_controls = False

        # Scene presets dict (if editing a channel)
        self._presets: Dict[str, List[BlurRegion]] = (
            deepcopy(self.channel.blur_presets) if self.channel else {"Основной": deepcopy(regions)}
        )
        self._active_preset: str = (
            self.channel.active_preset if self.channel else "Основной"
        )
        if self._active_preset not in self._presets:
            self._active_preset = next(iter(self._presets))

        self._build_ui()
        self._refresh_scene_combo()
        self.canvas.set_regions(self._presets[self._active_preset])

        if self.video_path and os.path.exists(self.video_path):
            self._init_video_file(self.video_path)
        elif self.channel:
            self._capture_live()
        elif self.vod_url:
            self._capture_vod()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(12)

        # Top toolbar
        top_bar = QHBoxLayout()
        top_bar.setSpacing(10)

        self.btn_live_snap = QPushButton("Снять кадр со стрима")
        self.btn_live_snap.setIcon(make_icon("camera", "#FFFFFF", 16))
        self.btn_live_snap.setProperty("role", "primary")
        self.btn_live_snap.setCursor(Qt.PointingHandCursor)
        self.btn_live_snap.clicked.connect(self._capture_live)
        self.btn_live_snap.setVisible(bool(self.channel))

        self.btn_vod_snap = QPushButton("Взять кадр из VOD")
        self.btn_vod_snap.setIcon(make_icon("film", "#FFFFFF", 16))
        self.btn_vod_snap.setProperty("role", "primary")
        self.btn_vod_snap.setCursor(Qt.PointingHandCursor)
        self.btn_vod_snap.clicked.connect(self._capture_vod)
        self.btn_vod_snap.setVisible(bool(self.vod_url))

        self.btn_load_video = QPushButton("Открыть видео / фото")
        self.btn_load_video.setIcon(make_icon("video", "#E8E9EE", 16))
        self.btn_load_video.setCursor(Qt.PointingHandCursor)
        self.btn_load_video.clicked.connect(self._pick_file_for_frame)

        # Scene preset selector (#4)
        self.scene_bar = QWidget()
        sb_l = QHBoxLayout(self.scene_bar)
        sb_l.setContentsMargins(0, 0, 0, 0)
        sb_l.setSpacing(6)
        lbl_sc = QLabel("Сцена:")
        lbl_sc.setProperty("muted", "true")
        self.combo_scene = QComboBox()
        self.combo_scene.setMinimumWidth(145)
        self.combo_scene.currentTextChanged.connect(self._on_scene_changed)
        btn_new_scene = QPushButton()
        btn_new_scene.setIcon(make_icon("plus", "#E8E9EE", 14))
        btn_new_scene.setToolTip("Новый пресет сцены (напр. Just Chatting / Игра)")
        btn_new_scene.setFixedSize(34, 34)
        btn_new_scene.clicked.connect(self._add_scene_preset)
        btn_del_scene = QPushButton()
        btn_del_scene.setIcon(make_icon("trash", "#F87171", 14))
        btn_del_scene.setProperty("role", "ghost-danger")
        btn_del_scene.setToolTip("Удалить текущую сцену")
        btn_del_scene.setFixedSize(34, 34)
        btn_del_scene.clicked.connect(self._delete_scene_preset)
        sb_l.addWidget(lbl_sc)
        sb_l.addWidget(self.combo_scene)
        sb_l.addWidget(btn_new_scene)
        sb_l.addWidget(btn_del_scene)
        self.scene_bar.setVisible(bool(self.channel))

        prev_wrap = QHBoxLayout()
        prev_wrap.setSpacing(8)
        self.sw_preview = ToggleSwitch(True)
        self.sw_preview.toggled.connect(self.canvas_preview_toggled)
        lbl_prev = QLabel("Предпросмотр")
        lbl_prev.setProperty("muted", "true")
        prev_wrap.addWidget(self.sw_preview)
        prev_wrap.addWidget(lbl_prev)

        self.lbl_status = QLabel("Выделите область мышкой")
        self.lbl_status.setProperty("muted", "true")

        top_bar.addWidget(self.btn_live_snap)
        top_bar.addWidget(self.btn_vod_snap)
        top_bar.addWidget(self.btn_load_video)
        top_bar.addWidget(self.scene_bar)
        top_bar.addLayout(prev_wrap)
        top_bar.addStretch()
        top_bar.addWidget(self.lbl_status)
        root.addLayout(top_bar)

        # Workspace
        mid = QHBoxLayout()
        mid.setSpacing(14)

        left_col = QVBoxLayout()
        left_col.setSpacing(10)

        canvas_frame = QFrame()
        canvas_frame.setProperty("card", "true")
        cf_l = QVBoxLayout(canvas_frame)
        cf_l.setContentsMargins(6, 6, 6, 6)
        self.canvas = BlurCanvas()
        self.canvas.regions_changed.connect(self._refresh_region_list)
        self.canvas.selection_changed.connect(self._on_canvas_selection)
        cf_l.addWidget(self.canvas, 1)
        left_col.addWidget(canvas_frame, 1)

        # Timeline scrub bar
        self.timeline_bar = QFrame()
        self.timeline_bar.setProperty("card", "true")
        tl_layout = QHBoxLayout(self.timeline_bar)
        tl_layout.setContentsMargins(14, 10, 14, 10)
        tl_layout.setSpacing(12)
        self.lbl_time = QLabel("00:00:00")
        self.lbl_time.setProperty("mono", "true")
        self.slider_time = QSlider(Qt.Horizontal)
        self.slider_time.setRange(0, 100)
        self.slider_time.sliderReleased.connect(self._on_timeline_released)
        self.slider_time.valueChanged.connect(self._on_timeline_moved)
        self.btn_grab_ts = QPushButton("Показать кадр")
        self.btn_grab_ts.clicked.connect(self._on_timeline_released)
        tl_layout.addWidget(self.lbl_time)
        tl_layout.addWidget(self.slider_time, 1)
        tl_layout.addWidget(self.btn_grab_ts)
        self.timeline_bar.setVisible(False)
        left_col.addWidget(self.timeline_bar)

        mid.addLayout(left_col, 1)

        # Right Scrollable Properties Panel
        side_card = QFrame()
        side_card.setProperty("card", "true")
        side_card.setFixedWidth(340)
        sc_outer = QVBoxLayout(side_card)
        sc_outer.setContentsMargins(0, 0, 0, 0)

        side_scroll = QScrollArea()
        side_scroll.setWidgetResizable(True)
        side_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        side_inner = QWidget()
        side_inner.setObjectName("ScrollContent")
        side = QVBoxLayout(side_inner)
        side.setContentsMargins(16, 14, 16, 14)
        side.setSpacing(10)

        lbl_list = QLabel("Зоны маскирования")
        lbl_list.setProperty("h2", "true")
        side.addWidget(lbl_list)

        self.list_regions = QListWidget()
        self.list_regions.setMinimumHeight(110)
        self.list_regions.setMaximumHeight(155)
        self.list_regions.currentRowChanged.connect(self._on_list_selection)
        side.addWidget(self.list_regions)

        self.edit_label = QLineEdit()
        self.edit_label.setPlaceholderText("Название зоны (напр. Баннер)")
        self.edit_label.textEdited.connect(self._apply_controls_to_selected)
        side.addWidget(self.edit_label)

        self.combo_mode = QComboBox()
        self.combo_mode.addItem("Размытие (Blur)", "blur")
        self.combo_mode.addItem("Пикселизация (Мозаика)", "pixelate")
        self.combo_mode.addItem("Чёрный прямоугольник", "solid")
        self.combo_mode.addItem("Заменить картинкой / лого", "image")
        self.combo_mode.currentIndexChanged.connect(self._apply_controls_to_selected)
        side.addWidget(self.combo_mode)

        # Custom Logo Overlay Picker (#2)
        self.image_row_widget = QWidget()
        img_l = QHBoxLayout(self.image_row_widget)
        img_l.setContentsMargins(0, 0, 0, 0)
        img_l.setSpacing(6)
        self.edit_img_path = QLineEdit()
        self.edit_img_path.setPlaceholderText("Путь к PNG/JPG логотипу...")
        self.edit_img_path.textEdited.connect(self._apply_controls_to_selected)
        btn_pick_img = QPushButton()
        btn_pick_img.setIcon(make_icon("image", "#E8E9EE", 15))
        btn_pick_img.setFixedSize(36, 36)
        btn_pick_img.clicked.connect(self._pick_overlay_image)
        img_l.addWidget(self.edit_img_path, 1)
        img_l.addWidget(btn_pick_img)
        self.image_row_widget.setVisible(False)
        side.addWidget(self.image_row_widget)

        # Strength slider
        str_h = QHBoxLayout()
        l_str = QLabel("Сила размытия")
        l_str.setProperty("caption", "true")
        self.lbl_strength_val = QLabel("25")
        self.lbl_strength_val.setProperty("mono", "true")
        str_h.addWidget(l_str)
        str_h.addStretch()
        str_h.addWidget(self.lbl_strength_val)
        side.addLayout(str_h)

        self.slider_strength = QSlider(Qt.Horizontal)
        self.slider_strength.setRange(4, 60)
        self.slider_strength.setValue(25)
        self.slider_strength.valueChanged.connect(self._apply_controls_to_selected)
        side.addWidget(self.slider_strength)

        # Soft feathered edges slider (#1)
        fea_h = QHBoxLayout()
        l_fea = QLabel("Мягкие края (Feather)")
        l_fea.setProperty("caption", "true")
        self.lbl_feather_val = QLabel("0 px")
        self.lbl_feather_val.setProperty("mono", "true")
        fea_h.addWidget(l_fea)
        fea_h.addStretch()
        fea_h.addWidget(self.lbl_feather_val)
        side.addLayout(fea_h)

        self.slider_feather = QSlider(Qt.Horizontal)
        self.slider_feather.setRange(0, 40)
        self.slider_feather.setValue(0)
        self.slider_feather.valueChanged.connect(self._apply_controls_to_selected)
        side.addWidget(self.slider_feather)

        # Timed blur intervals (#5)
        lbl_t = QLabel("Таймкод действия (сек, пусто = всё видео)")
        lbl_t.setProperty("caption", "true")
        side.addWidget(lbl_t)
        t_row = QHBoxLayout()
        t_row.setSpacing(8)
        self.edit_t_start = QLineEdit()
        self.edit_t_start.setPlaceholderText("От (напр. 60)")
        self.edit_t_start.editingFinished.connect(self._apply_controls_to_selected)
        self.edit_t_end = QLineEdit()
        self.edit_t_end.setPlaceholderText("До (напр. 180)")
        self.edit_t_end.editingFinished.connect(self._apply_controls_to_selected)
        t_row.addWidget(self.edit_t_start)
        t_row.addWidget(self.edit_t_end)
        side.addLayout(t_row)

        # Smart OpenCV Auto-Detect Banner (#3)
        ad_box = QFrame()
        ad_box.setProperty("surface", "subtle")
        ad_l = QVBoxLayout(ad_box)
        ad_l.setContentsMargins(10, 8, 10, 8)
        ad_l.setSpacing(6)
        ad_row = QHBoxLayout()
        lbl_ad = QLabel("Авто-детектор баннера (AI)")
        lbl_ad.setStyleSheet("font-size: 9pt; font-weight: 600;")
        self.sw_auto_detect = ToggleSwitch(False)
        self.sw_auto_detect.toggled.connect(self._on_auto_detect_toggled)
        ad_row.addWidget(lbl_ad, 1)
        ad_row.addWidget(self.sw_auto_detect)
        ad_l.addLayout(ad_row)

        self.lbl_tpl_status = QLabel("Блюрит зону только когда баннер виден в кадре")
        self.lbl_tpl_status.setProperty("caption", "true")
        self.lbl_tpl_status.setWordWrap(True)
        ad_l.addWidget(self.lbl_tpl_status)

        self.btn_save_tpl = QPushButton("Запомнить образец баннера")
        self.btn_save_tpl.setIcon(make_icon("sparkles", "#C4B5FD", 14))
        self.btn_save_tpl.setProperty("role", "soft-primary")
        self.btn_save_tpl.clicked.connect(self._capture_banner_template)
        ad_l.addWidget(self.btn_save_tpl)
        side.addWidget(ad_box)

        # Zone delete/clear buttons
        z_btns = QHBoxLayout()
        z_btns.setSpacing(8)
        btn_del = QPushButton("Удалить")
        btn_del.setIcon(make_icon("trash", "#F87171", 14))
        btn_del.setProperty("role", "ghost-danger")
        btn_del.clicked.connect(self.canvas.delete_selected)
        btn_clear = QPushButton("Очистить все")
        btn_clear.clicked.connect(self.canvas.clear_regions)
        z_btns.addWidget(btn_del)
        z_btns.addWidget(btn_clear)
        side.addLayout(z_btns)
        side.addStretch()

        side_scroll.setWidget(side_inner)
        sc_outer.addWidget(side_scroll)
        mid.addWidget(side_card)
        root.addLayout(mid, 1)

        # Bottom Save / Cancel bar
        bottom = QHBoxLayout()
        bottom.setSpacing(10)
        hint = QLabel("ЛКМ — нарисовать зону  ·  Потянуть — переместить  ·  Углы — размер  ·  Delete — удалить")
        hint.setProperty("caption", "true")
        bottom.addWidget(hint)
        bottom.addStretch()

        btn_cancel = QPushButton("Отмена")
        btn_cancel.clicked.connect(self.reject)
        btn_save = QPushButton("Сохранить")
        btn_save.setIcon(make_icon("check", "#FFFFFF", 16))
        btn_save.setProperty("role", "primary")
        btn_save.clicked.connect(self._on_accept)
        bottom.addWidget(btn_cancel)
        bottom.addWidget(btn_save)
        root.addLayout(bottom)

    # ------------------------------------------------------------------ Scene Presets (#4)
    def _refresh_scene_combo(self) -> None:
        self.combo_scene.blockSignals(True)
        self.combo_scene.clear()
        for name in self._presets.keys():
            self.combo_scene.addItem(name)
        idx = self.combo_scene.findText(self._active_preset)
        if idx >= 0:
            self.combo_scene.setCurrentIndex(idx)
        self.combo_scene.blockSignals(False)

    def _on_scene_changed(self, name: str) -> None:
        if not name or name == self._active_preset or name not in self._presets:
            return
        self._presets[self._active_preset] = self.canvas.get_regions()
        self._active_preset = name
        self.canvas.set_regions(self._presets[name])

    def _add_scene_preset(self) -> None:
        name, ok = QInputDialog.getText(self, "Новая сцена", "Название пресета (напр. Just Chatting или Игра):")
        name = name.strip()
        if not ok or not name:
            return
        self._presets[self._active_preset] = self.canvas.get_regions()
        if name not in self._presets:
            self._presets[name] = []
        self._active_preset = name
        self._refresh_scene_combo()
        self.canvas.set_regions(self._presets[name])

    def _delete_scene_preset(self) -> None:
        if len(self._presets) <= 1:
            QMessageBox.information(self, "Сцены", "Нельзя удалить последнюю сцену.")
            return
        del self._presets[self._active_preset]
        self._active_preset = next(iter(self._presets))
        self._refresh_scene_combo()
        self.canvas.set_regions(self._presets[self._active_preset])

    # ------------------------------------------------------------------ Frame Loading
    def canvas_preview_toggled(self, checked: bool) -> None:
        self.canvas.set_preview_enabled(checked)

    def _start_frame_loader(self, mode: str, target, ts: float = 10.0) -> None:
        if self._loader and self._loader.isRunning():
            return
        self.lbl_status.setText("Загрузка кадра...")
        self.btn_live_snap.setEnabled(False)
        self.btn_vod_snap.setEnabled(False)
        self._loader = FrameLoaderWorker(mode, target, self._temp_jpg, ts, self.quality, self.proxy_url, self)
        self._loader.loaded.connect(self._on_frame_loaded)
        self._loader.start()

    def _on_frame_loaded(self, ok: bool, path: str, msg: str) -> None:
        self.btn_live_snap.setEnabled(True)
        self.btn_vod_snap.setEnabled(True)
        if ok and path and os.path.exists(path):
            self.canvas.load_image(path)
        self.lbl_status.setText(msg)

    def _capture_live(self) -> None:
        if self.channel:
            self._start_frame_loader("live", self.channel)

    def _capture_vod(self) -> None:
        if self.vod_url:
            ts = float(self.slider_time.value()) if self.timeline_bar.isVisible() else 30.0
            self.timeline_bar.setVisible(True)
            if self.slider_time.maximum() <= 100:
                self.slider_time.setRange(0, 7200)
                self.slider_time.setValue(int(ts))
            self._start_frame_loader("vod", self.vod_url, ts)

    def _init_video_file(self, path: str) -> None:
        self.video_path = path
        ext = os.path.splitext(path)[1].lower()
        if ext in (".png", ".jpg", ".jpeg", ".bmp", ".webp"):
            self.timeline_bar.setVisible(False)
            if self.canvas.load_image(path):
                self.lbl_status.setText(f"Открыто: {os.path.basename(path)}")
            return
        meta = probe_video(path)
        dur = max(1.0, meta.get("duration", 0.0))
        self._video_duration = dur
        self.timeline_bar.setVisible(True)
        self.slider_time.setRange(0, int(dur))
        init_ts = min(15, int(dur * 0.1))
        self.slider_time.setValue(init_ts)
        self.lbl_time.setText(f"{format_duration(init_ts)} / {format_duration(dur)}")
        self._start_frame_loader("file", path, float(init_ts))

    def _pick_file_for_frame(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Выберите видео или скриншот стрима",
            "",
            "Видео и изображения (*.mp4 *.mkv *.ts *.flv *.mov *.png *.jpg *.jpeg *.webp);;Все файлы (*)",
        )
        if path:
            self._init_video_file(path)

    def _pick_overlay_image(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Выберите логотип или картинку (PNG/JPG)", "", "Изображения (*.png *.jpg *.jpeg *.webp)"
        )
        if path:
            self.edit_img_path.setText(path)
            self._apply_controls_to_selected()

    def _on_timeline_moved(self, val: int) -> None:
        if self._video_duration > 0:
            self.lbl_time.setText(f"{format_duration(val)} / {format_duration(self._video_duration)}")
        else:
            self.lbl_time.setText(format_duration(val))

    def _on_timeline_released(self) -> None:
        ts = float(self.slider_time.value())
        if self.video_path and os.path.exists(self.video_path):
            self._start_frame_loader("file", self.video_path, ts)
        elif self.vod_url:
            self._start_frame_loader("vod", self.vod_url, ts)

    # ------------------------------------------------------------------ Region Controls
    def _refresh_region_list(self) -> None:
        regions = self.canvas.get_regions()
        sel = self.canvas.selected_index()
        self._updating_controls = True
        self.list_regions.clear()
        mode_names = {"blur": "Блюр", "pixelate": "Пиксели", "solid": "Чёрный", "image": "Лого"}
        for i, r in enumerate(regions):
            ai = " [AI]" if r.auto_detect else ""
            txt = f"#{i + 1}  {r.label}{ai}  ·  {mode_names.get(r.mode, r.mode)}"
            self.list_regions.addItem(QListWidgetItem(txt))
        if 0 <= sel < len(regions):
            self.list_regions.setCurrentRow(sel)
        self._updating_controls = False
        self._sync_controls_from_selected()

    def _on_canvas_selection(self, idx: int) -> None:
        self._updating_controls = True
        if 0 <= idx < self.list_regions.count():
            self.list_regions.setCurrentRow(idx)
        self._updating_controls = False
        self._sync_controls_from_selected()

    def _on_list_selection(self, row: int) -> None:
        if not self._updating_controls:
            self.canvas.select_index(row)

    def _sync_controls_from_selected(self) -> None:
        idx = self.canvas.selected_index()
        regions = self.canvas.get_regions()
        self._updating_controls = True
        if 0 <= idx < len(regions):
            r = regions[idx]
            self.edit_label.setEnabled(True)
            self.edit_label.setText(r.label)
            m_idx = self.combo_mode.findData(r.mode)
            if m_idx >= 0:
                self.combo_mode.setCurrentIndex(m_idx)
            self.image_row_widget.setVisible(r.mode == "image")
            self.edit_img_path.setText(r.image_path)
            self.slider_strength.setEnabled(r.mode in ("blur", "pixelate"))
            self.slider_strength.setValue(r.strength)
            self.lbl_strength_val.setText(str(r.strength))
            self.slider_feather.setEnabled(r.mode in ("blur", "pixelate"))
            self.slider_feather.setValue(r.feather)
            self.lbl_feather_val.setText(f"{r.feather} px")
            self.edit_t_start.setText("" if r.start_sec is None else str(int(r.start_sec)))
            self.edit_t_end.setText("" if r.end_sec is None else str(int(r.end_sec)))
            self.sw_auto_detect.setChecked(r.auto_detect)
            if r.template_path and os.path.exists(r.template_path):
                self.lbl_tpl_status.setText(f"Образец сохранён: {Path(r.template_path).name}")
            else:
                self.lbl_tpl_status.setText("Блюрит зону только когда баннер виден в кадре")
        else:
            self.edit_label.setEnabled(False)
            self.edit_label.setText("")
            self.image_row_widget.setVisible(False)
        self._updating_controls = False

    def _parse_opt_float(self, txt: str) -> Optional[float]:
        s = txt.strip().replace(",", ".")
        if not s:
            return None
        try:
            # Support MM:SS or plain seconds
            if ":" in s:
                parts = [float(x) for x in s.split(":")]
                if len(parts) == 2:
                    return parts[0] * 60 + parts[1]
                if len(parts) == 3:
                    return parts[0] * 3600 + parts[1] * 60 + parts[2]
            return float(s)
        except Exception:
            return None

    def _apply_controls_to_selected(self) -> None:
        if self._updating_controls:
            return
        mode = self.combo_mode.currentData() or "blur"
        strength = self.slider_strength.value()
        feather = self.slider_feather.value()
        self.lbl_strength_val.setText(str(strength))
        self.lbl_feather_val.setText(f"{feather} px")
        self.image_row_widget.setVisible(mode == "image")
        self.slider_strength.setEnabled(mode in ("blur", "pixelate"))
        self.slider_feather.setEnabled(mode in ("blur", "pixelate"))
        self.canvas.set_default_style(mode, strength)

        idx = self.canvas.selected_index()
        regions = self.canvas.get_regions()
        if 0 <= idx < len(regions):
            r = regions[idx]
            r.label = self.edit_label.text().strip() or f"Баннер {idx + 1}"
            r.mode = mode
            r.strength = strength
            r.feather = feather
            r.image_path = self.edit_img_path.text().strip()
            r.start_sec = self._parse_opt_float(self.edit_t_start.text())
            r.end_sec = self._parse_opt_float(self.edit_t_end.text())
            r.auto_detect = self.sw_auto_detect.isChecked()
            self.canvas.update_region_props(idx, r)

    def _on_auto_detect_toggled(self, checked: bool) -> None:
        if self._updating_controls:
            return
        if checked and self.canvas.selected_index() >= 0:
            regions = self.canvas.get_regions()
            r = regions[self.canvas.selected_index()]
            if not (r.template_path and os.path.exists(r.template_path)) and self.canvas.has_image():
                self._capture_banner_template()
                return
        self._apply_controls_to_selected()

    def _capture_banner_template(self) -> None:
        idx = self.canvas.selected_index()
        if idx < 0:
            QMessageBox.information(self, "Авто-детектор", "Сначала выделите прямоугольник баннера на кадре.")
            return
        if not self.canvas.has_image():
            QMessageBox.information(self, "Авто-детектор", "Сначала загрузите кадр со стрима или видео.")
            return
        tpl_path = self.canvas.save_region_template(idx)
        if tpl_path:
            self._updating_controls = True
            self.sw_auto_detect.setChecked(True)
            self._updating_controls = False
            self.lbl_tpl_status.setText(f"Образец сохранён: {Path(tpl_path).name}")
            self._apply_controls_to_selected()

    def _on_accept(self) -> None:
        self._presets[self._active_preset] = self.canvas.get_regions()
        self.accept()

    def get_regions(self) -> List[BlurRegion]:
        return deepcopy(self._presets[self._active_preset])

    def get_presets_result(self) -> tuple[Dict[str, List[BlurRegion]], str]:
        return deepcopy(self._presets), self._active_preset

    def closeEvent(self, event) -> None:
        if self._loader and self._loader.isRunning():
            try:
                self._loader.loaded.disconnect()
            except Exception:
                pass
            self._loader.setParent(None)
            self._loader.finished.connect(self._loader.deleteLater)
        if os.path.exists(self._temp_jpg):
            try:
                os.remove(self._temp_jpg)
            except Exception:
                pass
        super().closeEvent(event)
