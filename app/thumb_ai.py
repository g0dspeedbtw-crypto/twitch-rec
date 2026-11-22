"""
AI Thumbnail Generator by Prompt + Photo Reference (v5.3.0).
Supports 2 modes requested by user:
  1. `stream_frame`: Real frame from the recorded stream + prompt-driven styling (colors, mood, typography)
     + photo reference color transfer & optional subject compositing (100% offline, instant).
  2. `flux_ai`: Free Flux AI neural image generation (zero API keys via Pollinations, OR OpenAI / local SD img2img
     when configured) enriched with photo reference style/palette analysis + reference color transfer/compositing
     + crisp Russian Pillow typography overlay so Cyrillic is always 100% sharp and readable.
"""
from __future__ import annotations

import base64
import io
import json
import os
import re
import tempfile
import uuid
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import quote

import requests

from app.config import AppConfig
from app.tools import extract_frame_from_file, generate_youtube_thumbnail, probe_video

_COLOR_KEYWORDS = [
    (r"(красн|алый|огонь|взрыв|шок|blood|red|fire)", "#EF4444", "vibrant red and orange neon lighting"),
    (r"(син|голуб|лёд|лед|холод|blue|cyan|ice)", "#3B82F6", "electric blue and cyan glow"),
    (r"(зелен|зелён|токсик|кислот|green|matrix|money)", "#22C55E", "neon emerald green lighting"),
    (r"(золот|желт|жёлт|богат|mrbeast|gold|yellow)", "#F59E0B", "bright golden yellow high-contrast YouTube lighting"),
    (r"(розов|кибер|cyber|pink|magenta)", "#EC4899", "cyberpunk magenta and neon pink atmosphere"),
    (r"(фиолет|twitch|твич|purple|violet)", "#8B5CF6", "vibrant purple neon esports lighting"),
]


def _parse_prompt_style(prompt: str) -> dict:
    low = (prompt or "").lower()
    accent = "#8B5CF6"
    lighting_en = "dramatic high-contrast YouTube thumbnail studio lighting"
    for pat, hx, desc in _COLOR_KEYWORDS:
        if re.search(pat, low):
            accent = hx
            lighting_en = desc
            break

    contrast = 1.20
    style_en = "ultra sharp YouTube gaming thumbnail, dynamic composition, vivid colors, 8k, no text, no letters, no watermark"
    if any(k in low for k in ("mrbeast", "бист", "кликбейт", "ярк", "сочн")):
        contrast = 1.28
        style_en = "MrBeast style YouTube thumbnail, ultra vivid saturated colors, expressive dramatic lighting, clean background, no text, no letters"
    elif any(k in low for k in ("хоррор", "страшн", "мрак", "темн", "тёмн", "horror", "dark")):
        contrast = 1.25
        style_en = "dark moody horror gaming thumbnail, dramatic rim light, deep shadows, cinematic fog, no text, no letters"
    elif any(k in low for k in ("кибер", "неон", "cyber", "neon")):
        contrast = 1.24
        style_en = "cyberpunk neon gaming thumbnail, glowing rim lights, futuristic bokeh background, no text, no letters"
    elif any(k in low for k in ("кино", "cinema", "эпик", "epic")):
        contrast = 1.18
        style_en = "epic cinematic blockbuster poster style, volumetric rays, dramatic depth of field, no text, no letters"

    # Extract explicit headline inside quotes «...» or "..." if user specified it in prompt
    extracted_head = ""
    mq = re.search(r'[«"\']([^«»"\']{2,34})[»"\']', prompt or "")
    if mq:
        extracted_head = mq.group(1).strip()

    return {
        "accent": accent,
        "lighting_en": lighting_en,
        "style_en": style_en,
        "contrast": contrast,
        "extracted_head": extracted_head,
    }


