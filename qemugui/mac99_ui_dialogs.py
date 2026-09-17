"""The small windows for the mac99 editor: the name for a duplicate, the
delete confirmation, reset NVRAM, and create a disk. A new machine is not one
of them -- it opens the settings window straight away, on the Machine page.

Mirrors :mod:`qemugui.ui_dialogs`'s shape; the difference is what a machine
folder can hold (no SCSI) and what "reset" means (mac99 has one persisted
file, ``nvram.img``, no ``pram.img`` -- and unlike g3beige, that file's
contents are rebuilt from this GUI's own fields at every start regardless,
see :mod:`qemugui.mac99_model`, so resetting it mainly clears what the
running Mac itself wrote there during its last continuous run).
"""

from __future__ import annotations

import subprocess
import tkinter as tk
from pathlib import Path
from tkinter import ttk, messagebox, simpledialog

from . import mac99_model as model
from . import paths

APP_NAME = "Qemu-system-ppc Mac99 openbios GUI"
DISK_SIZES = ("1", "2", "4", "8", "10", "20")


def refresh_native_style(widget: tk.Misc) -> None:
    """Re-select the running ttk theme on *widget*'s interpreter.

    Native aqua only -- no theme is ever chosen here, this reselects
    whichever one is already running. Tk's own theme-change handler is what
    actually paints frame/label backgrounds; a window built before that
    handler has run once keeps Tk's pre-theme default (white) instead. Call
    once per Tk interpreter (the root, and any Toplevel built on a fresh
    Tcl interpreter such as a headless test)."""
    style = ttk.Style(widget)
    style.theme_use(style.theme_use())


def show_validation(parent, errors: list[str], warnings: list[str]) -> bool:
    """Show what is wrong, or worth knowing. True if saving may go ahead."""
    if errors:
        text = "\n".join(f"• {e}" for e in errors)
        if warnings:
            text += "\n" + "\n".join(f"• {w}" for w in warnings)
        messagebox.showerror("Save", text, parent=parent)
        return False
    if warnings:
        text = "\n".join(f"• {w}" for w in warnings) + "\n\nSave anyway?"
        return messagebox.askyesno("Save", text, parent=parent)
    return True


def _bullets(files: list[str], limit: int = 20) -> str:
    shown = "\n".join(f"    {f}" for f in files[:limit])
    if len(files) > limit:
        shown += f"\n    … and {len(files) - limit} more"
    return shown


def confirm_delete(parent, name: str, will_go: list[str], will_stay: list[str],
                   folder: Path) -> bool:
    """Delete removes the record, the launcher and the saved NVRAM. Disk
    images are never deleted; the window lists both sides before anything
    happens."""
    text = f"Remove “{name}”?"
    if will_go:
        text += "\n\nDeleted:\n" + _bullets(will_go)
    if will_stay:
        text += "\n\nKept:\n" + _bullets(will_stay)
    return messagebox.askyesno("Remove", text, icon="warning", parent=parent)


def confirm_reset_saved_settings(parent, name: str) -> bool:
    """This machine rebuilds its NVRAM from this GUI's own fields at every
    start regardless (see mac99_model.py's module docstring), so deleting
    nvram.img mainly forgets what the Mac itself saved there during its
    last continuous run -- a start-up disk chosen from inside Mac OS, or a
    setenv typed at the Open Firmware prompt. It asks first because those
    are settings a person chose."""
    text = "You are about to delete what this Mac saved for itself last time."
    return messagebox.askyesno("Reset NVRAM", text, icon="warning",
                               default="no", parent=parent)


def ask_name(parent, title: str, prompt: str, initial: str, existing: list[str]) -> str | None:
    while True:
        name = simpledialog.askstring(title, prompt, initialvalue=initial, parent=parent)
        if name is None:
            return None
        name = name.strip()
        if not model.NAME_RE.match(name):
            messagebox.showerror(title, "That name will not work.", parent=parent)
            continue
        if name in existing:
            messagebox.showerror(title, f"You already have a machine called “{name}”.",
                                 parent=parent)
            continue
        return name


