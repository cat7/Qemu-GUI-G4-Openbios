"""winusb-switch output format.

Run:  python -m unittest discover -s tests
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from qemugui import winusb as wl  # noqa: E402
from qemugui import paths  # noqa: E402
from qemugui import mac99_model as model  # noqa: E402
from qemugui.mac99_model import Machine, UsbHostDevice  # noqa: E402

LIST = r'''
{"op":"list","warnings":[]}
{"id":"046d:0990","instance":"USB\\VID_046D&PID_0990\\8C5C6B32","description":"USB Composite Device","product":"Camera","speed":"high","class":"USB","class_guid":"{36fc9e60-c465-11cf-8056-444553540000}","service":"usbccgp","inf":"usb.inf","driver":"USB Composite Device","composite":true,"winusb":false,"problem":0,"functions":[{"class":"Camera","service":"usbvideo"},{"class":"MEDIA","service":"usbaudio"}],"refuse":""}
{"id":"0e8d:1887","instance":"USB\\VID_0E8D&PID_1887\\5&1A2B3C&0&3","description":"WinUsb Device","product":"MT1887","speed":"high","class":"USBDevice","class_guid":"{88bae032-5a81-49f0-bc3d-a4ff138216d6}","service":"WinUSB","inf":"oem42.inf","driver":"USB device 0e8d:1887 for QEMU (WinUSB)","provider":"winusb-switch","composite":false,"winusb":true,"problem":0,"functions":[],"refuse":""}

{"id":"046d:c52b","instance":"USB\\VID_046D&PID_C52B\\6&1&0&2","description":"USB Composite Device","product":"USB Receiver","speed":"full","class":"USB","class_guid":"{36fc9e60-c465-11cf-8056-444553540000}","service":"usbccgp","inf":"usb.inf","driver":"USB Composite Device","composite":true,"winusb":false,"problem":0,"functions":[{"class":"HIDClass","service":"HidUsb"}],"refuse":"keyboard or mouse"}
'''


class ListFormat(unittest.TestCase):
    def test_parse(self):
        devs = wl.parse_list(LIST)
        self.assertEqual([d.id for d in devs],
                         ["046d:0990", "0e8d:1887", "046d:c52b"])
        cam, dvd, rx = devs
        self.assertTrue(cam.composite)
        self.assertFalse(cam.winusb)
        self.assertEqual(cam.functions[0]["service"], "usbvideo")
        self.assertTrue(cam.switchable)
        self.assertEqual(cam.speed, "high")
        self.assertEqual(rx.speed, "full")
        self.assertEqual(cam.state, "Windows driver (usbccgp)")
        self.assertEqual(dvd.state, "WinUSB (ready for QEMU)")
        self.assertEqual(cam.label, "Camera")
        self.assertTrue(dvd.winusb)
        self.assertEqual(dvd.inf, "oem42.inf")
        self.assertEqual(dvd.provider, "winusb-switch")
        self.assertEqual(cam.provider, "")
        self.assertFalse(rx.switchable)
        self.assertEqual(rx.refuse, "keyboard or mouse")

    def test_warnings(self):
        devs = wl.parse_list(LIST)
        self.assertEqual(devs.warnings, [])
        self.assertEqual(wl.warnings_text(devs), "")
        text = LIST.replace('"warnings":[]', '"warnings":["UsbDk filter installed (from '
                            'virt-viewer?) - devices may not return to their Windows '
                            'drivers","046d:0a37: driver package oem12.inf installs '
                            'libusbK"]')
        devs = wl.parse_list(text)
        self.assertEqual(len(devs), 3)
        self.assertEqual(len(devs.warnings), 2)
        self.assertTrue(wl.warnings_text(devs).startswith("UsbDk filter installed"))
        self.assertEqual(wl.warnings_text([]), "")

    def test_bad_lines(self):
        with self.assertRaises(ValueError):
            wl.parse_list('{"id":"46d:990","instance":"x"}')
        with self.assertRaises(ValueError):
            wl.parse_list('{"id":"046d:0990"}')
        with self.assertRaises(ValueError):
            wl.parse_list('[1,2]')
        with self.assertRaises(ValueError):
            wl.parse_list('{"op":"list","ok":false,"error":"cannot enumerate"}')

    def test_result(self):
        r = wl.parse_result('{"op":"bind","id":"046d:0990","ok":true,'
                            '"method":"class-guid","service":"WinUSB",'
                            '"reboot":false}\n')
        self.assertTrue(r["ok"])
        r = wl.parse_result('{"op":"unbind","id":"046d:0990","ok":false,'
                            '"refuse":"not on WinUSB"}')
        self.assertEqual(r["refuse"], "not on WinUSB")
        r = wl.parse_result('{"op":"unbind","id":"046d:0990","ok":false,'
                            '"refuse":"in use","veto_type":5,'
                            '"veto":"outstanding open",'
                            '"veto_name":"USB\\\\VID_046D&PID_0990\\\\1"}')
        self.assertEqual(r["veto"], "outstanding open")
        r = wl.parse_result('{"op":"unbind","ok":true,"reboot":true,'
                            '"status":"pending_reboot"}')
        self.assertEqual(r["status"], "pending_reboot")
        with self.assertRaises(ValueError):
            wl.parse_result('{"ok":true}')
        with self.assertRaises(ValueError):
            wl.parse_result('{"op":"bind","ok":true}\n{"op":"bind","ok":true}')


class Ownership(unittest.TestCase):
    def test_note(self):
        self.assertEqual(wl.passthrough_note([], None, "x"), "")
        self.assertEqual(wl.passthrough_note(["046d:0990"], None, "no helper"),
                         "No USB device is passed through: no helper")
        self.assertEqual(wl.passthrough_note(["0e8d:1887"], {"0e8d:1887"}, ""), "")
        self.assertIn("046d:0990", wl.passthrough_note(["046d:0990", "0e8d:1887"],
                                                       {"0e8d:1887"}, ""))

    def test_owned_ids(self):
        saved = wl.list_devices
        try:
            wl.list_devices = lambda folder=None: (wl.parse_list(LIST), "")
            self.assertEqual(wl.owned_ids(), ({"0e8d:1887"}, ""))
            wl.list_devices = lambda folder=None: ([], wl.missing_message())
            owned, why = wl.owned_ids()
            self.assertIsNone(owned)
            self.assertIn("not next to", why)
        finally:
            wl.list_devices = saved


class Helper(unittest.TestCase):
    """winusb-switch.exe beside qemu-system-ppc.exe: list unelevated, bind
    and unbind through the UAC prompt with the result in a file."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        paths.use_install_dir(self.td.name)
        self.saved = (wl.shell_execute_runas, wl.subprocess.run)
        self.calls = []

    def tearDown(self):
        wl.shell_execute_runas, wl.subprocess.run = self.saved
        paths.use_install_dir(None)
        self.td.cleanup()

    def install(self):
        (Path(self.td.name) / "winusb-switch.exe").write_bytes(b"MZ")

    def fake_runas(self, result: dict | None, code: int = 0):
        def runas(exe, params):
            self.calls.append((exe, params))
            if result is None:
                return None
            out = re.search(r'--out "([^"]+)"', params).group(1)
            Path(out).write_text(json.dumps(result) + "\n", encoding="utf-8")
            return code
        wl.shell_execute_runas = runas

    def test_missing_helper(self):
        devs, why = wl.list_devices()
        self.assertEqual(devs, [])
        self.assertIn("winusb-switch.exe is not next to qemu-system-ppc.exe", why)
        res = wl.run_elevated("bind", "046d:0990")
        self.assertFalse(res["ok"])
        self.assertIn("not next to", wl.outcome_text(res))

    def test_list_runs_unelevated_without_a_window(self):
        self.install()
        seen = {}

        def run(argv, **kw):
            seen.update(argv=argv, kw=kw)
            return subprocess.CompletedProcess(argv, 0, LIST.encode(), b"")
        wl.subprocess.run = run
        devs, why = wl.list_devices()
        self.assertEqual(why, "")
        self.assertEqual([d.id for d in devs], ["046d:0990", "0e8d:1887", "046d:c52b"])
        self.assertEqual(seen["argv"][1:], ["list"])
        self.assertTrue(seen["argv"][0].endswith("winusb-switch.exe"))
        self.assertEqual(seen["kw"]["creationflags"], wl.CREATE_NO_WINDOW)

    def test_bind_through_runas_and_the_result_file(self):
        self.install()
        self.fake_runas({"op": "bind", "id": "046d:0990", "ok": True, "method": "class-guid",
                         "service": "WINUSB", "status": "done"})
        res = wl.run_elevated("bind", "046d:0990")
        exe, params = self.calls[0]
        self.assertTrue(exe.endswith("winusb-switch.exe"))
        self.assertTrue(params.startswith("bind 046d:0990 --out "))
        self.assertTrue(res["ok"])
        self.assertEqual(res["exit"], 0)
        self.assertEqual(wl.outcome_text(res), "Given to QEMU: the device is on WinUSB.")

    def test_outcomes(self):
        self.install()
        self.fake_runas(None)
        self.assertEqual(wl.outcome_text(wl.run_elevated("unbind", "046d:0990")),
                         "Nothing changed: the administrator prompt was cancelled.")
        self.fake_runas({"op": "unbind", "ok": False, "refuse": "in use", "veto_type": 5}, 5)
        self.assertIn("Quit the program using it (QEMU, Camera app) first",
                      wl.outcome_text(wl.run_elevated("unbind", "046d:0990")))
        self.fake_runas({"op": "unbind", "ok": True, "reboot": True,
                         "status": "pending_reboot"}, 3010)
        self.assertEqual(wl.outcome_text(wl.run_elevated("unbind", "046d:0990")),
                         "Replug the device to finish.")
        self.fake_runas({"op": "unbind", "ok": True, "service": "usbccgp", "status": "done"})
        self.assertEqual(wl.outcome_text(wl.run_elevated("unbind", "046d:0990")),
                         "Given back to Windows (usbccgp).")
        self.fake_runas({"op": "bind", "ok": False, "error": "cannot open device",
                         "detail": {"code": "0x2", "message": "not found"}}, 1)
        self.assertEqual(wl.outcome_text(wl.run_elevated("bind", "046d:0990")),
                         "Failed: cannot open device (not found).")

    def test_no_result_file(self):
        self.install()
        wl.shell_execute_runas = lambda exe, params: 2
        res = wl.run_elevated("bind", "zz")
        self.assertFalse(res["ok"])
        self.assertIn("exit code 2", res["error"])


