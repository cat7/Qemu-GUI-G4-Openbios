"""Host USB devices for QEMU's ``usb-host`` device: what is plugged in, what
may never be passed through, and the QEMU options.

No Tk in here, no privileges needed. QEMU takes a device from macOS only
when it runs as root, and as root it can take anything, so the list itself
refuses what must stay with the host: keyboards and mice, devices holding a
mounted volume that is not optical media (a DVD drive with a mounted disc
may go; a disk with mounted volumes may not), and devices made only of HID
interfaces (LED controllers, macro pads; a headset's HID button stays).
"""

from __future__ import annotations

import plistlib
import re
import subprocess
from dataclasses import dataclass

ID_RE = re.compile(r"^[0-9a-f]{4}:[0-9a-f]{4}$")

# mac99 with nec-usb=on: usb-bus.0 is the KeyLargo OHCI, usb-bus.1 the NEC
# card's EHCI, which hands full- and low-speed devices to its OHCI companions.
BUS_HIGH_SPEED = "usb-bus.1"
BUS_FULL_SPEED = "usb-bus.1"

SPEEDS = {0: "low", 1: "full", 2: "high", 3: "super", 4: "super"}

KEYBOARD_OR_MOUSE = "keyboard or mouse"
MOUNTED_DISK = "mounted disk"
HID_ONLY = "HID only"
HIDDEN_KINDS = "keyboards, mice, hubs, disks, Bluetooth, HID-only devices"


def hidden_note(n: int) -> str:
    """The one line standing in for the devices a tab leaves out."""
    if n <= 0:
        return ""
    return f"{n} device{'s' if n != 1 else ''} hidden ({HIDDEN_KINDS})"

OPTICAL_CLASSES = {"IOCDMedia", "IODVDMedia", "IOBDMedia"}
# Generic Desktop pointer, mouse, keyboard, keypad
HID_INPUT_USAGES = {(1, 1), (1, 2), (1, 6), (1, 7)}


def normalize_id(text: str) -> str | None:
    """'046D:0990' -> '046d:0990'; anything else -> None."""
    t = (text or "").strip().lower()
    return t if ID_RE.match(t) else None


def bus_for_speed(speed: str) -> str:
    return BUS_HIGH_SPEED if speed in ("high", "super") else BUS_FULL_SPEED


@dataclass
class HostDevice:
    id: str
    name: str = ""
    speed: str = ""
    reason: str = ""          # why it may never be passed through; "" = it may
    location: int = 0
    hid_input: bool = False
    media: tuple = ()         # ((bsd name, optical), ...)
    iface_classes: tuple = ()  # bInterfaceClass of each interface

    @property
    def label(self) -> str:
        return self.name or f"USB device {self.id}"

    @property
    def passable(self) -> bool:
        return not self.reason


def _hid_input(node: dict) -> bool:
    if node.get("bInterfaceClass") == 3 and node.get("bInterfaceProtocol") in (1, 2):
        return True
    if (node.get("PrimaryUsagePage"), node.get("PrimaryUsage")) in HID_INPUT_USAGES:
        return True
    for pair in node.get("DeviceUsagePairs") or []:
        if isinstance(pair, dict) and \
                (pair.get("DeviceUsagePage"), pair.get("DeviceUsage")) in HID_INPUT_USAGES:
            return True
    return False


def parse_ioreg(data: bytes) -> list[HostDevice]:
    """Devices from ``ioreg -a -r -c IOUSBHostDevice -l`` (service plane):
    each device with the keyboard/mouse interfaces and the disks below it.
    Hubs are left out; a device that also appears inside a hub's subtree is
    listed once."""
    try:
        roots = plistlib.loads(data)
    except Exception:
        return []
    found: dict[int, HostDevice] = {}
    order: list[int] = []

    def walk(node, dev: HostDevice | None, optical: bool):
        if not isinstance(node, dict):
            return
        if node.get("IOObjectClass") == "IOUSBHostDevice" and \
                isinstance(node.get("idVendor"), int) and isinstance(node.get("idProduct"), int):
            if node.get("bDeviceClass") == 9:
                dev = None          # a hub: its devices are handled on their own
            else:
                name = node.get("USB Product Name") or node.get("kUSBProductString") or ""
                dev = HostDevice(f"{node['idVendor']:04x}:{node['idProduct']:04x}",
                                 str(name).strip(), SPEEDS.get(node.get("Device Speed"), ""),
                                 location=int(node.get("locationID") or 0))
                if dev.location in found:
                    return
                found[dev.location] = dev
                order.append(dev.location)
            optical = False
        elif dev is not None:
            if node.get("IOObjectClass") == "IOUSBHostInterface" and \
                    isinstance(node.get("bInterfaceClass"), int):
                dev.iface_classes += (node["bInterfaceClass"],)
            if _hid_input(node):
                dev.hid_input = True
            if node.get("IOObjectClass") in OPTICAL_CLASSES:
                optical = True
            bsd = node.get("BSD Name")
            if isinstance(bsd, str) and "Whole" in node and "Content" in node:
                dev.media += ((bsd, optical),)
        for child in node.get("IORegistryEntryChildren") or []:
            walk(child, dev, optical)

    for root in (roots if isinstance(roots, list) else [roots]):
        walk(root, None, False)
    return [found[loc] for loc in order]


def parse_mounts(text: str) -> dict[str, str]:
    """``mount`` output -> {bsd name: mount point} for /dev/ devices."""
    out = {}
    for line in (text or "").splitlines():
        m = re.match(r"^/dev/(\S+) on (.*) \(", line)
        if m:
            out[m.group(1)] = m.group(2)
    return out


def judge(devices: list[HostDevice], mounts: dict[str, str]) -> list[HostDevice]:
    """Fills in ``reason`` for what may never be passed through."""
    for d in devices:
        if d.hid_input:
            d.reason = KEYBOARD_OR_MOUSE
        elif any(not optical and bsd in mounts for bsd, optical in d.media):
            d.reason = MOUNTED_DISK
        elif d.iface_classes and all(c == 3 for c in d.iface_classes):
            d.reason = HID_ONLY
        else:
            d.reason = ""
    return devices


def host_devices() -> list[HostDevice]:
    """What is plugged in now, judged. Never opens a device."""
    try:
        tree = subprocess.run(["/usr/sbin/ioreg", "-a", "-r", "-c", "IOUSBHostDevice", "-l",
                               "-w0"], capture_output=True, timeout=15)
        mounts = subprocess.run(["/sbin/mount"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return []
    if tree.returncode != 0:
        return []
    return judge(parse_ioreg(tree.stdout), parse_mounts(mounts.stdout))


def qemu_tokens(devices) -> list[str]:
    """-device usb-host for [(id, speed)], on the bus the speed picks."""
    out: list[str] = []
    for dev_id, speed in devices:
        vid, pid = dev_id.split(":")
        out += ["-device", f"usb-host,vendorid=0x{vid},productid=0x{pid},"
                           f"bus={bus_for_speed(speed)}"]
    return out
