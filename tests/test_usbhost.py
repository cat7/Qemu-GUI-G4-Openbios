"""Host USB devices for QEMU's usb-host: finding and judging them, the
machine record, the launcher, and the machine editor's USB devices tab.

Run:  python -m unittest discover -s tests
"""

from __future__ import annotations

import json
import plistlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from qemugui import usbhost  # noqa: E402
from qemugui import paths  # noqa: E402
from qemugui import mac99_command as command  # noqa: E402
from qemugui import mac99_model as model  # noqa: E402
from qemugui.mac99_model import Machine, UsbHostDevice  # noqa: E402


def dev(vid, pid, name, speed, loc, children=(), cls=0):
    d = {"IOObjectClass": "IOUSBHostDevice", "idVendor": vid, "idProduct": pid,
         "Device Speed": speed, "locationID": loc, "bDeviceClass": cls,
         "IORegistryEntryChildren": list(children)}
    if name:
        d["USB Product Name"] = name
    return d


def media(cls, bsd, whole, children=()):
    return {"IOObjectClass": cls, "BSD Name": bsd, "Whole": whole, "Content": "x",
            "IORegistryEntryChildren": list(children)}


KEYBOARD = dev(0x05ac, 0x0202, "Apple USB Keyboard", 0, 0x2110000, [
    {"IOObjectClass": "IOUSBHostInterface", "bInterfaceClass": 3, "bInterfaceProtocol": 1}])
MOUSE = dev(0x046d, 0xc03d, "USB-PS/2 Optical Mouse", 0, 0x2120000, [
    {"IOObjectClass": "IOUSBHostInterface", "bInterfaceClass": 3, "bInterfaceProtocol": 0,
     "IORegistryEntryChildren": [{"IOObjectClass": "AppleUserUSBHostHIDDevice",
                                  "DeviceUsagePairs": [{"DeviceUsagePage": 1,
                                                        "DeviceUsage": 2}]}]}])
CAMERA = dev(0x046d, 0x0990, "", 2, 0x2200000, [
    {"IOObjectClass": "IOUSBHostInterface", "bInterfaceClass": 14},
    {"IOObjectClass": "IOUSBHostInterface", "bInterfaceClass": 1}])
DVD = dev(0x0e8d, 0x1887, "Portable Super Multi Drive", 2, 0x110000, [
    {"IOObjectClass": "IOUSBMassStorageDriver", "IORegistryEntryChildren": [
        media("IOCDMedia", "disk5", True, [media("IOMedia", "disk5s1", False)])]}])
MACDATA = dev(0x174c, 0x55aa, "ASM105X", 3, 0x240000, [
    {"IOObjectClass": "IOUSBMassStorageDriver", "IORegistryEntryChildren": [
        media("IOMedia", "disk4", True, [media("IOMedia", "disk4s1", False),
                                         media("IOMedia", "disk4s2", False)])]}])
HUB = dev(0x1d5c, 0x5011, "USB2.0 Hub", 2, 0x100000, [DVD], cls=9)
LED = dev(0x048d, 0x5702, "ITE Device", 1, 0x2300000, [
    {"IOObjectClass": "IOUSBHostInterface", "bInterfaceClass": 3, "bInterfaceProtocol": 0}])
HEADSET = dev(0x046d, 0x0a37, "Logitech USB Headset H540", 1, 0x2400000, [
    {"IOObjectClass": "IOUSBHostInterface", "bInterfaceClass": 1},
    {"IOObjectClass": "IOUSBHostInterface", "bInterfaceClass": 1},
    {"IOObjectClass": "IOUSBHostInterface", "bInterfaceClass": 3, "bInterfaceProtocol": 0}])
MOUNT = """\
/dev/disk3s1s1 on / (apfs, sealed, local, read-only, journaled)
/dev/disk4s2 on /Volumes/Data (hfs, local, nodev, nosuid, journaled, noowners)
/dev/disk5 on /Volumes/Audio CD (cddafs, local, nodev, nosuid, read-only)
map auto_home on /System/Volumes/Data/home (autofs, automounted, nobrowse)
"""


