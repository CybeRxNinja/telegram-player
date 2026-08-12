"""Tests for the in-app update checker (no real network)."""
from __future__ import annotations

import time
import os
from pathlib import Path

import telegram_player.updater as updater
from telegram_player.updater import (
    ReleaseInfo,
    appimage_path,
    current_version,
    fetch_release,
    is_newer,
    parse_version,
)


def test_parse_version():
    assert parse_version("v0.1.17") == (0, 1, 17)
    assert parse_version("0.1.17") == (0, 1, 17)
    assert parse_version("1.2") == (1, 2, 0)
    assert parse_version("nightly") is None
    assert parse_version("rolling") is None
    assert parse_version("") is None


def test_is_newer():
    assert is_newer("v0.1.18", "0.1.17") is True
    assert is_newer("v0.1.17", "0.1.17") is False
    assert is_newer("v0.1.16", "0.1.17") is False
    # non-semver (nightly) tags never win a version comparison
    assert is_newer("nightly", "0.1.17") is False


def test_current_version_is_semver():
    v = current_version()
    assert parse_version(v) is not None


def test_fetch_release_parses_asset(monkeypatch):
    payload = {
        "tag_name": "v0.1.18",
        "name": "v0.1.18",
        "html_url": "https://github.com/x/telegram-player/releases/tag/v0.1.18",
        "prerelease": False,
        "published_at": "2026-08-12T10:00:00Z",
        "assets": [
            {"name": "Telegram-Player-0.1.18-x86_64.AppImage",
             "browser_download_url": "https://example/Telegram-Player-0.1.18-x86_64.AppImage"},
        ],
    }

    def fake_get_json(url, timeout=10.0):
        return payload

    monkeypatch.setattr(updater, "_get_json", fake_get_json)
    info = fetch_release("latest")
    assert isinstance(info, ReleaseInfo)
    assert info.tag == "v0.1.18"
    assert info.asset_name.endswith(".AppImage")
    assert info.asset_url.endswith(".AppImage")


def test_fetch_release_skips_non_appimage(monkeypatch):
    monkeypatch.setattr(
        updater, "_get_json",
        lambda url, timeout=10.0: {"tag_name": "v0.1.18", "assets": []},
    )
    assert fetch_release("latest") is None


def test_nightly_newer_by_mtime(monkeypatch, tmp_path):
    ap = tmp_path / "Telegram-Player.AppImage"
    ap.write_bytes(b"x")
    old = time.time() - 1000
    os.utime(str(ap), (old, old))

    def fake_appimage_path():
        return str(ap)

    monkeypatch.setattr(updater, "appimage_path", fake_appimage_path)
    nightly = ReleaseInfo(
        tag="nightly", name="nightly", url="u",
        prerelease=True, published_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time())),
        asset_name="Telegram-Player-nightly-x86_64.AppImage", asset_url="au",
    )
    assert updater._nightly_newer(nightly, str(ap)) is True

    # local build newer than the remote asset -> not newer
    os.utime(str(ap), (time.time(), time.time()))
    assert updater._nightly_newer(nightly, str(ap)) is False


def test_appimage_path_only_when_env_set(monkeypatch, tmp_path):
    monkeypatch.delenv("APPIMAGE", raising=False)
    assert appimage_path() is None
    ap = tmp_path / "Telegram-Player-x86_64.AppImage"
    ap.write_bytes(b"x")
    monkeypatch.setenv("APPIMAGE", str(ap))
    assert appimage_path() == str(ap)
