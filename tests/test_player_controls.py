"""Tests for the beautified player: keyboard controls, speed control with
playlist/session persistence, and the auto-hiding transport bar.

Runs headless (offscreen); widget painting is irrelevant. The app-level key
filter and fade machinery are exercised directly with synthesized events.
"""

from __future__ import annotations

import os
import sys
import tempfile

os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="tp-ctl-cfg-")
os.environ["XDG_DATA_HOME"] = tempfile.mkdtemp(prefix="tp-ctl-data-")

import pytest
from PyQt6.QtCore import QEvent, QObject, Qt, pyqtSignal, QPointF
from PyQt6.QtGui import QMouseEvent
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

from telegram_player.config import Config
from telegram_player.playlist import PlaylistItem, PlaylistCollection
from telegram_player.media import MediaItem
from telegram_player.ui.main_window import MainWindow, _JumpSlider


class _FakeTg(QObject):
    connected = pyqtSignal(str)
    status = pyqtSignal(str)
    login_failed = pyqtSignal(str)
    chats_loaded = pyqtSignal(list)
    qr_refreshed = pyqtSignal(str)
    playback_ready = pyqtSignal(list)
    playback_failed = pyqtSignal(str)

    def start(self): pass
    def stop(self): pass
    def refresh_chats(self): pass
    def apply_credentials(self): pass
    def logout(self): pass
    def load_topics(self, chat_id): return []
    def load_media(self, chat_id, offset=0, topic_id=None): return []
    def _call(self, fn, on_success=None, on_error=None): pass
    def play_playlist(self, items): pass
    def download_item(self, *args): pass
    def download_all(self, items): pass


class _FakeMpv(QObject):
    state_changed = pyqtSignal(dict)
    mpv_exited = pyqtSignal()
    file_loaded = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.pause = False
        self.speeds: list[float] = []
        self.seeks: list[float] = []
        self.seeks_abs: list[float] = []
        self.toggle_count = 0
        self.stop_count = 0
        self.volume = 80

    def poll_state(self):
        return {"position": 10.0, "duration": 100.0, "pause": self.pause,
                "title": "T", "playlist_position": -1, "playlist_count": 0}

    _ipc = None

    def set_speed(self, f): self.speeds.append(f)
    def seek(self, sec): self.seeks_abs.append(sec)
    def seek_relative(self, d): self.seeks.append(d)
    def prev(self): pass
    def next(self): pass
    def toggle(self): self.toggle_count += 1
    def stop(self): self.stop_count += 1
    def set_volume(self, v): self.volume = v
    def ensure_started(self): return True
    def load_playlist(self, entries): pass
    def play(self): pass
    def shutdown(self): pass
    def wait_stopped(self, timeout=5.0): return True
    def _verify_embed(self, unavailable_is_ok=True): return True


@pytest.fixture(scope="module")
def app():
    a = QApplication.instance() or QApplication(sys.argv)
    yield a


@pytest.fixture()
def win(app):
    w = MainWindow(Config(), _FakeTg(), _FakeMpv())
    w.show()
    app.processEvents()
    yield w
    # Defer destruction to the event loop: a synchronous close() can abort
    # during QWidget teardown under headless Qt. deleteLater runs it safely.
    w.deleteLater()
    app.processEvents()


def _show_player(win, app):
    win._show_player_panel("Loading...")
    app.processEvents()
    # Move focus off the chat filter so the player hotkeys become active
    # (in the real app the user has just clicked a play button).
    win.setFocus()
    app.processEvents()


# ---------------- speed control ----------------
def test_speed_button_shows_current_config_speed(win):
    assert win.btn_speed.text() == "1×"


def test_speed_selection_persists_to_config_and_mpv(win, app):
    action = next(a for a in win._speed_menu.actions() if a.data() == 1.5)
    win._on_speed_selected(action)
    assert win.mpv.speeds == [1.5]
    assert win.cfg.speed == 1.5
    assert win.btn_speed.text() == "1.5×"
    # Survives a restart: reload from disk.
    reloaded = Config.load()
    assert reloaded.speed == 1.5


def test_speed_clamped_and_button_label_updates(win):
    win._set_speed(99.0)
    assert win.cfg.speed == 10.0
    assert win.btn_speed.text() == "10×"
    win._set_speed(0.001)
    assert win.cfg.speed == 0.1


