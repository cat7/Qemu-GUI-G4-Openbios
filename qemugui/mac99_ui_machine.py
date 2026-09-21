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
from pathlib import Path
from tkinter import ttk, filedialog, messagebox

from . import paths
from . import mac99_model as model
from .mac99_model import Machine, AtaDrive, Gpu, Network, PromEnv, UsbStorage, Share
from .mac99_ui_dialogs import show_validation, refresh_native_style, CreateDiskDialog

KIND_LABELS = {"": "Empty", "disk": "Hard disk", "cdrom": "CD"}
KIND_BY_LABEL = {v: k for k, v in KIND_LABELS.items()}
IMAGE_TYPES = [("Hard disks and CDs", "*.img *.dsk *.qcow2 *.iso *.toast *.cdr"),
              ("Every file", "*")]
ROM_TYPES = [("ROM files", "*.rom *.ROM *.bin"), ("Every file", "*")]
CDROM_EXTS = {".iso", ".toast", ".cdr", ".dmg"}

GREY = "gray"
EDITOR_WIDTH = 780


def browse_file(parent, var: tk.StringVar, filetypes, fallback: Path | str | None = None) -> None:
    start = paths.browse_start_dir(var.get(), fallback)
    f = filedialog.askopenfilename(parent=parent, initialdir=str(start), filetypes=filetypes)
    if f:
        var.set(f)


