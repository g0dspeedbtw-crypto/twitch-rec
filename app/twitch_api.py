from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import requests

from app.config import ChannelConfig
from app.tools import CREATE_NO_WINDOW, get_ffmpeg_path, get_streamlink_cmd, get_ytdlp_cmd

TWITCH_GQL_URL = "https://gql.twitch.tv/gql"
TWITCH_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"


@dataclass
class StreamStatus:
    key: str
    username: str
    platform: str = "twitch"
    is_live: bool = False
    title: str = ""
    game: str = ""
    viewers: int = 0
    preview_url: str = ""
    error: str = ""


@dataclass
class VodItem:
    vod_id: str
    title: str
    url: str
    duration_sec: int
    views: int
    created_at: str
    game: str
    thumbnail_url: str


@dataclass
class ClipItem:
    clip_id: str
    slug: str
    title: str
    url: str
    duration_sec: float
    views: int
    created_at: str
    game: str
    thumbnail_url: str


def _gql_headers(oauth_token: str = "") -> Dict[str, str]:
    h = {
        "Client-ID": TWITCH_CLIENT_ID,
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }
    if oauth_token.strip():
        tok = oauth_token.strip()
        if not tok.lower().startswith("oauth:"):
            h["Authorization"] = f"OAuth {tok}"
        else:
            h["Authorization"] = f"OAuth {tok[6:]}"
    return h


def _requests_proxies(proxy_url: str = "") -> Optional[Dict[str, str]]:
    if proxy_url.strip():
        return {"http": proxy_url.strip(), "https": proxy_url.strip()}
    return None


# ------------------------------------------------------------------ #29 Multi-Platform Live Checker
def check_channels_status(
    channels: List[ChannelConfig],
    timeout: int = 10,
    oauth_token: str = "",
    proxy_url: str = "",
) -> Dict[str, StreamStatus]:
    """Batch-check live status across Twitch, Kick, YouTube Live, VK Play Live, and Trovo."""
    results: Dict[str, StreamStatus] = {}
    twitch_chs: List[ChannelConfig] = []
    other_chs: List[ChannelConfig] = []

    for ch in channels:
        results[ch.key] = StreamStatus(key=ch.key, username=ch.username, platform=ch.platform)
        if ch.platform == "twitch":
            twitch_chs.append(ch)
        else:
            other_chs.append(ch)

    proxies = _requests_proxies(proxy_url)

    # 1. Fast batched Twitch GQL
    if twitch_chs:
        headers = _gql_headers(oauth_token)
        query_str = """
        query($login: String!) {
          user(login: $login) {
            login
            stream {
              id
              title
              viewersCount
              game { name }
              previewImageURL(width: 640, height: 360)
            }
          }
        }
        """
        for start in range(0, len(twitch_chs), 25):
            chunk = twitch_chs[start : start + 25]
            payload = [{"query": query_str, "variables": {"login": c.username}} for c in chunk]
            try:
                resp = requests.post(
                    TWITCH_GQL_URL, json=payload, headers=headers, timeout=timeout, proxies=proxies
                )
                resp.raise_for_status()
                for c, item in zip(chunk, resp.json()):
                    user_obj = (item.get("data") or {}).get("user")
                    if not user_obj:
                        results[c.key] = StreamStatus(
                            key=c.key, username=c.username, platform="twitch", is_live=False, error="Канал не найден"
                        )
                        continue
                    stream_obj = user_obj.get("stream")
                    if stream_obj:
                        game_obj = stream_obj.get("game") or {}
                        results[c.key] = StreamStatus(
                            key=c.key,
                            username=c.username,
                            platform="twitch",
                            is_live=True,
                            title=stream_obj.get("title") or "",
                            game=game_obj.get("name") or "",
                            viewers=int(stream_obj.get("viewersCount") or 0),
                            preview_url=stream_obj.get("previewImageURL")
                            or f"https://static-cdn.jtvnw.net/previews-ttv/live_user_{c.username}-640x360.jpg",
                        )
                    else:
                        results[c.key] = StreamStatus(
                            key=c.key, username=c.username, platform="twitch", is_live=False
                        )
            except Exception as exc:
                for c in chunk:
                    results[c.key].error = f"Ошибка сети: {exc}"

    # 2. Other platforms (Kick, YouTube, VK Play, Trovo)
    sl_cmd = get_streamlink_cmd()
    for c in other_chs:
        if c.platform == "kick":
            try:
                r = requests.get(
                    f"https://kick.com/api/v2/channels/{c.username}",
                    headers={"User-Agent": "Mozilla/5.0"},
                    timeout=timeout,
                    proxies=proxies,
                )
                if r.status_code == 200:
                    data = r.json()
                    ls = data.get("livestream")
                    if ls and ls.get("is_live"):
                        cats = ls.get("categories") or []
                        thumb = (ls.get("thumbnail") or {}).get("url") or ""
                        results[c.key] = StreamStatus(
                            key=c.key,
                            username=c.username,
                            platform="kick",
                            is_live=True,
                            title=ls.get("session_title") or "",
                            game=cats[0].get("name", "") if cats else "",
                            viewers=int(ls.get("viewer_count") or 0),
                            preview_url=thumb,
                        )
                        continue
                    results[c.key] = StreamStatus(key=c.key, username=c.username, platform="kick", is_live=False)
                    continue
            except Exception:
                pass

        # Generic check via streamlink --json (works for YouTube Live, Kick, VK Play, Trovo!)
        if sl_cmd:
            try:
                cmd = [*sl_cmd, "--json", c.stream_url]
                if proxy_url.strip():
                    cmd.extend(["--http-proxy", proxy_url.strip()])
                res = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=14, creationflags=CREATE_NO_WINDOW
                )
                if res.returncode == 0 and res.stdout.strip():
                    info = json.loads(res.stdout)
                    has_streams = bool(info.get("streams"))
                    meta = info.get("metadata") or {}
                    results[c.key] = StreamStatus(
                        key=c.key,
                        username=c.username,
                        platform=c.platform,
                        is_live=has_streams,
                        title=str(meta.get("title") or ""),
                        game=str(meta.get("category") or ""),
                    )
                else:
                    results[c.key] = StreamStatus(key=c.key, username=c.username, platform=c.platform, is_live=False)
            except Exception as exc:
                results[c.key].error = str(exc)[:60]

    return results


