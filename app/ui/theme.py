from __future__ import annotations

from pathlib import Path

from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication

ASSETS_DIR = Path(__file__).resolve().parent / "assets"


def pick_ui_font_family() -> str:
    fams = set(QFontDatabase.families())
    for candidate in ("Segoe UI Variable Text", "Segoe UI Variable", "Segoe UI"):
        if candidate in fams:
            return candidate
    return "Segoe UI"


def pick_mono_font_family() -> str:
    fams = set(QFontDatabase.families())
    for candidate in ("Cascadia Mono", "Consolas", "Courier New"):
        if candidate in fams:
            return candidate
    return "Consolas"


def apply_app_theme(app: QApplication) -> None:
    ui_font = pick_ui_font_family()
    mono_font = pick_mono_font_family()

    font = QFont(ui_font)
    font.setPointSize(10)
    font.setStyleStrategy(QFont.PreferAntialias)
    app.setFont(font)

    chevron_url = (ASSETS_DIR / "chevron_down.svg").as_posix()

    qss = f"""
    * {{
        font-family: "{ui_font}", "Segoe UI", sans-serif;
        outline: none;
    }}

    QMainWindow, QDialog {{
        background-color: #0F1014;
        color: #E8E9EE;
    }}

    QWidget {{
        color: #E8E9EE;
    }}

    /* ---------- Sidebar ---------- */
    QFrame#Sidebar {{
        background-color: #14151B;
        border-right: 1px solid #222531;
    }}

    QPushButton[nav="true"] {{
        background-color: transparent;
        color: #9CA0B0;
        border: none;
        border-radius: 8px;
        padding: 10px 14px;
        text-align: left;
        font-size: 10pt;
        font-weight: 500;
    }}

    QPushButton[nav="true"]:hover {{
        background-color: #1D1F29;
        color: #F4F5F8;
    }}

    QPushButton[nav="true"][active="true"] {{
        background-color: rgba(139, 92, 246, 0.16);
        color: #C4B5FD;
        font-weight: 600;
    }}

    /* ---------- Cards & Surfaces ---------- */
    QFrame[card="true"] {{
        background-color: #181A22;
        border: 1px solid #252836;
        border-radius: 12px;
    }}

    QFrame[card="true"][hoverable="true"]:hover {{
        border-color: #383D52;
    }}

    QFrame[card="true"][recording="true"] {{
        background-color: #1B1720;
        border: 1px solid rgba(239, 68, 68, 0.55);
    }}

    QFrame[surface="subtle"] {{
        background-color: #13141A;
        border: 1px solid #222531;
        border-radius: 10px;
    }}

    QScrollArea {{
        background-color: transparent;
        border: none;
    }}

    QWidget#ScrollContent {{
        background-color: transparent;
    }}

    /* ---------- Typography helpers ---------- */
    QLabel[h1="true"] {{
        font-size: 16pt;
        font-weight: 700;
        color: #F5F6FA;
    }}

    QLabel[h2="true"] {{
        font-size: 11.5pt;
        font-weight: 600;
        color: #F0F1F5;
    }}

    QLabel[muted="true"] {{
        color: #8E92A4;
        font-size: 9.5pt;
    }}

    QLabel[caption="true"] {{
        color: #7D8194;
        font-size: 8.5pt;
    }}

    QLabel[mono="true"] {{
        font-family: "{mono_font}", "Consolas", monospace;
        font-size: 9.5pt;
    }}

    /* ---------- Inputs ---------- */
    QLineEdit, QSpinBox, QComboBox {{
        background-color: #111218;
        border: 1px solid #2A2E3D;
        border-radius: 8px;
        padding: 7px 12px;
        color: #F0F1F5;
        font-size: 10pt;
        min-height: 22px;
        selection-background-color: #8B5CF6;
    }}

    QLineEdit:hover, QSpinBox:hover, QComboBox:hover {{
        border-color: #3D4257;
    }}

    QLineEdit:focus, QSpinBox:focus, QComboBox:focus {{
        border: 1px solid #8B5CF6;
        background-color: #13141C;
    }}

    QLineEdit::placeholder {{
        color: #676B7E;
    }}

    QComboBox {{
        padding-right: 28px;
    }}

    QComboBox::drop-down {{
        subcontrol-origin: padding;
        subcontrol-position: top right;
        width: 28px;
        border: none;
    }}

    QComboBox::down-arrow {{
        image: url("{chevron_url}");
        width: 14px;
        height: 14px;
    }}

    QComboBox QAbstractItemView {{
        background-color: #181A22;
        border: 1px solid #323648;
        border-radius: 8px;
        padding: 4px;
        selection-background-color: #8B5CF6;
        selection-color: #FFFFFF;
        color: #E8E9EE;
    }}

    QComboBox QAbstractItemView::item {{
        min-height: 30px;
        padding: 4px 10px;
        border-radius: 5px;
    }}

    QSpinBox::up-button, QSpinBox::down-button {{
        background-color: transparent;
        border: none;
        width: 20px;
    }}

    /* ---------- Buttons ---------- */
    QPushButton {{
        background-color: #232633;
        color: #E8E9EE;
        border: 1px solid #313547;
        border-radius: 8px;
        padding: 8px 14px;
        font-size: 9.5pt;
        font-weight: 600;
    }}

    QPushButton:hover {{
        background-color: #2C3040;
        border-color: #444961;
    }}

    QPushButton:pressed {{
        background-color: #1D202B;
    }}

    QPushButton:disabled {{
        background-color: #191B23;
        color: #5C6072;
        border-color: #242733;
    }}

    QPushButton[role="primary"] {{
        background-color: #8B5CF6;
        border: 1px solid #8B5CF6;
        color: #FFFFFF;
    }}

    QPushButton[role="primary"]:hover {{
        background-color: #7C3AED;
        border-color: #7C3AED;
    }}

    QPushButton[role="primary"]:pressed {{
        background-color: #6D28D9;
    }}

    QPushButton[role="soft-primary"] {{
        background-color: rgba(139, 92, 246, 0.14);
        border: 1px solid rgba(139, 92, 246, 0.35);
        color: #C4B5FD;
    }}

    QPushButton[role="soft-primary"]:hover {{
        background-color: rgba(139, 92, 246, 0.24);
        border-color: rgba(139, 92, 246, 0.6);
        color: #FFFFFF;
    }}

    QPushButton[role="success"] {{
        background-color: #16A34A;
        border: 1px solid #16A34A;
        color: #FFFFFF;
    }}

    QPushButton[role="success"]:hover {{
        background-color: #15803D;
        border-color: #15803D;
    }}

    QPushButton[role="danger"] {{
        background-color: #DC2626;
        border: 1px solid #DC2626;
        color: #FFFFFF;
    }}

    QPushButton[role="danger"]:hover {{
        background-color: #B91C1C;
        border-color: #B91C1C;
    }}

    QPushButton[role="ghost"] {{
        background-color: transparent;
        border: 1px solid transparent;
        color: #8E92A4;
        padding: 6px;
    }}

    QPushButton[role="ghost"]:hover {{
        background-color: #252836;
        color: #F4F5F8;
    }}

    QPushButton[role="ghost-danger"] {{
        background-color: transparent;
        border: 1px solid transparent;
        color: #8E92A4;
        padding: 6px;
    }}

    QPushButton[role="ghost-danger"]:hover {{
        background-color: rgba(239, 68, 68, 0.15);
        color: #F87171;
    }}

    /* ---------- List & Logs ---------- */
    QListWidget {{
        background-color: #111218;
        border: 1px solid #262936;
        border-radius: 8px;
        padding: 4px;
        outline: none;
    }}

    QListWidget::item {{
        padding: 8px 10px;
        border-radius: 6px;
        margin: 1px 0;
    }}

    QListWidget::item:selected {{
        background-color: rgba(139, 92, 246, 0.22);
        color: #FFFFFF;
    }}

    QListWidget::item:hover:!selected {{
        background-color: #1C1F29;
    }}

    QPlainTextEdit#LogConsole {{
        background-color: #0D0E12;
        border: 1px solid #222531;
        border-radius: 10px;
        padding: 12px;
        color: #C7CBD9;
        font-family: "{mono_font}", "Consolas", monospace;
        font-size: 9.5pt;
        selection-background-color: #8B5CF6;
    }}

    /* ---------- Progress Bar ---------- */
    QProgressBar {{
        background-color: #111218;
        border: 1px solid #262936;
        border-radius: 6px;
        text-align: center;
        color: #FFFFFF;
        font-size: 9pt;
        font-weight: 600;
        min-height: 18px;
        max-height: 18px;
    }}

    QProgressBar::chunk {{
        background-color: #8B5CF6;
        border-radius: 5px;
    }}

    /* ---------- Sliders ---------- */
    QSlider::groove:horizontal {{
        height: 6px;
        background: #252836;
        border-radius: 3px;
    }}

    QSlider::sub-page:horizontal {{
        background: #8B5CF6;
        border-radius: 3px;
    }}

    QSlider::handle:horizontal {{
        background: #FFFFFF;
        border: 2px solid #8B5CF6;
        width: 14px;
        height: 14px;
        margin: -5px 0;
        border-radius: 9px;
    }}

    /* ---------- Scrollbars ---------- */
    QScrollBar:vertical {{
        background: transparent;
        width: 8px;
        margin: 4px 0;
    }}

    QScrollBar::handle:vertical {{
        background: #2E3242;
        border-radius: 4px;
        min-height: 28px;
    }}

    QScrollBar::handle:vertical:hover {{
        background: #444A61;
    }}

    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
        height: 0;
    }}

    QScrollBar:horizontal {{
        background: transparent;
        height: 8px;
    }}

    QScrollBar::handle:horizontal {{
        background: #2E3242;
        border-radius: 4px;
    }}

    QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
        width: 0;
    }}

    /* ---------- CheckBox ---------- */
    QCheckBox {{
        spacing: 8px;
        font-size: 9.5pt;
    }}

    QCheckBox::indicator {{
        width: 18px;
        height: 18px;
        border-radius: 5px;
        border: 1px solid #3D4257;
        background-color: #111218;
    }}

    QCheckBox::indicator:checked {{
        background-color: #8B5CF6;
        border-color: #8B5CF6;
    }}

    /* ---------- Tooltips & MessageBoxes ---------- */
    QToolTip {{
        background-color: #1E202B;
        color: #F4F5F8;
        border: 1px solid #35394B;
        border-radius: 6px;
        padding: 6px 10px;
        font-size: 9pt;
    }}
    """
    app.setStyleSheet(qss)
