"""One consistent, non-white ttk theme for every mac99 window and dialog.

The g3beige GUI deliberately shipped with no theme at all (its own handoff
says so); on this Mac that leaves every tab and dialog a flat white, which
the user asked to be rid of for this app. ``clam`` is used rather than the
default ``aqua`` because aqua's own frame/label backgrounds on macOS mostly
ignore ttk style overrides -- ``clam`` actually paints what it is told to.

Call :func:`apply` once per Tk interpreter (the main window's root); every
``Toplevel`` (the editor, its dialogs) shares that interpreter's style
database automatically. Call it again from a window built on its OWN root
(as the headless tests do) so it looks the same standing alone.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

BG = "#e7ebf0"          # soft blue-grey, not white
BG_RAISED = "#f2f4f7"   # entries, comboboxes: a shade lighter, still not white
BG_TAB_ACTIVE = "#ffffff"
FG = "#1c1c1e"
ACCENT = "#3f6fb4"
BORDER = "#c3c9d1"


def apply(root: tk.Misc) -> None:
    style = ttk.Style(root)
    if "clam" in style.theme_names():
        style.theme_use("clam")

    root_win = root.winfo_toplevel()
    root_win.configure(background=BG)

    for name in ("TFrame", "TLabelframe"):
        style.configure(name, background=BG)
    style.configure("TLabelframe.Label", background=BG, foreground=FG)
    style.configure("TLabel", background=BG, foreground=FG)
    style.configure("TCheckbutton", background=BG, foreground=FG)
    style.configure("TRadiobutton", background=BG, foreground=FG)
    style.configure("TButton", background=BG_RAISED, foreground=FG, bordercolor=BORDER)
    style.map("TButton", background=[("active", BG_TAB_ACTIVE), ("pressed", BORDER)])
    style.configure("TEntry", fieldbackground=BG_RAISED, foreground=FG, bordercolor=BORDER)
    style.configure("TCombobox", fieldbackground=BG_RAISED, foreground=FG, bordercolor=BORDER)
    style.map("TCombobox", fieldbackground=[("readonly", BG_RAISED)])
    style.configure("TSpinbox", fieldbackground=BG_RAISED, foreground=FG, bordercolor=BORDER)
    style.configure("TNotebook", background=BG, bordercolor=BORDER)
    style.configure("TNotebook.Tab", background=BG, foreground=FG, padding=(10, 4))
    style.map("TNotebook.Tab", background=[("selected", BG_TAB_ACTIVE)])
    style.configure("TSeparator", background=BORDER)
    style.configure("Treeview", background=BG_TAB_ACTIVE, fieldbackground=BG_TAB_ACTIVE,
                    foreground=FG)
    style.configure("Treeview.Heading", background=BG, foreground=FG)
    style.map("Treeview", background=[("selected", ACCENT)],
             foreground=[("selected", "#ffffff")])
    style.configure("TPanedwindow", background=BG)
