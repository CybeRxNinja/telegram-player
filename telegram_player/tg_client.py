"""Telegram integration: Telethon running on its own asyncio-loop thread.

All long-running work happens on that loop thread; the UI thread talks to the
service through `_await` (blocking bridge, for quick calls like login steps) or
`_call` (worker thread, results delivered via callbacks/signals). Playback
resolves + downloads each item into the cache and hands mpv a real local file.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import re
import threading
from pathlib import Path

from PyQt6.QtCore import QObject, pyqtSignal as Signal



# ---------------------------------------------------------------------------
# Download accelerator (Nekogram-style parallel part fetch)
#
# A single file is fetched by splitting it into contiguous byte ranges and
# downloading each range concurrently with its own ``iter_download`` stream.
# Telegram's MTProto lets multiple ``upload.getFile`` requests overlap on one
# connection, so a handful of in-flight ranges hides per-request latency and
# multiplies throughput without touching more than one file at a time.
#
# ``MAX_DOWNLOAD_CONCURRENCY`` is the "safe limit": enough to saturate a link
# without hammering the DC into a FLOOD_WAIT. Nekogram exposes this as a
# tunable thread count; we clamp the user setting into this window.
# ---------------------------------------------------------------------------
DOWNLOAD_PART = 512 * 1024          # largest single upload.getFile request
MAX_DOWNLOAD_CONCURRENCY = 8        # hard safe ceiling for parallel ranges


def _safe_concurrency(n: int | None) -> int:
    if not n or n < 1:
        n = 4
    return max(1, min(int(n), MAX_DOWNLOAD_CONCURRENCY))


def _split_ranges(total: int, n: int, part: int) -> list[tuple[int, int]]:
    """Split [0, total) into ``n`` contiguous, part-aligned byte ranges that
    together cover the whole file (the last range absorbs the remainder)."""
    nparts = total // part
    if nparts == 0:
        return [(0, total)]
    n = max(1, min(n, nparts))
    base = nparts // n
    extra = nparts % n
    ranges: list[tuple[int, int]] = []
    pos = 0
    for w in range(n):
        cnt = base + (1 if w < extra else 0)
        end = pos + cnt * part
        if w == n - 1:
            end = total  # last worker also takes any <part remainder
        ranges.append((pos, end))
        pos = end
    return ranges


from telethon import TelegramClient
from telethon.errors import (
    PasswordHashInvalidError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
)

from .config import Config
from .file_server import FileServer
from .media import ChatInfo, MediaHandle, MediaItem, video_title
from .playlist import classify_media

log = logging.getLogger(__name__)

_UI_TIMEOUT = 60.0


class PasswordNeeded(Exception):
    """Sentinel raised to the caller when a 2FA password must be entered."""


class TelegramService(QObject):
    connected = Signal(str)  # display name
    login_failed = Signal(str)  # human message
    status = Signal(str)
    chats_loaded = Signal(list)  # list[ChatInfo]
    qr_refreshed = Signal(str)  # new QR url after recreate()
    playback_ready = Signal(list)  # list[(str url, str title)] — full playlist to load
    playback_failed = Signal(str)  # human message

    def __init__(self, cfg: Config):
        super().__init__()
        self.cfg = cfg
        self._client: TelegramClient | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._tasks: set = set()  # concurrent.futures.Future handles
        self._active_dl: concurrent.futures.Future | None = None
        self._active_dl_key: tuple[int, int] | None = None
        self._phone: str | None = None
        self._file_server = FileServer()

    # ---------------- lifecycle ----------------

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return  # already running (MainWindow inits once; be safe anyway)
        self._file_server.start()
        self._thread = threading.Thread(target=self._run_loop, name="telethon-loop", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=5)
        if self.cfg.api_id and self.cfg.api_hash:
            asyncio.run_coroutine_threadsafe(self._connect(), self._loop)
        else:
            self.status.emit("Not logged in — open Settings to add your api_id/api_hash")

    def _run_loop(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        try:
            self._loop.run_forever()
        finally:
            for fut in list(self._tasks):
                fut.cancel()
            for fut in list(self._tasks):
                try:
                    fut.result(timeout=1)
                except Exception:
                    pass
            self._loop.close()

    def stop(self) -> None:
        self._file_server.close()
        if self._client is not None and self._loop is not None:
            try:
                asyncio.run_coroutine_threadsafe(
                    self._client.disconnect(), self._loop
                ).result(timeout=5)
            except Exception:
                pass
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=5)

    # ---------------- threading bridges ----------------

    def _await(self, coro, timeout: float = _UI_TIMEOUT):
        """Run a coroutine on the loop thread and block for its result."""
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout=timeout)

    def _spawn(self, coro_factory) -> "concurrent.futures.Future":
        """Schedule a coroutine on the loop thread and WAKE it.

        loop.create_task() is not thread-safe: called from the UI thread it
        queues the task but never wakes an idle loop (epoll), so the task only
        runs when unrelated I/O happens. run_coroutine_threadsafe uses
        call_soon_threadsafe, which wakes the loop."""
        fut = asyncio.run_coroutine_threadsafe(coro_factory(), self._loop)
        self._tasks.add(fut)
        fut.add_done_callback(self._tasks.discard)
        return fut

    def _call(self, coro_factory, on_success=None, on_error=None) -> threading.Thread:
        """Run a coroutine on a worker thread, delivering result/error via callbacks."""

        def worker():
            try:
                result = self._await(coro_factory())
            except PasswordNeeded:
                if on_error:
                    on_error(PasswordNeeded)
                return
            except Exception as e:
                log.exception("service call failed")
                if on_error:
                    on_error(str(e))
                return
            if on_success:
                on_success(result)

        t = threading.Thread(target=worker, daemon=True, name="service-call")
        t.start()
        return t

    # ---------------- connection / auth ----------------

    async def _connect(self) -> None:
        self.status.emit("Connecting…")
        try:
            # Telethon's SQLite session needs an existing parent dir; a fresh
            # machine has none (data_dir is only created here). Without this,
            # sqlite3 raises "unable to open database file" before any network I/O.
            self.cfg.session_path.parent.mkdir(parents=True, exist_ok=True)
            self._client = TelegramClient(
                str(self.cfg.session_path), self.cfg.api_id, self.cfg.api_hash
            )
            await self._client.connect()
        except Exception as e:
            self.login_failed.emit(f"Cannot reach Telegram: {e}")
            return
        if await self._client.is_user_authorized():
            await self._post_auth()
        else:
            self.status.emit("Connected to Telegram — please sign in")

    async def _post_auth(self) -> None:
        try:
            me = await self._client.get_me()
            name = (me.first_name or me.username or "user").strip()
        except Exception:
            name = "user"
        self.connected.emit(name)
        self.status.emit("Connected")
        task = self._loop.create_task(self._load_chats())
        self._tasks.add(task)

    def request_login_qr(self) -> str | None:
        """Start QR login; returns the tg:// URL to render, or None on failure."""
        if not (self.cfg.api_id and self.cfg.api_hash):
            self.login_failed.emit("Set your api_id/api_hash in Settings first")
            return None
        try:
            if self._client is None:
                self._await(self._connect(), timeout=30)
            qr = self._await(self._client.qr_login(), timeout=30)
        except Exception as e:
            self.login_failed.emit(f"QR login failed: {e}")
            return None
        self._spawn(lambda: self._wait_qr(qr))
        return qr.url

    async def _wait_qr(self, qr) -> None:
        try:
            while True:
                try:
                    await qr.wait(timeout=15)
                except asyncio.TimeoutError:
                    try:
                        # The TCP connection can drop while the QR waits for a
                        # scan; recreate() needs an alive client.
                        if self._client is not None and not self._client.is_connected():
                            await self._client.connect()
                        await qr.recreate()
                    except Exception as e:
                        self.login_failed.emit(f"QR refresh failed: {e}")
                        return
                    self.qr_refreshed.emit(qr.url)
                    continue
                break
            await self._post_auth()
        except SessionPasswordNeededError:
            self.login_failed.emit(
                "This account needs a 2FA password — sign in via the Phone tab instead"
            )
        except Exception as e:
            self.login_failed.emit(str(e))
        finally:
            self._tasks.discard(asyncio.current_task())

    def submit_phone(self, phone: str) -> str | None:
        """Returns None on success, else an error message."""
        phone = (phone or "").strip().replace(" ", "")
        if not phone:
            return "Enter a phone number"
        self._phone = phone
        try:
            self._await(self._client.send_code_request(phone))
            return None
        except PhoneNumberInvalidError:
            return "Invalid phone number"
        except Exception as e:
            return f"Error: {e}"

    def submit_code(self, code: str) -> str | None:
        """Returns None on success, else an error message. Raises PasswordNeeded for 2FA."""
        try:
            self._await(self._client.sign_in(self._phone, (code or "").strip()))
        except SessionPasswordNeededError:
            self.status.emit("2FA required — enter your password")
            raise PasswordNeeded()
        except PhoneCodeInvalidError:
            return "Invalid code"
        except Exception as e:
            return f"Error: {e}"
        self._call(self._post_auth)
        return None

    def submit_password(self, password: str) -> str | None:
        try:
            self._await(self._client.sign_in(password=password))
        except PasswordHashInvalidError:
            return "Wrong password"
        except Exception as e:
            return f"Error: {e}"
        self._call(self._post_auth)
        return None

    def logout(self) -> None:
        if self._client is not None:
            try:
                self._await(self._client.log_out(), timeout=30)
            except Exception as e:
                log.warning("log_out failed: %s", e)
        for p in (self.cfg.session_path, Path(str(self.cfg.session_path) + ".session")):
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass
        self.status.emit("Logged out")

    # ---------------- chats & media ----------------

    async def _load_chats(self) -> None:
        try:
            chats: list[ChatInfo] = []
            async for d in self._client.iter_dialogs():
                if d.is_channel or d.is_group:
                    chats.append(
                        ChatInfo(
                            d.id,
                            d.title,
                            "channel" if d.is_channel else "group",
                            forum=bool(getattr(d.entity, "forum", False)),
                        )
                    )
        except Exception as e:
            self.login_failed.emit(f"Could not load chats: {e}")
            return
        self.chats_loaded.emit(chats)

    def refresh_chats(self) -> None:
        if self._loop is not None:
            self._spawn(self._load_chats)

    async def load_media(
        self,
        chat_id: int,
        offset: int = 0,
        limit: int = 200,
        topic_id: int | None = None,
    ) -> list[MediaItem]:
        if topic_id is not None:
            msgs = await self._client.get_messages(
                chat_id, reply_to=topic_id, limit=limit, offset_id=offset, reverse=False
            )
            return classify_media(msgs)
        if offset:
            # "Load more": paginate within the same chat as the first load.
            msgs = await self._client.get_messages(
                chat_id, limit=limit, offset_id=offset, reverse=False
            )
            return classify_media(msgs)
        # First load: detect forum channels so their per-topic media is gathered.
        entity = await self._client.get_entity(chat_id)
        if getattr(entity, "forum", False):
            return await self._load_forum_media(chat_id, limit)
        msgs = await self._client.get_messages(
            chat_id, limit=limit, offset_id=0, reverse=False
        )
        return classify_media(msgs)

    async def load_topics(self, chat_id: int) -> list[tuple[int, str]]:
        """Forum topics as (topic_id, title), newest first; [] if not a forum."""
        try:
            return [(t.id, getattr(t, "title", f"Topic {t.id}")) for t in await self._forum_topics(chat_id)]
        except Exception as e:
            log.warning("load_topics(%s) failed: %s", chat_id, e)
            return []

    async def _forum_topics(self, chat_id: int) -> list:
        """All non-hidden forum topics, bounded to 300 (pagination in pages of 100)."""
        from telethon.tl.functions.messages import GetForumTopicsRequest

        topics: list = []
        offset_date = None
        offset_id = 0
        offset_topic = 0
        for _ in range(3):
            r = await self._client(
                GetForumTopicsRequest(
                    peer=chat_id,
                    offset_date=offset_date,
                    offset_id=offset_id,
                    offset_topic=offset_topic,
                    limit=100,
                    q="",
                )
            )
            page = [t for t in (r.topics or ()) if not getattr(t, "hidden", False)]
            if not page:
                break
            topics.extend(page)
            offset_date = page[-1].date
            offset_id = page[-1].top_message
            offset_topic = page[-1].id
            if len(page) < 100 or getattr(r, "count", None) is not None and len(topics) >= r.count:
                break
        return topics

    async def _load_forum_media(self, chat_id: int, limit: int) -> list[MediaItem]:
        """Forum channels keep videos inside topics; gather the newest across them."""
        topics = await self._forum_topics(chat_id)
        if not topics:
            msgs = await self._client.get_messages(
                chat_id, limit=limit, offset_id=0, reverse=False
            )
            return classify_media(msgs)
        per = max(1, limit // len(topics) + 1)
        out: list[MediaItem] = []
        for t in topics:
            try:
                msgs = await self._client.get_messages(
                    chat_id, reply_to=t.id, limit=per, reverse=False
                )
                out.extend(classify_media(msgs))
            except Exception as e:
                log.warning("topic %s (%s) failed: %s", t.id, getattr(t, "title", "?"), e)
        out.sort(key=lambda m: m.message_id, reverse=True)  # newest first
        return out[:limit]

    def apply_credentials(self) -> None:
        """Reconnect with (possibly new) api_id/api_hash from cfg."""
        if self._loop is not None:
            asyncio.run_coroutine_threadsafe(self._connect(), self._loop)
        else:
            self.start()
    async def resolve_media(self, chat_id: int, message_id: int) -> MediaHandle | None:
        msg = await self._client.get_messages(chat_id, ids=message_id)
        if msg is None or msg.media is None:
            return None
        doc = getattr(msg.media, "document", None)
        if doc is None:
            return None
        return MediaHandle(doc=doc, size=doc.size, title=video_title(msg))

    # ---------------- playback: download to cache, play while it grows ----------------

    def play_playlist(self, items: list[tuple[int, int, str, int | None]]) -> None:
        """Register every (chat_id, message_id, title, size) with the FileServer
        and hand the whole playlist to mpv at once — URLs only, no bytes are
        downloaded here. The UI calls `download_item` for each entry on demand
        as it becomes the currently-playing one (only the played item is ever
        downloaded). Emits one playback_ready with the full entry list."""
        if self._loop is None:
            self.playback_failed.emit("Not connected")
            return
        if not items:
            self.playback_failed.emit("No playable items")
            return
        self._spawn(lambda: self._register_playlist_async(list(items)))

    async def _register_playlist_async(self, items: list[tuple[int, int, str, int | None]]) -> None:
        entries: list[tuple[str, str]] = []
        for chat_id, message_id, title, size in items:
            dest = self._cache_path(chat_id, message_id, title)
            try:
                token = self._file_server.register(str(dest), size)
                entries.append((self._file_server.url_for(token), title))
            except Exception as e:
                log.warning("register %s failed: %s", title, e)
        if entries:
            self.playback_ready.emit(entries)
        else:
            self.playback_failed.emit("No playable items")

    # ---------------- on-demand download of the currently played item ----------------
        return entries

    def download_item(self, chat_id: int, message_id: int, title: str, size: int | None) -> None:
        """Download one playlist item into the cache, replacing any in-flight
        download of a different item (the user skipped ahead). Cached files are
        reused; a partial file (wrong size) is re-downloaded. The FileServer
        blocks mpv's reads until the bytes are there, so playback starts while
        the file is still growing."""
        if self._loop is None:
            return
        key = (chat_id, message_id)
        active = self._active_dl
        if active is not None and not active.done():
            if key == self._active_dl_key:
                return  # already downloading this very item
            active.cancel()
        self._active_dl_key = key
        fut = self._spawn(
            lambda: self._download_item_async(chat_id, message_id, title, size)
        )
        self._active_dl = fut

    async def _download_item_async(
        self, chat_id: int, message_id: int, title: str, size: int | None
    ) -> None:
        try:
            handle = await self.resolve_media(chat_id, message_id)
            if handle is None:
                self.status.emit(f"Skipped unavailable: {title}")
                return
            dest = self._cache_path(chat_id, message_id, title)
            expected = handle.size or size
            if dest.exists() and (expected is None or dest.stat().st_size == expected):
                self.status.emit(f"Cached: {title}")
                return
            self.status.emit(f"Downloading {title}…")
            exists = asyncio.Event()
            await self._download_parallel(handle, dest, exists=exists, stream_safe=True)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("download_item %s failed: %s", title, e)

    # ---------------- download all (sequential, into cache/playlist/) ----------------

    def download_all(self, items: list[tuple[int, int, str, int | None]]) -> None:
        """Download every playlist item one by one into cache_dir()/playlist/,
        reusing files that already exist with the right size."""
        if self._loop is None:
            self.status.emit("Not connected")
            return
        self._spawn(lambda: self._download_all_async(list(items)))

    async def _download_all_async(self, items: list[tuple[int, int, str, int | None]]) -> None:
        folder = Config.cache_dir() / "playlist"
        folder.mkdir(parents=True, exist_ok=True)
        ok = failed = 0
        for i, (chat_id, message_id, title, size) in enumerate(items, 1):
            self.status.emit(f"Download all {i}/{len(items)}: {title}")
            try:
                handle = await self.resolve_media(chat_id, message_id)
                if handle is None:
                    failed += 1
                    self.status.emit(f"Skipped unavailable: {title}")
                    continue
                dest = self._download_all_dest(folder, chat_id, message_id, title, handle.size or size)
                await self._download_parallel(handle, dest, exists=asyncio.Event(), stream_safe=False)
                ok += 1
            except asyncio.CancelledError:
                raise
            except Exception as e:
                failed += 1
                log.warning("download_all %s failed: %s", title, e)
                self.status.emit(f"Download failed: {title}")
        msg = f"Downloaded {ok} file(s) to {folder}"
        if failed:
            msg += f", {failed} failed"
        self.status.emit(msg)

    @staticmethod
    def _download_all_dest(folder: Path, chat_id: int, message_id: int, title: str, size: int | None) -> Path:
        """Sanitized title-based filename; -2/-3… suffix on size mismatch."""
        name = re.sub(r"[^\w.\- ]+", "_", title or "").strip(" ._")[:80]
        if not name:
            name = f"{chat_id}_{message_id}"
        if not Path(name).suffix or len(Path(name).suffix) > 6:
            name += ".mp4"
        dest = folder / name
        n = 2
        while dest.exists() and (size is None or dest.stat().st_size != size):
            stem, suffix = Path(name).stem, Path(name).suffix
            dest = folder / f"{stem}-{n}{suffix}"
            n += 1
        return dest

    # ---------------- shared download pump ----------------

    async def _download_parallel(
        self,
        handle: "MediaHandle",
        dest: Path,
        *,
        exists: asyncio.Event | None = None,
        stream_safe: bool = False,
        concurrency: int | None = None,
    ) -> None:
        """Download ``handle`` to ``dest`` as fast as safely possible.

        The file is split into contiguous byte ranges and each range is pulled
        concurrently with its own ``iter_download`` stream (the Nekogram
        "parallel call queues" trick). This hides per-request latency and
        multiplies throughput without downloading more than one file at a time.

        Two modes:
          * ``stream_safe=False`` (explicit save): each range is written
            straight to its offset. Gaps may exist mid-flight but the final
            file is correct — fine because nothing is reading it yet.
          * ``stream_safe=True`` (playback streaming): bytes are only flushed
            to disk in strict contiguous order from offset 0, so a player
            reading the growing file never sees a hole (mpv would treat zeros
            as corrupt media). This keeps "start playing while downloading"
            correct while still fetching the tail in parallel.
        """
        total = handle.size
        conc = _safe_concurrency(concurrency or getattr(self.cfg, "download_concurrency", None))
        # Small/unknown files: a single sequential stream is simplest and safe.
        if total is None or total <= DOWNLOAD_PART * 2 or conc <= 1:
            await self._download_sequential(handle, dest, exists, stream_safe)
            return

        dest.parent.mkdir(parents=True, exist_ok=True)
        # Create the file up front so the FileServer / player can open it.
        with open(dest, "wb"):
            pass
        if exists is not None:
            exists.set()

        ranges = _split_ranges(total, conc, DOWNLOAD_PART)
        if stream_safe:
            await self._download_streamsafe(handle, dest, total, ranges)
        else:
            await self._download_fast(handle, dest, total, ranges)

    async def _download_fast(
        self, handle, dest, total, ranges
    ) -> None:
        """Write each range directly at its offset (may leave mid-flight gaps)."""
        got = 0
        lock = asyncio.Lock()
        last_pct = -1

        async def worker(start: int, end: int) -> None:
            nonlocal got, last_pct
            n = 0
            fh = open(dest, "r+b")
            try:
                lim = max(1, (end - start + DOWNLOAD_PART - 1) // DOWNLOAD_PART)
                async for chunk in self._client.iter_download(
                    handle.doc, offset=start, limit=lim,
                    file_size=total, request_size=DOWNLOAD_PART,
                ):
                    fh.seek(start + n)
                    fh.write(chunk)
                    n += len(chunk)
                    async with lock:
                        got += len(chunk)
                        pct = int(got * 100 / total)
                        if pct >= last_pct + 2:
                            last_pct = pct
                            self.status.emit(f"Downloading {pct}%…")
            finally:
                fh.close()

        await asyncio.gather(*(worker(s, e) for s, e in ranges))

    async def _download_streamsafe(
        self, handle, dest, total, ranges
    ) -> None:
        """Reassemble in strict contiguous order so a reader never sees a hole.

        Workers fetch their ranges in parallel into memory and hand completed
        chunks to a single writer that only advances the on-disk frontier when
        the exact next bytes are ready. Memory use is bounded by the in-flight
        chunks (one per range), not the whole file.
        """
        fh = open(dest, "r+b")
        frontier = 0
        pending: dict[int, bytes] = {}
        got = 0
        lock = asyncio.Lock()
        last_pct = -1

        # NOTE: ``flush`` is only ever called while ``lock`` is held by the
        # caller, so it must NOT re-acquire the lock (asyncio.Lock is not
        # reentrant — doing so deadlocks).
        async def flush() -> None:
            nonlocal frontier, got, last_pct
            while frontier in pending:
                data = pending.pop(frontier)
                fh.seek(frontier)
                fh.write(data)
                frontier += len(data)
                got += len(data)
                pct = int(got * 100 / total)
                if pct >= last_pct + 2:
                    last_pct = pct
                    self.status.emit(f"Downloading {pct}%…")

        async def worker(start: int, end: int) -> None:
            n = 0
            lim = max(1, (end - start + DOWNLOAD_PART - 1) // DOWNLOAD_PART)
            async for chunk in self._client.iter_download(
                handle.doc, offset=start, limit=lim,
                file_size=total, request_size=DOWNLOAD_PART,
            ):
                off = start + n
                async with lock:
                    pending[off] = chunk
                    await flush()
                n += len(chunk)

        try:
            await asyncio.gather(*(worker(s, e) for s, e in ranges))
            # Drain anything left (all workers done, no contention).
            await flush()
        finally:
            fh.close()

    async def _download_sequential(
        self, handle, dest, exists, stream_safe
    ) -> None:
        """Fallback for tiny/unknown-size files: a single contiguous stream."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        got = 0
        last_pct = -1
        try:
            with open(dest, "wb") as f:
                if exists is not None:
                    exists.set()
                async for chunk in self._client.iter_download(
                    handle.doc, offset=0, limit=None,
                    file_size=handle.size, request_size=DOWNLOAD_PART,
                ):
                    f.write(chunk)
                    got += len(chunk)
                    if handle.size:
                        pct = int(got * 100 / handle.size)
                        if pct >= last_pct + 2:
                            last_pct = pct
                            self.status.emit(f"Downloading {pct}%…")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("download failed for %s: %s", dest.name, e)
            self.status.emit(f"Download failed: {dest.name}")
            raise

    # Backwards-compatible alias used by older call sites / tests.
    async def _pump_download(self, handle, dest, exists):
        await self._download_parallel(handle, dest, exists=exists, stream_safe=True)

    @staticmethod
    def _cache_path(chat_id: int, message_id: int, title: str) -> Path:
        suffix = Path(title).suffix
        if not suffix or len(suffix) > 6:
            suffix = ".mp4"
        return Config.cache_dir() / f"{chat_id}_{message_id}{suffix}"
