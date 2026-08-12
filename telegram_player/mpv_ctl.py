"""Control of the bundled standalone mpv via JSON IPC over a unix socket.

mpv is spawned as an unrelated process with an isolated `--config-dir` (the
host's ~/.config/mpv on this machine is broken and must never be read) and a
HUD-visible title per track (force-media-title). Playback targets are localhost
Local files downloaded into the cache dir.
"""

from __future__ import annotations

import concurrent.futures
import itertools
import json
import ctypes
import ctypes.util
import logging
import os
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from PyQt6.QtCore import QObject, pyqtSignal as Signal

from .config import Config



# --- X11 window-tree helpers (ctypes over libX11; no extra dependency) ---
# mpv embeds into the player container via --wid. If that wid is orphaned (Qt
# destroyed/recreated the native window) mpv opens its own top-level window.
# These helpers let us prove the embed landed at runtime and self-heal it.
_X11 = None


def _load_x11():
    global _X11
    if _X11 is not None:
        return _X11
    try:
        lib = ctypes.CDLL(ctypes.util.find_library("X11") or "libX11.so.6")
    except OSError:
        return None
    lib.XOpenDisplay.restype = ctypes.c_void_p
    lib.XOpenDisplay.argtypes = [ctypes.c_char_p]
    lib.XQueryTree.restype = ctypes.c_int
    lib.XQueryTree.argtypes = [
        ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong),
        ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.POINTER(ctypes.c_ulong)),
        ctypes.POINTER(ctypes.c_uint),
    ]
    lib.XFree.restype = None
    lib.XFree.argtypes = [ctypes.c_void_p]
    lib.XCloseDisplay.restype = None
    lib.XCloseDisplay.argtypes = [ctypes.c_void_p]
    # Swallow X errors (e.g. BadWindow for an orphaned/long-gone id) instead of
    # letting libX11's default handler abort the whole process.
    XERRORFUNC = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
    lib._xerr_dummy = XERRORFUNC(lambda *_a: 0)  # keep a reference alive
    lib.XSetErrorHandler.restype = ctypes.c_void_p
    lib.XSetErrorHandler.argtypes = [XERRORFUNC]
    lib.XSetErrorHandler(lib._xerr_dummy)
    _X11 = lib
    return _X11


def _x_window_is_descendant(child: int, ancestor: int) -> bool:
    """True if `child` is `ancestor` or lives in its ancestor chain.

    Returns True on any X error / no-display (conservative: never kill a
    perfectly good embed just because the X server is unprobeable).
    """
    if not child or not ancestor:
        return False
    lib = _load_x11()
    if lib is None:
        return True
    dpy = lib.XOpenDisplay(None)
    if not dpy:
        return True

    def ancestors(wid: int) -> set[int]:
        out: set[int] = set()
        seen: set[int] = set()
        cur = wid
        # Walk up at most 64 levels to avoid any pathological loop.
        for _ in range(64):
            if not cur or cur in seen:
                break
            seen.add(cur)
            out.add(cur)
            root = ctypes.c_ulong()
            parent = ctypes.c_ulong()
            children = ctypes.POINTER(ctypes.c_ulong)()
            nchildren = ctypes.c_uint()
            status = lib.XQueryTree(
                dpy, ctypes.c_ulong(cur), ctypes.byref(root),
                ctypes.byref(parent), ctypes.byref(children), ctypes.byref(nchildren),
            )
            if not status:
                break
            p = int(parent.value)
            if children:
                try:
                    lib.XFree(children)
                except Exception:
                    pass
            if p == cur or p == 0:
                break
            cur = p
        return out

    try:
        chain = ancestors(child)
    finally:
        try:
            lib.XCloseDisplay(dpy)
        except Exception:
            pass
    return ancestor in chain


log = logging.getLogger(__name__)


