"""Regression tests for the player fullscreen toggle.

Runs headless (offscreen platform): verifies the transport button wiring, the
panel hide/restore behaviour, Esc handling and the "video view must be active"
guard. Widget painting is irrelevant here; only visibility + window state.
"""

from __future__ import annotations

import os
import sys
import tempfile

# Isolate user config/data before anything touches the real paths, and run
# without a display (widget logic only, no painting).
os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="tp-fs-cfg-")
os.environ["XDG_DATA_HOME"] = tempfile.mkdtemp(prefix="tp-fs-data-")

import pytest
from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtGui import QKeyEvent
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication

from telegram_player.config import Config
from telegram_player.ui.main_window import MainWindow


class _FakeTg(QObject):
    connected = pyqtSignal(str)
    status = pyqtSignal(str)
    login_failed = pyqtSignal(str)
    chats_loaded = pyqtSignal(list)
    qr_refreshed = pyqtSignal(str)
    playback_ready = pyqtSignal(list)
    playback_failed = pyqtSignal(str)

    def start(self):
        pass

    def stop(self):
        pass

    def refresh_chats(self):
        pass

    def apply_credentials(self):
        pass

    def logout(self):
        pass

    def load_topics(self, chat_id):
        return []

    def load_media(self, chat_id, offset=0, topic_id=None):
        return []

    def _call(self, fn, on_success=None, on_error=None):
        pass

    def play_playlist(self, items):
        pass

    def download_item(self, *args):
        pass

    def download_all(self, items):
        pass


class _FakeMpv(QObject):
    state_changed = pyqtSignal(dict)
    mpv_exited = pyqtSignal()
    file_loaded = pyqtSignal()

    def __init__(self):
        super().__init__()
        self._ipc = None
        self.embed_wid = 0

    def poll_state(self):
        return {"position": None, "duration": None, "pause": False,
                "title": None, "playlist_position": -1, "playlist_count": 0}

    def stop(self):
        pass

    def prev(self):
        pass

    def next(self):
        pass

    def set_volume(self, v):
        pass

    def ensure_started(self):
        return True

    def load_playlist(self, entries):
        pass

    def play(self):
        pass

    def shutdown(self):
        pass

    def wait_stopped(self, timeout=5.0):
        return True

    def _verify_embed(self, unavailable_is_ok=True):
        return True


@pytest.fixture(scope="module")
def app():
    a = QApplication.instance() or QApplication(sys.argv)
    yield a


@pytest.fixture()
def window(app):
    w = MainWindow(Config(), _FakeTg(), _FakeMpv())
    w.show()
    app.processEvents()
    yield w
    w.close()
    app.processEvents()


def test_fullscreen_button_starts_disabled(window):
    assert window.btn_fullscreen.isEnabled() is False


def test_player_panel_enables_button_and_fullscreen_hides_chrome(window, app):
    window._show_player_panel("Loading...")
    app.processEvents()
    assert window.btn_fullscreen.isEnabled() is True

    window._toggle_fullscreen()
    app.processEvents()
    assert window.isFullScreen() is True
    assert window.left_panel.isVisible() is False
    assert window.playlist_panel.isVisible() is False
    assert window.media_panel.isVisible() is False
    assert window.player_container.isVisible() is True
    assert window.transport.isVisible() is True


def test_toggle_restores_layout(window, app):
    window._show_player_panel("Loading...")
    app.processEvents()
    sizes = (window.root_splitter.sizes(), window.right_splitter.sizes())
    window._toggle_fullscreen()
    app.processEvents()
    window._toggle_fullscreen()
    app.processEvents()
    assert window.isFullScreen() is False
    assert window.left_panel.isVisible() is True
    assert window.playlist_panel.isVisible() is True
    assert window.player_container.isVisible() is True
    assert window.root_splitter.sizes() == sizes[0]
    assert window.right_splitter.sizes() == sizes[1]


def test_esc_exits_fullscreen(window, app):
    window._show_player_panel("Loading...")
    app.processEvents()
    window._toggle_fullscreen()
    app.processEvents()
    assert window.isFullScreen() is True
    ev = QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_Escape,
                   Qt.KeyboardModifier.NoModifier)
    window.keyPressEvent(ev)
    app.processEvents()
    assert window.isFullScreen() is False


def test_fullscreen_requires_active_player_view(window, app):
    # Media panel visible (nothing playing): toggle must be a no-op.
    window._toggle_fullscreen()
    app.processEvents()
    assert window.isFullScreen() is False


def test_leaving_player_view_drops_fullscreen_and_disables_button(window, app):
    window._show_player_panel("Loading...")
    app.processEvents()
    window._toggle_fullscreen()
    app.processEvents()
    assert window.isFullScreen() is True
    window._show_media_panel()
    app.processEvents()
    assert window.isFullScreen() is False
    assert window.media_panel.isVisible() is True
    assert window.btn_fullscreen.isEnabled() is False