def analyze_reference_photo(ref_path: str) -> dict:
    """Extract dominant palette, brightness/mood, and optional PNG transparency from a reference photo."""
    info = {
        "ok": False,
        "accent_hex": "",
        "mood_en": "",
        "has_alpha": False,
        "palette_desc": "",
    }
    if not ref_path or not os.path.exists(ref_path):
        return info
    try:
        from PIL import Image, ImageStat

        raw = Image.open(ref_path)
        has_alpha = raw.mode in ("RGBA", "LA") or ("transparency" in raw.info)
        rgb = raw.convert("RGB").resize((96, 54))
        stat = ImageStat.Stat(rgb)
        r_m, g_m, b_m = stat.mean[:3]
        lum = 0.299 * r_m + 0.587 * g_m + 0.114 * b_m

        # Find most saturated vibrant pixel bucket for accent color
        best_px = (139, 92, 246)
        best_sat = -1.0
        for px in rgb.getdata():
            mx, mn = max(px), min(px)
            sat = (mx - mn) * (mx / 255.0)
            if sat > best_sat and mx > 90:
                best_sat = sat
                best_px = px
        accent_hex = "#{:02X}{:02X}{:02X}".format(*best_px)
        mood_en = "bright vibrant energetic daylight" if lum > 135 else ("dark dramatic high-contrast moody" if lum < 85 else "balanced cinematic studio")
        palette_desc = f"{mood_en} lighting with accent color {accent_hex}"
        info.update({
            "ok": True,
            "accent_hex": accent_hex,
            "mood_en": mood_en,
            "has_alpha": has_alpha,
            "palette_desc": palette_desc,
        })
    except Exception:
        pass
    return info


def _apply_reference_color_transfer(target_img, ref_path: str, strength: float = 0.38):
    """Subtle Reinhard-style mean/std color palette transfer from reference photo onto target image."""
    if not ref_path or not os.path.exists(ref_path) or strength <= 0:
        return target_img
    try:
        import numpy as np
        from PIL import Image

        ref = Image.open(ref_path).convert("RGB").resize((256, 144))
        t_arr = np.asarray(target_img.convert("RGB"), dtype=np.float32)
        r_arr = np.asarray(ref, dtype=np.float32)

        out = t_arr.copy()
        for c in range(3):
            t_mean, t_std = float(t_arr[:, :, c].mean()), float(t_arr[:, :, c].std()) + 1e-5
            r_mean, r_std = float(r_arr[:, :, c].mean()), float(r_arr[:, :, c].std()) + 1e-5
            transferred = (t_arr[:, :, c] - t_mean) * (r_std / t_std) + r_mean
            out[:, :, c] = t_arr[:, :, c] * (1.0 - strength) + np.clip(transferred, 0, 255) * strength
        return Image.fromarray(np.clip(out, 0, 255).astype(np.uint8), mode="RGB")
    except Exception:
        return target_img


