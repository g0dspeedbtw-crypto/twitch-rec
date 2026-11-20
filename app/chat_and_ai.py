from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import requests
from PySide6.QtCore import QThread, Signal

from app.tools import (
    CREATE_NO_WINDOW,
    encoder_ffmpeg_args,
    escape_ffmpeg_filter_path,
    get_ffmpeg_path,
    probe_video,
)

CHAT_BOT_ACCOUNTS = {
    "nightbot", "streamelements", "moobot", "fossabot", "wizebot",
    "streamlabs", "botrixoficial", "soundalerts", "sery_bot", "commanderroot",
}


def _default_ass_font() -> str:
    if sys.platform == "darwin":
        return "Helvetica Neue"
    if os.name == "nt":
        return "Segoe UI"
    return "DejaVu Sans"

HYPE_KEYWORDS = [
    "kekw", "lul", "lulw", "omegalul", "pog", "pogchamp", "poggers",
    "ахах", "хаха", "ахаха", "ор", "ору", "жесть", "капец", "имба",
    "клип", "clip", "???", "!!!", "wtf", "omg", "gg", "f ", "press f",
    "ez", "monkas", "pepelaugh", "catjam", "icant", "xdd", "rofl",
]


def _fmt_ass_time(sec: float) -> str:
    s = max(0.0, float(sec))
    h = int(s // 3600)
    m = int((s % 3600) // 60)
    rem = s % 60.0
    return f"{h}:{m:02d}:{rem:05.2f}"


def _fmt_srt_time(sec: float) -> str:
    s = max(0.0, float(sec))
    h = int(s // 3600)
    m = int((s % 3600) // 60)
    sec_i = int(s % 60)
    ms = int(round((s - int(s)) * 1000))
    if ms >= 1000:
        ms = 999
    return f"{h:02d}:{m:02d}:{sec_i:02d},{ms:03d}"


def _fmt_edl_tc(sec: float, fps: int = 30) -> str:
    s = max(0.0, float(sec))
    h = int(s // 3600)
    m = int((s % 3600) // 60)
    si = int(s % 60)
    fr = int((s - int(s)) * fps) % fps
    return f"{h:02d}:{m:02d}:{si:02d}:{fr:02d}"


def _hex_to_ass_bgr(hex_color: str) -> str:
    c = (hex_color or "#A78BFA").lstrip("#")
    if len(c) != 6:
        c = "A78BFA"
    r, g, b = c[0:2], c[2:4], c[4:6]
    return f"&H00{b}{g}{r}&"


# ------------------------------------------------------------------ #15 Parallel Twitch Chat Recorder
class TwitchChatRecorder:
    """
    Lightweight anonymous Twitch IRC chat recorder (no login required).
    Records messages with relative timestamps to .chat.json and exports .chat.ass overlay.
    """

    _PRIVMSG_RE = re.compile(r"^(?:@(?P<tags>\S+)\s+)?:(?P<user>[^!\s]+)!\S+\s+PRIVMSG\s+#\S+\s+:(?P<msg>.*)$")

    def __init__(
        self,
        channel: str,
        video_path: str,
        chat_position: str = "right",
        chat_font_size: int = 28,
        chat_hide_bots: bool = True,
        trigger_words: str = "",
        on_keyword_cb: Optional[Callable[[float, str, str], None]] = None,
        auto_clip_threshold: int = 0,
        on_hype_spike_cb: Optional[Callable[[float, int], None]] = None,
    ):
        self.channel = channel.strip().lower()
        self.video_path = video_path
        self.chat_position = chat_position
        self.chat_font_size = chat_font_size
        self.chat_hide_bots = chat_hide_bots
        self.trigger_words = [w.strip().lower() for w in (trigger_words or "").split(",") if w.strip()]
        self.on_keyword_cb = on_keyword_cb
        self.auto_clip_threshold = int(auto_clip_threshold or 0)
        self.on_hype_spike_cb = on_hype_spike_cb
        self._recent_hype_ts: List[float] = []
        self._last_clip_trigger = -120.0
        self._last_kw_trigger = -15.0
        self.json_path = str(Path(video_path).with_suffix(".chat.json"))
        self.ass_path = str(Path(video_path).with_suffix(".chat.ass"))
        self.messages: List[dict] = []
        self._stop_evt = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._start_time = time.time()

    def start(self) -> None:
        self._start_time = time.time()
        self._stop_evt.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_evt.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3.0)
        self._flush_files()

    def _run(self) -> None:
        while not self._stop_evt.is_set():
            sock: Optional[socket.socket] = None
            try:
                sock = socket.create_connection(("irc.chat.twitch.tv", 6667), timeout=10)
                sock.settimeout(2.0)
                sock.sendall(b"CAP REQ :twitch.tv/tags\r\n")
                sock.sendall(b"PASS SCHMOOPIIE\r\n")
                sock.sendall(f"NICK justinfan{os.getpid() % 89999 + 10000}\r\n".encode("ascii"))
                sock.sendall(f"JOIN #{self.channel}\r\n".encode("utf-8"))

                buf = ""
                while not self._stop_evt.is_set():
                    try:
                        chunk = sock.recv(4096)
                        if not chunk:
                            break
                        buf += chunk.decode("utf-8", errors="replace")
                        while "\r\n" in buf:
                            line, buf = buf.split("\r\n", 1)
                            if line.startswith("PING"):
                                sock.sendall(b"PONG :tmi.twitch.tv\r\n")
                                continue
                            self._parse_line(line)
                    except socket.timeout:
                        continue
            except Exception:
                time.sleep(3.0)
            finally:
                if sock:
                    try:
                        sock.close()
                    except Exception:
                        pass

    def _parse_line(self, line: str) -> None:
        m = self._PRIVMSG_RE.match(line)
        if not m:
            return
        tags_raw = m.group("tags") or ""
        tags: Dict[str, str] = {}
        for part in tags_raw.split(";"):
            if "=" in part:
                k, v = part.split("=", 1)
                tags[k] = v
        display = tags.get("display-name") or m.group("user")
        color = tags.get("color") or "#C4B5FD"
        text = m.group("msg").strip()
        if not text:
            return
        t_sec = round(max(0.0, time.time() - self._start_time), 2)
        self.messages.append({
            "t": t_sec,
            "user": display,
            "color": color,
            "text": text,
        })
        txt_low = text.lower()
        # #10 Auto-marker on keyword mention in chat
        if self.trigger_words and self.on_keyword_cb and (t_sec - self._last_kw_trigger >= 10.0):
            for kw in self.trigger_words:
                if kw in txt_low:
                    self._last_kw_trigger = t_sec
                    try:
                        self.on_keyword_cb(t_sec, kw, f"{display}: {text[:50]}")
                    except Exception:
                        pass
                    break

        # #7 Live Auto-Clipper on chat hype spike
        if self.auto_clip_threshold > 0 and self.on_hype_spike_cb:
            weight = 2 if any(hk in txt_low for hk in HYPE_KEYWORDS) else 1
            for _ in range(weight):
                self._recent_hype_ts.append(t_sec)
            cutoff = t_sec - 10.0
            self._recent_hype_ts = [x for x in self._recent_hype_ts if x >= cutoff]
            if len(self._recent_hype_ts) >= self.auto_clip_threshold and (t_sec - self._last_clip_trigger >= 60.0):
                self._last_clip_trigger = t_sec
                score = len(self._recent_hype_ts)
                self._recent_hype_ts.clear()
                try:
                    self.on_hype_spike_cb(t_sec, score)
                except Exception:
                    pass

    def _flush_files(self) -> None:
        if not self.messages:
            return
        try:
            Path(self.json_path).write_text(
                json.dumps({"channel": self.channel, "messages": self.messages}, ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
            generate_chat_ass(
                self.messages,
                self.ass_path,
                position=self.chat_position,
                font_size=self.chat_font_size,
                hide_bots=self.chat_hide_bots,
            )
        except Exception:
            pass


def generate_chat_ass(
    messages: List[dict],
    ass_path: str,
    max_visible: int = 12,
    position: str = "right",
    font_size: int = 28,
    hide_bots: bool = True,
) -> str:
    """Generate a sleek transparent side-chat ASS subtitle overlay from recorded chat messages."""
    font = _default_ass_font()
    fsz = max(16, min(48, int(font_size or 28)))
    margin_l, margin_r = (24, 1440) if position == "left" else (1440, 24)
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
WrapStyle: 1

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Chat,{font},{fsz},&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,2,1,1,{margin_l},{margin_r},40,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    filtered = []
    for m in messages:
        u_low = str(m.get("user", "")).strip().lower()
        t_raw = str(m.get("text", "")).strip()
        if not t_raw:
            continue
        if hide_bots and (u_low in CHAT_BOT_ACCOUNTS or t_raw.startswith("!")):
            continue
        filtered.append(m)

    lines = [header]
    window: List[dict] = []
    for i, msg in enumerate(filtered):
        t_start = float(msg.get("t", 0.0))
        t_end = float(filtered[i + 1]["t"]) if i + 1 < len(filtered) else t_start + 8.0
        t_end = max(t_start + 0.1, min(t_end, t_start + 12.0))

        window.append(msg)
        if len(window) > max_visible:
            window.pop(0)

        rendered_rows = []
        for w in window:
            u = str(w.get("user", "")).replace("{", "").replace("}", "")
            txt = str(w.get("text", "")).replace("{", "").replace("}", "").replace("\n", " ")[:90]
            col = _hex_to_ass_bgr(w.get("color", "#C4B5FD"))
            rendered_rows.append(f"{{\\b1\\c{col}}}{u}:{{\\b0\\c&H00FFFFFF&}} {txt}")

        body = "\\N".join(rendered_rows)
        lines.append(f"Dialogue: 0,{_fmt_ass_time(t_start)},{_fmt_ass_time(t_end)},Chat,,0,0,0,,{body}\n")

    Path(ass_path).write_text("".join(lines), encoding="utf-8")
    return ass_path


# ------------------------------------------------------------------ #33 Twitch VOD Chat Downloader
def fetch_twitch_vod_chat(
    vod_url_or_id: str,
    output_base_path: str,
    oauth_token: str = "",
    proxy_url: str = "",
    chat_position: str = "right",
    chat_font_size: int = 28,
    chat_hide_bots: bool = True,
    max_pages: int = 80,
    progress_cb: Optional[Callable[[int], None]] = None,
) -> Tuple[bool, str, str, int]:
    """
    Download Twitch VOD chat via GQL into .chat.json and .chat.ass.
    Returns (ok, json_path, error_or_msg, message_count).
    """
    m = re.search(r"twitch\.tv/videos/(\d+)", vod_url_or_id or "", re.IGNORECASE)
    vod_id = m.group(1) if m else (vod_url_or_id.strip() if (vod_url_or_id or "").strip().isdigit() else "")
    if not vod_id:
        return False, "", "Укажите ссылку на Twitch VOD или его ID", 0

    headers = {
        "Client-ID": "kimne78kx3ncx6brgo4mv6wki5h1ko",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0",
    }
    if oauth_token.strip():
        tok = oauth_token.strip()
        tok = tok[6:] if tok.lower().startswith("oauth:") else tok
        headers["Authorization"] = f"OAuth {tok}"
    proxies = {"http": proxy_url.strip(), "https": proxy_url.strip()} if proxy_url.strip() else None

    gql_query = """
    query($videoID: ID!, $cursor: String, $offset: Int) {
      video(id: $videoID) {
        comments(after: $cursor, contentOffsetSeconds: $offset) {
          edges {
            cursor
            node {
              contentOffsetSeconds
              commenter { displayName }
              message {
                userColor
                fragments { text }
              }
            }
          }
          pageInfo { hasNextPage }
        }
      }
    }
    """
    messages: List[dict] = []
    cursor: Optional[str] = None
    try:
        for page in range(max_pages):
            variables = {"videoID": vod_id}
            if cursor:
                variables["cursor"] = cursor
            else:
                variables["offset"] = 0
            resp = requests.post(
                "https://gql.twitch.tv/gql",
                json={"query": gql_query, "variables": variables},
                headers=headers,
                timeout=15,
                proxies=proxies,
            )
            resp.raise_for_status()
            video = (resp.json().get("data") or {}).get("video")
            if not video:
                if page == 0:
                    return False, "", f"VOD #{vod_id} не найден или чат недоступен", 0
                break
            comments = video.get("comments") or {}
            edges = comments.get("edges") or []
            if not edges:
                break
            for edge in edges:
                cursor = edge.get("cursor") or cursor
                node = edge.get("node") or {}
                msg_obj = node.get("message") or {}
                frags = msg_obj.get("fragments") or []
                txt = "".join(str(f.get("text") or "") for f in frags).strip()
                if not txt:
                    continue
                user = ((node.get("commenter") or {}).get("displayName")) or "viewer"
                color = msg_obj.get("userColor") or "#C4B5FD"
                t_sec = round(max(0.0, float(node.get("contentOffsetSeconds") or 0.0)), 2)
                messages.append({"t": t_sec, "user": user, "color": color, "text": txt})
            if progress_cb:
                progress_cb(len(messages))
            if not (comments.get("pageInfo") or {}).get("hasNextPage"):
                break
    except Exception as exc:
        if not messages:
            return False, "", f"Ошибка загрузки чата VOD: {exc}", 0

    if not messages:
        return False, "", "В этом VOD нет сохранённых сообщений чата", 0

    base = Path(output_base_path)
    json_path = str(base.with_suffix(".chat.json"))
    ass_path = str(base.with_suffix(".chat.ass"))
    Path(json_path).write_text(
        json.dumps({"channel": f"vod_{vod_id}", "vod_id": vod_id, "messages": messages}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    generate_chat_ass(
        messages,
        ass_path,
        position=chat_position,
        font_size=chat_font_size,
        hide_bots=chat_hide_bots,
    )
    return True, json_path, f"Загружено {len(messages)} сообщений чата VOD #{vod_id}", len(messages)


# ------------------------------------------------------------------ #16 Chat Activity Heatmap & Highlights
@dataclass
class HighlightMoment:
    timestamp_sec: float
    score: float
    source: str     # "chat" | "audio" | "marker"
    reason: str


def analyze_chat_highlights(
    chat_json_path: str,
    bucket_sec: int = 10,
    top_n: int = 15,
) -> Tuple[List[Tuple[float, float]], List[HighlightMoment]]:
    """
    Analyze a .chat.json file to produce:
      - heatmap: list of (bucket_start_sec, activity_score)
      - highlights: top ranked HighlightMoment spikes
    """
    if not os.path.exists(chat_json_path):
        return [], []
    try:
        raw = json.loads(Path(chat_json_path).read_text(encoding="utf-8"))
        msgs = raw if isinstance(raw, list) else (raw.get("messages", []) if isinstance(raw, dict) else [])
    except Exception:
        return [], []
    if not msgs:
        return [], []

    max_t = max(float(m.get("t", 0.0)) for m in msgs)
    num_buckets = max(1, int(max_t // bucket_sec) + 1)
    scores = [0.0] * num_buckets
    hype_hits: List[Dict[str, int]] = [{} for _ in range(num_buckets)]

    for m in msgs:
        b = min(num_buckets - 1, int(float(m.get("t", 0.0)) // bucket_sec))
        txt_low = str(m.get("text", "")).lower()
        weight = 1.0
        for kw in HYPE_KEYWORDS:
            if kw in txt_low:
                weight += 1.5
                hype_hits[b][kw.strip()] = hype_hits[b].get(kw.strip(), 0) + 1
                break
        scores[b] += weight

    heatmap = [(i * bucket_sec, scores[i]) for i in range(num_buckets)]
    avg = sum(scores) / max(1, len(scores))
    threshold = max(3.0, avg * 1.8)

    ranked_indices = sorted(range(num_buckets), key=lambda i: scores[i], reverse=True)
    highlights: List[HighlightMoment] = []
    used_times: List[float] = []

    for idx in ranked_indices:
        if len(highlights) >= top_n:
            break
        if scores[idx] < threshold and len(highlights) >= 3:
            break
        t = idx * bucket_sec
        if any(abs(t - ut) < bucket_sec * 2.5 for ut in used_times):
            continue
        used_times.append(t)
        top_kws = sorted(hype_hits[idx].items(), key=lambda kv: kv[1], reverse=True)[:3]
        kw_str = ", ".join(k for k, _ in top_kws) if top_kws else "всплеск сообщений"
        highlights.append(
            HighlightMoment(
                timestamp_sec=float(t),
                score=round(scores[idx], 1),
                source="chat",
                reason=f"Чат ({int(scores[idx])} балл.): {kw_str}",
            )
        )

    highlights.sort(key=lambda h: h.timestamp_sec)
    return heatmap, highlights


# ------------------------------------------------------------------ #18 Loud Audio Moments Detector
def detect_loud_moments(
    video_path: str,
    top_n: int = 12,
    bucket_sec: int = 5,
) -> Tuple[List[Tuple[float, float]], List[HighlightMoment]]:
    """Scan video audio loudness per second via FFmpeg astats to locate loud/funny/action spikes."""
    ffmpeg = get_ffmpeg_path()
    if not ffmpeg or not os.path.exists(video_path):
        return [], []
    try:
        res = subprocess.run(
            [
                ffmpeg, "-hide_banner", "-i", video_path,
                "-af", "asetnsamples=48000,astats=metadata=1:reset=1,ametadata=print:key=lavfi.astats.Overall.RMS_level",
                "-vn", "-f", "null", "-",
            ],
            capture_output=True, text=True, timeout=240, creationflags=CREATE_NO_WINDOW,
        )
        rms_per_sec: List[Tuple[float, float]] = []
        cur_t = 0.0
        for line in (res.stderr or "").splitlines():
            mt = re.search(r"pts_time:([\d.]+)", line)
            if mt:
                cur_t = float(mt.group(1))
            mv = re.search(r"RMS_level=(-?[\d.]+)", line)
            if mv:
                db = float(mv.group(1))
                # Map -60..0 dB to 0..60 linear energy score
                val = max(0.0, 60.0 + db)
                rms_per_sec.append((cur_t, val))

        if not rms_per_sec:
            return [], []

        # Aggregate into buckets
        max_t = rms_per_sec[-1][0]
        nb = max(1, int(max_t // bucket_sec) + 1)
        b_vals = [0.0] * nb
        for t, val in rms_per_sec:
            bi = min(nb - 1, int(t // bucket_sec))
            if val > b_vals[bi]:
                b_vals[bi] = val

        heatmap = [(i * bucket_sec, round(b_vals[i], 1)) for i in range(nb)]
        ranked = sorted(range(nb), key=lambda i: b_vals[i], reverse=True)
        highlights: List[HighlightMoment] = []
        used: List[float] = []
        for idx in ranked:
            if len(highlights) >= top_n:
                break
            t = idx * bucket_sec
            if any(abs(t - ut) < 15.0 for ut in used):
                continue
            used.append(t)
            db_est = b_vals[idx] - 60.0
            highlights.append(
                HighlightMoment(
                    timestamp_sec=float(t),
                    score=round(b_vals[idx], 1),
                    source="audio",
                    reason=f"Громкий момент ({db_est:.1f} дБ)",
                )
            )
        highlights.sort(key=lambda h: h.timestamp_sec)
        return heatmap, highlights
    except Exception:
        return [], []


# ------------------------------------------------------------------ #17 AI Laughter / Scream / Gunshot Audio Event Detector
def detect_audio_events(
    video_path: str,
    top_n: int = 12,
    bucket_sec: int = 4,
) -> Tuple[List[Tuple[float, float]], List[HighlightMoment]]:
    """
    Detect high-frequency vocal/action audio events (streamer laughter, screams, gunshots)
    by bandpass-filtering 900Hz–3800Hz energy + crest-factor bursts via FFmpeg astats.
    """
    ffmpeg = get_ffmpeg_path()
    if not ffmpeg or not os.path.exists(video_path):
        return [], []
    try:
        res = subprocess.run(
            [
                ffmpeg, "-hide_banner", "-i", video_path,
                "-af", (
                    "highpass=f=900,lowpass=f=3800,asetnsamples=24000,"
                    "astats=metadata=1:reset=1,"
                    "ametadata=print:key=lavfi.astats.Overall.Peak_level"
                ),
                "-vn", "-f", "null", "-",
            ],
            capture_output=True, text=True, timeout=240, creationflags=CREATE_NO_WINDOW,
        )
        peaks: List[Tuple[float, float]] = []
        cur_t = 0.0
        for line in (res.stderr or "").splitlines():
            mt = re.search(r"pts_time:([\d.]+)", line)
            if mt:
                cur_t = float(mt.group(1))
            mv = re.search(r"Peak_level=(-?[\d.]+)", line)
            if mv:
                db = float(mv.group(1))
                val = max(0.0, 55.0 + db)
                peaks.append((cur_t, val))

        if not peaks:
            return detect_loud_moments(video_path, top_n=top_n, bucket_sec=bucket_sec)

        max_t = peaks[-1][0]
        nb = max(1, int(max_t // bucket_sec) + 1)
        b_vals = [0.0] * nb
        b_counts = [0] * nb
        for t, val in peaks:
            bi = min(nb - 1, int(t // bucket_sec))
            if val > b_vals[bi]:
                b_vals[bi] = val
            if val > 38.0:
                b_counts[bi] += 1

        heatmap = [(i * bucket_sec, round(b_vals[i], 1)) for i in range(nb)]
        ranked = sorted(range(nb), key=lambda i: b_vals[i] + b_counts[i] * 2.5, reverse=True)
        highlights: List[HighlightMoment] = []
        used: List[float] = []
        for idx in ranked:
            if len(highlights) >= top_n:
                break
            if b_vals[idx] < 24.0 and len(highlights) >= 3:
                break
            t = idx * bucket_sec
            if any(abs(t - ut) < 12.0 for ut in used):
                continue
            used.append(t)
            kind = "Смех / Эмоции" if b_counts[idx] >= 3 else "Крик / Выстрел"
            db_est = b_vals[idx] - 55.0
            highlights.append(
                HighlightMoment(
                    timestamp_sec=float(t),
                    score=round(b_vals[idx], 1),
                    source="events",
                    reason=f"AI Аудио: {kind} ({db_est:.1f} дБ)",
                )
            )
        highlights.sort(key=lambda h: h.timestamp_sec)
        return heatmap, highlights
    except Exception:
        return [], []


# ------------------------------------------------------------------ #19 Live Markers & EDL Export
def save_markers_and_edl(video_path: str, markers: List[dict]) -> Tuple[str, str]:
    """Save manual highlight markers to .markers.json and Premiere/DaVinci .edl."""
    p = Path(video_path)
    j_path = str(p.with_suffix(".markers.json"))
    edl_path = str(p.with_suffix(".edl"))
    Path(j_path).write_text(json.dumps(markers, ensure_ascii=False, indent=2), encoding="utf-8")

    edl_lines = [f"TITLE: {p.stem}", "FCM: NON-DROP FRAME", ""]
    for idx, m in enumerate(markers, 1):
        t = float(m.get("t", 0.0))
        note = str(m.get("note", "Момент"))
        tc_in = _fmt_edl_tc(t)
        tc_out = _fmt_edl_tc(t + 1.0)
        edl_lines.append(f"{idx:03d}  001      V     C        {tc_in} {tc_out} {tc_in} {tc_out}")
        edl_lines.append(f"|C:ResolveColorPurple |M:{note} |D:1")
    Path(edl_path).write_text("\n".join(edl_lines) + "\n", encoding="utf-8")
    return j_path, edl_path


# ------------------------------------------------------------------ #22 AI Subtitles & #6 Censorship Worker
class WhisperAIWorker(QThread):
    """
    Runs local faster-whisper (auto GPU/CPU) for:
      - mode='subtitles': generates .srt + bold Shorts-style .ass (and optionally burns into video!)
      - mode='censor': locates banned words via word timestamps and mutes/beeps exact spans!
    """

    progress_updated = Signal(str, float, str)  # job_id, pct, status
    log_message = Signal(str)
    finished_ai = Signal(str, str, bool)        # job_id, output_path, success

    def __init__(
        self,
        job_id: str,
        mode: str,                 # "subtitles" | "censor"
        video_path: str,
        model_size: str = "small",
        language: str = "ru",
        burn_subtitles: bool = True,
        shorts_style: bool = True,
        banned_words: str = "",
        censor_beep: bool = True,
        encoder: str = "h264_nvenc",
        quality: int = 23,
        initial_prompt: str = "",
        task: str = "transcribe",  # "transcribe" | "translate" (RU -> EN)
        parent=None,
    ):
        super().__init__(parent)
        self.job_id = job_id
        self.mode = mode
        self.video_path = video_path
        self.model_size = model_size
        self.language = language or None
        self.burn_subtitles = burn_subtitles
        self.shorts_style = shorts_style
        self.banned_words = [w.strip().lower() for w in banned_words.split(",") if w.strip()]
        self.censor_beep = censor_beep
        self.encoder = encoder
        self.quality = quality
        self.initial_prompt = (initial_prompt or "").strip()
        self.task = task if task in ("transcribe", "translate") else "transcribe"
        self._stop_requested = False
        self._proc: Optional[subprocess.Popen] = None

    def request_stop(self) -> None:
        self._stop_requested = True
        p = self._proc
        if p and p.poll() is None:
            try:
                p.terminate()
            except Exception:
                pass

    def _run_ffmpeg_cancellable(self, cmd: List[str], timeout: int = 1800) -> Tuple[int, str]:
        self._proc = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", creationflags=CREATE_NO_WINDOW,
        )
        try:
            _, err = self._proc.communicate(timeout=timeout)
            return self._proc.returncode or 0, err or ""
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.communicate()
            return 1, "Таймаут FFmpeg"
        finally:
            self._proc = None

    def run(self) -> None:
        try:
            from faster_whisper import WhisperModel
        except Exception as exc:
            self.log_message.emit(f"[AI] faster-whisper не установлен: {exc}")
            self.finished_ai.emit(self.job_id, "", False)
            return

        if not os.path.exists(self.video_path):
            self.finished_ai.emit(self.job_id, "", False)
            return

        self.progress_updated.emit(self.job_id, 5.0, f"Загрузка модели Whisper ({self.model_size})...")
        self.log_message.emit(f"[AI] Загрузка Whisper «{self.model_size}» для {Path(self.video_path).name}...")

        model = None
        # Try CUDA first (RTX 4060 Ti), smoothly fall back to int8 CPU if cuDNN dll isn't on PATH
        for dev, ctype in (("cuda", "float16"), ("cpu", "int8")):
            try:
                model = WhisperModel(self.model_size, device=dev, compute_type=ctype)
                self.log_message.emit(f"[AI] Модель Whisper загружена на устройстве: {dev} ({ctype})")
                break
            except Exception:
                continue

        if model is None:
            self.log_message.emit("[AI] Не удалось инициализировать модель Whisper.")
            self.finished_ai.emit(self.job_id, "", False)
            return

        meta = probe_video(self.video_path)
        total_dur = max(1.0, meta.get("duration", 0.0))

        try:
            self.progress_updated.emit(self.job_id, 15.0, "Распознавание речи...")
            segments_gen, _ = model.transcribe(
                self.video_path,
                language=self.language if self.language != "auto" else None,
                task=self.task,
                initial_prompt=self.initial_prompt or None,
                word_timestamps=True,
                vad_filter=True,
            )
            segments = []
            for seg in segments_gen:
                if self._stop_requested:
                    self.progress_updated.emit(self.job_id, 0.0, "Отменено")
                    self.finished_ai.emit(self.job_id, "", False)
                    return
                segments.append(seg)
                pct = 15.0 + min(65.0, (seg.end / total_dur) * 65.0)
                self.progress_updated.emit(self.job_id, pct, f"Распознано {int(seg.end)} / {int(total_dur)} сек.")

            if self._stop_requested:
                self.progress_updated.emit(self.job_id, 0.0, "Отменено")
                self.finished_ai.emit(self.job_id, "", False)
                return

            if self.mode == "subtitles":
                self._finish_subtitles(segments)
            else:
                self._finish_censor(segments)
        except Exception as exc:
            self.log_message.emit(f"[AI] Ошибка распознавания: {exc}")
            self.progress_updated.emit(self.job_id, 0.0, f"Ошибка: {str(exc)[:60]}")
            self.finished_ai.emit(self.job_id, "", False)

    def _finish_subtitles(self, segments) -> None:
        p = Path(self.video_path)
        srt_path = p.with_suffix(".srt")
        ass_path = p.with_suffix(".subs.ass")

        # Write SRT
        srt_lines = []
        for idx, seg in enumerate(segments, 1):
            srt_lines.append(f"{idx}\n{_fmt_srt_time(seg.start)} --> {_fmt_srt_time(seg.end)}\n{seg.text.strip()}\n")
        srt_path.write_text("\n".join(srt_lines), encoding="utf-8")

        # Write stylish ASS (Shorts bold yellow/white centered or standard bottom)
        font_sz = 64 if self.shorts_style else 46
        margin_v = 180 if self.shorts_style else 60
        font = _default_ass_font()
        ass_header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font},{font_sz},&H00FFFFFF,&H0000FFFF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,4,2,2,80,80,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
        ass_lines = [ass_header]
        if self.shorts_style:
            # #20 Word-by-word karaoke pop-in Shorts subtitles (3 words per chunk, active word yellow & popped)
            all_words = []
            for seg in segments:
                wlist = getattr(seg, "words", None) or []
                for w in wlist:
                    wt = (getattr(w, "word", "") or "").strip().replace("{", "").replace("}", "")
                    if wt:
                        all_words.append((float(w.start), float(w.end), wt))
            if all_words:
                chunk_size = 3
                for ci in range(0, len(all_words), chunk_size):
                    chunk = all_words[ci : ci + chunk_size]
                    for active_idx, (ws, we, _) in enumerate(chunk):
                        next_s = chunk[active_idx + 1][0] if active_idx + 1 < len(chunk) else we
                        t_end = max(ws + 0.08, next_s)
                        parts = []
                        for wi, (_, _, wtxt) in enumerate(chunk):
                            if wi == active_idx:
                                parts.append(f"{{\\c&H0000FFFF&\\fscx114\\fscy114}}{wtxt.upper()}{{\\c&H00FFFFFF&\\fscx100\\fscy100}}")
                            else:
                                parts.append(wtxt.upper())
                        ass_lines.append(
                            f"Dialogue: 0,{_fmt_ass_time(ws)},{_fmt_ass_time(t_end)},Default,,0,0,0,,{' '.join(parts)}\n"
                        )
            else:
                for seg in segments:
                    txt = seg.text.strip().replace("\n", " ")
                    if txt:
                        ass_lines.append(
                            f"Dialogue: 0,{_fmt_ass_time(seg.start)},{_fmt_ass_time(seg.end)},Default,,0,0,0,,{txt}\n"
                        )
        else:
            for seg in segments:
                txt = seg.text.strip().replace("\n", " ")
                if txt:
                    ass_lines.append(
                        f"Dialogue: 0,{_fmt_ass_time(seg.start)},{_fmt_ass_time(seg.end)},Default,,0,0,0,,{txt}\n"
                    )
        ass_path.write_text("".join(ass_lines), encoding="utf-8")
        try:
            from app.ai_suite import enhance_subtitles_with_emojis
            enhance_subtitles_with_emojis(str(ass_path))
        except Exception:
            pass
        self.log_message.emit(f"[AI] Субтитры с AI-эмодзи сохранены: {srt_path.name} и {ass_path.name}")

        if not self.burn_subtitles:
            self.progress_updated.emit(self.job_id, 100.0, f"Готово: {srt_path.name}")
            self.finished_ai.emit(self.job_id, str(srt_path), True)
            return

        # Burn subtitles into video
        ffmpeg = get_ffmpeg_path()
        if not ffmpeg:
            self.log_message.emit("[AI] FFmpeg не найден — субтитры сохранены только в .srt/.ass")
            self.progress_updated.emit(self.job_id, 100.0, f"Готово: {srt_path.name} (без FFmpeg)")
            self.finished_ai.emit(self.job_id, str(srt_path), True)
            return
        if not segments:
            self.log_message.emit("[AI] В видео не обнаружена речь — вшивание субтитров пропущено.")
            self.progress_updated.emit(self.job_id, 100.0, "Речь не обнаружена")
            self.finished_ai.emit(self.job_id, self.video_path, True)
            return
        out_video = str(p.with_name(f"{p.stem}_subtitled{p.suffix}"))
        self.progress_updated.emit(self.job_id, 85.0, "Вшивание субтитров в видео...")
        escaped_ass = escape_ffmpeg_filter_path(str(ass_path))
        cmd = [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-i", self.video_path,
            "-vf", f"ass='{escaped_ass}'",
            *encoder_ffmpeg_args(self.encoder, self.quality),
            "-c:a", "copy", "-movflags", "+faststart", out_video,
        ]
        rc, err = self._run_ffmpeg_cancellable(cmd, timeout=1800)
        if self._stop_requested:
            if os.path.exists(out_video):
                try:
                    os.remove(out_video)
                except Exception:
                    pass
            self.progress_updated.emit(self.job_id, 0.0, "Отменено")
            self.finished_ai.emit(self.job_id, "", False)
            return
        if rc == 0 and os.path.exists(out_video):
            self.progress_updated.emit(self.job_id, 100.0, "Готово (субтитры вшиты)")
            self.log_message.emit(f"[AI] Видео с субтитрами готово: {Path(out_video).name}")
            self.finished_ai.emit(self.job_id, out_video, True)
        else:
            self.log_message.emit(f"[AI] Ошибка вшивания субтитров: {err}")
            self.progress_updated.emit(self.job_id, 0.0, f"Ошибка FFmpeg (SRT сохранён: {srt_path.name})")
            self.finished_ai.emit(self.job_id, str(srt_path), False)

    def _finish_censor(self, segments) -> None:
        matches: List[Tuple[float, float, str]] = []
        for seg in segments:
            words = getattr(seg, "words", None) or []
            for w in words:
                clean = re.sub(r"[^a-zA-Zа-яА-ЯёЁ0-9]", "", w.word or "").lower()
                if not clean:
                    continue
                if any(bw in clean for bw in self.banned_words):
                    matches.append((max(0.0, w.start - 0.05), w.end + 0.05, clean))

        if not matches:
            self.progress_updated.emit(self.job_id, 100.0, "Запрещённых слов не найдено")
            self.log_message.emit("[AI Цензура] Запрещённые слова не обнаружены.")
            self.finished_ai.emit(self.job_id, self.video_path, True)
            return

        ffmpeg = get_ffmpeg_path()
        if not ffmpeg:
            self.log_message.emit("[AI Цензура] Ошибка: FFmpeg не найден.")
            self.progress_updated.emit(self.job_id, 0.0, "Ошибка: FFmpeg не найден")
            self.finished_ai.emit(self.job_id, "", False)
            return
        p = Path(self.video_path)
        out_video = str(p.with_name(f"{p.stem}_censored{p.suffix}"))
        self.progress_updated.emit(self.job_id, 85.0, f"Цензура {len(matches)} слов...")

        cond = "+".join(f"between(t,{s:.2f},{e:.2f})" for s, e, _ in matches[:60])
        if self.censor_beep:
            af = (
                f"[0:a]volume=0:enable='{cond}'[muted];"
                f"sine=frequency=1000:sample_rate=48000,volume=0.22,"
                f"volume=0:enable='not({cond})'[beep];"
                f"[muted][beep]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[aout]"
            )
            cmd = [
                ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                "-i", self.video_path,
                "-filter_complex", af,
                "-map", "0:v", "-map", "[aout]",
                "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", out_video,
            ]
        else:
            cmd = [
                ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                "-i", self.video_path,
                "-af", f"volume=0:enable='{cond}'",
                "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", out_video,
            ]

        rc, err = self._run_ffmpeg_cancellable(cmd, timeout=900)
        if self._stop_requested:
            if os.path.exists(out_video):
                try:
                    os.remove(out_video)
                except Exception:
                    pass
            self.progress_updated.emit(self.job_id, 0.0, "Отменено")
            self.finished_ai.emit(self.job_id, "", False)
            return
        if rc == 0 and os.path.exists(out_video):
            self.progress_updated.emit(self.job_id, 100.0, f"Готово (запикано слов: {len(matches)})")
            self.log_message.emit(f"[AI Цензура] Обработано {len(matches)} слов -> {Path(out_video).name}")
            self.finished_ai.emit(self.job_id, out_video, True)
        else:
            self.log_message.emit(f"[AI Цензура] Ошибка FFmpeg: {err}")
            self.finished_ai.emit(self.job_id, "", False)


# ------------------------------------------------------------------ #26 AI YouTube Titles / Description / Timestamps
def generate_youtube_metadata(
    video_path: str,
    llm_api_url: str = "",
    llm_api_key: str = "",
    llm_model: str = "gpt-4o-mini",
    proxy_url: str = "",
) -> Tuple[bool, dict]:
    """
    Generate YouTube titles, description, tags, and chapter timestamps from companion
    .srt, .chat.json, .markers.json, and .meta.json files.
    Uses OpenAI-compatible LLM API if configured, otherwise produces smart local heuristics.
    """
    p = Path(video_path)
    meta_info = {}
    meta_file = p.with_suffix(".meta.json")
    if meta_file.exists():
        try:
            meta_info = json.loads(meta_file.read_text(encoding="utf-8"))
        except Exception:
            pass

    markers = []
    markers_file = p.with_suffix(".markers.json")
    if markers_file.exists():
        try:
            markers = json.loads(markers_file.read_text(encoding="utf-8"))
        except Exception:
            pass

    srt_excerpt = ""
    srt_file = p.with_suffix(".srt")
    if srt_file.exists():
        try:
            srt_excerpt = srt_file.read_text(encoding="utf-8")[:6000]
        except Exception:
            pass

    chat_highlights: List[HighlightMoment] = []
    chat_file = p.with_suffix(".chat.json")
    if chat_file.exists():
        _, chat_highlights = analyze_chat_highlights(str(chat_file), top_n=10)

    def _mmss(sec: float) -> str:
        s = max(0, int(sec))
        h, rem = divmod(s, 3600)
        m, sc = divmod(rem, 60)
        return f"{h:02d}:{m:02d}:{sc:02d}" if h > 0 else f"{m:02d}:{sc:02d}"

    chapters = ["00:00 Начало"]
    for mk in markers[:15]:
        chapters.append(f"{_mmss(mk.get('t', 0))} {mk.get('note', 'Хайлайт')}")
    for hl in chat_highlights[:10]:
        chapters.append(f"{_mmss(hl.timestamp_sec)} {hl.reason}")

    channel_name = meta_info.get("channel") or p.parent.name
    game_name = meta_info.get("game") or "Стрим"
    stream_title = meta_info.get("title") or p.stem

    # Fallback local heuristic when LLM API is not configured
    if not llm_api_url.strip():
        titles = [
            f"{channel_name}: ЛУЧШИЕ МОМЕНТЫ — {game_name}",
            f"{stream_title[:55]} | Нарезка {channel_name}",
            f"ЧАТ В ШОКЕ! Топ моменты {channel_name} ({game_name})",
            f"{channel_name} — самые смешные и жёсткие моменты стрима",
            f"НЕВЕРОЯТНЫЙ СТРИМ {channel_name} в {game_name}",
        ]
        tags = [channel_name, game_name, "twitch", "нарезка", "хайлайты", "стрим", "лучшие моменты", "shorts"]
        desc = (
            f"Лучшие моменты со стрима {channel_name} ({game_name}).\n"
            f"Оригинальный эфир: {stream_title}\n\n"
            f"Таймкоды:\n" + "\n".join(chapters)
        )
        return True, {
            "source": "local",
            "titles": titles,
            "description": desc,
            "tags": ", ".join(tags),
            "chapters": "\n".join(chapters),
        }

    # OpenAI-compatible LLM request (OpenAI, OpenRouter, Gemini OpenAI-compat, Ollama)
    endpoint = llm_api_url.strip().rstrip("/")
    if not endpoint.endswith("/chat/completions"):
        endpoint = f"{endpoint}/chat/completions"

    prompt = (
        f"Ты опытный продюсер YouTube-канала нарезок стримеров.\n"
        f"Канал: {channel_name}\nИгра: {game_name}\nНазвание стрима: {stream_title}\n"
        f"Таймкоды хайлайтов:\n" + "\n".join(chapters[:15]) + "\n\n"
        f"Фрагмент субтитров:\n{srt_excerpt[:3500] or '(субтитров нет)'}\n\n"
        f"Ответь СТРОГО в JSON формате с ключами:\n"
        f'{{"titles": ["5 кликабельных заголовков YouTube на русском"], '
        f'"description": "SEO-описание с таймкодами", '
        f'"tags": "теги через запятую", '
        f'"chapters": "таймкоды построчно"}}'
    )
    headers = {"Content-Type": "application/json"}
    if llm_api_key.strip():
        headers["Authorization"] = f"Bearer {llm_api_key.strip()}"
    proxies = {"http": proxy_url.strip(), "https": proxy_url.strip()} if proxy_url.strip() else None

    try:
        resp = requests.post(
            endpoint,
            json={
                "model": llm_model.strip() or "gpt-4o-mini",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.7,
            },
            headers=headers,
            timeout=45,
            proxies=proxies,
        )
        resp.raise_for_status()
        content = (((resp.json().get("choices") or [{}])[0].get("message") or {}).get("content")) or ""
        m_json = re.search(r"\{[\s\S]*\}", content)
        parsed = json.loads(m_json.group(0)) if m_json else {}
        return True, {
            "source": "llm",
            "titles": parsed.get("titles") or [stream_title],
            "description": parsed.get("description") or content,
            "tags": parsed.get("tags") or f"{channel_name}, {game_name}, twitch",
            "chapters": parsed.get("chapters") or "\n".join(chapters),
            "summary": parsed.get("summary") or f"Конспект эфира {channel_name} ({game_name}):\n" + "\n".join(chapters),
        }
    except Exception as exc:
        return False, {"error": f"Ошибка LLM API: {exc}"}


def generate_stream_digest(
    video_path: str,
    llm_api_url: str = "",
    llm_api_key: str = "",
    llm_model: str = "gpt-4o-mini",
    proxy_url: str = "",
) -> Tuple[bool, str]:
    """
    #18 Generate a structured Stream Digest / Summary (topics, highlights, chat stats, key moments)
    from companion .meta.json, .markers.json, .chat.json, and .srt files.
    """
    p = Path(video_path)
    meta_info = {}
    if p.with_suffix(".meta.json").exists():
        try:
            meta_info = json.loads(p.with_suffix(".meta.json").read_text(encoding="utf-8"))
        except Exception:
            pass

    markers = []
    if p.with_suffix(".markers.json").exists():
        try:
            markers = json.loads(p.with_suffix(".markers.json").read_text(encoding="utf-8"))
        except Exception:
            pass

    chat_msgs = []
    if p.with_suffix(".chat.json").exists():
        try:
            raw_c = json.loads(p.with_suffix(".chat.json").read_text(encoding="utf-8"))
            chat_msgs = raw_c if isinstance(raw_c, list) else raw_c.get("messages", [])
        except Exception:
            pass

    srt_txt = ""
    if p.with_suffix(".srt").exists():
        try:
            srt_txt = p.with_suffix(".srt").read_text(encoding="utf-8")[:5000]
        except Exception:
            pass

    _, chat_hl = analyze_chat_highlights(str(p.with_suffix(".chat.json")), top_n=12) if p.with_suffix(".chat.json").exists() else ([], [])

    def _mmss(sec: float) -> str:
        s = max(0, int(sec))
        h, rem = divmod(s, 3600)
        m, sc = divmod(rem, 60)
        return f"{h:02d}:{m:02d}:{sc:02d}" if h > 0 else f"{m:02d}:{sc:02d}"

    ch_name = meta_info.get("channel") or p.parent.name
    game = meta_info.get("game") or "Прямой эфир"
    title = meta_info.get("title") or p.stem
    dur_s = float(meta_info.get("duration_sec") or probe_video(video_path).get("duration", 0))

    lines = [
        f"📋 КОНСПЕКТ СТРИМА: {ch_name}",
        f"🎮 Категория: {game}",
        f"📌 Название: {title}",
        f"⏱ Длительность: {_mmss(dur_s)} · Сообщений чата: {len(chat_msgs)} · Маркеров: {len(markers)}",
        "",
        "🔥 ГЛАВНЫЕ МОМЕНТЫ И ТАЙМКОДЫ:",
        "  • 00:00 — Начало трансляции",
    ]
    for mk in markers[:20]:
        lines.append(f"  • {_mmss(mk.get('t', 0))} — [Маркер] {mk.get('note', 'Момент')}")
    for hl in chat_hl[:12]:
        lines.append(f"  • {_mmss(hl.timestamp_sec)} — {hl.reason}")

    if chat_msgs:
        user_cnt: Dict[str, int] = {}
        for m in chat_msgs:
            u = str(m.get("user") or "")
            if u and u.lower() not in CHAT_BOT_ACCOUNTS:
                user_cnt[u] = user_cnt.get(u, 0) + 1
        top_users = sorted(user_cnt.items(), key=lambda kv: kv[1], reverse=True)[:5]
        if top_users:
            lines.append("")
            lines.append("💬 Топ-активных зрителей чата: " + ", ".join(f"{u} ({c})" for u, c in top_users))

    if srt_txt.strip():
        clean_speech = [
            ln.strip() for ln in srt_txt.splitlines()
            if ln.strip() and not ln.strip().isdigit() and "-->" not in ln
        ]
        excerpt = " ".join(clean_speech[:35])[:700]
        if excerpt:
            lines.append("")
            lines.append(f"🎙 Выдержка из речи (Whisper): «{excerpt}...»")

    local_digest = "\n".join(lines)

    if llm_api_url.strip():
        try:
            endpoint = llm_api_url.strip().rstrip("/")
            if not endpoint.endswith("/chat/completions"):
                endpoint = f"{endpoint}/chat/completions"
            headers = {"Content-Type": "application/json"}
            if llm_api_key.strip():
                headers["Authorization"] = f"Bearer {llm_api_key.strip()}"
            proxies = {"http": proxy_url.strip(), "https": proxy_url.strip()} if proxy_url.strip() else None
            resp = requests.post(
                endpoint,
                json={
                    "model": llm_model.strip() or "gpt-4o-mini",
                    "messages": [{
                        "role": "user",
                        "content": (
                            f"Сделай подробный и структурированный конспект (саммари) стрима на русском языке "
                            f"для монтажёра и зрителей по этим данным:\n\n{local_digest}"
                        ),
                    }],
                    "temperature": 0.5,
                },
                headers=headers,
                timeout=35,
                proxies=proxies,
            )
            resp.raise_for_status()
            llm_txt = (((resp.json().get("choices") or [{}])[0].get("message") or {}).get("content")) or ""
            if llm_txt.strip():
                local_digest = llm_txt.strip() + "\n\n---\n" + local_digest
        except Exception:
            pass

    try:
        p.with_suffix(".digest.txt").write_text(local_digest, encoding="utf-8")
    except Exception:
        pass
    return True, local_digest