class MpvIpc:
    """Line-JSON IPC client for mpv's --input-ipc-server."""

    def __init__(self, sock: socket.socket, on_event=None):
        self._sock = sock
        self._buf = b""
        self._futures: dict[int, concurrent.futures.Future] = {}
        self._lock = threading.Lock()
        self._next_id = itertools.count()
        self.on_event = on_event
        self._closed = False
        threading.Thread(target=self._read_loop, name="mpv-ipc", daemon=True).start()

    def _read_loop(self) -> None:
        try:
            while True:
                data = self._sock.recv(65536)
                if not data:
                    break
                self._buf += data
                while b"\n" in self._buf:
                    line, self._buf = self._buf.split(b"\n", 1)
                    self._handle_line(line)
        except OSError:
            pass
        finally:
            self._fail_all()
            if self.on_event:
                try:
                    self.on_event({"event": "socket-closed"})
                except Exception:
                    pass

    def _fail_all(self) -> None:
        with self._lock:
            futures = list(self._futures.values())
            self._futures.clear()
        for fut in futures:
            if not fut.done():
                fut.set_exception(OSError("mpv IPC socket closed"))

    def _handle_line(self, line: bytes) -> None:
        if not line.strip():
            return
        try:
            obj = json.loads(line)
        except ValueError:
            return
        rid = obj.get("request_id")
        if rid is not None:
            with self._lock:
                fut = self._futures.pop(rid, None)
            if fut is not None and not fut.done():
                err = obj.get("error")
                if err and err != "success":
                    fut.set_exception(RuntimeError(str(err)))
                else:
                    fut.set_result(obj.get("data"))
            return
        if self.on_event is not None and "event" in obj:
            try:
                self.on_event(obj)
            except Exception:
                log.exception("mpv event callback failed")

    def command(self, *args, timeout: float = 10.0):
        with self._lock:
            rid = next(self._next_id)
            fut: concurrent.futures.Future = concurrent.futures.Future()
            self._futures[rid] = fut
            self._sock.sendall(
                (json.dumps({"command": list(args), "request_id": rid}) + "\n").encode()
            )
        try:
            return fut.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            with self._lock:
                self._futures.pop(rid, None)
            raise TimeoutError(f"mpv command timed out: {args!r}")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._sock.close()
        except OSError:
            pass


