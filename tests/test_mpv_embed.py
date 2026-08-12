"""Tests for mpv embedding self-verification (Step 10).

Covers the X11 descendant helper and the MpvCtl embed-verify/respawn logic
without requiring a real mpv process: a fake MpvIpc answers `get_property
window-id`, and a fake subprocess.Popen lets us assert the respawn path.
"""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from telegram_player.config import Config
from telegram_player.mpv_ctl import MpvCtl, _x_window_is_descendant


def _make_ctl(embed_wid: int | None = 1234, binary: str | None = "/bin/true"):
    cfg = Config()
    cfg.data_dir().mkdir(parents=True, exist_ok=True)
    ctl = MpvCtl(binary, cfg, embed_wid=embed_wid)
    return ctl


def test_descendant_helper_no_display_safe(monkeypatch):
    # No X display available in the test sandbox -> helper must be conservative
    # (never kill a good embed). Force the loader to fail.
    monkeypatch.setattr("telegram_player.mpv_ctl._load_x11", lambda: None)
    # child==ancestor trivially true; orphaned ids also return True (safe).
    assert _x_window_is_descendant(0x10, 0x10) is True
    assert _x_window_is_descendant(0x10, 0x20) is True


def test_descendant_helper_live_display():
    # On a machine with X (the agent's XWayland session) this must actually
    # walk the tree. Use a real window id from the display, not 0, and bound
    # the work so a pathological reply can't hang the suite.
    import ctypes
    import ctypes.util
    import os

    if not os.environ.get("DISPLAY"):
        pytest.skip("no X display")
    # Walking the live X window tree is only safe on a real, interactive
    # display. Under headless CI (xvfb + software GL) it can segfault, so skip.
    if os.environ.get("GITHUB_ACTIONS") or os.environ.get("LIBGL_ALWAYS_SOFTWARE") == "1":
        pytest.skip("live X11 tree walk is unreliable under headless CI")
    lib = ctypes.CDLL(ctypes.util.find_library("X11") or "libX11.so.6")
    lib.XOpenDisplay.restype = ctypes.c_void_p
    dpy = lib.XOpenDisplay(None)
    if not dpy:
        pytest.skip("cannot open X display")
    try:
        root = lib.XDefaultRootWindow(dpy)
        # A window is a descendant of itself.
        assert _x_window_is_descendant(int(root), int(root)) is True
        # A definitely-orphaned id is not a descendant of the root.
        assert _x_window_is_descendant(0x7FFFFFFF, int(root)) is False
    finally:
        lib.XCloseDisplay(dpy)


def test_verify_embed_true_when_window_id_matches():
    ctl = _make_ctl(embed_wid=0x1000016)
    fake_ipc = MagicMock()
    fake_ipc.command.return_value = "16777238"  # == 0x1000016
    ctl._ipc = fake_ipc
    with patch("telegram_player.mpv_ctl._x_window_is_descendant", return_value=True):
        assert ctl._verify_embed() is True


def test_verify_embed_true_when_no_embed_requested():
    ctl = _make_ctl(embed_wid=None)
    ctl._ipc = MagicMock()
    # No embed requested -> nothing to verify.
    assert ctl._verify_embed() is True


def test_verify_embed_false_when_window_id_mismatch():
    ctl = _make_ctl(embed_wid=0x1000016)
    fake_ipc = MagicMock()
    # mpv reports a DIFFERENT window id -> it opened its own window.
    fake_ipc.command.return_value = "99999999"
    ctl._ipc = fake_ipc
    with patch("telegram_player.mpv_ctl._x_window_is_descendant", return_value=False):
        assert ctl._verify_embed() is False


def test_verify_embed_true_on_query_failure():
    ctl = _make_ctl(embed_wid=0x1000016)
    fake_ipc = MagicMock()
    fake_ipc.command.side_effect = RuntimeError("not ready")
    ctl._ipc = fake_ipc
    # Can't query yet -> treat as OK (UI re-checks after file-loaded).
    assert ctl._verify_embed() is True


def test_respawn_after_mismatch_tears_down_and_respawns():
    # Exercise the REAL _respawn_after_mismatch: it must teardown and call the
    # real _spawn once with retries-1. No real mpv — fake Popen + fake IPC, and
    # make the IPC socket file "appear" so _spawn's connect succeeds.
    ctl = _make_ctl(embed_wid=0x1000016, binary="/bin/true")

    fake_proc = MagicMock()
    fake_ipc = MagicMock()

    with patch("telegram_player.mpv_ctl.subprocess.Popen", return_value=fake_proc), patch(
        "telegram_player.mpv_ctl.socket.socket"
    ) as sock, patch(
        "telegram_player.mpv_ctl.MpvIpc", return_value=fake_ipc
    ) as ipc_cls, patch.object(ctl, "_teardown") as teardown, patch(
        "telegram_player.mpv_ctl.Path.exists", return_value=True
    ):
        sock.return_value.connect.return_value = None
        # _respawn_after_mismatch(1) -> _spawn(0). The real _spawn builds argv,
        # starts the fake proc, connects the fake socket, creates the fake IPC,
        # then verifies (patched True so it doesn't recurse further).
        with patch.object(ctl, "_verify_embed", return_value=True):
            result = ctl._respawn_after_mismatch(1)
        assert result is True
        teardown.assert_called_once()
        ipc_cls.assert_called()  # a fresh MpvIpc was created on respawn


