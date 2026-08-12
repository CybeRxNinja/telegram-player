"""Application entry point.

`main()` stays window-import lazy so `--version` works even before PyQt6 or
the UI modules are importable (useful for AppImage smoke tests).
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import sys
from pathlib import Path

__all__ = ["main"]


def _resolve_mpv() -> str | None:
    if getattr(sys, "frozen", False):
        # PyInstaller onedir: sys._MEIPASS is the bundle dir that contains the
        # executable and _internal; mpv is bundled alongside (same dir).
        for base in (Path(sys._MEIPASS), Path(sys.executable).parent):
            cand = base / "mpv"
            if cand.is_file():
                return str(cand)
        return None
    env = os.environ.get("TELEGRAM_PLAYER_MPV")
    if env:
        return env
    return shutil.which("mpv")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="telegram-player", description="Minimal Telegram video player")
    parser.add_argument("--version", action="store_true", help="print version and exit")
    parser.add_argument("--debug", action="store_true", help="enable debug logging (incl. range requests)")
    args = parser.parse_args(argv)

    if args.version:
        from telegram_player import __version__

        print(f"telegram-player {__version__}")
        print(f"mpv: {_resolve_mpv() or 'NOT FOUND'}")
        return 0

    if args.debug:
        logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(name)s: %(message)s")
    else:
        logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    # mpv embeds into the player container via --wid, which is an X11 window id.
    # The wayland platform can't host foreign surfaces, so run on xcb (XWayland).
    # Must be set before the QApplication is constructed.
    os.environ.setdefault("QT_QPA_PLATFORM", "xcb")

    # Qt imports are heavy; keep them out of the --version fast path.
    from PyQt6.QtWidgets import QApplication

    from telegram_player.config import Config

    cfg = Config.load()

    app = QApplication(sys.argv[:1])
    app.setApplicationName("Telegram Player")
    app.setOrganizationName("telegram-player")

    from telegram_player.ui.theme import apply_theme

    apply_theme(app)

    from telegram_player.app_ui import build_main_window

    window = build_main_window(cfg, _resolve_mpv())
    window.show()
    return app.exec()