def _composite_reference_photo(bg_img, ref_path: str, accent_hex: str = "#8B5CF6"):
    """
    Composite the user's reference photo cleanly onto the right side of the 1280x720 thumbnail:
      - If transparent PNG cutout: places character/streamer cleanly on right with neon glow.
      - If regular JPG/PNG photo: blends as a sleek angled framed hero inset with neon glow border.
    """
    if not ref_path or not os.path.exists(ref_path):
        return bg_img
    try:
        from PIL import Image, ImageDraw, ImageFilter

        tw, th = bg_img.size
        ref_raw = Image.open(ref_path)
        hx = (accent_hex or "#8B5CF6").lstrip("#")
        ac_rgb = (int(hx[0:2], 16), int(hx[2:4], 16), int(hx[4:6], 16)) if len(hx) == 6 else (139, 92, 246)

        base = bg_img.convert("RGBA")
        has_alpha = ref_raw.mode in ("RGBA", "LA")
        if has_alpha:
            fg = ref_raw.convert("RGBA")
            # Check if alpha channel actually has transparency
            extrema = fg.getchannel("A").getextrema()
            has_real_alpha = extrema[0] < 245
        else:
            fg = ref_raw.convert("RGBA")
            has_real_alpha = False

        if has_real_alpha:
            # Fit cutout nicely on right half (max height 92% of 720)
            max_h = int(th * 0.92)
            max_w = int(tw * 0.56)
            scale = min(max_w / max(1, fg.width), max_h / max(1, fg.height))
            nw, nh = max(1, int(fg.width * scale)), max(1, int(fg.height * scale))
            fg = fg.resize((nw, nh), Image.Resampling.LANCZOS)
            # Create neon rim glow behind cutout
            alpha_mask = fg.getchannel("A")
            glow = Image.new("RGBA", (nw + 40, nh + 40), (0, 0, 0, 0))
            glow_fill = Image.new("RGBA", (nw, nh), (*ac_rgb, 210))
            glow.paste(glow_fill, (20, 20), alpha_mask)
            glow = glow.filter(ImageFilter.GaussianBlur(14))
            px = tw - nw - 28
            py = th - nh
            base.alpha_composite(glow, (max(0, px - 20), max(0, py - 20)))
            base.alpha_composite(fg, (max(0, px), max(0, py)))
            return base.convert("RGB")

        # Regular photo: sleek hero card inset on right third with rounded corners + neon border
        card_w, card_h = 460, 480
        iw, ih = fg.size
        sc = max(card_w / max(1, iw), card_h / max(1, ih))
        rw, rh = int(iw * sc), int(ih * sc)
        fg_fit = fg.resize((rw, rh), Image.Resampling.LANCZOS)
        cx, cy = (rw - card_w) // 2, (rh - card_h) // 2
        fg_fit = fg_fit.crop((cx, cy, cx + card_w, cy + card_h))

        mask = Image.new("L", (card_w, card_h), 0)
        md = ImageDraw.Draw(mask)
        md.rounded_rectangle([0, 0, card_w - 1, card_h - 1], radius=24, fill=255)
        fg_fit.putalpha(mask)

        # Neon shadow / border card
        glow_layer = Image.new("RGBA", (card_w + 40, card_h + 40), (0, 0, 0, 0))
        gd = ImageDraw.Draw(glow_layer)
        gd.rounded_rectangle([14, 14, card_w + 26, card_h + 26], radius=28, fill=(*ac_rgb, 190))
        glow_layer = glow_layer.filter(ImageFilter.GaussianBlur(14))

        border_draw = ImageDraw.Draw(fg_fit)
        border_draw.rounded_rectangle([2, 2, card_w - 3, card_h - 3], radius=24, outline=(*ac_rgb, 255), width=6)

        px = tw - card_w - 42
        py = (th - card_h) // 2 - 22
        base.alpha_composite(glow_layer, (px - 20, py - 20))
        base.alpha_composite(fg_fit, (px, py))
        return base.convert("RGB")
    except Exception:
        return bg_img


