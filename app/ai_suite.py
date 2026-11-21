from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests

from app.config import AppConfig, BlurRegion


def format_duration(sec: float) -> str:
    s = max(0, int(sec or 0))
    h, rem = divmod(s, 3600)
    m, r = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{r:02d}" if h > 0 else f"{m:02d}:{r:02d}"
from app.tools import (
    CREATE_NO_WINDOW,
    concat_videos,
    encoder_ffmpeg_args,
    extract_frame_from_file,
    get_ffmpeg_path,
    probe_video,
    resolve_encoder,
    trim_video_fast,
)


# ------------------------------------------------------------------ Local AI Auto-Discovery (Ollama / LM Studio — Zero API!)
def detect_local_llm_status() -> dict:
    """Detect local Ollama (11434) or LM Studio (1234) so AI works 100% locally without any API keys."""
    ollama_bin = shutil.which("ollama")
    if not ollama_bin and sys.platform == "darwin":
        for cand in ("/usr/local/bin/ollama", "/opt/homebrew/bin/ollama", "/Applications/Ollama.app/Contents/Resources/ollama"):
            if os.path.exists(cand):
                ollama_bin = cand
                break
    status = {
        "ollama_installed": bool(ollama_bin),
        "ollama_running": False,
        "lmstudio_running": False,
        "models": [],
        "active_url": "",
        "active_model": "",
        "engine_label": "Встроенный оффлайн AI (без API)",
    }
    try:
        r = requests.get("http://127.0.0.1:11434/api/tags", timeout=1.5)
        if r.status_code == 200:
            models = [m.get("name", "") for m in (r.json().get("models") or []) if m.get("name")]
            status["ollama_running"] = True
            status["models"] = models
            if models:
                status["active_url"] = "http://127.0.0.1:11434/v1"
                status["active_model"] = models[0]
                status["engine_label"] = f"Локальный Ollama ({models[0]})"
                return status
    except Exception:
        pass
    try:
        r2 = requests.get("http://127.0.0.1:1234/v1/models", timeout=1.2)
        if r2.status_code == 200:
            models = [m.get("id", "") for m in (r2.json().get("data") or []) if m.get("id")]
            status["lmstudio_running"] = True
            status["models"] = models
            if models:
                status["active_url"] = "http://127.0.0.1:1234/v1"
                status["active_model"] = models[0]
                status["engine_label"] = f"Локальный LM Studio ({models[0]})"
    except Exception:
        pass
    return status


# ------------------------------------------------------------------ Shared LLM / Vision Helper
def _call_llm_text(
    cfg: AppConfig,
    system_prompt: str,
    user_prompt: str,
    image_b64: Optional[str] = None,
    timeout: int = 45,
) -> Optional[str]:
    """Call configured LLM API OR automatically fall back to local Ollama / LM Studio (zero API keys!)."""
    api_url = (getattr(cfg, "llm_api_url", "") or "").strip()
    api_key = (getattr(cfg, "llm_api_key", "") or "").strip()
    model = (getattr(cfg, "llm_model", "") or "gpt-4o-mini").strip()

    if not api_url:
        loc = detect_local_llm_status()
        if loc["active_url"] and loc["active_model"]:
            api_url = loc["active_url"]
            pref = (getattr(cfg, "local_ai_model", "") or "").strip()
            model = pref if (pref and pref in loc["models"]) else loc["active_model"]
            api_key = "local"
        else:
            return None
    endpoint = api_url.rstrip("/")
    if not endpoint.endswith("/chat/completions"):
        endpoint += "/chat/completions"

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    brand = (getattr(cfg, "ai_brand_voice", "") or "").strip()
    if brand:
        system_prompt = f"{system_prompt}\nСтиль канала (Brand Voice): {brand}"

    if image_b64:
        user_content: Any = [
            {"type": "text", "text": user_prompt},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
        ]
    else:
        user_content = user_prompt

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0.7,
    }
    proxies = {"http": cfg.proxy_url, "https": cfg.proxy_url} if getattr(cfg, "proxy_url", "") else None
    try:
        r = requests.post(endpoint, headers=headers, json=payload, timeout=timeout, proxies=proxies)
        if r.status_code == 200:
            data = r.json()
            return str(data["choices"][0]["message"]["content"]).strip()
    except Exception:
        pass
    return None


def _load_sidecar_text(video_path: str) -> Tuple[str, List[dict], List[dict]]:
    """Load existing transcript/subtitles text, markers, and chat messages if present."""
    p = Path(video_path)
    speech_lines: List[str] = []
    for ext in (".srt", ".ass", ".digest.txt"):
        cand = p.with_suffix(ext)
        if cand.exists():
            try:
                raw = cand.read_text(encoding="utf-8", errors="ignore")
                if ext == ".ass":
                    for ln in raw.splitlines():
                        if ln.startswith("Dialogue:"):
                            parts = ln.split(",", 9)
                            if len(parts) >= 10:
                                txt = re.sub(r"\{[^}]*\}", "", parts[9]).strip()
                                if txt:
                                    speech_lines.append(f"[{parts[1]}] {txt}")
                else:
                    speech_lines.append(raw[:6000])
                break
            except Exception:
                pass

    markers: List[dict] = []
    mk_p = p.with_suffix(".markers.json")
    if mk_p.exists():
        try:
            data = json.loads(mk_p.read_text(encoding="utf-8"))
            if isinstance(data, list):
                markers = data
        except Exception:
            pass

    chat_msgs: List[dict] = []
    ch_p = p.with_suffix(".chat.json")
    if ch_p.exists():
        try:
            data = json.loads(ch_p.read_text(encoding="utf-8"))
            if isinstance(data, list):
                chat_msgs = data
        except Exception:
            pass

    return "\n".join(speech_lines)[:7000], markers, chat_msgs


# ------------------------------------------------------------------ #1 AI Viral Clips Finder (OpusClip-style)
def find_viral_clips(
    video_path: str,
    chat_path: str = "",
    cfg: Optional[AppConfig] = None,
    max_clips: int = 8,
) -> List[dict]:
    """Combine chat spikes, audio vocal/emotion bursts, and markers into scored viral clips (0..100)."""
    from app.chat_and_ai import analyze_chat_highlights, detect_audio_events, detect_loud_moments

    candidates: List[Tuple[float, float, str]] = []  # (ts, raw_score, source)

    # 1. Manual / trigger-word markers (highest precision)
    speech_txt, markers, _ = _load_sidecar_text(video_path)
    for m in markers:
        t = float(m.get("t", 0))
        note = str(m.get("note") or "Маркер стрима")
        candidates.append((t, 92.0, f"Маркер: {note}"))

    # 2. Chat hype spikes
    cpath = chat_path if (chat_path and os.path.exists(chat_path)) else str(Path(video_path).with_suffix(".chat.json"))
    if os.path.exists(cpath):
        try:
            _, chat_hls = analyze_chat_highlights(cpath)
            for idx, h in enumerate(chat_hls[:10]):
                candidates.append((float(h.timestamp_sec), max(72.0, 95.0 - idx * 3.0), h.reason))
        except Exception:
            pass

    # 3. Audio emotion bursts (laughter / scream / gunshot)
    try:
        _, ev_hls = detect_audio_events(video_path)
        for idx, h in enumerate(ev_hls[:8]):
            candidates.append((float(h.timestamp_sec), max(68.0, 88.0 - idx * 2.5), h.reason))
    except Exception:
        pass

    # 4. General loud peaks fallback
    if len(candidates) < 3:
        try:
            _, au_hls = detect_loud_moments(video_path)
            for idx, h in enumerate(au_hls[:6]):
                candidates.append((float(h.timestamp_sec), max(62.0, 82.0 - idx * 3.0), h.reason))
        except Exception:
            pass

    # Cluster candidates within 25s window and boost multi-signal overlaps!
    candidates.sort(key=lambda x: x[0])
    clustered: List[dict] = []
    for ts, sc, reason in candidates:
        merged = False
        for cl in clustered:
            if abs(cl["timestamp_sec"] - ts) <= 25.0:
                cl["score"] = min(99, int(max(cl["score"], sc) + 6))
                if reason not in cl["reasons"]:
                    cl["reasons"].append(reason)
                merged = True
                break
        if not merged:
            clustered.append({
                "timestamp_sec": round(ts, 1),
                "start_sec": round(max(0.0, ts - 18.0), 1),
                "end_sec": round(ts + 22.0, 1),
                "score": min(99, int(sc)),
                "reasons": [reason],
                "title": "",
            })

    clustered.sort(key=lambda x: x["score"], reverse=True)
    top = clustered[:max_clips]

    # Generate catchy titles
    llm_titles: List[str] = []
    if cfg and top:
        prompt = (
            f"Придумай ровно {len(top)} коротких вирусных заголовков (до 50 символов каждый, на русском, без нумерации) "
            f"для Shorts/TikTok клипов со стрима {Path(video_path).stem}.\n"
            f"Моменты: " + "; ".join(f"{format_duration(c['timestamp_sec'])} ({', '.join(c['reasons'])})" for c in top)
        )
        if speech_txt:
            prompt += f"\nФрагмент речи:\n{speech_txt[:1500]}"
        ans = _call_llm_text(cfg, "Ты опытный редактор вирусных Shorts и TikTok.", prompt)
        if ans:
            llm_titles = [re.sub(r"^\d+[\.\)\-\s]+", "", ln).strip() for ln in ans.splitlines() if ln.strip()]

    fallback_titles = [
        "ЭТО БЫЛО БЕЗУМИЕ! 🔥",
        "ЧАТ ПРОСТО ВЗОРВАЛСЯ 😂",
        "ЛУЧШИЙ МОМЕНТ СТРИМА 🏆",
        "НИКТО ЭТОГО НЕ ОЖИДАЛ 😱",
        "ЖЕСТКАЯ РЕАКЦИЯ В ПРЯМОМ ЭФИРЕ 💀",
        "ТОП ХАЙЛАЙТ ДНЯ ⚡",
        "ЭПИЧНЫЙ КЛАТЧ / ФЕЙЛ 🎯",
        "СМОТРЕТЬ ДО КОНЦА! 🔥",
    ]
    for i, c in enumerate(top):
        c["title"] = llm_titles[i] if i < len(llm_titles) else fallback_titles[i % len(fallback_titles)]
        c["reason"] = " + ".join(c["reasons"][:2])
    return top


