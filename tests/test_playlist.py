"""Tests for playlist filter, model and persistence."""

from __future__ import annotations
from types import SimpleNamespace

import json

import pytest

from telegram_player.media import MediaItem
from telegram_player.playlist import (
    PlaylistCollection,
    PlaylistItem,
    PlaylistStore,
    filter_videos,
    classify_media,
)


class FakeAttr:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class FakeFile:
    def __init__(self, name):
        self.name = name


class FakeDoc:
    def __init__(self, mime=None, size=1000, attributes=()):
        self.mime_type = mime
        self.size = size
        self.attributes = attributes


def make_msg(
    id,
    chat_id=1,
    *,
    media="video",
    mime="video/mp4",
    title=None,
    caption="",
    duration=60.0,
    name=None,
):
    from telethon.tl.types import DocumentAttributeVideo

    attributes = [DocumentAttributeVideo(duration=duration, w=0, h=0)] if duration is not None else []
    doc = FakeDoc(mime=mime, attributes=attributes)
    video = doc if media == "video" else None
    file = FakeFile(name)
    msg = FakeAttr(
        id=id,
        chat_id=chat_id,
        media=media,
        document=doc if media in ("video", "document") else None,
        video=video,
        file=file,
        message=caption,
    )
    if title is not None:
        msg.title_override = title
    return msg


def test_only_videos_in_order(tmp_path):
    msgs = [
        make_msg(1, media="video", caption="first clip"),
        make_msg(2, media="photo"),
        make_msg(3, media="audio"),
        make_msg(4, media="document", mime="video/x-matroska", name="movie.mkv"),
        make_msg(5, media=None),
        make_msg(6, media="document", mime="application/pdf", name="notes.pdf"),
        make_msg(7, media="video", caption="", duration=None),
    ]
    items = filter_videos(msgs)
    assert [i.message_id for i in items] == [1, 4, 7]
    assert items[0].title == "first clip"
    assert items[0].duration == 60.0
    assert items[1].title == "movie.mkv"
    assert items[2].title == "Video 7"
    assert items[2].duration is None
    assert items[2].size == 1000


def test_long_caption_truncated():
    items = filter_videos([make_msg(9, media="video", caption="x" * 200)])
    assert len(items[0].title) == 80


def test_media_none_skipped():
    msg = FakeAttr(id=1, chat_id=1, media=None, document=None, video=None, file=FakeFile(None), message="")
    assert filter_videos([msg]) == []


def test_store_roundtrip_and_corrupt(tmp_path):
    p = tmp_path / "playlist.json"
    store = PlaylistStore(p)
    items = [
        PlaylistItem(chat_id=1, message_id=10, title="a", duration=5.5, size=1234),
        PlaylistItem(chat_id=1, message_id=11, title="b"),
    ]
    store.save(items)
    assert PlaylistStore(p).load() == items
    assert json.loads(p.read_text()).__len__() == 2

    p.write_text("{corrupt")
    assert PlaylistStore(p).load() == []


def test_from_dict_defaults():
    assert PlaylistItem.from_dict({"chat_id": 1, "message_id": 2, "title": "t"}).duration is None


def test_webpage_skipped():
    from telethon.tl.types import MessageMediaWebPage

    msg = make_msg(2, media="video")
    msg.media = MessageMediaWebPage(webpage=())
    assert filter_videos([msg]) == []

# ---------------- PlaylistCollection (multi-playlist tabs) ----------------
def _item(mid: int, title: str = "Clip") -> PlaylistItem:
    return PlaylistItem(1, mid, title, 30.0, 1024)


def test_collection_migrates_legacy_single_playlist(tmp_path):
    legacy = tmp_path / "playlist.json"
    legacy.write_text(json.dumps([_item(7).to_dict()]), "utf-8")
    col = PlaylistCollection(tmp_path / "playlists.json", legacy_path=legacy)
    col.load()
    assert col.names() == ["Playlist 1"]
    assert [i.message_id for i in col.items()] == [7]
    assert col.active == 0


