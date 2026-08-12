"""Localhost HTTP server that serves growing cache files with byte-range support.

mpv plays a partially-downloaded cache file over HTTP: it treats HTTP as a
network stream, so when it reads ahead of what has been downloaded so far, its
demuxer cache simply waits for more data. This server blocks each request until
the requested byte range exists (or the download finishes), which gives true
"start playing while downloading" — mpv never needs to know the file is
incomplete, and no Telegram traffic ever flows through here (bytes come from
the local cache file only).
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

log = logging.getLogger(__name__)

_POLL = 0.1  # seconds between size checks while waiting for growth
_STALL = 60.0  # serve what exists after this long without the file growing


def _fsize(path: str) -> int:
    try:
        return Path(path).stat().st_size
    except OSError:
        return -1


class _FileHandler(BaseHTTPRequestHandler):
    server: "FileServer"  # type: ignore[misc]

    def log_message(self, *_a) -> None:  # silence default stderr logging
        pass

    def do_HEAD(self) -> None:
        self._serve(head_only=True)

    def do_GET(self) -> None:
        self._serve(head_only=False)

    def _serve(self, head_only: bool) -> None:
        token = self.path.removeprefix("/v/")
        info = self.server.tokens.get(token)
        if info is None:
            self.send_error(404, "unknown token")
            return
        path, expected = info

        rng = self.headers.get("Range")
        start, end = 0, None
        has_range = bool(rng and rng.startswith("bytes="))
        if has_range:
            try:
                start_s, _, end_s = rng[6:].partition("-")
                start = int(start_s)
                end = int(end_s) if end_s else None
            except ValueError:
                start, end = 0, None

        # Wait until the requested bytes exist or the download finishes. The
        # file may not exist yet at all (URLs are registered before their
        # download starts), so wait for it to appear too.
        deadline = time.monotonic() + _STALL
        last_size = -1
        while True:
            size = _fsize(path)
            need = (end + 1) if end is not None else (expected if expected is not None else 0)
            if size >= 0 and size >= need:
                break
            if size == last_size and time.monotonic() >= deadline:
                break  # stalled download: serve what we have
            last_size = size
            time.sleep(_POLL)
        if size < 0:
            self.send_error(404, "file gone")
            return

        avail = size if expected is None else min(size, expected)
        if expected is not None and start >= expected:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{expected}")
            self.end_headers()
            return
        if end is not None:
            end = min(end, avail - 1)
        else:
            end = avail - 1
        length = 0 if end < start else end - start + 1

        complete = expected is not None and size >= expected
        if not has_range and complete and start == 0:
            status, total = 200, length
        else:
            status, total = 206, length
        self.send_response(status)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(total))
        if status == 206:
            self.send_header(
                "Content-Range",
                f"bytes {start}-{end}/{expected if expected is not None else avail}",
            )
        self.end_headers()
        if head_only or length <= 0:
            return
        try:
            with open(path, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(65536, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass  # client went away


class FileServer:
    """Token-addressed range server over local cache files."""

    def __init__(self):
        self._tokens: dict[str, tuple[str, int | None]] = {}
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def tokens(self) -> dict[str, tuple[str, int | None]]:
        return self._tokens

    def start(self) -> int:
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _FileHandler)
        self._httpd.daemon_threads = True
        self._httpd.tokens = self._tokens  # handler reaches it via self.server.tokens
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, daemon=True, name="file-server"
        )
        self._thread.start()
        return self._httpd.server_address[1]

    def register(self, path: str, size: int | None) -> str:
        token = secrets.token_hex(12)
        self._tokens[token] = (path, size)
        return token

    def url_for(self, token: str) -> str:
        assert self._httpd is not None
        return f"http://127.0.0.1:{self._httpd.server_address[1]}/v/{token}"

    def close(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        self._tokens.clear()
