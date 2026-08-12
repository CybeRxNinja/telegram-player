"""In-app update checker + self-updater for the Telegram Player AppImage.

At a fixed interval (and once shortly after launch) this checks GitHub
Releases for a newer build:

* the **stable** ``latest`` release (compared by semantic version), and
* the rolling **``nightly``** release (compared by asset build time against the
  running AppImage's mtime).

When an update is found it shows a small banner. For AppImage runs the
"Update" action downloads the new ``.AppImage`` and replaces the running file
in place (then relaunches); otherwise it opens the release page in a browser.

All network access is best-effort: any failure silences the check rather than
interrupting the user.
"""
from __future__ import annotations

import os
import sys
import time
import json
import threading
from dataclasses import dataclass
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

from PyQt6.QtCore import QObject, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices


REPO = "CybeRxNinja/telegram-player"
API = f"https://api.github.com/repos/{REPO}"
RELEASES_PAGE = f"https://github.com/{REPO}/releases"


@dataclass
class ReleaseInfo:
    tag: str
    name: str
    url: str
    prerelease: bool
    published_at: str
    asset_name: str
    asset_url: str

    @property
    def is_appimage(self) -> bool:
        return self.asset_name.lower().endswith(".appimage")

    def __str__(self) -> str:  # for safe logging / display
        return f"{self.name} ({self.tag})"


def current_version() -> str:
    try:
        from importlib.metadata import version
        return version("telegram-player")
    except Exception:
        return "0.0.0"


def _get_json(url: str, timeout: float = 10.0):
    req = Request(
        url,
        headers={
            "User-Agent": "telegram-player-updater",
            "Accept": "application/vnd.github+json",
        },
    )
    with urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def parse_version(tag: str):
    """Return a 3-int tuple for semver tags, or None for non-semver (nightly)."""
    t = (tag or "").lower().lstrip("v").strip()
    if t in ("nightly", "rolling", ""):
        return None
    parts: list[int] = []
    for piece in t.split("."):
        num = ""
        for ch in piece:
            if ch.isdigit():
                num += ch
            else:
                break
        parts.append(int(num) if num else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


def is_newer(tag: str, current: str) -> bool:
    a, b = parse_version(tag), parse_version(current)
    if a is None or b is None:
        return False
    return a > b


def _appimage_asset(release: dict):
    for a in release.get("assets", []):
        if a.get("name", "").lower().endswith(".appimage"):
            return a
    return None


def fetch_release(kind: str = "latest") -> ReleaseInfo | None:
    """Fetch a release by kind: 'latest' (non-prerelease) or 'nightly' (by tag)."""
    url = f"{API}/releases/tags/nightly" if kind == "nightly" else f"{API}/releases/latest"
    data = _get_json(url)
    asset = _appimage_asset(data)
    if asset is None:
        return None
    return ReleaseInfo(
        tag=data.get("tag_name", ""),
        name=data.get("name") or data.get("tag_name", ""),
        url=data.get("html_url", ""),
        prerelease=bool(data.get("prerelease")),
        published_at=data.get("published_at", ""),
        asset_name=asset.get("name", ""),
        asset_url=asset.get("browser_download_url", ""),
    )


def appimage_path() -> str | None:
    """Path to the running AppImage, if we are an AppImage."""
    p = os.environ.get("APPIMAGE")
    if p and p.lower().endswith(".appimage") and os.path.exists(p):
        return p
    return None


def _nightly_newer(nightly: ReleaseInfo, ap: str) -> bool:
    try:
        from datetime import datetime, timezone

        local_mtime = os.path.getmtime(ap)
        remote_ts = (
            datetime.strptime(nightly.published_at, "%Y-%m-%dT%H:%M:%SZ")
            .replace(tzinfo=timezone.utc)
            .timestamp()
        )
        return remote_ts > local_mtime + 1
    except Exception:
        return False


class UpdateChecker(QObject):
    """Periodically checks for updates and reports via Qt signals."""

    update_available = pyqtSignal(ReleaseInfo)
    no_update = pyqtSignal()
    error = pyqtSignal(str)
    download_progress = pyqtSignal(int, int)  # downloaded, total (bytes)

    def __init__(self, parent=None, interval_ms: int = 3 * 60 * 60 * 1000, enabled: bool = True):
        super().__init__(parent)
        self.interval_ms = interval_ms
        self.enabled = enabled
        self._timer = QTimer(self)
        self._timer.setSingleShot(False)
        self._timer.timeout.connect(self.check)

    def start(self, initial_delay_ms: int = 5000) -> None:
        if not self.enabled:
            return
        self._timer.start(self.interval_ms)
        QTimer.singleShot(initial_delay_ms, self.check)

    def stop(self) -> None:
        self._timer.stop()

    def check(self) -> None:
        if not self.enabled:
            return
        try:
            current = current_version()
            rel = fetch_release("latest")
            if rel is not None and is_newer(rel.tag, current):
                self.update_available.emit(rel)
                return
            ap = appimage_path()
            if ap is not None:
                nightly = fetch_release("nightly")
                if nightly is not None and _nightly_newer(nightly, ap):
                    self.update_available.emit(nightly)
                    return
            self.no_update.emit()
        except (URLError, HTTPError, ValueError, KeyError) as e:
            self.error.emit(str(e))

    def apply_update(self, info: ReleaseInfo, on_done=None) -> None:
        """Download + apply in a worker thread (keeps the UI responsive)."""
        threading.Thread(
            target=self._apply, args=(info, on_done), daemon=True
        ).start()

    def _apply(self, info: ReleaseInfo, on_done) -> None:
        import tempfile

        ap = appimage_path()
        try:
            fd, tmp = tempfile.mkstemp(suffix=".appimage")
            os.close(fd)
            req = Request(info.asset_url, headers={"User-Agent": "telegram-player-updater"})
            with urlopen(req, timeout=600) as r:
                total = int(r.headers.get("Content-Length", "0") or "0")
                done = 0
                with open(tmp, "wb") as f:
                    while True:
                        chunk = r.read(1 << 20)
                        if not chunk:
                            break
                        f.write(chunk)
                        done += len(chunk)
                        self.download_progress.emit(done, total)
            os.chmod(tmp, 0o755)
            if ap:
                # Replace the running AppImage and relaunch it in place.
                os.replace(tmp, ap)
                os.chmod(ap, 0o755)
                os.execv(ap, [ap])
            else:
                QDesktopServices.openUrl(QUrl(info.url))
                if on_done:
                    on_done(None)
        except Exception as e:  # noqa: BLE001 - surface to UI
            try:
                os.unlink(tmp)
            except OSError:
                pass
            if on_done:
                on_done(str(e))