# ---------------- keyboard controls ----------------
def test_space_toggles_play_only_in_player_view(win, app):
    QTest.keyClick(win, Qt.Key.Key_Space)  # media panel visible -> ignored
    assert win.mpv.toggle_count == 0
    _show_player(win, app)
    # Simulate an mpv instance that is already up (socket live).
    win.mpv._ipc = object()
    QTest.keyClick(win, Qt.Key.Key_Space)
    assert win.mpv.toggle_count == 1
    QTest.keyClick(win, Qt.Key.Key_K)
    QTest.keyClick(win, Qt.Key.Key_P)
    assert win.mpv.toggle_count == 3


def test_arrow_keys_seek(win, app):
    _show_player(win, app)
    QTest.keyClick(win, Qt.Key.Key_Right)
    QTest.keyClick(win, Qt.Key.Key_Left)
    QTest.keyClick(win, Qt.Key.Key_Right, Qt.KeyboardModifier.ShiftModifier)
    QTest.keyClick(win, Qt.Key.Key_Up)
    QTest.keyClick(win, Qt.Key.Key_Down)
    assert win.mpv.seeks == [5.0, -5.0, 1.0, 60.0, -60.0]


def test_volume_and_mute_keys(win, app):
    _show_player(win, app)
    start = win.volume.value()
    QTest.keyClick(win, Qt.Key.Key_9)
    assert win.mpv.volume == start - 5
    QTest.keyClick(win, Qt.Key.Key_0)
    assert win.mpv.volume == start
    QTest.keyClick(win, Qt.Key.Key_M)
    assert win.volume.value() == 0
    QTest.keyClick(win, Qt.Key.Key_M)
    assert win.volume.value() == start


def test_speed_keys_fine_mult_reset(win, app):
    _show_player(win, app)
    QTest.keyClick(win, Qt.Key.Key_BracketRight)
    assert win.cfg.speed == 1.1
    QTest.keyClick(win, Qt.Key.Key_BracketLeft)
    assert win.cfg.speed == 1.0
    QTest.keyClick(win, Qt.Key.Key_BracketLeft)
    assert win.cfg.speed == 0.9
    QTest.keyClick(win, Qt.Key.Key_BraceRight)  # 0.9 * 2
    assert win.cfg.speed == 1.8
    QTest.keyClick(win, Qt.Key.Key_BraceLeft)   # 1.8 * 0.5
    assert win.cfg.speed == 0.9
    QTest.keyClick(win, Qt.Key.Key_Backspace)
    assert win.cfg.speed == 1.0
    assert win.btn_speed.text() == "1×"


def test_keys_typed_while_browsing_are_not_consumed(win, app):
    # Focus sits in the chat filter -> player hotkeys must NOT fire, and the
    # character must land in the field instead.
    win.chat_filter.setFocus()
    QTest.keyClick(win.chat_filter, Qt.Key.Key_BracketRight)
    QTest.keyClick(win.chat_filter, Qt.Key.Key_M)
    QTest.keyClick(win.chat_filter, Qt.Key.Key_Space)
    assert win.chat_filter.text() == "]m "
    assert win.mpv.speeds == []
    assert win.mpv.toggle_count == 0


# ---------------- bottom-band cursor restore ----------------
def test_cursor_band_restores_hidden_controls(win, app):
    from PyQt6.QtCore import QPoint
    win.isFullScreen = lambda: True  # auto-hide is fullscreen-only
    win._playing = True
    win._fade_controls_out()
    QTest.qWait(350)
    assert win.transport.isVisible() is False

    # Cursor over the video center: stays hidden.
    win._global_cursor_pos = lambda: QPoint(win.x() + 400, win.y() + 200)
    win._on_cursor_tick()
    assert win.transport.isVisible() is False

    # Cursor inside the bottom band (where the transport lives): restored.
    win._global_cursor_pos = lambda: QPoint(win.x() + 400, win.height() - 20)
    win._on_cursor_tick()
    QTest.qWait(350)
    assert win.transport.isVisible() is True
    win._playing = False
    win._cursor_timer.stop()


