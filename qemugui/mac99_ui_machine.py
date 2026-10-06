"""The mac99 machine editor: Machine, Display, Drives, Network & sound,
Shared folder, Advanced.

Same two rules as the g3beige editor (qemugui/ui_machine.py):

* **Nothing is ever filled in for you.** Every field that names a file
  starts empty and stays empty until it is chosen.
* A file field is one control: a path can be typed or pasted straight into
  it, and double-clicking it opens the chooser.
"""

from __future__ import annotations

import tkinter as tk
import threading
from pathlib import Path
from tkinter import ttk, filedialog, messagebox

from . import paths
from . import usbhost
from . import winusb
from . import mac99_model as model
from .mac99_model import Machine, AtaDrive, Gpu, Network, PromEnv, UsbStorage, Share, UsbHostDevice
from .mac99_ui_dialogs import show_validation, refresh_native_style, CreateDiskDialog

KIND_LABELS = {"": "Empty", "disk": "Hard disk", "cdrom": "CD"}
KIND_BY_LABEL = {v: k for k, v in KIND_LABELS.items()}
IMAGE_TYPES = [("Hard disks and CDs", "*.img *.dsk *.qcow2 *.iso *.toast *.cdr"),
              ("Every file", "*")]
ROM_TYPES = [("ROM files", "*.rom *.ROM *.bin"), ("Every file", "*")]
CDROM_EXTS = {".iso", ".toast", ".cdr", ".dmg"}

GREY = "gray"
EDITOR_WIDTH = 780


def browse_file(parent, var: tk.StringVar, filetypes, fallback: Path | str | None = None) -> str:
    start = paths.browse_start_dir(var.get(), fallback)
    f = filedialog.askopenfilename(parent=parent, initialdir=str(start), filetypes=filetypes)
    if f:
        var.set(f)
        return f
    return ""


def rom_value(path: str, qemu_dir: str) -> str:
    """A ROM beside the emulator is kept by name, anything else by path."""
    p = Path(path)
    try:
        if qemu_dir and p.parent.resolve() == Path(qemu_dir).resolve():
            return p.name
    except OSError:
        pass
    return path


class FilePicker:
    def __init__(self, master, var: tk.StringVar, filetypes, width: int = 40, fallback=None,
                 on_pick=None):
        self.var = var
        self.filetypes = filetypes
        self.fallback = fallback
        self.on_pick = on_pick
        self.entry = ttk.Entry(master, textvariable=var, width=width)
        self.entry.bind("<Double-Button-1>", self._browse)
        var.trace_add("write", lambda *_a: self._refresh())
        self._refresh()

    def grid(self, **kw) -> "FilePicker":
        self.entry.grid(**kw)
        return self

    def _refresh(self) -> None:
        if self.var.get():
            self.entry.xview_moveto(1.0)

    def _browse(self, _e=None):
        fb = self.fallback() if callable(self.fallback) else self.fallback
        chosen = browse_file(self.entry.winfo_toplevel(), self.var, self.filetypes, fb)
        if chosen and self.on_pick:
            self.on_pick(chosen)
        return "break"


class AtaRow:
    """One IDE position: what is in it, which file it is, and whether it is
    the marked boot drive ("Boot" -- mutually exclusive across rows, wired
    by whoever creates them via ``on_boot``; see MachineEditor)."""

    def __init__(self, master, row: int, label: str, fallback=None, on_boot=None):
        self.fallback = fallback
        self.on_boot = on_boot
        self.kind = tk.StringVar(value=KIND_LABELS[""])
        self.file = tk.StringVar()
        self.format = tk.StringVar(value="raw")
        self.boot = tk.BooleanVar(value=False)
        ttk.Label(master, text=label).grid(row=row, column=0, sticky="w", padx=(0, 4), pady=1)
        cb = ttk.Combobox(master, textvariable=self.kind, values=list(KIND_LABELS.values()),
                          state="readonly", width=9)
        cb.grid(row=row, column=1, padx=2, pady=1)
        cb.bind("<<ComboboxSelected>>", self._kind_changed)
        self.picker = FilePicker(master, self.file, IMAGE_TYPES, width=40, fallback=fallback,
                                 on_pick=self._file_picked)
        self.picker.grid(row=row, column=2, sticky="ew", padx=2, pady=1)
        self.file.trace_add("write", lambda *_a: self._infer_kind())
        ttk.Combobox(master, textvariable=self.format, values=model.FORMATS, state="readonly",
                    width=6).grid(row=row, column=3, padx=2)
        ttk.Checkbutton(master, text="Boot", variable=self.boot,
                       command=self._boot_toggled).grid(row=row, column=4, padx=(6, 0))

    def _file_picked(self, path: str):
        """Only a file chosen through the dialog re-detects the format, so a
        format set by hand survives until another file is chosen."""
        self.format.set(model.detect_format(path))

    def _infer_kind(self):
        if KIND_BY_LABEL[self.kind.get()] or not self.file.get().strip():
            return
        ext = Path(self.file.get().strip()).suffix.lower()
        self.kind.set(KIND_LABELS["cdrom" if ext in CDROM_EXTS else "disk"])

    def _kind_changed(self, _e=None):
        if not KIND_BY_LABEL[self.kind.get()]:
            self.file.set("")
            self.format.set("raw")
            self.boot.set(False)

    def _boot_toggled(self):
        if self.boot.get() and self.on_boot:
            self.on_boot(self)

    def set_ata(self, d: AtaDrive | None):
        self.kind.set(KIND_LABELS[d.kind if d else ""])
        self.file.set(d.file if d else "")
        self.format.set((d.format if d else "raw") or "raw")

    def get_ata(self) -> AtaDrive | None:
        self._infer_kind()
        k = KIND_BY_LABEL[self.kind.get()]
        if not k or not self.file.get().strip():
            return None
        return AtaDrive(kind=k, file=self.file.get().strip(), format=self.format.get() or "raw")


