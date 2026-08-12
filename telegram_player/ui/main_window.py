"""Main application window: chat list, media table, playlist, transport."""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path

from PyQt6.QtCore import (
    QEasingCurve,
    QEvent,
    QObject,
    QPropertyAnimation,
    Qt,
    QTimer,
)
from PyQt6.QtCore import QPointF
from PyQt6.QtGui import QFont
from PyQt6.QtGui import QPalette
from PyQt6.QtGui import QKeySequence
from PyQt6.QtGui import QCursor
from PyQt6.QtGui import QShortcut
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QDialog,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QPushButton,
    QSlider,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
    QTabBar,)

from ..config import Config
from .theme import uppercase_button
from ..media import ChatInfo
from ..mpv_ctl import MpvCtl
from ..playlist import PlaylistItem, PlaylistCollection
from ..tg_client import PasswordNeeded, TelegramService
from ..updater import UpdateChecker, ReleaseInfo, appimage_path
from .settings_dialog import SettingsDialog
from .login_dialog import LoginDialog

log = logging.getLogger(__name__)

_GLYPHS = {"prev": "⏮", "play": "▶", "pause": "⏸", "next": "⏭", "stop": "⏹", "fullscreen": "⛶"}


def _fmt(sec) -> str:
    if sec is None:
        return "--:--"
    s = int(sec)
    m, s = divmod(s, 60)
    return f"{m}:{s:02d}"


class _NumItem(QTableWidgetItem):
    """Table item that sorts by the raw numeric value in UserRole, not text."""

    def __init__(self, value: float):
        super().__init__()
        self.setData(Qt.ItemDataRole.UserRole, float(value or -1))

    def __lt__(self, other) -> bool:
        a = float(self.data(Qt.ItemDataRole.UserRole) or -1)
        b = float(other.data(Qt.ItemDataRole.UserRole) or -1)
        return a < b


class _DblClickFilter(QObject):
    """Turns a left double-click on the video container into a callback
    (toggle fullscreen). The overlay child is mouse-transparent, so the event
    reaches the container itself."""

    def __init__(self, callback):
        super().__init__()
        self._callback = callback

    def eventFilter(self, obj, event) -> bool:
        if (
            event.type() == QEvent.Type.MouseButtonDblClick
            and event.button() == Qt.MouseButton.LeftButton
        ):
            self._callback()
            return True
        return super().eventFilter(obj, event)


class _ActivityFilter(QObject):
    """App-level filter that reports user activity (mouse move/click/wheel,
    any keypress). Used to wake the fading transport controls."""

    def __init__(self, callback, parent=None):
        super().__init__(parent)
        self._callback = callback

    def eventFilter(self, obj, event) -> bool:
        if event.type() in (
            QEvent.Type.MouseMove,
            QEvent.Type.MouseButtonPress,
            QEvent.Type.Wheel,
            QEvent.Type.KeyPress,
            QEvent.Type.KeyRelease,
        ):
            self._callback()
        return False


class _ResizeSync(QObject):
    """Keeps an overlay label filling its parent widget's geometry on resize."""

    def __init__(self, target, parent):
        super().__init__(parent)
        self._target = target

    def eventFilter(self, obj, event) -> bool:
        if event.type() == QEvent.Type.Resize:
            self._target.setGeometry(obj.rect())
        return False


class _TabCloseButton(QPushButton):
    """Themed per-tab close (✕) — replaces QTabBar's native close button so it
    is uniformly visible on the dark theme (the native one renders red on the
    active tab and a mismatched gray on the others). The X is drawn directly so
    it never depends on a font glyph being available."""

    def __init__(self, parent=None):
        super().__init__()
        self.setObjectName("tabClose")
        self.setFixedSize(18, 18)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def paintEvent(self, event) -> None:
        from PyQt6.QtGui import QColor, QPainter

        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        color = QColor("#B3B3B3")  # MUTED
        if self.underMouse():
            color = QColor("#F3727F")  # DANGER on hover
        p.setPen(color)
        p.setBrush(color)
        r = self.rect().adjusted(5, 5, -5, -5)
        m = 2
        p.drawLine(r.x(), r.y(), r.right(), r.bottom())
        p.drawLine(r.right(), r.y(), r.x(), r.bottom())
        p.drawEllipse(r.center(), m, m)
        p.end()


class _GlyphButton(QPushButton):
    """QPushButton that draws its glyph dead-center on its REAL ink box.

    Symbol glyphs (▶ ⏸ ⏮ ⏭ ⏹ ⛶) have asymmetric font metrics — some sit
    visibly low, others high — so Qt's advance-based text centering makes the
    play/pause icon look off-center and jump when it swaps. We render the glyph
    once onto a transparent pixmap, measure its actual painted ink box, and
    blit that box centered on the button.
    """

    _cache: dict = {}

    def _ink(self, text: str):
        from PyQt6.QtGui import QFontMetrics, QPainter, QPixmap

        key = (text, self.font().toString(), self.palette().color(
            QPalette.ColorRole.ButtonText).name())
        hit = self._cache.get(key)
        if hit is not None:
            return hit
        fm = QFontMetrics(self.font())
        w = fm.horizontalAdvance(text) + 8
        h = fm.ascent() + fm.descent() + 8
        pm = QPixmap(w, h)
        pm.fill(Qt.GlobalColor.transparent)
        qp = QPainter(pm)
        qp.setFont(self.font())
        qp.setPen(self.palette().color(QPalette.ColorRole.ButtonText))
        qp.drawText(4, fm.ascent() + 4, text)
        qp.end()
        img = pm.toImage()
        minx, miny, maxx, maxy = w, h, -1, -1
        for y in range(h):
            for x in range(w):
                if img.pixelColor(x, y).alpha() > 8:
                    if x < minx: minx = x
                    if x > maxx: maxx = x
                    if y < miny: miny = y
                    if y > maxy: maxy = y
        ink = (minx, miny, maxx - minx + 1, maxy - miny + 1)
        self._cache[key] = (ink, pm)
        return self._cache[key]

    def paintEvent(self, event) -> None:
        from PyQt6.QtGui import QPalette
        from PyQt6.QtWidgets import QStyle, QStyleOptionButton, QStylePainter

        opt = QStyleOptionButton()
        self.initStyleOption(opt)
        text = self.text()
        opt.text = ""  # style paints the button chrome only
        p = QStylePainter(self)
        p.drawControl(QStyle.ControlElement.CE_PushButton, opt)
        if text:
            (ix, iy, iw, ih), pm = self._ink(text)
            c = self.rect().center()
            # +1: QStylePainter blits one device pixel left/up of the requested
            # target on this platform; compensate so the ink lands dead-center.
            p.drawPixmap(
                round(c.x() - iw / 2.0) + 1, round(c.y() - ih / 2.0) + 1,
                pm, ix, iy, iw, ih,
            )
        p.end()


