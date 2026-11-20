from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from app.config import BlurRegion

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

if sys.platform == "darwin" and not os.environ.get("SSL_CERT_FILE"):
    for _ca_cand in ("/etc/ssl/cert.pem", "/private/etc/ssl/cert.pem"):
        if os.path.isfile(_ca_cand):
            os.environ["SSL_CERT_FILE"] = _ca_cand
            break


def _extra_search_dirs() -> List[Path]:
    dirs: List[Path] = []
    # 1. Bundled bin/ inside PyInstaller bundle or next to executable
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        dirs.append(exe_dir / "bin")
        dirs.append(exe_dir)
        if hasattr(sys, "_MEIPASS"):
            meipass = Path(sys._MEIPASS)
            dirs.append(meipass / "bin")
            dirs.append(meipass)
    else:
        exe_dir = Path(sys.executable).resolve().parent
        dirs.append(exe_dir)
        dirs.append(exe_dir / "Scripts")
        dirs.append(exe_dir / "bin")

    # 2. macOS Homebrew & standard Unix paths
    if sys.platform == "darwin":
        dirs.extend([Path("/opt/homebrew/bin"), Path("/usr/local/bin")])

    # 3. Windows WinGet paths
    local_app = os.environ.get("LOCALAPPDATA", "")
    if local_app:
        dirs.append(Path(local_app) / "Microsoft" / "WinGet" / "Links")
        winget_pkgs = Path(local_app) / "Microsoft" / "WinGet" / "Packages"
        if winget_pkgs.exists():
            for pkg in winget_pkgs.glob("Gyan.FFmpeg*"):
                for bin_dir in pkg.rglob("bin"):
                    if bin_dir.is_dir():
                        dirs.append(bin_dir)
    return dirs


def find_executable(name: str) -> Optional[str]:
    candidates = [f"{name}.exe", name] if os.name == "nt" else [name]
    for d in _extra_search_dirs():
        for cand in candidates:
            p = d / cand
            if p.is_file():
                return str(p)
    return shutil.which(name)


def get_ffmpeg_path() -> Optional[str]:
    return find_executable("ffmpeg")


def get_ffprobe_path() -> Optional[str]:
    return find_executable("ffprobe")


def get_streamlink_path() -> Optional[str]:
    if getattr(sys, "frozen", False):
        return sys.executable
    return find_executable("streamlink")


def get_ytdlp_path() -> Optional[str]:
    if getattr(sys, "frozen", False):
        return sys.executable
    return find_executable("yt-dlp")


def get_streamlink_cmd() -> Optional[List[str]]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "--internal-streamlink"]
    p = find_executable("streamlink")
    return [p] if p else None


def get_ytdlp_cmd() -> Optional[List[str]]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "--internal-ytdlp"]
    p = find_executable("yt-dlp")
    return [p] if p else None


_CACHED_ENCODERS: Optional[List[str]] = None