class MachineEditor(tk.Toplevel):
    """One machine's settings. ``on_save(machine, old_name)`` runs after
    checking. With ``is_new`` the same window opens empty; nothing exists on
    disk until Save."""

    _usb_win = False

    def __init__(self, parent, machine: Machine, library: model.Library, qemu_dir: str, on_save,
                is_new: bool = False):
        super().__init__(parent)
        refresh_native_style(self)
        self.machine = machine.copy()
        self.old_name = machine.name
        self.is_new = is_new
        self.library = library
        self.qemu_dir = qemu_dir
        self.on_save = on_save
        self.title("New machine" if is_new else f"{machine.name} — settings")
        self.resizable(True, True)
        self.transient(parent)

        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=6, pady=6)
        self._build_machine()
        self._build_display()
        self._build_drives()
        self._build_net_audio()
        self._build_share()
        if paths.HOST_PLATFORM == "darwin":
            self._build_usb_host()
        elif paths.is_windows(paths.HOST_PLATFORM):
            self._build_usb_host_win()
        self._build_advanced()

        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=6, pady=(0, 6))
        self.msg = ttk.Label(bar, text="", foreground=GREY, wraplength=EDITOR_WIDTH - 310)
        self.msg.pack(side="left", fill="x", expand=True)
        ttk.Button(bar, text="Cancel", command=self.destroy).pack(side="right", padx=2)
        ttk.Button(bar, text="Save", command=self.save).pack(side="right", padx=2)
        self.bind("<Escape>", lambda _e: self.destroy())
        self.load(self.machine)
        self._size_window()
        self._centre_on(parent)
        self.nb.select(0)
        self.name_entry.focus_set()

    def _size_window(self):
        self.update_idletasks()
        height = min(self.winfo_reqheight(), max(400, self.winfo_screenheight() - 160))
        self.geometry(f"{EDITOR_WIDTH}x{height}")
        self.minsize(600, 380)

    def _centre_on(self, parent):
        self.update_idletasks()
        w, h = self.winfo_width(), self.winfo_height()
        if w <= 1 or h <= 1:
            w, h = EDITOR_WIDTH, self.winfo_reqheight()
        try:
            top = parent.winfo_toplevel()
            top.update_idletasks()
            x = top.winfo_rootx() + (top.winfo_width() - w) // 2
            y = top.winfo_rooty() + (top.winfo_height() - h) // 2
        except tk.TclError:
            return
        x = max(0, min(x, self.winfo_screenwidth() - w))
        y = max(0, min(y, self.winfo_screenheight() - h - 40))
        self.geometry(f"{w}x{h}+{x}+{y}")

    def machine_folder(self) -> Path:
        name = self.name_var.get().strip() or self.old_name
        return self.library.folder(name) if name else self.library.root

    def _tab(self, title: str) -> ttk.Frame:
        f = ttk.Frame(self.nb, padding=8)
        self.nb.add(f, text=title)
        return f

    def _build_machine(self):
        f = self._tab("Machine")
        f.columnconfigure(1, weight=1)
        r = 0
        ttk.Label(f, text="Name:").grid(row=r, column=0, sticky="w", pady=4)
        self.name_var = tk.StringVar()
        self.name_entry = ttk.Entry(f, textvariable=self.name_var, width=40)
        self.name_entry.grid(row=r, column=1, sticky="ew", pady=4)
        r += 1
        ttk.Label(f, text="Memory:").grid(row=r, column=0, sticky="w", pady=4)
        self.ram_var = tk.StringVar()
        ttk.Combobox(f, textvariable=self.ram_var, values=[str(x) for x in model.RAM_CHOICES],
                    width=10).grid(row=r, column=1, sticky="w", pady=4)
        r += 1
        ttk.Label(f, text="CPUs:").grid(row=r, column=0, sticky="w", pady=4)
        self.smp_var = tk.StringVar()
        ttk.Spinbox(f, textvariable=self.smp_var, from_=model.SMP_MIN, to=model.SMP_MAX,
                   width=5).grid(row=r, column=1, sticky="w", pady=4)
        r += 1
        ttk.Label(f, text="Use max 2 CPUS for Mac OS 9 up to OSX 10.3, use 4 CPUS for OSX 10.4 and 10.5 only",
                 foreground=GREY).grid(row=r, column=0, columnspan=2, sticky="w")
        r += 1
        ttk.Label(f, text="Via:").grid(row=r, column=0, sticky="w", pady=4)
        self.via_var = tk.StringVar()
        ttk.Combobox(f, textvariable=self.via_var, values=list(model.VIA_MODES), state="readonly",
                    width=10).grid(row=r, column=1, sticky="w", pady=4)
        r += 1

    def _build_display(self):
        f = self._tab("Display")
        f.columnconfigure(1, weight=1)
        self.display_var = tk.StringVar()
        displays = model.DISPLAYS.get("win32" if paths.is_windows(paths.HOST_PLATFORM) else paths.HOST_PLATFORM,
                                      ("sdl", "gtk"))
        ttk.Label(f, text="Display:").grid(row=0, column=0, sticky="w", pady=(0, 8))
        ttk.Combobox(f, textvariable=self.display_var, values=list(displays), state="readonly",
                    width=10).grid(row=0, column=1, sticky="w", pady=(0, 8))

        ttk.Label(f, text="Graphics card", font=("", 0, "bold")).grid(
            row=1, column=0, columnspan=3, sticky="w", pady=(6, 4))
        self.gpu_on = tk.BooleanVar(value=False)
        self.gpu_model_var = tk.StringVar(value=model.STD_VGA_LABEL)
        ttk.Radiobutton(f, text=model.STD_VGA_LABEL, value=model.STD_VGA_LABEL,
                        variable=self.gpu_model_var, command=self._gpu_pick).grid(
            row=2, column=0, sticky="w")
        ttk.Label(f, text="(QEMU std-vga, no acceleration)", foreground="#6e6e73").grid(
            row=2, column=1, columnspan=2, sticky="w", padx=2)
        self.rom_vars = {k: tk.StringVar() for k in model.GPU_MODELS}
        self.gpu_radios, self.gpu_rom_cbs, self.gpu_rom_buttons = {}, {}, {}
        hints = {"rage128": "For Mac OS 9", "radeon9800": "For Mac OS X 10.3 to 10.5 only"}
        roms = model.roms_in(self.qemu_dir)
        r = 3
        for key in model.GPU_MODELS:
            ttk.Label(f, text=hints[key], foreground="#6e6e73").grid(
                row=r, column=0, columnspan=3, sticky="w", pady=(6, 0), padx=(22, 0))
            rb = ttk.Radiobutton(f, text=model.GPU_LABELS[key], value=model.GPU_LABELS[key],
                                 variable=self.gpu_model_var, command=self._gpu_pick)
            rb.grid(row=r + 1, column=0, sticky="w")
            cb = ttk.Combobox(f, textvariable=self.rom_vars[key], width=34, values=roms)
            cb.grid(row=r + 1, column=1, sticky="ew", padx=2)
            bt = ttk.Button(f, text="Choose\u2026", command=lambda k=key: self._choose_rom(k))
            bt.grid(row=r + 1, column=2, sticky="w")
            self.gpu_radios[key], self.gpu_rom_cbs[key], self.gpu_rom_buttons[key] = rb, cb, bt
            if model.GPU_ROMS[key] in roms:
                self.rom_vars[key].set(model.GPU_ROMS[key])
            r += 2
        # r == 8: OpenGL and Backend sit right under the Radeon 9800 row
        ttk.Label(f, text="OpenGL:").grid(row=8, column=0, sticky="w", pady=(6, 0), padx=(22, 0))
        self.gl_var = tk.StringVar(value="fast")
        self.gl_cb = ttk.Combobox(f, textvariable=self.gl_var, values=list(model.GL_MODES),
                                  state="readonly", width=8)
        self.gl_cb.grid(row=8, column=1, sticky="w", padx=2, pady=(6, 0))
        self.gl_cb.bind("<<ComboboxSelected>>", self._gpu_model_changed)
        ttk.Label(f, text="Backend:").grid(row=9, column=0, sticky="w", pady=(6, 0), padx=(22, 0))
        self.gl_api_var = tk.StringVar(value="gl")
        apis = list(model.GL_APIS) if paths.HOST_PLATFORM == "darwin" else ["gl"]
        self.gl_api_cb = ttk.Combobox(f, textvariable=self.gl_api_var, values=apis,
                                      state="readonly", width=8)
        self.gl_api_cb.grid(row=9, column=1, sticky="w", padx=2, pady=(6, 0))
        ttk.Label(f, text="off: software; on: host OpenGL, exact; fast: host OpenGL, "
                          "fastest. metal: Apple GPU Macs only.",
                  foreground="#6e6e73", wraplength=420, justify="left").grid(
            row=10, column=0, columnspan=3, sticky="w", padx=(22, 0))

        ttk.Separator(f).grid(row=11, column=0, columnspan=3, sticky="ew", pady=10)
        self.vnc_on = tk.BooleanVar(value=False)
        ttk.Checkbutton(f, text="Show this Mac's screen over VNC instead",
                       variable=self.vnc_on, command=self._vnc_changed).grid(
            row=12, column=0, columnspan=3, sticky="w")
        ttk.Label(f, text="VNC display (e.g. :1):").grid(row=13, column=0, sticky="w", pady=(6, 0))
        self.vnc_var = tk.StringVar()
        self.vnc_entry = ttk.Entry(f, textvariable=self.vnc_var, width=16)
        self.vnc_entry.grid(row=13, column=1, sticky="w", pady=(6, 0))

    def _vnc_changed(self, _e=None):
        if self.vnc_on.get():
            self.vnc_entry.config(state="normal")
            if not self.vnc_var.get().strip():
                self.vnc_var.set(":1")
        else:
            self.vnc_entry.config(state="disabled")

    def _gpu_changed(self, _e=None):
        """The Rage 128 Pro needs OpenBIOS's own vga driver kept out of the
        way; no card means the normal driver is fine. Only sets a sensible
        starting point -- the Advanced tab's checkbox can still be changed
        by hand afterwards."""
        self.no_vga_driver_var.set(self.gpu_on.get())
        self._gpu_model_changed(set_rom=False)

    def _gpu_pick(self, _e=None):
        on = self._gpu_key() is not None
        if on != self.gpu_on.get():
            self.gpu_on.set(on)
            self._gpu_changed()
        else:
            self._gpu_model_changed()

    def _gpu_model_changed(self, _e=None, set_rom=True):
        """Each card has its own ROM field; the GL options only apply to the
        Radeon 9800, and Backend only while OpenGL is not off."""
        key = self._gpu_key()
        on = key is not None
        for k in model.GPU_MODELS:
            st = ["!disabled"] if on else ["disabled"]
            self.gpu_radios[k].state(st)
            self.gpu_rom_buttons[k].state(st)
            self.gpu_rom_cbs[k].state(st)
        gl_on = on and key == "radeon9800"
        self.gl_cb.state(["!disabled"] if gl_on else ["disabled"])
        api_on = gl_on and self.gl_var.get() != "off"
        self.gl_api_cb.state(["!disabled"] if api_on else ["disabled"])

    @property
    def gpu_rom_var(self):
        """The ROM field of the selected card."""
        return self.rom_vars[self._gpu_key() or "rage128"]

    def _choose_rom(self, key):
        var = self.rom_vars[key]
        current = var.get().strip()
        if current and not Path(current).is_absolute():
            current = paths.join_path(self.qemu_dir, current)
        start = paths.browse_start_dir(current, self.qemu_dir)
        f = filedialog.askopenfilename(parent=self, initialdir=str(start), filetypes=ROM_TYPES)
        if f:
            var.set(rom_value(f, self.qemu_dir))
            self.gpu_model_var.set(model.GPU_LABELS[key])
            self.gpu_on.set(True)
            self._gpu_changed()

    def _gpu_key(self):
        label = self.gpu_model_var.get()
        return next((k for k, v in model.GPU_LABELS.items() if v == label), None)

    def _build_drives(self):
        f = self._tab("Drives")
        f.columnconfigure(0, weight=1)
        r = 0
        ttk.Label(f, text="IDE", font=("", 0, "bold")).grid(row=r, column=0, sticky="w", pady=(0, 4))
        r += 1
        ata = ttk.Frame(f)
        ata.grid(row=r, column=0, sticky="ew")
        ata.columnconfigure(2, weight=1)
        for c, h in enumerate(("Position", "", "", "Format", "")):
            ttk.Label(ata, text=h, foreground=GREY).grid(row=0, column=c, sticky="w", padx=4)
        self.ata_rows = [AtaRow(ata, 1 + i, model.ata_slot_name(i), fallback=self.machine_folder,
                                on_boot=self._boot_row_toggled)
                        for i in range(len(model.ATA_SLOTS))]
        r += 1
        self.new_disk_button = None
        if paths.qemu_img_binary().is_file():
            self.new_disk_button = ttk.Button(f, text="New disk…", command=self._new_disk)
            self.new_disk_button.grid(row=r, column=0, sticky="w", pady=(8, 0))

    def _boot_row_toggled(self, row: AtaRow):
        for other in self.ata_rows:
            if other is not row:
                other.boot.set(False)

    def _new_disk(self):
        dlg = CreateDiskDialog(self, self.collect(), self.machine_folder())
        if not dlg.result:
            return
        path, fmt, place = dlg.result
        if place and place[0] == "ata":
            self.ata_rows[place[1]].set_ata(AtaDrive(kind="disk", file=path, format=fmt))
        elif place and place[0] == "usb":
            self.machine.usb_storage.append(UsbStorage(path, fmt))

    def _build_net_audio(self):
        f = self._tab("Network & sound")
        ttk.Label(f, text="Network", font=("", 0, "bold")).grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 4))
        ttk.Label(f, text="Connection:").grid(row=1, column=0, sticky="w")
        self.net_mode = tk.StringVar(value=model.network_mode_label("user"))
        self.net_mode_cb = ttk.Combobox(f, textvariable=self.net_mode, state="readonly", width=18,
                                        values=model.network_labels_for_host(paths.HOST_PLATFORM))
        self.net_mode_cb.grid(row=1, column=1, sticky="w")
        self.net_mode_cb.bind("<<ComboboxSelected>>", self._net_mode_changed)
        self.ifname_label = ttk.Label(f, text=model.ifname_label(paths.HOST_PLATFORM))
        self.ifname_label.grid(row=2, column=0, sticky="w", pady=(6, 0))
        self.ifname_var = tk.StringVar()
        self.ifname_entry = ttk.Entry(f, textvariable=self.ifname_var, width=28)
        self.ifname_entry.grid(row=2, column=1, sticky="w", pady=(6, 0))
        ttk.Label(f, text="Card MAC address:").grid(row=3, column=0, sticky="w", pady=(6, 0))
        self.mac_var = tk.StringVar()
        ttk.Entry(f, textvariable=self.mac_var, width=22).grid(row=3, column=1, sticky="w",
                                                               pady=(6, 0))
        ff = ttk.Frame(f)
        ff.grid(row=4, column=0, columnspan=3, sticky="w", pady=(10, 0))
        ttk.Label(ff, text="Port forwarding", font=("", 0, "bold")).grid(
            row=0, column=0, columnspan=5, sticky="w")
        ttk.Label(ff, text="Forward a port on this Mac to the guest: host 8080 -> guest 80. "
                           "Default (slirp) only." + ("" if paths.is_windows(paths.HOST_PLATFORM) else
                                                   " Host ports below 1024 start the machine with sudo."),
                  foreground=GREY, wraplength=640, justify="left").grid(
            row=1, column=0, columnspan=5, sticky="w", pady=(0, 4))
        for c, t in enumerate(("Protocol", "Host port", "Guest port")):
            ttk.Label(ff, text=t, foreground=GREY).grid(row=2, column=c, sticky="w", padx=(0, 6))
        self.fwd_rows = []
        for i in range(model.HOSTFWD_ROWS):
            proto = tk.StringVar(value="tcp")
            hp, gp = tk.StringVar(), tk.StringVar()
            w = [ttk.Combobox(ff, textvariable=proto, state="readonly", width=5,
                              values=list(model.HOSTFWD_PROTOS)),
                 ttk.Entry(ff, textvariable=hp, width=8),
                 ttk.Entry(ff, textvariable=gp, width=8)]
            for c, x in enumerate(w):
                x.grid(row=3 + i, column=c, sticky="w", padx=(0, 6), pady=1)
            self.fwd_rows.append((proto, hp, gp, w))
        ttk.Separator(f).grid(row=5, column=0, columnspan=3, sticky="ew", pady=10)
        ttk.Label(f, text="Sound interface", font=("", 0, "bold")).grid(
            row=6, column=0, columnspan=3, sticky="w", pady=(0, 4))
        self.audio_var = tk.StringVar(value="default")
        self.audio_default_rb = ttk.Radiobutton(f, text=model.default_audio_label(paths.HOST_PLATFORM),
                                                variable=self.audio_var, value="default")
        self.audio_default_rb.grid(row=7, column=0, columnspan=3, sticky="w")
        ttk.Radiobutton(f, text="SDL", variable=self.audio_var, value="sdl").grid(
            row=8, column=0, columnspan=3, sticky="w")
        ttk.Radiobutton(f, text="None", variable=self.audio_var, value="none").grid(
            row=9, column=0, columnspan=3, sticky="w")
        ttk.Separator(f).grid(row=10, column=0, columnspan=3, sticky="ew", pady=10)
        ttk.Label(f, text="USB", font=("", 0, "bold")).grid(
            row=11, column=0, columnspan=3, sticky="w", pady=(0, 4))
        self.usb_audio_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(f, text="USB audio device (microphone input)",
                       variable=self.usb_audio_var).grid(row=12, column=0, columnspan=3, sticky="w")

    def _net_mode_changed(self, _e=None):
        mode = model.network_mode_by_label(self.net_mode.get())
        if mode in model.NETWORK_MODES_WITH_IFNAME:
            self.ifname_entry.config(state="normal")
            if not self.ifname_var.get():
                self.ifname_var.set(model.default_ifname(mode, paths.HOST_PLATFORM))
        else:
            self.ifname_entry.config(state="disabled")
        for *_v, widgets in self.fwd_rows:
            for i, w in enumerate(widgets):
                w.config(state=("readonly" if i == 0 else "normal") if mode == "user"
                         else "disabled")

    def _build_share(self):
        f = self._tab("Shared folder")
        f.columnconfigure(1, weight=1)
        ttk.Label(f, text="Folder:").grid(row=0, column=0, sticky="w", pady=4)
        self.share_folder_var = tk.StringVar()
        ttk.Entry(f, textvariable=self.share_folder_var, width=40).grid(
            row=0, column=1, sticky="ew", pady=4)
        ttk.Button(f, text="Choose…", command=self._choose_share_folder).grid(
            row=0, column=2, sticky="w", padx=4, pady=4)
        ttk.Label(f, text="User:").grid(row=1, column=0, sticky="w", pady=4)
        self.share_user_var = tk.StringVar(value=Share().user)
        ttk.Entry(f, textvariable=self.share_user_var, width=20).grid(
            row=1, column=1, sticky="w", pady=4)
        ttk.Label(f, text="Password:").grid(row=2, column=0, sticky="w", pady=4)
        self.share_password_var = tk.StringVar()
        ttk.Entry(f, textvariable=self.share_password_var, width=20, show="*").grid(
            row=2, column=1, sticky="w", pady=4)
        self.share_scope = tk.StringVar(value="guest-only")
        ttk.Radiobutton(f, text="Guest only", variable=self.share_scope,
                       value="guest-only").grid(row=3, column=0, columnspan=3, sticky="w",
                                                pady=(8, 0))
        ttk.Radiobutton(f, text="All interfaces (needs a password)", variable=self.share_scope,
                       value="all-interfaces").grid(row=4, column=0, columnspan=3, sticky="w")
        ttk.Label(f, text="In the Mac: ftp://10.0.2.2/ with the default network setting.",
                 foreground=GREY).grid(row=5, column=0, columnspan=3, sticky="w", pady=(8, 0))

    def _choose_share_folder(self):
        start = paths.browse_start_dir(self.share_folder_var.get(), None)
        d = filedialog.askdirectory(parent=self, initialdir=str(start))
        if d:
            self.share_folder_var.set(d)

    def _build_usb_host(self):
        f = self._tab("USB devices")
        f.columnconfigure(0, weight=1)
        f.rowconfigure(1, weight=1)
        ttk.Label(f, text="Host USB devices this Mac takes while it runs. With any device "
                          "ticked the machine starts with sudo and asks for your password. "
                          "High-speed devices go on the USB 2.0 bus, others on the second "
                          "USB 1.1 bus.\n"
                          "USB 2.0 needs Mac OS X 10.2.8 or later. Mac OS 9 and OS X 10.0–10.2.7 only have USB 1.1: there a USB 2.0 device (flash drive, webcam, ...) works only when it is plugged into a real USB 1.1 hub connected to this Mac. Keyboards, mice and other simple devices work without one.",
                  foreground=GREY, wraplength=EDITOR_WIDTH - 60, justify="left").grid(
            row=0, column=0, sticky="ew", pady=(0, 6))
        self.usb_host_list = ttk.Frame(f)
        self.usb_host_list.grid(row=1, column=0, sticky="nsew")
        self.usb_host_vars: dict[str, tk.BooleanVar] = {}
        self.usb_host_info: dict[str, UsbHostDevice] = {}
        self.usb_host_boxes: dict[str, ttk.Checkbutton] = {}

    def _fill_usb_host(self, chosen: list[UsbHostDevice]):
        if self._usb_win:
            self._fill_usb_host_win(chosen)
            return
        for w in self.usb_host_list.winfo_children():
            w.destroy()
        picked = {u.id: u for u in chosen}
        plugged: dict[str, usbhost.HostDevice] = {}
        for d in usbhost.host_devices():
            plugged.setdefault(d.id, d)
        self.usb_host_vars = {}
        self.usb_host_info = {}
        self.usb_host_boxes = {}
        hidden = [i for i, d in plugged.items() if not d.passable]
        rows = [i for i in plugged if i not in hidden] + \
               [i for i in picked if i not in plugged]
        if not rows:
            ttk.Label(self.usb_host_list, text="No USB device is plugged in.",
                      foreground=GREY).grid(row=0, column=0, sticky="w")
        self.usb_hidden_label = ttk.Label(self.usb_host_list,
                                          text=usbhost.hidden_note(len(hidden)),
                                          foreground=GREY)
        self.usb_hidden_label.grid(row=len(rows) + 1, column=0, sticky="w",
                                   pady=(6, 0))
        for r, dev_id in enumerate(rows):
            d = plugged.get(dev_id)
            if d is not None:
                name = d.name or (picked[dev_id].name if dev_id in picked else "")
                info = UsbHostDevice(dev_id, name, d.speed)
                text = f"{name or d.label}  ({dev_id}, {d.speed or '?'} speed)"
                if not d.passable:
                    text += f" -- {d.reason}"
            else:
                info = picked[dev_id]
                text = (f"{info.name or 'USB device ' + dev_id}  ({dev_id}, "
                        f"{info.speed or '?'} speed) -- not connected")
            refused = d is not None and not d.passable
            var = tk.BooleanVar(value=dev_id in picked and not refused)
            box = ttk.Checkbutton(self.usb_host_list, text=text, variable=var)
            box.grid(row=r, column=0, sticky="w")
            if refused:
                box.state(["disabled"])
            self.usb_host_vars[dev_id] = var
            self.usb_host_info[dev_id] = info
            self.usb_host_boxes[dev_id] = box

    def _collect_usb_host(self) -> list[UsbHostDevice]:
        return [self.usb_host_info[i] for i, v in self.usb_host_vars.items() if v.get()]

    # Windows: QEMU opens only devices on WinUSB; winusb-switch moves them.

    def _build_usb_host_win(self):
        f = self._tab("USB devices")
        f.columnconfigure(0, weight=1)
        f.rowconfigure(1, weight=1)
        ttk.Label(f, text="Host USB devices this machine takes while it runs. QEMU can use a "
                          "device only while it is on Windows' WinUSB driver: Give to QEMU puts "
                          "it there, Give back to Windows returns it; each asks once for "
                          "administrator rights. High-speed devices go on the USB 2.0 bus, "
                          "others on the second USB 1.1 bus.\n"
                          "USB 2.0 needs Mac OS X 10.2.8 or later. Mac OS 9 and OS X 10.0–10.2.7 only have USB 1.1: there a USB 2.0 device (flash drive, webcam, ...) works only when it is plugged into a real USB 1.1 hub connected to this PC. Keyboards, mice and other simple devices work without one.",
                  foreground=GREY, wraplength=EDITOR_WIDTH - 60, justify="left").grid(
            row=0, column=0, sticky="ew", pady=(0, 6))
        self.usb_host_list = ttk.Frame(f)
        self.usb_host_list.grid(row=1, column=0, sticky="nsew")
        self.usb_status = ttk.Label(f, text="", wraplength=EDITOR_WIDTH - 60, justify="left")
        self.usb_status.grid(row=2, column=0, sticky="ew", pady=(6, 0))
        self._usb_win = True
        self.usb_busy = False
        self.usb_host_vars: dict[str, tk.BooleanVar] = {}
        self.usb_host_info: dict[str, UsbHostDevice] = {}
        self.usb_host_boxes: dict[str, ttk.Checkbutton] = {}
        self.usb_host_notes: dict[str, ttk.Label] = {}
        self.usb_host_buttons: dict[str, tuple[ttk.Button, ttk.Button]] = {}
        self.usb_host_plugged: dict[str, winusb.WinDevice] = {}

    def _fill_usb_host_win(self, chosen: list[UsbHostDevice]):
        lst = self.usb_host_list
        for w in lst.winfo_children():
            w.destroy()
        picked = {u.id: u for u in chosen}
        devices, problem = winusb.list_devices()
        plugged: dict[str, winusb.WinDevice] = {}
        for d in devices:
            plugged.setdefault(d.id, d)
        self.usb_host_plugged = plugged
        self.usb_host_vars = {}
        self.usb_host_info = {}
        self.usb_host_boxes = {}
        self.usb_host_notes = {}
        self.usb_host_buttons = {}
        # Refused devices stay out, unless one is on WinUSB and can go back
        hidden = [i for i, d in plugged.items() if d.refuse and not d.winusb]
        rows = [i for i in plugged if i not in hidden] + \
               [i for i in picked if i not in plugged]
        self.usb_hidden_label = ttk.Label(lst, text=usbhost.hidden_note(len(hidden)),
                                          foreground=GREY)
        self.usb_hidden_label.grid(row=2 * len(rows) + 2, column=0, columnspan=3,
                                   sticky="w", pady=(6, 0))
        # UsbDk, libusbK, libusb0: may keep devices from their Windows drivers
        self.usb_warning_label = ttk.Label(lst, text=winusb.warnings_text(devices),
                                           foreground="red", wraplength=EDITOR_WIDTH - 60,
                                           justify="left")
        self.usb_warning_label.grid(row=2 * len(rows) + 3, column=0, columnspan=3,
                                    sticky="w")
        if problem:
            ttk.Label(lst, text=problem, foreground=GREY).grid(row=0, column=0, columnspan=3,
                                                               sticky="w")
        elif not rows:
            ttk.Label(lst, text="No USB device is plugged in.",
                      foreground=GREY).grid(row=0, column=0, sticky="w")
        for n, dev_id in enumerate(rows):
            r = 1 + 2 * n
            d = plugged.get(dev_id)
            p = picked.get(dev_id)
            if d is not None:
                name = d.product or d.description or (p.name if p else "")
                info = UsbHostDevice(dev_id, name, d.speed or (p.speed if p else ""))
                text = f"{name or d.label}  ({dev_id}, {d.speed or '?'} speed) -- {d.state}"
                if d.refuse:
                    text += f" -- {d.refuse}"
            else:
                info = p
                text = (f"{info.name or 'USB device ' + dev_id}  ({dev_id}, "
                        f"{info.speed or '?'} speed) -- not connected")
            refused = d is not None and bool(d.refuse)
            # A saved tick stays even while Windows owns the device
            var = tk.BooleanVar(value=dev_id in picked and not refused)
            box = ttk.Checkbutton(lst, text=text, variable=var)
            box.grid(row=r, column=0, sticky="w")
            give = ttk.Button(lst, text="Give to QEMU",
                              command=lambda i=dev_id: self._usb_switch("bind", i))
            back = ttk.Button(lst, text="Give back to Windows",
                              command=lambda i=dev_id: self._usb_switch("unbind", i))
            give.grid(row=r, column=1, padx=2)
            back.grid(row=r, column=2, padx=2)
            note = ttk.Label(lst, text="", foreground=GREY)
            note.grid(row=r + 1, column=0, columnspan=3, sticky="w")
            # Only a device QEMU owns (on WinUSB) can be ticked
            if refused or (d is not None and not d.winusb):
                box.state(["disabled"])
            if refused or d is None or d.winusb:
                give.state(["disabled"])
            if d is None or not d.winusb:      # a refused one may still go back
                back.state(["disabled"])
            self.usb_host_vars[dev_id] = var
            self.usb_host_info[dev_id] = info
            self.usb_host_boxes[dev_id] = box
            self.usb_host_notes[dev_id] = note
            self.usb_host_buttons[dev_id] = (give, back)
            var.trace_add("write", lambda *_a, i=dev_id: self._usb_note(i))
            self._usb_note(dev_id)

    def _usb_note(self, dev_id: str):
        d = self.usb_host_plugged.get(dev_id)
        waiting = self.usb_host_vars[dev_id].get() and d is not None and not d.winusb
        self.usb_host_notes[dev_id].config(text=f"    {winusb.NOT_READY_NOTE}" if waiting else "")

    def _usb_switch(self, op: str, dev_id: str, wait: bool = False):
        """Run winusb-switch elevated for one device; *wait* runs it in line
        (tests), otherwise on a thread so the window stays alive."""
        if self.usb_busy:
            return
        self.usb_busy = True
        self.usb_status.config(text="Waiting for the administrator prompt ...")
        for give, back in self.usb_host_buttons.values():
            give.state(["disabled"])
            back.state(["disabled"])
        if wait:
            self._usb_switched(winusb.run_elevated(op, dev_id))
            return
        box: dict = {}
        worker = threading.Thread(
            target=lambda: box.setdefault("res", winusb.run_elevated(op, dev_id)), daemon=True)
        worker.start()

        def poll():
            try:
                if worker.is_alive():
                    self.after(200, poll)
                    return
                self._usb_switched(box.get("res") or
                                   {"op": op, "id": dev_id, "ok": False, "error": "no result"})
            except tk.TclError:
                pass        # the editor was closed meanwhile
        self.after(200, poll)

    def _usb_switched(self, res: dict):
        self.usb_busy = False
        self._fill_usb_host_win(self._collect_usb_host())
        self.usb_status.config(text=winusb.outcome_text(res))
        # The main window's command line follows the new ownership
        refresh = getattr(self.master, "refresh_details", None)
        if callable(refresh):
            refresh()

    def _build_advanced(self):
        f = self._tab("Advanced")
        f.columnconfigure(1, weight=1)
        ttk.Label(f, text="OpenBIOS", font=("", 0, "bold")).grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 4))
        self.boot_into_ofw_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(f, text="Boot into Open Firmware",
                       variable=self.boot_into_ofw_var).grid(
            row=1, column=0, columnspan=2, sticky="w")
        self.no_vga_driver_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(f, text="Do not load vga driver (required when running with the "
                                "ATI Rage128 Pro)",
                       variable=self.no_vga_driver_var).grid(
            row=2, column=0, columnspan=2, sticky="w")
        ttk.Label(f, text="Boot-device:").grid(row=3, column=0, sticky="w", pady=(6, 0))
        self.boot_device_var = tk.StringVar()
        ttk.Entry(f, textvariable=self.boot_device_var, width=30).grid(
            row=3, column=1, sticky="w", pady=(6, 0))
        ttk.Label(f, text="Boot-args:").grid(row=4, column=0, sticky="w", pady=(6, 0))
        self.boot_args_var = tk.StringVar()
        ttk.Entry(f, textvariable=self.boot_args_var, width=30).grid(
            row=4, column=1, sticky="w", pady=(6, 0))
        ttk.Label(f, text="Date and time:").grid(row=5, column=0, sticky="w", pady=(6, 0))
        self.rtc_base_var = tk.StringVar()
        ttk.Combobox(f, textvariable=self.rtc_base_var, values=list(model.RTC_BASE_CHOICES),
                    width=28).grid(row=5, column=1, sticky="w", pady=(6, 0))
        ttk.Label(f, text="Empty: the host's clock (UTC). localtime: the host's local time. "
                          "Or a fixed start like 2005-04-29T10:30:00.",
                 foreground=GREY).grid(row=6, column=0, columnspan=3, sticky="w")
        ttk.Separator(f).grid(row=7, column=0, columnspan=3, sticky="ew", pady=10)
        ttk.Label(f, text="Additional command line arguments", font=("", 0, "bold")).grid(
            row=8, column=0, columnspan=3, sticky="w", pady=(0, 4))
        self.extra_var = tk.StringVar()
        ttk.Entry(f, textvariable=self.extra_var, width=70).grid(
            row=9, column=0, columnspan=3, sticky="ew")

    def load(self, m: Machine):
        self.name_var.set(m.name)
        self.ram_var.set(str(m.ram_mb))
        self.smp_var.set(str(m.smp))
        self.via_var.set(m.via)
        self.display_var.set(m.display)
        self.vnc_on.set(bool(m.vnc.strip()))
        self.vnc_var.set(m.vnc)
        self._vnc_changed()
        if m.gpu:
            self.gpu_on.set(True)
            self.gpu_model_var.set(m.gpu.label)
            if m.gpu.romfile:
                self.rom_vars[m.gpu.model if m.gpu.model in self.rom_vars else "rage128"].set(
                    m.gpu.romfile)
            elif m.gpu.model in self.rom_vars:
                self.rom_vars[m.gpu.model].set("")
            self.gl_var.set(m.gpu.gl)
            self.gl_api_var.set(m.gpu.gl_api)
        else:
            self.gpu_on.set(False)
            self.gpu_model_var.set(model.STD_VGA_LABEL)
        self._gpu_model_changed(set_rom=False)
        for i, row in enumerate(self.ata_rows):
            row.set_ata(m.ata[i] if i < len(m.ata) else None)
            row.boot.set(i == m.boot_slot)
        self.net_mode_cb.config(
            values=model.network_labels_for_host(paths.HOST_PLATFORM, m.network.mode))
        self.net_mode.set(model.network_mode_label(m.network.mode))
        self.mac_var.set(m.network.mac)
        self.ifname_var.set(m.network.ifname)
        for i, (proto, hp, gp, _w) in enumerate(self.fwd_rows):
            r = m.network.hostfwd[i] if i < len(m.network.hostfwd) else model.HostFwd()
            proto.set(r.proto if r.proto in model.HOSTFWD_PROTOS else "tcp")
            hp.set(r.host_port)
            gp.set(r.guest_port)
        self._net_mode_changed()
        self.audio_var.set(m.audio)
        self.usb_audio_var.set(m.usb_audio)
        self.share_folder_var.set(m.share.folder)
        self.share_user_var.set(m.share.user)
        self.share_password_var.set(m.share.password)
        self.share_scope.set(m.share.scope)
        # inverted: the checkbox asks the opposite question from the field
        self.boot_into_ofw_var.set(not m.prom_env.auto_boot)
        self.no_vga_driver_var.set(not m.prom_env.vga_ndrv)
        self.boot_device_var.set(m.prom_env.boot_device)
        self.boot_args_var.set(m.prom_env.boot_args)
        self.rtc_base_var.set(m.rtc_base)
        self.extra_var.set(m.extra_args)
        if hasattr(self, "usb_host_list"):
            self._fill_usb_host(m.usb_host_devices)

    def collect(self) -> Machine:
        m = self.machine.copy()
        m.name = self.name_var.get().strip()
        try:
            m.ram_mb = int(self.ram_var.get().strip())
        except ValueError:
            m.ram_mb = -1
        try:
            m.smp = int(self.smp_var.get().strip())
        except ValueError:
            m.smp = -1
        m.via = self.via_var.get()
        m.display = self.display_var.get()
        m.vnc = self.vnc_var.get().strip() if self.vnc_on.get() else ""
        m.gpu = (Gpu(self.gpu_rom_var.get().strip() or None, self._gpu_key() or "rage128",
                     self.gl_var.get(), self.gl_api_var.get())
                 if self.gpu_on.get() else None)
        m.ata = [row.get_ata() for row in self.ata_rows]
        m.boot_slot = next((i for i, row in enumerate(self.ata_rows) if row.boot.get()), None)
        mode = model.network_mode_by_label(self.net_mode.get())
        ifname = self.ifname_var.get().strip() if mode in model.NETWORK_MODES_WITH_IFNAME else ""
        fwd = [model.HostFwd(p.get(), h.get().strip(), g.get().strip())
               for p, h, g, _w in self.fwd_rows]
        m.network = Network(mode, self.mac_var.get().strip(), ifname, fwd)
        m.audio = self.audio_var.get()
        m.usb_audio = self.usb_audio_var.get()
        m.share = Share(self.share_folder_var.get().strip(), self.share_user_var.get().strip(),
                        self.share_password_var.get(), self.share_scope.get())
        m.prom_env = PromEnv(not self.boot_into_ofw_var.get(), not self.no_vga_driver_var.get(),
                             self.boot_device_var.get().strip(), self.boot_args_var.get().strip())
        m.rtc_base = self.rtc_base_var.get().strip()
        m.extra_args = self.extra_var.get().strip()
        if hasattr(self, "usb_host_list"):
            m.usb_host_devices = self._collect_usb_host()
        return m

    def save(self):
        m = self.collect()
        errors, warnings = model.validate(m, self.qemu_dir, machine_dir=str(self.machine_folder()))
        if m.name != self.old_name and self.library.has_record(m.name):
            errors.append(f"You already have a machine called “{m.name}”.")
        self.msg.config(text="  ".join(errors + warnings)[:300])
        if not show_validation(self, errors, warnings):
            return
        try:
            self.on_save(m, self.old_name)
        except (OSError, ValueError) as e:
            messagebox.showerror("Save", str(e), parent=self)
            return
        self.destroy()