# ------------------------------------------------------------------ #2 AI Active Face Tracking (Shorts 9:16)
def detect_face_crop_region(video_path: str) -> Tuple[bool, dict, str]:
    """Use OpenCV Haar Cascade to locate the streamer's face and compute an optimal webcam crop box."""
    try:
        import cv2
        import numpy as np
    except Exception:
        return False, {}, "OpenCV недоступен"

    cascade_path = os.path.join(cv2.data.haarcascades, "haarcascade_frontalface_default.xml")
    if not os.path.exists(cascade_path):
        return False, {}, "Модель детекции лиц не найдена"
    detector = cv2.CascadeClassifier(cascade_path)

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return False, {}, "Не удалось открыть видео"

    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 1920)
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 1080)
    samples = 16
    step = max(1, total // samples) if total > samples else 1

    boxes: List[Tuple[float, float, float, float]] = []
    for i in range(samples):
        cap.set(cv2.CAP_PROP_POS_FRAMES, min(max(0, total - 1), i * step))
        ok, frame = cap.read()
        if not ok or frame is None:
            continue
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        faces = detector.detectMultiScale(gray, scaleFactor=1.15, minNeighbors=5, minSize=(48, 48))
        if len(faces) > 0:
            # Pick largest face
            fx, fy, fw, fh = max(faces, key=lambda b: b[2] * b[3])
            # Expand nicely around face for webcam framing (2.4x width, 2.6x height)
            cx = (fx + fw / 2.0) / w
            cy = (fy + fh / 2.0) / h
            rw = min(0.55, max(0.20, (fw * 2.5) / w))
            rh = min(0.55, max(0.24, (fh * 2.7) / h))
            rx = max(0.0, min(1.0 - rw, cx - rw / 2.0))
            ry = max(0.0, min(1.0 - rh, cy - rh / 2.0))
            boxes.append((rx, ry, rw, rh))
    cap.release()

    if not boxes:
        return False, {}, "Лицо стримера не обнаружено на кадрах (попробуйте выделить вручную)"

    arr = np.array(boxes)
    med = np.median(arr, axis=0)
    reg = {
        "x": round(float(med[0]), 4),
        "y": round(float(med[1]), 4),
        "w": round(float(med[2]), 4),
        "h": round(float(med[3]), 4),
        "mode": "blur",
        "strength": 10,
        "label": "1. AI Вебка (Face Track)",
    }
    return True, reg, f"Лицо найдено на {len(boxes)}/{samples} кадрах"


# ------------------------------------------------------------------ #3 AI Filler Words & Pause Remover (Descript-style)
FILLER_WORDS = {
    "ээ", "эээ", "мм", "ммм", "аа", "ааа", "ну", "типа", "короче", "вот", "какбы",
    "значит", "блин", "um", "uh", "erm", "like", "you know",
}


def detect_filler_and_pause_cuts(
    video_path: str,
    model_size: str = "small",
    language: str = "ru",
    min_pause_sec: float = 1.1,
) -> Tuple[bool, List[Tuple[float, float]], List[dict], str]:
    """Transcribe word timestamps and return (ok, keep_segments, removed_items, message)."""
    dur = probe_video(video_path).get("duration", 0.0)
    if dur <= 1.0:
        return False, [], [], "Не удалось определить длительность видео"
    try:
        from faster_whisper import WhisperModel
    except Exception:
        return False, [], [], "Установите faster-whisper для удаления слов-паразитов"

    try:
        model = WhisperModel(model_size or "small", device="auto", compute_type="int8")
        segments, _ = model.transcribe(
            video_path,
            language=None if language == "auto" else language,
            word_timestamps=True,
            vad_filter=True,
        )
        words = []
        for seg in segments:
            for w in getattr(seg, "words", []) or []:
                words.append({"word": (w.word or "").strip(), "start": float(w.start), "end": float(w.end)})
    except Exception as exc:
        return False, [], [], f"Ошибка Whisper: {exc}"

    if not words:
        return False, [], [], "Речь не обнаружена"

    cut_ranges: List[Tuple[float, float]] = []
    removed_items: List[dict] = []

    prev_end = 0.0
    for w in words:
        clean = re.sub(r"[^a-zа-яё0-9]", "", w["word"].lower())
        # Long pause before word
        if w["start"] - prev_end >= min_pause_sec:
            cut_ranges.append((prev_end + 0.18, w["start"] - 0.12))
            removed_items.append({"t": round(prev_end, 1), "type": "pause", "text": f"Пауза {w['start'] - prev_end:.1f}с"})
        # Filler word
        if clean in FILLER_WORDS and (w["end"] - w["start"]) <= 1.2:
            cut_ranges.append((max(0.0, w["start"] - 0.04), min(dur, w["end"] + 0.04)))
            removed_items.append({"t": round(w["start"], 1), "type": "filler", "text": w["word"]})
        prev_end = max(prev_end, w["end"])

    if not cut_ranges:
        return True, [(0.0, dur)], [], "Слов-паразитов и длинных пауз не найдено!"

    cut_ranges.sort()
    merged: List[Tuple[float, float]] = [cut_ranges[0]]
    for s, e in cut_ranges[1:]:
        if s <= merged[-1][1] + 0.15:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))

    keeps: List[Tuple[float, float]] = []
    cur = 0.0
    for cs, ce in merged:
        if cs - cur >= 0.35:
            keeps.append((round(cur, 2), round(cs, 2)))
        cur = max(cur, ce)
    if dur - cur >= 0.35:
        keeps.append((round(cur, 2), round(dur, 2)))

    return True, keeps, removed_items[:80], f"Найдено {len(removed_items)} пауз/слов-паразитов"