class FilePicker:
    def __init__(self, master, var: tk.StringVar, filetypes, width: int = 40, fallback=None):
        self.var = var
        self.filetypes = filetypes
        self.fallback = fallback
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
        browse_file(self.entry.winfo_toplevel(), self.var, self.filetypes, fb)
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
        self.picker = FilePicker(master, self.file, IMAGE_TYPES, width=40, fallback=fallback)
        self.picker.grid(row=row, column=2, sticky="ew", padx=2, pady=1)
        self.file.trace_add("write", lambda *_a: self._infer_kind())
        ttk.Combobox(master, textvariable=self.format, values=model.FORMATS, state="readonly",
                    width=6).grid(row=row, column=3, padx=2)
        ttk.Checkbutton(master, text="Boot", variable=self.boot,
                       command=self._boot_toggled).grid(row=row, column=4, padx=(6, 0))

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
        self.nb.select(0)
        self.name_entry.focus_set()

    def _size_window(self):
        self.update_idletasks()
        height = min(self.winfo_reqheight(), max(400, self.winfo_screenheight() - 160))
        self.geometry(f"{EDITOR_WIDTH}x{height}")
        self.minsize(600, 380)

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
        ttk.Label(f, text="Mac OS 9 loses keyboard control with 2+ CPUs; Mac OS X may use up to 4.",
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
        displays = model.DISPLAYS.get("win32" if paths.is_windows() else paths.HOST_PLATFORM,
                                      ("sdl", "gtk"))
        ttk.Label(f, text="Display:").grid(row=0, column=0, sticky="w", pady=(0, 8))
        ttk.Combobox(f, textvariable=self.display_var, values=list(displays), state="readonly",
                    width=10).grid(row=0, column=1, sticky="w", pady=(0, 8))

        ttk.Label(f, text="Graphics card", font=("", 0, "bold")).grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(6, 4))
        self.gpu_on = tk.BooleanVar(value=False)
        ttk.Checkbutton(f, text="ATI Rage 128 Pro", variable=self.gpu_on,
                       command=self._gpu_changed).grid(row=2, column=0, columnspan=2, sticky="w")
        ttk.Label(f, text="ROM:").grid(row=3, column=0, sticky="w", pady=(6, 0))
        self.gpu_rom_var = tk.StringVar()
        FilePicker(f, self.gpu_rom_var, ROM_TYPES, width=40,
                  fallback=lambda: self.qemu_dir).grid(row=3, column=1, sticky="ew", padx=2,
                                                       pady=(6, 0))

        ttk.Separator(f).grid(row=4, column=0, columnspan=2, sticky="ew", pady=10)
        self.vnc_on = tk.BooleanVar(value=False)
        ttk.Checkbutton(f, text="Show this Mac's screen over VNC instead",
                       variable=self.vnc_on, command=self._vnc_changed).grid(
            row=5, column=0, columnspan=2, sticky="w")
        ttk.Label(f, text="VNC display (e.g. :1):").grid(row=6, column=0, sticky="w", pady=(6, 0))
        self.vnc_var = tk.StringVar()
        self.vnc_entry = ttk.Entry(f, textvariable=self.vnc_var, width=16)
        self.vnc_entry.grid(row=6, column=1, sticky="w", pady=(6, 0))

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
                                        values=model.network_labels_for_host())
        self.net_mode_cb.grid(row=1, column=1, sticky="w")
        self.net_mode_cb.bind("<<ComboboxSelected>>", self._net_mode_changed)
        ttk.Label(f, text="Vmnet host interface:").grid(row=2, column=0, sticky="w", pady=(6, 0))
        self.ifname_var = tk.StringVar()
        self.ifname_entry = ttk.Entry(f, textvariable=self.ifname_var, width=28)
        self.ifname_entry.grid(row=2, column=1, sticky="w", pady=(6, 0))
        ttk.Label(f, text="Card MAC address:").grid(row=3, column=0, sticky="w", pady=(6, 0))
        self.mac_var = tk.StringVar()
        ttk.Entry(f, textvariable=self.mac_var, width=22).grid(row=3, column=1, sticky="w",
                                                               pady=(6, 0))
        ttk.Separator(f).grid(row=4, column=0, columnspan=3, sticky="ew", pady=10)
        ttk.Label(f, text="Sound interface", font=("", 0, "bold")).grid(
            row=5, column=0, columnspan=3, sticky="w", pady=(0, 4))
        self.audio_var = tk.StringVar(value="default")
        ttk.Radiobutton(f, text="CoreAudio", variable=self.audio_var, value="default").grid(
            row=6, column=0, columnspan=3, sticky="w")
        ttk.Radiobutton(f, text="SDL", variable=self.audio_var, value="sdl").grid(
            row=7, column=0, columnspan=3, sticky="w")
        ttk.Radiobutton(f, text="None", variable=self.audio_var, value="none").grid(
            row=8, column=0, columnspan=3, sticky="w")
        ttk.Separator(f).grid(row=9, column=0, columnspan=3, sticky="ew", pady=10)
        ttk.Label(f, text="USB", font=("", 0, "bold")).grid(
            row=10, column=0, columnspan=3, sticky="w", pady=(0, 4))
        self.usb_audio_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(f, text="USB audio device (microphone input)",
                       variable=self.usb_audio_var).grid(row=11, column=0, columnspan=3, sticky="w")

    def _net_mode_changed(self, _e=None):
        mode = model.network_mode_by_label(self.net_mode.get())
        if mode in model.NETWORK_MODES_WITH_IFNAME:
            self.ifname_entry.config(state="normal")
            if not self.ifname_var.get():
                self.ifname_var.set(model.default_ifname(mode))
        else:
            self.ifname_entry.config(state="disabled")

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
            self.gpu_rom_var.set(m.gpu.romfile or "")
        else:
            self.gpu_on.set(False)
            self.gpu_rom_var.set("")
        for i, row in enumerate(self.ata_rows):
            row.set_ata(m.ata[i] if i < len(m.ata) else None)
            row.boot.set(i == m.boot_slot)
        self.net_mode_cb.config(values=model.network_labels_for_host(current=m.network.mode))
        self.net_mode.set(model.network_mode_label(m.network.mode))
        self.mac_var.set(m.network.mac)
        self.ifname_var.set(m.network.ifname)
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
        m.gpu = Gpu(self.gpu_rom_var.get().strip() or None) if self.gpu_on.get() else None
        m.ata = [row.get_ata() for row in self.ata_rows]
        m.boot_slot = next((i for i, row in enumerate(self.ata_rows) if row.boot.get()), None)
        mode = model.network_mode_by_label(self.net_mode.get())
        ifname = self.ifname_var.get().strip() if mode in model.NETWORK_MODES_WITH_IFNAME else ""
        m.network = Network(mode, self.mac_var.get().strip(), ifname)
        m.audio = self.audio_var.get()
        m.usb_audio = self.usb_audio_var.get()
        m.share = Share(self.share_folder_var.get().strip(), self.share_user_var.get().strip(),
                        self.share_password_var.get(), self.share_scope.get())
        m.prom_env = PromEnv(not self.boot_into_ofw_var.get(), not self.no_vga_driver_var.get(),
                             self.boot_device_var.get().strip(), self.boot_args_var.get().strip())
        m.rtc_base = self.rtc_base_var.get().strip()
        m.extra_args = self.extra_var.get().strip()
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
