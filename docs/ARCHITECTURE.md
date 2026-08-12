# Architecture

A single `QApplication` drives a `MainWindow`. A `TelegramService` (Telethon)
runs on its own asyncio loop thread; a `MpvCtl` talks to an `mpv` process over
JSON IPC; and a `FileServer` streams cached files to mpv over localhost HTTP.

```
┌─────────────── QApplication (xcb) ───────────────┐
│  MainWindow                                       │
│   ├─ chat list / media table / playlist tabs      │
│   ├─ transport (glyph buttons, seek, volume)      │
│   └─ player_container (native X window)           │
│        ▲ --wid                                    │
│  TelegramService ──► Telethon ──► Telegram API    │
│        │ downloads to cache                        │
│        ▼                                           │
│  FileServer (127.0.0.1, byte-range) ──► mpv ◄──────┘
└────────────────────────────────────────────────────┘
```

## mpv embedding
mpv is launched with `--wid <window id>` of a dedicated `QWidget` that has
`WA_PaintOnScreen` + `WA_NativeWindow` so Qt never paints over mpv's frames. A
self-verification step confirms mpv actually painted into our window (and not its
own) before playback starts — see `tests/test_mpv_embed.py`.

## Streaming
`FileServer` registers each cache file with a random token and serves it at
`http://127.0.0.1:<port>/v/<token>` with byte-range support. It blocks each
request until the requested bytes exist (or a stall timeout), giving true
"play while downloading" without mpv knowing the file is partial. It is bound to
localhost and token-addressed, so it is not network-reachable.

## Threading
- Telethon runs on a dedicated loop thread. The UI bridges to it via
  `run_coroutine_threadsafe` (`_await` for blocking calls, `_call`/`_spawn` for
  callbacks).
- mpv IPC runs on its own thread; state is polled into the Qt thread.

## Theme
All visual tokens live in `telegram_player/ui/theme.py` (colors, borders, the
QSS string). Widgets reference tokens or object names (`#accent`, `#transport`,
`#tabClose`, …) rather than inline colors, so the look stays consistent.

## Playlists
`playlists.json` holds `{active, playlists:[{name, items}]}`. The legacy single
`playlist.json` is migrated to "Playlist 1" on first load. The tab bar mirrors
the collection; a custom-drawn close button (muted, red-on-hover) replaces the
native one for consistent dark-theme rendering.