# ------------------------------------------------------------------ #4 AI Speech Search Across All Recordings
def search_library_speech(output_dir: str, query: str) -> List[dict]:
    """Search subtitles (.srt/.ass), chat logs (.chat.json), markers, and digests across the library."""
    q = (query or "").strip().lower()
    if not q or not output_dir or not os.path.isdir(output_dir):
        return []
    root = Path(output_dir)
    hits: List[dict] = []

    def _find_video(stem_path: Path) -> Optional[str]:
        for vext in (".mp4", ".mkv", ".ts", ".mov", ".webm"):
            cand = stem_path.with_suffix(vext)
            if cand.exists():
                return str(cand)
        return None

    for p in root.rglob("*"):
        if not p.is_file():
            continue
        low = p.name.lower()
        if low.endswith(".srt"):
            vpath = _find_video(p)
            if not vpath:
                continue
            try:
                blocks = p.read_text(encoding="utf-8", errors="ignore").split("\n\n")
                for b in blocks:
                    if q in b.lower():
                        lines = [ln.strip() for ln in b.splitlines() if ln.strip()]
                        tm = re.search(r"(\d+):(\d+):(\d+)", b)
                        ts = int(tm.group(1)) * 3600 + int(tm.group(2)) * 60 + int(tm.group(3)) if tm else 0
                        snippet = " ".join(lines[2:]) if len(lines) >= 3 else b[:120]
                        hits.append({"video_path": vpath, "name": Path(vpath).name, "ts": ts, "source": "Речь (SRT)", "text": snippet[:160]})
                        if len(hits) >= 40:
                            return hits
            except Exception:
                pass
        elif low.endswith(".ass") and not low.endswith(".chat.ass"):
            vpath = _find_video(p)
            if not vpath:
                continue
            try:
                for ln in p.read_text(encoding="utf-8", errors="ignore").splitlines():
                    if ln.startswith("Dialogue:") and q in ln.lower():
                        parts = ln.split(",", 9)
                        if len(parts) >= 10:
                            tm = re.match(r"(\d+):(\d+):(\d+)", parts[1].strip())
                            ts = int(tm.group(1)) * 3600 + int(tm.group(2)) * 60 + int(tm.group(3)) if tm else 0
                            txt = re.sub(r"\{[^}]*\}", "", parts[9]).strip()
                            if q in txt.lower():
                                hits.append({"video_path": vpath, "name": Path(vpath).name, "ts": ts, "source": "Речь (ASS)", "text": txt[:160]})
                                if len(hits) >= 40:
                                    return hits
            except Exception:
                pass
        elif low.endswith(".chat.json"):
            base = Path(str(p)[:-len(".chat.json")])
            vpath = _find_video(base)
            if not vpath:
                continue
            try:
                msgs = json.loads(p.read_text(encoding="utf-8", errors="ignore"))
                for m in msgs:
                    txt = str(m.get("text") or "")
                    if q in txt.lower():
                        hits.append({
                            "video_path": vpath,
                            "name": Path(vpath).name,
                            "ts": int(float(m.get("t", 0))),
                            "source": f"Чат ({m.get('user', 'зритель')})",
                            "text": txt[:160],
                        })
                        if len(hits) >= 40:
                            return hits
            except Exception:
                pass
    return hits


# ------------------------------------------------------------------ #5 AI Auto-Blur Faces & Private Data
def detect_privacy_regions(video_path: str) -> Tuple[bool, List[dict], str]:
    """Detect faces in video frames and return BlurRegion dicts ready for Blur Studio."""
    ok, reg, msg = detect_face_crop_region(video_path)
    if ok:
        reg["mode"] = "blur"
        reg["strength"] = 35
        reg["feather"] = 14
        reg["label"] = "AI Приватность (Лицо)"
        return True, [reg], msg
    return False, [], msg


# ------------------------------------------------------------------ #6 AI Voice Isolation / DMCA Safe Audio
def isolate_voice_dmca_safe(input_path: str, output_path: str) -> Tuple[bool, str]:
    """Suppress background music/noise and isolate clean streamer voice (DMCA-safer)."""
    ffmpeg = get_ffmpeg_path()
    if not ffmpeg:
        return False, "FFmpeg не найден"
    af = (
        "highpass=f=110,lowpass=f=7800,"
        "afftdn=nf=-28,"
        "acompressor=threshold=-22dB:ratio=4:attack=8:release=110,"
        "loudnorm=I=-14:TP=-1.5:LRA=11"
    )
    cmd = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-i", input_path,
        "-c:v", "copy",
        "-af", af, "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart", output_path,
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=1800, creationflags=CREATE_NO_WINDOW)
    if res.returncode == 0 and os.path.exists(output_path):
        return True, output_path
    return False, (res.stderr or "Ошибка изоляции голоса").strip()


# ------------------------------------------------------------------ #7 AI Semantic YouTube Chapters
def generate_ai_chapters(video_path: str, cfg: AppConfig) -> str:
    """Generate clean YouTube chapters (00:00 ...) from transcript, markers, and chat spikes."""
    speech, markers, chat_msgs = _load_sidecar_text(video_path)
    dur = probe_video(video_path).get("duration", 0.0)
    lines = ["00:00 Вступление и начало эфира"]

    if cfg and (speech or markers):
        prompt = (
            f"Создай аккуратные главы (таймкоды YouTube формата ММ:СС Название) для стрима длительностью {format_duration(dur)}.\n"
            f"Маркеры: {json.dumps(markers[:20], ensure_ascii=False)}\n"
            f"Фрагмент речи:\n{speech[:3500]}\n"
            "Верни ТОЛЬКО список строк вида `00:00 Название главы` (6–12 глав)."
        )
        ans = _call_llm_text(cfg, "Ты профессиональный YouTube-продюсер.", prompt)
        if ans and "00:00" in ans:
            return ans

    for m in markers[:15]:
        lines.append(f"{format_duration(float(m.get('t', 0)))} {m.get('note') or 'Интересный момент'}")
    if len(lines) == 1 and dur > 180:
        for i, frac in enumerate((0.2, 0.45, 0.7, 0.9), 1):
            lines.append(f"{format_duration(dur * frac)} Ключевой момент #{i}")
    return "\n".join(lines)


# ------------------------------------------------------------------ #8 AI Studio Editing Copilot Chat
def run_ai_copilot(prompt: str, video_path: str, cfg: AppConfig) -> dict:
    """Natural-language AI copilot inside Studio returning advice + executable 1-click actions."""
    q = (prompt or "").strip()
    low = q.lower()
    actions: List[dict] = []

    if any(k in low for k in ("вирус", "shorts", "шорт", "тикток", "tiktok", "клип", "смешн")):
        actions.append({"id": "viral_clips", "label": "🔥 Найти вирусные клипы (OpusClip)"})
        actions.append({"id": "shorts", "label": "📱 Создать Shorts 9:16"})
    if any(k in low for k in ("субтитр", "текст", "караоке", "перевод")):
        actions.append({"id": "subtitles", "label": "✨ Сделать караоке-субтитры с эмодзи"})
    if any(k in low for k in ("тишин", "пауз", "afk", "паразит", "скучн")):
        actions.append({"id": "remove_afk", "label": "⚡ Удалить тишину и AFK"})
        actions.append({"id": "remove_fillers", "label": "🧹 Удалить слова-паразиты"})
    if any(k in low for k in ("облож", "превью", "thumb", "кадр")):
        actions.append({"id": "best_frames", "label": "🖼 Подобрать 5 лучших кадров для обложки"})
        actions.append({"id": "vision_thumb", "label": "🤖 AI идеи заголовков по кадру"})
    if any(k in low for k in ("разб", "част", "30", "час")):
        actions.append({"id": "split_30", "label": "✂ Разбить по 30 минут"})
        actions.append({"id": "split_60", "label": "✂ Разбить по 1 часу"})
    if any(k in low for k in ("мат", "бан", "цензур", "запик", "щит")):
        actions.append({"id": "ban_shield", "label": "🛡 Проверить на бан-ворды"})
    if any(k in low for k in ("глав", "таймкод", "youtube", "ютуб", "пост", "телеграм")):
        actions.append({"id": "chapters", "label": "📑 Сгенерировать главы YouTube"})
        actions.append({"id": "social_post", "label": "📣 Написать пост для Telegram"})
    if any(k in low for k in ("монтаж", "лучшее", "нарезк", "10 мин")):
        actions.append({"id": "auto_montage", "label": "🎬 Авто-монтаж «Лучшее за стрим»"})
    if any(k in low for k in ("музык", "dmca", "голос", "шум")):
        actions.append({"id": "voice_iso", "label": "🎙 Изолировать голос (DMCA Safe)"})

    if not actions:
        actions = [
            {"id": "viral_clips", "label": "🔥 Найти вирусные клипы"},
            {"id": "chapters", "label": "📑 Главы YouTube"},
            {"id": "best_frames", "label": "🖼 Лучшие кадры для обложки"},
        ]

    reply = _call_llm_text(
        cfg,
        "Ты встроенный AI-ассистент видеоредактора Twitch Recorder Pro. Отвечай кратко, по делу, на русском (2–4 предложения) и советуй лучшие шаги монтажа.",
        f"Видео: {Path(video_path).name if video_path else 'не выбрано'}.\nЗапрос пользователя: {q}",
    )
    if not reply:
        reply = (
            f"Понял задачу! Для файла «{Path(video_path).name if video_path else 'видео'}» я подготовил быстрые действия ниже — "
            "нажмите нужную кнопку, чтобы запустить обработку в 1 клик."
        )
    return {"reply": reply, "actions": actions}