class MpvCtl(QObject):
    state_changed = Signal(dict)
    mpv_exited = Signal()
    file_loaded = Signal()  # demuxer loaded a file: VO window exists, verify the embed now

    def __init__(self, mpv_binary: str | None, cfg: Config, embed_wid: int | None = None):
        super().__init__()
        self._binary = mpv_binary
        self.cfg = cfg
        self.embed_wid: int | None = embed_wid  # X11 window id to draw into (--wid)
        runtime = Path(os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir())
        self._sock_path = runtime / "telegram-player" / "mpv.sock"
        self._proc: subprocess.Popen | None = None
        self._ipc: MpvIpc | None = None
        self._start_lock = threading.Lock()
        self._closing = False

    # ---------------- lifecycle ----------------

    def ensure_started(self) -> bool:
        with self._start_lock:
            if self._proc is not None and self._proc.poll() is None and self._ipc is not None:
                return True
            # Before spawning, make sure the container wid we were handed is still
            # a live X window. If it was orphaned (Qt destroyed/recreated the
            # native window), re-read it now so we never hand mpv a dead --wid.
            if self.embed_wid is not None:
                cur = int(self.embed_wid)
                if not _x_window_is_descendant(cur, cur):  # False => orphaned/bad window
                    log.warning("embed wid 0x%x is no longer live; cannot verify", cur)
            return self._spawn(retries=1)

    def _spawn(self, retries: int = 0) -> bool:
        if not self._binary:
            log.warning("no mpv binary")
            self.mpv_exited.emit()
            return False
        self._sock_path.parent.mkdir(parents=True, exist_ok=True)
        # A previous app run may have crashed before its teardown, leaving an
        # orphan mpv that still owns this socket and keeps its own window on
        # screen. Quit it first or the fresh mpv cannot bind the IPC socket and
        # we get a second, uncontrollable, detached window.
        self._retire_stale_mpv()
        try:
            self._sock_path.unlink()
        except OSError:
            pass
        if self._proc is not None and self._proc.poll() is None:
            # Defensive: never leave a live mpv behind when respawning.
            try:
                self._proc.terminate()
                self._proc.wait(timeout=3)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            self._proc = None
        cmd = [
            self._binary,
            f"--config-dir={self.cfg.mpv_config_dir}",  # MUST be first: bypass broken host config
        ]
        if self.embed_wid is not None:
            # Embed into the app's player container (X11 window id). Requires
            # the Qt app to run on the xcb platform (set in app.main).
            cmd.append(f"--wid={self.embed_wid}")
        cmd += [
            "--force-window=yes",
            f"--input-ipc-server={self._sock_path}",
            "--keep-open=yes",
            "--cache=auto",
            "--demuxer-max-bytes=128MiB",
            f"--volume={self.cfg.volume}",
            "--idle=yes",
            # Capture diagnostics into the mpv.log file: mpv silences terminal
            # output when stderr is not a tty, so force it + status level.
            "--terminal=yes",
            "--msg-level=all=status",
            # gpu-next defaults to the Vulkan backend; on machines where Vulkan
            # swapchain creation fails the VO "initializes" but never presents a
            # frame (audio-only, dead window). GL + software fallbacks always
            # present. gpu-api applies to the gpu VO only; wlshm/x11 are fallbacks.
            "--vo=gpu,wlshm,x11",
            "--gpu-api=opengl",
            # Deterministic X11 embedding. On a Wayland session mpv's gpu
            # auto-detect picks the Wayland context, which IGNORES --wid and
            # opens its own window (the detached-window bug). The bundled mpv
            # has no GLX context, so x11egl is its X11/OpenGL embedding path.
            "--gpu-context=x11egl",
            "--hwdec=auto",
            # The app provides its own transport: keep mpv's built-in on-screen
            # controller and OSD out of the embedded frame. osd-level=1 still
            # honors explicit show-text messages (playback-error skip notice).
            "--no-osc",
            "--osd-level=1",
            # The app owns ALL input: mpv must not grab keys/mouse from its
            # embedded X window (or from the terminal) — after clicking the
            # video its foreign child window could otherwise own the keyboard
            # focus and swallow Esc/Space before Qt ever sees them.
            "--input-vo-keyboard=no",
            "--input-default-bindings=no",
            "--input-terminal=no",
            # Persisted playback speed applies from the very first frame and
            # stays set for every subsequent playlist entry in this instance.
            f"--speed={self.cfg.speed:.3f}",
            *self.cfg.mpv_extra_args,
        ]
        log_path = self.cfg.data_dir() / "mpv.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        # Append-only spawn diagnostics (mpv.log is truncated per spawn, so
        # keep a cumulative record of the exact argv + wid for live debugging).
        try:
            with (self.cfg.data_dir() / "spawn.log").open("a") as sf:
                sf.write(
                    f"{time.strftime('%Y-%m-%d %H:%M:%S')} wid={self.embed_wid} "
                    f"argv={' '.join(str(a) for a in cmd)}\n"
                )
        except OSError:
            pass
        logf = open(log_path, "wb")  # truncate: one fresh diagnostic log per spawn
        try:
            self._proc = subprocess.Popen(
                cmd, stdout=logf, stderr=logf, env=self._spawn_env()
            )
        except OSError as e:
            logf.close()
            log.warning("failed to start mpv: %s", e)
            self.mpv_exited.emit()
            return False
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if self._sock_path.exists():
                break
            time.sleep(0.05)
        if not self._sock_path.exists():
            log.warning("mpv IPC socket never appeared")
            self._teardown(silent=True)
            self.mpv_exited.emit()
            return False
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.connect(str(self._sock_path))
        except OSError as e:
            log.warning("mpv socket connect failed: %s", e)
            self._teardown(silent=True)
            self.mpv_exited.emit()
            return False
        self._ipc = MpvIpc(s, on_event=self._on_event)
        # Ask mpv to push pause changes as property-change events so the
        # transport glyph follows the real state instantly (the UI only polls
        # on other events, which would otherwise leave the button stale).
        try:
            self._ipc.command("observe_property", 1, "pause", timeout=5)
        except Exception:
            pass  # older mpv: glyph updates arrive with the next poll
        # The embed is verified AFTER the first frame by the UI (see
        # MainWindow._verify_embed_after_play): at spawn the VO window isn't in
        # the X tree yet, so a spawn-time descendant check is racy. The UI timer
        # runs a deep descendant check and self-heals a real mismatch exactly once.
        return True

    @staticmethod
    def _spawn_env() -> dict:
        """Environment for the mpv child: ours minus Wayland.

        mpv embeds into an X11 window id (--wid), which only works through the
        X11 GPU context. On a Wayland session mpv's ``auto`` context detection
        picks the Wayland context first and then IGNORES --wid, so the video
        renders in mpv's own top-level window instead of the app. Removing
        WAYLAND_DISPLAY leaves DISPLAY as the only display mpv can see, and
        auto-detection then lands on x11egl (see --gpu-context below).
        """
        env = dict(os.environ)
        env.pop("WAYLAND_DISPLAY", None)
        env.pop("XDG_SESSION_TYPE", None)
        return env

    def _retire_stale_mpv(self) -> None:
        """Ask any mpv left over from a previous app run to quit.

        If the previous run crashed (or was killed) before its teardown, that
        orphan keeps its own window on screen and still owns the IPC socket
        path; a fresh mpv would then be unable to bind the socket, leaving us
        with a second, uncontrollable, detached window. Talking to the old
        instance over its socket and waiting (bounded) for it to release the
        listener removes that window and frees the path.
        """
        if not self._sock_path.exists():
            return
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(1.0)
            s.connect(str(self._sock_path))
            s.sendall(b'{"command":["quit"]}\n')
            s.close()
        except OSError:
            return  # stale file only, no live server: safe to unlink below
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            try:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.settimeout(0.2)
                s.connect(str(self._sock_path))
                s.close()
                time.sleep(0.05)
            except OSError:
                return  # listener gone: the old mpv exited

    def _verify_embed(self, unavailable_is_ok: bool = True) -> bool:
        """Post-frame embed check: mpv's VO window must be a live descendant of
        the container wid in the X tree.

        mpv reports its own VO window id via `get_property window-id`; when
        embedded with --wid=<container>, that window is a CHILD of <container>,
        so the id differs from --wid but is a descendant of it. A detached mpv
        (the recurring bug) reports a top-level window that is NOT a descendant.
        Returns True if unverifiable (query/X failure) so we never kill a good
        embed on a transient error.

        `unavailable_is_ok=True` treats a missing window-id property (mpv has
        no X window at all) as "VO window not created yet" - fine for an early
        probe. `unavailable_is_ok=False` treats it as a mismatch: an embed was
        requested (embed_wid set) but mpv is rendering somewhere that exposes
        no X id - exactly what happens when mpv picked the Wayland context and
        ignores --wid (the detached-window bug).
        """
        if self._ipc is None or self.embed_wid is None:
            return True  # nothing to verify (audio-only / no embed requested)
        try:
            wid = self._ipc.command("get_property", "window-id", timeout=10)
        except RuntimeError:
            # "property unavailable": the active VO has no X window id, so with
            # an embed requested mpv is NOT drawing into our container.
            return unavailable_is_ok
        except Exception:
            return True  # IPC hiccup / mpv busy; re-check later
        if wid is None:
            return unavailable_is_ok
        wid = int(wid)
        if wid == int(self.embed_wid):
            return True  # mpv is drawing directly into the container wid
        # Otherwise the reported window must be nested inside the container.
        return _x_window_is_descendant(wid, int(self.embed_wid))


    def _respawn_after_mismatch(self, retries: int) -> bool:
        """Tear down and respawn once with a re-read container wid.

        MainWindow re-reads the live container wid into self.embed_wid before
        every ensure_started, so the next _spawn gets a fresh, rooted wid.
        """
        try:
            with (self.cfg.data_dir() / "spawn.log").open("a") as sf:
                sf.write(
                    f"{time.strftime('%Y-%m-%d %H:%M:%S')} embed mismatch: "
                    f"container_wid={self.embed_wid} retry={retries}\n"
                )
        except OSError:
            pass
        self._teardown(silent=True)
        return self._spawn(retries=retries - 1)

    def _teardown(self, silent: bool = False) -> None:
        ipc, proc = self._ipc, self._proc
        self._ipc = None
        self._proc = None
        if ipc is not None:
            try:
                ipc.close()
            except Exception:
                pass
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        try:
            self._sock_path.unlink()
        except OSError:
            pass

    def shutdown(self) -> None:
        self._closing = True
        try:
            if self._ipc is not None:
                try:
                    self._ipc.command("quit", timeout=2)
                except Exception:
                    pass
            self._teardown(silent=True)
        finally:
            self._closing = False

    def wait_stopped(self, timeout: float = 5.0) -> bool:
        """Block until the mpv process is gone (so it never outlives the app as a
        detached orphan window). Returns True if it exited within `timeout`."""
        proc = self._proc
        if proc is None:
            return True
        try:
            proc.wait(timeout=timeout)
            return True
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
            return False

    # ---------------- events ----------------

    def _on_event(self, event: dict) -> None:
        name = event.get("event")
        if name in ("socket-closed", "shutdown", "fatal_error"):
            if not self._closing:
                self.mpv_exited.emit()
            self._teardown(silent=True)
        elif name == "end-file" and event.get("reason") == "error":
            # Skip to the next entry off the reader thread: a blocking command
            # here would deadlock the IPC reader waiting for its reply.
            threading.Thread(target=self._skip_on_error, daemon=True).start()
        elif name == "end-file" and event.get("reason") == "eof":
            threading.Thread(target=self._advance_on_eof, daemon=True).start()
        elif name == "property-change" and event.get("name") == "pause":
            # Live pause/play state (via observe_property): refresh the UI now.
            threading.Thread(target=self._emit_state, daemon=True).start()
        elif name == "file-loaded":
            # The demuxer finished loading: mpv's VO window exists now, which
            # makes this the authoritative moment to prove the embed (the UI
            # hooks file_loaded and runs a strict descendant check).
            self.file_loaded.emit()
            threading.Thread(target=self._emit_state, daemon=True).start()

    def _skip_on_error(self) -> None:
        if self._ipc is None:
            return
        try:
            self._ipc.command("playlist-next", timeout=2)
            self.show_osd("Playback error — skipped")
        except Exception:
            pass

    def _advance_on_eof(self) -> None:
        """Auto-advance: when a video ends normally, move to the next playlist
        entry (kept at the last one). mpv pauses at EOF because of --keep-open,
        so we drive the playlist ourselves. Runs off the IPC reader thread so a
        blocking command can't deadlock it."""
        if self._ipc is None:
            return
        try:
            self._ipc.command("playlist-next", "weak", timeout=2)
        except Exception:
            pass

    def _emit_state(self) -> None:
        try:
            self.state_changed.emit(self.poll_state())
        except Exception:
            pass

    # ---------------- commands ----------------

    def _cmd(self, *args) -> None:
        if self._ipc is None:
            return
        try:
            self._ipc.command(*args, timeout=10)
        except Exception as e:
            log.warning("mpv command %r failed: %s", args, e)

    def load_playlist(self, entries: list[tuple[str, str]]) -> None:
        """entries = list of (url, title). First replaces, rest append.

        mpv >=0.40 loadfile signature: loadfile <url> <flags> <index> <options>.
        """
        if not entries or self._ipc is None:
            return
        for i, (url, title) in enumerate(entries):
            flag = "replace" if i == 0 else "append"
            try:
                self._ipc.command(
                    "loadfile", url, flag, -1, {"force-media-title": title}, timeout=30
                )
            except Exception as e:
                log.warning("loadfile %s failed: %s", url, e)

    def play(self) -> None:
        self._cmd("set", "pause", "no")

    def pause(self) -> None:
        self._cmd("pause")

    def toggle(self) -> None:
        self._cmd("cycle", "pause")

    def stop(self) -> None:
        self._cmd("stop")

    def next(self) -> None:
        self._cmd("playlist-next")

    def prev(self) -> None:
        self._cmd("playlist-prev")

    def seek(self, sec: float) -> None:
        self._cmd("seek", str(sec), "absolute")

    def seek_relative(self, delta: float) -> None:
        """Jump by +/- seconds from the current position (keyboard seeking)."""
        self._cmd("seek", f"{delta:+.3f}", "relative")

    def set_speed(self, factor: float) -> None:
        """Playback speed multiplier. The property persists inside mpv, so the
        next playlist entry keeps playing at this speed too."""
        self._cmd("set", "speed", f"{max(0.1, min(10.0, float(factor))):.3f}")

    def get_speed(self) -> float | None:
        if self._ipc is None:
            return None
        try:
            return float(self._ipc.command("get_property", "speed", timeout=5))
        except Exception:
            return None

    def set_volume(self, v: int) -> None:
        self._cmd("set", "volume", str(max(0, min(100, int(v)))))

    def show_osd(self, text: str) -> None:
        self._cmd("show-text", text, "4000")

    def poll_state(self) -> dict:
        def prop(name: str):
            try:
                return self._ipc.command("get_property", name, timeout=5)
            except Exception:
                return None

        return {
            "position": prop("time-pos"),
            "duration": prop("duration"),
            "paused": prop("pause"),
            "title": prop("media-title"),
            "playlist_position": prop("playlist-pos"),
            "playlist_count": prop("playlist-count"),
        }