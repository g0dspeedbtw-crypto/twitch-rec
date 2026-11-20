from __future__ import annotations

import json
import logging
import os
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Dict, List, Optional, Tuple


def get_resource_dir() -> Path:
    """Directory containing bundled read-only assets (web/, assets/, bin/)."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent.parent


def get_data_dir() -> Path:
    """Writable directory for config.json and templates/ on both Windows and macOS."""
    if sys.platform == "darwin":
        d = Path.home() / "Library" / "Application Support" / "TwitchRecorderPro"
        d.mkdir(parents=True, exist_ok=True)
        return d
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        # Portable next to .exe if writable, otherwise %APPDATA%\TwitchRecorderPro
        try:
            probe = exe_dir / ".write_test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            return exe_dir
        except Exception:
            d = Path(os.environ.get("APPDATA", str(Path.home()))) / "TwitchRecorderPro"
            d.mkdir(parents=True, exist_ok=True)
            return d
    return Path(__file__).resolve().parent.parent


BASE_DIR = get_resource_dir()
DATA_DIR = get_data_dir()
APP_VERSION = "2.2.0"
DEFAULT_GITHUB_REPO = "g0dspeedbtw-crypto/twitch-rec"
CONFIG_PATH = DATA_DIR / "config.json"


def normalize_github_repo(raw: str) -> str:
    """Normalize 'https://github.com/owner/repo(.git)' or 'owner/repo' -> 'owner/repo'."""
    s = (raw or "").strip()
    if not s:
        return ""
    m = re.search(r"github\.com[:/]([a-zA-Z0-9_.\-]+/[a-zA-Z0-9_.\-]+?)(?:\.git)?/?$", s, re.IGNORECASE)
    if m:
        return m.group(1)
    m2 = re.match(r"^([a-zA-Z0-9_.\-]+/[a-zA-Z0-9_.\-]+?)(?:\.git)?/?$", s)
    if m2:
        return m2.group(1)
    return s.strip("/")
DEFAULT_RECORDINGS_DIR = (
    (Path.home() / "Movies" / "TwitchRecorderPro")
    if sys.platform == "darwin"
    else (DATA_DIR / "recordings")
)
TEMPLATES_DIR = DATA_DIR / "templates"
LOG_PATH = DATA_DIR / "app.log"
LIBRARY_CACHE_PATH = DATA_DIR / "library_cache.json"

_FILE_LOGGER: Optional[logging.Logger] = None


def get_file_logger() -> logging.Logger:
    """Return a rotating file logger (5 MB x 3 backups) inside DATA_DIR."""
    global _FILE_LOGGER
    if _FILE_LOGGER is not None:
        return _FILE_LOGGER
    logger = logging.getLogger("twitch_recorder_pro")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        try:
            handler = RotatingFileHandler(
                str(LOG_PATH), maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
            )
            handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
            logger.addHandler(handler)
        except Exception:
            pass
    _FILE_LOGGER = logger
    return logger

PLATFORMS = [
    ("Twitch", "twitch"),
    ("YouTube Live", "youtube"),
    ("Kick", "kick"),
    ("VK Play Live", "vkplay"),
    ("Trovo", "trovo"),
]


@dataclass
class BlurRegion:
    """Normalized blur/mask rectangle (coordinates 0.0..1.0 relative to video frame)."""

    x: float
    y: float
    w: float
    h: float
    mode: str = "blur"                 # "blur" | "pixelate" | "solid" | "image"
    strength: int = 25                 # sigma (blur) or block factor (pixelate)
    feather: int = 0                   # soft edge feather pixels (0..40)
    image_path: str = ""               # custom PNG/JPG logo overlay when mode == "image"
    start_sec: Optional[float] = None  # timed blur interval start (sec)
    end_sec: Optional[float] = None    # timed blur interval end (sec)
    auto_detect: bool = False          # OpenCV template detection for dynamic banners
    template_path: str = ""            # saved template crop PNG for auto_detect
    detect_threshold: float = 0.72     # template matching confidence 0.4..0.95
    label: str = "Баннер"

    def clamped(self) -> "BlurRegion":
        x = max(0.0, min(0.99, float(self.x)))
        y = max(0.0, min(0.99, float(self.y)))
        w = max(0.01, min(1.0 - x, float(self.w)))
        h = max(0.01, min(1.0 - y, float(self.h)))
        strength = max(2, min(80, int(self.strength)))
        feather = max(0, min(50, int(self.feather)))
        mode = self.mode if self.mode in ("blur", "pixelate", "solid", "image") else "blur"
        s_sec = float(self.start_sec) if self.start_sec is not None else None
        e_sec = float(self.end_sec) if self.end_sec is not None else None
        if s_sec is not None and s_sec < 0:
            s_sec = 0.0
        if e_sec is not None and s_sec is not None and e_sec <= s_sec:
            e_sec = None
        thr = max(0.35, min(0.98, float(self.detect_threshold)))
        return BlurRegion(
            x=x,
            y=y,
            w=w,
            h=h,
            mode=mode,
            strength=strength,
            feather=feather,
            image_path=str(self.image_path or ""),
            start_sec=s_sec,
            end_sec=e_sec,
            auto_detect=bool(self.auto_detect),
            template_path=str(self.template_path or ""),
            detect_threshold=thr,
            label=str(self.label or "Баннер"),
        )


@dataclass
class ChannelConfig:
    username: str
    platform: str = "twitch"            # "twitch" | "youtube" | "kick" | "vkplay" | "trovo"
    quality: str = "best"
    auto_record: bool = True
    blur_mode: str = "postprocess"      # "none" | "postprocess" | "realtime"
    disable_ads: bool = True
    keep_original: bool = False
    # Scene presets (#4): dict of preset_name -> List[BlurRegion]
    blur_presets: Dict[str, List[BlurRegion]] = field(default_factory=lambda: {"Основной": []})
    active_preset: str = "Основной"
    # Advanced recording rules (#7, #8, #9, #10, #13, #15)
    record_chat: bool = True
    auto_reconnect: bool = True
    auto_concat_parts: bool = False
    segment_minutes: int = 0            # 0 = single continuous file, >0 = split every N min
    category_filter: str = ""           # comma-separated allowed categories (empty = any)
    title_filter: str = ""              # comma-separated required keywords (empty = any)
    schedule_enabled: bool = False
    schedule_start: str = "00:00"
    schedule_end: str = "23:59"
    schedule_days: List[int] = field(default_factory=lambda: [0, 1, 2, 3, 4, 5, 6])  # Mon=0..Sun=6

    @property
    def blur_regions(self) -> List[BlurRegion]:
        if not self.blur_presets:
            self.blur_presets = {"Основной": []}
        if self.active_preset not in self.blur_presets:
            self.active_preset = next(iter(self.blur_presets))
        return self.blur_presets[self.active_preset]

    @blur_regions.setter
    def blur_regions(self, regions: List[BlurRegion]) -> None:
        if not self.blur_presets:
            self.blur_presets = {"Основной": []}
        if self.active_preset not in self.blur_presets:
            self.active_preset = "Основной"
        self.blur_presets[self.active_preset] = [r.clamped() for r in regions]

    @property
    def key(self) -> str:
        return f"{self.platform}:{self.username.lower()}"

    @property
    def stream_url(self) -> str:
        u = self.username.strip()
        if u.startswith(("http://", "https://")):
            return u
        if self.platform == "kick":
            return f"https://kick.com/{u}"
        if self.platform == "youtube":
            if u.startswith("@"):
                return f"https://www.youtube.com/{u}/live"
            if u.startswith("UC"):
                return f"https://www.youtube.com/channel/{u}/live"
            return f"https://www.youtube.com/@{u}/live"
        if self.platform == "vkplay":
            return f"https://live.vkvideo.ru/{u}"
        if self.platform == "trovo":
            return f"https://trovo.live/s/{u}"
        return f"https://twitch.tv/{u}"

    def is_within_schedule(self, now: Optional[datetime] = None) -> bool:
        if not self.schedule_enabled:
            return True
        dt = now or datetime.now()
        if dt.weekday() not in self.schedule_days:
            return False
        try:
            sh, sm = [int(x) for x in self.schedule_start.split(":")]
            eh, em = [int(x) for x in self.schedule_end.split(":")]
        except Exception:
            return True
        cur = dt.hour * 60 + dt.minute
        st = sh * 60 + sm
        en = eh * 60 + em
        if st <= en:
            return st <= cur <= en
        # Overnight window (e.g. 18:00 - 03:00)
        return cur >= st or cur <= en

    def matches_filters(self, game: str, title: str) -> bool:
        if self.category_filter.strip():
            allowed = [x.strip().lower() for x in self.category_filter.split(",") if x.strip()]
            g_low = (game or "").lower()
            if allowed and not any(a in g_low for a in allowed):
                return False
        if self.title_filter.strip():
            req = [x.strip().lower() for x in self.title_filter.split(",") if x.strip()]
            t_low = (title or "").lower()
            if req and not any(k in t_low for k in req):
                return False
        return True

    @staticmethod
    def parse_input(raw: str, default_platform: str = "twitch") -> Tuple[str, str]:
        """Parse channel URL or username into (platform, normalized_username)."""
        text = raw.strip()
        if not text:
            return default_platform, ""
        m = re.search(r"twitch\.tv/([a-zA-Z0-9_]+)", text, re.IGNORECASE)
        if m:
            return "twitch", m.group(1).lower()
        m = re.search(r"kick\.com/([a-zA-Z0-9_\-]+)", text, re.IGNORECASE)
        if m:
            return "kick", m.group(1).lower()
        m = re.search(r"(?:vkplay\.live|live\.vkplay\.ru|live\.vkvideo\.ru)/([a-zA-Z0-9_\-]+)", text, re.IGNORECASE)
        if m:
            return "vkplay", m.group(1).lower()
        m = re.search(r"trovo\.live/(?:s/)?([a-zA-Z0-9_\-]+)", text, re.IGNORECASE)
        if m:
            return "trovo", m.group(1).lower()
        m = re.search(r"youtube\.com/(@?[a-zA-Z0-9_\-\.]+)", text, re.IGNORECASE)
        if m:
            return "youtube", m.group(1)
        if "youtu.be/" in text.lower() or "youtube.com/watch" in text.lower():
            return "youtube", text
        # Plain username
        if default_platform == "youtube":
            return "youtube", text.strip()
        clean = re.sub(r"[^a-zA-Z0-9_\-]", "", text).lower()
        return default_platform, clean

    @staticmethod
    def normalize_username(raw: str) -> str:
        _, uname = ChannelConfig.parse_input(raw, "twitch")
        return uname


@dataclass
class AppConfig:
    output_dir: str = str(DEFAULT_RECORDINGS_DIR)
    check_interval_sec: int = 30
    auto_monitor_enabled: bool = True
    encoder: str = "auto"
    video_quality: int = 23
    container: str = "mp4"
    vod_concurrent_fragments: int = 4
    # Network & Auth (#12)
    twitch_oauth_token: str = ""
    proxy_url: str = ""
    # Disk protection (#14)
    min_free_gb: int = 15
    hard_disk_guard: bool = True
    auto_cleanup_old: bool = False
    max_age_days: int = 30
    # System & Tray (#19, #30, #31)
    minimize_to_tray: bool = True
    start_with_windows: bool = False
    marker_hotkey: str = "F9"
    # Notifications & Remote Control (#32, #41)
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_bot_enabled: bool = False
    discord_webhook_url: str = ""
    notify_on_live: bool = True
    notify_on_finish: bool = True
    notify_on_blur: bool = True
    # Smart GPU Post-Process Queue & Game Mode (#33, #34, #43, #45)
    max_concurrent_blurs: int = 1
    night_render_enabled: bool = False
    night_render_start: str = "02:00"
    night_render_end: str = "08:00"
    normalize_audio_default: bool = False
    low_priority_render: bool = True
    game_mode_enabled: bool = True
    game_processes: str = (
        "cs2.exe,dota2.exe,VALORANT-Win64-Shipping.exe,GTA5.exe,Cyberpunk2077.exe,"
        "FortniteClient-Win64-Shipping.exe,r5apex.exe,EscapeFromTarkov.exe,eldenring.exe,"
        "cs2,dota2,LeagueClient"
    )
    # AI, Censorship & LLM (#6, #22, #26, #30)
    whisper_model: str = "small"
    whisper_prompt: str = ""
    banned_words: str = "блять,сука,нахуй,пиздец,ебать,пидор,нигер,даун"
    llm_api_url: str = ""
    llm_api_key: str = ""
    llm_model: str = "gpt-4o-mini"
    # Chat Overlay & Watermark (#23, #32)
    chat_position: str = "right"   # "right" | "left"
    chat_font_size: int = 28
    chat_hide_bots: bool = True
    watermark_path: str = ""
    # Appearance (#48)
    theme: str = "purple"          # "purple" | "oled" | "blue" | "emerald" | "light"
    compact_cards: bool = False
    # Cloud Auto-Updates (GitHub Releases)
    github_repo: str = DEFAULT_GITHUB_REPO
    auto_check_updates: bool = True
    # Channels
    channels: List[ChannelConfig] = field(default_factory=list)

    def ensure_output_dir(self) -> Path:
        raw = (self.output_dir or "").strip()
        # Guard against a Windows drive path (e.g. C:\...) copied onto macOS/Linux
        if not raw or (os.name != "nt" and re.match(r"^[a-zA-Z]:[\\/]", raw)):
            p = DEFAULT_RECORDINGS_DIR
            self.output_dir = str(p)
        else:
            p = Path(raw)
        try:
            p.mkdir(parents=True, exist_ok=True)
            return p
        except OSError:
            fallback = DEFAULT_RECORDINGS_DIR
            fallback.mkdir(parents=True, exist_ok=True)
            self.output_dir = str(fallback)
            return fallback

    def get_channel(self, key_or_name: str) -> Optional[ChannelConfig]:
        needle = key_or_name.strip().lower()
        for ch in self.channels:
            if ch.key == needle or ch.username.lower() == needle:
                return ch
        return None

    def is_night_render_window(self, now: Optional[datetime] = None) -> bool:
        if not self.night_render_enabled:
            return True
        dt = now or datetime.now()
        try:
            sh, sm = [int(x) for x in self.night_render_start.split(":")]
            eh, em = [int(x) for x in self.night_render_end.split(":")]
        except Exception:
            return True
        cur = dt.hour * 60 + dt.minute
        st = sh * 60 + sm
        en = eh * 60 + em
        if st <= en:
            return st <= cur <= en
        return cur >= st or cur <= en

    def save(self, path: Path = CONFIG_PATH) -> None:
        data = asdict(self)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)

    @staticmethod
    def _parse_region(r: dict) -> BlurRegion:
        return BlurRegion(
            x=r.get("x", 0.0),
            y=r.get("y", 0.0),
            w=r.get("w", 0.1),
            h=r.get("h", 0.1),
            mode=r.get("mode", "blur"),
            strength=r.get("strength", 25),
            feather=r.get("feather", 0),
            image_path=r.get("image_path", ""),
            start_sec=r.get("start_sec", None),
            end_sec=r.get("end_sec", None),
            auto_detect=bool(r.get("auto_detect", False)),
            template_path=r.get("template_path", ""),
            detect_threshold=float(r.get("detect_threshold", 0.72)),
            label=r.get("label", "Баннер"),
        ).clamped()

    @classmethod
    def load(cls, path: Path = CONFIG_PATH) -> "AppConfig":
        if not path.exists():
            cfg = cls()
            cfg.ensure_output_dir()
            cfg.save(path)
            return cfg
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            cfg = cls()
            cfg.ensure_output_dir()
            return cfg

        channels: List[ChannelConfig] = []
        for ch_raw in raw.get("channels", []):
            platform = ch_raw.get("platform", "twitch")
            uname = ch_raw.get("username", "").strip()
            if not uname:
                continue
            presets: Dict[str, List[BlurRegion]] = {}
            if isinstance(ch_raw.get("blur_presets"), dict) and ch_raw["blur_presets"]:
                for pname, plist in ch_raw["blur_presets"].items():
                    presets[str(pname)] = [cls._parse_region(r) for r in (plist or [])]
            else:
                # Migrate legacy blur_regions field
                legacy = [cls._parse_region(r) for r in ch_raw.get("blur_regions", [])]
                presets = {"Основной": legacy}

            active_preset = ch_raw.get("active_preset", "Основной")
            if active_preset not in presets:
                active_preset = next(iter(presets))

            channels.append(
                ChannelConfig(
                    username=uname,
                    platform=platform,
                    quality=ch_raw.get("quality", "best"),
                    auto_record=bool(ch_raw.get("auto_record", True)),
                    blur_mode=ch_raw.get("blur_mode", "postprocess"),
                    disable_ads=bool(ch_raw.get("disable_ads", True)),
                    keep_original=bool(ch_raw.get("keep_original", False)),
                    blur_presets=presets,
                    active_preset=active_preset,
                    record_chat=bool(ch_raw.get("record_chat", True)),
                    auto_reconnect=bool(ch_raw.get("auto_reconnect", True)),
                    auto_concat_parts=bool(ch_raw.get("auto_concat_parts", False)),
                    segment_minutes=max(0, int(ch_raw.get("segment_minutes", 0))),
                    category_filter=str(ch_raw.get("category_filter", "")),
                    title_filter=str(ch_raw.get("title_filter", "")),
                    schedule_enabled=bool(ch_raw.get("schedule_enabled", False)),
                    schedule_start=str(ch_raw.get("schedule_start", "00:00")),
                    schedule_end=str(ch_raw.get("schedule_end", "23:59")),
                    schedule_days=list(ch_raw.get("schedule_days", [0, 1, 2, 3, 4, 5, 6])),
                )
            )

        cfg = cls(
            output_dir=raw.get("output_dir", str(DEFAULT_RECORDINGS_DIR)),
            check_interval_sec=max(10, int(raw.get("check_interval_sec", 30))),
            auto_monitor_enabled=bool(raw.get("auto_monitor_enabled", True)),
            encoder=raw.get("encoder", "auto"),
            video_quality=max(14, min(35, int(raw.get("video_quality", 23)))),
            container=raw.get("container", "mp4") if raw.get("container") in ("mp4", "mkv") else "mp4",
            vod_concurrent_fragments=max(1, min(16, int(raw.get("vod_concurrent_fragments", 4)))),
            twitch_oauth_token=str(raw.get("twitch_oauth_token", "")),
            proxy_url=str(raw.get("proxy_url", "")),
            min_free_gb=max(1, int(raw.get("min_free_gb", 15))),
            hard_disk_guard=bool(raw.get("hard_disk_guard", True)),
            auto_cleanup_old=bool(raw.get("auto_cleanup_old", False)),
            max_age_days=max(1, int(raw.get("max_age_days", 30))),
            minimize_to_tray=bool(raw.get("minimize_to_tray", True)),
            start_with_windows=bool(raw.get("start_with_windows", False)),
            marker_hotkey=str(raw.get("marker_hotkey", "F9")),
            telegram_bot_token=str(raw.get("telegram_bot_token", "")),
            telegram_chat_id=str(raw.get("telegram_chat_id", "")),
            telegram_bot_enabled=bool(raw.get("telegram_bot_enabled", False)),
            discord_webhook_url=str(raw.get("discord_webhook_url", "")),
            notify_on_live=bool(raw.get("notify_on_live", True)),
            notify_on_finish=bool(raw.get("notify_on_finish", True)),
            notify_on_blur=bool(raw.get("notify_on_blur", True)),
            max_concurrent_blurs=max(1, min(6, int(raw.get("max_concurrent_blurs", 1)))),
            night_render_enabled=bool(raw.get("night_render_enabled", False)),
            night_render_start=str(raw.get("night_render_start", "02:00")),
            night_render_end=str(raw.get("night_render_end", "08:00")),
            normalize_audio_default=bool(raw.get("normalize_audio_default", False)),
            low_priority_render=bool(raw.get("low_priority_render", True)),
            game_mode_enabled=bool(raw.get("game_mode_enabled", True)),
            game_processes=str(
                raw.get(
                    "game_processes",
                    "cs2.exe,dota2.exe,VALORANT-Win64-Shipping.exe,GTA5.exe,Cyberpunk2077.exe,"
                    "FortniteClient-Win64-Shipping.exe,r5apex.exe,EscapeFromTarkov.exe,eldenring.exe,"
                    "cs2,dota2,LeagueClient",
                )
            ),
            whisper_model=str(raw.get("whisper_model", "small")),
            whisper_prompt=str(raw.get("whisper_prompt", "")),
            banned_words=str(
                raw.get("banned_words", "блять,сука,нахуй,пиздец,ебать,пидор,нигер,даун")
            ),
            llm_api_url=str(raw.get("llm_api_url", "")),
            llm_api_key=str(raw.get("llm_api_key", "")),
            llm_model=str(raw.get("llm_model", "gpt-4o-mini")),
            chat_position=raw.get("chat_position", "right") if raw.get("chat_position") in ("right", "left") else "right",
            chat_font_size=max(16, min(48, int(raw.get("chat_font_size", 28)))),
            chat_hide_bots=bool(raw.get("chat_hide_bots", True)),
            watermark_path=str(raw.get("watermark_path", "")),
            theme=raw.get("theme", "purple") if raw.get("theme") in ("purple", "oled", "blue", "emerald", "light") else "purple",
            compact_cards=bool(raw.get("compact_cards", False)),
            github_repo=normalize_github_repo(str(raw.get("github_repo") or DEFAULT_GITHUB_REPO)),
            auto_check_updates=bool(raw.get("auto_check_updates", True)),
            channels=channels,
        )
        cfg.ensure_output_dir()
        return cfg
