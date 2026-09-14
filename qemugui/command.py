"""Build the QEMU argv for a Machine and render it as run.command / run.bat.

Pure: no Tk, no filesystem access beyond string handling. The GUI renders
for the platform it runs on; tests pass ``platform=`` explicitly
(``"darwin"``, ``"win32"``, ``"linux"``).

QEMU itself parses ``vendor=X,product=Y Z``; the shell quoting produced
here only keeps each token whole. One argv list, two renderings.
"""

from __future__ import annotations

from . import paths
from .paths import qopt, split_extra_args, group_options, bat_quote, SUDO_KEEPALIVE  # re-exported
from .model import Machine

HEADER_NOTE = "Written by Qemu-system-ppc GUI. Do not edit."

# Kept for anything still reading command.AUDIO_DEFAULT directly; the
# resolution itself goes through paths.resolve_audio.
AUDIO_DEFAULT = paths.AUDIO_DEFAULT


def _path(p: str, base: str, platform: str) -> str:
    """Absolute path for *platform*; relative values resolve against *base*."""
    return paths.join_path(base, p, platform)


def governor_option(m: Machine) -> str:
    g = m.governor
    if g.mode == "off":
        return f"{m.machine},calibration-governor=off"
    if g.mode == "mips":
        return f"{m.machine},calibration-governor=mips={int(g.mips)}"
    return m.machine


def nic_option(net) -> str:
    """The -nic value. Every mode goes through -nic with model=bmac: macio.c
    only instantiates the onboard bmac when such a nic exists."""
    if net.mode == "none":
        return "none"
    tail = f"model=bmac,mac={net.mac}"
    if net.mode == "user":
        return f"user,{tail}"
    if net.mode in ("vmnet-bridged", "tap"):
        return f"{net.mode},ifname={qopt(net.ifname)},{tail}"
    if net.mode in ("vmnet-shared", "vmnet-host"):
        return f"{net.mode},{tail}"
    raise ValueError(f"unknown network mode {net.mode!r}")


def needs_sudo(m: Machine, platform: str = paths.HOST_PLATFORM) -> bool:
    """vmnet-* launchers run the binary under sudo (macOS only; never in a .bat)."""
    return paths.sudo_applies(m.network.needs_sudo, platform)


def build_argv(m: Machine, qemu_dir: str, machine_dir: str,
               platform: str = paths.HOST_PLATFORM) -> list[str]:
    """The complete argv, first token = absolute path of the QEMU binary.

    *qemu_dir* is the folder the program itself lives in; the caller passes
    it in so this module stays free of any notion of where that is."""
    qd = qemu_dir
    argv: list[str] = [paths.join_path(qd, paths.qemu_binary_name(platform), platform)]

    argv += ["-M", governor_option(m)]
    argv += ["-m", str(int(m.ram_mb))]
    if m.rom:
        # No ROM chosen yet: the option is left out rather than pointing at a
        # guess. Starting is refused separately, with a plain message.
        argv += ["-bios", _path(m.rom, qd, platform)]
    argv += ["-display", m.display]

    audio = paths.resolve_audio(m.audio, platform)
    argv += ["-audiodev", f"{audio},id=snd", "-global", "awacs.audiodev=snd"]

    if m.onboard_romfile:
        argv += ["-global", f"ati-mach64-gt.romfile={qopt(_path(m.onboard_romfile, qd, platform))}"]

    if m.second_gpu and m.second_gpu.device:
        parts = [m.second_gpu.device]
        if m.second_gpu.addr:
            parts.append(f"addr={m.second_gpu.addr}")
        if m.second_gpu.romfile:
            parts.append(f"romfile={qopt(_path(m.second_gpu.romfile, qd, platform))}")
        argv += ["-device", ",".join(parts)]

    argv += ["-nic", nic_option(m.network)]

    for index, d in enumerate(m.ata):
        if d is None or not d.file:
            continue  # empty slot, or a seeded slot with no image yet
        media = "cdrom" if d.kind == "cdrom" else "disk"
        argv += ["-drive", f"file={qopt(_path(d.file, machine_dir, platform))},"
                           f"format={d.format or 'raw'},media={media},index={index}"]

    for s in sorted(m.scsi, key=lambda x: x.id):
        if not s.file:
            continue
        prefix = "scd" if s.kind == "cdrom" else "shd"
        drive_id = f"{prefix}{s.id}"
        dev = "scsi-cd" if s.kind == "cdrom" else "scsi-hd"
        argv += ["-drive", f"file={qopt(_path(s.file, machine_dir, platform))},"
                           f"format={s.format or 'raw'},if=none,id={drive_id}"]
        tok = f"{dev},drive={drive_id},scsi-id={s.id}"
        if s.identity:
            ident = s.identity
            tok += (f",vendor={qopt(ident.vendor)},product={qopt(ident.product)},"
                    f"ver={qopt(ident.ver)}")
        argv += ["-device", tok]

    if m.floppy and m.floppy.file:
        argv += ["-drive", f"if=none,id=fd,file={qopt(_path(m.floppy.file, machine_dir, platform))},"
                           f"format={m.floppy.format or 'raw'}",
                 "-global", "swim3.drive=fd"]

    argv += split_extra_args(m.extra_args, platform)
    return argv


# The saved-settings files a sudo run chowns back to the real user once QEMU
# exits (it was root, so root owns whatever QEMU wrote).
OWNED_SETTINGS_FILES = ("nvram.img", "pram.img")


def render_shell(argv: list[str], sudo: bool = False) -> str:
    return paths.render_shell(argv, HEADER_NOTE, OWNED_SETTINGS_FILES, sudo)


def render_bat(argv: list[str]) -> str:
    return paths.render_bat(argv, HEADER_NOTE)


def render_launcher(argv: list[str], platform: str = paths.HOST_PLATFORM, sudo: bool = False) -> str:
    """The .bat never gets sudo; *sudo* only affects the shell rendering."""
    return paths.render_launcher(argv, HEADER_NOTE, platform, sudo, OWNED_SETTINGS_FILES)


def launcher_text(m: Machine, qemu_dir: str, machine_dir: str,
                  platform: str = paths.HOST_PLATFORM) -> str:
    return render_launcher(build_argv(m, qemu_dir, machine_dir, platform), platform,
                           needs_sudo(m, platform))


def write_launcher(m: Machine, qemu_dir: str, machine_dir: str,
                   platform: str = paths.HOST_PLATFORM):
    """Write run.command / run.bat into the machine folder; returns (path, argv)."""
    from pathlib import Path
    import os
    import stat
    argv = build_argv(m, qemu_dir, machine_dir, platform)
    text = render_launcher(argv, platform, needs_sudo(m, platform))
    path = Path(machine_dir) / paths.launcher_name(platform)
    path.write_text(text, encoding="utf-8", newline="")
    if not paths.is_windows(platform):
        st = os.stat(path)
        os.chmod(path, st.st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path, argv