def test_collection_create_switch_persist(tmp_path):
    col = PlaylistCollection(tmp_path / "playlists.json")
    col.load()
    assert col.names() == ["Playlist 1"]

    idx = col.new_playlist()                     # auto name
    assert idx == 1 and col.names() == ["Playlist 1", "Playlist 2"]
    col.set_items([_item(1)])
    col.save()

    col.new_playlist("Road trip")
    col.set_items([_item(2), _item(3)])
    col.save()

    reloaded = PlaylistCollection(tmp_path / "playlists.json")
    reloaded.load()
    assert reloaded.names() == ["Playlist 1", "Playlist 2", "Road trip"]
    assert reloaded.active == 2
    assert [i.message_id for i in reloaded.items()] == [2, 3]

    reloaded.active = 1
    reloaded.save()
    reloaded2 = PlaylistCollection(tmp_path / "playlists.json")
    reloaded2.load()
    assert [i.message_id for i in reloaded2.items()] == [1]
    assert reloaded2.name() == "Playlist 2"


def test_collection_rename_and_delete(tmp_path):
    col = PlaylistCollection(tmp_path / "playlists.json")
    col.load()
    col.new_playlist("Temp")
    col.rename(1, "Keepers")
    assert col.names() == ["Playlist 1", "Keepers"]
    new_active = col.delete(1)
    assert new_active == 0
    assert col.names() == ["Playlist 1"]
    # deleting the last playlist leaves a fresh empty one
    new_active = col.delete(0)
    assert col.names() == ["Playlist 1"]
    assert col.items() == []


def test_collection_new_playlist_auto_names_around_collisions(tmp_path):
    col = PlaylistCollection(tmp_path / "playlists.json")
    col.load()
    col.new_playlist("Playlist 3")
    col.new_playlist()                            # should become Playlist 2
    assert col.names() == ["Playlist 1", "Playlist 3", "Playlist 2"]


# ---------------- classify_media (media-type tabs) ----------------
def _m(mid, *, media="video", mime="video/mp4", size=100, dur=None, photo=False,
       animated=False, name="clip.mp4"):
    doc = None
    if not photo:
        attrs = []
        if dur is not None:
            cls = "DocumentAttributeAudio" if mime.startswith("audio/") else "DocumentAttributeVideo"
            attrs.append(SimpleNamespace(cls=cls, duration=dur))
        if animated:
            attrs.append(SimpleNamespace(cls="DocumentAttributeAnimated"))
        doc = SimpleNamespace(mime_type=mime, size=size, attributes=attrs)
    return SimpleNamespace(
        chat_id=1, id=mid,
        media=SimpleNamespace(),  # any non-None, non-WebPage media
        photo=(SimpleNamespace(sizes=[SimpleNamespace(size=size)]) if photo else None),
        document=doc,
        video=(doc if media == "video" else None),
        file=SimpleNamespace(name=name),
        message=f"clip {mid}",
    )


def test_classify_media_kinds():
    msgs = [
        _m(1, media="video", mime="video/mp4", dur=12),
        _m(2, media="audio", mime="audio/mpeg", dur=30),
        _m(3, photo=True),
        _m(4, mime="image/gif"),
        _m(5, mime="application/pdf"),
    ]
    out = classify_media(msgs)
    kinds = {i.message_id: i.kind for i in out}
    assert kinds == {1: "video", 2: "audio", 3: "photo", 4: "gif", 5: "other"}
    # duration + size carried through
    assert [i.duration for i in out if i.message_id == 2][0] == 30
    assert [i.size for i in out if i.message_id == 3][0] == 100


def test_classify_media_skips_webpage_and_empty():
    from telethon.tl.types import MessageMediaWebPage
    wp = _m(1); wp.media = MessageMediaWebPage(webpage=())
    empty = SimpleNamespace(chat_id=1, id=2, media=None, photo=None, document=None)
    assert classify_media([wp, empty]) == []