def _generate_neural_bg(
    prompt: str,
    ref_path: str,
    ref_info: dict,
    style_info: dict,
    cfg: AppConfig,
    stream_context: str = "",
) -> Tuple[Optional[object], str]:
    """
    Generate a 1280x720 neural image background:
      1. If local Stable Diffusion WebUI (127.0.0.1:7860) is running -> img2img with ref_path!
      2. Else if OpenAI API key configured -> DALL-E / gpt-image-1!
      3. Else -> Free Pollinations Flux AI (zero API keys, enriched with reference palette & style)!
    Returns (PIL.Image | None, engine_label).
    """
    from PIL import Image

    ref_desc = f", inspired by reference style ({ref_info['palette_desc']})" if ref_info.get("ok") else ""
    full_prompt = (
        f"{prompt.strip() or 'epic gaming stream moment'}. "
        f"{stream_context}. {style_info['lighting_en']}, {style_info['style_en']}{ref_desc}"
    ).strip()

    # 1. Local Stable Diffusion WebUI img2img (127.0.0.1:7860) if user runs it
    if ref_path and os.path.exists(ref_path):
        try:
            r_chk = requests.get("http://127.0.0.1:7860/sdapi/v1/sd-models", timeout=0.8, proxies={"http": None, "https": None})
            if r_chk.status_code == 200:
                b64_ref = base64.b64encode(Path(ref_path).read_bytes()).decode("ascii")
                r_sd = requests.post(
                    "http://127.0.0.1:7860/sdapi/v1/img2img",
                    json={
                        "init_images": [b64_ref],
                        "prompt": full_prompt,
                        "negative_prompt": "text, watermark, blurry, low quality, deformed",
                        "width": 1280,
                        "height": 720,
                        "steps": 22,
                        "denoising_strength": 0.62,
                    },
                    timeout=90,
                    proxies={"http": None, "https": None},
                )
                if r_sd.status_code == 200 and (r_sd.json() or {}).get("images"):
                    img_bytes = base64.b64decode(r_sd.json()["images"][0])
                    return Image.open(io.BytesIO(img_bytes)).convert("RGB"), "Локальный Stable Diffusion (img2img с референсом)"
        except Exception:
            pass

    # 2. OpenAI API if configured
    api_url = (getattr(cfg, "llm_api_url", "") or "").strip()
    api_key = (getattr(cfg, "llm_api_key", "") or "").strip()
    proxies = {"http": cfg.proxy_url, "https": cfg.proxy_url} if getattr(cfg, "proxy_url", "") else None
    if api_key and ("openai.com" in api_url.lower() or not api_url):
        try:
            base_url = (api_url or "https://api.openai.com/v1").rstrip("/")
            if base_url.endswith("/chat/completions"):
                base_url = base_url[: -len("/chat/completions")]
            r_oa = requests.post(
                f"{base_url}/images/generations",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={"model": "dall-e-3", "prompt": full_prompt[:950], "size": "1792x1024", "n": 1, "response_format": "b64_json"},
                timeout=60,
                proxies=proxies,
            )
            if r_oa.status_code == 200:
                b64 = ((r_oa.json() or {}).get("data") or [{}])[0].get("b64_json")
                if b64:
                    return Image.open(io.BytesIO(base64.b64decode(b64))).convert("RGB"), "OpenAI Image AI"
        except Exception:
            pass

    # 3. Free Flux AI (Pollinations — zero API keys!)
    try:
        seed = uuid.uuid4().int % 999999
        encoded = quote(full_prompt[:550], safe="")
        url = f"https://image.pollinations.ai/prompt/{encoded}?width=1280&height=720&model=flux&nologo=true&seed={seed}"
        r = requests.get(url, timeout=45, proxies=proxies, headers={"User-Agent": "TwitchRecorderPro/5.3"})
        if r.status_code == 200 and r.content and len(r.content) > 8000:
            img = Image.open(io.BytesIO(r.content)).convert("RGB")
            return img, "Flux AI (бесплатная нейросеть + стиль референса)"
    except Exception:
        pass

    return None, ""


