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
``-boot c`` there is the disk-boot default (``Machine.boot_slot`` unset);
``build_argv`` emits ``-boot d`` instead when the marked slot
(``model.resolved_boot_kind``) is a CD -- see ``mac99_model.py``'s module
docstring for what that character actually selects.
"""

from __future__ import annotations

from . import paths
from .paths import qopt, split_extra_args, group_options, bat_quote, SUDO_KEEPALIVE  # re-exported
from . import mac99_model as model
from .mac99_model import Machine

HEADER_NOTE = "Written by Qemu-system-ppc Mac99 openbios GUI. Do not edit."

# Kept for anything still reading command.AUDIO_DEFAULT directly; the
# resolution itself goes through paths.resolve_audio.
AUDIO_DEFAULT = paths.AUDIO_DEFAULT

PC_BIOS_DIR = "pc-bios"


def _path(p: str, base: str, platform: str) -> str:
    return paths.join_path(base, p, platform)


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
    return paths.sudo_applies(m.network.needs_sudo, platform)


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
    # 'c' tries the "hd" alias, anything else (here 'd') the "cd" one --
    # see mac99_model.py's module docstring for the alias mechanism.
    argv += ["-boot", "d" if model.resolved_boot_kind(m) == "cdrom" else "c"]

    if m.gpu:
        argv += ["-vga", "none"]
    if m.has_adb():
        argv += ["-global", "adb-mouse.extended-protocol=on"]

    audio = paths.resolve_audio(m.audio, platform)
    argv += ["-audiodev", f"{audio},id=snd", "-global", "screamer.audiodev=snd"]
    extra = split_extra_args(m.extra_args, platform)
    if m.usb_audio and not any(t.split(",")[0] == "usb-audio" for t in extra):
        argv += ["-device", "usb-audio,audiodev=snd"]

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
    if m.rtc_base.strip():
        argv += ["-rtc", f"base={m.rtc_base.strip()}"]
    argv += extra
    return argv


# mac99 has no PRAM file (see mac99_model.py's module docstring): only
# nvram.img is ever chowned back after a sudo run.
OWNED_SETTINGS_FILES = ("nvram.img",)


def render_shell(argv: list[str], sudo: bool = False) -> str:
    return paths.render_shell(argv, HEADER_NOTE, OWNED_SETTINGS_FILES, sudo)


def render_bat(argv: list[str]) -> str:
    return paths.render_bat(argv, HEADER_NOTE)


def render_launcher(argv: list[str], platform: str = paths.HOST_PLATFORM, sudo: bool = False) -> str:
    return paths.render_launcher(argv, HEADER_NOTE, platform, sudo, OWNED_SETTINGS_FILES)


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
