"""Host optical drives for a CD slot, with or without a disc: macOS lists
the drives IOKit knows (named "<vendor> <product>", the name QEMU's
host_cdrom drive= option matches), Windows the CD-ROM drive letters.

No Tk in here. Listing never opens a drive. QEMU owns the drive while the
machine runs and follows disc changes itself.
"""

from __future__ import annotations

import plistlib
import re
import subprocess
import sys
from dataclasses import dataclass

from .paths import DRIVE_PREFIX

DRIVE_CDROM = 5      # GetDriveType

MEDIA_KINDS = {"IOCDMedia": "CD", "IODVDMedia": "DVD", "IOBDMedia": "BD"}


@dataclass
class OpticalDrive:
    path: str            # what a CD slot stores: drive:<name> or D:
    name: str = ""       # drive model, or the letter
    disc: str = ""       # disc title or kind; "" without a disc

    @property
    def text(self) -> str:
        return f"{self.name or self.path}  -  {self.disc or 'no disc'}"


def drive_name(vendor: str, product: str) -> str:
    """The drive's name as QEMU builds it: vendor and product, white space
    folded."""
    return " ".join(f"{vendor or ''} {product or ''}".split())


def _whole_media(node: dict) -> tuple[str, str] | None:
    """(BSD name, media class) of the first whole-disc media below *node*."""
    for child in node.get("IORegistryEntryChildren") or []:
        if not isinstance(child, dict):
            continue
        bsd = child.get("BSD Name")
        if child.get("Whole") is True and isinstance(bsd, str) and bsd:
            return bsd, str(child.get("IOObjectClass") or "")
        found = _whole_media(child)
        if found:
            return found
    return None


def parse_ioreg(data: bytes) -> list[tuple[str, str, str]]:
    """(drive name, disc BSD name or "", media kind or "") per drive, from
    ``ioreg -a -l -r -c IOCDBlockStorageDevice``."""
    try:
        nodes = plistlib.loads(data)
    except Exception:
        return []
    if isinstance(nodes, dict):
        nodes = [nodes]
    if not isinstance(nodes, list):
        return []
    out = []
    for n in nodes:
        if not isinstance(n, dict):
            continue
        dc = n.get("Device Characteristics")
        if not isinstance(dc, dict):
            continue
        name = drive_name(str(dc.get("Vendor Name") or ""), str(dc.get("Product Name") or ""))
        if not name:
            continue
        media = _whole_media(n)
        bsd, cls = media if media else ("", "")
        out.append((name, bsd, MEDIA_KINDS.get(cls, "disc" if bsd else "")))
    return out


def disc_title(data: bytes) -> str:
    """The volume name from ``diskutil info -plist`` of a disc, or ""."""
    try:
        info = plistlib.loads(data)
    except Exception:
        return ""
    if not isinstance(info, dict):
        return ""
    return str(info.get("VolumeName") or "").strip()


def macos_drives() -> list[OpticalDrive]:
    try:
        out = subprocess.run(["/usr/sbin/ioreg", "-a", "-l", "-r", "-c", "IOCDBlockStorageDevice"],
                             capture_output=True, timeout=20)
        if out.returncode != 0:
            return []
        found = []
        for name, bsd, kind in parse_ioreg(out.stdout):
            disc = ""
            if bsd and re.fullmatch(r"disk\d+", bsd):
                info = subprocess.run(["/usr/sbin/diskutil", "info", "-plist", bsd],
                                      capture_output=True, timeout=20)
                title = disc_title(info.stdout) if info.returncode == 0 else ""
                disc = " - ".join(x for x in (title, kind) if x)
            found.append(OpticalDrive(DRIVE_PREFIX + name, name, disc))
        return found
    except (OSError, subprocess.SubprocessError):
        return []


def windows_drives() -> list[OpticalDrive]:
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        mask = k32.GetLogicalDrives()
        found = []
        for i in range(26):
            if not mask & (1 << i):
                continue
            root = f"{chr(65 + i)}:\\"
            if k32.GetDriveTypeW(root) != DRIVE_CDROM:
                continue
            label = ctypes.create_unicode_buffer(261)
            ok = k32.GetVolumeInformationW(root, label, 261, None, None, None, None, 0)
            disc = (label.value or "data disc") if ok else "no disc, or an audio CD"
            letter = f"{chr(65 + i)}:"
            found.append(OpticalDrive(letter, letter, disc))
        return found
    except (OSError, AttributeError):
        return []


def host_drives(platform: str = sys.platform) -> list[OpticalDrive]:
    if platform == "darwin":
        return macos_drives()
    if platform.startswith("win"):
        return windows_drives()
    return []
