from __future__ import annotations

import json
import os
import subprocess
import sys
import webbrowser
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests

from app.config import AppConfig, DATA_DIR


def _open_url(url: str) -> None:
    try:
        webbrowser.open(url)
    except Exception:
        pass


def _reveal_file(path: str) -> None:
    if not path or not os.path.exists(path):
        return
    try:
        if os.name == "nt":
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", path])
        else:
            subprocess.Popen(["xdg-open", str(Path(path).parent)])
    except Exception:
        pass


def publish_to_telegram(
    cfg: AppConfig,
    video_path: str,
    thumb_path: str,
    title: str,
    description: str,
    hashtags: str,
) -> Tuple[bool, str]:
    tok = (cfg.telegram_bot_token or "").strip()
    cid = (cfg.telegram_chat_id or "").strip()
    if not tok or not cid:
        return False, "Telegram: укажите Bot Token и Chat ID в Настройках"
    proxies = {"http": cfg.proxy_url, "https": cfg.proxy_url} if cfg.proxy_url else None
    caption = f"🔥 {title}\n\n{description}\n\n{hashtags}".strip()[:1020]

    # If video <= 49 MB, upload full video directly via sendVideo
    if video_path and os.path.exists(video_path) and os.path.getsize(video_path) <= 49 * 1024 * 1024:
        url = f"https://api.telegram.org/bot{tok}/sendVideo"
        files = {}
        try:
            with open(video_path, "rb") as vf:
                files["video"] = (Path(video_path).name, vf, "video/mp4")
                tf = open(thumb_path, "rb") if (thumb_path and os.path.exists(thumb_path)) else None
                try:
                    if tf:
                        files["thumbnail"] = (Path(thumb_path).name, tf, "image/jpeg")
                    r = requests.post(
                        url,
                        data={"chat_id": cid, "caption": caption, "supports_streaming": "true"},
                        files=files,
                        timeout=180,
                        proxies=proxies,
                    )
                finally:
                    if tf:
                        tf.close()
            if r.status_code == 200 and r.json().get("ok"):
                return True, "Telegram: видео успешно опубликовано!"
            return False, f"Telegram ошибка: {r.text[:180]}"
        except Exception as exc:
            return False, f"Telegram: {exc}"

    # Video > 49 MB: send thumbnail + full post caption (Telegram Bot API limit is 50 MB)
    if thumb_path and os.path.exists(thumb_path):
        try:
            url = f"https://api.telegram.org/bot{tok}/sendPhoto"
            with open(thumb_path, "rb") as tf:
                r = requests.post(
                    url,
                    data={"chat_id": cid, "caption": caption},
                    files={"photo": (Path(thumb_path).name, tf, "image/jpeg")},
                    timeout=45,
                    proxies=proxies,
                )
            if r.status_code == 200 and r.json().get("ok"):
                return True, "Telegram: обложка и пост опубликованы (видео > 50 МБ — сожмите в 50 МБ для прямой отправки)"
            return False, f"Telegram ошибка: {r.text[:160]}"
        except Exception as exc:
            return False, f"Telegram: {exc}"

    try:
        url = f"https://api.telegram.org/bot{tok}/sendMessage"
        r = requests.post(url, json={"chat_id": cid, "text": caption}, timeout=20, proxies=proxies)
        if r.status_code == 200:
            return True, "Telegram: текстовый пост опубликован!"
        return False, f"Telegram ошибка: {r.text[:160]}"
    except Exception as exc:
        return False, f"Telegram: {exc}"


def publish_to_discord(
    cfg: AppConfig,
    video_path: str,
    thumb_path: str,
    title: str,
    description: str,
    hashtags: str,
) -> Tuple[bool, str]:
    wh = (cfg.discord_webhook_url or "").strip()
    if not wh:
        return False, "Discord: укажите Webhook URL в Настройках"
    proxies = {"http": cfg.proxy_url, "https": cfg.proxy_url} if cfg.proxy_url else None
    content = f"**{title}**\n{description[:1200]}\n\n{hashtags}".strip()[:1950]

    attach_path = ""
    if video_path and os.path.exists(video_path) and os.path.getsize(video_path) <= 24 * 1024 * 1024:
        attach_path = video_path
    elif thumb_path and os.path.exists(thumb_path):
        attach_path = thumb_path

    try:
        if attach_path:
            with open(attach_path, "rb") as f:
                r = requests.post(
                    wh,
                    data={"payload_json": json.dumps({"content": content}, ensure_ascii=False)},
                    files={"file": (Path(attach_path).name, f)},
                    timeout=90,
                    proxies=proxies,
                )
        else:
            r = requests.post(wh, json={"content": content}, timeout=20, proxies=proxies)
        if r.status_code in (200, 204):
            return True, "Discord: пост успешно отправлен!"
        return False, f"Discord ошибка ({r.status_code}): {r.text[:160]}"
    except Exception as exc:
        return False, f"Discord: {exc}"


