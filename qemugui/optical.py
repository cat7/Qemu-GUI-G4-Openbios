"""Host optical drives for a CD slot: macOS lists the discs diskutil
shows, Windows the CD-ROM drive letters.

No Tk in here. Listing never opens a drive. QEMU reads the drive as
/dev/diskN (macOS, root only; the launcher unmounts it first) or \\\\.\\D:
(Windows, data discs only).
"""

from __future__ import annotations

import plistlib
import re
import subprocess
import sys
from dataclasses import dataclass

DRIVE_CDROM = 5      # GetDriveType


@dataclass
class OpticalDrive:
    path: str            # /dev/diskN or D:
    label: str = ""

    @property
    def text(self) -> str:
        return f"{self.path}  {self.label}".strip()


def is_optical(info: dict) -> bool:
    """A diskutil info record of an optical drive or disc."""
    return bool(info.get("OpticalDeviceType") or info.get("OpticalMediaType"))


def parse_whole_disks(data: bytes) -> list[str]:
    """Whole-disk identifiers from ``diskutil list -plist``."""
    try:
        plist = plistlib.loads(data)
    except Exception:
        return []
    names = plist.get("WholeDisks") if isinstance(plist, dict) else None
    if isinstance(names, list):
        return [n for n in names if isinstance(n, str) and re.fullmatch(r"disk\d+", n)]
    return []


def drive_from_info(data: bytes) -> OpticalDrive | None:
    """The optical drive a ``diskutil info -plist`` record describes."""
    try:
        info = plistlib.loads(data)
    except Exception:
        return None
    if not isinstance(info, dict) or not is_optical(info):
        return None
    ident = info.get("DeviceIdentifier")
    if not isinstance(ident, str) or not re.fullmatch(r"disk\d+", ident):
        return None
    name = info.get("VolumeName") or info.get("MediaName") or ""
    kind = info.get("OpticalMediaType") or info.get("OpticalDeviceType") or ""
    label = " - ".join(str(x).strip() for x in (name, kind) if str(x).strip())
    return OpticalDrive(f"/dev/{ident}", label)


def macos_drives() -> list[OpticalDrive]:
    try:
        out = subprocess.run(["/usr/sbin/diskutil", "list", "-plist"], capture_output=True,
                             timeout=20)
        if out.returncode != 0:
            return []
        found = []
        for ident in parse_whole_disks(out.stdout):
            info = subprocess.run(["/usr/sbin/diskutil", "info", "-plist", ident],
                                  capture_output=True, timeout=20)
            d = drive_from_info(info.stdout) if info.returncode == 0 else None
            if d:
                found.append(d)
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
            found.append(OpticalDrive(f"{chr(65 + i)}:", label.value if ok else ""))
        return found
    except (OSError, AttributeError):
        return []


def host_drives(platform: str = sys.platform) -> list[OpticalDrive]:
    if platform == "darwin":
        return macos_drives()
    if platform.startswith("win"):
        return windows_drives()
    return []
