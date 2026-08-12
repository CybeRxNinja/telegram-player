"""Telegram Player theme.

Design language (derived from the open-design design-systems catalog):
- Spotify: media-player structure — near-black theater surfaces (#121212 /
  #181818 / #1f1f1f), content-first darkness, pill CTAs, one functional accent.
- Linear: precision — ultra-thin white-alpha borders, subtle luminance
  hierarchy instead of color variation.
Brand accent stays Telegram blue so the app remains recognizable.
"""

from __future__ import annotations

from PyQt6.QtGui import QColor, QFont, QPalette

# --- design tokens -----------------------------------------------------------
BG = "#121212"            # deepest canvas
SURFACE = "#181818"       # panels / lists
SURFACE_2 = "#1F1F1F"     # buttons / raised surfaces
SURFACE_3 = "#2A2A2A"     # hover
BORDER = "rgba(255, 255, 255, 0.08)"
BORDER_STRONG = "#3D3D3D"
FG = "#FFFFFF"
MUTED = "#B3B3B3"
META = "#7A7A7A"
ACCENT = "#2AABEE"        # Telegram blue
ACCENT_HOVER = "#3EBAF0"
ACCENT_PRESSED = "#1F8EC7"
DANGER = "#F3727F"
WARN = "#FFA42B"
SUCCESS = "#1ED760"

FONT_FAMILY = '"Noto Sans", "Segoe UI", "DejaVu Sans", sans-serif'
MONO_FAMILY = '"Hack", "Noto Sans Mono", "DejaVu Sans Mono", monospace'


def uppercase_button(btn, spacing: float = 0.8) -> None:
    """Spotify-style CTA label: uppercase + slight letterspacing."""
    btn.setText(btn.text().upper())
    f = btn.font()
    f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, spacing)
    btn.setFont(f)


def apply_theme(app) -> None:
    """Install the QSS + palette + base font on a QApplication."""
    app.setStyleSheet(THEME_QSS)
    pal = app.palette()
    pal.setColor(QPalette.ColorRole.Link, QColor(ACCENT))
    pal.setColor(QPalette.ColorRole.LinkVisited, QColor(ACCENT_HOVER))
    app.setPalette(pal)
    f = app.font()
    f.setFamily("Noto Sans")
    f.setPointSize(10)  # ~13px
    app.setFont(f)