# ------------------------------------------------------------------ #9 AI Vision Thumbnail Ideas
def analyze_frame_for_thumbnail(video_path: str, ts: float, cfg: AppConfig) -> dict:
    """Analyze a video frame via Vision LLM (or OpenCV fallback) and suggest 5 clickbait thumbnail headlines."""
    tmp = Path(tempfile.gettempdir()) / f"tr_vis_{os.getpid()}.jpg"
    ok, _ = extract_frame_from_file(video_path, ts, str(tmp))
    b64 = None
    accent = "#8b5cf6"
    if ok and tmp.exists():
        b64 = base64.b64encode(tmp.read_bytes()).decode("ascii")
        try:
            import cv2
            import numpy as np
            img = cv2.imread(str(tmp))
            if img is not None:
                hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
                mean_h = float(np.mean(hsv[:, :, 0]))
                if mean_h < 20 or mean_h > 160:
                    accent = "#ef4444"
                elif 35 <= mean_h <= 85:
                    accent = "#22c55e"
                elif 95 <= mean_h <= 130:
                    accent = "#3b82f6"
                else:
                    accent = "#f59e0b"
        except Exception:
            pass

    ideas: List[dict] = []
    if b64:
        ans = _call_llm_text(
            cfg,
            "Ты дизайнер кликбейтных обложек YouTube. Посмотри на кадр со стрима и предложи 5 коротких взрывных заголовков (2–4 слова, КАПСОМ) и короткую плашку-подзаголовок.",
            "Верни 5 строк формата: ЗАГОЛОВОК | ПОДЗАГОЛОВОК",
            image_b64=b64,
        )
        if ans:
            for ln in ans.splitlines():
                if "|" in ln:
                    h_txt, s_txt = [x.strip(" -0123456789.)") for x in ln.split("|", 1)]
                    if h_txt:
                        ideas.append({"headline": h_txt[:42], "subtitle": s_txt[:32] or "TWITCH HIGHLIGHTS", "accent": accent})

    if not ideas:
        stem = Path(video_path).stem.upper()[:20]
        ideas = [
            {"headline": "ЭТО ПРОСТО БЕЗУМИЕ!", "subtitle": "ЛУЧШИЙ МОМЕНТ СТРИМА", "accent": "#ef4444"},
            {"headline": "КАК ОН ЭТО СДЕЛАЛ?!", "subtitle": stem or "TWITCH HIGHLIGHTS", "accent": accent},
            {"headline": "ЧАТ В ПОЛНОМ ШОКЕ!", "subtitle": "ПРЯМОЙ ЭФИР", "accent": "#8b5cf6"},
            {"headline": "НЕ ПОВЕРИТЕ ГЛАЗАМ", "subtitle": "СМОТРЕТЬ ДО КОНЦА", "accent": "#f59e0b"},
            {"headline": "ЛЕГЕНДАРНЫЙ МОМЕНТ", "subtitle": "ТОП КЛИП ДНЯ", "accent": "#22c55e"},
        ]
    try:
        if tmp.exists():
            tmp.unlink()
    except Exception:
        pass
    return {"ideas": ideas[:5], "accent": accent}


# ------------------------------------------------------------------ #10 AI Ban-Word Shield
def scan_ban_words(video_path: str, banned_words_csv: str, model_size: str = "small", language: str = "ru") -> Tuple[bool, List[dict], str]:
    """Scan audio with Whisper for Twitch/YouTube ban-words and return exact timestamps."""
    words_set = [w.strip().lower() for w in (banned_words_csv or "").split(",") if w.strip()]
    if not words_set:
        return False, [], "Список запрещённых слов пуст (заполните в Настройках или Студии)"
    try:
        from faster_whisper import WhisperModel
    except Exception:
        return False, [], "Модуль faster-whisper не установлен"
    try:
        model = WhisperModel(model_size or "small", device="auto", compute_type="int8")
        segments, _ = model.transcribe(video_path, language=None if language == "auto" else language, word_timestamps=True, vad_filter=True)
        hits: List[dict] = []
        for seg in segments:
            for w in getattr(seg, "words", []) or []:
                raw = (w.word or "").strip()
                low = re.sub(r"[^a-zа-яё0-9]", "", raw.lower())
                if any(bw in low for bw in words_set):
                    hits.append({"t": round(float(w.start), 2), "end": round(float(w.end), 2), "word": raw})
        return True, hits, f"Найдено совпадений: {len(hits)}"
    except Exception as exc:
        return False, [], str(exc)


# ------------------------------------------------------------------ #11 AI Auto-Hook Generator (Shorts First 3s)
def create_hooked_short(
    input_path: str,
    output_path: str,
    hook_ts: float,
    hook_duration: float = 2.6,
    encoder: str = "auto",
    video_quality: int = 19,
) -> Tuple[bool, str]:
    """Prepend a punchy 2.6s teaser hook from hook_ts to the start of a clip."""
    ffmpeg = get_ffmpeg_path()
    if not ffmpeg:
        return False, "FFmpeg не найден"
    dur = probe_video(input_path).get("duration", 0.0)
    h_start = max(0.0, min(max(0.0, dur - hook_duration), float(hook_ts)))
    enc = resolve_encoder(encoder)

    tmp_dir = Path(tempfile.gettempdir()) / f"tr_hook_{os.getpid()}"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    p_hook = str(tmp_dir / "hook.mp4")
    p_main = str(tmp_dir / "main.mp4")
    try:
        cmd1 = [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-ss", f"{h_start:.2f}", "-i", input_path, "-t", f"{hook_duration:.2f}",
            "-vf", "fade=t=out:st=" + f"{max(0.1, hook_duration - 0.25):.2f}" + ":d=0.25:color=white",
            *encoder_ffmpeg_args(enc, video_quality), "-c:a", "aac", "-b:a", "192k", p_hook,
        ]
        r1 = subprocess.run(cmd1, capture_output=True, text=True, timeout=300, creationflags=CREATE_NO_WINDOW)
        if r1.returncode != 0 or not os.path.exists(p_hook):
            return False, r1.stderr or "Не удалось создать тизер-хук"

        ok_cat, msg = concat_videos([p_hook, input_path], output_path, normalize_clips=True)
        return ok_cat, output_path if ok_cat else msg
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ------------------------------------------------------------------ #12 AI Smart Emoji & Highlighted Subtitles
EMOJI_RULES = [
    (r"\b(огонь|жесть|имба|круто|топ|разнос|красава|fire|insane|lit)\b", "🔥"),
    (r"\b(ахах|ору|смешно|хах|ржу|lol|lmao|kekw)\b", "😂"),
    (r"\b(умер|смерть|убил|убили|рип|rip|dead|f\b|кринж)\b", "💀"),
    (r"\b(деньги|бабки|донат|миллион|рубл|доллар|money|cash)\b", "💸"),
    (r"\b(клатч|эйс|хедшот|выиграл|победа|ace|clutch|win|victory)\b", "🏆"),
    (r"\b(шок|пипец|офигеть|жестко|капец|wtf|omg|shock)\b", "😱"),
    (r"\b(сердце|люблю|спасибо|красота|love|gg)\b", "❤️"),
]


def enhance_subtitles_with_emojis(ass_path: str) -> Tuple[bool, str]:
    """Inject smart contextual emojis & golden accent highlights into an .ass subtitle file."""
    p = Path(ass_path)
    if not p.exists():
        return False, "Файл субтитров .ass не найден"
    lines = p.read_text(encoding="utf-8", errors="ignore").splitlines()
    out: List[str] = []
    changed = 0
    for ln in lines:
        if ln.startswith("Dialogue:"):
            parts = ln.split(",", 9)
            if len(parts) == 10:
                txt = parts[9]
                for pat, emo in EMOJI_RULES:
                    if re.search(pat, txt, re.IGNORECASE) and emo not in txt:
                        txt = f"{txt} {emo}"
                        changed += 1
                        break
                parts[9] = txt
                ln = ",".join(parts)
        out.append(ln)
    p.write_text("\n".join(out), encoding="utf-8")
    return True, f"Добавлены AI-эмодзи в {changed} реплик"