def test_cursor_band_geometry(win):
    from PyQt6.QtCore import QPoint
    h = win.height()
    assert win._cursor_in_band(QPoint(0, h - 5)) is True      # very bottom
    assert win._cursor_in_band(QPoint(0, h - 70)) is True     # transport strip
    assert win._cursor_in_band(QPoint(0, h - 200)) is False   # upper video area
    assert win._cursor_in_band(QPoint(0, 0)) is False


def test_fade_hides_transport_only_while_playing(win, app):
    win.isFullScreen = lambda: True  # auto-hide is fullscreen-only
    win.transport.show()
    app.processEvents()

    # Not playing yet -> fade is a no-op.
    win._fade_controls_out()
    app.processEvents()
    assert win.transport.isVisible() is True

    # Playing -> fade hides, activity brings it back.
    win._playing = True
    win._fade_controls_out()
    QTest.qWait(350)
    assert win.transport.isVisible() is False

    win._on_user_activity()
    QTest.qWait(350)
    assert win.transport.isVisible() is True
    win._playing = False


def test_pause_keeps_transport_visible(win, app):
    win.isFullScreen = lambda: True  # auto-hide is fullscreen-only
    win._playing = True
    win._fade_controls_out()
    QTest.qWait(350)
    assert win.transport.isVisible() is False
    # Pausing calls _on_playing_changed(False): controls come right back.
    win._on_playing_changed(False)
    QTest.qWait(350)
    assert win.transport.isVisible() is True


# ---------------- empty states & workflow polish ----------------
def test_empty_states_guide_the_user(win, app):
    """With nothing loaded the panels explain what to do next instead of
    showing blank space."""
    assert win.empty_chats.isVisible()          # not logged in
    assert win.empty_chats.text() == "Sign in to browse your chats"
    assert win.empty_media.isVisible()
    assert "Pick a chat" in win.empty_media.text()
    assert win.empty_playlist.isVisible()
    assert "Nothing queued" in win.empty_playlist.text()


def test_empty_states_update_on_login_and_chats(win, app):
    win._on_connected("Alice")
    win._on_chats_loaded([])
    assert not win.btn_login.isVisible()
    assert win.empty_chats.text() == "No chats found"
    assert win.chat_header.text() == "CHATS · 0"


def test_media_rows_hide_empty_state_and_update_header(win, app):
    win._current_chat = 123
    from telegram_player.media import MediaItem

    win._render_media([MediaItem(123, 1, "Clip", 30.0, 1024)])
    assert not win.empty_media.isVisible()
    assert win.media_header.text() == "MEDIA · 1"


def test_window_title_follows_playback(win, app):
    _show_player(win, app)
    win.mpv._ipc = object()  # _poll early-returns without a live socket
    win._poll()
    assert "T — Telegram Player" == win.windowTitle()  # FakeMpv title "T"
    win._show_media_panel()
    assert win.windowTitle() == "Telegram Player"


def test_play_button_glyph_follows_pause_state(win, app):
    from telegram_player.ui.main_window import _GLYPHS
    _show_player(win, app)
    win.mpv._ipc = object()
    win._poll()
    assert win.btn_play.text() == _GLYPHS["pause"]
    win.mpv.pause = True
    win._poll()
    assert win.btn_play.text() == _GLYPHS["play"]


def test_help_button_present(win):
    assert win.btn_help.text() == "?"
    assert "Keyboard" in win.btn_help.toolTip()


def test_speed_menu_has_checkmark_sync(win):
    assert next(a for a in win._speed_menu.actions() if a.data() == 1.0).isChecked()
    win._set_speed(1.5)
    assert next(a for a in win._speed_menu.actions() if a.data() == 1.5).isChecked()
    assert not next(a for a in win._speed_menu.actions() if a.data() == 1.0).isChecked()


# ---------------- windowed mode: no auto-hide, no focus theft ----------------
def test_windowed_mode_keeps_transport_visible(win, app):
    """Outside fullscreen the transport never auto-hides."""
    win._playing = True
    win._fade_controls_out()          # must be a no-op
    app.processEvents()
    assert win.transport.isVisible() is True
    win._on_playing_changed(True)     # must NOT arm the hide/cursor timers
    assert win._hide_controls_timer.isActive() is False
    assert win._cursor_timer.isActive() is False
    win._on_cursor_tick()             # must not trigger anything
    assert win.transport.isVisible() is True
    win._playing = False


