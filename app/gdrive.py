"""
Native Pure-Python Google Drive OAuth2 & Resumable Upload API v3 (v5.2.0).
Zero external Google packages required — works out of the box on macOS & Windows.
Supports multi-gigabyte video uploads in 8 MB chunks, auto token refresh,
automatic folder/subfolder creation, and safe local file cleanup ONLY after confirmed 200/201 upload.
"""
from __future__ import annotations

import json
import os
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlencode, urlparse

import requests

from app.config import AppConfig, DATA_DIR

GDRIVE_TOKEN_PATH = DATA_DIR / "gdrive_token.json"
GDRIVE_SCOPES = "https://www.googleapis.com/auth/drive.file https://www.googleapis.com/auth/userinfo.email"


def _resolve_secrets_path(cfg: AppConfig) -> str:
    p = (getattr(cfg, "gdrive_client_secrets", "") or "").strip()
    if p and os.path.exists(p):
        return p
    # Automatically reuse YouTube OAuth client_secrets.json if user already configured it!
    yt_p = (getattr(cfg, "youtube_client_secrets", "") or "").strip()
    if yt_p and os.path.exists(yt_p):
        return yt_p
    return p or yt_p


def _load_client_creds(secrets_path: str) -> Tuple[Optional[dict], str]:
    p = (secrets_path or "").strip()
    if not p or not os.path.exists(p):
        return None, "Файл client_secrets.json не выбран (подойдёт тот же JSON, что и для YouTube)"
    try:
        raw = json.loads(Path(p).read_text(encoding="utf-8"))
        sec = raw.get("installed") or raw.get("web") or raw
        cid = (sec.get("client_id") or "").strip()
        csec = (sec.get("client_secret") or "").strip()
        if not cid or not csec:
            return None, "В JSON не найдены client_id / client_secret"
        return {
            "client_id": cid,
            "client_secret": csec,
            "auth_uri": sec.get("auth_uri") or "https://accounts.google.com/o/oauth2/v2/auth",
            "token_uri": sec.get("token_uri") or "https://oauth2.googleapis.com/token",
        }, ""
    except Exception as exc:
        return None, f"Ошибка чтения JSON: {exc}"


