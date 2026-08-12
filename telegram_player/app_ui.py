"""Builds and wires the top-level application objects."""

from __future__ import annotations

from telegram_player.config import Config
from telegram_player.mpv_ctl import MpvCtl
from telegram_player.tg_client import TelegramService
from telegram_player.ui.main_window import MainWindow


def build_main_window(cfg: Config, mpv_binary: str | None) -> MainWindow:
    tg = TelegramService(cfg)
    mpv = MpvCtl(mpv_binary, cfg)
    return MainWindow(cfg, tg, mpv)
