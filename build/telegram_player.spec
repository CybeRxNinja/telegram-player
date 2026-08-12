"""PyInstaller spec for the Telegram Player onedir bundle."""

from __future__ import annotations

from pathlib import Path

from PyInstaller.utils.hooks import collect_all

HERE = Path(SPEC).resolve().parent if "SPEC" in globals() else Path.cwd()
ROOT = HERE.parent  # repo root

# Telethon lazily imports many TL modules; collect the whole package so nothing
# is missed at runtime.
telethon_datas, telethon_bins, telethon_hidden = collect_all("telethon")

a = Analysis(
    [str(ROOT / "telegram_player" / "__main__.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=telethon_datas,
    hiddenimports=[
        "telethon",
        "segno",
        "telegram_player.ui",
    ] + list(telethon_hidden),
    excludes=[],
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="telegram_player",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_tracebacks=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    telethon_bins,
    strip=False,
    upx=False,
    name="telegram_player",
)