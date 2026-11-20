from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Deque, Dict, List, Optional, Tuple

from PySide6.QtCore import QThread, Signal

from app.chat_and_ai import TwitchChatRecorder, save_markers_and_edl
from app.config import AppConfig, BlurRegion, ChannelConfig
from app.services import low_priority_popen_kwargs, send_telegram_video_async
from app.tools import (
    CREATE_NO_WINDOW,
    EXPORT_PRESETS,
    build_blur_filter_graph,
    build_shorts_9_16_filter,
    concat_videos,
    detect_active_segments_no_silence_afk,
    detect_banner_intervals,
    encoder_ffmpeg_args,
    escape_ffmpeg_filter_path,
    extract_audio_track,
    generate_youtube_thumbnail,
    get_ffmpeg_path,
    get_streamlink_cmd,
    get_ytdlp_cmd,
    merge_time_intervals,
    probe_video,
    resolve_encoder,
    trim_video_fast,
)
from app.twitch_api import StreamStatus, check_channels_status


def _start_pipe_drain(pipe, sink: Deque[str]) -> Optional[threading.Thread]:
    """Continuously drain a subprocess text/bytes pipe into a bounded deque to prevent OS pipe deadlocks."""
    if pipe is None:
        return None

    def _reader() -> None:
        try:
            for raw in pipe:
                if isinstance(raw, bytes):
                    line = raw.decode("utf-8", errors="replace").strip()
                else:
                    line = raw.strip()
                if line:
                    sink.append(line)
        except Exception:
            pass
        finally:
            try:
                pipe.close()
            except Exception:
                pass

    t = threading.Thread(target=_reader, daemon=True)
    t.start()
    return t


def format_bytes(num_bytes: float) -> str:
    size = float(max(0.0, num_bytes))
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if size < 1024.0:
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} ПБ"


