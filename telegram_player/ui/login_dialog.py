"""Login dialog: QR (scan) and phone/code/2FA tabs. No icon assets."""

from __future__ import annotations

import base64

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QPainter
from PyQt6.QtWidgets import (
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
)

from ..config import Config
from ..tg_client import PasswordNeeded


def render_qr_pixmap(url: str, size: int = 240) -> "QPixmap":  # noqa: F821
    """Paint a QR code for `url` using segno + QPainter (no QtSvg).

    segno's `matrix` is a tuple of bytearray rows where 0 = dark, 1 = light.
    """
    import segno

    matrix = segno.make(url, error="m").matrix
    n = len(matrix)
    cell = size // n
    from PyQt6.QtGui import QPixmap

    pm = QPixmap(size, size)
    pm.fill(QColor("white"))
    painter = QPainter(pm)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("black"))
    for y, row in enumerate(matrix):
        for x in range(n):
            if row[x] == 0:  # dark module
                painter.fillRect(x * cell, y * cell, cell, cell, QColor("black"))
    painter.end()
    return pm


class LoginDialog(QDialog):
    def __init__(self, tg, cfg: Config, parent=None):
        super().__init__(parent)
        self.tg = tg
        self.cfg = cfg
        self.setWindowTitle("Sign in to Telegram")
        self.setMinimumWidth(360)

        self.tabs = QTabWidget()
        self._build_qr_tab()
        self._build_phone_tab()

        # Connect service signals.
        tg.connected.connect(self._on_connected)
        tg.login_failed.connect(self._on_error)
        tg.qr_refreshed.connect(self._set_qr)

        layout = QVBoxLayout(self)
        layout.addWidget(self.tabs)

        # If credentials are present, kick off the QR login immediately.
        self._start_qr()

    # ---------------- QR tab ----------------
    def _build_qr_tab(self) -> None:
        self.qr_label = QLabel("Starting…")
        self.qr_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint = QLabel("Open Telegram → Settings → Devices → Scan QR Code")
        hint.setObjectName("muted")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)

        box = QVBoxLayout()
        box.addStretch(1)
        box.addWidget(self.qr_label)
        box.addWidget(hint)
        box.addStretch(1)
        widget = QLabel()
        widget.setLayout(box)
        self.tabs.addTab(widget, "QR code")

    def _start_qr(self) -> None:
        if not (self.cfg.api_id and self.cfg.api_hash):
            self.qr_label.setText("Open Settings to add your api_id/api_hash first")
            return
        url = self.tg.request_login_qr()
        if url:
            self._set_qr(url)
        else:
            self.qr_label.setText("QR login failed — check the error on the Phone tab")

    def _set_qr(self, url: str) -> None:
        self.qr_label.setPixmap(render_qr_pixmap(url, 240))

    # ---------------- Phone tab ----------------
    def _build_phone_tab(self) -> None:
        self.phone = QLineEdit()
        self.phone.setPlaceholderText("+1234567890")
        self.code = QLineEdit()
        self.code.setPlaceholderText("12345")
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText("2FA password")
        self.password.setVisible(False)
        from .theme import uppercase_button

        self.err = QLabel()
        self.err.setObjectName("error")
        self.err.setWordWrap(True)

        send_code = QPushButton("Send code")
        send_code.setObjectName("accent")
        send_code.clicked.connect(self._send_code)
        verify = QPushButton("Verify")
        verify.clicked.connect(self._verify)
        self.sign_in = QPushButton("Sign in")
        self.sign_in.setObjectName("accent")
        self.sign_in.clicked.connect(self._sign_in)
        self.sign_in.setVisible(False)
        for _b in (send_code, verify, self.sign_in):
            uppercase_button(_b)

        form = QFormLayout()
        form.addRow("Phone", self.phone)
        form.addRow("Code", self.code)
        form.addRow("Password", self.password)
        form.addRow("", self.err)
        row = QHBoxLayout()
        row.addWidget(send_code)
        row.addWidget(verify)
        row.addWidget(self.sign_in)
        form.addRow("", row)

        widget = QLabel()
        widget.setLayout(form)
        self.tabs.addTab(widget, "Phone")

    def _send_code(self) -> None:
        self.err.setText("")
        err = self.tg.submit_phone(self.phone.text())
        if err:
            self.err.setText(err)
        else:
            self.code.setFocus()

    def _verify(self) -> None:
        self.err.setText("")
        try:
            err = self.tg.submit_code(self.code.text())
        except PasswordNeeded:
            self.password.setVisible(True)
            self.sign_in.setVisible(True)
            self.err.setText("2FA required — enter your password")
            self.password.setFocus()
            return
        if err:
            self.err.setText(err)
        else:
            # Signal will close the dialog on success.
            pass

    def _sign_in(self) -> None:
        self.err.setText("")
        err = self.tg.submit_password(self.password.text())
        if err:
            self.err.setText(err)

    # ---------------- results ----------------
    def _on_connected(self, name: str) -> None:
        self.accept()

    def _on_error(self, msg: str) -> None:
        self.err.setText(msg)
