# Qemu-system-ppc G4 Openbios Experimental GUI

A portable launcher for the OpenBIOS `mac99` machine of the
`G4-openbios` branch of `qemu-system-ppc` (github.com/cat7/qemu).
OpenBIOS ships with it; no Apple ROM needed. The program must sit in the
same folder as `qemu-system-ppc` (and `qemu-img`, `pc-bios/`).

## Requirements

- To run from source: Python 3.11+ with tkinter, plus `pyftpdlib` for the
  shared folder (`python -m pip install pyftpdlib`).
- To build a distributable bundle: PyInstaller, with `pyftpdlib` installed
  in the same Python.

## Run from source

    python mac99_gui.py

## Shared folder

Each machine can share one host folder over FTP while it runs. With the
default (slirp) network the Mac reaches it at `ftp://10.0.2.2/` (`:2121`
when port 21 is taken); with vmnet choose "All interfaces", set a
password, and use the host's own address.

## Host USB devices (macOS)

A machine can use USB devices plugged into the Mac (a camera, a DVD
writer): tick them in the machine's settings, USB devices tab. QEMU can
take a device from macOS only as root, so a machine with devices ticked
starts with `sudo` and asks for your password in Terminal, as vmnet does.
Devices go on the NEC USB 2.0 card (`nec-usb=on`, needs a QEMU with it;
Mac OS X 10.3 and later); a device that is not plugged in at start is taken when it is plugged
in. Keyboards, mice and disks with a mounted volume are never offered (a
DVD drive with a mounted disc is fine). A device goes back to macOS when
the machine quits.

## Host USB devices (Windows)

QEMU can use a device only while it is on Windows' WinUSB driver. In the
USB devices tab, Give to QEMU moves it there and Give back to Windows
returns it (one administrator prompt each; `winusb-switch.exe` must sit
next to `qemu-system-ppc.exe`). QEMU itself runs without elevation.

## Build on macOS

Needs a universal2 python.org framework build of Python (Homebrew's Python
is arch-specific and has no tkinter; Apple's `/usr/bin/python3` is arm64e
and not redistributable):

    /Library/Frameworks/Python.framework/Versions/3.13/bin/python3.13 \
        -m PyInstaller --noconfirm Mac99GUI.spec

Result: `dist/Qemu-system-ppc G4 Openbios Experimental GUI.app`. Put it in the
distribution folder that holds `qemu-system-ppc` and `pc-bios/`.

## Build on Windows

    pyinstaller --noconfirm Mac99GUI.spec

`Mac99GUI.spec` targets `universal2` only on macOS; on Windows it produces
a single windowed executable, `dist/Qemu-system-ppc G4 Openbios Experimental GUI.exe`.
Put it alongside `qemu-system-ppc.exe`.

USB switch helper, from `winusb/` with a 64-bit mingw-w64 cross compiler:
`x86_64-w64-mingw32-gcc -O2 -Wall -municode -o winusb-switch.exe winusb-switch.c -lsetupapi -lcfgmgr32 -lcrypt32 -lwintrust`

## Tests

    python -m unittest discover -s tests