def detect_available_encoders() -> List[str]:
    global _CACHED_ENCODERS
    if _CACHED_ENCODERS is not None:
        return list(_CACHED_ENCODERS)

    ffmpeg = get_ffmpeg_path()
    available: List[str] = []
    if not ffmpeg:
        return ["libx264"]

    try:
        res = subprocess.run(
            [ffmpeg, "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            timeout=8,
            creationflags=CREATE_NO_WINDOW,
        )
        out = res.stdout or ""
    except Exception:
        out = ""

    hw_candidates = (
        ("h264_videotoolbox", "hevc_videotoolbox")
        if sys.platform == "darwin"
        else ("h264_nvenc", "hevc_nvenc", "h264_qsv", "h264_amf")
    )
    for enc in hw_candidates:
        if enc in out:
            try:
                test = subprocess.run(
                    [
                        ffmpeg, "-hide_banner", "-loglevel", "error",
                        "-f", "lavfi", "-i", "color=c=black:s=256x256:d=0.1",
                        "-frames:v", "1", "-c:v", enc, "-f", "null", "-",
                    ],
                    capture_output=True,
                    timeout=8,
                    creationflags=CREATE_NO_WINDOW,
                )
                if test.returncode == 0:
                    available.append(enc)
            except Exception:
                pass

    available.append("libx264")
    _CACHED_ENCODERS = available
    return list(available)


def resolve_encoder(preferred: str) -> str:
    avail = detect_available_encoders()
    if preferred and preferred != "auto" and preferred in avail:
        return preferred
    return avail[0] if avail else "libx264"


def escape_ffmpeg_filter_path(path: str) -> str:
    """Escape a filesystem path for use inside single-quoted FFmpeg filter args (e.g. ass='...')."""
    s = os.path.abspath(path).replace("\\", "/")
    # Escape characters special to FFmpeg filtergraph & single-quoted options
    for ch, rep in (
        (":", r"\:"),
        ("[", r"\["),
        ("]", r"\]"),
        (",", r"\,"),
        (";", r"\;"),
        ("'", r"'\\\''"),
    ):
        s = s.replace(ch, rep)
    return s


def encoder_ffmpeg_args(encoder: str, quality: int = 20, target_bitrate_kbps: Optional[int] = None) -> List[str]:
    q = max(14, min(35, int(quality)))
    if target_bitrate_kbps and target_bitrate_kbps > 100:
        br = f"{int(target_bitrate_kbps)}k"
        max_br = f"{int(target_bitrate_kbps * 1.35)}k"
        buf_br = f"{int(target_bitrate_kbps * 2)}k"
        if encoder == "h264_videotoolbox":
            return [
                "-c:v", "h264_videotoolbox", "-profile:v", "high", "-coder", "cabac",
                "-spatial_aq", "1", "-b:v", br, "-maxrate", max_br, "-bufsize", buf_br,
                "-allow_sw", "1", "-pix_fmt", "yuv420p",
            ]
        if encoder == "hevc_videotoolbox":
            return [
                "-c:v", "hevc_videotoolbox", "-spatial_aq", "1",
                "-b:v", br, "-maxrate", max_br, "-bufsize", buf_br,
                "-allow_sw", "1", "-tag:v", "hvc1", "-pix_fmt", "yuv420p",
            ]
        if encoder == "h264_nvenc":
            return [
                "-c:v", "h264_nvenc", "-preset", "p5", "-tune", "hq", "-profile:v", "high",
                "-spatial-aq", "1", "-b:v", br, "-maxrate", max_br, "-bufsize", buf_br, "-pix_fmt", "yuv420p",
            ]
        if encoder == "hevc_nvenc":
            return [
                "-c:v", "hevc_nvenc", "-preset", "p5", "-tune", "hq",
                "-spatial-aq", "1", "-b:v", br, "-maxrate", max_br, "-bufsize", buf_br, "-tag:v", "hvc1", "-pix_fmt", "yuv420p",
            ]
        if encoder == "h264_qsv":
            return ["-c:v", "h264_qsv", "-preset", "medium", "-b:v", br, "-maxrate", max_br, "-bufsize", buf_br, "-pix_fmt", "yuv420p"]
        if encoder == "h264_amf":
            return ["-c:v", "h264_amf", "-quality", "quality", "-b:v", br, "-maxrate", max_br, "-bufsize", buf_br, "-pix_fmt", "yuv420p"]
        return ["-c:v", "libx264", "-preset", "faster", "-profile:v", "high", "-b:v", br, "-maxrate", max_br, "-bufsize", buf_br, "-pix_fmt", "yuv420p"]

    if encoder == "h264_videotoolbox":
        # Apple Silicon / Intel macOS hardware encoder:
        # Use high quality (q:v 78 at default q=20, 74 at legacy q=23) + High Profile + CABAC + Spatial AQ
        # so re-encoded 1080p60/1440p60 streams stay visually lossless without generation loss.
        vt_q = max(55, min(92, int(118 - q * 1.9)))
        return [
            "-c:v", "h264_videotoolbox",
            "-q:v", str(vt_q),
            "-profile:v", "high",
            "-coder", "cabac",
            "-spatial_aq", "1",
            "-prio_speed", "0",
            "-maxrate", "22000k",
            "-bufsize", "44000k",
            "-allow_sw", "1",
            "-pix_fmt", "yuv420p",
        ]
    if encoder == "hevc_videotoolbox":
        vt_q = max(58, min(94, int(120 - q * 1.9)))
        return [
            "-c:v", "hevc_videotoolbox",
            "-q:v", str(vt_q),
            "-spatial_aq", "1",
            "-prio_speed", "0",
            "-maxrate", "22000k",
            "-bufsize", "44000k",
            "-allow_sw", "1",
            "-tag:v", "hvc1",
            "-pix_fmt", "yuv420p",
        ]
    if encoder == "h264_nvenc":
        nv_cq = max(14, min(32, q - 2))
        return [
            "-c:v", "h264_nvenc", "-preset", "p5", "-tune", "hq", "-profile:v", "high",
            "-rc", "vbr", "-cq", str(nv_cq), "-b:v", "0", "-maxrate", "25000k", "-bufsize", "50000k",
            "-spatial-aq", "1", "-aq-strength", "8", "-pix_fmt", "yuv420p",
        ]
    if encoder == "hevc_nvenc":
        nv_cq = max(14, min(32, q - 2))
        return [
            "-c:v", "hevc_nvenc", "-preset", "p5", "-tune", "hq",
            "-rc", "vbr", "-cq", str(nv_cq), "-b:v", "0", "-maxrate", "25000k", "-bufsize", "50000k",
            "-spatial-aq", "1", "-tag:v", "hvc1", "-pix_fmt", "yuv420p",
        ]
    if encoder == "h264_qsv":
        return ["-c:v", "h264_qsv", "-global_quality", str(max(15, q - 2)), "-preset", "medium", "-pix_fmt", "yuv420p"]
    if encoder == "h264_amf":
        amf_q = max(15, q - 2)
        return ["-c:v", "h264_amf", "-rc", "cqp", "-qp_i", str(amf_q), "-qp_p", str(amf_q), "-quality", "quality", "-pix_fmt", "yuv420p"]
    return ["-c:v", "libx264", "-preset", "faster", "-profile:v", "high", "-crf", str(max(15, q - 2)), "-pix_fmt", "yuv420p"]


# ------------------------------------------------------------------ #3 Smart OpenCV Banner Auto-Detector
def detect_banner_intervals(
    video_path: str,
    region: BlurRegion,
    sample_fps: float = 1.0,
    progress_cb: Optional[Callable[[float], None]] = None,
) -> List[Tuple[float, float]]:
    """
    Scan video at sample_fps using OpenCV template matching on the region's saved template.
    Returns merged list of (start_sec, end_sec) intervals where the ad banner is visible.
    If region.track_motion is True, searches a wider window (+/-25%) to follow moving banners.
    """
    if not region.template_path or not os.path.exists(region.template_path):
        return []
    try:
        import cv2
        import numpy as np
    except Exception:
        return []

    tpl = cv2.imread(region.template_path)
    if tpl is None or tpl.size == 0:
        return []
    tpl_gray = cv2.cvtColor(tpl, cv2.COLOR_BGR2GRAY)

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return []

    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    step = max(1, int(round(fps / max(0.25, sample_fps))))

    active_times: List[float] = []
    frame_idx = 0
    r = region.clamped()

    while True:
        cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
        ok, frame = cap.read()
        if not ok or frame is None:
            break

        h, w = frame.shape[:2]
        pad_ratio = 0.24 if r.track_motion else 0.04
        pad_x = int(w * pad_ratio)
        pad_y = int(h * pad_ratio)
        x1 = max(0, int(r.x * w) - pad_x)
        y1 = max(0, int(r.y * h) - pad_y)
        x2 = min(w, int((r.x + r.w) * w) + pad_x)
        y2 = min(h, int((r.y + r.h) * h) + pad_y)
        roi = frame[y1:y2, x1:x2]
        if roi.size > 0:
            roi_gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
            target_tw = max(8, int(r.w * w))
            target_th = max(8, int(r.h * h))
            if target_tw <= roi_gray.shape[1] and target_th <= roi_gray.shape[0]:
                resized_tpl = cv2.resize(tpl_gray, (target_tw, target_th), interpolation=cv2.INTER_AREA)
                res = cv2.matchTemplate(roi_gray, resized_tpl, cv2.TM_CCOEFF_NORMED)
                _, max_val, _, _ = cv2.minMaxLoc(res)
                if max_val >= r.detect_threshold:
                    active_times.append(frame_idx / fps)

        frame_idx += step
        if total_frames > 0 and progress_cb:
            progress_cb(min(100.0, (frame_idx / total_frames) * 100.0))
        if total_frames > 0 and frame_idx >= total_frames:
            break

    cap.release()
    if not active_times:
        return []

    gap_tol = max(2.0, (step / fps) * 2.5)
    intervals: List[Tuple[float, float]] = []
    cur_s = max(0.0, active_times[0] - 0.5)
    cur_e = active_times[0] + (step / fps) + 0.5
    for t in active_times[1:]:
        if t <= cur_e + gap_tol:
            cur_e = t + (step / fps) + 0.5
        else:
            intervals.append((round(cur_s, 2), round(cur_e, 2)))
            cur_s = max(0.0, t - 0.5)
            cur_e = t + (step / fps) + 0.5
    intervals.append((round(cur_s, 2), round(cur_e, 2)))
    return intervals


def track_moving_banner_boxes(
    video_path: str,
    region: BlurRegion,
    sample_fps: float = 2.0,
) -> List[Tuple[float, float, float, float]]:
    """
    Track a moving banner's position across a video using OpenCV template matching.
    Returns list of (t_start, t_end, norm_x, norm_y) keyframes where the banner moved.
    """
    if not region.template_path or not os.path.exists(region.template_path):
        return []
    try:
        import cv2
    except Exception:
        return []

    tpl = cv2.imread(region.template_path)
    if tpl is None or tpl.size == 0:
        return []
    tpl_gray = cv2.cvtColor(tpl, cv2.COLOR_BGR2GRAY)
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return []

    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    step = max(1, int(round(fps / max(0.5, sample_fps))))
    dt = step / fps
    r = region.clamped()
    keyframes: List[Tuple[float, float, float, float]] = []
    frame_idx = 0

    while True:
        cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
        ok, frame = cap.read()
        if not ok or frame is None:
            break
        h, w = frame.shape[:2]
        pad_x, pad_y = int(w * 0.30), int(h * 0.30)
        x1 = max(0, int(r.x * w) - pad_x)
        y1 = max(0, int(r.y * h) - pad_y)
        x2 = min(w, int((r.x + r.w) * w) + pad_x)
        y2 = min(h, int((r.y + r.h) * h) + pad_y)
        roi = frame[y1:y2, x1:x2]
        if roi.size > 0:
            roi_gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
            tw, th = max(8, int(r.w * w)), max(8, int(r.h * h))
            if tw <= roi_gray.shape[1] and th <= roi_gray.shape[0]:
                rtpl = cv2.resize(tpl_gray, (tw, th), interpolation=cv2.INTER_AREA)
                res = cv2.matchTemplate(roi_gray, rtpl, cv2.TM_CCOEFF_NORMED)
                _, max_val, _, max_loc = cv2.minMaxLoc(res)
                if max_val >= r.detect_threshold:
                    nx = max(0.0, min(1.0 - r.w, (x1 + max_loc[0]) / w))
                    ny = max(0.0, min(1.0 - r.h, (y1 + max_loc[1]) / h))
                    t_cur = frame_idx / fps
                    if keyframes and abs(keyframes[-1][2] - nx) < 0.012 and abs(keyframes[-1][3] - ny) < 0.012 and t_cur <= keyframes[-1][1] + dt * 1.5:
                        prev = keyframes[-1]
                        keyframes[-1] = (prev[0], round(t_cur + dt, 2), prev[2], prev[3])
                    else:
                        keyframes.append((round(t_cur, 2), round(t_cur + dt, 2), round(nx, 4), round(ny, 4)))
        frame_idx += step
        if total_frames > 0 and frame_idx >= total_frames:
            break
    cap.release()
    return keyframes


# ------------------------------------------------------------------ #1, #2, #5 Supercharged Blur Filter Builder
def _format_enable_expr(
    region: BlurRegion,
    detected_intervals: Optional[List[Tuple[float, float]]] = None,
) -> str:
    if detected_intervals is not None:
        if not detected_intervals:
            return ":enable='0'"
        # Cap to 40 intervals to keep FFmpeg expression fast
        chunks = [f"between(t,{s:.2f},{e:.2f})" for s, e in detected_intervals[:40]]
        return f":enable='{'+'.join(chunks)}'"

    if region.start_sec is not None and region.end_sec is not None:
        return f":enable='between(t,{region.start_sec:.2f},{region.end_sec:.2f})'"
    if region.start_sec is not None:
        return f":enable='gte(t,{region.start_sec:.2f})'"
    if region.end_sec is not None:
        return f":enable='lte(t,{region.end_sec:.2f})'"
    return ""


def build_blur_filter_graph(
    regions: List[BlurRegion],
    detected_map: Optional[Dict[int, List[Tuple[float, float]]]] = None,
    input_video_pad: str = "[0:v]",
) -> Tuple[Optional[str], List[str]]:
    """
    Build a resolution-independent FFmpeg filter_complex string supporting:
      - Gaussian blur (with optional soft feathered edges #1)
      - Pixelate mosaic (with optional soft feathered edges #1)
      - Solid black rectangle
      - Custom Logo / Image overlay (#2)
      - Timed intervals (#5) and OpenCV auto-detected intervals (#3)

    Returns (filter_complex_str_or_None, extra_ffmpeg_input_args).
    Output video pad is always [vout].
    """
    valid: List[Tuple[int, BlurRegion]] = [
        (orig_i, r.clamped())
        for orig_i, r in enumerate(regions)
        if r.w >= 0.005 and r.h >= 0.005
    ]
    if not valid:
        return None, []

    parts: List[str] = []
    extra_inputs: List[str] = []
    next_input_idx = 1  # 0 is main video

    # Regions that need cropped source from video stream
    need_src = [
        (orig_i, r)
        for orig_i, r in valid
        if r.mode in ("blur", "pixelate") or (r.mode in ("image", "video") and not (r.image_path and os.path.exists(r.image_path)))
    ]
    # Image or animated/video overlay regions (need extra -i input + reference size)
    img_regions = [
        (orig_i, r)
        for orig_i, r in valid
        if r.mode in ("image", "video") and r.image_path and os.path.exists(r.image_path)
    ]

    split_targets: List[str] = ["[base]"]
    src_map: Dict[int, str] = {}
    ref_map: Dict[int, str] = {}

    for orig_i, _ in need_src:
        lbl = f"[src{orig_i}]"
        src_map[orig_i] = lbl
        split_targets.append(lbl)
    for orig_i, _ in img_regions:
        lbl = f"[iref{orig_i}]"
        ref_map[orig_i] = lbl
        split_targets.append(lbl)

    current_pad = input_video_pad
    if len(split_targets) > 1:
        parts.append(f"{input_video_pad}split={len(split_targets)}{''.join(split_targets)}")
        current_pad = "[base]"

    for orig_i, r in valid:
        det_iv = detected_map.get(orig_i) if detected_map and orig_i in detected_map else None
        enable_expr = _format_enable_expr(r, det_iv)

        crop_w = f"max(4,trunc(iw*{r.w:.6f}/2)*2)"
        crop_h = f"max(4,trunc(ih*{r.h:.6f}/2)*2)"
        crop_x = f"max(0,min(iw-({crop_w}),trunc(iw*{r.x:.6f}/2)*2))"
        crop_y = f"max(0,min(ih-({crop_h}),trunc(ih*{r.y:.6f}/2)*2))"
        ov_x = f"max(0,min(main_w-overlay_w,trunc(main_w*{r.x:.6f}/2)*2))"
        ov_y = f"max(0,min(main_h-overlay_h,trunc(main_h*{r.y:.6f}/2)*2))"

        # Soft edge feathering filter snippet (#1)
        feather_chain = ""
        if r.feather > 0:
            fpx = max(2, min(50, int(r.feather)))
            feather_chain = (
                f",format=yuva420p,geq=lum='p(X,Y)':cb='p(X,Y)':cr='p(X,Y)':"
                f"a='255*min(1,min(X/{fpx},(W-X)/{fpx}))*min(1,min(Y/{fpx},(H-Y)/{fpx}))'"
            )

        if r.mode == "solid" and r.feather == 0:
            next_pad = f"[ov{orig_i}]"
            parts.append(
                f"{current_pad}drawbox=x='trunc(iw*{r.x:.6f})':y='trunc(ih*{r.y:.6f})':"
                f"w='max(2,trunc(iw*{r.w:.6f}))':h='max(2,trunc(ih*{r.h:.6f}))':"
                f"color=black@1.0:t=fill{enable_expr}{next_pad}"
            )
            current_pad = next_pad
            continue

        if r.mode in ("image", "video") and orig_i in ref_map:
            ext_low = os.path.splitext(r.image_path)[1].lower()
            is_anim = r.mode == "video" or ext_low in (".mp4", ".webm", ".gif", ".mov", ".mkv")
            if is_anim:
                extra_inputs.extend(["-stream_loop", "-1", "-i", r.image_path])
            else:
                extra_inputs.extend(["-i", r.image_path])
            img_idx = next_input_idx
            next_input_idx += 1
            parts.append(
                f"{ref_map[orig_i]}crop=w='{crop_w}':h='{crop_h}':x='{crop_x}':y='{crop_y}'[icrop{orig_i}]"
            )
            ck_filter = ""
            if r.chromakey:
                ck_col = r.chromakey_color or "0x00FF00"
                ck_sim = max(0.05, min(0.70, float(r.chromakey_similarity or 0.25)))
                ck_filter = f",chromakey={ck_col}:{ck_sim:.2f}:0.08"
            parts.append(
                f"[{img_idx}:v]format=rgba{ck_filter}[irgba{orig_i}]"
            )
            parts.append(
                f"[irgba{orig_i}][icrop{orig_i}]scale2ref=w=iw:h=ih:flags=lanczos[fx{orig_i}][idrop{orig_i}]"
            )
            parts.append(f"[idrop{orig_i}]nullsink")
            next_pad = f"[ov{orig_i}]"
            parts.append(
                f"{current_pad}[fx{orig_i}]overlay=x='{ov_x}':y='{ov_y}':shortest=0:eof_action=repeat{enable_expr}{next_pad}"
            )
            current_pad = next_pad
            continue

        # Blur or Pixelate (or feathered solid fallback)
        src_pad = src_map.get(orig_i)
        if not src_pad:
            # Feathered solid
            next_pad = f"[ov{orig_i}]"
            parts.append(
                f"{current_pad}drawbox=x='trunc(iw*{r.x:.6f})':y='trunc(ih*{r.y:.6f})':"
                f"w='max(2,trunc(iw*{r.w:.6f}))':h='max(2,trunc(ih*{r.h:.6f}))':"
                f"color=black@1.0:t=fill{enable_expr}{next_pad}"
            )
            current_pad = next_pad
            continue

        if r.mode == "pixelate":
            factor = max(4, min(48, int(r.strength)))
            parts.append(
                f"{src_pad}crop=w='{crop_w}':h='{crop_h}':x='{crop_x}':y='{crop_y}',"
                f"split=2[p_in{orig_i}][p_ref{orig_i}]"
            )
            parts.append(
                f"[p_in{orig_i}]scale=w='max(2,trunc(iw/{factor}))':h='max(2,trunc(ih/{factor}))':flags=area[p_sm{orig_i}]"
            )
            parts.append(
                f"[p_sm{orig_i}][p_ref{orig_i}]scale2ref=w=iw:h=ih:flags=neighbor[fx_raw{orig_i}][p_dr{orig_i}]"
            )
            parts.append(f"[p_dr{orig_i}]nullsink")
            if feather_chain:
                parts.append(f"[fx_raw{orig_i}]{feather_chain.lstrip(',')}[fx{orig_i}]")
            else:
                parts[-2] = parts[-2].replace(f"[fx_raw{orig_i}]", f"[fx{orig_i}]")
        else:
            sigma = max(2, min(80, int(r.strength)))
            parts.append(
                f"{src_pad}crop=w='{crop_w}':h='{crop_h}':x='{crop_x}':y='{crop_y}',"
                f"gblur=sigma={sigma}{feather_chain}[fx{orig_i}]"
            )

        next_pad = f"[ov{orig_i}]"
        parts.append(
            f"{current_pad}[fx{orig_i}]overlay=x='{ov_x}':y='{ov_y}':shortest=0{enable_expr}{next_pad}"
        )
        current_pad = next_pad

    # Rename final pad to [vout]
    if parts and current_pad != "[vout]":
        parts.append(f"{current_pad}null[vout]")

    return ";".join(parts), extra_inputs


def build_blur_filter_complex(regions: List[BlurRegion]) -> Optional[str]:
    """Legacy wrapper returning only filter string (for realtime streams without external image inputs)."""
    fg, _ = build_blur_filter_graph(regions)
    return fg


# ------------------------------------------------------------------ Video Probing & Frames
def _has_audio_stream(path: str) -> bool:
    ffprobe = get_ffprobe_path()
    if not ffprobe or not os.path.exists(path):
        return True
    try:
        res = subprocess.run(
            [
                ffprobe, "-v", "error", "-select_streams", "a:0",
                "-show_entries", "stream=codec_type", "-of", "csv=p=0", path,
            ],
            capture_output=True, text=True, timeout=6, creationflags=CREATE_NO_WINDOW,
        )
        return "audio" in (res.stdout or "").lower()
    except Exception:
        return True


def probe_video(path: str) -> Dict[str, float]:
    ffprobe = get_ffprobe_path()
    info = {"duration": 0.0, "width": 0.0, "height": 0.0, "size_bytes": 0.0}
    if not os.path.exists(path):
        return info
    info["size_bytes"] = float(os.path.getsize(path))
    if not ffprobe:
        return info
    try:
        res = subprocess.run(
            [
                ffprobe, "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=width,height:format=duration",
                "-of", "json", path,
            ],
            capture_output=True, text=True, timeout=10, creationflags=CREATE_NO_WINDOW,
        )
        data = json.loads(res.stdout or "{}")
        fmt = data.get("format", {})
        if fmt.get("duration"):
            info["duration"] = float(fmt["duration"])
        streams = data.get("streams", [])
        if streams:
            info["width"] = float(streams[0].get("width", 0))
            info["height"] = float(streams[0].get("height", 0))
    except Exception:
        pass
    return info


def extract_frame_from_file(video_path: str, timestamp_sec: float, output_jpg: str) -> Tuple[bool, str]:
    ffmpeg = get_ffmpeg_path()
    if not ffmpeg:
        return False, "FFmpeg не найден"
    ts = max(0.0, float(timestamp_sec))
    try:
        res = subprocess.run(
            [
                ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                "-ss", f"{ts:.2f}", "-i", video_path,
                "-frames:v", "1", "-q:v", "2", output_jpg,
            ],
            capture_output=True, text=True, timeout=25, creationflags=CREATE_NO_WINDOW,
        )
        if res.returncode == 0 and os.path.exists(output_jpg) and os.path.getsize(output_jpg) > 0:
            return True, ""
        return False, (res.stderr or "Не удалось извлечь кадр").strip()
    except Exception as e:
        return False, str(e)


# ------------------------------------------------------------------ #20 Instant Lossless Trimmer
def trim_video_fast(
    input_path: str,
    start_sec: float,
    end_sec: float,
    output_path: str,
    accurate_reencode: bool = False,
    encoder: str = "h264_nvenc",
    quality: int = 23,
) -> Tuple[bool, str]:
    ffmpeg = get_ffmpeg_path()
    if not ffmpeg:
        return False, "FFmpeg не найден"
    dur = max(0.5, float(end_sec) - float(start_sec))
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{max(0.0, start_sec):.3f}", "-i", input_path, "-t", f"{dur:.3f}"]
    if accurate_reencode:
        cmd.extend([*encoder_ffmpeg_args(encoder, quality), "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart"])
    else:
        cmd.extend(["-c", "copy", "-avoid_negative_ts", "make_zero", "-movflags", "+faststart"])
    cmd.append(output_path)
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=600, creationflags=CREATE_NO_WINDOW)
        if res.returncode == 0 and os.path.exists(output_path):
            return True, output_path
        return False, (res.stderr or "Ошибка обрезки").strip()
    except Exception as exc:
        return False, str(exc)