def format_duration(seconds: float) -> str:
    sec = max(0, int(seconds))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def sanitize_filename(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")


class ChannelMonitorWorker(QThread):
    """Checks online status across all configured multi-platform channels."""

    statuses_ready = Signal(dict)  # Dict[str, StreamStatus]
    log_message = Signal(str)

    def __init__(self, channels: List[ChannelConfig], app_config: AppConfig, parent=None):
        super().__init__(parent)
        self.channels = list(channels)
        self.app_config = app_config

    def run(self) -> None:
        if not self.channels:
            self.statuses_ready.emit({})
            return
        try:
            res: Dict[str, StreamStatus] = check_channels_status(
                self.channels,
                timeout=12,
                oauth_token=self.app_config.twitch_oauth_token,
                proxy_url=self.app_config.proxy_url,
            )
            self.statuses_ready.emit(res)
        except Exception as exc:
            self.log_message.emit(f"[Мониторинг] Ошибка проверки каналов: {exc}")
            self.statuses_ready.emit({})


class LiveRecorderWorker(QThread):
    """
    Records a live stream (Twitch/YouTube/Kick/VK Play/Trovo) via Streamlink -> FFmpeg.
    Supports:
      - Realtime GPU blur (including feathering, image logo overlay, timed blur)
      - Zero-CPU copy + auto post-process blur queue
      - #7 Auto-reconnect on stream drops
      - #8 Auto-concat dropped stream parts
      - #9 Automatic file splitting every N minutes
      - #12 Twitch OAuth & Proxy
      - #15 Parallel Twitch Chat recording (.chat.json + .chat.ass)
      - #19 Manual live highlight markers (F9 / button -> .markers.json + .edl)
    """

    progress_updated = Signal(str, str, str, str)        # key, duration_str, size_str, speed_str
    status_changed = Signal(str, str)                    # key, status_text
    log_message = Signal(str)
    finished_recording = Signal(str, list, bool, bool)   # key, recorded_files, success, needs_post_blur

    _TIME_RE = re.compile(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)")
    _SPEED_RE = re.compile(r"speed=\s*([\d.]+x)")
    _FPS_RE = re.compile(r"fps=\s*([\d.]+)")
    _BITRATE_RE = re.compile(r"bitrate=\s*([\d.]+\s*[kM]bits/s)")

    def __init__(
        self,
        channel: ChannelConfig,
        app_config: AppConfig,
        stream_meta: Optional[dict] = None,
        parent=None,
    ):
        super().__init__(parent)
        self.channel = channel
        self.app_config = app_config
        self.stream_meta = dict(stream_meta or {})
        self._stop_requested = False
        self._sl_proc: Optional[subprocess.Popen] = None
        self._ff_proc: Optional[subprocess.Popen] = None
        self._chat_rec: Optional[TwitchChatRecorder] = None
        self._start_wall = time.time()
        self._markers: List[dict] = []
        self.recorded_files: List[str] = []

    def request_stop(self) -> None:
        self._stop_requested = True
        sl = self._sl_proc
        if sl and sl.poll() is None:
            try:
                sl.terminate()
            except Exception:
                pass
        ff = self._ff_proc
        if ff and ff.poll() is None:
            try:
                ff.terminate()
            except Exception:
                pass

    def add_marker(self, note: str = "Момент") -> float:
        t = round(max(0.0, time.time() - self._start_wall), 2)
        self._markers.append({"t": t, "note": note})
        return t

    def clip_retrospective(self, seconds: int = 60, tag: str = "dvr") -> Optional[str]:
        """
        #6 DVR Retrospective Clipper & #7 Live Auto-Clipper:
        Cuts the last N seconds from the active fragmented MP4/MKV live recording without stopping the stream!
        """
        cur_f = getattr(self, "_current_active_file", None)
        if not cur_f or not Path(cur_f).exists():
            return None
        elapsed = max(0.0, time.time() - self._start_wall)
        if elapsed < 4.0:
            return None
        dur = min(float(seconds), elapsed)
        start_t = max(0.0, elapsed - dur)
        clips_dir = self.app_config.ensure_output_dir() / "AutoClips"
        clips_dir.mkdir(parents=True, exist_ok=True)
        ts_str = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        uname = sanitize_filename(self.channel.username)
        out_clip = str(clips_dir / f"{ts_str}_{uname}_{tag}_{int(dur)}s.mp4")

        def _cut_bg() -> None:
            ok, _ = trim_video_fast(str(cur_f), start_t, start_t + dur, out_clip, accurate_reencode=False)
            if ok and os.path.exists(out_clip) and os.path.getsize(out_clip) > 10_000:
                self.log_message.emit(f"[{self.channel.username}] Сохранён клип-откат ({int(dur)} сек): {Path(out_clip).name}")
                if self.app_config.telegram_bot_token.strip() and self.app_config.telegram_chat_id.strip():
                    send_telegram_video_async(
                        self.app_config.telegram_bot_token,
                        self.app_config.telegram_chat_id,
                        out_clip,
                        caption=f"🎬 Авто-клип {self.channel.username} ({int(dur)} сек)",
                        proxy_url=self.app_config.proxy_url,
                    )

        threading.Thread(target=_cut_bg, daemon=True).start()
        return out_clip

    def run(self) -> None:
        key = self.channel.key
        uname = self.channel.username
        streamlink_cmd = get_streamlink_cmd()
        ffmpeg = get_ffmpeg_path()

        if not streamlink_cmd or not ffmpeg:
            self.log_message.emit(f"[{uname}] Ошибка: не найден streamlink или ffmpeg!")
            self.finished_recording.emit(key, [], False, False)
            return

        out_dir = self.app_config.ensure_output_dir() / sanitize_filename(uname)
        out_dir.mkdir(parents=True, exist_ok=True)

        session_ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        ext = self.app_config.container
        regions = self.channel.blur_regions
        has_regions = len(regions) > 0
        use_realtime_blur = self.channel.blur_mode == "realtime" and has_regions
        needs_post_blur = self.channel.blur_mode == "postprocess" and has_regions
        suffix = "_blurred" if use_realtime_blur else ("_raw" if needs_post_blur else "")

        # Start parallel Twitch chat recorder (#15, #32, #7 Live Auto-Clipper, #10 Keyword Trigger)
        first_base = out_dir / f"{session_ts}_{sanitize_filename(uname)}{suffix}.{ext}"
        self._current_active_file: Path = first_base

        def _on_chat_keyword(t_sec: float, kw: str, msg_preview: str) -> None:
            self._markers.append({"t": t_sec, "note": f"Чат «{kw}»: {msg_preview}"})
            self.log_message.emit(f"[{uname}] Авто-маркер по слову «{kw}» ({format_duration(t_sec)})")

        def _on_chat_hype_spike(t_sec: float, score: int) -> None:
            self._markers.append({"t": t_sec, "note": f"Взрыв чата ({score} балл.)"})
            self.log_message.emit(f"[{uname}] Взрыв чата ({score} балл. на {format_duration(t_sec)}) — авто-клип 45 сек...")
            self.clip_retrospective(45, tag=f"hype_{int(t_sec)}s")

        if self.channel.platform == "twitch" and self.channel.record_chat:
            self._chat_rec = TwitchChatRecorder(
                uname,
                str(first_base),
                chat_position=self.app_config.chat_position,
                chat_font_size=self.app_config.chat_font_size,
                chat_hide_bots=self.app_config.chat_hide_bots,
                trigger_words=self.channel.chat_trigger_words,
                on_keyword_cb=_on_chat_keyword,
                auto_clip_threshold=self.channel.auto_clip_threshold if self.channel.live_auto_clip else 0,
                on_hype_spike_cb=_on_chat_hype_spike if self.channel.live_auto_clip else None,
            )
            self._chat_rec.start()
            self.log_message.emit(f"[{uname}] Параллельная запись чата Twitch запущена.")

        self._start_wall = time.time()
        attempt = 0
        max_reconnect_fails = 4 if self.channel.auto_reconnect else 0
        consecutive_fails = 0
        part_idx = 0

        while not self._stop_requested:
            part_idx += 1
            attempt += 1
            part_tag = f"_part{part_idx:02d}" if part_idx > 1 else ""
            file_prefix = f"{session_ts}_{sanitize_filename(uname)}{part_tag}"
            if self.channel.segment_minutes > 0:
                out_pattern = str(out_dir / f"{file_prefix}_%03d{suffix}.{ext}")
                primary_file = Path(out_pattern.replace("%03d", "000"))
            else:
                out_pattern = str(out_dir / f"{file_prefix}{suffix}.{ext}")
                primary_file = Path(out_pattern)
            self._current_active_file = primary_file

            ok, produced = self._record_single_pass(
                streamlink_cmd, ffmpeg, out_pattern, primary_file, file_prefix, ext, regions, use_realtime_blur, needs_post_blur
            )

            if produced:
                self.recorded_files.extend(produced)
                consecutive_fails = 0
            else:
                consecutive_fails += 1

            if self._stop_requested:
                break

            if self.channel.auto_reconnect and consecutive_fails <= max_reconnect_fails:
                # #3 Smart end-of-stream check: avoid 40s reconnect loop if streamer actually went offline
                if ok and self.channel.platform == "twitch":
                    try:
                        chk = check_channels_status(
                            [self.channel],
                            timeout=6,
                            oauth_token=self.app_config.twitch_oauth_token,
                            proxy_url=self.app_config.proxy_url,
                        )
                        st_now = chk.get(self.channel.key)
                        if st_now and not st_now.is_live and not st_now.error:
                            self.log_message.emit(f"[{uname}] Стример завершил эфир — запись остановлена.")
                            break
                    except Exception:
                        pass

                wait_s = 6 if ok else 10
                self.status_changed.emit(key, f"Переподключение ({consecutive_fails}/{max_reconnect_fails})...")
                self.log_message.emit(f"[{uname}] Поток прервался. Попытка переподключения через {wait_s} сек...")
                for _ in range(wait_s * 10):
                    if self._stop_requested:
                        break
                    time.sleep(0.1)
            else:
                break

        # Stop chat recorder
        if self._chat_rec:
            self._chat_rec.stop()
            if self._chat_rec.messages:
                self.log_message.emit(f"[{uname}] Чат сохранён ({len(self._chat_rec.messages)} сообщений).")

        # Auto-concat dropped parts if enabled (#8)
        final_files = list(self.recorded_files)
        if len(final_files) > 1 and self.channel.auto_concat_parts and self.channel.segment_minutes == 0:
            merged_path = str(out_dir / f"{session_ts}_{sanitize_filename(uname)}_full{suffix}.{ext}")
            self.log_message.emit(f"[{uname}] Авто-склейка {len(final_files)} частей в {Path(merged_path).name}...")
            c_ok, _ = concat_videos(final_files, merged_path, normalize_clips=False)
            if c_ok:
                # Rename chat sidecars to match merged_path
                for side_ext in (".chat.json", ".chat.ass"):
                    old_side = first_base.with_suffix(side_ext)
                    new_side = Path(merged_path).with_suffix(side_ext)
                    if old_side.exists() and old_side != new_side:
                        try:
                            old_side.replace(new_side)
                        except Exception:
                            pass
                for p in final_files:
                    try:
                        os.remove(p)
                    except Exception:
                        pass
                final_files = [merged_path]

        # Save manual F9 markers (#19)
        if self._markers and final_files:
            j_m, edl_m = save_markers_and_edl(final_files[0], self._markers)
            self.log_message.emit(
                f"[{uname}] Сохранено {len(self._markers)} маркеров -> {Path(edl_m).name}"
            )

        # Save session metadata (.meta.json #9) & optional separate clean audio track (#9)
        if final_files:
            if self.channel.separate_audio_track:
                try:
                    mp3_out = str(Path(final_files[0]).with_suffix(".mp3"))
                    self.log_message.emit(f"[{uname}] Извлечение отдельной аудиодорожки -> {Path(mp3_out).name}...")
                    ok_a, _ = extract_audio_track(final_files[0], mp3_out, "mp3")
                    if ok_a:
                        self.log_message.emit(f"[{uname}] Аудиодорожка сохранена: {Path(mp3_out).name}")
                except Exception:
                    pass
            try:
                meta_payload = {
                    "channel": uname,
                    "platform": self.channel.platform,
                    "title": self.stream_meta.get("title", ""),
                    "game": self.stream_meta.get("game", ""),
                    "viewers": self.stream_meta.get("viewers", 0),
                    "started_at": session_ts,
                    "duration_sec": round(max(0.0, time.time() - self._start_wall), 1),
                    "markers_count": len(self._markers),
                }
                Path(final_files[0]).with_suffix(".meta.json").write_text(
                    json.dumps(meta_payload, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            except Exception:
                pass

        success = len(final_files) > 0
        self.finished_recording.emit(key, final_files, success, needs_post_blur)

    def _record_single_pass(
        self,
        streamlink_cmd: List[str],
        ffmpeg: str,
        out_pattern: str,
        primary_file: Path,
        file_prefix: str,
        ext: str,
        regions: List[BlurRegion],
        use_realtime_blur: bool,
        needs_post_blur: bool,
    ) -> Tuple[bool, List[str]]:
        key = self.channel.key
        uname = self.channel.username

        sl_cmd = [
            *streamlink_cmd,
            "--stdout",
            "--hls-live-restart",
            "--ringbuffer-size", "32M",
            "--stream-segment-timeout", "15",
            "--stream-timeout", "30",
            "--stream-segment-attempts", "4",
            "--stream-segment-threads", "2",
            "--retry-streams", "4",
            "--retry-max", "3",
        ]
        if self.channel.platform == "twitch":
            # Request Twitch Enhanced Broadcasting (HEVC / 1440p60 / high-bitrate 1080p60) when available
            sl_cmd.append("--twitch-supported-codecs=h265,h264")
            if self.channel.disable_ads:
                sl_cmd.append("--twitch-disable-ads")
            if self.app_config.twitch_oauth_token.strip():
                tok = self.app_config.twitch_oauth_token.strip()
                tok = tok[6:] if tok.lower().startswith("oauth:") else tok
                sl_cmd.append(f"--twitch-api-header=Authorization=OAuth {tok}")
        if self.app_config.proxy_url.strip():
            sl_cmd.extend(["--http-proxy", self.app_config.proxy_url.strip()])
        q_arg = f"{self.channel.quality},best" if self.channel.quality != "best" else "best"
        sl_cmd.extend([self.channel.stream_url, q_arg])

        ff_cmd = [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "info",
            "-fflags", "+genpts+discardcorrupt",
            "-i", "pipe:0",
        ]

        if use_realtime_blur:
            filter_graph, extra_inputs = build_blur_filter_graph(regions)
            enc = resolve_encoder(self.app_config.encoder)
            if filter_graph:
                ff_cmd.extend(extra_inputs)
                ff_cmd.extend(["-filter_complex", filter_graph, "-map", "[vout]", "-map", "0:a?"])
                ff_cmd.extend(encoder_ffmpeg_args(enc, self.app_config.video_quality))
                ff_cmd.extend(["-c:a", "copy"])
                self.log_message.emit(
                    f"[{uname}] Запись с РЕАЛТАЙМ-блюром ({len(regions)} зон, кодек: {enc})"
                )
            else:
                ff_cmd.extend(["-c", "copy"])
        else:
            ff_cmd.extend(["-c", "copy"])
            note = " (авто-блюр в очередь после эфира)" if needs_post_blur else ""
            self.log_message.emit(f"[{uname}] Запись без перекодирования{note} -> {primary_file.name}")

        if self.channel.segment_minutes > 0:
            seg_sec = self.channel.segment_minutes * 60
            if not use_realtime_blur and ext == "mp4":
                ff_cmd.extend(["-bsf:a", "aac_adtstoasc"])
            ff_cmd.extend([
                "-f", "segment", "-segment_time", str(seg_sec),
                "-reset_timestamps", "1", "-strftime", "0",
            ])
            if ext == "mp4":
                ff_cmd.extend(["-segment_format_options", "movflags=+frag_keyframe+empty_moov+default_base_moof"])
            self.log_message.emit(f"[{uname}] Авто-разбивка записи: по {self.channel.segment_minutes} мин.")
        elif ext == "mp4":
            if not use_realtime_blur:
                ff_cmd.extend(["-bsf:a", "aac_adtstoasc"])
            ff_cmd.extend(["-movflags", "+frag_keyframe+empty_moov+default_base_moof"])

        ff_cmd.append(out_pattern)
        self.status_changed.emit(key, "Подключение...")

        def _match_pass_files() -> List[Path]:
            if self.channel.segment_minutes > 0:
                return [
                    p for p in primary_file.parent.glob(f"{file_prefix}_*.{ext}")
                    if p.exists()
                ]
            return [primary_file] if primary_file.exists() else []

        sl_err_lines: Deque[str] = deque(maxlen=30)
        sl_drain: Optional[threading.Thread] = None
        try:
            self._sl_proc = subprocess.Popen(
                sl_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=CREATE_NO_WINDOW
            )
            sl_drain = _start_pipe_drain(self._sl_proc.stderr, sl_err_lines)
            self._ff_proc = subprocess.Popen(
                ff_cmd,
                stdin=self._sl_proc.stdout,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace",
                creationflags=CREATE_NO_WINDOW,
            )
            if self._sl_proc.stdout:
                self._sl_proc.stdout.close()

            started = False
            last_emit = 0.0
            pass_start = time.time()
            last_progress_ts = [time.time()]
            wd_stop = threading.Event()
            last_errs: List[str] = []

            def _stall_watchdog() -> None:
                while not wd_stop.wait(5.0):
                    if self._stop_requested:
                        return
                    idle_s = time.time() - last_progress_ts[0]
                    limit_s = 50.0 if started else 45.0
                    if idle_s >= limit_s:
                        self.log_message.emit(
                            f"[{uname}] [Watchdog] Поток завис ({int(idle_s)}с без данных) — перезапуск соединения..."
                        )
                        for p in (self._sl_proc, self._ff_proc):
                            if p and p.poll() is None:
                                try:
                                    p.terminate()
                                except Exception:
                                    pass
                        return

            wd_thread = threading.Thread(target=_stall_watchdog, daemon=True)
            wd_thread.start()

            assert self._ff_proc.stderr is not None
            buf = ""
            while True:
                ch = self._ff_proc.stderr.read(1)
                if not ch:
                    break
                if ch in ("\r", "\n"):
                    line = buf.strip()
                    buf = ""
                    if not line:
                        continue
                    if "time=" in line:
                        last_progress_ts[0] = time.time()
                        if not started:
                            started = True
                            self.status_changed.emit(key, "Запись (блюр)" if use_realtime_blur else "Запись")
                        now = time.time()
                        if now - last_emit >= 0.8:
                            last_emit = now
                            dur_str = format_duration(now - pass_start)
                            tm = self._TIME_RE.search(line)
                            if tm:
                                dur_str = format_duration(int(tm.group(1)) * 3600 + int(tm.group(2)) * 60 + float(tm.group(3)))
                            sp = self._SPEED_RE.search(line)
                            speed_str = sp.group(1) if sp else "1.0x"
                            fps_m = self._FPS_RE.search(line)
                            br_m = self._BITRATE_RE.search(line)
                            extras = [speed_str]
                            if fps_m and float(fps_m.group(1)) > 0:
                                extras.append(f"{int(round(float(fps_m.group(1))))}fps")
                            if br_m:
                                extras.append(br_m.group(1).replace("bits/s", "bps"))
                            # Sum size of current pass segment(s) only
                            cur_files = _match_pass_files()
                            total_sz = sum(p.stat().st_size for p in cur_files if p.exists())
                            self.progress_updated.emit(key, dur_str, format_bytes(total_sz), " · ".join(extras))
                    else:
                        last_errs.append(line)
                        if len(last_errs) > 10:
                            last_errs.pop(0)
                else:
                    buf += ch

            wd_stop.set()
            try:
                self._ff_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._ff_proc.kill()
                self._ff_proc.wait(timeout=3)

            if self._sl_proc:
                try:
                    self._sl_proc.wait(timeout=4)
                except Exception:
                    try:
                        self._sl_proc.kill()
                        self._sl_proc.wait(timeout=2)
                    except Exception:
                        pass
            if sl_drain:
                sl_drain.join(timeout=1.5)

            pass_files = _match_pass_files()
            produced = sorted(
                str(p)
                for p in pass_files
                if p.exists() and p.stat().st_size > 10_000
            )
            if produced:
                tot = sum(os.path.getsize(p) for p in produced)
                self.log_message.emit(f"[{uname}] Сегмент сохранён ({len(produced)} файл., {format_bytes(tot)})")
                return True, produced

            # Clean tiny broken file created by this pass only
            for p in pass_files:
                if p.exists() and p.stat().st_size <= 10_000:
                    try:
                        p.unlink()
                    except Exception:
                        pass
            reason = sl_err_lines[-1] if sl_err_lines else (last_errs[-1] if last_errs else "Поток недоступен")
            if not self._stop_requested:
                self.log_message.emit(f"[{uname}] Ошибка потока: {reason}")
            return False, []
        except Exception as exc:
            self.log_message.emit(f"[{uname}] Ошибка записи: {exc}")
            return False, []


class VodDownloadWorker(QThread):
    """Downloads a Twitch VOD / Clip via yt-dlp with OAuth, Proxy, concurrent fragments, and time range."""

    progress_updated = Signal(str, float, str)
    log_message = Signal(str)
    finished_download = Signal(str, str, bool)

    _PCT_RE = re.compile(r"\[download\]\s+(\d+(?:\.\d+)?)%\s+of\s+~?\s*([^\s]+)\s+at\s+([^\s]+)\s+ETA\s+([^\s]+)")
    _PCT_SIMPLE_RE = re.compile(r"\[download\]\s+(\d+(?:\.\d+)?)%")

    def __init__(
        self,
        job_id: str,
        vod_url: str,
        quality: str,
        app_config: AppConfig,
        start_time: str = "",
        end_time: str = "",
        subdir: str = "VODs",
        parent=None,
    ):
        super().__init__(parent)
        self.job_id = job_id
        self.vod_url = vod_url.strip()
        self.quality = quality
        self.app_config = app_config
        self.start_time = start_time.strip()
        self.end_time = end_time.strip()
        self.subdir = subdir
        self._stop_requested = False
        self._proc: Optional[subprocess.Popen] = None

    def request_stop(self) -> None:
        self._stop_requested = True
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.terminate()
            except Exception:
                pass

    def _build_format_selector(self) -> str:
        q = self.quality.lower()
        if q in ("best", "source", "лучшее"):
            return "bestvideo+bestaudio/best"
        m = re.match(r"(\d+)p", q)
        if m:
            h = m.group(1)
            return f"bestvideo[height<={h}]+bestaudio/best[height<={h}]/best"
        return "best"

    def run(self) -> None:
        ytdlp_cmd = get_ytdlp_cmd()
        ffmpeg = get_ffmpeg_path()
        if not ytdlp_cmd:
            self.log_message.emit(f"[VOD] Ошибка: yt-dlp не найден!")
            self.finished_download.emit(self.job_id, "", False)
            return

        vod_dir = self.app_config.ensure_output_dir() / self.subdir
        vod_dir.mkdir(parents=True, exist_ok=True)
        out_tmpl = str(vod_dir / "%(upload_date>%Y-%m-%d)s_%(uploader)s_%(id)s.%(ext)s")

        cmd = [
            *ytdlp_cmd, "--newline", "--no-colors",
            "-N", str(self.app_config.vod_concurrent_fragments),
            "-f", self._build_format_selector(),
            "--merge-output-format", self.app_config.container,
            "-o", out_tmpl,
            "--print", "after_move:FINAL_PATH:%(filepath)s",
        ]
        if ffmpeg:
            cmd.extend(["--ffmpeg-location", str(Path(ffmpeg).parent)])
        if self.app_config.proxy_url.strip():
            cmd.extend(["--proxy", self.app_config.proxy_url.strip()])
        if self.app_config.twitch_oauth_token.strip():
            tok = self.app_config.twitch_oauth_token.strip()
            tok = tok[6:] if tok.lower().startswith("oauth:") else tok
            cmd.extend(["--add-header", f"Authorization: OAuth {tok}"])
        if self.start_time or self.end_time:
            st = self.start_time or "00:00:00"
            en = self.end_time or "inf"
            cmd.extend(["--download-sections", f"*{st}-{en}"])

        cmd.append(self.vod_url)
        self.progress_updated.emit(self.job_id, 0.0, "Получение метаданных...")
        self.log_message.emit(f"[VOD] Загрузка: {self.vod_url}")

        final_path = ""
        last_lines: List[str] = []
        try:
            self._proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", creationflags=CREATE_NO_WINDOW,
            )
            assert self._proc.stdout is not None
            for raw in self._proc.stdout:
                line = raw.strip()
                if not line:
                    continue
                if line.startswith("FINAL_PATH:"):
                    final_path = line[len("FINAL_PATH:") :].strip()
                    continue
                m = self._PCT_RE.search(line)
                if m:
                    pct = float(m.group(1))
                    self.progress_updated.emit(self.job_id, pct, f"{pct:.1f}% · {m.group(2)} · {m.group(3)} · ETA {m.group(4)}")
                    continue
                m2 = self._PCT_SIMPLE_RE.search(line)
                if m2:
                    pct = float(m2.group(1))
                    self.progress_updated.emit(self.job_id, pct, f"Загрузка {pct:.1f}%")
                    continue
                if "[Merger]" in line:
                    self.progress_updated.emit(self.job_id, 99.0, "Сборка файла...")
                last_lines.append(line)
                if len(last_lines) > 8:
                    last_lines.pop(0)

            rc = self._proc.wait()
            if self._stop_requested:
                self.progress_updated.emit(self.job_id, 0.0, "Отменено")
                self.finished_download.emit(self.job_id, "", False)
                return

            if rc == 0 and final_path and os.path.exists(final_path):
                fsize = format_bytes(os.path.getsize(final_path))
                self.progress_updated.emit(self.job_id, 100.0, f"Готово ({fsize})")
                self.log_message.emit(f"[VOD] Готово: {Path(final_path).name} ({fsize})")
                self.finished_download.emit(self.job_id, final_path, True)
            else:
                err = last_lines[-1] if last_lines else f"Код {rc}"
                self.progress_updated.emit(self.job_id, 0.0, f"Ошибка: {err[:60]}")
                self.log_message.emit(f"[VOD] Ошибка: {err}")
                self.finished_download.emit(self.job_id, "", False)
        except Exception as exc:
            self.log_message.emit(f"[VOD] Исключение: {exc}")
            self.finished_download.emit(self.job_id, "", False)


# ------------------------------------------------------------------ #27 Batch Top Clips Downloader + Compilation
class ClipsBatchWorker(QThread):
    progress_updated = Signal(str, float, str)
    log_message = Signal(str)
    finished_batch = Signal(str, str, bool)

    def __init__(
        self,
        job_id: str,
        channel: str,
        clip_urls: List[str],
        merge_compilation: bool,
        app_config: AppConfig,
        parent=None,
    ):
        super().__init__(parent)
        self.job_id = job_id
        self.channel = channel
        self.clip_urls = list(clip_urls)
        self.merge_compilation = merge_compilation
        self.app_config = app_config
        self._stop = False
        self._proc: Optional[subprocess.Popen] = None

    def request_stop(self) -> None:
        self._stop = True
        p = self._proc
        if p and p.poll() is None:
            try:
                p.terminate()
            except Exception:
                pass

    def run(self) -> None:
        ytdlp_cmd = get_ytdlp_cmd()
        ffmpeg = get_ffmpeg_path()
        if not ytdlp_cmd:
            self.finished_batch.emit(self.job_id, "", False)
            return

        ts = datetime.now().strftime("%Y-%m-%d_%H-%M")
        clips_dir = self.app_config.ensure_output_dir() / "Clips" / f"{sanitize_filename(self.channel)}_{ts}"
        clips_dir.mkdir(parents=True, exist_ok=True)

        downloaded: List[str] = []
        total = max(1, len(self.clip_urls))
        for idx, url in enumerate(self.clip_urls, 1):
            if self._stop:
                self.progress_updated.emit(self.job_id, 0.0, "Отменено")
                self.finished_batch.emit(self.job_id, "", False)
                return
            pct = ((idx - 1) / total) * (80.0 if self.merge_compilation else 100.0)
            self.progress_updated.emit(self.job_id, pct, f"Скачивание клипа {idx}/{total}...")
            out_tmpl = str(clips_dir / f"{idx:02d}_%(id)s.%(ext)s")
            cmd = [*ytdlp_cmd, "--no-colors", "-f", "best", "-o", out_tmpl, "--print", "after_move:PATH:%(filepath)s"]
            if ffmpeg:
                cmd.extend(["--ffmpeg-location", str(Path(ffmpeg).parent)])
            if self.app_config.proxy_url.strip():
                cmd.extend(["--proxy", self.app_config.proxy_url.strip()])
            if self.app_config.twitch_oauth_token.strip():
                tok = self.app_config.twitch_oauth_token.strip()
                tok = tok[6:] if tok.lower().startswith("oauth:") else tok
                cmd.extend(["--add-header", f"Authorization: OAuth {tok}"])
            cmd.append(url)
            try:
                self._proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, encoding="utf-8", errors="replace", creationflags=CREATE_NO_WINDOW,
                )
                try:
                    out_txt, _ = self._proc.communicate(timeout=180)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
                    out_txt, _ = self._proc.communicate()
                finally:
                    self._proc = None
                for line in (out_txt or "").splitlines():
                    if line.startswith("PATH:"):
                        fp = line[5:].strip()
                        if os.path.exists(fp):
                            downloaded.append(fp)
            except Exception:
                pass

        if not downloaded:
            self.progress_updated.emit(self.job_id, 0.0, "Не удалось скачать клипы")
            self.finished_batch.emit(self.job_id, "", False)
            return

        if self.merge_compilation and len(downloaded) >= 2:
            self.progress_updated.emit(self.job_id, 85.0, f"Склейка {len(downloaded)} клипов в подборку...")
            comp_path = str(clips_dir / f"{sanitize_filename(self.channel)}_compilation_{ts}.mp4")
            enc = resolve_encoder(self.app_config.encoder)
            ok, msg = concat_videos(downloaded, comp_path, normalize_clips=True, encoder=enc, quality=self.app_config.video_quality)
            if ok:
                self.progress_updated.emit(self.job_id, 100.0, f"Подборка готова ({len(downloaded)} клипов)")
                self.log_message.emit(f"[Клипы] Подборка готова: {Path(comp_path).name}")
                self.finished_batch.emit(self.job_id, comp_path, True)
                return
            self.log_message.emit(f"[Клипы] Ошибка склейки: {msg}")

        self.progress_updated.emit(self.job_id, 100.0, f"Скачано клипов: {len(downloaded)}")
        self.log_message.emit(f"[Клипы] Скачано {len(downloaded)} клипов в {clips_dir}")
        self.finished_batch.emit(self.job_id, downloaded[0], True)


# ------------------------------------------------------------------ Blur Post-Processing Worker (#1, #2, #3, #5, #23)
class BlurPostProcessWorker(QThread):
    progress_updated = Signal(str, float, str)
    log_message = Signal(str)
    finished_blur = Signal(str, str, bool)

    _TIME_RE = re.compile(r"out_time_us=(\d+)")
    _SPEED_RE = re.compile(r"speed=\s*([\d.]+x)")

    def __init__(
        self,
        job_id: str,
        input_path: str,
        regions: List[BlurRegion],
        app_config: AppConfig,
        output_path: Optional[str] = None,
        delete_original: bool = False,
        normalize_audio: bool = False,
        parent=None,
    ):
        super().__init__(parent)
        self.job_id = job_id
        self.input_path = input_path
        self.regions = [r.clamped() for r in regions]
        self.app_config = app_config
        self.delete_original = delete_original
        self.normalize_audio = normalize_audio
        self._stop_requested = False
        self._proc: Optional[subprocess.Popen] = None

        in_p = Path(input_path)
        if output_path:
            self.output_path = output_path
        else:
            stem = in_p.stem[:-4] if in_p.stem.endswith("_raw") else in_p.stem
            self.output_path = str(in_p.with_name(f"{stem}_blurred{in_p.suffix}"))

    def request_stop(self) -> None:
        self._stop_requested = True
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.terminate()
            except Exception:
                pass

    def run(self) -> None:
        ffmpeg = get_ffmpeg_path()
        if not ffmpeg or not os.path.exists(self.input_path):
            self.finished_blur.emit(self.job_id, "", False)
            return

        # Step 1: Smart OpenCV Banner Template Auto-Detection (#3) if enabled on any region
        detected_map: Dict[int, List[Tuple[float, float]]] = {}
        for idx, r in enumerate(self.regions):
            if r.auto_detect and r.template_path and os.path.exists(r.template_path):
                self.progress_updated.emit(self.job_id, 2.0, f"Поиск баннера «{r.label}» в видео...")
                self.log_message.emit(f"[Авто-детектор] Сканирование кадров для зоны «{r.label}»...")
                ivs = detect_banner_intervals(
                    self.input_path,
                    r,
                    sample_fps=1.0,
                    progress_cb=lambda p: self.progress_updated.emit(
                        self.job_id, p * 0.25, f"Поиск баннера: {p:.0f}%"
                    ),
                )
                detected_map[idx] = ivs
                self.log_message.emit(f"[Авто-детектор] Зона «{r.label}»: найдено {len(ivs)} появлений баннера.")

        filter_graph, extra_inputs = build_blur_filter_graph(self.regions, detected_map=detected_map)
        if not filter_graph:
            self.finished_blur.emit(self.job_id, "", False)
            return

        meta = probe_video(self.input_path)
        total_dur = meta.get("duration", 0.0)
        enc = resolve_encoder(self.app_config.encoder)

        # #1 Smart Segment Blur: if ALL regions are time-bounded or AI-detected, check if we can lossless-copy
        # the clean portions of the stream and re-encode ONLY the active banner intervals!
        if (
            getattr(self.app_config, "smart_segment_blur", True)
            and not self.normalize_audio
            and total_dur > 25.0
            and all((idx in detected_map) or (r.start_sec is not None or r.end_sec is not None) for idx, r in enumerate(self.regions))
        ):
            raw_ivs: List[Tuple[float, float]] = []
            for idx, r in enumerate(self.regions):
                if idx in detected_map:
                    raw_ivs.extend(detected_map[idx])
                else:
                    s_i = float(r.start_sec or 0.0)
                    e_i = float(r.end_sec if r.end_sec is not None else total_dur)
                    if e_i > s_i:
                        raw_ivs.append((s_i, e_i))
            active_ivs = merge_time_intervals(raw_ivs, total_dur, pad_sec=1.5, min_gap_sec=4.0)
            active_dur = sum(e - s for s, e in active_ivs)
            if not active_ivs:
                # Banner never appeared! Instant lossless copy (0% quality loss, 100x speed)
                self.log_message.emit(f"[Smart Segment Blur] Баннер в кадре не обнаружен — мгновенное копирование без потерь!")
                import shutil
                shutil.copy2(self.input_path, self.output_path)
                self._finalize_blur_success()
                return
            if active_dur < total_dur * 0.65 and len(active_ivs) <= 25:
                if self._run_smart_segment_blur(ffmpeg, enc, total_dur, active_ivs):
                    self._finalize_blur_success()
                    return

        cmd = [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-progress", "pipe:1", "-nostats",
            "-i", self.input_path,
            *extra_inputs,
            "-filter_complex", filter_graph,
            "-map", "[vout]", "-map", "0:a?",
            *encoder_ffmpeg_args(enc, self.app_config.video_quality),
        ]
        if self.normalize_audio:
            cmd.extend(["-af", "loudnorm=I=-14:TP=-1.5:LRA=11", "-c:a", "aac", "-b:a", "192k"])
        else:
            cmd.extend(["-c:a", "copy"])

        if Path(self.output_path).suffix.lower() == ".mp4":
            cmd.extend(["-movflags", "+faststart"])
        cmd.append(self.output_path)

        base_pct = 25.0 if detected_map else 0.0
        scale_pct = 0.75 if detected_map else 1.0
        self.progress_updated.emit(self.job_id, base_pct, f"Кодирование ({enc})...")
        self.log_message.emit(
            f"[Блюр] Обработка {Path(self.input_path).name} ({len(self.regions)} зон, кодек: {enc})"
        )

        err_lines: Deque[str] = deque(maxlen=40)
        err_drain: Optional[threading.Thread] = None
        try:
            self._proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace",
                **low_priority_popen_kwargs(self.app_config.low_priority_render),
            )
            err_drain = _start_pipe_drain(self._proc.stderr, err_lines)
            assert self._proc.stdout is not None
            cur_speed, cur_us, last_emit = "", 0, 0.0
            for raw in self._proc.stdout:
                line = raw.strip()
                m_t = self._TIME_RE.match(line)
                if m_t:
                    cur_us = int(m_t.group(1))
                m_s = self._SPEED_RE.match(line)
                if m_s:
                    cur_speed = m_s.group(1)
                if line in ("progress=continue", "progress=end"):
                    now = time.time()
                    if now - last_emit >= 0.4 or line == "progress=end":
                        last_emit = now
                        cur_sec = cur_us / 1_000_000.0
                        raw_pct = min(99.9, (cur_sec / total_dur) * 100.0) if total_dur > 0 else 0.0
                        pct = 100.0 if line == "progress=end" else (base_pct + raw_pct * scale_pct)
                        sp_txt = f" · {cur_speed}" if cur_speed else ""
                        self.progress_updated.emit(
                            self.job_id, pct,
                            f"{pct:.1f}% ({format_duration(cur_sec)} / {format_duration(total_dur)}){sp_txt}",
                        )

            rc = self._proc.wait()
            if err_drain:
                err_drain.join(timeout=1.5)
            err_out = "\n".join(err_lines)

            if self._stop_requested:
                if os.path.exists(self.output_path):
                    try:
                        os.remove(self.output_path)
                    except Exception:
                        pass
                self.progress_updated.emit(self.job_id, 0.0, "Отменено")
                self.finished_blur.emit(self.job_id, "", False)
                return

            if rc == 0 and os.path.exists(self.output_path) and os.path.getsize(self.output_path) > 10_000:
                self._finalize_blur_success()
            else:
                err_msg = (err_out or f"Код {rc}").strip().splitlines()[-1]
                self.progress_updated.emit(self.job_id, 0.0, f"Ошибка: {err_msg[:60]}")
                self.log_message.emit(f"[Блюр] Ошибка FFmpeg: {err_msg}")
                self.finished_blur.emit(self.job_id, "", False)
        except Exception as exc:
            self.log_message.emit(f"[Блюр] Ошибка: {exc}")
            self.finished_blur.emit(self.job_id, "", False)

    def _finalize_blur_success(self) -> None:
        fsize = format_bytes(os.path.getsize(self.output_path))
        for side_ext in (".chat.json", ".chat.ass", ".markers.json", ".meta.json", ".edl"):
            src_side = Path(self.input_path).with_suffix(side_ext)
            dst_side = Path(self.output_path).with_suffix(side_ext)
            if src_side.exists() and not dst_side.exists():
                try:
                    if self.delete_original:
                        src_side.replace(dst_side)
                    else:
                        import shutil
                        shutil.copy2(src_side, dst_side)
                except Exception:
                    pass
        if self.delete_original and os.path.abspath(self.input_path) != os.path.abspath(self.output_path):
            try:
                os.remove(self.input_path)
            except Exception:
                pass
        self.progress_updated.emit(self.job_id, 100.0, f"Готово ({fsize})")
        self.log_message.emit(f"[Блюр] Готово: {Path(self.output_path).name} ({fsize})")
        self.finished_blur.emit(self.job_id, self.output_path, True)

    def _run_smart_segment_blur(
        self,
        ffmpeg: str,
        enc: str,
        total_dur: float,
        active_ivs: List[Tuple[float, float]],
    ) -> bool:
        """
        Re-encode ONLY the active banner intervals and stream-copy the untouched segments,
        then concatenate seamlessly. Saves up to 95% time and preserves 100% original quality outside banner windows.
        """
        active_dur = sum(e - s for s, e in active_ivs)
        self.log_message.emit(
            f"[Smart Segment Blur] Перекодируем только {format_duration(active_dur)} из {format_duration(total_dur)} "
            f"({len(active_ivs)} интерв., остальное 1:1 оригинал)..."
        )
        # Build full timeline partition: [(start, end, needs_blur), ...]
        timeline: List[Tuple[float, float, bool]] = []
        cursor = 0.0
        for s, e in active_ivs:
            if s - cursor >= 0.5:
                timeline.append((cursor, s, False))
            timeline.append((s, e, True))
            cursor = e
        if total_dur - cursor >= 0.5:
            timeline.append((cursor, total_dur, False))

        # Unbounded regions for active chunks (blur is active throughout each active chunk)
        unbounded_regs = [
            BlurRegion(
                x=r.x, y=r.y, w=r.w, h=r.h, mode=r.mode, strength=r.strength,
                feather=r.feather, image_path=r.image_path, chromakey=r.chromakey,
                chromakey_color=r.chromakey_color, chromakey_similarity=r.chromakey_similarity,
                label=r.label,
            )
            for r in self.regions
        ]
        fg, extra_in = build_blur_filter_graph(unbounded_regs)
        if not fg:
            return False

        tmp_dir = Path(self.output_path).parent / f".tmp_sblur_{self.job_id}"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        part_files: List[str] = []
        ext = Path(self.output_path).suffix or ".mp4"
        try:
            for idx, (s, e, is_blur) in enumerate(timeline):
                if self._stop_requested:
                    return False
                dur = max(0.2, e - s)
                pct = 25.0 + (idx / max(1, len(timeline))) * 68.0
                self.progress_updated.emit(
                    self.job_id, pct,
                    f"Smart Blur {idx + 1}/{len(timeline)} ({'GPU Блюр' if is_blur else '1:1 Копия'})...",
                )
                pf = str(tmp_dir / f"part_{idx:04d}{ext}")
                if is_blur:
                    cmd = [
                        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                        "-ss", f"{s:.3f}", "-i", self.input_path, "-t", f"{dur:.3f}",
                        *extra_in, "-filter_complex", fg, "-map", "[vout]", "-map", "0:a?",
                        *encoder_ffmpeg_args(enc, self.app_config.video_quality),
                        "-c:a", "copy", pf,
                    ]
                    res = subprocess.run(cmd, capture_output=True, text=True, timeout=900, creationflags=CREATE_NO_WINDOW)
                    if res.returncode != 0 or not os.path.exists(pf):
                        return False
                else:
                    ok_c, _ = trim_video_fast(self.input_path, s, e, pf, accurate_reencode=False)
                    if not ok_c or not os.path.exists(pf):
                        return False
                part_files.append(pf)

            self.progress_updated.emit(self.job_id, 95.0, "Бесшовная сборка Smart Segment Blur...")
            ok_cat, _ = concat_videos(part_files, self.output_path, normalize_clips=False)
            return bool(ok_cat and os.path.exists(self.output_path) and os.path.getsize(self.output_path) > 10_000)
        except Exception:
            return False
        finally:
            for pf in part_files:
                if os.path.exists(pf):
                    try:
                        os.remove(pf)
                    except Exception:
                        pass
            try:
                tmp_dir.rmdir()
            except Exception:
                pass