# ------------------------------------------------------------------ #11 Live Thumbnail Fetcher
def download_preview_bytes(url: str, proxy_url: str = "") -> Optional[bytes]:
    if not url:
        return None
    try:
        # Append cache-buster so Twitch serves the freshest frame
        sep = "&" if "?" in url else "?"
        r = requests.get(f"{url}{sep}t={os.getpid()}", timeout=8, proxies=_requests_proxies(proxy_url))
        if r.status_code == 200 and len(r.content) > 1500:
            return r.content
    except Exception:
        pass
    return None


# ------------------------------------------------------------------ #26 Twitch VOD & Clips Browser
def fetch_channel_vods(username: str, limit: int = 20, oauth_token: str = "", proxy_url: str = "") -> Tuple[List[VodItem], str]:
    """Fetch recent Twitch VODs (past broadcasts) for a channel via GQL."""
    uname = ChannelConfig.normalize_username(username)
    if not uname:
        return [], "Укажите имя канала Twitch"

    query = """
    query($login: String!, $first: Int!) {
      user(login: $login) {
        videos(first: $first, type: ARCHIVE, sort: TIME) {
          edges {
            node {
              id
              title
              lengthSeconds
              viewCount
              createdAt
              previewThumbnailURL(width: 320, height: 180)
              game { name }
            }
          }
        }
      }
    }
    """
    try:
        resp = requests.post(
            TWITCH_GQL_URL,
            json={"query": query, "variables": {"login": uname, "first": limit}},
            headers=_gql_headers(oauth_token),
            timeout=12,
            proxies=_requests_proxies(proxy_url),
        )
        resp.raise_for_status()
        user = (resp.json().get("data") or {}).get("user")
        if not user:
            return [], f"Канал «{uname}» не найден"
        edges = ((user.get("videos") or {}).get("edges")) or []
        items: List[VodItem] = []
        for edge in edges:
            n = edge.get("node") or {}
            vid = str(n.get("id") or "")
            if not vid:
                continue
            items.append(
                VodItem(
                    vod_id=vid,
                    title=str(n.get("title") or "Без названия"),
                    url=f"https://www.twitch.tv/videos/{vid}",
                    duration_sec=int(n.get("lengthSeconds") or 0),
                    views=int(n.get("viewCount") or 0),
                    created_at=str(n.get("createdAt") or "")[:10],
                    game=str((n.get("game") or {}).get("name") or ""),
                    thumbnail_url=str(n.get("previewThumbnailURL") or ""),
                )
            )
        return items, ""
    except Exception as exc:
        return [], f"Ошибка загрузки списка VOD: {exc}"