# ------------------------------------------------------------------ #8 Seamless Multi-Part Concat
def concat_videos(
    input_paths: List[str],
    output_path: str,
    normalize_clips: bool = False,
    encoder: str = "h264_nvenc",
    quality: int = 23,
) -> Tuple[bool, str]:
    ffmpeg = get_ffmpeg_path()
    if not ffmpeg:
        return False, "FFmpeg не найден"
    valid = [p for p in input_paths if os.path.exists(p)]
    if len(valid) < 2:
        return False, "Для склейки нужно минимум 2 файла"

    if not normalize_clips:
        list_file = os.path.join(tempfile.gettempdir(), f"twitch_concat_{os.getpid()}_{uuid.uuid4().hex[:8]}.txt")
        try:
            with open(list_file, "w", encoding="utf-8") as f:
                for p in valid:
                    safe = os.path.abspath(p).replace("\\", "/").replace("'", "'\\''")
                    f.write(f"file '{safe}'\n")
            res = subprocess.run(
                [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", list_file, "-c", "copy", "-movflags", "+faststart", output_path],
                capture_output=True, text=True, timeout=600, creationflags=CREATE_NO_WINDOW,
            )
            if res.returncode == 0 and os.path.exists(output_path):
                return True, output_path
            return False, (res.stderr or "Ошибка склейки").strip()
        finally:
            if os.path.exists(list_file):
                try:
                    os.remove(list_file)
                except Exception:
                    pass
    else:
        # Normalize resolution/fps/audio (great for compiling Clips from different streams!)
        cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error"]
        f_parts = []
        cat_in = []
        for idx, p in enumerate(valid):
            cmd.extend(["-i", p])
            f_parts.append(
                f"[{idx}:v]scale=1920:1080:force_original_aspect_ratio=decrease,"
                f"pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=60[v{idx}]"
            )
            # Check if file has an audio stream; if not, synthesize silent stereo audio so concat never fails
            has_audio = _has_audio_stream(p)
            if has_audio:
                f_parts.append(f"[{idx}:a]aresample=48000,aformat=channel_layouts=stereo[a{idx}]")
            else:
                dur = max(0.5, probe_video(p).get("duration", 1.0))
                f_parts.append(f"anullsrc=channel_layout=stereo:sample_rate=48000:d={dur:.2f}[a{idx}]")
            cat_in.append(f"[v{idx}][a{idx}]")
        f_parts.append(f"{''.join(cat_in)}concat=n={len(valid)}:v=1:a=1[vout][aout]")
        cmd.extend([
            "-filter_complex", ";".join(f_parts),
            "-map", "[vout]", "-map", "[aout]",
            *encoder_ffmpeg_args(encoder, quality),
            "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", output_path,
        ])
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=1800, creationflags=CREATE_NO_WINDOW)
        if res.returncode == 0 and os.path.exists(output_path):
            return True, output_path
        return False, (res.stderr or "Ошибка компиляции клипов").strip()


