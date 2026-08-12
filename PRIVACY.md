# Privacy

Telegram Player is a **local** desktop client. It makes **no telemetry, analytics,
tracking, or third-party network calls** of any kind. The only network traffic it
generates is to **Telegram's own servers** (the same API any Telegram client
uses) in order to log in and fetch the media you choose to play.

## Data stored on your machine

All data lives under the XDG base directories and is owned by you. Nothing is
uploaded anywhere except the Telegram API.

| File | Location | Contents | Sensitivity |
| --- | --- | --- | --- |
| `config.json` | `$XDG_CONFIG_HOME/telegram-player/` | `api_id`, `api_hash`, volume, playback speed, extra mpv args | Low — your Telegram **app credentials** (from my.telegram.org) |
| `telegram.session` | `$XDG_DATA_HOME/telegram-player/` | Telethon auth session (your login key) | **High** — equivalent to a logged-in session; protects it like a password |
| `playlists.json` | `$XDG_CONFIG_HOME/telegram-player/` | Playlist **names** and message metadata (chat id, message id, title, duration, size) | Low — metadata only, **not** media content |
| cache files | `$XDG_CACHE_HOME/telegram-player/` | Downloaded video files you chose to play | Medium — your actual media, retained locally for streaming/playback |

No chat messages, captions, contacts, or media are ever sent to anyone other
than Telegram (to fetch the media you request).

## Network behavior

- **Outbound only** to Telegram (`api.telegram.org`, `web.telegram.org`, etc.).
- **No** connections to ad, analytics, or tracking domains.
- A **local HTTP server** (`127.0.0.1`, random port) serves the growing cache
  file to `mpv` via byte-range requests so playback can start before a download
  finishes. It is **bound to localhost only** and is **token-addressed**, so it
  cannot be reached from the network. No Telegram traffic flows through it.
- Logging goes to stderr at `WARNING` level by default and contains no
  credentials or media content.

## Credentials

- `api_id` / `api_hash` are created by you at
  [my.telegram.org](https://my.telegram.org) and stored **locally only**.
- Login uses Telegram's official QR-code or phone + code + 2FA flow. Your
  password is never stored.

## Recommendations

- Treat `telegram.session` as a secret; do not copy it off your machine or
  commit it to version control.
- To fully clear local data, delete the `telegram-player` directories under your
  XDG `config`, `data`, and `cache` folders, or use **Clear cache** in the UI.
- This is not affiliated with, endorsed by, or sponsored by Telegram.
