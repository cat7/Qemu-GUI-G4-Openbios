"""The mac99 main window: the list of machines, the command line Start will
run, and Start. Mirrors qemugui/ui_main.py's shape.
"""

from __future__ import annotations

import subprocess
import time
import tkinter as tk
from pathlib import Path
from tkinter import ttk, messagebox

from . import mac99_command as command
from . import mac99_model as model
from . import mac99_theme
from . import paths
from .mac99_model import Machine, Library
from .paths import Settings
from .mac99_ui_dialogs import ask_name, confirm_delete, confirm_reset_saved_settings, open_folder
from .mac99_ui_machine import MachineEditor

APP_TITLE = "Qemu-system-ppc Mac99 openbios GUI"
LOG_NAME = "last-run.log"


def qemu_dir() -> str:
    return str(paths.install_dir())


class RunningMachine:
    def __init__(self, name: str, proc: subprocess.Popen, log_path: Path, argv: list[str], log_fh):
        self.name = name
        self.proc = proc
        self.log_path = log_path
        self.argv = argv
        self.started = time.time()
        self._log_fh = log_fh
        self.exit_code: int | None = None

    @property
    def pid(self) -> int:
        return self.proc.pid

    def poll(self) -> int | None:
        rc = self.proc.poll()
        if rc is not None and self.exit_code is None:
            self.exit_code = rc
            try:
                self._log_fh.close()
            except OSError:
                pass
        return rc

    def uptime(self) -> float:
        return time.time() - self.started


TERMINAL_STATUS = "Started in Terminal"


def start_in_terminal(m: Machine, machine_dir: Path) -> Path:
    machine_dir = Path(machine_dir)
    machine_dir.mkdir(parents=True, exist_ok=True)
    launcher, _argv = command.write_launcher(m, qemu_dir(), str(machine_dir))
    subprocess.Popen(["open", "-a", "Terminal", str(launcher)], cwd=str(machine_dir))
    return launcher


def start_machine(m: Machine, machine_dir: Path) -> RunningMachine:
    machine_dir = Path(machine_dir)
    machine_dir.mkdir(parents=True, exist_ok=True)
    _launcher, argv = command.write_launcher(m, qemu_dir(), str(machine_dir))
    log_path = machine_dir / LOG_NAME
    log_fh = open(log_path, "w", encoding="utf-8")
    log_fh.write("# " + " ".join(argv) + "\n")
    log_fh.flush()
    proc = subprocess.Popen(argv, cwd=str(machine_dir), stdout=log_fh, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL)
    return RunningMachine(m.name, proc, log_path, argv, log_fh)


