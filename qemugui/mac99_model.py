"""The mac99 machine record: dataclasses, JSON load/save, checks.

``machine.json`` schema version 1 for the mac99 (OpenBIOS) family. This is a
separate record shape from the g3beige one in :mod:`qemugui.model` -- the two
machines share almost no options -- but the two GUIs share the same on-disk
conventions (paths.py): one Machines folder beside the program, one folder
per machine, nothing ever fills in a file for you, no disk image is ever
deleted. They also share every host-platform mechanic verbatim (paths.py
again): which display/network backends a given host offers, the audio
backend "default" resolves to, and the .command/.bat launcher rendering --
imported here, not reimplemented.

Ground truth (verified in ``qemu-ppc-smp``, branch ``smp-audio-usb``, HEAD
``4987ce252f``):

* No SCSI: ``hw/ppc/mac_newworld.c`` instantiates no MESH/SCSI controller.
* No floppy: ``hw/ppc/mac_newworld.c:300``, "We consider that NewWorld
  PowerMac never have any floppy drive".
* Four IDE slots, index 0..3: ``MAX_IDE_BUS`` (2) x ``MAX_IDE_DEVS`` (2),
  same indexing convention as g3beige.
* NVRAM (``macio-nvram``) has a ``drive`` property
  (``hw/nvram/mac_nvram.c:140``) but ``mac_newworld.c`` does not set it by
  default, so this GUI attaches one itself (``-global macio-nvram.drive``),
  giving each machine folder a persistent ``nvram.img``. BUT
  ``hw/ppc/mac_newworld.c:561`` calls ``pmac_format_nvram_partition()``
  UNCONDITIONALLY at every machine start -- no check against what the drive
  already holds -- and that function (``hw/nvram/mac_nvram.c:206-215``,
  building on ``chrp_nvram_create_system_partition()``,
  ``hw/nvram/chrp_nvram.c:48-86``, itself an unconditional rebuild from the
  ``-prom-env`` flags of THAT invocation) rewrites the whole thing from
  scratch every time. So the persisted file only matters for what survives
  a warm Restart performed *inside* a single running QEMU process (that
  does not re-run machine init); anything the guest saves to NVRAM is
  discarded the moment the process is quit and started again, because the
  next start rebuilds the system partition from this GUI's own -prom-env
  fields regardless of what is on disk. There is no separate PRAM file or
  partition for Mac OS 9 here: whatever Mac OS 9 keeps in PRAM on this
  machine type lives in the same one NVRAM structure Open Firmware uses
  (``hw/nvram/mac_nvram.c:184-203`` formats a second, OS X labelled half of
  the same chip, also unconditionally, also every boot).
* Default NIC is sungem: ``mc->default_nic = "sungem"`` (mac_newworld.c).
* ``-vga none`` is needed only because the machine otherwise adds a default
  "std" VGA card of its own (``mc->default_display = "std"``); leaving the
  graphics card unset here means no ``-vga none`` and no ``-device``, so
  that default card is what runs.
* ``adb-mouse.extended-protocol`` only matters when ADB exists, which is
  ``via=cuda`` or ``via=pmu-adb`` (``has_adb`` in mac_newworld.c); under
  ``via=pmu`` no ADB device is ever created and the property has nothing to
  attach to.
* ``-boot c`` vs ``-boot d`` picks which OpenFirmware ALIAS OpenBIOS tries,
  not a literal drive index. ``mac_newworld.c:298-314`` folds ``-boot``'s
  order string down to a single char (the first one in ``c``..``f``) and
  hands it to the guest as ``FW_CFG_BOOT_DEVICE``; OpenBIOS
  (``roms/openbios/arch/ppc/qemu/init.c:1243-1264``) only consults that
  byte when its own ``boot-device`` variable is still the default placeholder
  string ``"disk"`` -- an explicit ``-prom-env boot-device=...`` (this
  GUI's Advanced tab) overrides it outright. When it does apply, 'c' means
  the "hd" alias, anything else (default) means the "cd" alias. Those
  aliases are set by IDE probing, FIRST MATCH WINS
  (``roms/openbios/drivers/ide.c:1312-1489``, ``set_hd_alias``/
  ``set_cd_alias`` both bail if the alias already exists): the probe walks
  channel 0 then 1, master then slave -- i.e. exactly this GUI's ATA index
  order 0..3 -- so "hd" is the lowest-index drive of kind disk and "cd" is
  the lowest-index drive of kind cdrom, independent of each other. This GUI
  used to hardcode ``-boot c`` always, which meant a CD could never be the
  automatic boot target no matter where it sat.

  ``Machine.boot_slot`` (an ATA index, or ``None``) names the drive the
  Drives tab's "Boot" checkbox marks. Because of the alias rule above,
  checking it does NOT itself move anything -- it only decides ``-boot
  c``/``-boot d`` from that slot's own kind (``resolved_boot_kind``,
  falling back to "disk" when unset, empty, or out of range, which is the
  pre-``boot_slot`` default behaviour). Whether the marked drive is
  actually the one OpenBIOS boots still depends on it being the lowest
  index of its kind; ``validate()`` warns, it does not renumber anything
  -- slot position stays exactly what the person set, same as every other
  field on this machine.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from . import paths
# Host-platform mechanics -- which display/network choices exist and which of
# them this host can offer -- live in paths.py, shared with model.py (the
# g3beige GUI).
from .paths import (DISPLAYS, default_display, AUDIO_DEFAULT,
                    NETWORK_MODES, NETWORK_MODE_PLATFORM, NETWORK_MODES_WITH_IFNAME,
                    NETWORK_MODE_LABELS, network_mode_label, network_mode_by_label,
                    network_modes_for_host, network_labels_for_host, default_ifname)

DEFAULT_MAC = "00:05:02:12:34:56"

SCHEMA = 1
NAME_RE = re.compile(r"^[A-Za-z0-9._ -]+$")
MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")
VNC_RE = re.compile(r"^([A-Za-z0-9.\-]*:)?\d+$")

# The four positions on the built-in IDE cable (index 0..3): two channels,
# master and slave each, same convention g3beige uses.
ATA_SLOTS = ("IDE 0 Master", "IDE 0 Slave", "IDE 1 Master (CD)", "IDE 1 Slave")
ATA_CD_SLOT = 2

DRIVE_KINDS = ("disk", "cdrom")
FORMATS = ("raw", "qcow2")


def ata_slot_name(i: int) -> str:
    return ATA_SLOTS[i]


def resolved_boot_kind(m: Machine) -> str:
    """'cdrom' if ``boot_slot`` names a populated CD slot, else 'disk' --
    the pre-``boot_slot`` default, and what an unset, empty, or
    out-of-range slot falls back to. Drives ``-boot c`` vs ``-boot d``;
    does not say WHICH drive of that kind actually boots -- see the module
    docstring."""
    i = m.boot_slot
    if i is not None and 0 <= i < len(m.ata) and m.ata[i] and m.ata[i].file:
        return m.ata[i].kind if m.ata[i].kind in DRIVE_KINDS else "disk"
    return "disk"


def lowest_slot_of_kind(m: Machine, kind: str) -> int | None:
    """The ATA index OpenBIOS will actually alias for *kind* -- the first
    populated slot of that kind, in probe order (see the module
    docstring). None if there is no such drive."""
    for i, d in enumerate(m.ata):
        if d and d.file and d.kind == kind:
            return i
    return None


AUDIO_MODES = ("default", "sdl", "none")
VIA_MODES = ("cuda", "pmu", "pmu-adb")
RAM_CHOICES = (512, 768, 1024, 1536, 2048)
RAM_MIN, RAM_MAX = 64, 4096
SMP_MIN, SMP_MAX = 1, 4

# The NVRAM this GUI gives each machine. mac99 has no PRAM file the way
# g3beige does -- see the module docstring.
NVRAM_FILE = "nvram.img"
NVRAM_SIZE = 8192            # MACIO_NVRAM_SIZE (include/hw/nvram/mac_nvram.h)
SAVED_SETTINGS_FILES = (NVRAM_FILE,)


def ensure_nvram_file(machine_dir: str | Path) -> Path:
    """The backing file for -global macio-nvram.drive=nvr. QEMU's raw file
    block driver does not create a missing file, so without this the very
    first Start on a fresh machine fails ("Could not open nvram.img"). Never
    touches a file that already exists -- Reset NVRAM deletes it, and the
    next Start should hand OpenBIOS a blank slate, not silently restore one."""
    p = Path(machine_dir) / NVRAM_FILE
    if not p.exists():
        p.write_bytes(b"\0" * NVRAM_SIZE)
    return p

OWNED_FILES = ("machine.json", "run.command", "run.bat", "last-run.log",
              NVRAM_FILE, ".DS_Store")

IMAGE_SUFFIXES = {".img", ".dsk", ".qcow2", ".iso", ".toast", ".cdr", ".dmg",
                  ".hfv", ".hfs", ".vmdk", ".raw"}


def looks_like_disk_image(name: str) -> bool:
    return Path(name).suffix.lower() in IMAGE_SUFFIXES


@dataclass
class AtaDrive:
    kind: str = "disk"
    file: str = ""
    format: str = "raw"

    def to_dict(self) -> dict:
        return {"kind": self.kind, "file": self.file, "format": self.format}

    @classmethod
    def from_dict(cls, d: Any) -> "AtaDrive | None":
        if not isinstance(d, dict):
            return None
        return cls(str(d.get("kind", "disk")), str(d.get("file", "")), str(d.get("format", "raw")))


@dataclass
class UsbStorage:
    file: str = ""
    format: str = "raw"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Any) -> "UsbStorage | None":
        if not isinstance(d, dict):
            return None
        return cls(str(d.get("file", "")), str(d.get("format", "raw")))


@dataclass
class Gpu:
    """The one graphics card this GUI offers. ``None`` on the machine means
    the machine's own default VGA card runs instead (no ``-vga none``, no
    ``-device``)."""
    romfile: str | None = None

    def to_dict(self) -> dict:
        return {"romfile": self.romfile}

    @classmethod
    def from_dict(cls, d: Any) -> "Gpu | None":
        if not isinstance(d, dict):
            return None
        rom = d.get("romfile")
        return cls(str(rom) if rom else None)


@dataclass
class Network:
    mode: str = "user"
    mac: str = DEFAULT_MAC
    ifname: str = ""

    def to_dict(self) -> dict:
        d = {"mode": self.mode, "mac": self.mac}
        if self.mode in NETWORK_MODES_WITH_IFNAME or self.ifname:
            d["ifname"] = self.ifname
        return d

    @classmethod
    def from_dict(cls, d: Any) -> "Network":
        if not isinstance(d, dict):
            return cls()
        return cls(str(d.get("mode", "user")), str(d.get("mac", DEFAULT_MAC)),
                   str(d.get("ifname", "") or ""))

    @property
    def needs_sudo(self) -> bool:
        return self.mode.startswith("vmnet-")

    @property
    def platform(self) -> str | None:
        return NETWORK_MODE_PLATFORM.get(self.mode)


@dataclass
class PromEnv:
    """OpenBIOS nvram variables rebuilt from scratch at every single machine
    start (``hw/ppc/mac_newworld.c:561``, unconditional, no check against
    the persisted NVRAM -- see the module docstring). Keys OpenBIOS reads on
    this path: ``auto-boot?``, ``boot-device``, ``boot-args``, ``vga-ndrv?``
    (roms/openbios/arch/ppc/qemu/init.c).

    ``vga_ndrv`` defaults to True (let OpenBIOS load its own vga driver),
    sensible with no graphics card chosen; the editor flips it to False the
    moment the Rage 128 Pro is turned on, because that card needs OpenBIOS's
    own driver kept out of the way."""
    auto_boot: bool = True
    vga_ndrv: bool = True
    boot_device: str = ""
    boot_args: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Any) -> "PromEnv":
        if not isinstance(d, dict):
            return cls()
        return cls(bool(d.get("auto_boot", True)), bool(d.get("vga_ndrv", True)),
                   str(d.get("boot_device", "")), str(d.get("boot_args", "")))


SHARE_SCOPES = ("guest-only", "all-interfaces")
SHARE_DEFAULT_USER = "guest"


@dataclass
class Share:
    """One host folder, offered to the guest over FTP while it runs -- see
    mac99_share.py. Ported from the g3beige GUI's own shared folder."""
    folder: str = ""             # "" = no shared folder
    user: str = SHARE_DEFAULT_USER
    password: str = ""
    scope: str = "guest-only"    # guest-only | all-interfaces

    def to_dict(self) -> dict:
        return {"folder": self.folder, "user": self.user, "password": self.password,
                "scope": self.scope}

    @classmethod
    def from_dict(cls, d: Any) -> "Share":
        if not isinstance(d, dict):
            return cls()
        return cls(str(d.get("folder", "") or ""),
                   str(d.get("user", SHARE_DEFAULT_USER) or SHARE_DEFAULT_USER),
                   str(d.get("password", "") or ""),
                   str(d.get("scope", "guest-only") or "guest-only"))

    @property
    def enabled(self) -> bool:
        return bool(self.folder.strip())


