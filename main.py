from __future__ import annotations

import multiprocessing
import sys


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


def main() -> int:
    multiprocessing.freeze_support()
    if _maybe_run_internal_cli():
        return 0

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