class MainWindow(tk.Tk):
    def __init__(self, settings: Settings, settings_path: Path | None = None):
        super().__init__()
        mac99_theme.apply(self)
        self.settings = settings
        self.settings_path = settings_path or paths.settings_path()
        self.library = Library()
        self.library.ensure()
        self.running: dict[str, RunningMachine] = {}
        self.finished: dict[str, RunningMachine] = {}
        self.terminal_started: dict[str, float] = {}
        self.title(APP_TITLE)
        self.geometry("1100x680")
        self.minsize(880, 520)
        self.notes_name: str | None = None
        self._build()
        self.refresh_list(select=settings.last_machine)
        self.after(1000, self._poll)
        self.protocol("WM_DELETE_WINDOW", self._quit)

    def _build(self):
        menubar = tk.Menu(self)
        filem = tk.Menu(menubar, tearoff=0)
        filem.add_command(label="New machine…", command=self.new_machine)
        filem.add_command(label="Open the Machines folder", command=self.open_machines_folder)
        filem.add_separator()
        filem.add_command(label="Quit", command=self._quit)
        menubar.add_cascade(label="File", menu=filem)
        self.config(menu=menubar)

        pane = ttk.PanedWindow(self, orient="horizontal")
        pane.pack(fill="both", expand=True, padx=8, pady=(8, 4))

        left = ttk.Frame(pane)
        pane.add(left, weight=1)
        self.tree = ttk.Treeview(left, columns=("machine", "state"), show="headings",
                                 selectmode="browse", height=18)
        self.tree.heading("machine", text="Machine")
        self.tree.heading("state", text="")
        self.tree.column("machine", width=240, anchor="w")
        self.tree.column("state", width=80, anchor="w")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self.on_select())
        self.tree.bind("<Double-1>", lambda _e: self.edit_machine())

        btns = ttk.Frame(left)
        btns.pack(fill="x", pady=(6, 0))
        spec = [("New machine…", self.new_machine, False),
               ("Duplicate", self.duplicate_machine, True),
               ("Edit…", self.edit_machine, True),
               ("Delete", self.delete_machine, True),
               ("Open machine folder", self.open_machine_folder, True),
               ("Reset NVRAM", self.reset_nvram, True)]
        self.machine_buttons: list[ttk.Button] = []
        for i, (label, cmd, needs_machine) in enumerate(spec):
            button = ttk.Button(btns, text=label, command=cmd)
            button.grid(row=i // 2, column=i % 2, sticky="ew", padx=2, pady=2)
            if needs_machine:
                self.machine_buttons.append(button)
        self.start_button = ttk.Button(btns, text="Start this Mac", command=self.start_selected)
        self.start_button.grid(row=(len(spec) + 1) // 2, column=0, columnspan=2, sticky="ew",
                               padx=2, pady=(8, 2))
        self.machine_buttons.append(self.start_button)
        btns.columnconfigure(0, weight=1)
        btns.columnconfigure(1, weight=1)

        right = ttk.Frame(pane)
        pane.add(right, weight=3)
        self.run_status = ttk.Label(right, text="", justify="left", font=("", 0, "bold"))
        self.run_status.pack(anchor="w", pady=(0, 6))

        ttk.Label(right, text="Command line constructed:").pack(anchor="w")
        self.command_line = tk.Text(right, height=14, wrap="none", font=self._mono(11))
        self.command_line.pack(fill="both", expand=True)
        self.command_line.config(state="disabled")

        ttk.Label(right, text="Notes").pack(anchor="w", pady=(8, 0))
        self.notes = tk.Text(right, height=6, wrap="word")
        self.notes.pack(fill="x")
        self.notes.bind("<FocusOut>", lambda _e: self.save_notes())

    @staticmethod
    def _mono(size: int):
        return ("Menlo", size) if paths.HOST_PLATFORM == "darwin" else ("Consolas", size - 1)

    @staticmethod
    def _set_text(widget: tk.Text, text: str):
        widget.config(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", text)
        widget.config(state="disabled")

    def _show_notes(self, name: str | None, text: str):
        self.notes_name = name
        self.notes.config(state="normal")
        self.notes.delete("1.0", "end")
        self.notes.insert("1.0", text)
        if name is None:
            self.notes.config(state="disabled")

    def save_notes(self):
        name = getattr(self, "notes_name", None)
        if not name or not self.library.has_record(name):
            return
        text = self.notes.get("1.0", "end").rstrip("\n")
        try:
            m = self.library.load(name)
        except (OSError, ValueError, KeyError, TypeError):
            return
        if m.notes == text:
            return
        m.notes = text
        try:
            self.library.save(m)
        except OSError as e:
            messagebox.showerror(APP_TITLE, f"The notes for “{name}” could not be saved.\n\n{e}")

    def refresh_list(self, select: str | None = None):
        current = select or self.selected_name()
        for item in self.tree.get_children():
            self.tree.delete(item)
        for m in self.library.load_all():
            state = "running" if m.name in self.running else ""
            self.tree.insert("", "end", iid=m.name, values=(m.name, state))
        names = self.library.names()
        if current in names:
            self.tree.selection_set(current)
            self.tree.see(current)
        elif names:
            self.tree.selection_set(names[0])
        self.on_select()

    def selected_name(self) -> str | None:
        sel = self.tree.selection()
        return sel[0] if sel else None

    def selected_machine(self) -> Machine | None:
        name = self.selected_name()
        if not name:
            return None
        try:
            return self.library.load(name)
        except (OSError, ValueError, KeyError, TypeError) as e:
            messagebox.showerror(APP_TITLE, f"“{name}” could not be read.\n\n{e}")
            return None

    def on_select(self):
        self.save_notes()
        name = self.selected_name()
        if name:
            self.settings.last_machine = name
            self._save_settings()
        self.refresh_details()

    def refresh_details(self):
        m = self.selected_machine()
        for button in self.machine_buttons:
            button.state(["disabled"] if m is None else ["!disabled"])
        if not m:
            self._set_text(self.command_line, "")
            self._show_notes(None, "")
            self.run_status.config(text="")
            return
        folder = self.library.folder(m.name)
        try:
            text = command.launcher_text(m, qemu_dir(), str(folder))
        except Exception as e:
            text = f"({e})"
        self._set_text(self.command_line, text)
        self._show_notes(m.name, m.notes)
        self._refresh_run_status(m.name)

    def _refresh_run_status(self, name: str):
        r = self.running.get(name) or self.finished.get(name)
        if not r:
            if name in self.terminal_started:
                when = time.strftime("%H:%M", time.localtime(self.terminal_started[name]))
                self.run_status.config(text=f"{TERMINAL_STATUS} ({when})")
            else:
                self.run_status.config(text="Not running")
            return
        if r.exit_code is None:
            self.run_status.config(text=f"Running — {self._duration(r.uptime())}")
        elif r.exit_code == 0:
            self.run_status.config(text=f"Stopped after {self._duration(r.uptime())}")
        else:
            self.run_status.config(
                text=f"Stopped after {self._duration(r.uptime())} — code {r.exit_code}")

    @staticmethod
    def _duration(seconds: float) -> str:
        s = int(seconds)
        if s < 60:
            return f"{s} second{'' if s == 1 else 's'}"
        if s < 3600:
            return f"{s // 60} minute{'' if s // 60 == 1 else 's'}"
        return f"{s // 3600} hour{'' if s // 3600 == 1 else 's'} {(s % 3600) // 60} min"

    def new_machine(self):
        m = model.new_machine("")
        MachineEditor(self, m, self.library, qemu_dir(), self._on_editor_save, is_new=True)

    def duplicate_machine(self):
        name = self.selected_name()
        if not name:
            return
        new_name = ask_name(self, "Duplicate", "Name:", f"{name} copy", self.library.names())
        if not new_name:
            return
        try:
            m = self.library.duplicate(name, new_name)
        except (OSError, ValueError) as e:
            messagebox.showerror("Duplicate", str(e))
            return
        self._write_launcher(m)
        self.refresh_list(select=new_name)

    def delete_machine(self):
        name = self.selected_name()
        if not name:
            return
        if name in self.running and self.running[name].poll() is None:
            messagebox.showwarning("Delete", f"“{name}” is running.")
            return
        will_go, will_stay = self.library.delete_preview(name)
        if not confirm_delete(self, name, will_go, will_stay, self.library.folder(name)):
            return
        self.library.delete(name)
        self.finished.pop(name, None)
        self.terminal_started.pop(name, None)
        self.refresh_list()

    def edit_machine(self):
        m = self.selected_machine()
        if not m:
            return
        MachineEditor(self, m, self.library, qemu_dir(), self._on_editor_save)

    def _on_editor_save(self, m: Machine, old_name: str):
        if old_name != m.name and old_name in self.running:
            raise ValueError("A machine cannot be renamed while it is running.")
        self.library.save(m, old_name=old_name)
        if old_name != m.name:
            self.finished.pop(old_name, None)
        self._write_launcher(m)
        self.refresh_list(select=m.name)

    def _write_launcher(self, m: Machine):
        try:
            command.write_launcher(m, qemu_dir(), str(self.library.folder(m.name)))
        except OSError as e:
            messagebox.showerror(APP_TITLE, f"The start-up file could not be written.\n\n{e}")

    def reset_nvram(self):
        """mac99's NVRAM is otherwise volatile; this GUI wires it to a
        persistent nvram.img (see mac99_command.py), so there is something
        real to reset here, the way g3beige's machines have."""
        name = self.selected_name()
        if not name:
            return
        if name in self.running and self.running[name].poll() is None:
            messagebox.showwarning("Reset NVRAM", f"“{name}” is running.")
            return
        status = self.library.saved_settings_status(name)
        if not any(size is not None for size in status.values()):
            messagebox.showinfo("Reset NVRAM", f"“{name}” has nothing to reset yet.", parent=self)
            return
        if not confirm_reset_saved_settings(self, name):
            return
        try:
            self.library.clear_saved_settings(name)
        except OSError as e:
            messagebox.showerror("Reset NVRAM", f"“{name}” could not be reset.\n\n{e}")
            return
        self.refresh_details()

    def open_machine_folder(self):
        name = self.selected_name()
        if name:
            open_folder(self.library.folder(name))

    def open_machines_folder(self):
        self.library.ensure()
        open_folder(self.library.root)

    def start_selected(self) -> RunningMachine | None:
        m = self.selected_machine()
        if not m:
            return None
        r = self.running.get(m.name)
        if r and r.poll() is None:
            messagebox.showwarning("Start", f"“{m.name}” is already running.")
            return None
        errors, _warnings = model.validate(m, qemu_dir(), machine_dir=str(self.library.folder(m.name)))
        errors += model.start_blockers(m)
        if errors:
            messagebox.showerror("Start", "\n".join(f"• {e}" for e in errors))
            return None
        if command.needs_sudo(m):
            if paths.HOST_PLATFORM != "darwin":
                messagebox.showerror("Start", "This network setting only works on a Mac.")
                return None
            try:
                start_in_terminal(m, self.library.folder(m.name))
            except OSError as e:
                messagebox.showerror("Start", f"A Terminal window could not be opened.\n\n{e}")
                return None
            self.terminal_started[m.name] = time.time()
            self.finished.pop(m.name, None)
            self.refresh_list(select=m.name)
            return None
        try:
            r = start_machine(m, self.library.folder(m.name))
        except OSError as e:
            messagebox.showerror("Start", f"It could not be started.\n\n{e}")
            return None
        self.running[m.name] = r
        self.finished.pop(m.name, None)
        self.refresh_list(select=m.name)
        return r

    def _poll(self):
        changed = False
        for name, r in list(self.running.items()):
            if r.poll() is not None:
                self.finished[name] = r
                del self.running[name]
                changed = True
        if changed:
            self.refresh_list()
        else:
            sel = self.selected_name()
            if sel and (sel in self.running or sel in self.finished):
                self._refresh_run_status(sel)
        self.after(1000, self._poll)

    def _save_settings(self):
        try:
            self.settings.save(self.settings_path)
        except OSError:
            pass

    def _quit(self):
        self.save_notes()
        self._save_settings()
        if self.running:
            names = ", ".join(self.running)
            if not messagebox.askyesno("Quit", f"Still running: {names} — quit anyway?"):
                return
        self.destroy()
