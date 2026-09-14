# Qemu-system-ppc Mac99 openbios GUI

A portable launcher for the OpenBIOS `mac99` machine of the
`smp-audio-usb` branch of `qemu-system-ppc` (github.com/cat7/qemu).
OpenBIOS ships with it; no Apple ROM needed. The program must sit in the
same folder as `qemu-system-ppc` (and `qemu-img`, `pc-bios/`).

## Requirements

- To run from source: Python 3.11+ with tkinter, no third-party packages.
- To build a distributable bundle: PyInstaller.

## Run from source

    python mac99_gui.py

## Build on macOS

Needs a universal2 python.org framework build of Python (Homebrew's Python
is arch-specific and has no tkinter; Apple's `/usr/bin/python3` is arm64e
and not redistributable):

    /Library/Frameworks/Python.framework/Versions/3.13/bin/python3.13 \
        -m PyInstaller --noconfirm Mac99GUI.spec

Result: `dist/Qemu-system-ppc Mac99 openbios GUI.app`. Put it in the
distribution folder that holds `qemu-system-ppc` and `pc-bios/`.

## Build on Windows

    pyinstaller --noconfirm Mac99GUI.spec

`Mac99GUI.spec` targets `universal2` only on macOS; on Windows it produces
a windowed, onedir build at `dist/Qemu-system-ppc Mac99 openbios GUI/`. Put
that folder's contents alongside `qemu-system-ppc.exe`.

## Tests

    python -m unittest discover -s tests