@dataclass
class Machine:
    name: str = "New machine"
    machine: str = "mac99"
    via: str = "pmu"              # cuda | pmu | pmu-adb
    ram_mb: int = 512
    smp: int = 1
    display: str = "cocoa"
    vnc: str = ""                  # "" = off; else a -vnc display spec, e.g. ":1"
    audio: str = "default"
    usb_audio: bool = False
    gpu: Gpu | None = None
    network: Network = field(default_factory=Network)
    boot_slot: int | None = None   # index into ata, or None -- see resolved_boot_kind
    ata: list = field(default_factory=lambda: [None, None, None, None])
    usb_storage: list = field(default_factory=list)
    prom_env: PromEnv = field(default_factory=PromEnv)
    share: Share = field(default_factory=Share)
    extra_args: str = ""
    notes: str = ""

    def to_dict(self) -> dict:
        return {
            "schema": SCHEMA,
            "name": self.name,
            "machine": self.machine,
            "via": self.via,
            "ram_mb": self.ram_mb,
            "smp": self.smp,
            "display": self.display,
            "vnc": self.vnc,
            "audio": self.audio,
            "usb_audio": self.usb_audio,
            "gpu": self.gpu.to_dict() if self.gpu else None,
            "network": self.network.to_dict(),
            "boot_slot": self.boot_slot,
            "ata": [d.to_dict() if d else None for d in self.ata],
            "usb_storage": [u.to_dict() for u in self.usb_storage],
            "prom_env": self.prom_env.to_dict(),
            "share": self.share.to_dict(),
            "extra_args": self.extra_args,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Machine":
        ata_raw = list(d.get("ata") or [])
        ata = [AtaDrive.from_dict(x) for x in ata_raw][:4]
        while len(ata) < 4:
            ata.append(None)
        usb = [u for u in (UsbStorage.from_dict(x) for x in d.get("usb_storage") or []) if u]
        boot_slot = d.get("boot_slot")
        boot_slot = int(boot_slot) if isinstance(boot_slot, int) else None
        return cls(
            name=str(d.get("name", "New machine")),
            machine=str(d.get("machine", "mac99")),
            via=str(d.get("via") or "pmu"),
            ram_mb=int(d.get("ram_mb", 512)),
            smp=int(d.get("smp", 1) or 1),
            display=str(d.get("display") or default_display()),
            vnc=str(d.get("vnc", "") or ""),
            audio=str(d.get("audio", "default")),
            usb_audio=bool(d.get("usb_audio", False)),
            gpu=Gpu.from_dict(d.get("gpu")),
            network=Network.from_dict(d.get("network")),
            boot_slot=boot_slot,
            ata=ata,
            usb_storage=usb,
            prom_env=PromEnv.from_dict(d.get("prom_env")),
            share=Share.from_dict(d.get("share")),
            extra_args=str(d.get("extra_args", "")),
            notes=str(d.get("notes", "")),
        )

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, ensure_ascii=False) + "\n"

    @classmethod
    def from_json(cls, text: str) -> "Machine":
        return cls.from_dict(json.loads(text))

    @classmethod
    def load(cls, path: Path) -> "Machine":
        return cls.from_json(Path(path).read_text(encoding="utf-8"))

    def save(self, path: Path) -> None:
        Path(path).write_text(self.to_json(), encoding="utf-8")

    def copy(self) -> "Machine":
        return Machine.from_dict(self.to_dict())

    def has_adb(self) -> bool:
        return self.via in ("cuda", "pmu-adb")

    def first_empty_ata(self) -> int | None:
        for i, d in enumerate(self.ata):
            if d is None:
                return i
        return None

    def first_unfilled_ata(self) -> int | None:
        for i, d in enumerate(self.ata):
            if d is None or not d.file:
                return i
        return None

    def ata_slot_status(self, i: int) -> str:
        d = self.ata[i]
        if d is None or not d.file:
            return "empty"
        return f"replace {Path(d.file).name}"

    def image_paths(self) -> list[str]:
        return [f for _label, f in _image_files(self) if f]


