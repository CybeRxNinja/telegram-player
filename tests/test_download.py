"""Tests for the parallel (Nekogram-style) download accelerator.

We use a faithful fake TelegramClient whose ``iter_download`` mirrors Telethon's
real semantics (offset in bytes, limit = chunk count, yields up to request_size
bytes per chunk). That lets us exercise the range splitting and the
contiguous reassembly without any network.
"""

from __future__ import annotations

import asyncio
import random
import types
from pathlib import Path

import pytest

from telegram_player.config import Config
from telegram_player.media import MediaHandle
from telegram_player.tg_client import (
    DOWNLOAD_PART,
    MAX_DOWNLOAD_CONCURRENCY,
    TelegramService,
    _safe_concurrency,
    _split_ranges,
)


def _rand(n: int, seed: int = 0) -> bytes:
    return random.Random(seed).randbytes(n)


class FakeDoc:
    """Stand-in for a Telethon document; the fake client ignores it."""


class FakeClient:
    def __init__(self, data: bytes, sleep: float = 0.0,
                 sleep_for=None):
        self._data = data
        self.sleep = sleep
        self.sleep_for = sleep_for  # optional: offset -> seconds

    async def iter_download(self, file, *, offset: int = 0, limit=None,
                            file_size=None, request_size=DOWNLOAD_PART, **kw):
        data = self._data
        part = request_size or DOWNLOAD_PART
        n = limit if limit is not None else (len(data) + part - 1) // part
        pos = offset
        chunks = []
        for _ in range(n):
            if pos >= len(data):
                break
            chunk = data[pos:pos + part]
            if not chunk:
                break
            chunks.append(chunk)
            pos += len(chunk)
        # Deliver in offset order (the worker writes at start + n); interleave
        # with sleeps so ranges complete out of order.
        for c in chunks:
            if self.sleep_for is not None:
                await asyncio.sleep(self.sleep_for(offset))
            elif self.sleep:
                await asyncio.sleep(self.sleep * random.random())
            offset += len(c)
            yield c


def _make_service(data: bytes, **kw) -> TelegramService:
    svc = TelegramService(Config())
    svc._client = FakeClient(data, **kw)
    # Neutralize the Qt signal so no QApplication is needed.
    svc.status = types.SimpleNamespace(emit=lambda *a, **k: None)
    return svc


async def _download(data: bytes, *, concurrency=None, stream_safe=False,
                    sleep=0.0, sleep_for=None, tmp_path: Path) -> bytes:
    svc = _make_service(data, sleep=sleep, sleep_for=sleep_for)
    dest = tmp_path / "out.bin"
    handle = MediaHandle(doc=FakeDoc(), size=len(data), title="x")
    await svc._download_parallel(
        handle, dest, stream_safe=stream_safe, concurrency=concurrency,
    )
    return dest.read_bytes()


# ---------------- range splitting ----------------

def test_split_ranges_covers_and_aligns():
    part = DOWNLOAD_PART
    for total in (100, part - 1, part, part * 3 + 123, part * 10):
        for n in (1, 2, 4, 8):
            ranges = _split_ranges(total, n, part)
            # contiguous + cover everything
            assert ranges[0][0] == 0
            assert ranges[-1][1] == total
            for (s, e), (s2, e2) in zip(ranges, ranges[1:]):
                assert e == s2
            # each start aligned to part (except a possible <part tail)
            for s, e in ranges:
                assert s % part == 0 or s == 0
                assert e > s


def test_split_ranges_tiny_file_is_single():
    assert _split_ranges(500, 8, DOWNLOAD_PART) == [(0, 500)]


def test_safe_concurrency_clamps():
    # <=0 is treated as "use the default" (4); the clamp only bounds the max.
    assert _safe_concurrency(None) == 4
    assert _safe_concurrency(0) == 4
    assert _safe_concurrency(-3) == 4
    assert _safe_concurrency(100) == MAX_DOWNLOAD_CONCURRENCY
    assert _safe_concurrency(8) == 8
    assert _safe_concurrency(3) == 3


# ---------------- reassembly correctness ----------------

@pytest.mark.parametrize("size", [500, DOWNLOAD_PART, DOWNLOAD_PART * 2 + 7,
                                  DOWNLOAD_PART * 5, 17 * 1024 * 1024,
                                  100 * 1024 * 1024])
@pytest.mark.parametrize("conc", [1, 4, 8, 100])  # 100 exercises clamping
@pytest.mark.parametrize("safe", [False, True])
def test_parallel_reconstructs_exactly(size, conc, safe, tmp_path):
    data = _rand(size, seed=size + conc)
    out = asyncio.run(_download(data, concurrency=conc, stream_safe=safe,
                                tmp_path=tmp_path))
    assert out == data, f"size={size} conc={conc} safe={safe}"


def test_streamsafe_reorders_later_ranges_first(tmp_path):
    """Force later byte-ranges to finish before earlier ones: the contiguous
    writer must hold them in `pending` until the frontier reaches them."""
    size = 50 * 1024 * 1024
    data = _rand(size, seed=42)

    def sleep_for(offset):
        # higher offsets sleep less -> complete first
        return 0.005 * (1.0 - offset / size)

    out = asyncio.run(_download(data, concurrency=8, stream_safe=True,
                                sleep_for=sleep_for, tmp_path=tmp_path))
    assert out == data


def test_config_download_concurrency_clamped_on_load(tmp_path, monkeypatch):
    import json
    from telegram_player import config as cfg_mod
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    cfgdir = tmp_path / "telegram-player"
    cfgdir.mkdir(parents=True, exist_ok=True)
    (cfgdir / "config.json").write_text(
        json.dumps({"download_concurrency": 99}))
    cfg = cfg_mod.Config.load()
    assert cfg.download_concurrency == MAX_DOWNLOAD_CONCURRENCY