def fetch_channel_clips(
    username: str,
    period: str = "LAST_WEEK",
    limit: int = 24,
    oauth_token: str = "",
    proxy_url: str = "",
) -> Tuple[List[ClipItem], str]:
    """Fetch top Twitch Clips for a channel (LAST_DAY, LAST_WEEK, LAST_MONTH, ALL_TIME)."""
    uname = ChannelConfig.normalize_username(username)
    if not uname:
        return [], "Укажите имя канала Twitch"

    allowed_periods = {"LAST_DAY", "LAST_WEEK", "LAST_MONTH", "ALL_TIME"}
    safe_period = period if period in allowed_periods else "LAST_WEEK"

    # Twitch GQL uses ClipsPeriod enum directly in criteria
    query_direct = f"""
    query($login: String!, $first: Int!) {{
      user(login: $login) {{
        clips(first: $first, criteria: {{ period: {safe_period} }}) {{
          edges {{
            node {{
              id
              slug
              title
              url
              durationSeconds
              viewCount
              createdAt
              thumbnailURL
              game {{ name }}
            }}
          }}
        }}
      }}
    }}
    """
    try:
        resp = requests.post(
            TWITCH_GQL_URL,
            json={"query": query_direct, "variables": {"login": uname, "first": limit}},
            headers=_gql_headers(oauth_token),
            timeout=12,
            proxies=_requests_proxies(proxy_url),
        )
        resp.raise_for_status()
        user = (resp.json().get("data") or {}).get("user")
        if not user:
            return [], f"Канал «{uname}» не найден"
        edges = ((user.get("clips") or {}).get("edges")) or []
        items: List[ClipItem] = []
        for edge in edges:
            n = edge.get("node") or {}
            slug = str(n.get("slug") or "")
            if not slug:
                continue
            items.append(
                ClipItem(
                    clip_id=str(n.get("id") or slug),
                    slug=slug,
                    title=str(n.get("title") or "Без названия"),
                    url=str(n.get("url") or f"https://clips.twitch.tv/{slug}"),
                    duration_sec=float(n.get("durationSeconds") or 0),
                    views=int(n.get("viewCount") or 0),
                    created_at=str(n.get("createdAt") or "")[:10],
                    game=str((n.get("game") or {}).get("name") or ""),
                    thumbnail_url=str(n.get("thumbnailURL") or ""),
                )
            )
        return items, ""
    except Exception as exc:
        return [], f"Ошибка загрузки клипов: {exc}"


