"""Build the QEMU argv for a mac99 Machine and render it as run.command /
run.bat.

Pure: no Tk, no filesystem access beyond string handling. Mirrors
:mod:`qemugui.command`'s shape (one argv list, two renderings) with a
different, mac99-specific option set. See ``qemugui/mac99_model.py`` for the
ground truth each choice below is based on.

Order follows the user's own reference launcher (verbatim, given
2026-09-14): ``-L pc-bios -M mac99,via=... -smp N -display D -m M -boot c
[-vga none -global adb-mouse... ] -audiodev ... -global screamer.audiodev=snd
[-device ati-rage128-pro,romfile=...] -nic ... -drive ... -prom-env ...``.
"""

from __future__ import annotations

import shlex

from . import paths
from .mac99_model import Machine

HEADER_NOTE = "Written by Qemu-system-ppc Mac99 openbios GUI. Do not edit."

AUDIO_DEFAULT = {"darwin": "coreaudio", "win32": "dsound"}

PC_BIOS_DIR = "pc-bios"


def qopt(value: str) -> str:
    return str(value).replace(",", ",,")


def _path(p: str, base: str, platform: str) -> str:
    return paths.join_path(base, p, platform)


def split_extra_args(text: str, platform: str = paths.HOST_PLATFORM) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []
    lex = shlex.shlex(text, posix=True)
    lex.whitespace_split = True
    lex.commentchars = ""
    if paths.is_windows(platform):
        lex.escape = ""
    return list(lex)


def machine_option(m: Machine) -> str:
    return f"{m.machine},via={m.via}"


def nic_option(net) -> str:
    """Every mode goes through -nic with model=sungem: that is mac99's
    default_nic (hw/ppc/mac_newworld.c), made explicit rather than relied
    on."""
    if net.mode == "none":
        return "none"
    tail = f"model=sungem,mac={net.mac}"
    if net.mode == "user":
        return f"user,{tail}"
    if net.mode in ("vmnet-bridged", "tap"):
        return f"{net.mode},ifname={qopt(net.ifname)},{tail}"
    if net.mode in ("vmnet-shared", "vmnet-host"):
        return f"{net.mode},{tail}"
    raise ValueError(f"unknown network mode {net.mode!r}")


def needs_sudo(m: Machine, platform: str = paths.HOST_PLATFORM) -> bool:
    return m.network.needs_sudo and not paths.is_windows(platform)


def prom_env_tokens(m: Machine) -> list[str]:
    """-prom-env pairs, in the order the reference launcher uses them. Empty
    strings are left out rather than sent as ``boot-device=`` -- an
    OpenBIOS variable set to nothing is not the same as one left alone."""
    pe = m.prom_env
    out = ["-prom-env", f"auto-boot?={'true' if pe.auto_boot else 'false'}",
          "-prom-env", f"vga-ndrv?={'true' if pe.vga_ndrv else 'false'}"]
    if pe.boot_device.strip():
        out += ["-prom-env", f"boot-device={pe.boot_device.strip()}"]
    if pe.boot_args.strip():
        out += ["-prom-env", f"boot-args={pe.boot_args.strip()}"]
    return out


def build_argv(m: Machine, qemu_dir: str, machine_dir: str,
               platform: str = paths.HOST_PLATFORM) -> list[str]:
    """The complete argv, first token = absolute path of the QEMU binary."""
    qd = qemu_dir
    argv: list[str] = [paths.join_path(qd, paths.qemu_binary_name(platform), platform)]

    argv += ["-L", _path(PC_BIOS_DIR, qd, platform)]
    argv += ["-M", machine_option(m)]
    argv += ["-smp", str(int(m.smp))]
    # VNC and a local display window are mutually exclusive here: -display
    # none plus -vnc is the combination confirmed working end-to-end
    # (5900+N listens, reachable) against this machine type.
    if m.vnc.strip():
        argv += ["-display", "none", "-vnc", m.vnc.strip()]
    else:
        argv += ["-display", m.display]
    argv += ["-m", str(int(m.ram_mb))]
    argv += ["-boot", "c"]

    if m.gpu:
        argv += ["-vga", "none"]
    if m.has_adb():
        argv += ["-global", "adb-mouse.extended-protocol=on"]

    audio = m.audio
    if audio == "default":
        audio = AUDIO_DEFAULT.get(platform, "sdl")
    argv += ["-audiodev", f"{audio},id=snd", "-global", "screamer.audiodev=snd"]

    if m.gpu:
        parts = ["ati-rage128-pro"]
        if m.gpu.romfile:
            parts.append(f"romfile={qopt(_path(m.gpu.romfile, qd, platform))}")
        argv += ["-device", ",".join(parts)]

    argv += ["-nic", nic_option(m.network)]

    for index, d in enumerate(m.ata):
        if d is None or not d.file:
            continue
        media = "cdrom" if d.kind == "cdrom" else "disk"
        argv += ["-drive", f"file={qopt(_path(d.file, machine_dir, platform))},"
                           f"format={d.format or 'raw'},media={media},index={index}"]

    for i, u in enumerate(m.usb_storage):
        if not u.file:
            continue
        drive_id = f"usbs{i}"
        argv += ["-drive", f"file={qopt(_path(u.file, machine_dir, platform))},"
                           f"format={u.format or 'raw'},if=none,id={drive_id}"]
        argv += ["-device", f"usb-storage,drive={drive_id}"]

    # NVRAM: mac99's macio-nvram is volatile unless a drive is attached
    # (hw/nvram/mac_nvram.c has a "drive" property, mac_newworld.c never
    # sets it). Attaching one here is what gives each machine folder its
    # own persistent nvram.img, the way g3beige's machines get one for free
    # from their own cwd.
    nvram_path = _path("nvram.img", machine_dir, platform)
    argv += ["-drive", f"if=none,id=nvr,file={qopt(nvram_path)},format=raw"]
    argv += ["-global", "macio-nvram.drive=nvr"]

    argv += prom_env_tokens(m)
    argv += split_extra_args(m.extra_args, platform)
    return argv


