"""Main window and entry point."""

from __future__ import annotations

import argparse
import os
import sys
import tempfile

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QAction, QIcon
from PyQt6.QtWidgets import (
    QApplication, QLabel, QMainWindow, QMessageBox, QTabWidget,
)

from . import __version__
from .download_tab import DownloadTab
from .login import LoginDialog, forget_password, try_auto_login
from .service import ROLE_LABELS, KitsuService
from .upload_tab import UploadTab
from .widgets import settings
from .workers import TRANSFER_POOL, run_async

APP_NAME = "Kitsu Iko"
# In the packaged .exe, bundled files live in PyInstaller's unpack folder.
_BASE_DIR = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ICON_PATH = os.path.join(_BASE_DIR, "Iko_ico.ico")


class MainWindow(QMainWindow):
    def __init__(self, service: KitsuService, demo_files: list[str] | None = None):
        super().__init__()
        self.service = service
        self._demo_files = demo_files
        self.setWindowTitle(f"{APP_NAME} {__version__}" + (" (demo)" if service.is_demo else ""))
        self.resize(1400, 860)

        self.tabs = QTabWidget()
        self.upload = UploadTab(service)
        self.download = DownloadTab(service)
        self.tabs.addTab(self.upload, "↑  Upload")
        self.tabs.addTab(self.download, "↓  Download")
        self.tabs.setCurrentIndex(settings().value("ui/tab", 0, type=int))
        self.tabs.currentChanged.connect(lambda i: settings().setValue("ui/tab", i))
        self.setCentralWidget(self.tabs)

        self.user_lbl = QLabel()
        self.statusBar().addPermanentWidget(self.user_lbl)
        for tab in (self.upload, self.download):
            tab.status_message.connect(self._status)

        m = self.menuBar().addMenu("&Account")
        self.switch_act = QAction("Sign out / switch server...", self)
        self.switch_act.triggered.connect(self._switch_account)
        m.addAction(self.switch_act)
        m.addSeparator()
        quit_act = QAction("Quit", self)
        quit_act.triggered.connect(self.close)
        m.addAction(quit_act)

        geo = settings().value("ui/geometry")
        if geo is not None:
            self.restoreGeometry(geo)

    def _status(self, msg: str) -> None:
        self.statusBar().showMessage(msg, 0 if msg.startswith("Error") else 15000)

    # -- session ----------------------------------------------------------

    def begin(self) -> None:
        """Show the window immediately, then sign in in the background."""
        if self.service.is_demo:
            self.service.login("", "demo@example.com", "")
            self._signed_in()
            return
        self._status("Signing in...")
        run_async(lambda: try_auto_login(self.service), self._auto_login_result)

    def _auto_login_result(self, err: str | None) -> None:
        if err is None:
            self._signed_in()
        else:
            self._ask_login("" if err == "manual" else f"Automatic sign-in failed: {err}")

    def _ask_login(self, message: str = "") -> None:
        dlg = LoginDialog(self.service, self, message)
        if dlg.exec():
            self._signed_in()
        else:
            self.close()

    def _signed_in(self) -> None:
        u = self.service.user
        host = self.service.web_url()
        role = ROLE_LABELS.get(self.service.role, self.service.role)
        self.user_lbl.setText(f"{u.get('full_name') or u.get('email', '')}  ·  {role}  ·  {host}"
                              + (f"  ·  Zou {self.service.server_version}" if self.service.server_version else ""))
        self._status("Signed in.")
        self.upload.start()
        self.download.start()
        if self._demo_files:
            self.upload.add_paths(self._demo_files)
            self._demo_files = None

    def _switch_account(self) -> None:
        if self._busy():
            QMessageBox.information(self, APP_NAME, "Wait for the current transfer to finish first.")
            return
        s = settings()
        forget_password(s.value("login/host", ""), s.value("login/email", ""))
        self.service.logout()
        self._ask_login()

    def _busy(self) -> bool:
        return self.upload.is_busy() or self.download.is_busy()

    def closeEvent(self, e) -> None:
        if self._busy():
            r = QMessageBox.question(
                self, APP_NAME,
                "Transfers are still running. Quit anyway? Files that haven't finished won't be "
                "uploaded/downloaded.",
            )
            if r != QMessageBox.StandardButton.Yes:
                e.ignore()
                return
            TRANSFER_POOL.clear()
        settings().setValue("ui/geometry", self.saveGeometry())
        super().closeEvent(e)