# ------------------------------------------------------------------ #13 AI Auto-Montage «Лучшее за стрим»
def build_auto_montage(
    input_path: str,
    output_path: str,
    clips: List[dict],
    max_clips: int = 15,
) -> Tuple[bool, str]:
    """Assemble chronological Best-Of YouTube montage from top scored viral clips."""
    if not clips:
        return False, "Нет найденных моментов для авто-монтажа"
    chosen = sorted(clips[:max_clips], key=lambda c: float(c.get("start_sec", 0)))
    tmp_dir = Path(tempfile.gettempdir()) / f"tr_montage_{os.getpid()}"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    part_files: List[str] = []
    try:
        for i, c in enumerate(chosen):
            s = max(0.0, float(c.get("start_sec", 0)))
            e = max(s + 5.0, float(c.get("end_sec", s + 30.0)))
            pf = str(tmp_dir / f"seg_{i:03d}.mp4")
            ok, _ = trim_video_fast(input_path, s, e, pf, accurate_reencode=False)
            if ok and os.path.exists(pf):
                part_files.append(pf)
        if not part_files:
            return False, "Не удалось вырезать сегменты"
        ok_cat, msg = concat_videos(part_files, output_path, normalize_clips=False)
        return ok_cat, output_path if ok_cat else msg
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ------------------------------------------------------------------ #14 AI Quote Cards Generator
def generate_quote_card(
    video_path: str,
    timestamp_sec: float,
    quote_text: str,
    author: str = "",
    accent_color: str = "#8b5cf6",
    output_path: Optional[str] = None,
) -> Tuple[bool, str]:
    """Render a sleek 1280x720 Quote Card (.jpg) with streamer frame + stylized quote."""
    try:
        from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont
    except Exception:
        return False, "Pillow не установлен"

    src = Path(video_path)
    out = output_path or str(src.with_name(f"{src.stem}_quote_{int(max(0, timestamp_sec))}s.jpg"))
    tmp = Path(tempfile.gettempdir()) / f"tr_qframe_{os.getpid()}.jpg"
    ok, err = extract_frame_from_file(video_path, timestamp_sec, str(tmp))
    if not ok or not tmp.exists():
        return False, err or "Не удалось снять кадр"

    try:
        img = Image.open(tmp).convert("RGB").resize((1280, 720), Image.Resampling.LANCZOS)
        bg = img.filter(ImageFilter.GaussianBlur(18))
        bg = ImageEnhance.Brightness(bg).enhance(0.45)

        # Circular portrait crop on the left
        portrait_sz = 340
        pw, ph = img.size
        side = min(pw, ph)
        crop = img.crop(((pw - side) // 2, (ph - side) // 2, (pw + side) // 2, (ph + side) // 2)).resize((portrait_sz, portrait_sz), Image.Resampling.LANCZOS)
        mask = Image.new("L", (portrait_sz, portrait_sz), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, portrait_sz, portrait_sz), fill=255)

        canvas = bg.convert("RGBA")
        # Glass panel on right
        overlay = Image.new("RGBA", (1280, 720), (0, 0, 0, 0))
        od = ImageDraw.Draw(overlay)
        od.rounded_rectangle((460, 120, 1210, 600), radius=28, fill=(14, 17, 28, 210), outline=accent_color, width=3)
        canvas = Image.alpha_composite(canvas, overlay)

        # Ring + portrait
        draw = ImageDraw.Draw(canvas)
        draw.ellipse((64, 184, 64 + portrait_sz + 12, 184 + portrait_sz + 12), outline=accent_color, width=6)
        canvas.paste(crop, (70, 190), mask)

        def _font(sz: int):
            for fp in (
                "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
                "/System/Library/Fonts/Helvetica.ttc",
                "C:/Windows/Fonts/arialbd.ttf",
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            ):
                if os.path.exists(fp):
                    try:
                        return ImageFont.truetype(fp, sz)
                    except Exception:
                        pass
            return ImageFont.load_default()

        f_big = _font(88)
        f_quote = _font(40)
        f_auth = _font(28)

        draw.text((495, 130), "“", font=f_big, fill=accent_color)
        # Word-wrap quote
        words = (quote_text or "Лучшая цитата со стрима!").strip().split()
        lines, cur = [], ""
        for w in words:
            test = (cur + " " + w).strip()
            if len(test) > 28 and cur:
                lines.append(cur)
                cur = w
            else:
                cur = test
        if cur:
            lines.append(cur)
        y = 225
        for ln in lines[:5]:
            draw.text((510, y), ln, font=f_quote, fill="#FFFFFF")
            y += 52
        sig = f"— {author or src.stem}"
        draw.text((510, 525), sig[:40], font=f_auth, fill=accent_color)

        canvas.convert("RGB").save(out, "JPEG", quality=94)
        return True, out
    except Exception as exc:
        return False, str(exc)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except Exception:
                pass


# ------------------------------------------------------------------ #15 AI Dubbing (RU <-> EN)
def dub_video_ai(
    input_path: str,
    output_path: str,
    target_lang: str = "en",
    cfg: Optional[AppConfig] = None,
) -> Tuple[bool, str]:
    """Translate speech and synthesize AI voiceover (OpenAI TTS or native macOS say / Windows SAPI), mixed over ducked original."""
    ffmpeg = get_ffmpeg_path()
    if not ffmpeg:
        return False, "FFmpeg не найден"

    speech_txt, _, _ = _load_sidecar_text(input_path)
    if not speech_txt:
        try:
            from faster_whisper import WhisperModel
            model = WhisperModel((cfg.whisper_model if cfg else "small") or "small", device="auto", compute_type="int8")
            task = "translate" if target_lang == "en" else "transcribe"
            segs, _ = model.transcribe(input_path, task=task, vad_filter=True)
            speech_txt = " ".join((s.text or "").strip() for s in segs).strip()
        except Exception as exc:
            return False, f"Для дубляжа нужен Whisper или субтитры: {exc}"

    if not speech_txt:
        return False, "Не найдено речи для дубляжа"

    if cfg and target_lang != "en":
        tr = _call_llm_text(cfg, f"Переведи текст на язык {target_lang} естественно и кратко для озвучки видео.", speech_txt[:3500])
        if tr:
            speech_txt = tr
    elif cfg and target_lang == "en" and re.search(r"[а-яА-ЯёЁ]", speech_txt):
        tr = _call_llm_text(cfg, "Translate this stream transcript to natural, energetic English for voiceover.", speech_txt[:3500])
        if tr:
            speech_txt = tr

    tmp_dir = Path(tempfile.gettempdir()) / f"tr_dub_{os.getpid()}"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    voice_audio = str(tmp_dir / "dub.wav")
    try:
        tts_ok = False
        # 1. Try OpenAI TTS API if configured
        if cfg and (cfg.llm_api_key or "").strip() and "openai.com" in (cfg.llm_api_url or "").lower():
            try:
                r = requests.post(
                    "https://api.openai.com/v1/audio/speech",
                    headers={"Authorization": f"Bearer {cfg.llm_api_key.strip()}", "Content-Type": "application/json"},
                    json={"model": "tts-1", "voice": "onyx", "input": speech_txt[:3800]},
                    timeout=60,
                )
                if r.status_code == 200:
                    voice_audio = str(tmp_dir / "dub.mp3")
                    Path(voice_audio).write_bytes(r.content)
                    tts_ok = True
            except Exception:
                pass

        # 2. Native macOS `say` TTS fallback (free, instant, high quality)
        if not tts_ok and sys.platform == "darwin" and shutil.which("say"):
            aiff = str(tmp_dir / "dub.aiff")
            voice = "Samantha" if target_lang == "en" else "Milena"
            res = subprocess.run(["say", "-v", voice, "-o", aiff, speech_txt[:3500]], capture_output=True, timeout=120)
            if res.returncode == 0 and os.path.exists(aiff):
                voice_audio = aiff
                tts_ok = True

        # 3. Native Windows SAPI TTS fallback
        if not tts_ok and os.name == "nt":
            ps_txt = speech_txt[:3000].replace("'", "''").replace("\n", " ")
            ps = (
                "Add-Type -AssemblyName System.Speech;"
                "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
                f"$s.SetOutputToWaveFile('{voice_audio}');"
                f"$s.Speak('{ps_txt}');"
                "$s.Dispose();"
            )
            res = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, timeout=120, creationflags=CREATE_NO_WINDOW)
            if res.returncode == 0 and os.path.exists(voice_audio):
                tts_ok = True

        if not tts_ok:
            return False, "Не удалось синтезировать озвучку (укажите OpenAI API Key или проверьте системный TTS)"

        # Mix dubbed voice over ducked original (18% original game/ambience + 100% dub)
        cmd = [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-i", input_path, "-i", voice_audio,
            "-filter_complex", "[0:a]volume=0.18[orig];[1:a]volume=1.15[dub];[orig][dub]amix=inputs=2:duration=first:dropout_transition=2[aout]",
            "-map", "0:v:0", "-map", "[aout]",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
            output_path,
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=600, creationflags=CREATE_NO_WINDOW)
        if res.returncode == 0 and os.path.exists(output_path):
            return True, output_path
        return False, (res.stderr or "Ошибка микширования дубляжа").strip()
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ------------------------------------------------------------------ #16 AI Smart Background Music Ducking
def mix_music_with_ducking(
    video_path: str,
    music_path: str,
    output_path: str,
    music_vol: float = 0.24,
) -> Tuple[bool, str]:
    """Mix background music with automatic sidechain ducking whenever the streamer speaks."""
    ffmpeg = get_ffmpeg_path()
    if not ffmpeg:
        return False, "FFmpeg не найден"
    if not os.path.exists(music_path):
        return False, "Выберите аудиофайл фоновой музыки (.mp3 / .wav)"
    fg = (
        f"[1:a]aloop=loop=-1:size=2e+09,volume={max(0.05, min(0.8, music_vol)):.2f}[bg];"
        f"[0:a]asplit=2[voice][sc];"
        f"[bg][sc]sidechaincompress=threshold=0.03:ratio=8:attack=15:release=350[ducked];"
        f"[voice][ducked]amix=inputs=2:duration=first:dropout_transition=2[aout]"
    )
    cmd = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-i", video_path, "-i", music_path,
        "-filter_complex", fg,
        "-map", "0:v:0", "-map", "[aout]",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart", output_path,
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=1200, creationflags=CREATE_NO_WINDOW)
    if res.returncode == 0 and os.path.exists(output_path):
        return True, output_path
    return False, (res.stderr or "Ошибка наложения музыки").strip()


