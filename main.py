from __future__ import annotations

import multiprocessing
import os
import sys


def _ensure_ssl_cert_env() -> None:
    """
    Ensure SSL_CERT_FILE is set on macOS so static OpenSSL builds (e.g. bundled static ffmpeg)
    and child subprocesses can verify HTTPS TLS certificates (Twitch HLS, VODs, APIs).
    """
    if sys.platform == "darwin" and not os.environ.get("SSL_CERT_FILE"):
        for candidate in ("/etc/ssl/cert.pem", "/private/etc/ssl/cert.pem"):
            if os.path.isfile(candidate):
                os.environ["SSL_CERT_FILE"] = candidate
                break
        if not os.environ.get("SSL_CERT_FILE"):
            try:
                import certifi
                ca = certifi.where()
                if ca and os.path.isfile(ca):
                    os.environ["SSL_CERT_FILE"] = ca
            except Exception:
                pass


_ensure_ssl_cert_env()


def _maybe_run_internal_cli() -> bool:
    """
    When bundled as a standalone .exe (Windows) or .app (macOS), allow subprocesses
    to invoke bundled streamlink and yt-dlp directly with zero external Python needed.
    """
    if len(sys.argv) >= 2 and sys.argv[1] == "--internal-streamlink":
        sys.argv = ["streamlink"] + sys.argv[2:]
        from streamlink_cli.main import main as sl_main
        sl_main()
        return True

    if len(sys.argv) >= 2 and sys.argv[1] == "--internal-ytdlp":
        import yt_dlp
        yt_dlp.main(sys.argv[2:])
        return True

    return False


def _install_crash_logger() -> None:
    import threading
    import traceback

    def _log_exc(prefix: str, exc_type, exc_value, exc_tb) -> None:
        try:
            from app.config import get_file_logger
            tb_text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
            get_file_logger().error("%s:\n%s", prefix, tb_text)
        except Exception:
            pass

    def _sys_hook(exc_type, exc_value, exc_tb) -> None:
        _log_exc("UNHANDLED_EXCEPTION", exc_type, exc_value, exc_tb)
        sys.__excepthook__(exc_type, exc_value, exc_tb)

    def _thread_hook(args) -> None:
        _log_exc(f"THREAD_EXCEPTION ({getattr(args.thread, 'name', 'worker')})", args.exc_type, args.exc_value, args.exc_traceback)

    sys.excepthook = _sys_hook
    if hasattr(threading, "excepthook"):
        threading.excepthook = _thread_hook


def main() -> int:
    multiprocessing.freeze_support()
    if _maybe_run_internal_cli():
        return 0
    _install_crash_logger()

    from PySide6.QtWidgets import QApplication
    from app.web_window import StudioDesktopWindow

    app = QApplication(sys.argv)
    app.setApplicationName("Twitch Recorder Pro")
    app.setQuitOnLastWindowClosed(False)

    window = StudioDesktopWindow()
    if "--minimized" not in sys.argv:
        window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
