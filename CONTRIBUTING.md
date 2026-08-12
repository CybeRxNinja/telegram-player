# Contributing

Thanks for your interest in improving Telegram Player!

## Development setup
```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
pip install pytest pyinstaller meson
# tests need an X server (xcb); under CI/headless use xvfb-run
pytest
```

## Guidelines
- Keep `telethon`, `PyQt6` and `segno` pinned in `pyproject.toml`.
- UI lives under `telegram_player/ui/`; the theme tokens are centralized in
  `telegram_player/ui/theme.py` — add new colors there, not as inline
  stylesheets.
- The player embeds mpv into a native X window, so the UI **must** run on the
  `xcb` Qt platform (XWayland is fine). Wayland-native embedding is not
  supported.
- Add/extend tests under `tests/`. Coverage includes unit tests (mocked mpv)
  and real-display e2e checks (see `tests/test_fullscreen.py`,
  `tests/test_mpv_embed.py`).

## Reporting bugs
Open an issue with: app version, distro, Qt/mpv versions, and reproduction
steps. See [SECURITY.md](SECURITY.md) for vulnerabilities.
