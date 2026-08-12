"""Regression tests for TelegramService.load_media (offset_id + forum topics)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from telegram_player.config import Config
from telegram_player.tg_client import TelegramService

# Fake Telethon message surface used by filter_videos / video_title.
def _video_msg(mid: int, name: str | None = None) -> SimpleNamespace:
    doc = SimpleNamespace(
        mime_type="video/mp4", size=1234,
        attributes=(SimpleNamespace(cls="DocumentAttributeVideo", duration=12),),
    )
    media = SimpleNamespace(document=doc, cls="MessageMediaDocument")
    return SimpleNamespace(
        chat_id=123, id=mid, media=media, document=doc, video=media,
        file=SimpleNamespace(name=name), message=f"caption {mid}",
    )


class FakeTopic:
    def __init__(self, tid: int, title: str):
        self.id = tid
        self.top_message = tid + 100
        self.date = None
        self.title = title
        self.hidden = False


class FakeClient:
    """Records get_messages calls; GetForumTopicsRequest via __call__."""

    def __init__(self, forum: bool = False, topic_ids: list[int] | None = None):
        self.forum = forum
        self.topic_ids = topic_ids or []
        self.get_calls: list[dict] = []
        self.topic_calls = 0

    async def get_entity(self, chat_id: int):
        return SimpleNamespace(forum=self.forum)

    async def get_messages(self, chat_id, **kwargs):
        self.get_calls.append(dict(chat_id=chat_id, **kwargs))
        if kwargs.get("reply_to") is not None:
            return [_video_msg(kwargs["reply_to"], f"topic-{kwargs['reply_to']}.mp4")]
        return [_video_msg(1, "plain.mp4")]

    async def __call__(self, request):
        self.topic_calls += 1
        return SimpleNamespace(
            topics=[FakeTopic(t, f"T{t}") for t in self.topic_ids],
            count=len(self.topic_ids),
        )


def _service(client: FakeClient) -> TelegramService:
    svc = TelegramService(Config())
    svc._client = client  # type: ignore[assignment]
    return svc


def test_first_load_passes_int_offset_id():
    """Regression: offset=0 must become offset_id=0 (int), not None — Telethon
    does `max(offset_id, max_id)` and `max(None, 0)` raises TypeError."""
    svc = _service(FakeClient())
    items = asyncio.run(svc.load_media(123, offset=0))
    assert svc._client.get_calls[0]["offset_id"] == 0
    assert items[0].title == "plain.mp4"


def test_load_more_passes_explicit_offset():
    svc = _service(FakeClient())
    asyncio.run(svc.load_media(123, offset=42))
    assert svc._client.get_calls[0]["offset_id"] == 42


def test_non_forum_uses_plain_history():
    svc = _service(FakeClient(forum=False))
    asyncio.run(svc.load_media(123))
    assert svc._client.topic_calls == 0
    assert "reply_to" not in svc._client.get_calls[0]


def test_forum_gathers_across_topics_newest_first():
    svc = _service(FakeClient(forum=True, topic_ids=[7, 9]))
    items = asyncio.run(svc.load_media(123, limit=200))
    assert svc._client.topic_calls == 1
    replies = [c.get("reply_to") for c in svc._client.get_calls]
    assert set(replies) == {7, 9}
    # newest-first by message id
    ids = [it.message_id for it in items]
    assert ids == sorted(ids, reverse=True)


def test_forum_without_topics_falls_back_to_plain():
    svc = _service(FakeClient(forum=True, topic_ids=[]))
    items = asyncio.run(svc.load_media(123))
    assert svc._client.topic_calls == 1
    assert items[0].title == "plain.mp4"


def test_topic_load_passes_reply_to_and_offset():
    svc = _service(FakeClient())
    asyncio.run(svc.load_media(123, offset=7, topic_id=9))
    c = svc._client.get_calls[0]
    assert c["reply_to"] == 9
    assert c["offset_id"] == 7
    # topic path must not consult get_entity/forum detection
    assert svc._client.topic_calls == 0


def test_load_topics_returns_id_title_pairs():
    svc = _service(FakeClient(forum=True, topic_ids=[3, 4]))
    topics = asyncio.run(svc.load_topics(123))
    assert topics == [(3, "T3"), (4, "T4")]



# ---------------- streaming (instant playback) design ----------------
def test_play_playlist_streams_without_downloading(tmp_path):
    """play_playlist must register localhost URLs and NOT download bytes; the
    file is served by the FileServer as it grows, so playback can start before
    the download finishes."""
    import asyncio
    import urllib.request
    from pathlib import Path

    svc = TelegramService(Config())
    # Keep the registered cache file inside tmp so the test is hermetic and not
    # affected by any previously cached clips.
    def _cache_path(cid, mid, title):
        return tmp_path / f"{cid}_{mid}.mp4"
    svc._cache_path = _cache_path
    svc._file_server.start()
    try:
        items = [(123, 9, "clip.mp4", 1000)]
        entries = asyncio.run(svc._register_playlist_async(items))
        assert entries, "expected at least one entry"
        url = entries[0][0]
        assert url.startswith("http://127.0.0.1"), url
        # No download happened: the registered path does not exist yet.
        token = url.rsplit("/", 1)[1]
        path = svc._file_server.tokens[token][0]
        assert not Path(path).exists(), "playlist should not pre-download"
        # Simulate the download writing bytes; the server must now serve them.
        Path(path).write_bytes(b"PARTIAL-DATA")
        with urllib.request.urlopen(url, timeout=5) as resp:
            body = resp.read()
        assert body == b"PARTIAL-DATA"
    finally:
        svc._file_server.close()