# ------------------------------------------------------------------ #17 AI Auto SFX (Meme Boom / Ding on Peaks)
def add_auto_sfx(
    video_path: str,
    output_path: str,
    timestamps: List[float],
    sfx_type: str = "boom",
) -> Tuple[bool, str]:
    """Synthesize punchy SFX (Vine Boom / Ding / Whoosh) and place them on peak timestamps."""
    ffmpeg = get_ffmpeg_path()
    if not ffmpeg:
        return False, "FFmpeg не найден"
    ts_list = [max(0.1, float(t)) for t in (timestamps or [])[:12]]
    if not ts_list:
        return False, "Нет таймкодов для добавления SFX (сначала найдите хайлайты)"

    if sfx_type == "ding":
        sfx_src = "sine=frequency=1320:duration=0.45,afade=t=out:st=0.05:d=0.40,volume=0.45"
    elif sfx_type == "whoosh":
        sfx_src = "anoisesrc=color=pink:duration=0.55,bandpass=f=700:w=500,afade=t=in:st=0:d=0.18,afade=t=out:st=0.25:d=0.30,volume=0.55"
    else:  # Vine Boom deep sub-bass drop
        sfx_src = "sine=frequency=58:duration=0.75,afade=t=out:st=0.04:d=0.70,volume=0.85"

    parts = []
    mix_ins = ["[0:a]"]
    for idx, t in enumerate(ts_list):
        ms = int(t * 1000)
        parts.append(f"aevalsrc=0:d=0.01[z{idx}]")  # unused placeholder avoided below
    parts = []
    for idx, t in enumerate(ts_list):
        ms = int(t * 1000)
        parts.append(f"{sfx_src},adelay={ms}|{ms}[sfx{idx}]")
        mix_ins.append(f"[sfx{idx}]")
    parts.append(f"{''.join(mix_ins)}amix=inputs={len(mix_ins)}:duration=first:normalize=0[aout]")

    cmd = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-i", video_path,
        "-filter_complex", ";".join(parts),
        "-map", "0:v:0", "-map", "[aout]",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart", output_path,
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=900, creationflags=CREATE_NO_WINDOW)
    if res.returncode == 0 and os.path.exists(output_path):
        return True, output_path
    return False, (res.stderr or "Ошибка добавления SFX").strip()


# ------------------------------------------------------------------ #18 AI Game Event Detector (Killfeed / Victory)
def detect_game_events_vision(video_path: str) -> List[dict]:
    """Scan frames with OpenCV for high-action HUD flashes / killfeed red-gold bursts."""
    try:
        import cv2
        import numpy as np
    except Exception:
        return []

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return []
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    step = max(1, int(fps * 2.0))  # 0.5 fps scan for speed

    scores: List[Tuple[float, float]] = []
    prev_gray = None
    idx = 0
    while idx < total:
        cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
        ok, frame = cap.read()
        if not ok or frame is None:
            break
        h, w = frame.shape[:2]
        # Top-right killfeed + center banner zone
        kf = frame[0 : int(h * 0.25), int(w * 0.65) : w]
        hsv = cv2.cvtColor(kf, cv2.COLOR_BGR2HSV)
        # Red / Gold killfeed highlight pixels
        red_mask = ((hsv[:, :, 0] < 12) | (hsv[:, :, 0] > 168)) & (hsv[:, :, 1] > 140) & (hsv[:, :, 2] > 120)
        gold_mask = (hsv[:, :, 0] >= 18) & (hsv[:, :, 0] <= 34) & (hsv[:, :, 1] > 140) & (hsv[:, :, 2] > 140)
        hud_ratio = float(np.mean(red_mask | gold_mask))

        small = cv2.resize(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (160, 90))
        motion = float(np.mean(cv2.absdiff(small, prev_gray))) if prev_gray is not None else 0.0
        prev_gray = small
        scores.append((idx / fps, hud_ratio * 220.0 + motion * 0.45))
        idx += step
    cap.release()

    if not scores:
        return []
    vals = [s[1] for s in scores]
    thr = float(np.mean(vals) + 1.45 * np.std(vals))
    events: List[dict] = []
    last_t = -30.0
    for t, v in scores:
        if v >= thr and (t - last_t) >= 22.0:
            events.append({"timestamp_sec": round(t, 1), "score": round(v, 1), "reason": "🎯 Игровое событие / Killfeed (Vision)"})
            last_t = t
    events.sort(key=lambda x: x["score"], reverse=True)
    return events[:12]