# ------------------------------------------------------------------ #24 Silence & AFK / Static Cut
def detect_active_segments_no_silence_afk(
    video_path: str,
    silence_db: float = -35.0,
    min_silence_sec: float = 3.0,
    remove_freeze: bool = True,
    min_freeze_sec: float = 8.0,
) -> List[Tuple[float, float]]:
    """
    Detect silent and/or frozen (AFK 'Be Right Back') segments using FFmpeg silencedetect + freezedetect.
    Returns list of ACTIVE (keep) intervals [(start, end), ...].
    """
    ffmpeg = get_ffmpeg_path()
    meta = probe_video(video_path)
    total = meta.get("duration", 0.0)
    if not ffmpeg or total <= 1.0:
        return []

    cut_ranges: List[Tuple[float, float]] = []

    # 1. Detect long silences
    try:
        res = subprocess.run(
            [
                ffmpeg, "-hide_banner", "-i", video_path,
                "-af", f"silencedetect=noise={silence_db}dB:d={min_silence_sec}",
                "-f", "null", "-",
            ],
            capture_output=True, text=True, timeout=300, creationflags=CREATE_NO_WINDOW,
        )
        s_start = None
        for line in (res.stderr or "").splitlines():
            m1 = re.search(r"silence_start:\s*([\d.]+)", line)
            if m1:
                s_start = float(m1.group(1))
            m2 = re.search(r"silence_end:\s*([\d.]+)", line)
            if m2 and s_start is not None:
                cut_ranges.append((s_start, float(m2.group(1))))
                s_start = None
        if s_start is not None:
            cut_ranges.append((s_start, total))
    except Exception:
        pass

    # 2. Detect frozen / AFK static screens
    if remove_freeze:
        try:
            res = subprocess.run(
                [
                    ffmpeg, "-hide_banner", "-i", video_path,
                    "-vf", f"freezedetect=n=0.003:d={min_freeze_sec}",
                    "-an", "-f", "null", "-",
                ],
                capture_output=True, text=True, timeout=300, creationflags=CREATE_NO_WINDOW,
            )
            f_start = None
            for line in (res.stderr or "").splitlines():
                m1 = re.search(r"freeze_start:\s*([\d.]+)", line)
                if m1:
                    f_start = float(m1.group(1))
                m2 = re.search(r"freeze_end:\s*([\d.]+)", line)
                if m2 and f_start is not None:
                    cut_ranges.append((f_start, float(m2.group(1))))
                    f_start = None
            if f_start is not None:
                cut_ranges.append((f_start, total))
        except Exception:
            pass

    if not cut_ranges:
        return [(0.0, total)]

    # Merge overlapping cut ranges
    cut_ranges.sort()
    merged_cuts: List[Tuple[float, float]] = [cut_ranges[0]]
    for s, e in cut_ranges[1:]:
        ps, pe = merged_cuts[-1]
        if s <= pe + 0.5:
            merged_cuts[-1] = (ps, max(pe, e))
        else:
            merged_cuts.append((s, e))

    # Invert cuts to obtain KEEP intervals (leave 0.3s breathing padding)
    keeps: List[Tuple[float, float]] = []
    cursor = 0.0
    for cs, ce in merged_cuts:
        ks = cursor
        ke = min(total, cs + 0.3)
        if ke - ks >= 0.8:
            keeps.append((round(ks, 2), round(ke, 2)))
        cursor = max(cursor, ce - 0.3)
    if total - cursor >= 0.8:
        keeps.append((round(cursor, 2), round(total, 2)))
    return keeps


