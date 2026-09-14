"""The mac99 machine record: dataclasses, JSON load/save, checks.

``machine.json`` schema version 1 for the mac99 (OpenBIOS) family. This is a
separate record shape from the g3beige one in :mod:`qemugui.model` -- the two
machines share almost no options -- but the two GUIs share the same on-disk
conventions (paths.py): one Machines folder beside the program, one folder
per machine, nothing ever fills in a file for you, no disk image is ever
deleted.

Ground truth (verified in ``qemu-ppc-smp``, branch ``smp-audio-usb``, tip
``76f2383475``):

* No SCSI: ``hw/ppc/mac_newworld.c`` instantiates no MESH/SCSI controller.
* No floppy: ``hw/ppc/mac_newworld.c:300``, "We consider that NewWorld
  PowerMac never have any floppy drive".
* Four IDE slots, index 0..3: ``MAX_IDE_BUS`` (2) x ``MAX_IDE_DEVS`` (2),
  same indexing convention as g3beige.
* NVRAM (``macio-nvram``) has a ``drive`` property
  (``hw/nvram/mac_nvram.c``) but ``mac_newworld.c`` does not set it, so it is
  volatile unless a drive is explicitly attached -- which this GUI does, so
  each machine gets a persistent ``nvram.img`` the way the g3beige GUI's
  machines do. There is no PRAM equivalent for mac99: neither via mode wires
  a "pram" drive anywhere in this tree.
* Default NIC is sungem: ``mc->default_nic = "sungem"`` (mac_newworld.c).
* ``-vga none`` is needed only because the machine otherwise adds a default
  "std" VGA card of its own (``mc->default_display = "std"``); leaving the
  graphics card unset here means no ``-vga none`` and no ``-device``, so
  that default card is what runs.
* ``adb-mouse.extended-protocol`` only matters when ADB exists, which is
  ``via=cuda`` or ``via=pmu-adb`` (``has_adb`` in mac_newworld.c); under
  ``via=pmu`` no ADB device is ever created and the property has nothing to
  attach to.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from . import paths
from .mac99_systems import SYSTEMS, DEFAULT_MAC, normalise_system_id

SCHEMA = 1
NAME_RE = re.compile(r"^[A-Za-z0-9._ -]+$")
MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")

# The four positions on the built-in IDE cable (index 0..3): two channels,
# master and slave each, same convention g3beige uses.
ATA_SLOTS = ("IDE 0 Master", "IDE 0 Slave", "IDE 1 Master (CD)", "IDE 1 Slave")
ATA_CD_SLOT = 2

DRIVE_KINDS = ("disk", "cdrom")
FORMATS = ("raw", "qcow2")
DISPLAYS = {"darwin": ("cocoa", "sdl"), "win32": ("sdl", "gtk"), "linux": ("sdl", "gtk")}


def ata_slot_name(i: int) -> str:
    return ATA_SLOTS[i]


def default_display(platform: str = paths.HOST_PLATFORM) -> str:
    return DISPLAYS.get("win32" if paths.is_windows(platform) else platform, ("sdl",))[0]


AUDIO_MODES = ("default", "sdl", "none")
NETWORK_MODES = ("none", "user", "vmnet-bridged", "vmnet-shared", "vmnet-host", "tap")
NETWORK_MODE_PLATFORM = {"none": None, "user": None, "vmnet-bridged": "darwin",
                         "vmnet-shared": "darwin", "vmnet-host": "darwin", "tap": "win32"}
NETWORK_MODES_WITH_IFNAME = ("vmnet-bridged", "tap")
VIA_MODES = ("cuda", "pmu", "pmu-adb")
RAM_CHOICES = (512, 768, 1024, 1536, 2048)
RAM_MIN, RAM_MAX = 64, 4096
SMP_MIN, SMP_MAX = 1, 4

# The NVRAM this GUI gives each machine. mac99 has no PRAM file the way
# g3beige does -- see the module docstring.
NVRAM_FILE = "nvram.img"
NVRAM_SIZE = 8192            # MACIO_NVRAM_SIZE (include/hw/nvram/mac_nvram.h)
SAVED_SETTINGS_FILES = (NVRAM_FILE,)

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


NETWORK_MODE_LABELS = {"user": "default (slirp)"}


def network_mode_label(mode: str) -> str:
    return NETWORK_MODE_LABELS.get(mode, mode)


def network_mode_by_label(label: str) -> str:
    for mode, shown in NETWORK_MODE_LABELS.items():
        if shown == label:
            return mode
    return label


def network_modes_for_host(platform: str = paths.HOST_PLATFORM, current: str | None = None) -> list[str]:
    host = "win32" if paths.is_windows(platform) else platform
    out = [m for m in NETWORK_MODES if NETWORK_MODE_PLATFORM[m] in (None, host)]
    if current and current not in out:
        out.append(current)
    return out


def network_labels_for_host(platform: str = paths.HOST_PLATFORM, current: str | None = None) -> list[str]:
    return [network_mode_label(m) for m in network_modes_for_host(platform, current)]


def default_ifname(mode: str, platform: str = paths.HOST_PLATFORM) -> str:
    if mode == "vmnet-bridged" and platform == "darwin":
        return "en0"
    return ""


@dataclass
class PromEnv:
    """OpenBIOS nvram variables seeded fresh on every boot via ``-prom-env``,
    independent of whatever is in the persisted NVRAM. Keys OpenBIOS reads on
    this path: ``auto-boot?``, ``boot-device``, ``boot-args``, ``vga-ndrv?``
    (roms/openbios/arch/ppc/qemu/init.c)."""
    auto_boot: bool = True
    vga_ndrv: bool = False
    boot_device: str = ""
    boot_args: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Any) -> "PromEnv":
        if not isinstance(d, dict):
            return cls()
        return cls(bool(d.get("auto_boot", True)), bool(d.get("vga_ndrv", False)),
                   str(d.get("boot_device", "")), str(d.get("boot_args", "")))


@dataclass
class Machine:
    name: str = "New machine"
    system: str = "other"
    machine: str = "mac99"
    via: str = "pmu"              # cuda | pmu | pmu-adb
    ram_mb: int = 512
    smp: int = 1
    firmware: str = ""            # advanced override for -bios; "" = OpenBIOS default
    display: str = "cocoa"
    audio: str = "default"
    gpu: Gpu | None = None
    network: Network = field(default_factory=Network)
    ata: list = field(default_factory=lambda: [None, None, None, None])
    usb_storage: list = field(default_factory=list)
    prom_env: PromEnv = field(default_factory=PromEnv)
    extra_args: str = ""
    notes: str = ""

    def to_dict(self) -> dict:
        return {
            "schema": SCHEMA,
            "name": self.name,
            "system": self.system,
            "machine": self.machine,
            "via": self.via,
            "ram_mb": self.ram_mb,
            "smp": self.smp,
            "firmware": self.firmware,
            "display": self.display,
            "audio": self.audio,
            "gpu": self.gpu.to_dict() if self.gpu else None,
            "network": self.network.to_dict(),
            "ata": [d.to_dict() if d else None for d in self.ata],
            "usb_storage": [u.to_dict() for u in self.usb_storage],
            "prom_env": self.prom_env.to_dict(),
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
        return cls(
            name=str(d.get("name", "New machine")),
            system=normalise_system_id(d.get("system")),
            machine=str(d.get("machine", "mac99")),
            via=str(d.get("via") or "pmu"),
            ram_mb=int(d.get("ram_mb", 512)),
            smp=int(d.get("smp", 1) or 1),
            firmware=str(d.get("firmware", "") or ""),
            display=str(d.get("display") or default_display()),
            audio=str(d.get("audio", "default")),
            gpu=Gpu.from_dict(d.get("gpu")),
            network=Network.from_dict(d.get("network")),
            ata=ata,
            usb_storage=usb,
            prom_env=PromEnv.from_dict(d.get("prom_env")),
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


def new_machine(name: str, system_id: str) -> Machine:
    """Seed a record from one of the systems: memory, CPU count and via
    mode. No file field is ever filled in."""
    p = SYSTEMS[normalise_system_id(system_id)]
    m = Machine(name=name, system=p.id, ram_mb=p.ram_mb, smp=p.smp, via=p.via,
               display=default_display())
    m.ata = [None, None, None, None]
    return m


def file_fields(m: Machine) -> dict[str, str]:
    fields = {"firmware": m.firmware or "",
              "gpu.romfile": (m.gpu.romfile or "") if m.gpu else ""}
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
    if m.system == "macos9" and m.smp > 1:
        warnings.append("Mac OS 9 loses keyboard control with more than one CPU.")
    if m.display == "cocoa" and platform != "darwin":
        warnings.append("'cocoa' only works on a Mac.")

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

    if check_files:
        qd = qemu_dir or ""
        if qd and paths.has_qemu(qd, platform):
            for label, rel in (("The firmware override", m.firmware),
                               ("The graphics card's ROM",
                                m.gpu.romfile if m.gpu else None)):
                if rel and not Path(paths.join_path(qd, rel, platform)).is_file():
                    warnings.append(f"{label} is missing: "
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