def new_machine(name: str) -> Machine:
    """A fresh record: one CPU, via pmu, no drive, no GPU. There is no
    governor and no system-type profile on this machine -- a name is all
    "New machine" asks for. Whoever wants more than one CPU turns it up
    themselves; the CPU field's own help text says what that costs on
    Mac OS 9. No file field is ever filled in."""
    return Machine(name=name, display=default_display())


def file_fields(m: Machine) -> dict[str, str]:
    fields = {"gpu.romfile": (m.gpu.romfile or "") if m.gpu else ""}
    for i, d in enumerate(m.ata):
        fields[f"ata[{i}]"] = d.file if d else ""
    for i, u in enumerate(m.usb_storage):
        fields[f"usb_storage[{i}]"] = u.file
    return fields


def start_blockers(m: Machine) -> list[str]:
    """mac99's firmware ships with the distribution (OpenBIOS is not an
    Apple ROM the person has to find), so unlike g3beige there is nothing
    that must be chosen before Start."""
    return []


# ---------------------------------------------------------------- checking

def validate(m: Machine, qemu_dir: str | None, platform: str = paths.HOST_PLATFORM,
            check_files: bool = True, machine_dir: str | None = None) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []

    if not m.name or not m.name.strip():
        errors.append("This machine has no name.")
    elif not NAME_RE.match(m.name) or m.name.strip() != m.name:
        errors.append("That name will not work.")
    if not (RAM_MIN <= m.ram_mb <= RAM_MAX):
        errors.append(f"Memory has to be between {RAM_MIN} and {RAM_MAX} MB.")
    if m.via not in VIA_MODES:
        errors.append(f"'{m.via}' is not a via setting.")
    if not (SMP_MIN <= m.smp <= SMP_MAX):
        errors.append(f"CPUs has to be between {SMP_MIN} and {SMP_MAX}.")
    elif m.smp > 1 and m.via not in ("pmu", "pmu-adb"):
        errors.append("More than one CPU needs via pmu or pmu-adb.")
    if m.display == "cocoa" and platform != "darwin":
        warnings.append("'cocoa' only works on a Mac.")
    if m.vnc.strip() and not VNC_RE.match(m.vnc.strip()):
        errors.append("VNC display has to look like :1 or 127.0.0.1:1.")

    net = m.network
    if net.mode not in NETWORK_MODES:
        errors.append(f"'{net.mode}' is not a network setting.")
    else:
        if net.mode != "none" and not MAC_RE.match(net.mac):
            errors.append("The card address has to look like 00:05:02:12:34:56.")
        if net.mode in NETWORK_MODES_WITH_IFNAME and not net.ifname.strip():
            errors.append("No interface named.")
        host = "win32" if paths.is_windows(platform) else platform
        if net.platform is not None and net.platform != host:
            warnings.append("This network setting only works on "
                            f"{'a Mac' if net.platform == 'darwin' else 'Windows'}.")

    if m.boot_slot is not None and not (0 <= m.boot_slot < len(m.ata)):
        errors.append("The marked boot drive is not a real drive position.")

    if len(m.ata) != 4:
        errors.append("There are not four drive positions.")
    cd_in_wrong_place = False
    for i, d in enumerate(m.ata):
        if d is None or not d.file:
            continue
        if d.kind not in DRIVE_KINDS:
            errors.append(f"{ata_slot_name(i)} has no drive type.")
        if d.kind == "cdrom" and i != ATA_CD_SLOT:
            cd_in_wrong_place = True
    if cd_in_wrong_place and (m.ata[ATA_CD_SLOT] is None or not m.ata[ATA_CD_SLOT].file):
        warnings.append(f"The CD is not in {ata_slot_name(ATA_CD_SLOT)}.")

    if m.boot_slot is not None and 0 <= m.boot_slot < len(m.ata):
        marked = m.ata[m.boot_slot]
        if marked is None or not marked.file:
            warnings.append(f"{ata_slot_name(m.boot_slot)} is marked Boot but is empty.")
        elif marked.kind in DRIVE_KINDS:
            winner = lowest_slot_of_kind(m, marked.kind)
            if winner is not None and winner != m.boot_slot:
                kind_word = "CD" if marked.kind == "cdrom" else "hard disk"
                warnings.append(f"{ata_slot_name(m.boot_slot)} is marked Boot, but "
                                f"{ata_slot_name(winner)} (lower index, also a {kind_word}) "
                                "will boot first.")

    share = m.share
    if share.scope not in SHARE_SCOPES:
        errors.append(f"'{share.scope}' is not a sharing setting.")
    if share.enabled:
        if not Path(share.folder.strip()).expanduser().is_dir():
            errors.append("The shared folder is not a folder that exists.")
        if not share.user.strip():
            errors.append("The shared folder has no user name.")
        if share.scope == "all-interfaces" and not share.password:
            errors.append("Sharing on all interfaces needs a password.")
        if share.scope == "guest-only" and net.mode != "user":
            warnings.append("Guest only sharing is only reachable with default (slirp).")

    if check_files:
        qd = qemu_dir or ""
        if qd and paths.has_qemu(qd, platform):
            rel = m.gpu.romfile if m.gpu else None
            if rel and not Path(paths.join_path(qd, rel, platform)).is_file():
                warnings.append("The graphics card's ROM is missing: "
                                f"{paths.join_path(qd, rel, platform)}")
        for label, f in _image_files(m):
            if not f:
                continue
            p = Path(f).expanduser()
            if not p.is_absolute():
                if machine_dir is None:
                    continue
                p = Path(machine_dir) / p
            if not p.is_file():
                warnings.append(f"{label}: {f} is not there.")
    return errors, warnings


