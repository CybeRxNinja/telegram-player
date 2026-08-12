"""FileServer: growing-file byte-range serving (progressive playback support)."""

import http.client
import tempfile
import threading
import time
from pathlib import Path

from telegram_player.file_server import FileServer


def _get(port: int, token: str, rng: str | None = None, timeout: float = 5.0):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    headers = {"Range": rng} if rng else {}
    c.request("GET", f"/v/{token}", headers=headers)
    r = c.getresponse()
    body = r.read()
    c.close()
    return r.status, dict(r.getheaders()), body


def test_serves_available_range():
    fs = FileServer()
    port = fs.start()
    try:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "vid.mp4"
            p.write_bytes(b"0123456789" * 10)  # 100 bytes
            tok = fs.register(str(p), 100)
            status, headers, body = _get(port, tok, "bytes=0-9")
            assert status == 206
            assert body == b"0123456789"
            assert headers["Content-Range"] == "bytes 0-9/100"
            # open-ended range
            status, headers, body = _get(port, tok, "bytes=50-")
            assert status == 206
            assert body == b"0123456789" * 5
    finally:
        fs.close()


def test_waits_for_growth():
    fs = FileServer()
    port = fs.start()
    try:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "grow.mp4"
            p.write_bytes(b"A" * 50)
            tok = fs.register(str(p), 200)
            # Request bytes 100-199: not available yet -> server must block.
            result = {}

            def req():
                result["resp"] = _get(port, tok, "bytes=100-199", timeout=10)

            t = threading.Thread(target=req)
            t.start()
            time.sleep(0.5)
            assert t.is_alive(), "server should be waiting for growth"
            with open(p, "ab") as f:
                f.write(b"B" * 150)  # now 200 bytes
            t.join(timeout=5)
            assert not t.is_alive(), "request should complete once data exists"
            status, headers, body = result["resp"]
            assert status == 206
            assert body == b"B" * 100
            assert headers["Content-Range"] == "bytes 100-199/200"
    finally:
        fs.close()


def test_complete_file_200_and_416():
    fs = FileServer()
    port = fs.start()
    try:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "done.mp4"
            p.write_bytes(b"x" * 64)
            tok = fs.register(str(p), 64)
            status, headers, body = _get(port, tok)
            assert status == 200
            assert len(body) == 64
            status, _, _ = _get(port, tok, "bytes=64-")
            assert status == 416
    finally:
        fs.close()


def test_waits_for_file_to_appear():
    """URLs are registered before their download starts; the server must block
    until the file exists instead of 404ing."""
    fs = FileServer()
    port = fs.start()
    try:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "soon.mp4"
            tok = fs.register(str(p), 100)
            result = {}

            def req():
                result["resp"] = _get(port, tok, timeout=10)

            t = threading.Thread(target=req)
            t.start()
            time.sleep(0.5)
            assert t.is_alive(), "server should be waiting for the file"
            p.write_bytes(b"C" * 100)
            t.join(timeout=5)
            assert not t.is_alive(), "request should complete once the file exists"
            status, headers, body = result["resp"]
            assert status == 200
            assert len(body) == 100
    finally:
        fs.close()


def test_unknown_token_404():
    fs = FileServer()
    port = fs.start()
    try:
        status, _, _ = _get(port, "deadbeef")
        assert status == 404
    finally:
        fs.close()
