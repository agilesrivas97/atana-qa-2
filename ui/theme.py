"""
ui/theme.py
============
Shared palette/typography for the CustomTkinter panel (ui/panel_app.py,
ui/config_panel.py, ui/totp_tool.py) — one place for what used to be
scattered hardcoded hex colors across all three files.

Single appearance mode, committed to deliberately: the panel always runs in
light mode (set once here, on import). Properly supporting both light and
dark would mean maintaining a light AND dark variant of every color below —
not worth it for a small internal ops tool with one deliberate look.

Two surfaces stay dark on purpose even in light mode — the top header bar
and the event log — same as the original plain-Tkinter panel always did
(a light app with a dark "terminal" strip is a very standard look). Text
drawn on those two needs TEXT_ON_DARK, not TEXT — see their usage in
ui/panel_app.py.
"""

import customtkinter as ctk

ctk.set_appearance_mode("light")
ctk.set_default_color_theme("blue")

# ── Palette ─────────────────────────────────────────────────────────────────
PRIMARY        = "#1971c2"
PRIMARY_HOVER  = "#155e9c"   # darker on hover — the right direction for a light UI
SUCCESS        = "#2f9e44"
WARNING        = "#e8590c"
WARNING_HOVER  = "#c2410c"
DANGER         = "#e03131"
DANGER_HOVER   = "#c92a2a"
NEUTRAL        = "#e9ecef"   # light gray secondary-button fill
NEUTRAL_HOVER  = "#dee2e6"

TEXT           = "#1a1a2e"   # default text — dark, for the light background
TEXT_DIM       = "#5c5f66"
TEXT_DIM_2     = "#adb5bd"
TEXT_ON_DARK   = "#e6e6e6"   # for the two surfaces that stay dark (header, event log)

SURFACE        = "#1a1a2e"   # deliberately dark: header bar, event log background
SURFACE_ALT    = "#2d2d5e"   # deliberately dark: buttons sitting on the header
TABLE_BG       = "#ffffff"   # the (kept-ttk) Treeview's own background
TABLE_HEADER_BG = "#f1f3f5"
CARD           = "#fff3cd"   # intervention alert banner — standard light "warning" tint
CARD_WHITE     = "#ffffff"   # individual alert row card, against the CARD-tinted banner
BORDER         = "#dcdfe3"

# ── Typography ──────────────────────────────────────────────────────────────
FONT_FAMILY   = "Segoe UI"
FONT_MONO     = "Consolas"

FONT_H1       = (FONT_FAMILY, 14, "bold")
FONT_TITLE    = (FONT_FAMILY, 13, "bold")
FONT_SUBTITLE = (FONT_FAMILY, 11, "bold")
FONT_BODY     = (FONT_FAMILY, 10)
FONT_BODY_B   = (FONT_FAMILY, 10, "bold")
FONT_SMALL    = (FONT_FAMILY, 9)
FONT_MONO_BODY = (FONT_MONO, 10)
FONT_ICON     = (FONT_FAMILY, 14)   # icon-only buttons (📁, 💾, ✕) — bigger than body text


class TitledFrame(ctk.CTkFrame):
    """
    CustomTkinter has no LabelFrame equivalent — this approximates one: a
    bordered card with a bold title, and a `.body` sub-frame for content.

    Content MUST go inside `.body`, not directly on the TitledFrame instance:
    the title label is `.pack()`-ed directly on `self`, so `self` can only
    ever take more packed children afterwards — grid-based content (which is
    what most forms here use) would immediately conflict with that ("cannot
    use geometry manager grid inside ... already has slaves managed by
    pack"). `.body` is a fresh, empty container where callers are free to use
    either grid or pack exclusively, same as before this widget existed.
    """

    def __init__(self, parent, title: str, **kwargs):
        kwargs.setdefault("corner_radius", 8)
        kwargs.setdefault("border_width", 1)
        kwargs.setdefault("border_color", BORDER)
        super().__init__(parent, **kwargs)
        ctk.CTkLabel(self, text=title, font=FONT_SUBTITLE, text_color=TEXT, anchor="w").pack(
            anchor="w", padx=14, pady=(12, 2),
        )
        self.body = ctk.CTkFrame(self, fg_color="transparent")
        self.body.pack(fill="both", expand=True, padx=4, pady=(0, 10))