def _tk_available():
    try:
        import tkinter
        r = tkinter.Tk(); r.withdraw(); r.destroy(); return True
    except Exception:
        return False


@unittest.skipUnless(_tk_available(), "no display")
class WindowsEditorTab(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.saved = (paths.HOST_PLATFORM, wl.list_devices, wl.run_elevated)
        paths.HOST_PLATFORM = "win32"
        self.listing = LIST
        wl.list_devices = lambda folder=None: (wl.parse_list(self.listing), "")
        self.runs = []
        self.roots = []

    def tearDown(self):
        paths.HOST_PLATFORM, wl.list_devices, wl.run_elevated = self.saved
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

    def test_rows_state_speed_and_hidden_refusals(self):
        ed = self.editor(Machine(name="t"))
        self.assertEqual(list(ed.usb_host_vars), ["046d:0990", "0e8d:1887"])
        self.assertTrue(ed.usb_hidden_label.cget("text").startswith("1 device hidden ("))
        self.assertEqual(self.text(ed, "046d:0990"),
                         "Camera  (046d:0990, high speed) -- Windows driver (usbccgp)")
        self.assertEqual(self.text(ed, "0e8d:1887"),
                         "MT1887  (0e8d:1887, high speed) -- WinUSB (ready for QEMU)")
        give, back = ed.usb_host_buttons["046d:0990"]
        self.assertNotIn("disabled", give.state())
        self.assertIn("disabled", back.state())
        give, back = ed.usb_host_buttons["0e8d:1887"]
        self.assertIn("disabled", give.state())
        self.assertNotIn("disabled", back.state())

    def test_refused_but_on_winusb_can_go_back(self):
        self.listing = LIST + ('{"id":"048d:5702","instance":"USB\\\\VID_048D&PID_5702\\\\1",'
                               '"product":"ITE Device","speed":"full","service":"WinUSB",'
                               '"winusb":true,"refuse":"HID only"}\n')
        ed = self.editor(Machine(name="t"))
        self.assertIn("048d:5702", ed.usb_host_vars)
        self.assertIn("HID only", self.text(ed, "048d:5702"))
        self.assertIn("disabled", ed.usb_host_boxes["048d:5702"].state())
        give, back = ed.usb_host_buttons["048d:5702"]
        self.assertIn("disabled", give.state())
        self.assertNotIn("disabled", back.state())

    def test_tick_only_what_qemu_owns(self):
        ed = self.editor(Machine(name="t"))
        self.assertIn("disabled", ed.usb_host_boxes["046d:0990"].state())     # Windows'
        self.assertNotIn("disabled", ed.usb_host_boxes["0e8d:1887"].state())  # on WinUSB
        ed.usb_host_vars["0e8d:1887"].set(True)
        self.assertEqual(ed.usb_host_notes["0e8d:1887"].cget("text"), "")
        self.assertEqual(ed.collect().usb_host_devices,
                         [UsbHostDevice("0e8d:1887", "MT1887", "high")])

    def test_saved_tick_kept_while_windows_owns_it(self):
        ed = self.editor(Machine(name="t", usb_host_devices=[
            UsbHostDevice("046d:0990", "Camera", "high")]))
        self.assertTrue(ed.usb_host_vars["046d:0990"].get())
        self.assertIn("disabled", ed.usb_host_boxes["046d:0990"].state())
        self.assertIn("not passed through: owned by Windows",
                      ed.usb_host_notes["046d:0990"].cget("text"))
        give, _back = ed.usb_host_buttons["046d:0990"]
        self.assertNotIn("disabled", give.state())
        self.assertEqual(ed.collect().usb_host_devices,
                         [UsbHostDevice("046d:0990", "Camera", "high")])

    def test_switch_refreshes_the_main_window(self):
        ed = self.editor(Machine(name="t"))
        calls = []
        ed.master.refresh_details = lambda: calls.append(1)
        wl.run_elevated = lambda op, i, folder=None: {"op": op, "ok": True, "status": "done"}
        ed._usb_switch("bind", "046d:0990", wait=True)
        self.assertEqual(calls, [1])

    def test_give_to_qemu_refreshes_and_keeps_ticks(self):
        ed = self.editor(Machine(name="t", usb_host_devices=[
            UsbHostDevice("046d:0990", "Camera", "high"),
            UsbHostDevice("dead:beef", "Old scanner", "full")]))
        self.assertIn("not connected", self.text(ed, "dead:beef"))

        def run(op, dev_id, folder=None):
            self.runs.append((op, dev_id))
            self.listing = LIST.replace('"service":"usbccgp","inf":"usb.inf","driver":"USB '
                                        'Composite Device","composite":true,"winusb":false',
                                        '"service":"WINUSB","inf":"winusb.inf","driver":"WinUsb '
                                        'Device","composite":false,"winusb":true', 1)
            return {"op": op, "id": dev_id, "ok": True, "status": "done", "exit": 0}
        wl.run_elevated = run
        ed._usb_switch("bind", "046d:0990", wait=True)
        self.assertEqual(self.runs, [("bind", "046d:0990")])
        self.assertIn("WinUSB (ready for QEMU)", self.text(ed, "046d:0990"))
        self.assertTrue(ed.usb_host_vars["046d:0990"].get())
        self.assertTrue(ed.usb_host_vars["dead:beef"].get())
        self.assertEqual(ed.usb_host_notes["046d:0990"].cget("text"), "")
        self.assertEqual(ed.usb_status.cget("text"), "Given to QEMU: the device is on WinUSB.")

    def test_in_use_and_cancel_messages(self):
        ed = self.editor(Machine(name="t"))
        wl.run_elevated = lambda op, i, folder=None: {"op": op, "ok": False,
                                                      "refuse": "in use", "exit": 5}
        ed._usb_switch("unbind", "0e8d:1887", wait=True)
        self.assertIn("Quit the program using it", ed.usb_status.cget("text"))
        wl.run_elevated = lambda op, i, folder=None: {"op": op, "ok": False, "cancelled": True}
        ed._usb_switch("bind", "046d:0990", wait=True)
        self.assertIn("cancelled", ed.usb_status.cget("text"))
        self.assertFalse(ed.usb_busy)

    def test_warnings_line(self):
        self.listing = LIST.replace('"warnings":[]',
                                    '"warnings":["UsbDk filter installed (from virt-viewer?)'
                                    ' - devices may not return to their Windows drivers"]')
        ed = self.editor(Machine(name="t"))
        self.assertTrue(ed.usb_warning_label.cget("text").startswith("UsbDk filter installed"))
        self.listing = LIST
        ed2 = self.editor(Machine(name="t"))
        self.assertEqual(ed2.usb_warning_label.cget("text"), "")

    def test_helper_missing_is_said(self):
        wl.list_devices = lambda folder=None: ([], wl.missing_message())
        ed = self.editor(Machine(name="t", usb_host_devices=[
            UsbHostDevice("046d:0990", "Camera", "high")]))
        texts = [w.cget("text") for w in ed.usb_host_list.winfo_children()
                 if w.winfo_class() == "TLabel"]
        self.assertTrue(any("winusb-switch.exe is not next to" in t for t in texts), texts)
        self.assertTrue(ed.usb_host_vars["046d:0990"].get())


@unittest.skipUnless(_tk_available(), "no display")
class WindowsMainWindowCommandLine(unittest.TestCase):
    """The preview and run.bat take a ticked device only while QEMU owns it,
    asking winusb-switch each time."""

    def setUp(self):
        from qemugui import mac99_ui_main as ui
        self.ui = ui
        self.td = tempfile.TemporaryDirectory()
        self.saved = (paths.HOST_PLATFORM, wl.list_devices)
        paths.HOST_PLATFORM = "win32"
        paths.use_install_dir(self.td.name)
        self.listing = (wl.parse_list(LIST), "")
        wl.list_devices = lambda folder=None: self.listing
        self.windows = []

    def tearDown(self):
        paths.HOST_PLATFORM, wl.list_devices = self.saved
        for w in self.windows:
            w.destroy()
        paths.use_install_dir(None)
        self.td.cleanup()

    def window(self):
        lib = model.Library(Path(self.td.name) / "Machines")
        lib.save(Machine(name="L", usb_host_devices=[
            UsbHostDevice("046d:0990", "Camera", "high"),
            UsbHostDevice("0e8d:1887", "MT1887", "high")]))
        w = self.ui.MainWindow(paths.Settings(last_machine="L"),
                               settings_path=Path(self.td.name) / "settings.json")
        w.withdraw()
        self.windows.append(w)
        w.refresh_list(select="L")
        return w

    def preview(self, w):
        return w.command_line.get("1.0", "end")

    def test_only_owned_ticked_devices(self):
        w = self.window()
        text = self.preview(w)
        self.assertIn("vendorid=0x0e8d,productid=0x1887", text)
        self.assertNotIn("vendorid=0x046d,productid=0x0990", text)
        self.assertIn("owned by Windows (Give to QEMU first): 046d:0990", text)

    def test_missing_helper_passes_nothing(self):
        self.listing = ([], wl.missing_message())
        w = self.window()
        text = self.preview(w)
        self.assertNotIn("usb-host", text)
        self.assertIn("No USB device is passed through: winusb-switch.exe is not next to",
                      text)

    def test_run_bat_follows_ownership(self):
        w = self.window()
        m = w.library.load("L")
        w._write_launcher(m)
        bat = (w.library.folder("L") / "run.bat").read_text(encoding="utf-8")
        self.assertIn("vendorid=0x0e8d,productid=0x1887", bat)
        self.assertNotIn("vendorid=0x046d", bat)
        self.listing = (wl.parse_list(LIST.replace('"winusb":false', '"winusb":true', 1)), "")
        w._write_launcher(m)
        bat = (w.library.folder("L") / "run.bat").read_text(encoding="utf-8")
        self.assertIn("vendorid=0x046d,productid=0x0990", bat)


if __name__ == "__main__":
    unittest.main()