def judged(tree=None, mounts=MOUNT):
    tree = tree if tree is not None else [KEYBOARD, MOUSE, HUB, DVD, CAMERA, MACDATA]
    return usbhost.judge(usbhost.parse_ioreg(plistlib.dumps(tree)),
                         usbhost.parse_mounts(mounts))


class Finding(unittest.TestCase):
    def test_hid_only_refused_headset_kept(self):
        devs = {d.id: d for d in judged([LED, HEADSET, CAMERA])}
        self.assertEqual(devs["048d:5702"].reason, usbhost.HID_ONLY)
        self.assertEqual(devs["046d:0a37"].reason, "")
        self.assertEqual(devs["046d:0a37"].iface_classes, (1, 1, 3))
        self.assertTrue(devs["046d:0990"].passable)

    def test_hidden_note(self):
        self.assertEqual(usbhost.hidden_note(0), "")
        self.assertTrue(usbhost.hidden_note(1).startswith("1 device hidden (keyboards"))
        self.assertTrue(usbhost.hidden_note(3).startswith("3 devices hidden"))

    def test_devices_and_reasons(self):
        devs = {d.id: d for d in judged()}
        self.assertEqual(list(devs), ["05ac:0202", "046d:c03d", "0e8d:1887", "046d:0990",
                                      "174c:55aa"])        # hub out, DVD once
        self.assertEqual(devs["05ac:0202"].reason, "keyboard or mouse")
        self.assertEqual(devs["046d:c03d"].reason, "keyboard or mouse")
        self.assertEqual(devs["174c:55aa"].reason, "mounted disk")
        self.assertTrue(devs["0e8d:1887"].passable)     # mounted audio CD is fine
        self.assertTrue(devs["046d:0990"].passable)
        self.assertEqual(devs["046d:0990"].label, "USB device 046d:0990")
        self.assertEqual(devs["046d:0990"].speed, "high")
        self.assertEqual(devs["174c:55aa"].speed, "super")

    def test_disk_without_mounted_volume_may_go(self):
        devs = {d.id: d for d in judged([MACDATA], mounts="")}
        self.assertTrue(devs["174c:55aa"].passable)

    def test_mounts_parsing(self):
        m = usbhost.parse_mounts(MOUNT)
        self.assertEqual(m["disk4s2"], "/Volumes/Data")
        self.assertEqual(m["disk5"], "/Volumes/Audio CD")
        self.assertNotIn("auto_home", m)

    def test_bad_input(self):
        self.assertEqual(usbhost.parse_ioreg(b"not a plist"), [])

    @unittest.skipUnless(sys.platform == "darwin", "ioreg")
    def test_this_mac(self):
        """Whatever is plugged in here: the call works without privileges and
        never offers a keyboard or mouse."""
        for d in usbhost.host_devices():
            self.assertTrue(usbhost.normalize_id(d.id))
            if d.hid_input:
                self.assertFalse(d.passable)

    def test_bus_by_speed(self):
        self.assertEqual(usbhost.bus_for_speed("high"), "usb-bus.1")
        self.assertEqual(usbhost.bus_for_speed("super"), "usb-bus.1")
        self.assertEqual(usbhost.bus_for_speed("full"), "usb-bus.1")
        self.assertEqual(usbhost.bus_for_speed("low"), "usb-bus.1")
        self.assertEqual(usbhost.bus_for_speed(""), "usb-bus.1")


class Record(unittest.TestCase):
    def test_old_records_load_without_devices(self):
        d = json.loads(Machine(name="old").to_json())
        del d["usb_host_devices"]
        self.assertEqual(Machine.from_dict(d).usb_host_devices, [])

    def test_leopard_record_as_it_is(self):
        m = Machine.from_dict({"name": "Leopard", "usb_host_devices": [
            {"id": "0e8d:1887", "name": "Portable Super Multi Drive", "speed": "high"},
            {"id": "046d:0990", "name": "USB device 046d:0990", "speed": "high"}]})
        self.assertEqual([u.id for u in m.usb_host_devices], ["0e8d:1887", "046d:0990"])
        self.assertEqual(Machine.from_json(m.to_json()), m)

    def test_cleaning_and_validation(self):
        m = Machine.from_dict({"usb_host_devices": [{"id": "046D:0990"}, {"id": "046d:0990"},
                                                    "junk", {"id": "0e8d:1887"}]})
        self.assertEqual([u.id for u in m.usb_host_devices], ["046d:0990", "0e8d:1887"])
        self.assertEqual(Machine.from_dict({"usb_host_devices": "x"}).usb_host_devices, [])
        errors, _w = model.validate(Machine(name="L", usb_host_devices=[UsbHostDevice("zz")]),
                                    None, "darwin", check_files=False)
        self.assertTrue(any("zz" in e for e in errors))
        for platform in ("darwin", "win32"):
            _e, warnings = model.validate(Machine(name="L", usb_host_devices=[
                UsbHostDevice("046d:0990")]), None, platform, check_files=False)
            self.assertFalse(any("only work on" in w for w in warnings), platform)
        _e, warnings = model.validate(Machine(name="L", usb_host_devices=[
            UsbHostDevice("046d:0990")]), None, "linux", check_files=False)
        self.assertTrue(any("only work on a Mac or on Windows" in w for w in warnings))


