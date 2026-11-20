# -*- mode: python ; coding: utf-8 -*-
"""
Cross-platform PyInstaller spec for Twitch Recorder Pro
- Windows: builds dist/TwitchRecorderPro/TwitchRecorderPro.exe (standalone, no console)
- macOS:   builds dist/TwitchRecorderPro.app (native Retina macOS bundle)
"""

import os
import re
import shutil
import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_all, collect_submodules

ROOT = Path(SPECPATH).resolve()
IS_MAC = sys.platform == "darwin"
IS_WIN = os.name == "nt"

# Single source of truth for version from app/config.py
_cfg_src = (ROOT / "app" / "config.py").read_text(encoding="utf-8")
_ver_m = re.search(r'^APP_VERSION\s*=\s*"([^"]+)"', _cfg_src, re.MULTILINE)
APP_VERSION = _ver_m.group(1) if _ver_m else "2.1.0"

# Locate ffmpeg & ffprobe to bundle into bin/ for 100% standalone portability
extra_binaries = []

def _find_bin(name: str):
    # Prefer a local static binary placed in ./bin/ (recommended for portable macOS builds)
    local_bin = ROOT / "bin" / (f"{name}.exe" if IS_WIN else name)
    if local_bin.is_file():
        return str(local_bin)
    p = shutil.which(name)
    if p:
        return p
    if IS_WIN:
        local_app = os.environ.get("LOCALAPPDATA", "")
        if local_app:
            for cand in (Path(local_app) / "Microsoft" / "WinGet" / "Packages").glob("Gyan.FFmpeg*"):
                for found in cand.rglob(f"{name}.exe"):
                    return str(found)
    elif IS_MAC:
        for d in ("/opt/homebrew/bin", "/usr/local/bin"):
            cand = Path(d) / name
            if cand.is_file():
                return str(cand)
    return None

for tool_name in ("ffmpeg", "ffprobe"):
    tool_path = _find_bin(tool_name)
    if tool_path:
        extra_binaries.append((tool_path, "bin"))

# Collect streamlink + plugins + yt-dlp + faster_whisper cleanly
sl_datas, sl_bins, sl_hidden = collect_all("streamlink")
slc_datas, slc_bins, slc_hidden = collect_all("streamlink_cli")
yt_datas, yt_bins, yt_hidden = collect_all("yt_dlp")
fw_datas, fw_bins, fw_hidden = collect_all("faster_whisper")

datas = [
    (str(ROOT / "web"), "web"),
    (str(ROOT / "assets"), "assets"),
    (str(ROOT / "CHANGELOG.md"), "."),
    *sl_datas,
    *slc_datas,
    *yt_datas,
    *fw_datas,
]

binaries = [
    *extra_binaries,
    *sl_bins,
    *slc_bins,
    *yt_bins,
    *fw_bins,
]

hiddenimports = list(
    set(
        sl_hidden
        + slc_hidden
        + yt_hidden
        + fw_hidden
        + collect_submodules("streamlink.plugins")
        + [
            "PySide6.QtWebEngineWidgets",
            "PySide6.QtWebEngineCore",
            "PySide6.QtWebChannel",
            "PySide6.QtSvg",
            "cv2",
            "numpy",
            "psutil",
            "PIL",
            "ctranslate2",
            "onnxruntime",
        ]
    )
)

icon_file = None
if IS_WIN and (ROOT / "assets" / "app_icon.ico").exists():
    icon_file = str(ROOT / "assets" / "app_icon.ico")
elif IS_MAC and (ROOT / "assets" / "app_icon.icns").exists():
    icon_file = str(ROOT / "assets" / "app_icon.icns")

a = Analysis(
    [str(ROOT / "main.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "scipy", "pandas", "pytest"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="TwitchRecorderPro",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=icon_file,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="TwitchRecorderPro",
)

if IS_MAC:
    app = BUNDLE(
        coll,
        name="TwitchRecorderPro.app",
        icon=icon_file,
        bundle_identifier="com.twitchrecorder.pro",
        info_plist={
            "CFBundleName": "Twitch Recorder Pro",
            "CFBundleDisplayName": "Twitch Recorder Pro",
            "CFBundleShortVersionString": APP_VERSION,
            "CFBundleVersion": APP_VERSION,
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "12.0",
            "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True, "NSAllowsArbitraryLoads": True},
        },
    )
