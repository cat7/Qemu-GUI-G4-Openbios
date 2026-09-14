"""The machine record: dataclasses, JSON load/save, checks, machine folders.

No Tk in here. ``machine.json`` schema version 1 (see doc/HANDOFF-qemu-gui.md).

Three rules this file exists to enforce:

* Machines live in the ``Machines`` folder next to the program. There is no
  configurable location and no per-machine emulator override.
* **Nothing here ever deletes a disk image.** Deleting a machine removes the
  handful of files the program itself wrote (see ``OWNED_FILES``) and leaves
  every other file, disk images above all, exactly where it is.
* **Nothing here ever chooses a file.** Every field naming a file -- the
  Mac's ROM, the graphics ROMs, hard disks, CDs, floppy disks -- starts empty
  and stays empty until the person picks something. Nothing is guessed, and
  nothing is filled in because a likely-looking file happens to sit beside
  the emulator.
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
# them this host can offer -- live in paths.py, shared with mac99_model.py.
# Re-exported here so existing callers keep using model.DISPLAYS etc.
from .paths import (DISPLAYS, default_display, AUDIO_DEFAULT,
                    NETWORK_MODES, NETWORK_MODE_PLATFORM, NETWORK_MODES_WITH_IFNAME,
                    NETWORK_MODE_LABELS, network_mode_label, network_mode_by_label,
                    network_modes_for_host, network_labels_for_host, default_ifname)
from .systems import (SYSTEMS, DEFAULT_MAC, DEFAULT_SECOND_GPU_ADDR,
                      DEFAULT_DISK_IDENTITY, DEFAULT_CDROM_IDENTITY,
                      normalise_system_id)

SCHEMA = 1
NAME_RE = re.compile(r"^[A-Za-z0-9._ -]+$")
MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")
ADDR_RE = re.compile(r"^(0x[0-9A-Fa-f]{1,2}|[0-9]{1,2})(\.[0-7])?$")

# ---------------------------------------------------------------- naming
#
# The four positions on the Mac's built-in drive cable (index 0..3), and the
# numbers on its SCSI chain.
ATA_SLOTS = ("IDE 0 Master", "IDE 0 Slave", "IDE 1 Master (CD)", "IDE 1 Slave")
ATA_STARTUP_SLOT = 0        # the position the Mac starts up from
ATA_CD_SLOT = 2             # where a CD is expected


def ata_slot_name(i: int) -> str:
    return ATA_SLOTS[i]


SCSI_IDS = list(range(7))          # 7 is the Mac itself
SCSI_SELF_ID = 7


def scsi_name(sid: int) -> str:
    return f"Device {sid}"


SCSI_SELF_LABEL = "Macintosh"

DRIVE_KINDS = ("disk", "cdrom")
FORMATS = ("raw", "qcow2")

AUDIO_MODES = ("default", "sdl", "none")
GOVERNOR_MODES = ("default", "off", "mips")
RAM_CHOICES = (128, 256, 512, 768, 1024)
RAM_MIN, RAM_MAX = 32, 4096
SECOND_GPU_SUPPORTED = ("ati-rage128-pro",)
SECOND_GPU_EXPERIMENTAL = ("ati-vga", "VGA", "cirrus-vga")

# The Mac's own saved settings (startup disk, date and time, screen depth).
# QEMU writes these two into the machine folder on every run.
SAVED_SETTINGS_FILES = ("nvram.img", "pram.img")

# The complete list of files Qemu-system-ppc GUI is allowed to delete from a machine
# folder. Anything not on this list -- above all a disk image -- is left
# alone, whatever it is called. This list is the whole safety story: it is
# an allow-list, not a deny-list, so a new kind of file is safe by default.
OWNED_FILES = ("machine.json", "run.command", "run.bat", "last-run.log",
               "nvram.img", "pram.img", ".DS_Store")

# Only used to word messages ("your disk images are still there"); nothing is
# deleted or kept on the strength of an extension.
IMAGE_SUFFIXES = {".img", ".dsk", ".qcow2", ".iso", ".toast", ".cdr", ".dmg",
                  ".hfv", ".hfs", ".vmdk", ".raw"}


def looks_like_disk_image(name: str) -> bool:
    return Path(name).suffix.lower() in IMAGE_SUFFIXES


@dataclass
class Identity:
    vendor: str = ""
    product: str = ""
    ver: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Any) -> "Identity | None":
        if not isinstance(d, dict):
            return None
        return cls(str(d.get("vendor", "")), str(d.get("product", "")), str(d.get("ver", "")))


@dataclass
class AtaDrive:
    kind: str = "disk"           # disk | cdrom
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
class ScsiDrive:
    id: int = 0
    kind: str = "disk"
    file: str = ""
    format: str = "raw"
    identity: Identity | None = None

    def to_dict(self) -> dict:
        return {"id": self.id, "kind": self.kind, "file": self.file, "format": self.format,
                "identity": self.identity.to_dict() if self.identity else None}

    @classmethod
    def from_dict(cls, d: Any) -> "ScsiDrive | None":
        if not isinstance(d, dict):
            return None
        return cls(int(d.get("id", 0)), str(d.get("kind", "disk")), str(d.get("file", "")),
                   str(d.get("format", "raw")), Identity.from_dict(d.get("identity")))


@dataclass
class Floppy:
    file: str = ""
    format: str = "raw"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Any) -> "Floppy | None":
        if not isinstance(d, dict):
            return None
        return cls(str(d.get("file", "")), str(d.get("format", "raw")))


@dataclass
class SecondGpu:
    device: str = "ati-rage128-pro"
    addr: str = DEFAULT_SECOND_GPU_ADDR
    romfile: str | None = None

    def to_dict(self) -> dict:
        return {"device": self.device, "addr": self.addr, "romfile": self.romfile}

    @classmethod
    def from_dict(cls, d: Any) -> "SecondGpu | None":
        if not isinstance(d, dict):
            return None
        rom = d.get("romfile")
        return cls(str(d.get("device", "ati-rage128-pro")), str(d.get("addr", "") or ""),
                   str(rom) if rom else None)


@dataclass
class Network:
    mode: str = "user"           # none | user | vmnet-bridged | vmnet-shared | vmnet-host | tap
    mac: str = DEFAULT_MAC
    ifname: str = ""             # host interface for vmnet-bridged (en0) / tap (TAP-Windows adapter name)

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
        """vmnet backends need root (or the com.apple.vm.networking entitlement)."""
        return self.mode.startswith("vmnet-")

    @property
    def platform(self) -> str | None:
        return NETWORK_MODE_PLATFORM.get(self.mode)


@dataclass
class Governor:
    mode: str = "default"        # default | off | mips
    mips: int = 100

    def to_dict(self) -> dict:
        d = {"mode": self.mode}
        if self.mode == "mips":
            d["mips"] = self.mips
        return d

    @classmethod
    def from_dict(cls, d: Any) -> "Governor":
        if not isinstance(d, dict):
            return cls()
        return cls(str(d.get("mode", "default")), int(d.get("mips", 100) or 100))


@dataclass
class Machine:
    name: str = "New machine"
    system: str = "other"
    machine: str = "g3beige"
    ram_mb: int = 512
    rom: str = ""                # chosen by the person; never guessed
    display: str = "cocoa"
    audio: str = "default"
    onboard_romfile: str | None = None
    second_gpu: SecondGpu | None = None
    network: Network = field(default_factory=Network)
    governor: Governor = field(default_factory=Governor)
    ata: list = field(default_factory=lambda: [None, None, None, None])
    scsi: list = field(default_factory=list)
    floppy: Floppy | None = None
    extra_args: str = ""
    notes: str = ""

    # ---- JSON ----
    def to_dict(self) -> dict:
        return {
            "schema": SCHEMA,
            "name": self.name,
            "system": self.system,
            "machine": self.machine,
            "ram_mb": self.ram_mb,
            "rom": self.rom,
            "display": self.display,
            "audio": self.audio,
            "onboard_romfile": self.onboard_romfile,
            "second_gpu": self.second_gpu.to_dict() if self.second_gpu else None,
            "network": self.network.to_dict(),
            "governor": self.governor.to_dict(),
            "ata": [d.to_dict() if d else None for d in self.ata],
            "scsi": [d.to_dict() for d in sorted(self.scsi, key=lambda s: s.id)],
            "floppy": self.floppy.to_dict() if self.floppy else None,
            "extra_args": self.extra_args,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Machine":
        # A "qemu_dir" key written by an older version is read and dropped:
        # the emulator is now always the one next to the program.
        ata_raw = list(d.get("ata") or [])
        ata = [AtaDrive.from_dict(x) for x in ata_raw][:4]
        while len(ata) < 4:
            ata.append(None)
        scsi = [s for s in (ScsiDrive.from_dict(x) for x in d.get("scsi") or []) if s]
        m = cls(
            name=str(d.get("name", "New machine")),
            system=normalise_system_id(d.get("system")),
            machine=str(d.get("machine", "g3beige")),
            ram_mb=int(d.get("ram_mb", 512)),
            rom=str(d.get("rom") or ""),
            display=str(d.get("display") or default_display()),
            audio=str(d.get("audio", "default")),
            onboard_romfile=(str(d["onboard_romfile"]) if d.get("onboard_romfile") else None),
            second_gpu=SecondGpu.from_dict(d.get("second_gpu")),
            network=Network.from_dict(d.get("network")),
            governor=Governor.from_dict(d.get("governor")),
            ata=ata,
            scsi=scsi,
            floppy=Floppy.from_dict(d.get("floppy")),
            extra_args=str(d.get("extra_args", "")),
            notes=str(d.get("notes", "")),
        )
        return m

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

    # ---- helpers ----
    def scsi_by_id(self, sid: int) -> ScsiDrive | None:
        for s in self.scsi:
            if s.id == sid:
                return s
        return None

    def first_empty_ata(self) -> int | None:
        for i, d in enumerate(self.ata):
            if d is None:
                return i
        return None

    def first_unfilled_ata(self) -> int | None:
        """First drive position carrying no image: empty, or a row where a
        type was chosen but no file picked."""
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
        """Every image this record points at, in no particular order."""
        return [f for _label, f in _image_files(self) if f]


def new_machine(name: str, system_id: str) -> Machine:
    """Seed a record from one of the five systems: the memory, the calibration
    governor, and whether the extra graphics card is fitted. Nothing that names
    a file is filled in -- not a disk, not a CD, not the Mac's ROM, not a
    graphics ROM -- and the notes start empty. The install folder is never
    searched for likely files."""
    p = SYSTEMS[normalise_system_id(system_id)]
    m = Machine(name=name, system=p.id, ram_mb=p.ram_mb, display=default_display())
    m.governor = Governor(p.governor)
    m.ata = [None, None, None, None]
    return m


def file_fields(m: Machine) -> dict[str, str]:
    """Every field of a record that names a file, for the guard that says
    none of them is ever filled in by the program."""
    fields = {"rom": m.rom or "",
              "onboard_romfile": m.onboard_romfile or "",
              "second_gpu.romfile": (m.second_gpu.romfile or "") if m.second_gpu else "",
              "floppy": m.floppy.file if m.floppy else ""}
    for i, d in enumerate(m.ata):
        fields[f"ata[{i}]"] = d.file if d else ""
    for s in m.scsi:
        fields[f"scsi[{s.id}]"] = s.file
    return fields


def start_blockers(m: Machine) -> list[str]:
    """Reasons this machine cannot be started yet, in plain words.

    Separate from :func:`validate` on purpose: a half-finished machine can be
    saved and come back to another day, but it cannot be run."""
    out = []
    if not (m.rom or "").strip():
        out.append("No ROM chosen.")
    return out


def default_identity(kind: str) -> Identity:
    src = DEFAULT_CDROM_IDENTITY if kind == "cdrom" else DEFAULT_DISK_IDENTITY
    return Identity(**src)


# ---------------------------------------------------------------- checking

def validate(m: Machine, qemu_dir: str | None, platform: str = paths.HOST_PLATFORM,
             check_files: bool = True, machine_dir: str | None = None) -> tuple[list[str], list[str]]:
    """Return (things that must be fixed, things worth knowing). Saving is
    blocked by the first list, allowed with the second."""
    errors: list[str] = []
    warnings: list[str] = []

    if not m.name or not m.name.strip():
        errors.append("This machine has no name.")
    elif not NAME_RE.match(m.name) or m.name.strip() != m.name:
        errors.append("That name will not work.")
    if not (RAM_MIN <= m.ram_mb <= RAM_MAX):
        errors.append(f"Memory has to be between {RAM_MIN} and {RAM_MAX} MB.")
    elif m.ram_mb > 1024:
        warnings.append("More than 1024 MB is untested.")
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
    if m.governor.mode == "mips" and not (1 <= m.governor.mips <= 100000):
        errors.append("The speed has to be between 1 and 100000.")
    if m.second_gpu:
        if m.second_gpu.addr and not ADDR_RE.match(m.second_gpu.addr):
            errors.append("The card slot has to look like 0x0e.")


    seen_ids = set()
    for s in m.scsi:
        if s.id in seen_ids:
            errors.append(f"Two SCSI drives are both set to device {s.id}.")
        seen_ids.add(s.id)
        if s.id == SCSI_SELF_ID:
            errors.append(f"SCSI device {SCSI_SELF_ID} is the Macintosh.")
        elif s.id not in SCSI_IDS:
            errors.append(f"SCSI device {s.id} does not exist.")
        if s.kind not in DRIVE_KINDS:
            errors.append(f"SCSI device {s.id} has no drive type.")
    if len(m.ata) != 4:
        errors.append("There are not four drive positions.")
    cd_in_wrong_place = False
    for i, d in enumerate(m.ata):
        if d is None or not d.file:
            continue            # nothing chosen here: an empty position, quietly ignored
        if d.kind not in DRIVE_KINDS:
            errors.append(f"{ata_slot_name(i)} has no drive type.")
        if d.kind == "cdrom" and i != ATA_CD_SLOT:
            cd_in_wrong_place = True
    if cd_in_wrong_place and (m.ata[ATA_CD_SLOT] is None or not m.ata[ATA_CD_SLOT].file):
        warnings.append(f"The CD is not in {ata_slot_name(ATA_CD_SLOT)}.")

    if check_files:
        qd = qemu_dir or ""
        if qd and paths.has_qemu(qd, platform):
            for label, rel in (("The Mac's ROM", m.rom),
                               ("The built-in graphics ROM", m.onboard_romfile),
                               ("The extra graphics card's ROM",
                                m.second_gpu.romfile if m.second_gpu else None)):
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
    for s in m.scsi:
        yield scsi_name(s.id), s.file
    if m.floppy:
        yield "The floppy disk", m.floppy.file


# ---------------------------------------------------------------- machines

@dataclass
class DeleteResult:
    """What ``Library.delete`` actually did."""

    folder: Path
    removed: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    folder_removed: bool = False

    @property
    def kept_images(self) -> list[str]:
        return [f for f in self.kept if looks_like_disk_image(f)]


class Library:
    """The Machines folder: one subfolder per machine, named after it."""

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
        m.name = name  # the folder is authoritative
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
        """Write machine.json; rename the folder if the name changed.

        Renaming moves the folder, so any image kept inside it moves with it:
        the record is re-pointed at the new location rather than left aiming
        at a path that no longer exists. Nothing is deleted or overwritten.
        """
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
        """A folder of that name is there, whatever is in it."""
        return self.folder(name).exists()

    def has_record(self, name: str) -> bool:
        """A machine of that name is really there. A bare folder is not one:
        creating a disk image before saving makes the folder first, and that
        must not read as "you already have a machine called this"."""
        return self.json_path(name).is_file()

    def duplicate(self, name: str, new_name: str) -> Machine:
        """Copy the record (and the Mac's saved settings) into a new folder.
        Disk images are not copied and not touched: the copy starts out
        pointing at the same image files."""
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
        """(files Delete would remove, files Delete would leave behind) --
        so the person can be told before anything happens."""
        contents = self.folder_contents(name)
        removed = [f for f in contents if f in OWNED_FILES]
        kept = [f for f in contents if f not in OWNED_FILES]
        return removed, kept

    def delete(self, name: str) -> DeleteResult:
        """Remove this machine's record, launcher and saved settings.

        Only the names in ``OWNED_FILES`` are ever deleted, and only directly
        inside the machine's own folder. A disk image -- whatever it is
        called, wherever it sits -- is never deleted, and the folder itself
        stays if anything is left in it.
        """
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
            # rmdir refuses on anything that is not empty, so this cannot
            # take a file with it.
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
        """{'nvram.img': size|None, 'pram.img': size|None}"""
        out: dict[str, int | None] = {}
        for f in SAVED_SETTINGS_FILES:
            p = self.folder(name) / f
            out[f] = p.stat().st_size if p.is_file() else None
        return out


    def clear_saved_settings(self, name: str) -> list[str]:
        """Delete nvram.img / pram.img -- fixed names, never an image."""
        removed = []
        for f in SAVED_SETTINGS_FILES:
            p = self.folder(name) / f
            if p.is_file():
                p.unlink()
                removed.append(f)
        return removed



IMAGE_NAME_SUFFIXES = (".img", ".qcow2", ".dsk")


def check_new_image_path(folder: Path | str, name: str, fmt: str) -> tuple[Path | None, str | None]:
    """Where a new disk image would be created, or a plain reason it cannot be.

    Refusing to write over anything that already exists is the point of this
    function: a disk image can be a whole afternoon of installing an old
    system, and Qemu-system-ppc GUI never overwrites one.
    """
    name = (name or "").strip()
    if not name:
        return None, "Give the disk a name."
    if "/" in name or "\\" in name or name in (".", ".."):
        return None, "The name cannot contain slashes."
    if not name.lower().endswith(IMAGE_NAME_SUFFIXES):
        name += ".qcow2" if fmt == "qcow2" else ".img"
    target = Path(folder) / name
    if target.exists() or target.is_symlink():
        return None, f"There is already a file called {name}."
    return target, None


def _repoint_images(m: Machine, moved: list[tuple[Path, Path]]) -> None:
    """After a folder rename, point image paths at the folder's new place, so
    an image kept inside the machine's folder is never left orphaned."""
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
    for s in m.scsi:
        s.file = fixed(s.file)
    if m.floppy:
        m.floppy.file = fixed(m.floppy.file)
