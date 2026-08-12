"""Settings dialog: api_id / api_hash entry."""

from __future__ import annotations

from PyQt6.QtWidgets import (
    QDialog,
    QFormLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)

from ..config import Config


class SettingsDialog(QDialog):
    def __init__(self, cfg: Config, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.cfg = cfg

        self.api_id = QLineEdit()
        self.api_id.setPlaceholderText("1234567")
        if cfg.api_id is not None:
            self.api_id.setText(str(cfg.api_id))
        self.api_hash = QLineEdit()
        self.api_hash.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_hash.setPlaceholderText("your api_hash")
        if cfg.api_hash:
            self.api_hash.setText(cfg.api_hash)

        form = QFormLayout()
        form.addRow("API ID", self.api_id)
        form.addRow("API Hash", self.api_hash)

        hint = QLabel(
            'Get your credentials at <a href="https://my.telegram.org">my.telegram.org</a>. '
            "They are stored locally only."
        )
        hint.setObjectName("muted")
        hint.setOpenExternalLinks(True)

        from .theme import uppercase_button

        save = QPushButton("Save")
        save.setObjectName("accent")
        save.clicked.connect(self._save)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        for _b in (save, cancel):
            uppercase_button(_b)
        cancel.setObjectName("danger")

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(hint)
        layout.addWidget(save)
        layout.addWidget(cancel)

    def _save(self) -> None:
        raw = self.api_id.text().strip()
        if raw:
            try:
                self.cfg.api_id = int(raw)
            except ValueError:
                self.api_id.setFocus()
                return
        else:
            self.cfg.api_id = None
        self.cfg.api_hash = self.api_hash.text().strip() or None
        self.cfg.save()
        self.accept()