# ------------------------------------------------------------------ #21 Shorts / Reels 9:16 Builder
def build_shorts_9_16_filter(
    cam_region: Optional[BlurRegion],
    game_region: Optional[BlurRegion],
    blur_regions: List[BlurRegion],
    split_ratio: float = 0.36,
    layout: str = "split",        # "split" | "pip_circle" | "pip_rounded" | "blur_bg"
    progress_bar: bool = False,
    total_duration: float = 0.0,
    killcam_zoom: bool = False,
) -> Tuple[str, List[str]]:
    """
    Build a 1080x1920 vertical Shorts/TikTok filter graph:
      - First applies any blur_regions to the full frame [0:v] -> [pre]
      - layout="split": top (split_ratio of 1920) = zoomed webcam, bottom = gameplay
      - layout="pip_circle" / "pip_rounded": full 1080x1920 gameplay with circular/rounded webcam PiP
      - layout="blur_bg" (or no cam_region): blurred background fill + centered crisp gameplay
      - killcam_zoom: smooth 1.35x center zoom on gameplay
      - progress_bar: animated neon bottom progress bar
    """
    parts: List[str] = []
    extra_inputs: List[str] = []

    base_pad = "[0:v]"
    if blur_regions:
        fg, extra_inputs = build_blur_filter_graph(blur_regions, input_video_pad="[0:v]")
        if fg:
            parts.append(fg.replace("[vout]", "[pre]"))
            base_pad = "[pre]"

    gr = game_region.clamped() if game_region else BlurRegion(0.18, 0.0, 0.64, 1.0)
    if killcam_zoom:
        # Zoom in 1.35x toward center of game_region
        zw = max(0.08, gr.w / 1.35)
        zh = max(0.08, gr.h / 1.35)
        zx = max(0.0, min(1.0 - zw, gr.x + (gr.w - zw) / 2.0))
        zy = max(0.0, min(1.0 - zh, gr.y + (gr.h - zh) / 2.0))
        gr = BlurRegion(zx, zy, zw, zh)

    gr_w = f"max(4,trunc(iw*{gr.w:.6f}/2)*2)"
    gr_h = f"max(4,trunc(ih*{gr.h:.6f}/2)*2)"

    if cam_region and layout in ("pip_circle", "pip_rounded"):
        cr = cam_region.clamped()
        cr_w = f"max(4,trunc(iw*{cr.w:.6f}/2)*2)"
        cr_h = f"max(4,trunc(ih*{cr.h:.6f}/2)*2)"
        pip_sz = 380
        parts.append(f"{base_pad}split=2[scam][sgame]")
        parts.append(
            f"[sgame]crop=w='{gr_w}':h='{gr_h}':"
            f"x='max(0,min(iw-({gr_w}),trunc(iw*{gr.x:.6f}/2)*2))':y='max(0,min(ih-({gr_h}),trunc(ih*{gr.y:.6f}/2)*2))',"
            f"scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1[bgv]"
        )
        if layout == "pip_circle":
            r_px = pip_sz // 2
            alpha_expr = f"if(lte((X-{r_px})*(X-{r_px})+(Y-{r_px})*(Y-{r_px}),{r_px - 2}*{r_px - 2}),255,0)"
        else:
            rad = 42
            alpha_expr = (
                f"if(gt(abs(X-{pip_sz/2}),{pip_sz/2 - rad})*gt(abs(Y-{pip_sz/2}),{pip_sz/2 - rad}),"
                f"if(lte((abs(X-{pip_sz/2})-{pip_sz/2 - rad})*(abs(X-{pip_sz/2})-{pip_sz/2 - rad})+"
                f"(abs(Y-{pip_sz/2})-{pip_sz/2 - rad})*(abs(Y-{pip_sz/2})-{pip_sz/2 - rad}),{rad}*{rad}),255,0),255)"
            )
        parts.append(
            f"[scam]crop=w='{cr_w}':h='{cr_h}':"
            f"x='max(0,min(iw-({cr_w}),trunc(iw*{cr.x:.6f}/2)*2))':y='max(0,min(ih-({cr_h}),trunc(ih*{cr.y:.6f}/2)*2))',"
            f"scale={pip_sz}:{pip_sz}:force_original_aspect_ratio=increase,crop={pip_sz}:{pip_sz},"
            f"format=yuva420p,geq=lum='p(X,Y)':cb='p(X,Y)':cr='p(X,Y)':a='{alpha_expr}'[pipv]"
        )
        parts.append("[bgv][pipv]overlay=W-w-44:76,setsar=1[vstage]")
    elif cam_region and layout != "blur_bg":
        cam_h = int(round(1920 * max(0.2, min(0.55, split_ratio)) / 2) * 2)
        game_h = 1920 - cam_h
        cr = cam_region.clamped()
        cr_w = f"max(4,trunc(iw*{cr.w:.6f}/2)*2)"
        cr_h = f"max(4,trunc(ih*{cr.h:.6f}/2)*2)"

        parts.append(f"{base_pad}split=2[scam][sgame]")
        parts.append(
            f"[scam]crop=w='{cr_w}':h='{cr_h}':"
            f"x='max(0,min(iw-({cr_w}),trunc(iw*{cr.x:.6f}/2)*2))':y='max(0,min(ih-({cr_h}),trunc(ih*{cr.y:.6f}/2)*2))',"
            f"scale=1080:{cam_h}:force_original_aspect_ratio=increase,crop=1080:{cam_h},setsar=1[topv]"
        )
        parts.append(
            f"[sgame]crop=w='{gr_w}':h='{gr_h}':"
            f"x='max(0,min(iw-({gr_w}),trunc(iw*{gr.x:.6f}/2)*2))':y='max(0,min(ih-({gr_h}),trunc(ih*{gr.y:.6f}/2)*2))',"
            f"scale=1080:{game_h}:force_original_aspect_ratio=increase,crop=1080:{game_h},setsar=1[botv]"
        )
        parts.append(f"[topv][botv]vstack=inputs=2,drawbox=x=0:y={cam_h - 3}:w=1080:h=6:color=#8B5CF6@0.95:t=fill[vstage]")
    else:
        # Classic sleek Shorts layout: blurred ambient background + sharp centered 16:9 clip
        parts.append(f"{base_pad}split=2[bg][fg]")
        parts.append("[bg]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,gblur=sigma=35[bgb]")
        if killcam_zoom:
            parts.append(
                f"[fg]crop=w='{gr_w}':h='{gr_h}':"
                f"x='max(0,min(iw-({gr_w}),trunc(iw*{gr.x:.6f}/2)*2))':y='max(0,min(ih-({gr_h}),trunc(ih*{gr.y:.6f}/2)*2))',"
                f"scale=1080:-2:force_original_aspect_ratio=decrease[fgs]"
            )
        else:
            parts.append("[fg]scale=1080:-2:force_original_aspect_ratio=decrease[fgs]")
        parts.append("[bgb][fgs]overlay=(W-w)/2:(H-h)/2,setsar=1[vstage]")

    if progress_bar and total_duration > 1.0:
        # Animated bottom progress bar via color source overlay
        dur_f = max(1.0, float(total_duration))
        parts.append(f"color=c=#A855F7:s=1080x12[pbar]")
        parts.append(f"[vstage][pbar]overlay=x='-W+(W*t/{dur_f:.2f})':y=H-12:shortest=1[vout]")
    else:
        parts[-1] = parts[-1].replace("[vstage]", "[vout]")

    return ";".join(parts), extra_inputs


