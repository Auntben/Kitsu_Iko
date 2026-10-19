"""Login dialog. Host/email are kept in QSettings; the password (only if
"Remember me" is ticked) goes to the OS keychain via ``keyring``."""

from __future__ import annotations

from PyQt6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QFormLayout, QLabel, QLineEdit,
    QVBoxLayout,
)

from .service import KitsuService, describe_error, normalize_host
from .widgets import settings
from .workers import run_async

try:
    import keyring
except Exception:  # pragma: no cover - keyring is optional
    keyring = None

KEYRING_SERVICE = "kitsu-iko"


def _key(host: str, email: str) -> str:
    return f"{normalize_host(host)}|{email.strip().lower()}"


def stored_password(host: str, email: str) -> str | None:
    if keyring is None or not host or not email:
        return None
    try:
        return keyring.get_password(KEYRING_SERVICE, _key(host, email))
    except Exception:
        return None


def forget_password(host: str, email: str) -> None:
    if keyring is None:
        return
    try:
        keyring.delete_password(KEYRING_SERVICE, _key(host, email))
    except Exception:
        pass


class LoginDialog(QDialog):
    def __init__(self, service: KitsuService, parent=None, message: str = ""):
        super().__init__(parent)
        self.service = service
        self.setWindowTitle("Kitsu Iko - Sign in")
        self.setMinimumWidth(420)
        s = settings()

        self.host = QLineEdit(s.value("login/host", ""))
        self.host.setPlaceholderText("kitsu.mystudio.local")
        self.email = QLineEdit(s.value("login/email", ""))
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.totp = QLineEdit()
        self.totp.setPlaceholderText("Only if two-factor auth is enabled")
        self.ssl = QCheckBox("Verify SSL certificate")
        self.ssl.setChecked(s.value("login/ssl_verify", True, type=bool))
        self.ssl.setToolTip("Untick for self-hosted servers using a self-signed certificate.")
        self.remember = QCheckBox("Remember me (password stored in the system keychain)")
        self.remember.setChecked(s.value("login/remember", True, type=bool))
        self.remember.setEnabled(keyring is not None)
        self.error = QLabel(message)
        self.error.setStyleSheet("color: #e5534b;")
        self.error.setWordWrap(True)

        form = QFormLayout()
        form.addRow("Kitsu URL", self.host)
        form.addRow("Email", self.email)
        form.addRow("Password", self.password)
        form.addRow("2FA code", self.totp)
        form.addRow("", self.ssl)
        form.addRow("", self.remember)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Sign in")
        self.buttons.accepted.connect(self._submit)
        self.buttons.rejected.connect(self.reject)

        lay = QVBoxLayout(self)
        hint = QLabel("Enter the address you use for Kitsu in the browser; <i>/api</i> is added automatically.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: gray;")
        lay.addLayout(form)
        lay.addWidget(hint)
        lay.addWidget(self.error)
        lay.addWidget(self.buttons)

        pw = stored_password(self.host.text(), self.email.text())
        if pw:
            self.password.setText(pw)
        (self.password if self.host.text() and self.email.text() else self.host).setFocus()

    def _submit(self) -> None:
        host, email, pw = self.host.text().strip(), self.email.text().strip(), self.password.text()
        if not host or not email or not pw:
            self.error.setText("URL, email and password are required.")
            return
        self.error.setText("Signing in...")
        self.buttons.setEnabled(False)
        ssl_verify = self.ssl.isChecked()
        totp = self.totp.text().strip()
        run_async(
            lambda: self.service.login(host, email, pw, ssl_verify, totp),
            lambda user: self._ok(host, email, pw),
            self._fail,
        )

    def _ok(self, host: str, email: str, pw: str) -> None:
        s = settings()
        s.setValue("login/host", host)
        s.setValue("login/email", email)
        s.setValue("login/ssl_verify", self.ssl.isChecked())
        s.setValue("login/remember", self.remember.isChecked())
        if keyring is not None:
            try:
                if self.remember.isChecked():
                    keyring.set_password(KEYRING_SERVICE, _key(host, email), pw)
                else:
                    forget_password(host, email)
            except Exception:
                pass
        self.accept()

    def _fail(self, msg: str) -> None:
        self.buttons.setEnabled(True)
        if "auth" in msg.lower() or msg == "AuthFailedException":
            msg = "Wrong email, password or 2FA code."
        self.error.setText(msg)


def try_auto_login(service: KitsuService) -> str | None:
    """Sign in silently with remembered credentials. Returns an error message,
    or None on success. Runs synchronously (called before the window shows)."""
    s = settings()
    host, email = s.value("login/host", ""), s.value("login/email", "")
    if not s.value("login/remember", True, type=bool):
        return "manual"
    pw = stored_password(host, email)
    if not pw:
        return "manual"
    try:
        service.login(host, email, pw, s.value("login/ssl_verify", True, type=bool))
        return None
    except Exception as exc:
        return describe_error(exc)
