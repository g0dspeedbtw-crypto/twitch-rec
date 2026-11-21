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


YOUTUBE_TOKEN_PATH = DATA_DIR / "youtube_token.json"
YOUTUBE_SCOPES = "https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube.readonly"


def _load_youtube_client_creds(secrets_path: str) -> Tuple[Optional[dict], str]:
    p = (secrets_path or "").strip()
    if not p or not os.path.exists(p):
        return None, "Файл client_secrets.json не выбран или не найден"
    try:
        raw = json.loads(Path(p).read_text(encoding="utf-8"))
        section = raw.get("installed") or raw.get("web") or raw
        cid = (section.get("client_id") or "").strip()
        csec = (section.get("client_secret") or "").strip()
        if not cid or not csec:
            return None, "В файле не найдены client_id / client_secret (скачайте OAuth Desktop JSON из Google Cloud Console)"
        return {
            "client_id": cid,
            "client_secret": csec,
            "auth_uri": section.get("auth_uri") or "https://accounts.google.com/o/oauth2/v2/auth",
            "token_uri": section.get("token_uri") or "https://oauth2.googleapis.com/token",
        }, ""
    except Exception as exc:
        return None, f"Ошибка чтения JSON: {exc}"


def _read_youtube_token() -> Optional[dict]:
    if not YOUTUBE_TOKEN_PATH.exists():
        return None
    try:
        return json.loads(YOUTUBE_TOKEN_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None


def _save_youtube_token(data: dict) -> None:
    try:
        YOUTUBE_TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        YOUTUBE_TOKEN_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def _fetch_youtube_channel_info(access_token: str, proxies: Optional[dict] = None) -> dict:
    try:
        r = requests.get(
            "https://www.googleapis.com/youtube/v3/channels",
            params={"part": "snippet", "mine": "true"},
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=15,
            proxies=proxies,
        )
        if r.status_code == 200:
            items = (r.json() or {}).get("items") or []
            if items:
                sn = items[0].get("snippet") or {}
                thumbs = sn.get("thumbnails") or {}
                av = (thumbs.get("default") or thumbs.get("medium") or {}).get("url", "")
                return {
                    "channel_id": items[0].get("id", ""),
                    "channel_title": sn.get("title", "YouTube Канал"),
                    "channel_handle": sn.get("customUrl", ""),
                    "avatar_url": av,
                }
    except Exception:
        pass
    return {}


def _get_valid_youtube_access_token(cfg: AppConfig) -> Tuple[Optional[str], str]:
    import time as _t
    tok = _read_youtube_token()
    if not tok:
        return None, "Аккаунт YouTube не подключён — нажмите «Войти в YouTube»"
    proxies = {"http": cfg.proxy_url, "https": cfg.proxy_url} if cfg.proxy_url else None
    now = _t.time()
    if tok.get("access_token") and float(tok.get("expires_at") or 0) > now + 90:
        return str(tok["access_token"]), ""

    refresh = tok.get("refresh_token")
    cid = tok.get("client_id")
    csec = tok.get("client_secret")
    token_uri = tok.get("token_uri") or "https://oauth2.googleapis.com/token"
    if (not cid or not csec) and getattr(cfg, "youtube_client_secrets", ""):
        loaded, _ = _load_youtube_client_creds(cfg.youtube_client_secrets)
        if loaded:
            cid, csec, token_uri = loaded["client_id"], loaded["client_secret"], loaded["token_uri"]

    if not refresh or not cid or not csec:
        return None, "Сессия YouTube истекла — войдите заново через кнопку «Войти в YouTube»"

    try:
        r = requests.post(
            token_uri,
            data={
                "client_id": cid,
                "client_secret": csec,
                "refresh_token": refresh,
                "grant_type": "refresh_token",
            },
            timeout=20,
            proxies=proxies,
        )
        if r.status_code == 200:
            new_d = r.json()
            tok["access_token"] = new_d.get("access_token", "")
            tok["expires_at"] = now + float(new_d.get("expires_in", 3500))
            _save_youtube_token(tok)
            return str(tok["access_token"]), ""
        return None, f"Не удалось обновить токен YouTube ({r.status_code}): войдите заново"
    except Exception as exc:
        return None, f"Ошибка обновления токена YouTube: {exc}"


def get_youtube_account_status(cfg: AppConfig) -> dict:
    secrets_path = (getattr(cfg, "youtube_client_secrets", "") or "").strip()
    has_secrets = bool(secrets_path and os.path.exists(secrets_path))
    tok = _read_youtube_token()
    if not tok or not (tok.get("refresh_token") or tok.get("access_token")):
        return {
            "connected": False,
            "has_secrets": has_secrets,
            "secrets_path": secrets_path,
            "channel_title": "",
            "channel_handle": "",
            "avatar_url": "",
        }
    return {
        "connected": True,
        "has_secrets": has_secrets,
        "secrets_path": secrets_path,
        "channel_title": tok.get("channel_title") or "YouTube Канал",
        "channel_handle": tok.get("channel_handle") or "",
        "avatar_url": tok.get("avatar_url") or "",
    }


def youtube_oauth_logout() -> bool:
    try:
        if YOUTUBE_TOKEN_PATH.exists():
            YOUTUBE_TOKEN_PATH.unlink()
        return True
    except Exception:
        return False


def youtube_oauth_login(cfg: AppConfig, timeout_sec: int = 180) -> Tuple[bool, str, dict]:
    """
    Pure-Python interactive Google OAuth2 Desktop login (zero extra libraries required).
    Opens the user's browser, listens on a random localhost callback port, saves refresh_token + channel info.
    """
    import time as _t
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from urllib.parse import parse_qs, urlencode, urlparse

    creds_info, err = _load_youtube_client_creds(getattr(cfg, "youtube_client_secrets", ""))
    if not creds_info:
        return False, err, {}

    result_box: Dict[str, str] = {"code": "", "error": ""}

    class _OAuthCallback(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args) -> None:
            return

        def do_GET(self) -> None:
            qs = parse_qs(urlparse(self.path).query)
            if "code" in qs:
                result_box["code"] = qs["code"][0]
                html = (
                    "<!doctype html><html><head><meta charset='utf-8'><title>YouTube подключён</title>"
                    "<style>body{background:#0f111a;color:#eef2ff;font-family:system-ui,sans-serif;display:flex;"
                    "align-items:center;justify-content:center;height:100vh;margin:0;}"
                    ".box{background:#181b2a;border:1px solid #2e3452;border-radius:16px;padding:32px 40px;text-align:center;}"
                    "h1{color:#4ade80;margin:0 0 10px;}p{color:#94a3b8;margin:0;}</style></head>"
                    "<body><div class='box'><h1>✅ Аккаунт YouTube подключён!</h1>"
                    "<p>Можете закрыть эту вкладку и вернуться в Twitch Recorder Pro.</p></div></body></html>"
                )
            else:
                result_box["error"] = qs.get("error", ["Отменено пользователем"])[0]
                html = (
                    "<!doctype html><html><head><meta charset='utf-8'><title>Ошибка входа</title></head>"
                    f"<body style='background:#0f111a;color:#f87171;font-family:system-ui;padding:40px;'>"
                    f"<h2>Ошибка авторизации: {result_box['error']}</h2></body></html>"
                )
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = HTTPServer(("127.0.0.1", 0), _OAuthCallback)
    port = server.server_address[1]
    redirect_uri = f"http://127.0.0.1:{port}"
    server.timeout = 1.0

    params = urlencode({
        "client_id": creds_info["client_id"],
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": YOUTUBE_SCOPES,
        "access_type": "offline",
        "prompt": "consent select_account",
    })
    auth_url = f"{creds_info['auth_uri']}?{params}"
    _open_url(auth_url)

    deadline = _t.time() + max(30, timeout_sec)
    try:
        while _t.time() < deadline and not result_box["code"] and not result_box["error"]:
            server.handle_request()
    finally:
        try:
            server.server_close()
        except Exception:
            pass

    if result_box["error"]:
        return False, f"Вход отменён: {result_box['error']}", {}
    if not result_box["code"]:
        return False, "Время ожидания входа в браузере истекло (3 мин)", {}

    proxies = {"http": cfg.proxy_url, "https": cfg.proxy_url} if cfg.proxy_url else None
    try:
        r = requests.post(
            creds_info["token_uri"],
            data={
                "code": result_box["code"],
                "client_id": creds_info["client_id"],
                "client_secret": creds_info["client_secret"],
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
            timeout=25,
            proxies=proxies,
        )
        if r.status_code != 200:
            return False, f"Ошибка Google OAuth ({r.status_code}): {r.text[:200]}", {}
        td = r.json()
        access_tok = td.get("access_token", "")
        if not access_tok:
            return False, "Google не вернул access_token", {}
        ch_info = _fetch_youtube_channel_info(access_tok, proxies=proxies)
        tok_payload = {
            "access_token": access_tok,
            "refresh_token": td.get("refresh_token", ""),
            "expires_at": _t.time() + float(td.get("expires_in", 3500)),
            "client_id": creds_info["client_id"],
            "client_secret": creds_info["client_secret"],
            "token_uri": creds_info["token_uri"],
            **ch_info,
        }
        _save_youtube_token(tok_payload)
        ch_name = ch_info.get("channel_title") or "YouTube Канал"
        return True, f"Аккаунт YouTube подключён: {ch_name}", get_youtube_account_status(cfg)
    except Exception as exc:
        return False, f"Ошибка обмена токена: {exc}", {}


def publish_to_youtube(
    cfg: AppConfig,
    video_path: str,
    thumb_path: str,
    title: str,
    description: str,
    hashtags: str,
    privacy: str = "unlisted",
) -> Tuple[bool, str]:
    """Upload directly via pure-Python YouTube Data API v3 Resumable Upload (zero extra packages)."""
    full_desc = f"{description}\n\n{hashtags}".strip()
    proxies = {"http": cfg.proxy_url, "https": cfg.proxy_url} if cfg.proxy_url else None

    access_token, err = _get_valid_youtube_access_token(cfg)
    if access_token and video_path and os.path.exists(video_path):
        try:
            tags = [t.lstrip("#").strip() for t in hashtags.replace(",", " ").split() if t.strip()][:25]
            file_size = os.path.getsize(video_path)
            meta = {
                "snippet": {"title": title[:100], "description": full_desc[:4900], "tags": tags, "categoryId": "20"},
                "status": {"privacyStatus": privacy or "unlisted"},
            }
            init_r = requests.post(
                "https://www.googleapis.com/upload/youtube/v3/videos",
                params={"uploadType": "resumable", "part": "snippet,status"},
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json; charset=UTF-8",
                    "X-Upload-Content-Length": str(file_size),
                    "X-Upload-Content-Type": "video/*",
                },
                data=json.dumps(meta, ensure_ascii=False).encode("utf-8"),
                timeout=30,
                proxies=proxies,
            )
            if init_r.status_code not in (200, 201):
                return False, f"YouTube API init ошибка ({init_r.status_code}): {init_r.text[:200]}"
            upload_url = init_r.headers.get("Location")
            if not upload_url:
                return False, "YouTube API не вернул URL загрузки"

            chunk_size = 8 * 1024 * 1024
            vid_id = ""
            with open(video_path, "rb") as vf:
                offset = 0
                while offset < file_size:
                    chunk = vf.read(chunk_size)
                    if not chunk:
                        break
                    end_byte = offset + len(chunk) - 1
                    ur = requests.put(
                        upload_url,
                        headers={
                            "Content-Length": str(len(chunk)),
                            "Content-Range": f"bytes {offset}-{end_byte}/{file_size}",
                            "Content-Type": "video/*",
                        },
                        data=chunk,
                        timeout=300,
                        proxies=proxies,
                    )
                    if ur.status_code in (200, 201):
                        vid_id = (ur.json() or {}).get("id", "")
                        break
                    elif ur.status_code == 308:
                        offset += len(chunk)
                        continue
                    else:
                        return False, f"YouTube upload ошибка ({ur.status_code}): {ur.text[:200]}"

            if not vid_id:
                return False, "YouTube не вернул ID загруженного видео"

            if thumb_path and os.path.exists(thumb_path):
                try:
                    with open(thumb_path, "rb") as tf:
                        requests.post(
                            "https://www.googleapis.com/upload/youtube/v3/thumbnails/set",
                            params={"videoId": vid_id, "uploadType": "media"},
                            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "image/jpeg"},
                            data=tf.read(),
                            timeout=60,
                            proxies=proxies,
                        )
                except Exception:
                    pass
            return True, f"https://youtu.be/{vid_id}"
        except Exception as exc:
            return False, f"YouTube API: {exc}"

    _reveal_file(video_path)
    _open_url("https://studio.youtube.com/")
    return True, "YouTube Studio открыт (чтобы заливать автоматически — нажмите «Войти в YouTube»)"


def publish_to_tiktok(video_path: str) -> Tuple[bool, str]:
    _reveal_file(video_path)
    _open_url("https://www.tiktok.com/tiktokstudio/upload")
    return True, "TikTok Studio открыт, вертикальный файл выделен в папке (описание и хэштеги скопированы)"