def log_path() -> str:
    base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    os.makedirs(os.path.join(base, "KitsuIko"), exist_ok=True)
    return os.path.join(base, "KitsuIko", "error.log")


def install_excepthook() -> None:
    """PyQt6 aborts the whole app on an exception escaping a slot. Log it
    and show it instead, so a bug costs one action, not the session."""
    import datetime
    import traceback

    def hook(exc_type, exc, tb):
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        try:
            with open(log_path(), "a", encoding="utf-8") as fh:
                fh.write(f"\n--- {datetime.datetime.now():%Y-%m-%d %H:%M:%S}  (v{__version__})\n{text}")
        except OSError:
            pass
        sys.__stderr__ and sys.__stderr__.write(text)
        if QApplication.instance() is not None:
            box = QMessageBox(QMessageBox.Icon.Warning, APP_NAME,
                              f"Something went wrong: {exc}\n\nThe app is still running. "
                              f"Details were saved to:\n{log_path()}")
            box.setDetailedText(text)
            box.exec()

    sys.excepthook = hook


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kitsu_iko", description="Bulk upload/download for Kitsu")
    parser.add_argument("--demo", action="store_true", help="run against an in-memory fake Kitsu")
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    # Checks a packaged build end to end (no console in the .exe, so results
    # go to a file). Used by build.py; also handy when a build misbehaves.
    parser.add_argument("--selftest", metavar="REPORT.json", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.selftest:
        return selftest(args.selftest)

    if sys.platform == "win32":
        # Give the app its own taskbar identity; otherwise Windows groups it
        # under pythonw.exe and shows Python's icon.
        try:
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("KitsuIko.KitsuIko")
        except Exception:
            pass
    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv[:1])
    if os.path.exists(ICON_PATH):
        app.setWindowIcon(QIcon(ICON_PATH))  # all windows and dialogs inherit it
    app.setApplicationName(APP_NAME)
    app.setOrganizationName("KitsuIko")
    app.setStyle("Fusion")
    install_excepthook()

    demo_files = None
    if args.demo:
        from .demo_service import DemoService, demo_files as make_demo_files

        service: KitsuService = DemoService()
        demo_files = make_demo_files(os.path.join(tempfile.gettempdir(), "kitsu_iko_demo_files"))
    else:
        service = KitsuService()

    win = MainWindow(service, demo_files)
    win.show()
    QTimer.singleShot(0, win.begin)
    return app.exec()


def selftest(report_path: str) -> int:
    """Exercise what a frozen build can lose: Qt plugins, the icon, the
    keychain backend, the HTTPS certificate bundle, and the demo UI."""
    import json
    import time

    import certifi
    import gazu
    import keyring

    report: dict = {"version": __version__, "frozen": bool(getattr(sys, "frozen", False))}
    try:
        report["icon"] = os.path.exists(ICON_PATH)
        report["certifi_bundle"] = os.path.exists(certifi.where())
        report["gazu"] = gazu.__version__
        report["keyring_backend"] = type(keyring.get_keyring()).__name__
        try:
            keyring.set_password("kitsu-iko-selftest", "probe", "ok")
            report["keyring_roundtrip"] = keyring.get_password("kitsu-iko-selftest", "probe") == "ok"
            keyring.delete_password("kitsu-iko-selftest", "probe")
        except Exception as exc:
            report["keyring_roundtrip"] = f"error: {exc}"
        app = QApplication(sys.argv[:1])
        report["qt_platform"] = app.platformName()
        app.setWindowIcon(QIcon(ICON_PATH))
        report["icon_loaded"] = not app.windowIcon().isNull()
        from .demo_service import DemoService

        win = MainWindow(DemoService())
        win.show()
        win.begin()
        end = time.time() + 5
        while time.time() < end and win.upload.context_bar.project_cb.count() < 2:
            app.processEvents()
            time.sleep(0.02)
        report["demo_ui_projects"] = win.upload.context_bar.project_cb.count() - 1
        report["title"] = win.windowTitle()
        win.close()
        report["ok"] = (report["icon"] and report["certifi_bundle"] and report["keyring_roundtrip"] is True
                        and report["icon_loaded"] and report["demo_ui_projects"] > 0)
    except Exception as exc:
        import traceback

        report["ok"] = False
        report["error"] = "".join(traceback.format_exception(exc))
    with open(report_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=1)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