# ------------------------------------------------------------------ #19 AI Best Frame Picker (Top 5 Thumbnail Frames)
def pick_best_thumbnail_frames(video_path: str, count: int = 5) -> List[dict]:
    """Score frames on sharpness (Laplacian), color vibrancy, and face presence; save top 5 JPGs."""
    try:
        import cv2
        import numpy as np
    except Exception:
        return []

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return []
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    if total <= 0:
        cap.release()
        return []

    cascade_path = os.path.join(cv2.data.haarcascades, "haarcascade_frontalface_default.xml")
    detector = cv2.CascadeClassifier(cascade_path) if os.path.exists(cascade_path) else None

    samples = min(30, max(8, total // max(1, int(fps * 10))))
    step = max(1, total // samples)
    scored: List[Tuple[float, float, Any, bool]] = []

    for i in range(samples):
        f_idx = min(total - 1, i * step + step // 2)
        cap.set(cv2.CAP_PROP_POS_FRAMES, f_idx)
        ok, frame = cap.read()
        if not ok or frame is None:
            continue
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        sharp = float(cv2.Laplacian(gray, cv2.CV_64F).var())
        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
        sat = float(np.mean(hsv[:, :, 1]))
        bright = float(np.mean(hsv[:, :, 2]))
        if bright < 28:
            continue  # skip black screens
        has_face = False
        if detector is not None:
            faces = detector.detectMultiScale(gray, 1.2, 4, minSize=(42, 42))
            has_face = len(faces) > 0
        total_score = min(500.0, sharp) * 0.5 + sat * 1.4 + (160.0 if has_face else 0.0)
        scored.append((total_score, f_idx / fps, frame, has_face))
    cap.release()

    scored.sort(key=lambda x: x[0], reverse=True)
    out_dir = Path(tempfile.gettempdir()) / "tr_best_frames"
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = re.sub(r"[^a-zA-Z0-9_-]", "_", Path(video_path).stem)[:24]

    results: List[dict] = []
    for i, (sc, ts, frame, has_face) in enumerate(scored[:count]):
        fp = str(out_dir / f"{stem}_best_{i}_{int(ts)}s.jpg")
        cv2.imwrite(fp, frame, [int(cv2.IMWRITE_JPEG_QUALITY), 90])
        results.append({
            "path": fp,
            "ts": round(ts, 1),
            "score": int(min(99, 60 + sc / 12.0)),
            "has_face": has_face,
        })
    return results


# ------------------------------------------------------------------ #20 AI Virtual Green Screen (Portrait Cutout Shorts)
def create_virtual_greenscreen_short(
    input_path: str,
    output_path: str,
    cam_region: Optional[dict],
    game_region: Optional[dict],
    encoder: str = "auto",
    video_quality: int = 19,
) -> Tuple[bool, str]:
    """Composite streamer webcam with soft feathered portrait alpha cutout (virtual green screen) over full 9:16 gameplay."""
    ffmpeg = get_ffmpeg_path()
    if not ffmpeg:
        return False, "FFmpeg не найден"
    cr = BlurRegion(**{k: cam_region[k] for k in ("x", "y", "w", "h")}).clamped() if cam_region else BlurRegion(0.02, 0.55, 0.28, 0.35)
    gr = BlurRegion(**{k: game_region[k] for k in ("x", "y", "w", "h")}).clamped() if game_region else BlurRegion(0.20, 0.0, 0.60, 1.0)
    enc = resolve_encoder(encoder)

    cw = f"max(4,trunc(iw*{cr.w:.6f}/2)*2)"
    ch = f"max(4,trunc(ih*{cr.h:.6f}/2)*2)"
    gw = f"max(4,trunc(iw*{gr.w:.6f}/2)*2)"
    gh = f"max(4,trunc(ih*{gr.h:.6f}/2)*2)"

    # Soft portrait elliptical matte cutout around streamer silhouette (no green screen required)
    pip_w, pip_h = 460, 460
    rx, ry = pip_w / 2.0, pip_h / 2.0
    alpha_expr = (
        f"255*clip(1.35-((X-{rx})*(X-{rx})/({rx*0.88}*{rx*0.88})+(Y-{ry})*(Y-{ry})/({ry*0.92}*{ry*0.92})),0,1)"
    )
    fg = (
        f"[0:v]split=2[scam][sgame];"
        f"[sgame]crop=w='{gw}':h='{gh}':x='trunc(iw*{gr.x:.6f}/2)*2':y='trunc(ih*{gr.y:.6f}/2)*2',"
        f"scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1[bg];"
        f"[scam]crop=w='{cw}':h='{ch}':x='trunc(iw*{cr.x:.6f}/2)*2':y='trunc(ih*{cr.y:.6f}/2)*2',"
        f"scale={pip_w}:{pip_h}:force_original_aspect_ratio=increase,crop={pip_w}:{pip_h},"
        f"format=yuva420p,geq=lum='p(X,Y)':cb='p(X,Y)':cr='p(X,Y)':a='{alpha_expr}'[cam];"
        f"[bg][cam]overlay=(W-w)/2:H-h-90,setsar=1[vout]"
    )
    cmd = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-i", input_path, "-filter_complex", fg,
        "-map", "[vout]", "-map", "0:a?",
        *encoder_ffmpeg_args(enc, video_quality), "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart", output_path,
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=1800, creationflags=CREATE_NO_WINDOW)
    if res.returncode == 0 and os.path.exists(output_path):
        return True, output_path
    return False, (res.stderr or "Ошибка Virtual Green Screen").strip()


# ------------------------------------------------------------------ #21 AI Smart Scene Cut (BRB / Starting Soon)
def detect_brb_and_loading_scenes(video_path: str) -> List[Tuple[float, float]]:
    """Detect static 'Starting Soon / BRB / Loading' screens and return KEEP segments."""
    from app.tools import detect_active_segments_no_silence_afk
    return detect_active_segments_no_silence_afk(
        video_path, silence_db=-36.0, min_silence_sec=4.0, remove_freeze=True, min_freeze_sec=6.0
    )


# ------------------------------------------------------------------ #22 AI Social Post Generator (Telegram / VK / Discord)
def generate_social_post(video_path: str, platform: str, cfg: AppConfig) -> str:
    """Generate an engaging social post (Telegram / VK / Discord) with timestamps & hashtags."""
    speech, markers, _ = _load_sidecar_text(video_path)
    dur = probe_video(video_path).get("duration", 0.0)
    plat_name = {"telegram": "Telegram-канала", "vk": "сообщества VK", "discord": "сервера Discord"}.get(platform, "Telegram")
    prompt = (
        f"Напиши вовлекающий пост-анонс для {plat_name} о новом видео/стриме «{Path(video_path).stem}» ({format_duration(dur)}).\n"
        f"Маркеры: {json.dumps(markers[:8], ensure_ascii=False)}\n"
        f"Речь: {speech[:1800]}\n"
        "Добавь цепляющий заголовок с эмодзи, 3 буллита самых сочных моментов, призыв к действию и 5 хэштегов."
    )
    ans = _call_llm_text(cfg, "Ты SMM-менеджер топового стримера.", prompt)
    if ans:
        return ans
    mk_lines = "\n".join(f"• {format_duration(float(m.get('t', 0)))} — {m.get('note') or 'Жёсткий момент'}" for m in markers[:5])
    if not mk_lines:
        mk_lines = "• Топовые эмоции и моменты с прямого эфира\n• Лучшие реакции чата\n• Самая сочная выжимка без воды"
    return (
        f"🔥 НОВЫЙ РОЛИК УЖЕ ГОТОВ: {Path(video_path).stem}!\n\n"
        f"Что внутри ({format_duration(dur)}):\n{mk_lines}\n\n"
        f"👇 Пишите в комментах, какой момент залетел больше всего!\n"
        f"#twitch #стрим #хайлайты #нарезка #shorts"
    )


# ------------------------------------------------------------------ #23 AI Virality Score & Critique
def analyze_virality_critique(video_path: str, title: str, cfg: AppConfig) -> dict:
    """Score video virality potential (0..100) and give concrete editing/title improvement tips."""
    speech, markers, chat_msgs = _load_sidecar_text(video_path)
    info = probe_video(video_path)
    dur = info.get("duration", 0.0)
    w, h = int(info.get("width", 0)), int(info.get("height", 0))

    score = 68
    tips: List[str] = []
    if h >= 1080:
        score += 6
    else:
        tips.append("Разрешение ниже 1080p — рекомендуется запись в 1080p60/1440p60.")
    if len(markers) >= 3 or len(chat_msgs) >= 30:
        score += 8
    if 15 <= dur <= 65:
        score += 8
        tips.append("Идеальная длительность для Shorts/TikTok/Reels (15–60 сек)!")
    elif dur > 1200:
        tips.append("Длинный стрим — используйте «AI Вирусные клипы» или «Авто-монтаж» для нарезки динамичных роликов.")
    if title and len(title) >= 12:
        score += 5
    else:
        tips.append("Добавьте интригующий заголовок (40–60 символов) с эмоцией или вопросом.")
    tips.append("Убедитесь, что первые 3 секунды сразу цепляют — используйте «AI Авто-хук».")

    llm_critique = _call_llm_text(
        cfg,
        "Ты YouTube-аналитик. Оцени потенциал ролика от 0 до 100 и дай 4 конкретных совета на русском, как увеличить CTR и удержание.",
        f"Название: {title or Path(video_path).stem}\nДлительность: {format_duration(dur)}, {w}x{h}\nНачало речи: {speech[:1200]}",
    )
    if llm_critique:
        m = re.search(r"\b(\d{2,3})\s*/\s*100\b", llm_critique)
        if m:
            score = max(10, min(99, int(m.group(1))))
        return {"score": score, "critique": llm_critique}

    return {
        "score": min(98, score),
        "critique": f"Оценка вирусности: {min(98, score)}/100\n\nРекомендации:\n" + "\n".join(f"• {t}" for t in tips),
    }


# ------------------------------------------------------------------ #25 AI Chat Sentiment Graph
LAUGH_RE = re.compile(r"(ахах|хах|ору|ржу|смеш|lol|lmao|kekw|lul|omegalul|😂|🤣|😆)", re.IGNORECASE)
HYPE_RE = re.compile(r"(пог|pog|клатч|эйс|топ|имба|красава|огонь|gg|w\b|goat|🔥|🏆|⚡|❤)", re.IGNORECASE)
SALT_RE = re.compile(r"(кринж|бан|фу|слив|лох|туп|f\b|rip|l\b|cringe|💀|🤬|👎)", re.IGNORECASE)


def analyze_chat_sentiment(chat_json_path: str) -> dict:
    """Classify chat mood per minute into Laugh / Hype / Salt curves."""
    if not chat_json_path or not os.path.exists(chat_json_path):
        return {"ok": False, "error": "Файл чата .chat.json не найден"}
    try:
        msgs = json.loads(Path(chat_json_path).read_text(encoding="utf-8", errors="ignore"))
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    if not msgs:
        return {"ok": False, "error": "Чат пуст"}

    buckets: Dict[int, List[int]] = {}  # minute -> [laugh, hype, salt]
    totals = [0, 0, 0]
    for m in msgs:
        t = float(m.get("t", 0))
        minute = max(0, int(t // 60))
        txt = str(m.get("text") or "")
        b = buckets.setdefault(minute, [0, 0, 0])
        if LAUGH_RE.search(txt):
            b[0] += 1
            totals[0] += 1
        if HYPE_RE.search(txt):
            b[1] += 1
            totals[1] += 1
        if SALT_RE.search(txt):
            b[2] += 1
            totals[2] += 1

    max_m = max(buckets.keys()) if buckets else 0
    timeline = []
    for mn in range(max_m + 1):
        l_cnt, h_cnt, s_cnt = buckets.get(mn, [0, 0, 0])
        timeline.append({"min": mn, "laugh": l_cnt, "hype": h_cnt, "salt": s_cnt})

    peak_laugh = max(timeline, key=lambda x: x["laugh"]) if timeline else {"min": 0, "laugh": 0}
    peak_hype = max(timeline, key=lambda x: x["hype"]) if timeline else {"min": 0, "hype": 0}
    return {
        "ok": True,
        "totals": {"laugh": totals[0], "hype": totals[1], "salt": totals[2], "messages": len(msgs)},
        "peak_laugh_min": peak_laugh["min"],
        "peak_hype_min": peak_hype["min"],
        "timeline": timeline[:240],
    }


# ------------------------------------------------------------------ Full Local AI Publish Package Generator (Zero-API!)
def generate_full_publish_package(
    video_path: str,
    format_mode: str,   # "full" | "clip" | "vertical"
    platforms: List[str],
    cfg: AppConfig,
) -> dict:
    """
    Generate complete ready-to-post package (5 titles, description with timestamps, hashtags,
    and thumbnail headline/subtitle) using Local Ollama, Cloud LLM, OR built-in Offline Russian NLP.
    """
    p = Path(video_path) if video_path else Path("stream.mp4")
    speech, markers, chat_msgs = _load_sidecar_text(video_path) if (video_path and os.path.exists(video_path)) else ("", [], [])
    dur = probe_video(video_path).get("duration", 0.0) if (video_path and os.path.exists(video_path)) else 0.0

    # Extract rich context from .meta.json if present
    streamer = ""
    game = ""
    stream_title = ""
    meta_p = p.with_suffix(".meta.json")
    if meta_p.exists():
        try:
            md = json.loads(meta_p.read_text(encoding="utf-8"))
            streamer = str(md.get("channel") or md.get("username") or "").strip()
            game = str(md.get("game") or "").strip()
            stream_title = str(md.get("title") or "").strip()
        except Exception:
            pass
    if not streamer:
        m_st = re.sub(r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}_", "", p.stem)
        streamer = m_st.split("_")[0] or "Стример"

    # Offline NLP keyword extraction from speech + chat
    stop_w = {"это", "как", "что", "вот", "для", "или", "если", "есть", "был", "была", "были", "просто", "очень", "меня", "тебя", "него", "когда", "даже", "чтобы", "здесь", "тут", "там", "надо", "можно"}
    word_freq: Dict[str, int] = {}
    for src_txt in (speech, " ".join(str(m.get("text") or "") for m in chat_msgs[:400]), stream_title):
        for w in re.findall(r"[а-яА-ЯёЁa-zA-Z0-9]{4,}", src_txt.lower()):
            if w not in stop_w and not w.isdigit():
                word_freq[w] = word_freq.get(w, 0) + 1
    top_words = [w for w, _ in sorted(word_freq.items(), key=lambda x: x[1], reverse=True)[:8]]

    fmt_label = {
        "full": "полноценный ролик YouTube (16:9)",
        "clip": "динамичная нарезка / хайлайт (16:9)",
        "vertical": "вертикальный Shorts / TikTok / Reels (9:16)",
    }.get(format_mode, "ролик")

    # Try Local Ollama / Cloud LLM first
    loc = detect_local_llm_status()
    engine_label = loc["engine_label"] if not (cfg.llm_api_url or "").strip() else f"Cloud LLM ({cfg.llm_model})"

    prompt = (
        f"Сделай готовый пакет для публикации ({fmt_label}) на платформы: {', '.join(platforms or ['youtube'])}.\n"
        f"Стример: {streamer}. Игра: {game or 'Стрим'}. Длительность: {format_duration(dur)}.\n"
        f"Ключевые слова: {', '.join(top_words)}.\n"
        f"Маркеры: {json.dumps(markers[:10], ensure_ascii=False)}\n"
        f"Речь: {speech[:1800]}\n\n"
        "Верни строго JSON без markdown-обёртки:\n"
        '{"titles":["...","...","...","...","..."],"description":"...","hashtags":"#tag1 #tag2 ...","thumb_headline":"КОРОТКИЙ КАПС","thumb_subtitle":"ПЛАШКА"}'
    )
    ans = _call_llm_text(cfg, "Ты топовый контент-продюсер YouTube/TikTok/Telegram. Отвечай строго валидным JSON на русском.", prompt)
    if ans:
        try:
            jm = re.search(r"\{.*\}", ans, re.DOTALL)
            if jm:
                parsed = json.loads(jm.group(0))
                if parsed.get("titles"):
                    return {
                        "ok": True,
                        "ai_engine": engine_label,
                        "titles": [str(t) for t in parsed["titles"][:5]],
                        "description": str(parsed.get("description") or ""),
                        "hashtags": str(parsed.get("hashtags") or ""),
                        "thumb_headline": str(parsed.get("thumb_headline") or "ЛУЧШИЙ МОМЕНТ!"),
                        "thumb_subtitle": str(parsed.get("thumb_subtitle") or streamer.upper()),
                    }
        except Exception:
            pass

    # Built-in Smart Offline Russian NLP Generator (Works 100% locally without Ollama or API!)
    game_part = f" в {game}" if game else ""
    kw_clean = [re.sub(r"[^а-яА-ЯёЁa-zA-Z0-9]", "", w) for w in top_words if len(w) >= 4]

    if format_mode == "vertical":
        titles = [
            f"{streamer} взорвал чат{game_part}! 🔥 #shorts",
            f"ЭТО НЕВОЗМОЖНО ПОВТОРИТЬ 😱 | {streamer} #shorts",
            f"Лучшие 30 секунд со стрима {streamer} 😂 #shorts",
            f"Чат просто сошёл с ума{game_part} 💀 #shorts",
            f"Смотреть до конца! Топ момент {streamer} ⚡ #shorts",
        ]
        base_tags = ["#shorts", "#tiktok", "#reels", "#twitch", "#стрим", "#хайлайты", "#нарезка"]
        th_head = "ЭТО БЕЗУМИЕ!"
    elif format_mode == "clip":
        titles = [
            f"ЛУЧШЕЕ СО СТРИМА {streamer}{game_part} — Чат в шоке! 🔥",
            f"{streamer} выдал легендарный момент{game_part} 😂",
            f"Топ-нарезка эфира {streamer} ({format_duration(dur)}) ⚡",
            f"Самые смешные и жёсткие моменты {streamer}{game_part} 💀",
            f"{stream_title or f'Хайлайты {streamer}'}{game_part} 🏆",
        ]
        base_tags = ["#twitch", "#нарезка", "#хайлайты", "#стрим", "#моменты", "#ютуб"]
        th_head = "ТОП НАРЕЗКА!"
    else:
        titles = [
            f"{stream_title or f'Полная запись стрима {streamer}'}{game_part} 🔥",
            f"{streamer}{game_part} — Полный эфир ({format_duration(dur)}) 🎬",
            f"Легендарный стрим {streamer}{game_part} | Без рекламы ⚡",
            f"{streamer} играет{game_part} — Лучшие моменты и эмоции 🏆",
            f"Запись прямого эфира {streamer}{game_part} ({p.stem[:10]})",
        ]
        base_tags = ["#twitch", "#стрим", "#записьстрима", "#прямойэфир", "#игры", "#youtube"]
        th_head = (game or "ЛУЧШИЙ СТРИМ").upper()[:20]

    extra_tags = [f"#{re.sub(r'[^а-яА-ЯёЁa-zA-Z0-9]', '', streamer.lower())}"]
    if game:
        extra_tags.append(f"#{re.sub(r'[^а-яА-ЯёЁa-zA-Z0-9]', '', game.lower())}")
    for w in kw_clean[:4]:
        tag = f"#{w}"
        if tag not in base_tags and tag not in extra_tags:
            extra_tags.append(tag)
    hashtags = " ".join(dict.fromkeys(base_tags + extra_tags))

    mk_block = ""
    if markers:
        mk_block = "\n\n⏱ Таймкоды:\n00:00 Начало\n" + "\n".join(
            f"{format_duration(float(m.get('t', 0)))} — {m.get('note') or 'Топ момент'}" for m in markers[:12]
        )

    description = (
        f"🔥 {titles[0]}\n\n"
        f"Канал: {streamer}" + (f" · Игра: {game}" if game else "") + f" · Длительность: {format_duration(dur)}\n"
        f"Самые яркие эмоции, реакции чата и лучшие моменты с эфира!"
        f"{mk_block}\n\n"
        f"👍 Ставьте лайк и подписывайтесь, чтобы не пропускать новые ролики!"
    )

    return {
        "ok": True,
        "ai_engine": "Встроенный оффлайн AI (без API)",
        "titles": titles,
        "description": description,
        "hashtags": hashtags,
        "thumb_headline": th_head,
        "thumb_subtitle": f"{streamer.upper()}" + (f" · {game.upper()[:16]}" if game else " · TWITCH"),
    }