QD = "/Applications/qemu-system-ppc-mac99"
MD = QD + "/Machines/Leopard"


class Launcher(unittest.TestCase):
    def machine(self, **kw) -> Machine:
        return Machine(name="Leopard", extra_args="-d guest_errors", usb_host_devices=[
            UsbHostDevice("0e8d:1887", "Portable Super Multi Drive", "high"),
            UsbHostDevice("0a12:0001", "BT", "full")], **kw)

    def test_usb_host_options(self):
        argv = command.build_argv(self.machine(), QD, MD, "darwin")
        i = argv.index("usb-host,vendorid=0x0e8d,productid=0x1887,bus=usb-bus.1")
        self.assertEqual(argv[i - 1], "-device")
        self.assertIn("usb-host,vendorid=0x0a12,productid=0x0001,bus=usb-bus.1", argv)
        self.assertEqual(argv[-2:], ["-d", "guest_errors"])
        for t in argv:
            self.assertNotIn("usb-ehci", t)
            self.assertNotIn("chardev", t)

    def test_devices_need_sudo_and_give_the_nvram_back(self):
        m = self.machine()
        self.assertTrue(command.needs_sudo(m, "darwin"))
        text = command.launcher_text(m, QD, MD, "darwin")
        self.assertIn("sudo -v", text)
        self.assertIn(f"sudo {QD}/qemu-system-ppc \\", text)
        self.assertIn('chown "${SUDO_USER:-$(id -un)}" nvram.img', text)
        for word in ("usbhostd", "attach", "USBREDIR"):
            self.assertNotIn(word, text)
        r = subprocess.run(["/bin/bash", "-n"], input=text, text=True, capture_output=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_no_devices_no_sudo(self):
        m = Machine(name="L")
        self.assertFalse(command.needs_sudo(m, "darwin"))
        self.assertNotIn("usb-host", " ".join(command.build_argv(m, QD, MD, "darwin")))

    def test_windows_only_devices_qemu_owns(self):
        m = self.machine()
        self.assertFalse(command.needs_sudo(m, "win32"))
        dvd = "usb-host,vendorid=0x0e8d,productid=0x1887,bus=usb-bus.1"
        bt = "usb-host,vendorid=0x0a12,productid=0x0001,bus=usb-bus.1"
        # nothing known about ownership: nothing passed through
        argv = command.build_argv(m, r"C:\q", r"C:\m", "win32")
        self.assertNotIn("usb-host", " ".join(argv))
        argv = command.build_argv(m, r"C:\q", r"C:\m", "win32", owned=set())
        self.assertNotIn("usb-host", " ".join(argv))
        # the DVD is on WinUSB, the other ticked device still Windows'
        argv = command.build_argv(m, r"C:\q", r"C:\m", "win32", owned={"0e8d:1887"})
        self.assertEqual(argv[argv.index(dvd) - 1], "-device")
        self.assertNotIn(bt, argv)
        # owned but not ticked: not passed through
        argv = command.build_argv(Machine(name="L"), r"C:\q", r"C:\m", "win32",
                                  owned={"0e8d:1887"})
        self.assertNotIn("usb-host", " ".join(argv))
        text = command.launcher_text(m, r"C:\q", r"C:\m", "win32",
                                     owned={"0e8d:1887", "0a12:0001"})
        self.assertIn(f'"{dvd}" ^', text)
        self.assertIn(f'"{bt}" ^', text)
        for word in ("sudo", "runas", "winusb-switch"):
            self.assertNotIn(word, text)

    def test_mac_ignores_ownership(self):
        m = self.machine()
        self.assertEqual(command.build_argv(m, QD, MD, "darwin"),
                         command.build_argv(m, QD, MD, "darwin", owned=set()))

    def test_linux_has_none(self):
        m = self.machine()
        self.assertNotIn("usb-host", " ".join(command.build_argv(m, "/q", "/m", "linux")))


def _tk_available():
    try:
        import tkinter
        r = tkinter.Tk(); r.withdraw(); r.destroy(); return True
    except Exception:
        return False


@unittest.skipUnless(_tk_available(), "no display")
class EditorTab(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.saved = (paths.HOST_PLATFORM, usbhost.host_devices)
        paths.HOST_PLATFORM = "darwin"
        usbhost.host_devices = lambda: judged()
        self.roots = []

    def tearDown(self):
        paths.HOST_PLATFORM, usbhost.host_devices = self.saved
        for r in self.roots:
            r.destroy()
        self.td.cleanup()

    def editor(self, m: Machine):
        import tkinter as tk
        from qemugui.mac99_ui_machine import MachineEditor
        root = tk.Tk()
        root.withdraw()
        self.roots.append(root)
        ed = MachineEditor(root, m, model.Library(self.td.name), "/q", on_save=lambda *a: None)
        ed.withdraw()
        return ed

    def text(self, ed, dev_id):
        return ed.usb_host_boxes[dev_id].cget("text")

    def test_refused_devices_are_hidden_and_counted(self):
        ed = self.editor(Machine(name="t"))
        self.assertEqual(list(ed.usb_host_vars), ["0e8d:1887", "046d:0990"])
        self.assertIn("USB device 046d:0990  (046d:0990, high speed)", self.text(ed, "046d:0990"))
        self.assertTrue(ed.usb_hidden_label.cget("text").startswith("3 devices hidden ("))
        self.assertNotIn("disabled", ed.usb_host_boxes["0e8d:1887"].state())

    def test_ticking_saves_id_name_and_speed(self):
        ed = self.editor(Machine(name="t"))
        self.assertEqual(ed.collect().usb_host_devices, [])
        ed.usb_host_vars["0e8d:1887"].set(True)
        ed.usb_host_vars["046d:0990"].set(True)
        self.assertEqual(ed.collect().usb_host_devices,
                         [UsbHostDevice("0e8d:1887", "Portable Super Multi Drive", "high"),
                          UsbHostDevice("046d:0990", "", "high")])

    def test_saved_but_absent_shows_not_connected(self):
        m = Machine(name="t", usb_host_devices=[UsbHostDevice("dead:beef", "Old scanner",
                                                              "full")])
        ed = self.editor(m)
        self.assertIn("Old scanner  (dead:beef, full speed) -- not connected",
                      self.text(ed, "dead:beef"))
        self.assertTrue(ed.usb_host_vars["dead:beef"].get())
        self.assertEqual(ed.collect().usb_host_devices, m.usb_host_devices)

    def test_saved_name_kept_and_refused_pick_dropped(self):
        m = Machine(name="t", usb_host_devices=[UsbHostDevice("046d:0990", "QuickCam", "high"),
                                                UsbHostDevice("174c:55aa", "External Disk", "super")])
        ed = self.editor(m)
        self.assertTrue(self.text(ed, "046d:0990").startswith("QuickCam  (046d:0990"))
        self.assertNotIn("174c:55aa", ed.usb_host_vars)
        self.assertEqual(ed.collect().usb_host_devices,
                         [UsbHostDevice("046d:0990", "QuickCam", "high")])

    def test_no_tab_on_linux(self):
        paths.HOST_PLATFORM = "linux"
        m = Machine(name="t", usb_host_devices=[UsbHostDevice("0e8d:1887", "DVD", "high")])
        ed = self.editor(m)
        self.assertFalse(hasattr(ed, "usb_host_vars"))
        self.assertEqual(ed.collect().usb_host_devices, m.usb_host_devices)


if __name__ == "__main__":
    unittest.main()