def group_options(argv: list[str]) -> list[list[str]]:
    groups: list[list[str]] = []
    for tok in argv[1:]:
        if tok.startswith("-") or not groups:
            groups.append([tok])
        else:
            groups[-1].append(tok)
    return groups


SUDO_KEEPALIVE = ('sudo -v\n'
                  'while true; do sudo -n true; sleep 60; '
                  'kill -0 "$$" 2>/dev/null || exit; done &\n'
                  'SUDO_KEEPALIVE_PID=$!')
CHOWN_LINE = ('kill "$SUDO_KEEPALIVE_PID" 2>/dev/null\n'
              'sudo -n chown "${SUDO_USER:-$(id -un)}" nvram.img 2>/dev/null')


def render_shell(argv: list[str], sudo: bool = False) -> str:
    lines = ["#!/bin/bash",
             f"# {HEADER_NOTE}",
             'cd "$(dirname "$0")"',
             ""]
    if sudo:
        lines += [SUDO_KEEPALIVE, ""]
    lines.append(("sudo " if sudo else "") + shlex.quote(argv[0]) + " \\")
    groups = group_options(argv)
    for i, g in enumerate(groups):
        cont = " \\" if i < len(groups) - 1 else ""
        lines.append(" ".join(shlex.quote(t) for t in g) + cont)
    if sudo:
        lines += ["", CHOWN_LINE]
    return "\n".join(lines) + "\n"


def bat_quote(token: str) -> str:
    t = token.replace("%", "%%")
    if (" " in t or "," in t) and not (t.startswith('"') and t.endswith('"')):
        return f'"{t}"'
    return t


def render_bat(argv: list[str]) -> str:
    lines = ["@echo off",
             f"rem {HEADER_NOTE}",
             'cd /d "%~dp0"',
             "",
             bat_quote(argv[0]) + " ^"]
    groups = group_options(argv)
    for i, g in enumerate(groups):
        cont = " ^" if i < len(groups) - 1 else ""
        lines.append(" ".join(bat_quote(t) for t in g) + cont)
    return "\r\n".join(lines) + "\r\n"


def render_launcher(argv: list[str], platform: str = paths.HOST_PLATFORM, sudo: bool = False) -> str:
    return render_bat(argv) if paths.is_windows(platform) else render_shell(argv, sudo)


def launcher_text(m: Machine, qemu_dir: str, machine_dir: str,
                  platform: str = paths.HOST_PLATFORM) -> str:
    return render_launcher(build_argv(m, qemu_dir, machine_dir, platform), platform,
                           needs_sudo(m, platform))


def write_launcher(m: Machine, qemu_dir: str, machine_dir: str,
                   platform: str = paths.HOST_PLATFORM):
    from pathlib import Path
    import os
    import stat
    from .mac99_model import ensure_nvram_file
    ensure_nvram_file(machine_dir)
    argv = build_argv(m, qemu_dir, machine_dir, platform)
    text = render_launcher(argv, platform, needs_sudo(m, platform))
    path = Path(machine_dir) / paths.launcher_name(platform)
    path.write_text(text, encoding="utf-8", newline="")
    if not paths.is_windows(platform):
        st = os.stat(path)
        os.chmod(path, st.st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path, argv
