"""Playlist model and persistence, plus the video-message filter."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from telethon.tl.types import MessageMediaWebPage

from .media import MediaItem, video_title


@dataclass
class PlaylistItem:
    chat_id: int
    message_id: int
    title: str
    duration: float | None = None
    size: int | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "PlaylistItem":
        return PlaylistItem(
            chat_id=int(d["chat_id"]),
            message_id=int(d["message_id"]),
            title=str(d["title"]),
            duration=d.get("duration"),
            size=d.get("size"),
        )


def _video_duration(m) -> float | None:
    try:
        from telethon.tl.types import DocumentAttributeVideo

        for attr in m.document.attributes or ():
            if isinstance(attr, DocumentAttributeVideo) and attr.duration:
                return float(attr.duration)
    except AttributeError:
        pass
    return None


def filter_videos(messages) -> list[MediaItem]:
    """Reduce a list of Telethon messages to video MediaItems, in input order.

    Accepts anything with the Telethon message attribute surface (tests use
    fakes). Webpage embeds and messages without media are skipped.
    """
    out: list[MediaItem] = []
    for m in messages:
        if m.media is None or isinstance(m.media, MessageMediaWebPage):
            continue
        doc = getattr(m, "document", None)
        video = getattr(m, "video", None)
        mime = getattr(doc, "mime_type", None)
        if not (video is not None or (doc is not None and mime is not None and mime.startswith("video/"))):
            continue
        out.append(
            MediaItem(
                chat_id=m.chat_id,
                message_id=m.id,
                title=video_title(m),
                duration=_video_duration(m),
                size=getattr(doc, "size", None),
            )
        )
    return out


class PlaylistStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.items: list[PlaylistItem] = []

    def load(self) -> list[PlaylistItem]:
        if not self.path.is_file():
            self.items = []
            return self.items
        try:
            data = json.loads(self.path.read_text("utf-8"))
            self.items = [PlaylistItem.from_dict(d) for d in data]
        except (ValueError, KeyError, TypeError, OSError):
            self.items = []
        return self.items

    def save(self, items: list[PlaylistItem] | None = None) -> None:
        if items is not None:
            self.items = items
        tmp = self.path.with_suffix(".tmp")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps([i.to_dict() for i in self.items], indent=2), "utf-8")
        tmp.replace(self.path)


class PlaylistCollection:
    """Multiple named playlists persisted as one JSON document.

    The first playlist is always present (the app guarantees at least one).
    Legacy single-playlist files (the old PlaylistStore layout) are migrated
    into "Playlist 1" on first load, so upgrading never loses a saved queue.
    """

    def __init__(self, path: Path, legacy_path: Path | None = None):
        self.path = Path(path)
        self.legacy_path = Path(legacy_path) if legacy_path else None
        self.playlists: list[dict] = []  # [{"name": str, "items": [PlaylistItem]}]
        self.active: int = 0

    # ---------------- persistence ----------------

    def load(self) -> None:
        data = None
        if self.path.is_file():
            try:
                data = json.loads(self.path.read_text("utf-8"))
            except (ValueError, TypeError, OSError):
                data = None
        if isinstance(data, dict) and isinstance(data.get("playlists"), list):
            self.playlists = [
                {"name": str(p.get("name", f"Playlist {i + 1}")),
                 "items": [PlaylistItem.from_dict(d) for d in p.get("items", [])]}
                for i, p in enumerate(data["playlists"])
            ]
            self.active = int(data.get("active", 0))
        else:
            # Legacy single playlist (or nothing): migrate if there were items.
            legacy: list[PlaylistItem] = []
            if self.legacy_path and self.legacy_path.is_file():
                legacy = PlaylistStore(self.legacy_path).load()
            self.playlists = [{"name": "Playlist 1", "items": legacy}]
            self.active = 0
        self._normalize()

    def _normalize(self) -> None:
        if not self.playlists:
            self.playlists = [{"name": "Playlist 1", "items": []}]
        self.active = max(0, min(self.active, len(self.playlists) - 1))

    def save(self) -> None:
        self._normalize()
        tmp = self.path.with_suffix(".tmp")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps({
            "active": self.active,
            "playlists": [
                {"name": p["name"], "items": [i.to_dict() for i in p["items"]]}
                for p in self.playlists
            ],
        }, indent=2), "utf-8")
        tmp.replace(self.path)

    # ---------------- active playlist access ----------------

    def names(self) -> list[str]:
        return [p["name"] for p in self.playlists]

    def name(self) -> str:
        return self.playlists[self.active]["name"]

    def items(self) -> list[PlaylistItem]:
        return self.playlists[self.active]["items"]

    def set_items(self, items: list[PlaylistItem]) -> None:
        self.playlists[self.active]["items"] = list(items)

    def add_item(self, item: PlaylistItem) -> None:
        self.playlists[self.active]["items"].append(item)

    # ---------------- tab management ----------------

    def new_playlist(self, name: str | None = None) -> int:
        """Append a fresh playlist and activate it. Returns its index."""
        taken = set(self.names())
        if name is None or not name.strip():
            n = 1
            while f"Playlist {n}" in taken:
                n += 1
            name = f"Playlist {n}"
        self.playlists.append({"name": name, "items": []})
        self.active = len(self.playlists) - 1
        return self.active

    def delete(self, index: int) -> int:
        """Remove a playlist (never the last one) and return the new active
        index. Deleting the only playlist leaves a fresh empty one active."""
        if not (0 <= index < len(self.playlists)):
            return self.active
        del self.playlists[index]
        if self.active >= len(self.playlists):
            self.active = len(self.playlists) - 1
        self._normalize()
        return self.active

    def rename(self, index: int, name: str) -> None:
        if 0 <= index < len(self.playlists) and name.strip():
            self.playlists[index]["name"] = name.strip()