def publish_to_vk(
    cfg: AppConfig,
    video_path: str,
    title: str,
    description: str,
    hashtags: str,
    is_vertical: bool = False,
) -> Tuple[bool, str]:
    vk_tok = (getattr(cfg, "vk_access_token", "") or "").strip()
    vk_gid = (getattr(cfg, "vk_group_id", "") or "").strip()
    full_desc = f"{description}\n\n{hashtags}".strip()

    if vk_tok and video_path and os.path.exists(video_path):
        try:
            params = {
                "name": title[:128],
                "description": full_desc[:4000],
                "wallpost": 1,
                "access_token": vk_tok,
                "v": "5.199",
            }
            if vk_gid:
                params["group_id"] = vk_gid.lstrip("-")
            r = requests.get("https://api.vk.com/method/video.save", params=params, timeout=20)
            data = r.json()
            if "response" in data and data["response"].get("upload_url"):
                up_url = data["response"]["upload_url"]
                with open(video_path, "rb") as vf:
                    ur = requests.post(up_url, files={"video_file": (Path(video_path).name, vf, "video/mp4")}, timeout=300)
                if ur.status_code == 200:
                    return True, "VK: видео успешно загружено и опубликовано!"
                return False, f"VK upload ошибка: {ur.text[:160]}"
            return False, f"VK API ошибка: {str(data.get('error', data))[:180]}"
        except Exception as exc:
            return False, f"VK: {exc}"

    # Guided 1-click studio fallback
    _reveal_file(video_path)
    _open_url("https://vk.com/clips" if is_vertical else "https://vk.com/video")
    return True, "VK: открыт VK Видео / Клипы и выделен файл (текст скопирован — вставьте Ctrl+V)"


def publish_to_youtube(
    cfg: AppConfig,
    video_path: str,
    thumb_path: str,
    title: str,
    description: str,
    hashtags: str,
    privacy: str = "unlisted",
) -> Tuple[bool, str]:
    """Upload directly via YouTube Data API v3 if client_secrets.json + token exist, else open YouTube Studio."""
    secrets_path = (getattr(cfg, "youtube_client_secrets", "") or "").strip()
    full_desc = f"{description}\n\n{hashtags}".strip()

    if secrets_path and os.path.exists(secrets_path) and video_path and os.path.exists(video_path):
        try:
            from google.oauth2.credentials import Credentials
            from google_auth_oauthlib.flow import InstalledAppFlow
            from googleapiclient.discovery import build
            from googleapiclient.http import MediaFileUpload

            scopes = ["https://www.googleapis.com/auth/youtube.upload"]
            tok_file = DATA_DIR / "youtube_token.json"
            creds = None
            if tok_file.exists():
                creds = Credentials.from_authorized_user_file(str(tok_file), scopes)
            if not creds or not creds.valid:
                flow = InstalledAppFlow.from_client_secrets_file(secrets_path, scopes)
                creds = flow.run_local_server(port=0)
                tok_file.write_text(creds.to_json(), encoding="utf-8")

            yt = build("youtube", "v3", credentials=creds)
            tags = [t.lstrip("#").strip() for t in hashtags.replace(",", " ").split() if t.strip()][:25]
            body = {
                "snippet": {"title": title[:100], "description": full_desc[:4900], "tags": tags, "categoryId": "20"},
                "status": {"privacyStatus": privacy or "unlisted"},
            }
            media = MediaFileUpload(video_path, chunksize=8 * 1024 * 1024, resumable=True)
            req = yt.videos().insert(part="snippet,status", body=body, media_body=media)
            resp = None
            while resp is None:
                _, resp = req.next_chunk()
            vid_id = resp.get("id", "")
            if vid_id and thumb_path and os.path.exists(thumb_path):
                try:
                    yt.thumbnails().set(videoId=vid_id, media_body=MediaFileUpload(thumb_path)).execute()
                except Exception:
                    pass
            return True, f"YouTube: видео загружено! https://youtu.be/{vid_id}"
        except ImportError:
            pass
        except Exception as exc:
            return False, f"YouTube API: {exc}"

    _reveal_file(video_path)
    _open_url("https://studio.youtube.com/")
    return True, "YouTube Studio открыт, файл выделен в папке (заголовок, описание и теги скопированы)"


def publish_to_tiktok(video_path: str) -> Tuple[bool, str]:
    _reveal_file(video_path)
    _open_url("https://www.tiktok.com/tiktokstudio/upload")
    return True, "TikTok Studio открыт, вертикальный файл выделен в папке (описание и хэштеги скопированы)"
