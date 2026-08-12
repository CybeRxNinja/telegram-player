# Changelog

All notable changes to Telegram Player are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [0.1.17] - 2026-08-12

### Added
- **Media-type filter tabs.** The media panel now has a tab bar — **Videos /
  Audio / Photos & GIF / Other** — that filters the loaded chat's media by
  type. This makes it easy to build a playlist per category (switch tab, then
  "▶ Play" / "+ Add to playlist" acts on that whole category) and to find the
  exact clip you want without scrolling a mixed list.
- **Download-only "Other" tab.** Files mpv cannot play (PDFs, archives, docs,
  …) live in the **Other** tab, whose play button becomes **Download** and
  triggers a local download instead of attempting playback.
- **Truly instant playback.** The localhost file server now streams an open
  range the moment any bytes exist, so playback starts before the whole file
  has downloaded (previously it waited for the complete file / a 60s stall).

### Fixed
- **Missing-glyph buttons.** The "Add to playlist" and "Create a new playlist"
  (`+`) buttons were showing tofu / blank squares — the fullwidth `+` (U+FF0B)
  rendered as zero ink, and the round `+` button's accent padding clipped the
  glyph entirely. Both now render a clean ASCII `+` (and the round button uses a
  zero-padding accent style).

## [0.1.16] - 2026-08-12

### Added
- **Multiple playlists with tabs.** The playlist panel now has a tab bar — one
  tab per saved playlist, with a `+` button to create another, a close (✕)
  button to delete, and double-click to rename. The active tab is persisted
  and restored on launch (`playlists.json`). "▶ Play" / "+ Add to playlist"
  always target the active tab. Legacy single-playlist files are migrated
  automatically into "Playlist 1".
- **Tap-to-seek.** Click or tap anywhere on the seek (or volume) bar to jump
  straight to that position; the handle snaps to the cursor and a seek is
  triggered on release.
- **Fullscreen cursor auto-hide.** In fullscreen the mouse cursor and transport
  fade out after ~3.5s of inactivity and reappear on any movement (including
  hovering the bottom band). Windowed mode is unaffected.
- **Centered transport glyphs.** Play/pause/next/prev/stop/fullscreen icons are
  now measured against their real ink box and centered exactly, so the icon no
  longer sits low or jumps when it swaps between ▶ and ⏸.

### Changed
- Reworked the dark theme (Spotify/Linear-inspired) with explicit design
  tokens, consistent borders, themed scrollbars, menus, tooltips, comboboxes
  and sliders.
- Replaced the native `QTabBar` close button (which rendered inconsistently on
  the dark theme) with a custom-drawn, theme-consistent ✕.

### Fixed
- Slider `sliderReleased` could fire twice (and over-seek); now emits once.
- `playlists_path` property wiring.

## [0.1.15]
- Transport auto-hide and focus handling restricted to fullscreen-only.
- Paused-state property key + `observe_property` for instant glyph updates.
- Button padding/centering and word-wrapped list labels.

## [0.1.14]
- Spotify-like dark theme, empty-state overlays, mpv EOF handling.

## [0.1.0]
- Initial release: Telethon login (QR / phone+2FA), chat & media browsing,
  byte-range local HTTP streaming, mpv embedding via `--wid`, playlist.