def test_windowed_activity_does_not_steal_focus(win, app):
    """_on_user_activity must not activateWindow outside fullscreen."""
    calls = []
    win.activateWindow = lambda: calls.append("activate")
    win.setFocus = lambda: calls.append("focus")
    win._playing = True
    win._on_user_activity()
    assert calls == []


def test_fullscreen_arms_and_windowed_disarms_autohide(win, app):
    win._playing = True
    win.isFullScreen = lambda: True
    win._enter_fullscreen_mode()
    assert win._hide_controls_timer.isActive() is True
    assert win._cursor_timer.isActive() is True
    win.isFullScreen = lambda: False
    win._exit_fullscreen_mode()
    assert win._hide_controls_timer.isActive() is False
    assert win._cursor_timer.isActive() is False
    assert win.transport.isVisible() is True
    win._playing = False


# ---------------- seekbar tap-to-seek ----------------
def test_seekbar_tap_jumps_and_seeks(win, app):
    from PyQt6.QtCore import QPoint, Qt
    from PyQt6.QtTest import QTest

    win.seek.setRange(0, 1000)
    win.seek.setValue(0)
    win._playing = True
    win.mpv._ipc = object()
    win.mpv.poll_state = lambda: {"duration": 100.0, "position": 0.0}
    # Click at 75% of the track width -> value jumps to ~750 AND the release
    # triggers _on_seek_released -> mpv.seek(75s).
    QTest.mouseClick(win.seek, Qt.MouseButton.LeftButton,
                     Qt.KeyboardModifier.NoModifier,
                     QPoint(int(win.seek.width() * 0.75), win.seek.height() // 2))
    assert 700 <= win.seek.value() <= 800
    assert win.mpv.seeks_abs and 70 <= win.mpv.seeks_abs[-1] <= 80




def _reset_playlists(win):
    """Tests share one XDG_CONFIG_HOME; reset to a single fresh playlist."""
    win.playlists.playlists = [{"name": "Playlist 1", "items": []}]
    win.playlists.active = 0
    win._sync_playlist_tabs()

# ---------------- playlist tabs ----------------
def test_playlist_tabs_create_and_switch(win, app):
    _reset_playlists(win)
    assert win.playlist_tabs.count() == 1
    assert win.playlist_tabs.tabText(0) == "Playlist 1"
    win._new_playlist()
    assert win.playlist_tabs.count() == 2
    assert win.playlist_tabs.currentIndex() == 1
    assert win.playlists.active == 1
    # add items to the new tab
    win.playlists.set_items([PlaylistItem(1, 9, "Nine", 30.0, 1)])
    win._load_playlist_view()
    assert win.playlist_view.count() == 1
    # switch back to tab 0
    win.playlist_tabs.setCurrentIndex(0)
    app.processEvents()
    assert win.playlists.active == 0
    assert win.playlist_view.count() == 0
    assert win.playlist_count.text() == "0 items"


def test_playlist_tab_delete_and_last_kept(win, app):
    _reset_playlists(win)
    win._new_playlist()
    assert win.playlist_tabs.count() == 2
    win._delete_playlist(1)
    assert win.playlist_tabs.count() == 1
    assert win.playlists.active == 0
    win._delete_playlist(0)  # deleting the last one keeps a fresh empty list
    assert win.playlist_tabs.count() == 1
    assert win.playlists.items() == []


def test_play_selected_writes_into_active_playlist(win, app):
    _reset_playlists(win)
    from telegram_player.media import MediaItem
    from telegram_player.ui.main_window import _NumItem

    win._new_playlist()  # active tab = index 1
    it = MediaItem(1, 5, "Five", 30.0, 1)
    cell = _NumItem(5)
    cell.setData(Qt.ItemDataRole.UserRole, it)
    cell.setText("Five")
    win.media_table.setRowCount(1)
    win.media_table.setItem(0, 0, cell)
    win.media_table.selectRow(0)
    win._play_selected()
    assert [i.message_id for i in win.playlists.items()] == [5]
    assert win.playlists.active == 1  # wrote into the active (new) playlist


# ---------------- media-type filter tabs ----------------
def _media_items():
    # Distinct message ids (90+) so they don't collide with playlists persisted
    # by sibling tests into the shared XDG_CONFIG_HOME.
    return [
        MediaItem(1, 90, "Vid A", 10, 100, "video"),
        MediaItem(1, 91, "Vid B", 20, 200, "video"),
        MediaItem(1, 92, "Song", 30, 300, "audio"),
        MediaItem(1, 93, "Pic", None, 400, "photo"),
        MediaItem(1, 94, "Doc.pdf", None, 500, "other"),
    ]


def test_media_tab_filters_by_kind(win, app):
    win._media_items = _media_items()
    win._media_kind_filter = win._media_tab_defs[0][1]  # Videos
    win._render_media(win._media_items)
    assert win.media_table.rowCount() == 2
    assert {win.media_table.item(r, 0).text() for r in range(2)} == {"Vid A", "Vid B"}
    # switch to Other tab
    win.media_tabs.setCurrentIndex(3)
    win._on_media_tab_changed(3)
    assert win.media_table.rowCount() == 1
    assert win.media_table.item(0, 0).text() == "Doc.pdf"
    assert win.btn_play_media.text() == "DOWNLOAD"  # non-playable -> download


def test_media_play_on_other_tab_downloads_not_plays(win, app):
    win._media_items = _media_items()
    win.media_tabs.setCurrentIndex(3)            # Other
    win._on_media_tab_changed(3)
    seen = []
    win.tg.download_all = lambda items: seen.append(items)
    win._play_selected()                          # nothing selected -> whole tab
    assert seen, "Other tab 'Play' should download"
    # The non-playable doc (mid 94) was downloaded, not added to a playlist.
    assert seen[0][0][1] == 94
    assert all(i.message_id != 94 for i in win.playlists.items())


def test_media_selected_or_whole_view(win, app):
    win._media_items = _media_items()
    win._media_kind_filter = win._media_tab_defs[0][1]
    win._render_media(win._media_items)
    # nothing selected -> whole visible (filtered) view
    assert [i.message_id for i in win._selected_media()] == [90, 91]


# ---------------- seek bar: tap AND drag-to-seek ----------------

def _mouse_event(typ, x, buttons):
    # Press/release report the involved button; move reports NoButton.
    button = Qt.MouseButton.NoButton if typ == QEvent.Type.MouseMove else Qt.MouseButton.LeftButton
    return QMouseEvent(typ, QPointF(x, 8), button, buttons, Qt.KeyboardModifier.NoModifier)


def test_jumpslider_tap_and_drag():
    s = _JumpSlider(Qt.Orientation.Horizontal)
    s.setRange(0, 1000)
    s.resize(1000, 20)
    # Tap at 25% -> jumps there.
    s.mousePressEvent(_mouse_event(QEvent.Type.MouseButtonPress, 250, Qt.MouseButton.LeftButton))
    assert s.value() == 250
    s.mouseReleaseEvent(_mouse_event(QEvent.Type.MouseButtonRelease, 250, Qt.MouseButton.NoButton))
    # Drag: press at 25%, move to 75% while held -> handle follows pointer.
    s.mousePressEvent(_mouse_event(QEvent.Type.MouseButtonPress, 250, Qt.MouseButton.LeftButton))
    assert s.isSliderDown()
    s.mouseMoveEvent(_mouse_event(QEvent.Type.MouseMove, 750, Qt.MouseButton.LeftButton))
    assert s.value() == 750, "handle must follow the drag"
    s.mouseReleaseEvent(_mouse_event(QEvent.Type.MouseButtonRelease, 750, Qt.MouseButton.NoButton))
    assert not s.isSliderDown()


def test_poll_does_not_overwrite_while_seeking(win):
    # Simulate a live playback poll; with the slider held down (_JumpSlider
    # sets it during tap/drag) the position must NOT be overwritten.
    win.mpv._ipc = "x"  # enable _poll's early-out guard
    win.seek.setSliderDown(True)
    win.seek.setValue(300)
    win._poll()
    assert win.seek.value() == 300, "drag position must survive a poll"
    win.seek.setSliderDown(False)
    win._poll()
    assert win.seek.value() == 100, "released -> follows playback (10/100)"
