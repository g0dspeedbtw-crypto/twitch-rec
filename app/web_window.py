from __future__ import annotations

from PySide6.QtCore import QUrl, Signal
from PySide6.QtGui import QAction, QColor
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineCore import QWebEngineSettings
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QApplication, QMainWindow, QMenu, QMessageBox, QSystemTrayIcon

from app.local_media_server import LocalMediaServer
from app.ui.icons import apply_windows_dark_titlebar, make_app_icon
from app.web_bridge import AppBridge


class DropWebEngineView(QWebEngineView):
    """WebEngineView subclass that intercepts native Finder/Explorer file drops (#49)."""

    filesDropped = Signal(list)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            return
        super().dragMoveEvent(event)

    def dropEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            paths = [u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()]
            if paths:
                self.filesDropped.emit(paths)
                event.acceptProposedAction()
                return
        super().dropEvent(event)


class StudioDesktopWindow(QMainWindow):
    """
    Native Windows 11 desktop container hosting the hardware-accelerated Chromium UI
    connected to our Python FFmpeg/Streamlink/AI backend via QWebChannel + LocalMediaServer.
    """

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Twitch Recorder Pro")
        self.setWindowIcon(make_app_icon(64))
        self.resize(1420, 880)
        self.setMinimumSize(1100, 700)
        apply_windows_dark_titlebar(self)
        self._force_quit = False

        # Start local HTTP media server (supports Range streaming for <video> + thumbnails)
        self.media_server = LocalMediaServer(
            allowed_roots_fn=lambda: self.bridge.get_allowed_media_roots() if hasattr(self, "bridge") else [],
            allowed_files_fn=lambda: self.bridge.get_allowed_media_files() if hasattr(self, "bridge") else set(),
        )
        port = self.media_server.start()

        # Backend RPC Bridge
        self.bridge = AppBridge(self, self.media_server)
        self.bridge.recordingCountChanged.connect(self._on_recording_count_changed)

        # Chromium WebEngine View with Native OS Drag & Drop (#49)
        self.web = DropWebEngineView(self)
        self.web.filesDropped.connect(self.bridge.handleNativeFileDrop)
        self.web.page().setBackgroundColor(QColor("#090A0F"))
        settings = self.web.settings()
        settings.setAttribute(QWebEngineSettings.LocalContentCanAccessRemoteUrls, True)
        settings.setAttribute(QWebEngineSettings.PlaybackRequiresUserGesture, False)
        settings.setAttribute(QWebEngineSettings.ScrollAnimatorEnabled, True)

        self.channel = QWebChannel(self.web.page())
        self.channel.registerObject("backend", self.bridge)
        self.web.page().setWebChannel(self.channel)
        self.bridge.bind_web_page(self.web.page())
        self.web.setUrl(QUrl(f"http://127.0.0.1:{port}/"))
        self.setCentralWidget(self.web)

        self._setup_tray()

    def _setup_tray(self) -> None:
        self.tray = QSystemTrayIcon(make_app_icon(64, recording_dot=False), self)
        self.tray.setToolTip("Twitch Recorder Pro")
        self._tray_menu = QMenu()
        self.tray.setContextMenu(self._tray_menu)
        self._rebuild_tray_menu()
        self.tray.activated.connect(
            lambda reason: self._restore_from_tray() if reason == QSystemTrayIcon.DoubleClick else None
        )
        self.tray.show()

    def _rebuild_tray_menu(self) -> None:
        menu = self._tray_menu
        menu.clear()
        act_open = QAction("Открыть окно", self)
        act_open.triggered.connect(self._restore_from_tray)
        menu.addAction(act_open)

        active_recs = list(self.bridge.live_recorders.items())
        if active_recs:
            menu.addSeparator()
            for key, w in active_recs:
                uname = w.channel.username
                act_mk = QAction(f"Маркер: {uname}", self)
                act_mk.triggered.connect(lambda _=False, k=key: self.bridge.dropMarker(k))
                menu.addAction(act_mk)
                act_st = QAction(f"Стоп: {uname}", self)
                act_st.triggered.connect(lambda _=False, k=key: self.bridge.stopRecording(k))
                menu.addAction(act_st)
            menu.addSeparator()
            act_stop_all = QAction("Остановить все записи", self)
            act_stop_all.triggered.connect(self.bridge.stopAllRecordings)
            menu.addAction(act_stop_all)
        else:
            act_rec_all = QAction("Записать всех онлайн", self)
            act_rec_all.triggered.connect(self.bridge.recordAllOnline)
            menu.addAction(act_rec_all)

        menu.addSeparator()
        act_quit = QAction("Выход", self)
        act_quit.triggered.connect(self._quit_completely)
        menu.addAction(act_quit)

    def _on_recording_count_changed(self, count: int) -> None:
        self.tray.setIcon(make_app_icon(64, recording_dot=(count > 0)))
        self.tray.setToolTip(f"Twitch Recorder Pro — активных записей: {count}")
        self._rebuild_tray_menu()

    def _restore_from_tray(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _quit_completely(self) -> None:
        self._force_quit = True
        self.close()

    def quit_for_update(self) -> None:
        """Force clean shutdown & exit so the detached updater script can swap binaries and relaunch."""
        self._force_quit = True
        self.bridge.shutdown()
        self.media_server.stop()
        self.tray.hide()
        app = QApplication.instance()
        if app is not None:
            app.quit()

    def closeEvent(self, event) -> None:
        if self.bridge.config.minimize_to_tray and not self._force_quit:
            self.hide()
            self.tray.showMessage(
                "Twitch Recorder Pro",
                "Свернуто в системный трей — записи продолжают работу в фоне.",
                QSystemTrayIcon.Information,
                2500,
            )
            event.ignore()
            return

        active = self.bridge.active_count()
        if active > 0:
            reply = QMessageBox.question(
                self,
                "Выход из программы",
                f"Сейчас выполняются активные задачи ({active}).\nКорректно завершить их и выйти?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                self._force_quit = False
                event.ignore()
                return

        self.bridge.shutdown()
        self.media_server.stop()
        self.tray.hide()
        event.accept()
        app = QApplication.instance()
        if app is not None:
            app.quit()