class CreateDiskDialog(simpledialog.Dialog):
    """Make a new, empty hard disk for this machine and offer it a position:
    one of the four ATA slots, or a new USB stick.

    Never writes over an existing file: ``model.check_new_image_path`` is the
    guard, and it refuses rather than overwriting.
    """

    def __init__(self, parent, machine: model.Machine, machine_dir: Path):
        self.machine = machine
        self.machine_dir = Path(machine_dir)
        self.qemu_img = paths.qemu_img_binary()
        self.target: Path | None = None
        self.result = None  # (path, format, placement) placement = ("ata", i) | ("usb", None) | None
        super().__init__(parent, "New hard disk")

    def body(self, master):
        refresh_native_style(self)
        r = 0
        ttk.Label(master, text="Name:").grid(row=r, column=0, sticky="w", padx=4, pady=3)
        self.name_var = tk.StringVar(value="hard disk")
        ttk.Entry(master, textvariable=self.name_var, width=30).grid(
            row=r, column=1, sticky="ew", padx=4)
        r += 1
        ttk.Label(master, text="Size (GB):").grid(row=r, column=0, sticky="w", padx=4, pady=3)
        self.size_var = tk.StringVar(value="4")
        ttk.Combobox(master, textvariable=self.size_var, values=DISK_SIZES, width=8).grid(
            row=r, column=1, sticky="w", padx=4)
        r += 1
        ttk.Label(master, text="Format:").grid(row=r, column=0, sticky="w", padx=4, pady=3)
        self.fmt_var = tk.StringVar(value="raw")
        ttk.Combobox(master, textvariable=self.fmt_var, values=model.FORMATS, state="readonly",
                     width=8).grid(row=r, column=1, sticky="w", padx=4)
        r += 1
        ttk.Label(master, text="Put it in:").grid(row=r, column=0, sticky="w", padx=4, pady=3)
        self.choices: list[tuple[str, tuple | None]] = [("Nowhere", None)]
        for i in range(len(model.ATA_SLOTS)):
            self.choices.append((f"{model.ata_slot_name(i)} — {self.machine.ata_slot_status(i)}",
                                 ("ata", i)))
        self.choices.append(("A new USB stick", ("usb", None)))
        free = self.machine.first_unfilled_ata()
        self.place_var = tk.StringVar(
            value=self.choices[1 + free][0] if free is not None else self.choices[0][0])
        ttk.Combobox(master, textvariable=self.place_var, values=[c[0] for c in self.choices],
                     state="readonly", width=32).grid(row=r, column=1, sticky="w", padx=4)
        r += 1
        if not self.qemu_img.is_file():
            ttk.Label(master, text=f"{self.qemu_img.name} is missing.", foreground="#a00").grid(
                row=r, column=0, columnspan=2, sticky="w", padx=4, pady=(8, 2))
        return None

    def validate(self):
        target, why = model.check_new_image_path(self.machine_dir, self.name_var.get(),
                                                 self.fmt_var.get())
        if why:
            messagebox.showerror("New hard disk", why, parent=self)
            return False
        try:
            size = float(self.size_var.get())
            if size <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("New hard disk", "The size has to be a number.", parent=self)
            return False
        if not self.qemu_img.is_file():
            messagebox.showerror("New hard disk", f"{self.qemu_img.name} is missing.", parent=self)
            return False
        self.target = target
        return True

    def apply(self):
        fmt = self.fmt_var.get()
        size = self.size_var.get().strip()
        size_arg = f"{size}G" if not size.upper().endswith(("G", "M")) else size
        self.machine_dir.mkdir(parents=True, exist_ok=True)
        cmd = [str(self.qemu_img), "create", "-f", fmt, str(self.target), size_arg]
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.SubprocessError) as e:
            messagebox.showerror("New hard disk", f"The disk could not be made.\n\n{e}", parent=self)
            return
        if out.returncode != 0:
            messagebox.showerror("New hard disk",
                                 f"The disk could not be made.\n\n{out.stderr or out.stdout}",
                                 parent=self)
            return
        chosen = self.place_var.get()
        place = next((where for label, where in self.choices if label == chosen), None)
        self.result = (str(self.target), fmt, place)


def open_folder(path: Path) -> None:
    path = Path(path)
    try:
        if paths.HOST_PLATFORM == "darwin":
            subprocess.Popen(["open", str(path)])
        elif paths.is_windows():
            subprocess.Popen(["explorer", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except OSError as e:
        messagebox.showerror(APP_NAME, f"That folder could not be opened.\n\n{e}")