def generate_prompt_thumbnail(
    video_path: str,
    output_jpg: str,
    prompt: str,
    cfg: AppConfig,
    *,
    mode: str = "flux_ai",            # "stream_frame" | "flux_ai"
    ref_image_path: str = "",         # User's photo reference (JPG/PNG)
    composite_ref: bool = True,       # Place photo reference on thumbnail (or style/color only)
    timestamp_sec: float = -1.0,      # < 0 = auto pick bright frame
    headline: str = "",
    subtitle: str = "",
    accent_color: str = "",
) -> Tuple[bool, str, str]:
    """
    Master Prompt + Photo Reference Thumbnail Generator (1280x720).
    Returns (ok, output_path_or_error, engine_used).
    """
    from PIL import Image

    style_info = _parse_prompt_style(prompt)
    ref_info = analyze_reference_photo(ref_image_path)
    final_accent = (accent_color or "").strip() or (ref_info["accent_hex"] if ref_info.get("ok") and not accent_color else style_info["accent"])
    final_head = (headline or "").strip() or style_info["extracted_head"] or "ЛУЧШИЙ МОМЕНТ!"

    # Load stream metadata for rich prompt context
    stream_ctx = ""
    if video_path and os.path.exists(video_path):
        p = Path(video_path)
        mp = p.with_suffix(".meta.json")
        if mp.exists():
            try:
                md = json.loads(mp.read_text(encoding="utf-8"))
                ch = md.get("channel") or ""
                gm = md.get("game") or ""
                stream_ctx = f"Game: {gm}, Streamer: {ch}".strip(", ")
                if not subtitle:
                    subtitle = f"{ch} · {gm}".strip(" ·")
            except Exception:
                pass

    base_img = None
    engine_used = ""

    # Mode 2: Neural Flux AI + Photo Reference
    if mode == "flux_ai":
        base_img, engine_used = _generate_neural_bg(prompt, ref_image_path, ref_info, style_info, cfg, stream_ctx)
        if base_img is not None and ref_info.get("ok"):
            base_img = _apply_reference_color_transfer(base_img, ref_image_path, strength=0.32)

    # Mode 1 (or graceful fallback if offline): Real stream frame + prompt style + photo reference
    if base_img is None:
        tmp_frame = os.path.join(tempfile.gettempdir(), f"tr_pf_{uuid.uuid4().hex[:6]}.jpg")
        try:
            if video_path and os.path.exists(video_path):
                ts = timestamp_sec
                if ts < 0:
                    dur = float(probe_video(video_path).get("duration") or 30.0)
                    ts = max(2.0, min(dur * 0.25, 120.0))
                ok_f, _ = extract_frame_from_file(video_path, ts, tmp_frame)
                if not ok_f:
                    ok_f, _ = extract_frame_from_file(video_path, 0.5, tmp_frame)
                if ok_f and os.path.exists(tmp_frame):
                    base_img = Image.open(tmp_frame).convert("RGB")
                    engine_used = "Кадр стрима + стиль по промпту" + (" + фото-референс" if ref_info.get("ok") else "")
        finally:
            if os.path.exists(tmp_frame):
                try:
                    os.remove(tmp_frame)
                except Exception:
                    pass

        if base_img is None and ref_image_path and os.path.exists(ref_image_path):
            base_img = Image.open(ref_image_path).convert("RGB")
            engine_used = "Фото-референс + стиль по промпту"
        elif base_img is not None and ref_info.get("ok"):
            base_img = _apply_reference_color_transfer(base_img, ref_image_path, strength=0.40)

    if base_img is None:
        return False, "Не удалось получить кадр стрима или сгенерировать фон (выберите видео или фото-референс)", ""

    # Normalize to 1280x720 cover-crop before compositing
    tw, th = 1280, 720
    iw, ih = base_img.size
    sc = max(tw / max(1, iw), th / max(1, ih))
    nw, nh = int(iw * sc), int(ih * sc)
    base_img = base_img.resize((nw, nh), Image.Resampling.LANCZOS)
    base_img = base_img.crop(((nw - tw) // 2, (nh - th) // 2, (nw - tw) // 2 + tw, (nh - th) // 2 + th))

    # Composite user's photo reference onto right side if requested (and not already used as the sole background)
    if composite_ref and ref_image_path and os.path.exists(ref_image_path) and "Фото-референс +" not in engine_used:
        base_img = _composite_reference_photo(base_img, ref_image_path, accent_hex=final_accent)

    # Save intermediate canvas and run crisp Russian typography & vignette finisher
    tmp_canvas = os.path.join(tempfile.gettempdir(), f"tr_canvas_{uuid.uuid4().hex[:6]}.jpg")
    try:
        base_img.save(tmp_canvas, "JPEG", quality=94)
        ok_t, msg_t = generate_youtube_thumbnail(
            tmp_canvas,
            output_jpg,
            timestamp_sec=0.0,
            headline=final_head,
            subtitle=(subtitle or "TWITCH HIGHLIGHTS").strip(),
            accent_color=final_accent,
            add_border=True,
            contrast_boost=style_info["contrast"],
        )
        if ok_t and os.path.exists(output_jpg):
            return True, output_jpg, engine_used
        return False, msg_t or "Ошибка сохранения обложки", engine_used
    finally:
        if os.path.exists(tmp_canvas):
            try:
                os.remove(tmp_canvas)
            except Exception:
                pass