def extract_audio_track(input_path: str, output_path: str, fmt: str = "mp3") -> Tuple[bool, str]:
    """Extract clean audio track (.mp3 or .wav) from a video file."""
    ffmpeg = get_ffmpeg_path()
    if not ffmpeg or not os.path.exists(input_path):
        return False, "FFmpeg или исходный видеофайл не найден"
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", input_path, "-vn"]
    if fmt.lower() == "wav":
        cmd.extend(["-c:a", "pcm_s16le", "-ar", "48000", output_path])
    else:
        cmd.extend(["-c:a", "libmp3lame", "-q:a", "2", output_path])
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=600, creationflags=CREATE_NO_WINDOW)
        if res.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 1000:
            return True, output_path
        # Fallback to AAC .m4a if libmp3lame is not compiled in
        res2 = subprocess.run(
            [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", input_path, "-vn", "-c:a", "aac", "-b:a", "192k", output_path],
            capture_output=True, text=True, timeout=600, creationflags=CREATE_NO_WINDOW,
        )
        if res2.returncode == 0 and os.path.exists(output_path):
            return True, output_path
        return False, (res.stderr or "Не удалось извлечь аудио").strip()
    except Exception as exc:
        return False, str(exc)


def generate_youtube_thumbnail(
    video_or_image_path: str,
    output_jpg: str,
    timestamp_sec: float = 10.0,
    headline: str = "",
    subtitle: str = "",
    accent_color: str = "#8B5CF6",
    add_border: bool = True,
    contrast_boost: float = 1.18,
) -> Tuple[bool, str]:
    """
    Generate a 1280x720 YouTube / Shorts thumbnail using Pillow:
      - Extracts frame from video (or opens image)
      - Enhances contrast & saturation
      - Draws sleek bottom gradient vignette, neon border, and bold stroked headline/subtitle
    """
    from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

    src_path = video_or_image_path
    tmp_jpg = ""
    ext = os.path.splitext(src_path)[1].lower()
    if ext not in (".jpg", ".jpeg", ".png", ".webp", ".bmp"):
        tmp_jpg = os.path.join(tempfile.gettempdir(), f"tr_thumb_src_{uuid.uuid4().hex[:6]}.jpg")
        ok, err = extract_frame_from_file(src_path, timestamp_sec, tmp_jpg)
        if not ok:
            ok, err = extract_frame_from_file(src_path, 0.5, tmp_jpg)
        if not ok:
            return False, err or "Не удалось извлечь кадр из видео"
        src_path = tmp_jpg

    try:
        img = Image.open(src_path).convert("RGB")
        # Cover-crop to 1280x720
        tw, th = 1280, 720
        iw, ih = img.size
        scale = max(tw / max(1, iw), th / max(1, ih))
        nw, nh = int(iw * scale), int(ih * scale)
        img = img.resize((nw, nh), Image.Resampling.LANCZOS)
        left = (nw - tw) // 2
        top = (nh - th) // 2
        img = img.crop((left, top, left + tw, top + th))

        if contrast_boost > 1.0:
            img = ImageEnhance.Contrast(img).enhance(contrast_boost)
            img = ImageEnhance.Color(img).enhance(min(1.4, contrast_boost + 0.05))
            img = ImageEnhance.Sharpness(img).enhance(1.25)

        overlay = Image.new("RGBA", (tw, th), (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)

        # Dark bottom gradient for readable typography
        for y in range(th // 2, th):
            alpha = int(215 * ((y - th / 2) / (th / 2)) ** 1.35)
            draw.line([(0, y), (tw, y)], fill=(8, 9, 15, min(230, alpha)))

        # Parse accent hex color
        hx = (accent_color or "#8B5CF6").lstrip("#")
        if len(hx) != 6:
            hx = "8B5CF6"
        ac_rgb = (int(hx[0:2], 16), int(hx[2:4], 16), int(hx[4:6], 16))

        if add_border:
            draw.rectangle([0, 0, tw - 1, th - 1], outline=(*ac_rgb, 255), width=10)
            draw.rectangle([10, 10, tw - 11, th - 11], outline=(255, 255, 255, 70), width=2)

        def _load_font(sz: int):
            font_candidates = [
                "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
                "/System/Library/Fonts/Helvetica.ttc",
                "C:/Windows/Fonts/arialbd.ttf",
                "C:/Windows/Fonts/segoeuib.ttf",
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            ]
            for fp in font_candidates:
                if os.path.exists(fp):
                    try:
                        return ImageFont.truetype(fp, sz)
                    except Exception:
                        continue
            return ImageFont.load_default()

        if subtitle.strip():
            f_sub = _load_font(36)
            sub_txt = subtitle.strip().upper()
            bbox = draw.textbbox((0, 0), sub_txt, font=f_sub)
            bw, bh = bbox[2] - bbox[0], bbox[3] - bbox[1]
            bx, by = 58, th - (195 if headline.strip() else 110)
            draw.rounded_rectangle([bx - 16, by - 10, bx + bw + 16, by + bh + 14], radius=12, fill=(*ac_rgb, 235))
            draw.text((bx, by), sub_txt, font=f_sub, fill=(255, 255, 255, 255))

        if headline.strip():
            f_head = _load_font(74)
            head_txt = headline.strip().upper()
            hx_pos, hy_pos = 56, th - 130
            # Heavy black outline + drop shadow
            draw.text((hx_pos + 4, hy_pos + 5), head_txt, font=f_head, fill=(0, 0, 0, 220), stroke_width=8, stroke_fill=(0, 0, 0, 220))
            draw.text((hx_pos, hy_pos), head_txt, font=f_head, fill=(255, 255, 255, 255), stroke_width=6, stroke_fill=(10, 12, 18, 255))

        out_img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
        Path(output_jpg).parent.mkdir(parents=True, exist_ok=True)
        out_img.save(output_jpg, "JPEG", quality=95)
        return True, output_jpg
    except Exception as exc:
        return False, f"Ошибка генерации обложки: {exc}"
    finally:
        if tmp_jpg and os.path.exists(tmp_jpg):
            try:
                os.remove(tmp_jpg)
            except Exception:
                pass


def merge_time_intervals(
    intervals: List[Tuple[float, float]],
    total_dur: float,
    pad_sec: float = 1.5,
    min_gap_sec: float = 4.0,
) -> List[Tuple[float, float]]:
    """Merge and pad active banner intervals for Smart Segment Blur."""
    cleaned = []
    for s, e in intervals:
        cs = max(0.0, float(s) - pad_sec)
        ce = min(total_dur, float(e) + pad_sec) if total_dur > 0 else float(e) + pad_sec
        if ce > cs:
            cleaned.append((cs, ce))
    if not cleaned:
        return []
    cleaned.sort()
    merged = [cleaned[0]]
    for s, e in cleaned[1:]:
        ps, pe = merged[-1]
        if s <= pe + min_gap_sec:
            merged[-1] = (ps, max(pe, e))
        else:
            merged.append((s, e))
    return [(round(s, 2), round(e, 2)) for s, e in merged]


# ------------------------------------------------------------------ #25 Target Export Presets
EXPORT_PRESETS = {
    "youtube_1080p60": {"label": "YouTube 1080p60 (высокое качество)", "max_mb": None, "height": 1080, "fps": 60, "cq": 19, "audio_kbps": 192},
    "telegram_50mb": {"label": "Telegram — до 50 МБ", "max_mb": 48, "height": 720, "fps": 30, "cq": 25, "audio_kbps": 128},
    "telegram_2gb": {"label": "Telegram — до 2 ГБ", "max_mb": 1900, "height": 1080, "fps": 60, "cq": 22, "audio_kbps": 160},
    "discord_25mb": {"label": "Discord — до 25 МБ", "max_mb": 23, "height": 720, "fps": 30, "cq": 26, "audio_kbps": 96},
}