def _image_files(m: Machine):
    for i, d in enumerate(m.ata):
        if d:
            yield ata_slot_name(i), d.file
    for i, u in enumerate(m.usb_storage):
        yield f"USB storage {i}", u.file


def check_new_image_path(folder: Path | str, name: str, fmt: str) -> tuple[Path | None, str | None]:
    name = (name or "").strip()
    if not name:
        return None, "Give the disk a name."
    if "/" in name or "\\" in name or name in (".", ".."):
        return None, "The name cannot contain slashes."
    suffixes = (".img", ".qcow2", ".dsk")
    if not name.lower().endswith(suffixes):
        name += ".qcow2" if fmt == "qcow2" else ".img"
    target = Path(folder) / name
    if target.exists() or target.is_symlink():
        return None, f"There is already a file called {name}."
    return target, None


# ---------------------------------------------------------------- machines

@dataclass
class DeleteResult:
    folder: Path
    removed: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    folder_removed: bool = False

    @property
    def kept_images(self) -> list[str]:
        return [f for f in self.kept if looks_like_disk_image(f)]


class Library:
    """The Machines folder: one subfolder per machine, named after it. Same
    shape and the same safety rules as :class:`qemugui.model.Library` --
    nothing here ever deletes a disk image, and nothing ever picks a file."""

    def __init__(self, root: Path | str | None = None):
        self.root = Path(root).expanduser() if root else paths.machines_dir()

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    def folder(self, name: str) -> Path:
        return self.root / name

    def json_path(self, name: str) -> Path:
        return self.folder(name) / "machine.json"

    def names(self) -> list[str]:
        if not self.root.is_dir():
            return []
        out = []
        for p in sorted(self.root.iterdir(), key=lambda x: x.name.lower()):
            if p.is_dir() and (p / "machine.json").is_file():
                out.append(p.name)
        return out

    def load(self, name: str) -> Machine:
        m = Machine.load(self.json_path(name))
        m.name = name
        return m

    def load_all(self) -> list[Machine]:
        out = []
        for n in self.names():
            try:
                out.append(self.load(n))
            except (OSError, ValueError, KeyError, TypeError):
                out.append(Machine(name=n, notes="(unreadable)"))
        return out

    def save(self, m: Machine, old_name: str | None = None) -> Path:
        if not NAME_RE.match(m.name):
            raise ValueError("illegal machine name")
        self.ensure()
        if old_name and old_name != m.name and self.folder(old_name).is_dir():
            if self.folder(m.name).exists():
                raise FileExistsError(f"A machine called '{m.name}' already exists.")
            old_folder, new_folder = self.folder(old_name), self.folder(m.name)
            moved = [(old_folder, new_folder),
                     (old_folder.resolve(), new_folder.parent.resolve() / new_folder.name)]
            old_folder.rename(new_folder)
            _repoint_images(m, moved)
        self.folder(m.name).mkdir(parents=True, exist_ok=True)
        m.save(self.json_path(m.name))
        return self.folder(m.name)

    def exists(self, name: str) -> bool:
        return self.folder(name).exists()

    def has_record(self, name: str) -> bool:
        return self.json_path(name).is_file()

    def duplicate(self, name: str, new_name: str) -> Machine:
        if self.exists(new_name):
            raise FileExistsError(f"A machine called '{new_name}' already exists.")
        m = self.load(name)
        m.name = new_name
        self.save(m)
        for f in SAVED_SETTINGS_FILES:
            src = self.folder(name) / f
            dst = self.folder(new_name) / f
            if src.is_file() and not dst.exists():
                shutil.copy2(src, dst)
        return m

    def folder_contents(self, name: str) -> list[str]:
        d = self.folder(name)
        if not d.is_dir():
            return []
        return sorted(str(p.relative_to(d)) for p in d.rglob("*") if p.is_file() or p.is_symlink())

    def delete_preview(self, name: str) -> tuple[list[str], list[str]]:
        contents = self.folder_contents(name)
        removed = [f for f in contents if f in OWNED_FILES]
        kept = [f for f in contents if f not in OWNED_FILES]
        return removed, kept

    def delete(self, name: str) -> DeleteResult:
        d = self.folder(name)
        result = DeleteResult(folder=d)
        if not d.is_dir() or d.resolve().parent != self.root.resolve():
            return result
        for f in OWNED_FILES:
            p = d / f
            if p.is_symlink() or p.is_file():
                try:
                    p.unlink()
                except OSError:
                    continue
                result.removed.append(f)
        result.kept = self.folder_contents(name)
        if not result.kept:
            for sub in sorted((p for p in d.rglob("*") if p.is_dir()),
                              key=lambda p: len(p.parts), reverse=True):
                try:
                    sub.rmdir()
                except OSError:
                    pass
            try:
                d.rmdir()
                result.folder_removed = True
            except OSError:
                pass
        return result

    def saved_settings_status(self, name: str) -> dict[str, int | None]:
        out: dict[str, int | None] = {}
        for f in SAVED_SETTINGS_FILES:
            p = self.folder(name) / f
            out[f] = p.stat().st_size if p.is_file() else None
        return out

    def clear_saved_settings(self, name: str) -> list[str]:
        removed = []
        for f in SAVED_SETTINGS_FILES:
            p = self.folder(name) / f
            if p.is_file():
                p.unlink()
                removed.append(f)
        return removed


def _repoint_images(m: Machine, moved: list[tuple[Path, Path]]) -> None:
    def fixed(p: str) -> str:
        if not p:
            return p
        for old_folder, new_folder in moved:
            try:
                rel = Path(p).relative_to(old_folder)
            except ValueError:
                continue
            return str(new_folder / rel)
        return p

    for d in m.ata:
        if d:
            d.file = fixed(d.file)
    for u in m.usb_storage:
        u.file = fixed(u.file)
