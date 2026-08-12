"""Shared data shapes between the Telegram service, playlist and UI."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class MediaItem:
    chat_id: int
    message_id: int
    title: str
    duration: float | None = None
    size: int | None = None
    kind: str = "video"  # video | audio | photo | gif | other


@dataclass
class ChatInfo:
    id: int
    title: str
    kind: str  # "channel" | "group"
    forum: bool = False


@dataclass
class MediaHandle:
    doc: object  # telethon types.Document — iter_download accepts it directly
    size: int
    title: str


def video_title(m) -> str:
    """Best-effort title for a Telethon message (or test fake)."""
    try:
        name = (m.file.name or "").strip()
        if name:
            return name
    except AttributeError:
        pass
    caption = (getattr(m, "message", None) or "").strip()
    if caption:
        first = next((line.strip() for line in caption.splitlines() if line.strip()), "")
        return first[:80] or f"Video {m.id}"
    return f"Video {m.id}"