THEME_QSS = f"""
QWidget {{
    background-color: {BG};
    color: {FG};
    font-family: {FONT_FAMILY};
    font-size: 13px;
}}
QMainWindow, QDialog {{
    background-color: {BG};
}}

/* ---------- buttons ---------- */
QPushButton {{
    background-color: transparent;
    border: none;
    border-radius: 8px;
    padding: 6px 14px;
    color: {MUTED};
    font-weight: 600;
}}
QPushButton:hover {{ color: {FG}; background-color: {SURFACE_2}; }}
QPushButton:pressed {{ background-color: {SURFACE_3}; }}
QPushButton:disabled {{ color: #4A4A4A; }}
QPushButton:checked {{ color: {FG}; background-color: {SURFACE_3}; }}
QPushButton#accent {{
    background-color: {ACCENT};
    color: #06121C;
    border-radius: 16px;
    padding: 7px 22px;
    font-weight: 700;
}}
QPushButton#accent:hover {{ background-color: {ACCENT_HOVER}; color: #06121C; }}
QPushButton#accent:pressed {{ background-color: {ACCENT_PRESSED}; }}
QPushButton#accent:disabled {{ background-color: #2A4A5E; color: #6A8CA0; }}
QPushButton#accentRound {{
    background-color: {ACCENT};
    color: #06121C;
    border-radius: 14px;
    padding: 0px;
}}
QPushButton#accentRound:hover {{ background-color: {ACCENT_HOVER}; }}
QPushButton#accentRound:pressed {{ background-color: {ACCENT_PRESSED}; }}
QPushButton#danger {{ color: {DANGER}; }}
QPushButton#danger:hover {{ background-color: rgba(243, 114, 127, 0.12); color: {DANGER}; }}
QPushButton#transport {{
    padding: 2px;
    border-radius: 8px;
    font-weight: 400;
}}
QPushButton#transportPlay {{
    background-color: {ACCENT};
    border-radius: 23px;
    color: #06121C;
    font-size: 20px;
    font-weight: 700;
    padding: 0;
}}
QPushButton#transportPlay:hover {{ background-color: {ACCENT_HOVER}; }}
QPushButton#transportPlay:pressed {{ background-color: {ACCENT_PRESSED}; }}
QPushButton#transportPill {{
    background-color: {SURFACE_2};
    border: 1px solid {BORDER_STRONG};
    border-radius: 15px;
    padding: 2px 10px;
    font-weight: 600;
}}
QPushButton#transportPill:hover {{ border-color: {ACCENT}; color: {FG}; }}

/* ---------- inputs ---------- */
QLineEdit, QTextEdit, QSpinBox {{
    background-color: {SURFACE};
    border: 1px solid {BORDER_STRONG};
    border-radius: 8px;
    padding: 6px 10px;
    color: {FG};
    selection-background-color: {ACCENT};
    selection-color: #06121C;
}}
QLineEdit:focus, QTextEdit:focus {{ border: 1px solid {ACCENT}; }}
QLineEdit::placeholder {{ color: {META}; }}

/* ---------- labels ---------- */
QLabel {{ background: transparent; }}
QLabel#title {{ font-size: 14px; font-weight: 700; }}
QLabel#muted {{ color: {MUTED}; }}
QLabel#meta {{ color: {META}; font-size: 12px; }}
QLabel#brand {{
    color: {FG};
    font-size: 13px;
    font-weight: 800;
    letter-spacing: 1.6px;
}}
QLabel#panelHeader {{
    color: {META};
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 1.2px;
    padding: 2px 2px 4px 2px;
}}
QLabel#empty {{
    color: {META};
    font-size: 14px;
    padding: 24px;
}}
QLabel#error {{ color: {DANGER}; }}
QLabel#overlayState {{
    color: {FG};
    font-size: 26px;
    font-weight: 600;
    background: transparent;
}}
QLabel#overlayTitle {{
    color: {MUTED};
    font-size: 15px;
    background: transparent;
}}

/* ---------- lists / tables ---------- */
QListWidget, QTableWidget {{
    background-color: {SURFACE};
    border: none;
    border-radius: 8px;
    outline: 0;
}}
QListWidget::item, QTableWidget::item {{
    padding: 7px 10px;
    border-bottom: 1px solid rgba(255, 255, 255, 0.04);
}}
QListWidget::item:hover, QTableWidget::item:hover {{ background-color: {SURFACE_2}; }}
QListWidget::item:selected, QTableWidget::item:selected {{
    background-color: {SURFACE_3};
    color: {FG};
}}
QListWidget::item.playing, QTableWidget::item.playing {{
    background-color: rgba(42, 171, 238, 0.14);
    border-left: 3px solid {ACCENT};
    color: {FG};
}}
QTableWidget {{ gridline-color: rgba(255, 255, 255, 0.04); }}
QHeaderView::section {{
    background-color: {SURFACE};
    border: none;
    border-bottom: 1px solid {BORDER};
    padding: 6px 10px;
    color: {META};
    font-size: 11px;
    font-weight: 700;
}}

/* ---------- combo box ---------- */
QComboBox {{
    background-color: {SURFACE};
    border: 1px solid {BORDER_STRONG};
    border-radius: 8px;
    padding: 5px 10px;
    color: {FG};
}}
QComboBox:hover {{ border-color: #555555; }}
QComboBox:focus {{ border-color: {ACCENT}; }}
QComboBox::drop-down {{ border: none; width: 24px; }}
QComboBox QAbstractItemView {{
    background-color: {SURFACE_2};
    border: 1px solid {BORDER_STRONG};
    border-radius: 8px;
    selection-background-color: rgba(42, 171, 238, 0.25);
    selection-color: {FG};
    outline: 0;
}}

/* ---------- scrollbars ---------- */
QScrollBar:vertical, QScrollBar:horizontal {{
    background: transparent;
    width: 8px; height: 8px;
    margin: 0;
}}
QScrollBar::handle:vertical, QScrollBar::handle:horizontal {{
    background: #3A3A3A;
    border-radius: 4px;
    min-height: 24px; min-width: 24px;
}}
QScrollBar::handle:hover {{ background: #4A4A4A; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

/* ---------- structure ---------- */
QSplitter::handle {{ background-color: {BG}; }}
QWidget#transportBar {{
    background-color: #161616;
    border-top: 1px solid {BORDER};
}}
QStatusBar {{ background: {SURFACE}; color: {META}; }}

/* ---------- menus / tooltips / tabs ---------- */
QMenu {{
    background-color: {SURFACE_2};
    border: 1px solid {BORDER_STRONG};
    border-radius: 8px;
    padding: 4px;
}}
QMenu::item {{ padding: 6px 28px 6px 12px; border-radius: 6px; color: {MUTED}; }}
QMenu::item:selected {{ background-color: rgba(42, 171, 238, 0.22); color: {FG}; }}
QMenu::item:disabled {{ color: #4A4A4A; }}
QMenu::separator {{ height: 1px; background: {BORDER}; margin: 4px 8px; }}
QToolTip {{
    background-color: {SURFACE_2};
    color: {FG};
    border: 1px solid {BORDER_STRONG};
    border-radius: 6px;
    padding: 5px 9px;
}}
QTabWidget::pane {{ border: 1px solid {BORDER_STRONG}; border-radius: 8px; top: -1px; }}
QTabBar::tab {{
    background: transparent;
    padding: 7px 16px;
    border-bottom: 2px solid transparent;
    color: {MUTED};
    font-weight: 600;
}}
QTabBar::tab:selected {{ color: {FG}; border-bottom: 2px solid {ACCENT}; }}
QTabBar::tab:hover {{ color: {FG}; }}
QPushButton#tabClose {{
    background: transparent;
    border: none;
    border-radius: 9px;
    color: {MUTED};
    font-size: 11px;
    padding: 0;
}}
QPushButton#tabClose:hover {{
    background: rgba(243, 114, 127, 0.16);
    color: {DANGER};
}}

/* ---------- sliders ---------- */
QSlider::groove:horizontal {{
    background: #3D3D3D;
    height: 4px;
    border-radius: 2px;
}}
QSlider::sub-page:horizontal {{ background: {ACCENT}; border-radius: 2px; }}
QSlider::handle:horizontal {{
    background: #FFFFFF;
    width: 14px; border-radius: 7px;
    margin-top: -5px; margin-bottom: -5px;
}}
QSlider::handle:horizontal:hover {{ background: {ACCENT_HOVER}; }}
QSlider::groove:vertical {{
    background: #3D3D3D;
    width: 4px;
    border-radius: 2px;
}}
QSlider::sub-page:vertical {{ background: {ACCENT}; border-radius: 2px; }}
QSlider::handle:vertical {{
    background: #FFFFFF;
    height: 14px; border-radius: 7px;
    margin-left: -5px; margin-right: -5px;
}}
"""