def _read_token() -> Optional[dict]:
    if not GDRIVE_TOKEN_PATH.exists():
        return None
    try:
        return json.loads(GDRIVE_TOKEN_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None


def _save_token(data: dict) -> None:
    try:
        GDRIVE_TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        GDRIVE_TOKEN_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def _fetch_user_email(access_token: str, proxies: Optional[dict] = None) -> str:
    try:
        r = requests.get(
            "https://www.googleapis.com/oauth2/v2/userinfo",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=15,
            proxies=proxies,
        )
        if r.status_code == 200:
            return str((r.json() or {}).get("email") or "")
    except Exception:
        pass
    return ""


def get_gdrive_status(cfg: AppConfig) -> dict:
    sp = _resolve_secrets_path(cfg)
    has_secrets = bool(sp and os.path.exists(sp))
    tok = _read_token()
    connected = bool(tok and (tok.get("refresh_token") or tok.get("access_token")))
    return {
        "connected": connected,
        "email": (tok or {}).get("email") or ("Google Диск" if connected else ""),
        "has_secrets": has_secrets,
        "secrets_path": sp,
        "folder_name": getattr(cfg, "gdrive_folder_name", "TwitchRecorderPro") or "TwitchRecorderPro",
        "enabled": bool(getattr(cfg, "gdrive_enabled", False)),
        "delete_local": bool(getattr(cfg, "gdrive_delete_local_after", True)),
    }


def gdrive_oauth_logout() -> bool:
    try:
        if GDRIVE_TOKEN_PATH.exists():
            GDRIVE_TOKEN_PATH.unlink()
        return True
    except Exception:
        return False


def gdrive_oauth_login(cfg: AppConfig, timeout_sec: int = 180) -> Tuple[bool, str, dict]:
    creds_info, err = _load_client_creds(_resolve_secrets_path(cfg))
    if not creds_info:
        return False, err, get_gdrive_status(cfg)

    result_box: Dict[str, str] = {"code": "", "error": ""}

    class _Callback(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args) -> None:
            return

        def do_GET(self) -> None:
            qs = parse_qs(urlparse(self.path).query)
            if "code" in qs:
                result_box["code"] = qs["code"][0]
                html = (
                    "<!doctype html><html><head><meta charset='utf-8'><title>Google Диск подключён</title>"
                    "<style>body{background:#09090b;color:#f4f4f5;font-family:system-ui,sans-serif;display:flex;"
                    "align-items:center;justify-content:center;height:100vh;margin:0;}"
                    ".box{background:#121216;border:1px solid rgba(255,255,255,0.1);border-radius:16px;padding:32px 40px;text-align:center;}"
                    "h1{color:#4ade80;margin:0 0 10px;}p{color:#a1a1aa;margin:0;}</style></head>"
                    "<body><div class='box'><h1>✅ Google Диск подключён!</h1>"
                    "<p>Можно закрыть эту вкладку и вернуться в Twitch Recorder Pro.</p></div></body></html>"
                )
            else:
                result_box["error"] = qs.get("error", ["Отменено пользователем"])[0]
                html = (
                    "<!doctype html><html><head><meta charset='utf-8'></head>"
                    f"<body style='background:#09090b;color:#f87171;font-family:system-ui;padding:40px;'>"
                    f"<h2>Ошибка входа: {result_box['error']}</h2></body></html>"
                )
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = HTTPServer(("127.0.0.1", 0), _Callback)
    port = server.server_address[1]
    redirect_uri = f"http://127.0.0.1:{port}"
    server.timeout = 1.0

    params = urlencode({
        "client_id": creds_info["client_id"],
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": GDRIVE_SCOPES,
        "access_type": "offline",
        "prompt": "consent select_account",
    })
    try:
        webbrowser.open(f"{creds_info['auth_uri']}?{params}")
    except Exception:
        pass

    deadline = time.time() + max(30, timeout_sec)
    try:
        while time.time() < deadline and not result_box["code"] and not result_box["error"]:
            server.handle_request()
    finally:
        try:
            server.server_close()
        except Exception:
            pass

    if result_box["error"]:
        return False, f"Вход отменён: {result_box['error']}", get_gdrive_status(cfg)
    if not result_box["code"]:
        return False, "Время ожидания входа истекло (3 мин)", get_gdrive_status(cfg)

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
            return False, f"Google OAuth ошибка ({r.status_code}): {r.text[:180]}", get_gdrive_status(cfg)
        td = r.json()
        access_tok = td.get("access_token", "")
        if not access_tok:
            return False, "Google не вернул access_token", get_gdrive_status(cfg)
        email = _fetch_user_email(access_tok, proxies=proxies)
        _save_token({
            "access_token": access_tok,
            "refresh_token": td.get("refresh_token", ""),
            "expires_at": time.time() + float(td.get("expires_in", 3500)),
            "client_id": creds_info["client_id"],
            "client_secret": creds_info["client_secret"],
            "token_uri": creds_info["token_uri"],
            "email": email or "Google Диск",
        })
        return True, f"Google Диск подключён ({email or 'OK'})", get_gdrive_status(cfg)
    except Exception as exc:
        return False, f"Ошибка входа в Google Диск: {exc}", get_gdrive_status(cfg)


def _get_valid_access_token(cfg: AppConfig) -> Tuple[Optional[str], str]:
    tok = _read_token()
    if not tok:
        return None, "Google Диск не подключён — нажмите «Войти в Google Диск» в Настройках"
    proxies = {"http": cfg.proxy_url, "https": cfg.proxy_url} if cfg.proxy_url else None
    now = time.time()
    if tok.get("access_token") and float(tok.get("expires_at") or 0) > now + 90:
        return str(tok["access_token"]), ""

    refresh = tok.get("refresh_token")
    cid = tok.get("client_id")
    csec = tok.get("client_secret")
    token_uri = tok.get("token_uri") or "https://oauth2.googleapis.com/token"
    if not refresh or not cid or not csec:
        return None, "Сессия Google Диска истекла — войдите заново"

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
            nd = r.json()
            tok["access_token"] = nd.get("access_token", "")
            tok["expires_at"] = now + float(nd.get("expires_in", 3500))
            _save_token(tok)
            return str(tok["access_token"]), ""
        return None, f"Ошибка обновления токена Google Диска ({r.status_code})"
    except Exception as exc:
        return None, f"Ошибка сети Google Диска: {exc}"


def _find_or_create_folder(
    access_token: str,
    name: str,
    parent_id: Optional[str] = None,
    proxies: Optional[dict] = None,
) -> Optional[str]:
    safe_name = name.replace("'", "\\'").strip() or "TwitchRecorderPro"
    q = f"name = '{safe_name}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    if parent_id:
        q += f" and '{parent_id}' in parents"
    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        r = requests.get(
            "https://www.googleapis.com/drive/v3/files",
            params={"q": q, "fields": "files(id,name)", "pageSize": 5},
            headers=headers,
            timeout=20,
            proxies=proxies,
        )
        if r.status_code == 200:
            files = (r.json() or {}).get("files") or []
            if files:
                return str(files[0]["id"])
        meta: dict = {"name": name.strip() or "TwitchRecorderPro", "mimeType": "application/vnd.google-apps.folder"}
        if parent_id:
            meta["parents"] = [parent_id]
        cr = requests.post(
            "https://www.googleapis.com/drive/v3/files",
            headers={**headers, "Content-Type": "application/json"},
            json=meta,
            timeout=20,
            proxies=proxies,
        )
        if cr.status_code in (200, 201):
            return str((cr.json() or {}).get("id") or "")
    except Exception:
        pass
    return None


def upload_file_to_gdrive(
    cfg: AppConfig,
    file_path: str,
    subfolder: str = "",
    delete_local: bool = False,
    progress_cb: Optional[Callable[[int, str], None]] = None,
) -> Tuple[bool, str, str]:
    """
    Uploads a file (supports multi-GB videos) to Google Drive via Resumable Upload API v3 (8 MB chunks).
    If delete_local=True, deletes the local file ONLY after Google Drive confirms upload (200/201 + file_id).
    Returns (ok, message, drive_url).
    """
    p = Path(file_path)
    if not p.exists() or not p.is_file():
        return False, f"Файл не найден: {file_path}", ""

    access_token, err = _get_valid_access_token(cfg)
    if not access_token:
        return False, err, ""

    proxies = {"http": cfg.proxy_url, "https": cfg.proxy_url} if cfg.proxy_url else None
    file_size = p.stat().st_size
    if file_size <= 0:
        return False, "Файл пуст (0 байт)", ""

    if progress_cb:
        progress_cb(2, "Подготовка папки на Google Диске...")

    root_name = (getattr(cfg, "gdrive_folder_name", "TwitchRecorderPro") or "TwitchRecorderPro").strip()
    target_parent = _find_or_create_folder(access_token, root_name, None, proxies=proxies)
    if target_parent and subfolder.strip():
        sub_id = _find_or_create_folder(access_token, subfolder.strip(), target_parent, proxies=proxies)
        if sub_id:
            target_parent = sub_id

    meta: dict = {"name": p.name}
    if target_parent:
        meta["parents"] = [target_parent]

    try:
        init_r = requests.post(
            "https://www.googleapis.com/upload/drive/v3/files",
            params={"uploadType": "resumable", "fields": "id,name,webViewLink"},
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json; charset=UTF-8",
                "X-Upload-Content-Length": str(file_size),
                "X-Upload-Content-Type": "application/octet-stream",
            },
            data=json.dumps(meta, ensure_ascii=False).encode("utf-8"),
            timeout=30,
            proxies=proxies,
        )
        if init_r.status_code not in (200, 201):
            return False, f"Google Drive init ошибка ({init_r.status_code}): {init_r.text[:200]}", ""
        upload_url = init_r.headers.get("Location")
        if not upload_url:
            return False, "Google Drive не вернул URL загрузки", ""

        chunk_size = 8 * 1024 * 1024
        file_id = ""
        web_link = ""
        with open(p, "rb") as f:
            offset = 0
            while offset < file_size:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                end_byte = offset + len(chunk) - 1
                ur = requests.put(
                    upload_url,
                    headers={
                        "Content-Length": str(len(chunk)),
                        "Content-Range": f"bytes {offset}-{end_byte}/{file_size}",
                        "Content-Type": "application/octet-stream",
                    },
                    data=chunk,
                    timeout=300,
                    proxies=proxies,
                )
                if ur.status_code in (200, 201):
                    jd = ur.json() or {}
                    file_id = str(jd.get("id") or "")
                    web_link = str(jd.get("webViewLink") or (f"https://drive.google.com/file/d/{file_id}/view" if file_id else ""))
                    break
                elif ur.status_code == 308:
                    offset += len(chunk)
                    if progress_cb:
                        pct = min(98, int((offset / file_size) * 100))
                        mb_done = offset / (1024 * 1024)
                        mb_tot = file_size / (1024 * 1024)
                        progress_cb(pct, f"Google Диск: {pct}% ({mb_done:.0f} / {mb_tot:.0f} МБ)")
                    continue
                else:
                    return False, f"Ошибка загрузки чанка Google Drive ({ur.status_code}): {ur.text[:180]}", ""

        if not file_id:
            return False, "Google Drive не подтвердил сохранение файла — локальная копия сохранена", ""

        deleted_note = ""
        if delete_local:
            try:
                p.unlink()
                deleted_note = " · локальный файл очищен с ПК"
            except Exception as exc:
                deleted_note = f" (не удалось удалить локально: {exc})"

        if progress_cb:
            progress_cb(100, "Загружено на Google Диск!")
        return True, f"Загружено на Google Диск: {p.name}{deleted_note}", web_link
    except Exception as exc:
        return False, f"Ошибка загрузки на Google Диск: {exc}", ""