# ------------------------------------------------------------------ Snapshots
def capture_live_snapshot(
    channel: ChannelConfig | str,
    output_jpg: str,
    quality: str = "best",
    proxy_url: str = "",
) -> Tuple[bool, str]:
    if isinstance(channel, str):
        ch = ChannelConfig(username=channel.strip().lower(), platform="twitch")
    else:
        ch = channel

    streamlink_cmd = get_streamlink_cmd()
    ffmpeg = get_ffmpeg_path()

    if streamlink_cmd and ffmpeg:
        # Method 1: Resolve HLS stream URL via Streamlink and extract 1 frame via FFmpeg
        try:
            cmd = [*streamlink_cmd, "--stream-url"]
            if ch.platform == "twitch":
                cmd.append("--twitch-disable-ads")
            if proxy_url.strip():
                cmd.extend(["--http-proxy", proxy_url.strip()])
            cmd.extend([ch.stream_url, f"{quality},best" if quality != "best" else "best"])
            url_res = subprocess.run(cmd, capture_output=True, text=True, timeout=18, creationflags=CREATE_NO_WINDOW)
            stream_url = [line.strip() for line in (url_res.stdout or "").splitlines() if line.strip().startswith("http")]
            if url_res.returncode == 0 and stream_url:
                hls_url = stream_url[-1]
                ff_res = subprocess.run(
                    [
                        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                        "-i", hls_url, "-frames:v", "1", "-q:v", "2", output_jpg,
                    ],
                    capture_output=True, text=True, timeout=20, creationflags=CREATE_NO_WINDOW,
                )
                if ff_res.returncode == 0 and os.path.exists(output_jpg) and os.path.getsize(output_jpg) > 1000:
                    return True, "Кадр захвачен напрямую со стрима (1080p)"
        except Exception:
            pass

        # Method 2: Pipe Streamlink stdout directly into FFmpeg stdin (bypasses any FFmpeg HTTPS/TLS issues)
        try:
            sl_args = [*streamlink_cmd, "--stdout", "--quiet"]
            if ch.platform == "twitch":
                sl_args.append("--twitch-disable-ads")
            if proxy_url.strip():
                sl_args.extend(["--http-proxy", proxy_url.strip()])
            sl_args.extend([ch.stream_url, f"{quality},best" if quality != "best" else "best"])
            sl_proc = subprocess.Popen(
                sl_args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW
            )
            try:
                ff_proc = subprocess.Popen(
                    [
                        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                        "-i", "pipe:0", "-frames:v", "1", "-q:v", "2", output_jpg,
                    ],
                    stdin=sl_proc.stdout, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                    creationflags=CREATE_NO_WINDOW,
                )
                if sl_proc.stdout:
                    sl_proc.stdout.close()
                ff_proc.wait(timeout=18)
            finally:
                try:
                    sl_proc.kill()
                except Exception:
                    pass
            if os.path.exists(output_jpg) and os.path.getsize(output_jpg) > 1000:
                return True, "Кадр захвачен напрямую со стрима"
        except Exception:
            pass

    if ch.platform == "twitch":
        try:
            preview_url = f"https://static-cdn.jtvnw.net/previews-ttv/live_user_{ch.username.lower()}-1920x1080.jpg"
            r = requests.get(
                preview_url,
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=10,
                proxies=_requests_proxies(proxy_url),
                allow_redirects=True,
            )
            # Note: Twitch redirects offline channels to 404_preview-1920x1080.jpg (~2-4KB)
            if r.status_code == 200 and len(r.content) > 5000 and "404_preview" not in (r.url or ""):
                with open(output_jpg, "wb") as f:
                    f.write(r.content)
                return True, "Загружен живой кадр прямого эфира Twitch"
        except Exception:
            pass

    return False, f"Канал {ch.username} сейчас оффлайн или не удалось захватить кадр"


def capture_vod_snapshot(
    vod_url: str,
    output_jpg: str,
    timestamp_sec: float = 30.0,
    oauth_token: str = "",
    proxy_url: str = "",
) -> Tuple[bool, str]:
    ytdlp_cmd = get_ytdlp_cmd()
    ffmpeg = get_ffmpeg_path()
    if not ytdlp_cmd or not ffmpeg:
        return False, "Не найден yt-dlp или ffmpeg"

    try:
        cmd = [*ytdlp_cmd, "-g", "-f", "best"]
        if proxy_url.strip():
            cmd.extend(["--proxy", proxy_url.strip()])
        if oauth_token.strip():
            tok = oauth_token.strip()
            tok = tok[6:] if tok.lower().startswith("oauth:") else tok
            cmd.extend(["--add-header", f"Authorization: OAuth {tok}"])
        cmd.append(vod_url.strip())
        res = subprocess.run(
            cmd,
            capture_output=True, text=True, timeout=25, creationflags=CREATE_NO_WINDOW,
        )
        urls = [line.strip() for line in (res.stdout or "").splitlines() if line.strip()]
        if res.returncode != 0 or not urls:
            return False, (res.stderr or "Не удалось получить ссылку на поток VOD").strip()

        ff = subprocess.run(
            [
                ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                "-ss", f"{max(0.0, timestamp_sec):.2f}", "-i", urls[0],
                "-frames:v", "1", "-q:v", "2", output_jpg,
            ],
            capture_output=True, text=True, timeout=30, creationflags=CREATE_NO_WINDOW,
        )
        if ff.returncode == 0 and os.path.exists(output_jpg) and os.path.getsize(output_jpg) > 0:
            return True, "Кадр извлечён из VOD"
        return False, (ff.stderr or "FFmpeg не смог извлечь кадр из VOD").strip()
    except Exception as exc:
        return False, str(exc)


def parse_vod_id(url: str) -> Optional[str]:
    m = re.search(r"twitch\.tv/videos/(\d+)", url, re.IGNORECASE)
    if m:
        return m.group(1)
    if url.strip().isdigit():
        return url.strip()
    return None
