#!/usr/bin/env python3
"""Qemu-system-ppc Mac99 openbios GUI: start an emulated OpenBIOS PowerMac.

Runs from the folder that holds qemu-system-ppc; machines live in a
"Machines" folder next to it. Standard library only (tkinter).

    python mac99_gui.py
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from qemugui import paths  # noqa: E402

NO_TKINTER = "This Python has no tkinter."

EXIT_CANNOT_RUN = 3
EXIT_NO_TKINTER = 2


def report_problem_on_screen(message: str) -> None:
    print(message, file=sys.stderr)
    try:
        import tkinter as tk
        from tkinter import messagebox
    except ImportError:
        return
    try:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("Qemu-system-ppc Mac99 openbios GUI", message)
        root.destroy()
    except Exception:      # no display: the terminal message stands
        pass


def main(argv=None, report_problem=None) -> int:
    argparse.ArgumentParser(description=__doc__.splitlines()[0]).parse_args(argv)
    report = report_problem or report_problem_on_screen

    problem = paths.startup_problem()
    if problem:
        report(problem)
        return EXIT_CANNOT_RUN

    try:
        import tkinter  # noqa: F401
    except ImportError:
        report(NO_TKINTER)
        return EXIT_NO_TKINTER

    from qemugui.mac99_ui_main import MainWindow
    settings_file = paths.settings_path()
    app = MainWindow(paths.Settings.load(settings_file), settings_file)
    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