def test_spawn_args_hide_mpv_chrome_and_apply_speed():
    """The embedded mpv must run without its own OSD/on-screen controller, and
    must start with the persisted playlist speed."""
    ctl = _make_ctl(binary="/bin/true")
    ctl.cfg.speed = 1.5
    fake_proc = MagicMock()
    fake_ipc = MagicMock()
    captured = {}

    def fake_popen(*args, **kwargs):
        captured["args"] = args[0]
        return fake_proc

    with patch("telegram_player.mpv_ctl.subprocess.Popen", side_effect=fake_popen), patch(
        "telegram_player.mpv_ctl.socket.socket"
    ) as sock, patch("telegram_player.mpv_ctl.MpvIpc", return_value=fake_ipc), patch(
        "telegram_player.mpv_ctl.Path.exists", return_value=True
    ), patch.object(ctl, "_verify_embed", return_value=True):
        sock.return_value.connect.return_value = None
        ctl._spawn(0)
    args = captured["args"]
    assert "--no-osc" in args          # app provides its own transport bar
    assert "--osd-level=1" in args     # no volume/seek popups, keep forced text
    assert "--speed=1.500" in args     # playlist-locked speed from the first frame
    # mpv must never grab input away from the app (its embedded window or a
    # terminal): it is a pure renderer, the app owns all keys/mouse.
    assert "--input-vo-keyboard=no" in args
    assert "--input-default-bindings=no" in args
    assert "--input-terminal=no" in args


def test_set_speed_and_seek_relative_commands():
    ctl = _make_ctl()
    fake_ipc = MagicMock()
    ctl._ipc = fake_ipc
    ctl.set_speed(1.5)
    fake_ipc.command.assert_called_with("set", "speed", "1.500", timeout=10)
    ctl.set_speed(99.0)  # clamped
    fake_ipc.command.assert_called_with("set", "speed", "10.000", timeout=10)
    ctl.seek_relative(-5)
    fake_ipc.command.assert_called_with("seek", "-5.000", "relative", timeout=10)
    ctl.seek_relative(30)
    fake_ipc.command.assert_called_with("seek", "+30.000", "relative", timeout=10)


def test_ensure_started_self_heals_exactly_once():
    """The self-heal trigger is _respawn_after_mismatch (called by the UI's
    post-frame check). It must respawn exactly once and then stop (no loop),
    even if the embed keeps mismatching.
    """
    ctl = _make_ctl(embed_wid=0x1000016, binary="/bin/true")
    fake_proc = MagicMock()
    fake_ipc = MagicMock()

    spawn_calls = []
    real_spawn = ctl._spawn

    def tracing_spawn(retries=0):
        spawn_calls.append(retries)
        return real_spawn(retries)

    with patch("telegram_player.mpv_ctl.subprocess.Popen", return_value=fake_proc), patch(
        "telegram_player.mpv_ctl.socket.socket"
    ) as sock, patch(
        "telegram_player.mpv_ctl.MpvIpc", return_value=fake_ipc
    ), patch.object(ctl, "_teardown"), patch(
        "telegram_player.mpv_ctl.Path.exists", return_value=True
    ), patch.object(ctl, "_verify_embed", return_value=False), patch.object(
        ctl, "_spawn", side_effect=tracing_spawn
    ) as spawn:
        sock.return_value.connect.return_value = None
        # UI calls _respawn_after_mismatch(1) on a detected mismatch.
        # It tears down, spawns with retries=0; the re-spawn's verify also fails
        # but retries==0 so it stops (no further respawn).
        result = ctl._respawn_after_mismatch(1)
        assert result is True
        # Exactly one respawn (retries=0); then it stops.
        assert spawn.call_count == 1
        assert spawn_calls == [0]








# ---------------- auto-advance on EOF ----------------
def test_eof_event_advances_playlist():
    """When a video ends normally, mpv (--keep-open) pauses at the last frame;
    the app drives the playlist forward itself via playlist-next weak."""
    ctl = _make_ctl()
    fake = MagicMock()
    ctl._ipc = fake
    ctl._advance_on_eof()
    fake.command.assert_called_once_with("playlist-next", "weak", timeout=2)


def test_eof_advance_ignored_without_ipc():
    ctl = _make_ctl()
    ctl._ipc = None
    ctl._advance_on_eof()  # must not raise


# ---------------- live pause state events ----------------
def test_pause_property_change_refreshes_state():
    """on observe_property, mpv pushes property-change events for `pause`;
    the ctl must forward a fresh poll so the transport glyph updates."""
    from PyQt6.QtCore import Qt

    ctl = _make_ctl()
    fake = MagicMock()
    props = {"time-pos": 12.0, "duration": 60.0, "pause": True,
             "media-title": "T", "playlist-pos": 0, "playlist-count": 2}
    fake.command.side_effect = lambda *a, **k: props.get(a[1]) if len(a) > 1 else None
    ctl._ipc = fake
    got = []
    ctl.state_changed.connect(got.append, Qt.ConnectionType.DirectConnection)
    ctl._on_event({"event": "property-change", "name": "pause", "data": True})
    import time

    t0 = time.time()
    while time.time() - t0 < 2 and not got:
        time.sleep(0.02)
    assert got and got[0].get("paused") is True
    assert got[0].get("title") == "T"