# ------------------------------------------------------------------ Studio Processing Worker (#8, #17, #20, #21, #23, #24, #25)
class StudioToolWorker(QThread):
    """Unified background worker for Trimmer, Concat, Shorts 9:16, Silence/AFK cut, Chat burn-in, and Export Presets."""

    progress_updated = Signal(str, float, str)
    log_message = Signal(str)
    finished_task = Signal(str, str, bool)

    def __init__(self, job_id: str, action: str, params: dict, app_config: AppConfig, parent=None):
        super().__init__(parent)
        self.job_id = job_id
        self.action = action
        self.params = params
        self.app_config = app_config
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

    _TIME_RE = re.compile(r"out_time_us=(\d+)")
    _SPEED_RE = re.compile(r"speed=\s*([\d.]+x)")

    def _run_ffmpeg_with_progress(
        self,
        cmd: List[str],
        total_dur: float,
        label: str,
        timeout: int = 1800,
    ) -> Tuple[int, str]:
        """Run FFmpeg with -progress pipe:1 so Studio tasks show real-time % and speed (#46)."""
        # Inject -progress pipe:1 -nostats right after ffmpeg binary
        full_cmd = [cmd[0], "-progress", "pipe:1", "-nostats", *cmd[1:]]
        err_lines: Deque[str] = deque(maxlen=40)
        self._proc = subprocess.Popen(
            full_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            **low_priority_popen_kwargs(self.app_config.low_priority_render),
        )
        err_drain = _start_pipe_drain(self._proc.stderr, err_lines)
        try:
            assert self._proc.stdout is not None
            cur_us, cur_speed, last_emit = 0, "", 0.0
            for raw in self._proc.stdout:
                line = raw.strip()
                mt = self._TIME_RE.match(line)
                if mt:
                    cur_us = int(mt.group(1))
                ms = self._SPEED_RE.match(line)
                if ms:
                    cur_speed = ms.group(1)
                if line in ("progress=continue", "progress=end"):
                    now = time.time()
                    if now - last_emit >= 0.4 or line == "progress=end":
                        last_emit = now
                        cur_sec = cur_us / 1_000_000.0
                        pct = 100.0 if line == "progress=end" else (
                            min(99.5, (cur_sec / total_dur) * 100.0) if total_dur > 0 else 25.0
                        )
                        sp_txt = f" · {cur_speed}" if cur_speed else ""
                        self.progress_updated.emit(
                            self.job_id, pct, f"{label}: {pct:.1f}%{sp_txt}"
                        )
            rc = self._proc.wait(timeout=timeout)
            if err_drain:
                err_drain.join(timeout=1.5)
            return rc or 0, "\n".join(err_lines)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait(timeout=3)
            return 1, "Таймаут FFmpeg"
        finally:
            self._proc = None

    def run(self) -> None:
        ffmpeg = get_ffmpeg_path()
        if not ffmpeg:
            self._emit_done(False, "FFmpeg не найден", "")
            return
        enc = resolve_encoder(self.app_config.encoder)
        q = self.app_config.video_quality
        try:
            if self.action == "trim":
                inp = self.params["input_path"]
                out = self.params["output_path"]
                s_sec = max(0.0, float(self.params["start_sec"]))
                e_sec = float(self.params["end_sec"])
                dur = max(0.5, e_sec - s_sec)
                accurate = bool(self.params.get("accurate", False))
                if accurate:
                    self.progress_updated.emit(self.job_id, 5.0, f"Точная обрезка ({enc})...")
                    cmd = [
                        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                        "-ss", f"{s_sec:.3f}", "-i", inp, "-t", f"{dur:.3f}",
                        *encoder_ffmpeg_args(enc, q),
                        "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", out,
                    ]
                    rc, err = self._run_ffmpeg_with_progress(cmd, dur, f"Обрезка ({enc})", timeout=1800)
                    if self._stop_requested:
                        if os.path.exists(out):
                            try:
                                os.remove(out)
                            except Exception:
                                pass
                        self._emit_done(False, "Отменено", "")
                        return
                    self._emit_done(rc == 0 and os.path.exists(out), out if rc == 0 else err, "Обрезка готова")
                else:
                    self.progress_updated.emit(self.job_id, 25.0, "Быстрая обрезка без перекодирования...")
                    ok, res = trim_video_fast(inp, s_sec, e_sec, out, accurate_reencode=False, encoder=enc, quality=q)
                    self._emit_done(ok, res, "Обрезка готова")

            elif self.action == "multi_cut":
                # #18 Multi-Cut Timeline Compilation from user-selected segments
                inp = self.params["input_path"]
                out = self.params["output_path"]
                segs = self.params.get("segments") or []
                accurate = bool(self.params.get("accurate", True))
                if not segs:
                    self._emit_done(False, "Добавьте хотя бы 1 отрезок", "")
                    return
                parts_dir = Path(out).parent / f".tmp_mc_{self.job_id}"
                parts_dir.mkdir(parents=True, exist_ok=True)
                seg_files: List[str] = []
                try:
                    for idx, seg in enumerate(segs):
                        if self._stop_requested:
                            self._emit_done(False, "Отменено", "")
                            return
                        s, e = float(seg[0]), float(seg[1])
                        if e <= s:
                            continue
                        pct = 10.0 + (idx / max(1, len(segs))) * 75.0
                        self.progress_updated.emit(self.job_id, pct, f"Отрезок {idx + 1}/{len(segs)}...")
                        sf = str(parts_dir / f"mc_{idx:04d}.mp4")
                        ok_t, _ = trim_video_fast(inp, s, e, sf, accurate_reencode=accurate, encoder=enc, quality=q)
                        if ok_t:
                            seg_files.append(sf)
                    if self._stop_requested:
                        self._emit_done(False, "Отменено", "")
                        return
                    self.progress_updated.emit(self.job_id, 90.0, f"Склейка {len(seg_files)} отрезков...")
                    if len(seg_files) == 1:
                        os.replace(seg_files[0], out)
                        ok, res = True, out
                    elif len(seg_files) > 1:
                        ok, res = concat_videos(seg_files, out, normalize_clips=False)
                    else:
                        ok, res = False, "Не удалось вырезать отрезки"
                finally:
                    for sf in seg_files:
                        if os.path.exists(sf):
                            try:
                                os.remove(sf)
                            except Exception:
                                pass
                    try:
                        parts_dir.rmdir()
                    except Exception:
                        pass
                self._emit_done(ok, res, f"Нарезка готова ({len(segs)} отрезков)")

            elif self.action == "batch_highlights":
                # #25 Batch-cut all detected highlight timestamps into separate clips
                inp = self.params["input_path"]
                timestamps = self.params.get("timestamps") or []
                pad = float(self.params.get("pad_sec", 20.0))
                if not timestamps:
                    self._emit_done(False, "Нет хайлайтов для нарезки", "")
                    return
                p_in = Path(inp)
                hl_dir = p_in.parent / f"{p_in.stem}_highlights"
                hl_dir.mkdir(parents=True, exist_ok=True)
                created: List[str] = []
                for idx, t in enumerate(timestamps):
                    if self._stop_requested:
                        self._emit_done(False, "Отменено", "")
                        return
                    ts = float(t)
                    s, e = max(0.0, ts - pad), ts + pad
                    pct = (idx / max(1, len(timestamps))) * 95.0
                    self.progress_updated.emit(self.job_id, pct, f"Хайлайт {idx + 1}/{len(timestamps)}...")
                    out_clip = str(hl_dir / f"hl_{idx + 1:02d}_{int(ts)}s{p_in.suffix}")
                    ok_t, _ = trim_video_fast(inp, s, e, out_clip, accurate_reencode=False)
                    if ok_t:
                        created.append(out_clip)
                if created:
                    self._emit_done(True, created[0], f"Нарезано {len(created)} хайлайтов")
                else:
                    self._emit_done(False, "Не удалось нарезать хайлайты", "")

            elif self.action == "concat":
                self.progress_updated.emit(self.job_id, 20.0, "Склейка файлов...")
                ok, res = concat_videos(
                    self.params["input_paths"],
                    self.params["output_path"],
                    normalize_clips=self.params.get("normalize", False),
                    encoder=enc,
                    quality=q,
                )
                self._emit_done(ok, res, "Склейка завершена")

            elif self.action == "remove_silence_afk":
                inp = self.params["input_path"]
                out = self.params["output_path"]
                self.progress_updated.emit(self.job_id, 10.0, "Поиск тишины и AFK-пауз...")
                keeps = detect_active_segments_no_silence_afk(
                    inp,
                    silence_db=self.params.get("silence_db", -35.0),
                    min_silence_sec=self.params.get("min_silence", 3.0),
                    remove_freeze=self.params.get("remove_freeze", True),
                    min_freeze_sec=self.params.get("min_freeze", 8.0),
                )
                if self._stop_requested:
                    self._emit_done(False, "Отменено", "")
                    return
                if not keeps:
                    self._emit_done(False, "Не найдено активных фрагментов", "")
                    return
                self.log_message.emit(f"[Авто-резка] Найдено активных сегментов: {len(keeps)}")
                parts_dir = Path(out).parent / f".tmp_cut_{self.job_id}"
                parts_dir.mkdir(parents=True, exist_ok=True)
                seg_files = []
                try:
                    total_k = len(keeps)
                    for idx, (s, e) in enumerate(keeps):
                        if self._stop_requested:
                            self._emit_done(False, "Отменено", "")
                            return
                        pct = 20.0 + (idx / max(1, total_k)) * 65.0
                        self.progress_updated.emit(self.job_id, pct, f"Фрагмент {idx + 1}/{total_k}...")
                        sf = str(parts_dir / f"seg_{idx:04d}.mp4")
                        ok_t, _ = trim_video_fast(
                            inp, s, e, sf, accurate_reencode=True, encoder=enc, quality=q
                        )
                        if ok_t:
                            seg_files.append(sf)
                    if self._stop_requested:
                        self._emit_done(False, "Отменено", "")
                        return
                    self.progress_updated.emit(self.job_id, 90.0, f"Склейка {len(seg_files)} фрагментов...")
                    if len(seg_files) == 1:
                        os.replace(seg_files[0], out)
                        ok, res = True, out
                    elif len(seg_files) > 1:
                        ok, res = concat_videos(seg_files, out, normalize_clips=False)
                    else:
                        ok, res = False, "Ошибка нарезки"
                finally:
                    for sf in seg_files:
                        if os.path.exists(sf):
                            try:
                                os.remove(sf)
                            except Exception:
                                pass
                    try:
                        parts_dir.rmdir()
                    except Exception:
                        pass
                self._emit_done(ok, res, f"Паузы удалены ({len(keeps)} фрагм.)")

            elif self.action == "shorts":
                inp = self.params["input_path"]
                out = self.params["output_path"]
                total_dur = probe_video(inp).get("duration", 0.0)
                fg, extra_in = build_shorts_9_16_filter(
                    self.params.get("cam_region"),
                    self.params.get("game_region"),
                    self.params.get("blur_regions") or [],
                    split_ratio=self.params.get("split_ratio", 0.36),
                    layout=self.params.get("layout", "split"),
                    progress_bar=bool(self.params.get("progress_bar", False)),
                    total_duration=total_dur,
                    killcam_zoom=bool(self.params.get("killcam_zoom", False)),
                )
                self.progress_updated.emit(self.job_id, 5.0, f"Рендер Shorts 9:16 ({enc})...")
                cmd = [
                    ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                    "-i", inp, *extra_in,
                    "-filter_complex", fg, "-map", "[vout]", "-map", "0:a?",
                    *encoder_ffmpeg_args(enc, q),
                    "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", out,
                ]
                rc, err = self._run_ffmpeg_with_progress(cmd, total_dur, f"Shorts ({enc})", timeout=1800)
                if self._stop_requested:
                    if os.path.exists(out):
                        try:
                            os.remove(out)
                        except Exception:
                            pass
                    self._emit_done(False, "Отменено", "")
                    return
                self._emit_done(rc == 0 and os.path.exists(out), out if rc == 0 else err, "Shorts 9:16 готов")

            elif self.action == "batch_shorts":
                # #15 1-Click Batch Shorts Pipeline from all detected highlights
                inp = self.params["input_path"]
                timestamps = self.params.get("timestamps") or []
                pad = float(self.params.get("pad_sec", 20.0))
                if not timestamps:
                    self._emit_done(False, "Нет хайлайтов для генерации Shorts", "")
                    return
                p_in = Path(inp)
                sh_dir = p_in.parent / f"{p_in.stem}_shorts_9x16"
                sh_dir.mkdir(parents=True, exist_ok=True)
                clip_dur = pad * 2.0
                fg, extra_in = build_shorts_9_16_filter(
                    self.params.get("cam_region"),
                    self.params.get("game_region"),
                    self.params.get("blur_regions") or [],
                    split_ratio=self.params.get("split_ratio", 0.36),
                    layout=self.params.get("layout", "split"),
                    progress_bar=bool(self.params.get("progress_bar", True)),
                    total_duration=clip_dur,
                    killcam_zoom=bool(self.params.get("killcam_zoom", False)),
                )
                created_shorts: List[str] = []
                for idx, t in enumerate(timestamps):
                    if self._stop_requested:
                        self._emit_done(False, "Отменено", "")
                        return
                    ts = float(t)
                    s_t = max(0.0, ts - pad)
                    pct = (idx / max(1, len(timestamps))) * 95.0
                    self.progress_updated.emit(self.job_id, pct, f"Shorts 9:16 ({idx + 1}/{len(timestamps)})...")
                    out_sh = str(sh_dir / f"short_{idx + 1:02d}_{int(ts)}s_9x16.mp4")
                    cmd = [
                        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                        "-ss", f"{s_t:.2f}", "-i", inp, "-t", f"{clip_dur:.2f}",
                        *extra_in,
                        "-filter_complex", fg, "-map", "[vout]", "-map", "0:a?",
                        *encoder_ffmpeg_args(enc, q),
                        "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", out_sh,
                    ]
                    res = subprocess.run(cmd, capture_output=True, text=True, timeout=600, creationflags=CREATE_NO_WINDOW)
                    if res.returncode == 0 and os.path.exists(out_sh) and os.path.getsize(out_sh) > 10_000:
                        created_shorts.append(out_sh)
                if created_shorts:
                    self._emit_done(True, created_shorts[0], f"Создано {len(created_shorts)} Shorts 9:16")
                else:
                    self._emit_done(False, "Не удалось сгенерировать пакет Shorts", "")

            elif self.action == "extract_audio":
                inp = self.params["input_path"]
                out = self.params["output_path"]
                fmt = self.params.get("format", "mp3")
                self.progress_updated.emit(self.job_id, 25.0, f"Извлечение аудио ({fmt.upper()})...")
                ok, res = extract_audio_track(inp, out, fmt=fmt)
                self._emit_done(ok, res, f"Аудиодорожка ({fmt.upper()}) сохранена")

            elif self.action == "burn_chat":
                inp = self.params["input_path"]
                ass_path = self.params["ass_path"]
                out = self.params["output_path"]
                if ass_path and not os.path.exists(ass_path):
                    # Auto-generate .chat.ass from .chat.json if only JSON exists
                    json_cand = re.sub(r"\.chat\.ass$", ".chat.json", ass_path)
                    if os.path.exists(json_cand):
                        try:
                            from app.chat_and_ai import generate_chat_ass
                            raw_c = json.loads(Path(json_cand).read_text(encoding="utf-8"))
                            msgs = raw_c if isinstance(raw_c, list) else (raw_c.get("messages", []) if isinstance(raw_c, dict) else [])
                            if msgs:
                                generate_chat_ass(
                                    msgs,
                                    ass_path,
                                    position=self.app_config.chat_position,
                                    font_size=self.app_config.chat_font_size,
                                    hide_bots=self.app_config.chat_hide_bots,
                                )
                        except Exception:
                            pass
                if not ass_path or not os.path.exists(ass_path):
                    self._emit_done(False, f"Файл чата (.chat.ass / .chat.json) не найден: {ass_path}", "")
                    return
                total_dur = probe_video(inp).get("duration", 0.0)
                self.progress_updated.emit(self.job_id, 5.0, f"Наложение чата ({enc})...")
                esc = escape_ffmpeg_filter_path(str(ass_path))
                cmd = [
                    ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                    "-i", inp, "-vf", f"ass='{esc}'",
                    *encoder_ffmpeg_args(enc, q),
                    "-c:a", "copy", "-movflags", "+faststart", out,
                ]
                rc, err = self._run_ffmpeg_with_progress(cmd, total_dur, f"Чат ({enc})", timeout=1800)
                if self._stop_requested:
                    if os.path.exists(out):
                        try:
                            os.remove(out)
                        except Exception:
                            pass
                    self._emit_done(False, "Отменено", "")
                    return
                self._emit_done(rc == 0 and os.path.exists(out), out if rc == 0 else err, "Чат вшит в видео")

            elif self.action == "export_preset":
                inp = self.params["input_path"]
                out = self.params["output_path"]
                preset_id = self.params["preset_id"]
                normalize_audio = self.params.get("normalize_audio", True)
                voice_enhance = bool(self.params.get("voice_enhance", False))
                watermark_path = (self.params.get("watermark_path") or "").strip()
                pr = EXPORT_PRESETS[preset_id]
                meta = probe_video(inp)
                dur = max(1.0, meta.get("duration", 1.0))

                target_v_kbps = None
                if pr["max_mb"]:
                    total_kbits = pr["max_mb"] * 8192.0
                    target_v_kbps = max(250, int((total_kbits / dur) - pr["audio_kbps"]))

                vf = f"scale=-2:'min({pr['height']},ih)':flags=lanczos"
                if pr.get("fps"):
                    vf += f",fps={pr['fps']}"

                self.progress_updated.emit(self.job_id, 5.0, f"Экспорт ({pr['label']})...")
                if watermark_path and os.path.exists(watermark_path):
                    # #23 Overlay channel watermark in top-right corner (~10% width, 78% opacity)
                    fg = (
                        f"[0:v]{vf},split=2[base][wmref];"
                        f"[1:v]format=rgba,colorchannelmixer=aa=0.78[wmraw];"
                        f"[wmraw][wmref]scale2ref=w='max(64,trunc(iw*0.10/2)*2)':h='trunc(ow/mdar/2)*2'[wm][wmdrop];"
                        f"[wmdrop]nullsink;"
                        f"[base][wm]overlay=W-w-28:24[vout]"
                    )
                    cmd = [
                        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                        "-i", inp, "-i", watermark_path,
                        "-filter_complex", fg, "-map", "[vout]", "-map", "0:a?",
                        *encoder_ffmpeg_args(enc, pr["cq"], target_bitrate_kbps=target_v_kbps),
                    ]
                else:
                    cmd = [
                        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                        "-i", inp, "-vf", vf,
                        *encoder_ffmpeg_args(enc, pr["cq"], target_bitrate_kbps=target_v_kbps),
                    ]

                # #24 Audio processing chain (Voice Compressor + Highpass + Loudnorm)
                af_chain = []
                if voice_enhance:
                    af_chain.extend(["highpass=f=80", "acompressor=threshold=-20dB:ratio=3:attack=10:release=120"])
                if normalize_audio:
                    af_chain.append("loudnorm=I=-14:TP=-1.5:LRA=11")
                if af_chain:
                    cmd.extend(["-af", ",".join(af_chain)])
                cmd.extend(["-c:a", "aac", "-b:a", f"{pr['audio_kbps']}k", "-movflags", "+faststart", out])
                rc, err = self._run_ffmpeg_with_progress(cmd, dur, f"Экспорт ({enc})", timeout=1800)
                if self._stop_requested:
                    if os.path.exists(out):
                        try:
                            os.remove(out)
                        except Exception:
                            pass
                    self._emit_done(False, "Отменено", "")
                    return
                self._emit_done(rc == 0 and os.path.exists(out), out if rc == 0 else err, "Экспорт готов")

            elif self.action == "split_parts":
                from app.tools import split_video_by_duration
                inp = self.params["input_path"]
                seg_min = max(1, int(self.params.get("segment_minutes", 30)))
                self.progress_updated.emit(self.job_id, 20.0, f"Разбивка без перекодирования по {seg_min} мин...")
                ok, parts, info = split_video_by_duration(inp, seg_min)
                if ok and parts:
                    self.progress_updated.emit(self.job_id, 100.0, f"Готово: {len(parts)} частей по {seg_min} мин")
                    self.log_message.emit(f"[Студия] Видео разбито на {len(parts)} частей в {info}")
                    self.finished_task.emit(self.job_id, parts[0], True)
                else:
                    self._emit_done(False, info, "")

            elif self.action == "studio_effects":
                from app.tools import build_studio_effects_filters
                inp = self.params["input_path"]
                out = self.params["output_path"]
                dur = probe_video(inp).get("duration", 0.0)
                spd = float(self.params.get("speed", 1.0))
                vf_list, af_list = build_studio_effects_filters(
                    speed=spd,
                    color_preset=self.params.get("color_preset", "none"),
                    brightness=float(self.params.get("brightness", 0.0)),
                    contrast=float(self.params.get("contrast", 1.0)),
                    saturation=float(self.params.get("saturation", 1.0)),
                    sharpen=bool(self.params.get("sharpen", False)),
                    fade_in=float(self.params.get("fade_in", 0.0)),
                    fade_out=float(self.params.get("fade_out", 0.0)),
                    total_duration=dur,
                    volume_db=float(self.params.get("volume_db", 0.0)),
                    denoise=bool(self.params.get("denoise", False)),
                    voice_enhance=bool(self.params.get("voice_enhance", False)),
                    normalize_audio=bool(self.params.get("normalize_audio", False)),
                )
                cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", inp]
                if vf_list:
                    cmd.extend(["-vf", ",".join(vf_list)])
                cmd.extend(encoder_ffmpeg_args(enc, self.app_config.video_quality))
                if af_list:
                    cmd.extend(["-af", ",".join(af_list), "-c:a", "aac", "-b:a", "192k"])
                else:
                    cmd.extend(["-c:a", "copy"])
                cmd.extend(["-movflags", "+faststart", out])
                eff_dur = (dur / max(0.5, spd)) if dur > 0 else 0.0
                self.progress_updated.emit(self.job_id, 5.0, f"Применение эффектов ({enc})...")
                rc, err = self._run_ffmpeg_with_progress(cmd, eff_dur, f"Эффекты ({enc})", timeout=1800)
                if self._stop_requested:
                    if os.path.exists(out):
                        try:
                            os.remove(out)
                        except Exception:
                            pass
                    self._emit_done(False, "Отменено", "")
                    return
                self._emit_done(rc == 0 and os.path.exists(out), out if rc == 0 else err, "Эффекты применены")

            elif self.action == "remove_fillers":
                from app.ai_suite import detect_filler_and_pause_cuts
                inp = self.params["input_path"]
                out = self.params["output_path"]
                self.progress_updated.emit(self.job_id, 15.0, "Whisper: поиск слов-паразитов и пауз...")
                ok, keeps, removed, msg = detect_filler_and_pause_cuts(
                    inp,
                    model_size=self.params.get("model", self.app_config.whisper_model or "small"),
                    language=self.params.get("language", "ru"),
                )
                if not ok:
                    self._emit_done(False, msg, "")
                    return
                if not removed:
                    self._emit_done(False, msg, "")
                    return
                self.progress_updated.emit(self.job_id, 60.0, f"Вырезаем {len(removed)} пауз/паразитов...")
                from app.tools import cut_video_segments
                ok2, err2 = cut_video_segments(inp, keeps, out, encoder=enc, video_quality=self.app_config.video_quality)
                self._emit_done(ok2 and os.path.exists(out), out if ok2 else err2, f"Очищено ({len(removed)} удал.)")

            elif self.action == "voice_isolate":
                from app.ai_suite import isolate_voice_dmca_safe
                inp = self.params["input_path"]
                out = self.params["output_path"]
                self.progress_updated.emit(self.job_id, 25.0, "AI Изоляция голоса (DMCA Safe)...")
                ok, res = isolate_voice_dmca_safe(inp, out)
                self._emit_done(ok, res, "Голос изолирован")

            elif self.action == "auto_hook":
                from app.ai_suite import create_hooked_short
                inp = self.params["input_path"]
                out = self.params["output_path"]
                hook_ts = float(self.params.get("hook_ts", 15.0))
                self.progress_updated.emit(self.job_id, 20.0, "Создание AI Авто-хука (первые 3 сек)...")
                ok, res = create_hooked_short(inp, out, hook_ts, encoder=enc, video_quality=self.app_config.video_quality)
                self._emit_done(ok, res, "Ролик с Авто-хуком готов")

            elif self.action == "auto_montage":
                from app.ai_suite import build_auto_montage, find_viral_clips
                inp = self.params["input_path"]
                out = self.params["output_path"]
                clips = self.params.get("clips") or []
                if not clips:
                    self.progress_updated.emit(self.job_id, 15.0, "AI Поиск лучших моментов стрима...")
                    clips = find_viral_clips(inp, self.params.get("chat_path", ""), self.app_config, max_clips=14)
                self.progress_updated.emit(self.job_id, 55.0, f"Сборка авто-монтажа ({len(clips)} моментов)...")
                ok, res = build_auto_montage(inp, out, clips)
                self._emit_done(ok, res, f"Авто-монтаж готов ({len(clips)} сцен)")

            elif self.action == "ai_dub":
                from app.ai_suite import dub_video_ai
                inp = self.params["input_path"]
                out = self.params["output_path"]
                lang = self.params.get("target_lang", "en")
                self.progress_updated.emit(self.job_id, 20.0, f"AI Перевод и озвучка ({lang.upper()})...")
                ok, res = dub_video_ai(inp, out, target_lang=lang, cfg=self.app_config)
                self._emit_done(ok, res, f"AI Дубляж ({lang.upper()}) готов")

            elif self.action == "music_ducking":
                from app.ai_suite import mix_music_with_ducking
                inp = self.params["input_path"]
                mus = self.params["music_path"]
                out = self.params["output_path"]
                self.progress_updated.emit(self.job_id, 25.0, "Микширование музыки с авто-приглушением...")
                ok, res = mix_music_with_ducking(inp, mus, out, float(self.params.get("music_vol", 0.24)))
                self._emit_done(ok, res, "Фоновая музыка добавлена")

            elif self.action == "auto_sfx":
                from app.ai_suite import add_auto_sfx
                inp = self.params["input_path"]
                out = self.params["output_path"]
                ts_list = self.params.get("timestamps") or []
                sfx_type = self.params.get("sfx_type", "boom")
                self.progress_updated.emit(self.job_id, 30.0, f"Наложение SFX ({sfx_type}) на {len(ts_list)} пиков...")
                ok, res = add_auto_sfx(inp, out, ts_list, sfx_type=sfx_type)
                self._emit_done(ok, res, "Авто-SFX добавлены")

            elif self.action == "virtual_greenscreen":
                from app.ai_suite import create_virtual_greenscreen_short
                inp = self.params["input_path"]
                out = self.params["output_path"]
                self.progress_updated.emit(self.job_id, 20.0, "Рендер Shorts 9:16 (Virtual Green Screen)...")
                ok, res = create_virtual_greenscreen_short(
                    inp, out, self.params.get("cam_region"), self.params.get("game_region"),
                    encoder=enc, video_quality=self.app_config.video_quality,
                )
                self._emit_done(ok, res, "Virtual Green Screen Shorts готов")

            elif self.action == "smart_scene_cut":
                from app.ai_suite import detect_brb_and_loading_scenes
                from app.tools import cut_video_segments
                inp = self.params["input_path"]
                out = self.params["output_path"]
                self.progress_updated.emit(self.job_id, 20.0, "AI Поиск экранов «Скоро начнём» / BRB...")
                keeps = detect_brb_and_loading_scenes(inp)
                if not keeps:
                    self._emit_done(False, "Не найдено сцен для вырезания", "")
                    return
                self.progress_updated.emit(self.job_id, 60.0, f"Сборка активных сцен ({len(keeps)} фрагм.)...")
                ok, err = cut_video_segments(inp, keeps, out, encoder=enc, video_quality=self.app_config.video_quality)
                self._emit_done(ok and os.path.exists(out), out if ok else err, "BRB/Загрузки вырезаны")

        except Exception as exc:
            self._emit_done(False, str(exc), "")

    def _emit_done(self, ok: bool, res: str, ok_msg: str) -> None:
        if ok:
            sz = format_bytes(os.path.getsize(res)) if os.path.exists(res) else ""
            self.progress_updated.emit(self.job_id, 100.0, f"{ok_msg} ({sz})")
            self.log_message.emit(f"[Студия] {ok_msg}: {Path(res).name}")
            self.finished_task.emit(self.job_id, res, True)
        else:
            err = (res or "Неизвестная ошибка").strip().splitlines()[-1]
            self.progress_updated.emit(self.job_id, 0.0, f"Ошибка: {err[:60]}")
            self.log_message.emit(f"[Студия] Ошибка: {err}")
            self.finished_task.emit(self.job_id, "", False)