class _JumpSlider(QSlider):
    """QSlider that jumps the handle straight to the pointer, so a single tap
    anywhere on the track seeks there (tap-to-skip) **and** the handle can be
    dragged to scrub (drag-to-seek)."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # Make sure we receive move events even between presses.
        self.setMouseTracking(True)

    def _value_at(self, event) -> int:
        frac = event.position().x() / max(1, self.width())
        return round(self.minimum() + frac * (self.maximum() - self.minimum()))

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.setValue(self._value_at(event))
            self.setSliderDown(True)  # so the release emits sliderReleased
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        # While the button is held, follow the pointer so the handle can be
        # dragged to scrub. _poll() is suppressed while isSliderDown(), so it
        # won't fight the drag.
        if self.isSliderDown() and (event.buttons() & Qt.MouseButton.LeftButton):
            self.setValue(self._value_at(event))
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        # Ending a press (tap or drag) with the slider down makes QSlider emit
        # sliderReleased itself -> _on_seek_released seeks to the final value.
        if event.button() == Qt.MouseButton.LeftButton and self.isSliderDown():
            self.setSliderDown(False)
            event.accept()
            return
        super().mouseReleaseEvent(event)



class _PlayerKeysFilter(QObject):
    """App-level filter that intercepts player hotkeys while the video view is
    active. Returns True (consumed) for keys the player owns; everything else
    falls through to the focused widget unchanged."""

    def __init__(self, dispatch, parent=None):
        super().__init__(parent)
        self._dispatch = dispatch

    def eventFilter(self, obj, event) -> bool:
        if event.type() == QEvent.Type.KeyPress:
            return self._dispatch(event) is True
        return False


class MainWindow(QMainWindow):
    def __init__(self, cfg: Config, tg: TelegramService, mpv: MpvCtl):
        super().__init__()
        self.cfg = cfg
        self.tg = tg
        self.mpv = mpv
        # Multiple named playlists (tabs). The old single-playlist file is
        # migrated into "Playlist 1" transparently.
        self.playlists = PlaylistCollection(cfg.playlists_path, legacy_path=cfg.playlist_path)
        self.playlists.load()

        self.setWindowTitle("Telegram Player")
        self.resize(1000, 680)
        self._current_chat: int | None = None
        self._current_topic: int | None = None
        self._chat_forum: dict[int, bool] = {}
        self._logged_in = False
        self._media_items: list = []
        self._play_index = -1
        self._play_base = 0
        self._download_target: tuple[int, int] | None = None

        self._build_ui()
        self._wire()
        self._load_playlist_view()

        self._playing = False  # mpv is mid-playback (drives control auto-hide)
        self._controls_hidden = False
        self._prev_volume: int | None = None

        self._timer = QTimer(self)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self._poll)
        self._timer.start()

        self.tg.start()
        self._refresh_status()

    # ---------------- UI ----------------
    def _build_ui(self) -> None:
        root = QSplitter(Qt.Orientation.Horizontal)

        left = QWidget()
        left.setMinimumWidth(180)
        self.chat_filter = QLineEdit(); self.chat_filter.setPlaceholderText("Filter chats…")
        self.chat_list = QListWidget()
        # Chat titles must wrap instead of pushing a horizontal scrollbar out;
        # the list scrolls vertically only.
        self.chat_list.setWordWrap(True)
        self.chat_list.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.topic_combo = QComboBox()
        self.topic_combo.hide()
        self.status_label = QLabel("Starting…"); self.status_label.setObjectName("muted")
        self.btn_settings = QPushButton("Settings…")
        self.btn_login = QPushButton("Login…"); self.btn_login.setObjectName("accent")
        self.btn_logout = QPushButton("Logout"); self.btn_logout.setObjectName("danger")
        self.btn_refresh = QPushButton("Refresh chats")
        for _b in (self.btn_settings, self.btn_login, self.btn_logout, self.btn_refresh):
            uppercase_button(_b)

        # ---- left panel: brand mark + chat browser ----
        self.brand_label = QLabel("TELEGRAM PLAYER")
        self.brand_label.setObjectName("brand")
        self.chat_header = QLabel("CHATS")
        self.chat_header.setObjectName("panelHeader")
        self.empty_chats = self._make_empty(self.chat_list, "Sign in to browse your chats")
        lcol = QVBoxLayout(left)
        lcol.setContentsMargins(10, 10, 6, 6)
        lcol.setSpacing(6)
        lcol.addWidget(self.brand_label)
        lcol.addWidget(self.chat_header)
        lcol.addWidget(self.chat_filter)
        lcol.addWidget(self.chat_list, 1)
        lcol.addWidget(self.topic_combo)
        lcol.addWidget(self.status_label)
        lcol.addWidget(self.btn_settings)
        lcol.addWidget(self.btn_login)
        lcol.addWidget(self.btn_logout)
        lcol.addWidget(self.btn_refresh)

        right = QSplitter(Qt.Orientation.Vertical)
        rtop = QWidget()
        self.media_panel = QWidget()
        self.media_table = QTableWidget(0, 3)
        self.media_table.setHorizontalHeaderLabels(["Title", "Duration", "Size"])
        self.media_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.media_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.media_table.verticalHeader().setVisible(False)
        self.btn_play_media = QPushButton("▶ Play")
        self.btn_add = QPushButton("+ Add to playlist")
        self.btn_load_more = QPushButton("Load more…")
        for _b in (self.btn_play_media, self.btn_add, self.btn_load_more):
            uppercase_button(_b)
        self.media_header = QLabel("MEDIA")
        self.media_header.setObjectName("panelHeader")
        self.empty_media = self._make_empty(self.media_table, "Pick a chat to see its videos")
        # Media-type filter tabs (like the playlist tabs): Videos / Audio /
        # Photos & GIF / Other. The visible list is filtered by the active tab;
        # "▶ Play" plays video/audio/photos/gifs, while the "Other" tab (files
        # mpv cannot play) is a download-only action.
        self._media_tab_defs = [
            ("Videos", {"video"}),
            ("Audio", {"audio"}),
            ("Photos & GIF", {"photo", "gif"}),
            ("Other", {"other"}),
        ]
        self._media_kind_filter = self._media_tab_defs[0][1]
        self._media_items: list = []
        self._media_shown: list = []
        self.media_tabs = QTabBar()
        self.media_tabs.setDocumentMode(True)
        self.media_tabs.setExpanding(False)
        self.media_tabs.setToolTip("Filter media by type")
        for label, _ in self._media_tab_defs:
            self.media_tabs.addTab(label)
        mcol = QVBoxLayout(self.media_panel)
        mcol.setContentsMargins(0, 10, 0, 0)
        mcol.setSpacing(6)
        mcol.addWidget(self.media_header)
        mcol.addWidget(self.media_tabs)
        mcol.addWidget(self.media_table, 1)
        mrow = QHBoxLayout()
        mrow.addWidget(self.btn_play_media)
        mrow.addWidget(self.btn_add)
        mrow.addWidget(self.btn_load_more)
        mcol.addLayout(mrow)

        # The upper panel becomes the video player while playing. mpv draws
        # directly into this widget's native X window (--wid), so Qt must never
        # paint its backing store over it (WA_PaintOnScreen) — requires the xcb
        # platform, which app.main() forces.
        self.player_container = QWidget()
        self.player_container.setStyleSheet("background: #000;")
        self.player_container.setAttribute(Qt.WidgetAttribute.WA_PaintOnScreen, True)
        # Keep ONE stable native window for the app's lifetime: without
        # WA_NativeWindow, Qt destroys the native window when the container is
        # hidden (media panel swap) and recreates it on the next show — mpv
        # must never embed into a fresh, unmapped window (live-tested: first
        # play in a session could end up with mpv opening its OWN window).
        self.player_container.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
        self.player_container.hide()
        # Status overlay drawn INSIDE the video container while mpv is still
        # loading/downloading. The container is a native X window (mpv paints
        # into it via --wid), so this child becomes a native window too and X
        # stacks it above mpv's frames. Hidden once playback actually starts.
        self.player_overlay = QWidget(self.player_container)
        self.player_overlay.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.overlay_state = QLabel("Preparing…")
        self.overlay_state.setObjectName("overlayState")
        self.overlay_state.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.overlay_title = QLabel("")
        self.overlay_title.setObjectName("overlayTitle")
        self.overlay_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ovl = QVBoxLayout(self.player_overlay)
        ovl.addStretch(1)
        ovl.addWidget(self.overlay_state)
        ovl.addWidget(self.overlay_title)
        ovl.addStretch(2)
        self.player_overlay.hide()
        # A layout on the native container only manages the overlay's geometry;
        # Qt never paints the container itself (WA_PaintOnScreen).
        cl = QVBoxLayout(self.player_container)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.addWidget(self.player_overlay)
        rtcol = QVBoxLayout(rtop)
        rtcol.setContentsMargins(0, 0, 0, 0)
        rtcol.setSpacing(0)
        rtcol.addWidget(self.media_panel)
        rtcol.addWidget(self.player_container)

        rbot = QWidget()
        self.playlist_view = QListWidget()
        self.playlist_view.setWordWrap(True)
        self.playlist_view.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.btn_remove = QPushButton("Remove")
        self.btn_clear = QPushButton("Clear"); self.btn_clear.setObjectName("danger")
        self.btn_download_all = QPushButton("Download all")
        self.btn_clear_cache = QPushButton("Clear cache")
        for _b in (self.btn_remove, self.btn_clear, self.btn_download_all, self.btn_clear_cache):
            uppercase_button(_b)
        # Playlist tabs: one tab per saved list, a "+" creates another, tabs
        # are closable, double-click a tab to rename it.
        self.playlist_tabs = QTabBar()
        self.playlist_tabs.setTabsClosable(False)  # custom close button
        self.playlist_tabs.setMovable(False)
        self.playlist_tabs.setDocumentMode(True)
        self.playlist_tabs.setExpanding(False)
        self.playlist_tabs.setToolTip(
            "Double-click a tab to rename · close (✕) to delete"
        )
        self.btn_new_playlist = QPushButton("+")
        self.btn_new_playlist.setFixedSize(28, 28)
        self.btn_new_playlist.setObjectName("accentRound")
        self.btn_new_playlist.setToolTip("Create a new playlist")
        self.playlist_count = QLabel()
        self.playlist_count.setObjectName("meta")
        tabrow = QHBoxLayout()
        tabrow.setContentsMargins(0, 0, 0, 0)
        tabrow.setSpacing(6)
        tabrow.addWidget(self.playlist_tabs, 1)
        tabrow.addWidget(self.playlist_count)
        tabrow.addWidget(self.btn_new_playlist)
        self.empty_playlist = self._make_empty(
            self.playlist_view,
            "Nothing queued — double-click a video to add it",
        )
        pcol = QVBoxLayout(rbot)
        pcol.setContentsMargins(0, 10, 0, 0)
        pcol.setSpacing(6)
        pcol.addLayout(tabrow)
        pcol.addWidget(self.playlist_view, 1)
        prow = QHBoxLayout()
        prow.addWidget(self.btn_remove); prow.addWidget(self.btn_clear)
        prow.addWidget(self.btn_download_all); prow.addWidget(self.btn_clear_cache)
        pcol.addLayout(prow)

        right.addWidget(rtop); right.addWidget(rbot)
        right.setStretchFactor(0, 3); right.setStretchFactor(1, 2)
        root.addWidget(left); root.addWidget(right)
        root.setSizes([240, 760])
        root.setHandleWidth(1)
        right.setHandleWidth(1)

        # References kept for the fullscreen toggle: hide everything except the
        # player container + transport, and restore afterwards.
        self.left_panel = left
        self.right_splitter = right
        self.playlist_panel = rbot
        self.root_splitter = root

        self._build_transport()
        outer = QVBoxLayout()
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.update_banner = self._make_update_banner()
        self.update_banner.hide()
        outer.addWidget(self.update_banner)
        outer.addWidget(root, 1)
        outer.addWidget(self.transport)
        container = QWidget(); container.setLayout(outer)
        self.setCentralWidget(container)
        self._pending_update: ReleaseInfo | None = None
        root.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        right.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        # Double-click on the video toggles fullscreen (standard player UX).
        self._container_dblclick = _DblClickFilter(self._toggle_fullscreen)
        self.player_container.installEventFilter(self._container_dblclick)
        # Create the player container's native X window NOW, after the full
        # widget hierarchy is rooted in the QMainWindow. Creating it earlier
        # (while the container had no real top-level) built a native window in a
        # transient tree that Qt later orphaned — handing mpv a dead --wid so it
        # opened its OWN window. With WA_NativeWindow this single window is kept
        # for the app's lifetime and reused on every play.
        self.player_container.winId()
        self._update_empty_states()

    def _make_empty(self, parent: QWidget, text: str) -> QLabel:
        """A mouse-transparent placeholder shown over a list/table when it has
        no rows, so every empty panel explains what to do next."""
        lbl = QLabel(text, parent)
        lbl.setObjectName("empty")
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl.setWordWrap(True)
        lbl.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        lbl.hide()
        sync = _ResizeSync(lbl, parent)
        parent.installEventFilter(sync)
        return lbl

    def _update_empty_states(self) -> None:
        """Keep the placeholder overlays in sync with the data panels, so the
        app always explains its current state instead of showing blank space."""
        if self.chat_list.count() == 0:
            self.empty_chats.setText(
                "No chats found" if self._logged_in else "Sign in to browse your chats"
            )
            self.empty_chats.setGeometry(self.chat_list.rect())
            self.empty_chats.raise_()
            self.empty_chats.show()
        else:
            self.empty_chats.hide()
        if self.media_table.rowCount() == 0:
            if self._current_chat is None:
                msg = "Pick a chat to see its media"
            else:
                tab = self._media_tab_defs[self.media_tabs.currentIndex()][0]
                msg = f"No {tab.lower()} here yet"
            self.empty_media.setText(msg)
            self.empty_media.setGeometry(self.media_table.rect())
            self.empty_media.raise_()
            self.empty_media.show()
        else:
            self.empty_media.hide()
        if self.playlist_view.count() == 0:
            self.empty_playlist.setGeometry(self.playlist_view.rect())
            self.empty_playlist.raise_()
            self.empty_playlist.show()
        else:
            self.empty_playlist.hide()

    def _build_transport(self) -> None:
        self.btn_prev = _GlyphButton(_GLYPHS["prev"])
        self.btn_play = _GlyphButton(_GLYPHS["play"])
        self.btn_play.setObjectName("transportPlay")
        self.btn_play.setToolTip("Play / Pause  (Space)")
        self.btn_next = _GlyphButton(_GLYPHS["next"])
        self.btn_stop = _GlyphButton(_GLYPHS["stop"])
        self.btn_stop.setToolTip("Stop  (and return to the media table)")
        self.btn_fullscreen = _GlyphButton(_GLYPHS["fullscreen"])
        self.btn_fullscreen.setCheckable(True)
        self.btn_fullscreen.setToolTip(
            "Fullscreen (F11, F, double-click video) — Esc to leave"
        )
        self.btn_fullscreen.setEnabled(False)  # meaningful only while playing
        for b in (self.btn_prev, self.btn_next, self.btn_stop, self.btn_fullscreen):
            b.setFixedWidth(40); b.setFixedHeight(36)
            b.setObjectName("transport")
        self.btn_play.setFixedSize(46, 46)
        self.btn_prev.setToolTip("Previous  (Shift+←)")
        self.btn_next.setToolTip("Next  (Shift+→)")
        # Playback-speed control. The choice is saved to config AND applied as
        # mpv's persistent `speed` property, so the next playlist entry (and the
        # next app session) keeps the same rate.
        self.btn_speed = QPushButton(f"{self.cfg.speed:g}×")
        self.btn_speed.setFixedSize(64, 32)
        self.btn_speed.setObjectName("transportPill")
        self.btn_speed.setToolTip(
            "Playback speed — [ ] fine steps, { } half/double, Backspace reset"
        )
        self._speed_menu = QMenu(self.btn_speed)
        for _spd in (0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0, 4.0):
            act = self._speed_menu.addAction(f"{_spd:g}×")
            act.setCheckable(True)
            act.setData(_spd)
        self._speed_menu.triggered.connect(self._on_speed_selected)
        if self.cfg.speed not in (0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0, 4.0):
            act = self._speed_menu.addAction(f"Custom {self.cfg.speed:g}×")
            act.setCheckable(True)
            act.setData(self.cfg.speed)
        self._sync_speed_menu_checks()
        self.btn_speed.setMenu(self._speed_menu)
        # Keyboard help.
        self.btn_help = QPushButton("?")
        self.btn_help.setFixedWidth(40); self.btn_help.setFixedHeight(36)
        self.btn_help.setObjectName("transport")
        self.btn_help.setToolTip("Keyboard shortcuts")

        self.track_label = QLabel("Nothing playing"); self.track_label.setObjectName("title")
        self.seek = _JumpSlider(Qt.Orientation.Horizontal)
        self.seek.setRange(0, 1000)
        self.seek.setToolTip("Click / tap anywhere on the bar to seek — or drag the handle to scrub")
        self.seek.sliderReleased.connect(self._on_seek_released)
        self.volume = _JumpSlider(Qt.Orientation.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setValue(self.cfg.volume)
        self.volume.valueChanged.connect(self._on_volume)
        self.volume.setMaximumWidth(100)
        self.time_label = QLabel("--:-- / --:--")
        self.time_label.setObjectName("meta")

        row = QHBoxLayout()
        row.setContentsMargins(10, 5, 10, 5)
        row.setSpacing(8)
        row.addWidget(self.btn_prev, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(self.btn_play, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(self.btn_next, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(self.btn_stop, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(self.track_label, 1, Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(self.seek, 3, Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(self.time_label, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(self.volume, 1, Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(self.btn_speed, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(self.btn_fullscreen, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(self.btn_help, 0, Qt.AlignmentFlag.AlignVCenter)
        self.transport = QWidget(); self.transport.setLayout(row)
        self.transport.setObjectName("transportBar")

    def _sync_speed_menu_checks(self) -> None:
        for a in self._speed_menu.actions():
            a.setChecked(a.data() == self.cfg.speed)

    # ---------------- wiring ----------------
    def _wire(self) -> None:
        self.tg.connected.connect(self._on_connected)
        self.tg.status.connect(self._on_status)
        self.tg.login_failed.connect(self._on_login_failed)
        self.tg.chats_loaded.connect(self._on_chats_loaded)
        self.tg.qr_refreshed.connect(lambda _u: None)
        self.tg.playback_ready.connect(self._on_playback_ready)
        self.tg.playback_failed.connect(self._on_playback_failed)

        self.chat_filter.textChanged.connect(self._filter_chats)
        self.chat_list.currentItemChanged.connect(self._on_chat_selected)
        self.topic_combo.currentIndexChanged.connect(self._on_topic_selected)
        self.btn_settings.clicked.connect(self._open_settings)
        self.btn_login.clicked.connect(self._open_login)
        self.btn_logout.clicked.connect(self._logout)
        self.btn_refresh.clicked.connect(lambda: self.tg.refresh_chats())
        self.media_table.cellDoubleClicked.connect(self._on_media_double)
        self.btn_play_media.clicked.connect(self._play_selected)
        self.media_tabs.currentChanged.connect(self._on_media_tab_changed)
        self.btn_add.clicked.connect(self._add_selected_to_playlist)
        self.btn_load_more.clicked.connect(self._load_more)
        self.btn_remove.clicked.connect(self._remove_selected)
        self.btn_clear.clicked.connect(self._clear_playlist)
        self.btn_download_all.clicked.connect(self._download_all)
        self.btn_clear_cache.clicked.connect(self._clear_cache)
        self.playlist_view.itemDoubleClicked.connect(self._on_playlist_double)
        self.playlist_tabs.currentChanged.connect(self._on_playlist_tab_changed)
        self.playlist_tabs.tabBarDoubleClicked.connect(self._rename_playlist)
        self.btn_new_playlist.clicked.connect(self._new_playlist)
        self.btn_prev.clicked.connect(self.mpv.prev)
        self.btn_play.clicked.connect(self._toggle_play)
        self.btn_next.clicked.connect(self.mpv.next)
        self.btn_stop.clicked.connect(self._stop)
        self.btn_fullscreen.clicked.connect(self._toggle_fullscreen)
        self.btn_help.clicked.connect(self._open_help)
        self._fs_shortcut = QShortcut(QKeySequence("F11"), self)
        self._fs_shortcut.activated.connect(self._toggle_fullscreen)
        self.mpv.state_changed.connect(self._on_mpv_state)
        self.mpv.mpv_exited.connect(self._on_mpv_exited)
        self.mpv.file_loaded.connect(self._on_file_loaded)

        self._setup_player_keys()
        self._setup_fading_controls()

        # Periodic update check against GitHub Releases.
        self._updater = UpdateChecker(enabled=self.cfg.check_updates)
        self._updater.update_available.connect(self._on_update_available)
        self._updater.no_update.connect(self.update_banner.hide)
        self._updater.error.connect(lambda _e: self.update_banner.hide())
        self._updater.download_progress.connect(self._on_update_progress)
        self._updater.start()

    # ---------------- update checker ----------------
    def _make_update_banner(self) -> QWidget:
        from .theme import uppercase_button

        bar = QWidget()
        bar.setObjectName("updateBanner")
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(10, 6, 10, 6)
        lay.setSpacing(8)
        self.update_label = QLabel("Update available")
        self.update_label.setObjectName("updateText")
        self._btn_update = QPushButton("Update")
        self._btn_update.setObjectName("accent")
        uppercase_button(self._btn_update)
        btn_later = QPushButton("Later")
        btn_later.setObjectName("ghost")
        uppercase_button(btn_later)
        lay.addWidget(self.update_label, 1)
        lay.addWidget(self._btn_update)
        lay.addWidget(btn_later)
        self._btn_update.clicked.connect(self._on_update_click)
        btn_later.clicked.connect(bar.hide)
        return bar

    def _on_update_available(self, info: ReleaseInfo) -> None:
        self._pending_update = info
        is_appimage = appimage_path() is not None
        self._btn_update.setText("UPDATE" if is_appimage else "OPEN")
        self.update_label.setText(
            f"A new build is available: {info.name}  ·  "
            + ("update & restart" if is_appimage else "open release page")
        )
        self.update_banner.show()

    def _on_update_click(self) -> None:
        info = self._pending_update
        if info is None:
            return
        self._btn_update.setEnabled(False)
        self._btn_update.setText("DOWNLOADING…")
        self._updater.apply_update(info, on_done=self._on_update_done)

    def _on_update_progress(self, done: int, total: int) -> None:
        if total:
            self._btn_update.setText(f"DOWNLOADING… {done * 100 // total}%")

    def _on_update_done(self, err: str | None) -> None:
        if err:
            self.update_label.setText(f"Update failed: {err}")
            self._btn_update.setEnabled(True)
            self._btn_update.setText("RETRY")
        # On AppImage success the process is replaced and this never returns.

    # ---------------- status / auth ----------------
    def _refresh_status(self) -> None:
        self._on_status("Starting…")

    def _on_status(self, msg: str) -> None:
        self.status_label.setText(msg)
        # Mirror download progress onto the player panel: mpv stays black while
        # the file downloads (its demuxer needs the moov atom, usually at the
        # end of the file), so the overlay is the only visible feedback.
        if msg.startswith("Downloading "):
            m = re.match(r"^Downloading (\d+)%…$", msg)
            if m:
                self._show_overlay(f"Downloading {m.group(1)}%…")
            else:
                self._show_overlay("Downloading…")
        elif msg.startswith(("Cached:", "Skipped unavailable:", "Download failed:", "Downloaded")):
            # Download finished/aborted: mpv will either start or error-skip.
            self.player_overlay.hide()

    def _on_connected(self, name: str) -> None:
        self._logged_in = True
        self.btn_login.setVisible(False)
        self.btn_logout.setVisible(True)
        self._on_status(f"Logged in as {name}")
        self._update_empty_states()
        self.tg.refresh_chats()

    def _on_login_failed(self, msg: str) -> None:
        self.status_label.setText(msg)
        self.track_label.setText(msg)

    def _open_settings(self) -> None:
        dlg = SettingsDialog(self.cfg, self)
        dlg.settings_changed.connect(self._on_settings_changed)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.tg.apply_credentials()

    def _on_settings_changed(self) -> None:
        # Reflect the new update-check preference on the live checker.
        enabled = self.cfg.check_updates
        if getattr(self, "_updater", None) is None:
            return
        self._updater.enabled = enabled
        if enabled:
            self._updater.start()
        else:
            self._updater.stop()
            self.update_banner.hide()

    def closeEvent(self, e) -> None:
        # Stop the update poller before Qt tears the window down, so a pending
        # timer/thread can never fire into a half-destroyed MainWindow.
        upd = getattr(self, "_updater", None)
        if upd is not None:
            upd.enabled = False
            upd.stop()
        super().closeEvent(e)
    def _open_help(self) -> None:
        """Small always-available cheat sheet so the app explains its own
        shortcuts instead of hiding them."""
        from PyQt6.QtWidgets import QDialog, QLabel, QVBoxLayout
        from PyQt6.QtCore import Qt as _Qt
        dlg = QDialog(self)
        dlg.setWindowTitle("Keyboard shortcuts")
        dlg.setModal(True)
        rows = [
            ("Space / K", "Play / pause"),
            ("← / →", "Seek −5 s / +5 s"),
            ("Shift + ← / →", "Previous / next item"),
            ("↑ / ↓", "Seek −60 s / +60 s"),
            (", / .", "Seek −1 s / +1 s"),
            ("[ / ]", "Speed −0.1 / +0.1"),
            ("{ / }", "Speed ×½ / ×2"),
            ("Backspace", "Reset speed to 1×"),
            ("9 / 0", "Volume −5 / +5"),
            ("M", "Mute"),
            ("F / F11", "Fullscreen"),
            ("Esc", "Leave fullscreen"),
            ("Double-click video", "Fullscreen"),
            ("Hover screen bottom", "Show controls in fullscreen"),
        ]
        body = "".join(
            f'<tr><td style="color:#B3B3B3;padding:3px 14px 3px 0;">{k}</td>'
            f'<td style="padding:3px 0;">{v}</td></tr>'
            for k, v in rows
        )
        lbl = QLabel(f"<table>{body}</table>")
        lbl.setTextInteractionFlags(_Qt.TextInteractionFlag.TextSelectableByMouse)
        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.addWidget(lbl)
        dlg.setFixedWidth(330)
        dlg.exec()


    def _open_login(self) -> None:
        if not (self.cfg.api_id and self.cfg.api_hash):
            self._open_settings()
            return
        dlg = LoginDialog(self.tg, self.cfg, self)
        dlg.exec()

    def _logout(self) -> None:
        self._logged_in = False
        self.tg.logout()
        self.btn_login.setVisible(True)
        self.btn_logout.setVisible(False)
        self.chat_list.clear()
        self._update_empty_states()

    # ---------------- chats ----------------
    def _on_chats_loaded(self, chats: list[ChatInfo]) -> None:
        self._all_chats = chats
        self._chat_forum = {c.id: c.forum for c in chats}
        self._render_chats(chats)

    def _render_chats(self, chats: list[ChatInfo]) -> None:
        self.chat_list.clear()
        for c in chats:
            suffix = f", topics" if c.forum else ""
            item = QListWidgetItem(f"{c.title}  ({c.kind}{suffix})")
            item.setData(1, c.id)
            self.chat_list.addItem(item)
        self.chat_header.setText(f"CHATS · {len(chats)}")
        self._update_empty_states()

    def _filter_chats(self, text: str) -> None:
        chats = getattr(self, "_all_chats", [])
        text = text.lower()
        self._render_chats([c for c in chats if text in c.title.lower()])

    def _on_chat_selected(self, cur, _prev) -> None:
        if cur is None:
            return
        chat_id = cur.data(1)
        self._current_chat = chat_id
        self._current_topic = None
        self._media_items = []
        self.media_table.setRowCount(0)
        self._setup_topic_combo(chat_id, self._chat_forum.get(chat_id, False))
        self._load_media(chat_id)

    def _setup_topic_combo(self, chat_id: int, forum: bool) -> None:
        self.topic_combo.blockSignals(True)
        self.topic_combo.clear()
        if not forum:
            self.topic_combo.hide()
            self.topic_combo.blockSignals(False)
            return
        self.topic_combo.addItem("All topics", None)
        self.topic_combo.setCurrentIndex(0)
        self.topic_combo.show()
        self.topic_combo.blockSignals(False)

        def on_topics(topics: list):
            self.topic_combo.blockSignals(True)
            for tid, title in topics:
                self.topic_combo.addItem(title, tid)
            self.topic_combo.blockSignals(False)

        self.tg._call(lambda: self.tg.load_topics(chat_id), on_success=on_topics)

    def _on_topic_selected(self, _idx: int) -> None:
        if self._current_chat is None:
            return
        self._current_topic = self.topic_combo.currentData()
        self._media_items = []
        self.media_table.setRowCount(0)
        self._load_media(self._current_chat)

    # ---------------- media ----------------
    def _load_media(self, chat_id: int, offset: int = 0) -> None:
        def on_success(items):
            self._media_items = items if offset == 0 else self._media_items + items
            self._render_media(self._media_items)

        self.tg._call(
            lambda: self.tg.load_media(chat_id, offset, topic_id=self._current_topic),
            on_success=on_success,
            on_error=lambda e: self._on_status(f"Load failed: {e}"),
        )

    def _load_more(self) -> None:
        if self._current_chat is None or not self._media_items:
            return
        if self._current_topic is None and self._chat_forum.get(self._current_chat, False):
            self._on_status("Load more is not available in the All-topics view")
            return
        last = self._media_items[-1].message_id
        self._load_media(self._current_chat, offset=last)

    def _render_media(self, items: list) -> None:
        items = [i for i in items if i.kind in self._media_kind_filter]
        self._media_shown = items
        self.media_table.setSortingEnabled(False)
        self.media_table.setRowCount(len(items))
        for r, it in enumerate(items):
            t = QTableWidgetItem(it.title)
            t.setData(Qt.ItemDataRole.UserRole, it)
            d = _NumItem(it.duration if it.duration is not None else -1)
            d.setText(_fmt(it.duration))
            s = _NumItem(it.size if it.size is not None else -1)
            s.setText(self._size(it.size))
            self.media_table.setItem(r, 0, t)
            self.media_table.setItem(r, 1, d)
            self.media_table.setItem(r, 2, s)
        # Reset to "no sort" BEFORE enabling: enabling sorting re-sorts by Qt's
        # default indicator (col 0, descending), silently reversing the list on
        # every load. With -1 the table keeps natural (newest-first) order until
        # the user clicks a header.
        self.media_table.horizontalHeader().setSortIndicator(-1, Qt.SortOrder.AscendingOrder)
        self.media_table.setSortingEnabled(True)
        self.media_table.resizeColumnsToContents()
        self.media_header.setText(f"MEDIA · {len(items)}")
        self._update_empty_states()

    def _size(self, size) -> str:
        if not size:
            return ""
        mb = size / (1024 * 1024)
        return f"{mb:.1f} MB"

    def _media_item_at(self, row: int):
        it = self.media_table.item(row, 0)
        return it.data(Qt.ItemDataRole.UserRole) if it is not None else None

    def _selected_media(self) -> list:
        rows = sorted({i.row() for i in self.media_table.selectedIndexes()})
        sel = [m for m in (self._media_item_at(r) for r in rows) if m is not None]
        if sel:
            return sel
        # Nothing selected: act on the whole visible (filtered) media view, so a
        # tab + "▶ Play"/"+ Add" builds a playlist from that whole category.
        return list(self._media_shown)

    def _play_selected(self) -> None:
        items = self._selected_media()
        if not items:
            self._on_status("Select a media item first")
            return
        label = self._media_tab_defs[self.media_tabs.currentIndex()][0]
        if label == "Other":
            # Files mpv cannot play (pdf, zip, …) — just download them.
            self._download_items(items)
            return
        self.playlists.set_items([PlaylistItem(i.chat_id, i.message_id, i.title, i.duration, i.size, i.kind)
                                    for i in items])
        self.playlists.save()
        self._load_playlist_view()
        self._play(0)

    def _download_items(self, items: list) -> None:
        """Download the given media items into the local cache (no playback)."""
        if not items:
            return
        self._on_status(f"Downloading {len(items)} item(s)…")
        self.tg.download_all(
            [(it.chat_id, it.message_id, it.title, it.size) for it in items]
        )

    def _on_media_tab_changed(self, idx: int) -> None:
        self._media_kind_filter = self._media_tab_defs[idx][1]
        self._update_play_button_label()
        self._render_media(self._media_items)
        self._update_empty_states()

    def _update_play_button_label(self) -> None:
        label = self._media_tab_defs[self.media_tabs.currentIndex()][0]
        self.btn_play_media.setText("Download" if label == "Other" else "▶ Play")
        uppercase_button(self.btn_play_media)

    def _add_selected_to_playlist(self) -> None:
        items = self._selected_media()
        if not items:
            self._on_status("Select a media item first")
            return
        existing = {p.message_id for p in self.playlists.items()}
        added = 0
        for i in items:
            if i.message_id in existing:
                continue
            self.playlists.add_item(PlaylistItem(i.chat_id, i.message_id, i.title, i.duration, i.size, i.kind))
            existing.add(i.message_id)
            added += 1
        if added:
            self.playlists.save()
            self._load_playlist_view()
            self._on_status(f"Added {added} to the playlist")
        else:
            self._on_status("Already in the playlist")

    def _on_media_double(self, row, _col) -> None:
        item = self._media_item_at(row)
        if item is None:
            return
        self.playlists.set_items([PlaylistItem(item.chat_id, item.message_id, item.title,
                                        item.duration, item.size, item.kind)])
        self.playlists.save()
        self._load_playlist_view()
        if item.kind == "other":
            self._download_items([item])
        else:
            self._play(0)

    # ---------------- playlist view ----------------
    def _load_playlist_view(self) -> None:
        self.playlist_view.clear()
        for it in self.playlists.items():
            self.playlist_view.addItem(it.title)
        if len(self.playlists.items()) == 1:
            count = "1 item"
        else:
            count = f"{len(self.playlists.items())} items"
        self.playlist_count.setText(count)
        self._sync_playlist_tabs()
        self._update_empty_states()

    def _sync_playlist_tabs(self) -> None:
        """Mirror the collection's playlists/active index into the tab bar
        without retriggering the currentChanged handler."""
        names = self.playlists.names()
        if self.playlist_tabs.count() == len(names) and all(
            self.playlist_tabs.tabText(i) == n for i, n in enumerate(names)
        ):
            if self.playlist_tabs.currentIndex() != self.playlists.active:
                self.playlist_tabs.setCurrentIndex(self.playlists.active)
            return
        self.playlist_tabs.blockSignals(True)
        while self.playlist_tabs.count():
            self.playlist_tabs.removeTab(0)
        for name in names:
            self.playlist_tabs.addTab(name)
        self.playlist_tabs.blockSignals(False)
        self.playlist_tabs.setCurrentIndex(self.playlists.active)
        # Custom close button per tab (native one rendered inconsistently).
        for i in range(self.playlist_tabs.count()):
            close_btn = _TabCloseButton()
            self.playlist_tabs.setTabButton(
                i, QTabBar.ButtonPosition.RightSide, close_btn
            )
            # Capturing i is safe: every structural change (add/delete/rename/
            # switch) rebuilds all tab buttons via _sync_playlist_tabs, so the
            # captured index always matches the current tab layout.
            close_btn.clicked.connect(
                lambda _=False, i=i: self._delete_playlist(i)
            )

    def _on_playlist_tab_changed(self, index: int) -> None:
        if index < 0:
            return
        self.playlists.active = index
        self.playlists.save()
        self._load_playlist_view()

    def _new_playlist(self) -> None:
        self.playlists.new_playlist()
        self.playlists.save()
        self._sync_playlist_tabs()
        self._load_playlist_view()
        self._on_status(f"Created {self.playlists.name()}")

    def _delete_playlist(self, index: int) -> None:
        names = self.playlists.names()
        was_active = index == self.playlists.active
        removed = names[index]
        self.playlists.delete(index)
        self.playlists.save()
        self._sync_playlist_tabs()
        self._load_playlist_view()
        self._on_status(f"Deleted {removed}")

    def _rename_playlist(self, index: int) -> None:
        from PyQt6.QtWidgets import QInputDialog

        name, ok = QInputDialog.getText(
            self, "Rename playlist", "Name:", text=self.playlists.playlists[index]["name"]
        )
        if ok:
            self.playlists.rename(index, name)
            self.playlists.save()
            self._sync_playlist_tabs()
            self._load_playlist_view()

    def _clear_playlist(self) -> None:
        self.playlists.set_items([])
        self.playlists.save()
        self._load_playlist_view()

    def _clear_cache(self) -> None:
        import shutil

        cache = Config.cache_dir()
        if cache.exists():
            shutil.rmtree(cache)
        self._on_status("Download cache cleared")

    def _remove_selected(self) -> None:
        row = self.playlist_view.currentRow()
        if row < 0:
            return
        items = self.playlists.items()
        del items[row]
        self.playlists.set_items(items)
        self.playlists.save()
        self._load_playlist_view()

    # ---------------- playback ----------------
    def _play(self, start_index: int = 0) -> None:
        items = self.playlists.items()
        if not items:
            self._on_status("Playlist is empty")
            return
        if start_index >= len(items):
            start_index = 0
        # mpv's playlist is built from the first item at start_index, so a
        # start offset must slice the item list, not reorder mpv's playlist.
        self._play_base = start_index
        self._play_index = start_index
        self._download_target = None
        sub = items[start_index:]
        # Swap the UI to the player panel IMMEDIATELY: Telegram resolution can
        # take seconds, and a silent, unchanged window reads as "nothing
        # happened". The overlay explains what is going on.
        self._show_player_panel("Preparing…")
        self._on_status(f"Preparing {len(sub)} {'item' if len(sub)==1 else 'items'}…")
        self.tg.play_playlist(
            [(it.chat_id, it.message_id, it.title, it.size) for it in sub]
        )

    def _show_player_panel(self, overlay: str | None = None) -> None:
        self.media_panel.hide()
        self.player_container.show()
        self.btn_fullscreen.setEnabled(True)
        self.btn_fullscreen.setChecked(self.isFullScreen())
        QApplication.processEvents()  # map the container before mpv embeds
        if overlay is not None:
            self._show_overlay(overlay)
        else:
            self.player_overlay.hide()

    # ---------------- fullscreen ----------------
    def _enter_fullscreen_mode(self) -> None:
        """Hide everything except the player container and transport, and
        remember what to restore once fullscreen is left. mpv is embedded into
        the container's native window (--wid), so it follows the resize and
        fills the screen automatically."""
        self._fs_state = (
            self.left_panel.isVisible(),
            self.playlist_panel.isVisible(),
            self.media_panel.isVisible(),
        )
        self._fs_sizes = (self.root_splitter.sizes(), self.right_splitter.sizes())
        self.left_panel.hide()
        self.playlist_panel.hide()
        self.media_panel.hide()
        self.player_container.show()
        self.btn_fullscreen.setChecked(True)
        # Entering fullscreen (re)arms the auto-hide countdown for the
        # transport — the bar disappears after idle playback, as designed.
        if self._playing:
            self._show_controls()
            self._hide_controls_timer.start()
            self._cursor_timer.start()
        QApplication.processEvents()

    def _exit_fullscreen_mode(self) -> None:
        """Restore the panels/splitter sizes that were visible before entering
        fullscreen. The player container stays visible if it was the active
        view (playback continues in the window)."""
        state = getattr(self, "_fs_state", None)
        sizes = getattr(self, "_fs_sizes", None)
        if state is None:
            return
        left_vis, playlist_vis, media_vis = state
        self.left_panel.setVisible(left_vis)
        self.playlist_panel.setVisible(playlist_vis)
        self.media_panel.setVisible(media_vis)
        if sizes is not None:
            try:
                self.root_splitter.setSizes(sizes[0])
                self.right_splitter.setSizes(sizes[1])
            except Exception:
                pass
        self._fs_state = None
        self._fs_sizes = None
        self.btn_fullscreen.setChecked(False)
        self.setCursor(Qt.CursorShape.ArrowCursor)
        # Leaving fullscreen disables auto-hide completely: the transport must
        # stay visible while the video plays in the window.
        self._hide_controls_timer.stop()
        self._cursor_timer.stop()
        self._show_controls()
        QApplication.processEvents()

    def _toggle_fullscreen(self) -> None:
        if self.isFullScreen():
            self.showNormal()
            self._exit_fullscreen_mode()
        elif self.player_container.isVisible():
            # Fullscreen is a video-player function: only engage while the
            # player view is active (button is disabled otherwise; keep the
            # F11 shortcut / double-click consistent too).
            self._enter_fullscreen_mode()
            self.showFullScreen()

    def keyPressEvent(self, event) -> None:
        # Esc leaves fullscreen (F11 toggles via shortcut; double-click too).
        if event.key() == Qt.Key.Key_Escape and self.isFullScreen():
            self._toggle_fullscreen()
            event.accept()
            return
        super().keyPressEvent(event)

    def _show_media_panel(self) -> None:
        # Leaving the player view always drops fullscreen too, so the media
        # table is visible again after stop/error/exit.
        if self.isFullScreen():
            self.showNormal()
            self._exit_fullscreen_mode()
        self.player_container.hide()
        self.player_overlay.hide()
        self.media_panel.show()
        self.setWindowTitle("Telegram Player")
        self.btn_fullscreen.setEnabled(False)
        self.btn_fullscreen.setChecked(False)
        # Leaving the player view stops the auto-hide game entirely: transport
        # stays visible while browsing, cursor returns.
        self._playing = False
        self.btn_play.setText(_GLYPHS["play"])
        self._hide_controls_timer.stop()
        self._cursor_timer.stop()
        self._show_controls()
        self.setCursor(Qt.CursorShape.ArrowCursor)

    def _show_overlay(self, state: str, title: str | None = None) -> None:
        if not self.player_container.isVisible():
            return  # the overlay lives on the player panel only
        self.overlay_state.setText(state)
        if title is None:
            title = self._current_title()
        self.overlay_title.setText(title or "")
        self.player_overlay.show()
        self.player_overlay.raise_()

    def _current_title(self) -> str:
        if 0 <= self._play_index < len(self.playlists.items()):
            return self.playlists.items()[self._play_index].title
        return ""

    def _on_playback_failed(self, msg: str) -> None:
        self._on_status(msg)
        self.track_label.setText(msg)
        self._show_media_panel()

    def _on_playback_ready(self, entries: list) -> None:
        if not entries:
            self._show_media_panel()
            self._on_status("No playable items")
            return
        self._playlist_entries = entries  # (url, title) list — reused on self-heal respawn
        self._embed_respawned = False  # allow one self-heal respawn per playback
        self._show_player_panel("Loading…")
        # Re-read the container's native window id right before spawning mpv.
        # This is THE load-bearing step: passing a stale/orphaned wid hands mpv a
        # dead --wid and it opens its OWN window (the recurring bug).
        self.mpv.embed_wid = int(self.player_container.winId())
        if self.mpv.embed_wid == 0:
            log.warning("player container winId() returned 0 — mpv cannot embed")
        try:  # record the exact embed state alongside mpv_ctl's spawn.log
            with (self.cfg.data_dir() / "spawn.log").open("a") as sf:
                sf.write(
                    f"{time.strftime('%Y-%m-%d %H:%M:%S')} ui: container_visible="
                    f"{self.player_container.isVisible()} wid={self.mpv.embed_wid}\n"
                )
        except OSError:
            pass
        if not self.mpv.ensure_started():
            self._show_media_panel()
            self._on_status("mpv failed to start")
            return
        self.mpv.load_playlist(entries)
        self.mpv.play()  # explicit: loadfile keeps the pause state, "Play" must resume
        self._highlight_playing()
        self._on_status(f"Playing {len(entries)} {'item' if len(entries)==1 else 'items'}")
        self._ensure_current_download()
        # mpv's VO window id may not be settled at spawn; re-check shortly after
        # the first frame and self-heal a wrong embed (detached-window bug).
        # The early probe is lenient (window may not exist yet); file-loaded and
        # the 5s probe are strict: an embed was requested, so mpv MUST expose an
        # X window inside our container by then.
        QTimer.singleShot(1500, lambda: self._verify_embed_after_play(unavailable_is_ok=True))
        QTimer.singleShot(5000, lambda: self._verify_embed_after_play(unavailable_is_ok=False))

    def _on_file_loaded(self) -> None:
        """mpv's demuxer loaded a file, so its VO window exists now. This is the
        authoritative moment to prove the embed: if window-id is missing here,
        mpv picked the Wayland context and is rendering in its own window."""
        self._verify_embed_after_play(unavailable_is_ok=False)

    def _verify_embed_after_play(self, unavailable_is_ok: bool = True) -> None:
        """After the first frame, prove mpv's window is actually embedded. If it
        isn't (mpv opened its own window / Wayland context), re-read the live
        container wid and respawn once. Belt-and-suspenders behind mpv_ctl's own
        spawn-time check. `unavailable_is_ok` is forwarded to mpv_ctl so the
        early probe tolerates a not-yet-created window while later probes treat
        a missing X window as proof of a detached renderer.
        """
        if self.mpv._ipc is None or not hasattr(self.mpv, "_verify_embed"):
            return
        # Deep check: mpv's window must be a live descendant of the container in
        # the X tree (proves it's drawn inside the app, not a detached window).
        if self.mpv._verify_embed(unavailable_is_ok=unavailable_is_ok):
            return
        log.warning("embed self-check failed after play; re-reading wid and respawning")
        if getattr(self, "_embed_respawned", False):
            # Respawned once already and still mismatched: stop (no loop). The
            # fixed spawn env makes the first respawn embed correctly in the
            # common case; a second failure means a deeper problem worth logging.
            log.warning("embed still mismatched after respawn; giving up")
            return
        self._embed_respawned = True
        self.mpv.embed_wid = int(self.player_container.winId())
        try:  # record the self-heal trigger alongside spawn.log
            with (self.cfg.data_dir() / "spawn.log").open("a") as sf:
                sf.write(
                    f"{time.strftime('%Y-%m-%d %H:%M:%S')} ui: embed self-check failed; "
                    f"re-read wid={self.mpv.embed_wid}\n"
                )
        except OSError:
            pass
        # mpv_ctl._respawn_after_mismatch tears down and respawns once with the
        # freshly updated embed_wid; then we re-load the playlist in place.
        self.mpv._respawn_after_mismatch(1)
        if getattr(self, "_playlist_entries", None):
            self.mpv.load_playlist(self._playlist_entries)
            self.mpv.play()

    def _current_entries(self) -> list[tuple[str, str]]:
        return list(getattr(self, "_playlist_entries", []))

    def _ensure_current_download(self, state: dict | None = None) -> None:
        """Download exactly the item mpv is currently playing. Runs when
        playback starts and whenever mpv advances to another playlist entry."""
        if state is None:
            if self.mpv._ipc is None:
                return
            state = self.mpv.poll_state()
        posp = state.get("playlist_position")
        if posp is None or posp < 0:
            return
        idx = self._play_base + posp
        if not (0 <= idx < len(self.playlists.items())):
            return
        it = self.playlists.items()[idx]
        key = (it.chat_id, it.message_id)
        if key == self._download_target:
            return
        self._download_target = key
        self._show_overlay("Loading…", it.title)
        self.tg.download_item(it.chat_id, it.message_id, it.title, it.size)

    def _on_playlist_double(self, item) -> None:
        row = self.playlist_view.row(item)
        if row >= 0:
            self._play(row)

    def _download_all(self) -> None:
        items = self.playlists.items()
        if not items:
            self._on_status("Playlist is empty")
            return
        self._on_status(f"Downloading {len(items)} item(s)…")
        self.tg.download_all(
            [(it.chat_id, it.message_id, it.title, it.size) for it in items]
        )

    def _toggle_play(self) -> None:
        if self.mpv._ipc is None:
            self._play(0)
            return
        st = self.mpv.poll_state()
        if st.get("position") is None and st.get("playlist_position", 0) < 0:
            # mpv is running but idle (window open, nothing loaded): start the playlist.
            self._play(0)
            return
        self.mpv.toggle()
        self._poll()

    def _stop(self) -> None:
        self.mpv.stop()
        self.track_label.setText("Stopped")
        self._play_index = -1
        self._play_base = 0
        self._download_target = None
        self._highlight_playing()
        self._show_media_panel()

    def _on_seek_released(self) -> None:
        frac = self.seek.value() / 1000.0
        state = self.mpv.poll_state()
        if state.get("duration"):
            self.mpv.seek(frac * state["duration"])

    def _on_volume(self, v: int) -> None:
        self.cfg.volume = v
        self.mpv.set_volume(v)
        self.cfg.save()

    # ---------------- keybindings ----------------
    def _setup_player_keys(self) -> None:
        """Player-style keyboard controls (Space, arrows, [ ] speed, …). An
        app-level event filter dispatches them BEFORE the focused widget sees
        the key, so they work no matter what has focus — while the player view
        is active only (`_player_keys_active`), which keeps typing in the
        search fields and list navigation fully intact whenever the video view
        is not the current panel."""
        self._player_keys_filter = _PlayerKeysFilter(self._on_player_key, self)
        QApplication.instance().installEventFilter(self._player_keys_filter)

    def _player_keys_active(self) -> bool:
        if QApplication.activeModalWidget() is not None:
            return False  # dialogs own their keys
        if not self.player_container.isVisible():
            return False  # browsing mode: keys belong to the lists/filters
        w = QApplication.focusWidget()
        if isinstance(w, (QLineEdit, QComboBox)):
            return False  # never steal typing
        return True

    def _on_player_key(self, event) -> bool:
        """Dispatch a keypress. Returns True when the app consumed it."""
        if not self._player_keys_active():
            return False
        key = event.key()
        mods = event.modifiers()
        ctrl = mods & Qt.KeyboardModifier.ControlModifier
        alt = mods & Qt.KeyboardModifier.AltModifier
        shift = mods & Qt.KeyboardModifier.ShiftModifier
        if ctrl or alt:
            return False
        if key in (Qt.Key.Key_Space, Qt.Key.Key_K, Qt.Key.Key_P):
            self._toggle_play()
            return True
        if key == Qt.Key.Key_Left:
            self.mpv.seek_relative(-1.0 if shift else -5.0)
            return True
        if key == Qt.Key.Key_Right:
            self.mpv.seek_relative(1.0 if shift else 5.0)
            return True
        if key == Qt.Key.Key_Up:
            self.mpv.seek_relative(60.0)
            return True
        if key == Qt.Key.Key_Down:
            self.mpv.seek_relative(-60.0)
            return True
        if key == Qt.Key.Key_Comma:
            self.mpv.seek_relative(-1.0)
            return True
        if key == Qt.Key.Key_Period:
            self.mpv.seek_relative(1.0)
            return True
        if key == Qt.Key.Key_9:
            self._kb_volume(-5)
            return True
        if key == Qt.Key.Key_0:
            self._kb_volume(5)
            return True
        if key == Qt.Key.Key_M:
            self._toggle_mute()
            return True
        if key == Qt.Key.Key_F:
            self._toggle_fullscreen()
            return True
        if key == Qt.Key.Key_BracketLeft:
            self._kb_speed(-0.1)
            return True
        if key == Qt.Key.Key_BracketRight:
            self._kb_speed(0.1)
            return True
        if key == Qt.Key.Key_BraceLeft:
            self._kb_speed_mult(0.5)
            return True
        if key == Qt.Key.Key_BraceRight:
            self._kb_speed_mult(2.0)
            return True
        if key == Qt.Key.Key_Backspace:
            self._kb_speed_reset()
            return True
        return False

    # ---------------- keyboard actions ----------------
    def _kb_volume(self, delta: int) -> None:
        self._on_volume(max(0, min(100, self.volume.value() + delta)))

    def _toggle_mute(self) -> None:
        cur = self.volume.value()
        if cur > 0:
            self._prev_volume = cur
            self.volume.setValue(0)
        else:
            self.volume.setValue(self._prev_volume if self._prev_volume else 80)
            self._prev_volume = None

    def _kb_speed(self, delta: float) -> None:
        self._set_speed(round(self.cfg.speed + delta, 3))

    def _kb_speed_mult(self, factor: float) -> None:
        self._set_speed(round(self.cfg.speed * factor, 3))

    def _kb_speed_reset(self) -> None:
        self._set_speed(1.0)

    def _on_speed_selected(self, action) -> None:
        self._set_speed(float(action.data()))

    def _set_speed(self, factor: float) -> None:
        """Apply + persist a speed change. Persisting to config AND keeping the
        mpv `speed` property set means the whole playlist (and later sessions)
        keeps playing at the chosen rate."""
        factor = max(0.1, min(10.0, round(float(factor), 3)))
        if factor == self.cfg.speed:
            return
        self.cfg.speed = factor
        self.cfg.save()
        self.btn_speed.setText(f"{factor:g}×")
        self._sync_speed_menu_checks()
        try:
            self.mpv.set_speed(factor)
        except Exception:
            pass  # mpv not running yet — the --speed spawn arg applies it

    # ---------------- fading controls ----------------
    def _setup_fading_controls(self) -> None:
        """Auto-hide the transport bar after a pause in user activity while the
        video is actually playing; any mouse move/click/wheel or keypress brings
        it back (faded in/out, not an abrupt toggle)."""
        self.transport_effect = QGraphicsOpacityEffect(self.transport)
        self.transport.setGraphicsEffect(self.transport_effect)
        self._hide_controls_timer = QTimer(self)
        self._hide_controls_timer.setInterval(3500)
        self._hide_controls_timer.setSingleShot(True)
        self._hide_controls_timer.timeout.connect(self._fade_controls_out)
        self._activity_filter = _ActivityFilter(self._on_user_activity, self)
        QApplication.instance().installEventFilter(self._activity_filter)
        # The video covers the whole window (in fullscreen this is the whole
        # screen) and is a FOREIGN X child window (mpv --wid): mouse events
        # over it go to mpv, never to Qt — so no event wakes the controls.
        # Poll the global cursor: hovering the bottom band where the transport
        # lives brings the bar (and keyboard focus) back.
        self._cursor_timer = QTimer(self)
        self._cursor_timer.setInterval(200)
        self._cursor_timer.timeout.connect(self._on_cursor_tick)
        for w in (self, self.centralWidget(), self.player_container,
                  self.transport, self.left_panel, self.right_splitter):
            w.setMouseTracking(True)

    def _global_cursor_pos(self):
        return QCursor.pos()

    def _cursor_in_band(self, pos) -> bool:
        """Is the (window-local) point inside the restore band at the bottom of
        the window, where the transport bar lives?"""
        band = max(90, self.transport.height() + 70)
        return pos.y() >= self.height() - band

    def _on_cursor_tick(self) -> None:
        if not self.isFullScreen() or not self._playing:
            return
        pos = self.mapFromGlobal(self._global_cursor_pos())
        if self._cursor_in_band(pos):
            # Hovering the control strip wakes the bar even though the mouse
            # sits over the foreign video window.
            self._on_user_activity()
        elif self._controls_hidden and self.isFullScreen():
            pass  # cursor parked over the video: keep the bar hidden

    def _on_user_activity(self) -> None:
        if not self._playing:
            return
        # In fullscreen, clicking the embedded video may hand the X keyboard
        # focus to mpv's foreign child window, so Qt would never see Esc/Space
        # again. Reclaim focus only then: in windowed mode the app must NOT
        # yank focus (or raise itself over) the user's other windows.
        if (self.isFullScreen()
                and QApplication.activeModalWidget() is None
                and not self.isActiveWindow()):
            self.activateWindow()
            self.setFocus()
        self._show_controls()
        # Only fullscreen auto-hides the transport; in windowed mode activity
        # just makes sure the bar is up (no idle countdown machinery).
        if self.isFullScreen():
            self._hide_controls_timer.start()

    def _show_controls(self) -> None:
        if not self._controls_hidden:
            return
        self._controls_hidden = False
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.transport.show()
        self._animate_transport(1.0)

    def _fade_controls_out(self) -> None:
        if not self.isFullScreen() or not self._playing or self._controls_hidden:
            return
        self._controls_hidden = True
        if self.isFullScreen():
            self.setCursor(Qt.CursorShape.BlankCursor)
        self._animate_transport(0.0)

    def _animate_transport(self, target: float) -> None:
        if getattr(self, "_transport_anim", None) is not None:
            self._transport_anim.stop()
        self._transport_anim = QPropertyAnimation(
            self.transport_effect, b"opacity", self
        )
        self._transport_anim.setDuration(260)
        self._transport_anim.setStartValue(self.transport_effect.opacity())
        self._transport_anim.setEndValue(target)
        self._transport_anim.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self._transport_anim.finished.connect(
            lambda: self.transport.hide() if self._controls_hidden else None
        )
        self._transport_anim.start()

    def _on_playing_changed(self, playing: bool) -> None:
        if playing:
            self._show_controls()
            # Auto-hiding is a fullscreen-only behavior: in the windowed player
            # the transport stays put (a video player's controls are expected
            # to always be reachable unless the video fills the screen).
            if self.isFullScreen():
                self._hide_controls_timer.start()
                self._cursor_timer.start()
        else:
            self._hide_controls_timer.stop()
            self._cursor_timer.stop()
            self._show_controls()

    # ---------------- mpv state ----------------
    def _poll(self) -> None:
        if not self.mpv._ipc:
            return
        state = self.mpv.poll_state()
        dur = state.get("duration") or 0
        pos = state.get("position") or 0
        self.time_label.setText(f"{_fmt(pos)} / {_fmt(dur)}")
        if dur and not self.seek.isSliderDown():
            # Don't fight a user who is tapping/dragging the seek bar.
            self.seek.blockSignals(True)
            self.seek.setValue(int((pos / dur) * 1000))
            self.seek.blockSignals(False)
        if state.get("title"):
            self.track_label.setText(state["title"])
            if self.player_container.isVisible():
                self.setWindowTitle(f"{state['title']} — Telegram Player")
        # Show the right glyph on the circular transport button — only when we
        # actually have live paused/playing state (mpv reports it as "paused").
        pause = state.get("paused")
        if pause is None:
            pause = state.get("pause")
        if pause is not None:
            self.btn_play.setText(
                _GLYPHS["play"] if bool(pause) else _GLYPHS["pause"]
            )
        # Once mpv knows the duration, its demuxer probe finished and video is
        # (about to be) rendered — the overlay has served its purpose.
        if dur:
            self.player_overlay.hide()
        # Playing state drives the auto-hiding transport: keep the bar visible
        # while paused, fade it when the video actually runs and the user goes
        # idle. seek.blockSignals above means the slider is never `pressed`.
        playing = bool(dur) and not bool(state.get("pause"))
        if playing != self._playing:
            self._playing = playing
            self._on_playing_changed(playing)
    # ---------------- close ----------------
    def closeEvent(self, event) -> None:
        # Unregister the app-level event filters NOW: QApplication outlives
        # this window, and a stale filter would call back into the dying
        # window (segfault) whenever the next event arrives.
        app = QApplication.instance()
        if app is not None:
            for flt in (getattr(self, "_activity_filter", None),
                        getattr(self, "_player_keys_filter", None)):
                if flt is not None:
                    app.removeEventFilter(flt)
        self._timer.stop()
        self.mpv.shutdown()
        # Block until mpv is actually gone so it never survives as a detached,
        # orphaned X window after the app exits (a trigger of the detached-window bug).
        self.mpv.wait_stopped(timeout=5.0)
        self.tg.stop()
        self.playlists.save()
        self.cfg.save()
        super().closeEvent(event)

    def _on_mpv_state(self, state: dict) -> None:
        self._poll()

    def _on_mpv_exited(self) -> None:
        self.track_label.setText("mpv stopped")
        self.btn_play.setText(_GLYPHS["play"])
        self._play_index = -1
        self._play_base = 0
        self._download_target = None
        self._highlight_playing()
        self._show_media_panel()

    def _highlight_playing(self) -> None:
        for r in range(self.playlist_view.count()):
            item = self.playlist_view.item(r)
            item.setSelected(False)
            item.setData(1, r)
        if 0 <= self._play_index < self.playlist_view.count():
            it = self.playlist_view.item(self._play_index)
            it.setSelected(True)
            self.playlist_view.scrollToItem(it)
