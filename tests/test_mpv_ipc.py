"""Tests for the mpv JSON-IPC client (protocol behavior against a fake peer)."""

from __future__ import annotations

import json
import socket
import threading

import pytest

from telegram_player.mpv_ctl import MpvIpc


@pytest.fixture()
def pair():
    a, b = socket.socketpair()
    events: list[dict] = []

    def server():
        buf = b""
        while True:
            try:
                data = b.recv(65536)
            except OSError:
                return
            if not data:
                return
            buf += data
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                req = json.loads(line)
                rid = req.get("request_id")
                if rid is None:
                    continue
                cmd = req.get("command", [])
                if cmd and cmd[0] == "silent":
                    continue  # never reply -> client timeout
                b.sendall(
                    json.dumps({"error": None, "data": "ok", "request_id": rid}).encode()
                    + b"\n"
                )

    threading.Thread(target=server, daemon=True).start()
    ipc = MpvIpc(a, on_event=events.append)
    yield ipc, b, events
    ipc.close()
    try:
        b.close()
    except OSError:
        pass


def test_command_roundtrip(pair):
    ipc, _, _ = pair
    assert ipc.command("test", "arg") == "ok"


def test_event_delivery(pair):
    ipc, b, events = pair
    b.sendall(json.dumps({"event": "file-loaded"}).encode() + b"\n")
    for _ in range(50):
        if events:
            break
        import time

        time.sleep(0.01)
    assert events == [{"event": "file-loaded"}]


def test_command_timeout(pair):
    ipc, _, _ = pair
    with pytest.raises(TimeoutError):
        ipc.command("silent", timeout=0.3)


def test_error_response_raises(pair):
    ipc, b, _ = pair

    def reply():
        return json.dumps({"error": "property not found", "data": None, "request_id": None})

    # Craft a reply for the next request: the fixture always replies "ok", so
    # use a raw second MpvIpc against a dedicated socket pair.
    ipc.close()
    a2, b2 = socket.socketpair()

    def server2():
        line = b2.recv(4096).split(b"\n", 1)[0]
        req = json.loads(line)
        b2.sendall(
            json.dumps(
                {"error": "property not found", "request_id": req["request_id"]}
            ).encode()
            + b"\n"
        )

    threading.Thread(target=server2, daemon=True).start()
    ipc2 = MpvIpc(a2)
    with pytest.raises(RuntimeError, match="property not found"):
        ipc2.command("get_property", "nope")
    ipc2.close()
    try:
        b2.close()
    except OSError:
        pass