"""Tk-backed tests for the mac99 editor: the two checkboxes whose on-screen
sense is the opposite of the field they write, and the GPU choice's effect
on one of them.

These build a real (withdrawn) Tk window, so they are skipped where there is
no working tkinter -- see qemugui/mac99_ui_machine.py for the pure layer
these exercise.

Run:  python -m unittest discover -s tests
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from qemugui import mac99_model as model  # noqa: E402
from qemugui import paths  # noqa: E402
from qemugui.mac99_model import Machine, PromEnv, Gpu, AtaDrive, UsbStorage  # noqa: E402


def _tk_available():
    try:
        import tkinter
        r = tkinter.Tk(); r.withdraw(); r.destroy(); return True
    except Exception:
        return False


@unittest.skipUnless(_tk_available(), "no display")
class CheckboxPolarity(unittest.TestCase):
    """"Boot into Open Firmware" and "Do not load vga driver" ask the
    opposite question from the field they write (user review, 2026-09-14).
    Both states of both checkboxes are exercised here, independent of the
    other and of the record's starting value."""

    def _editor(self, m: Machine):
        import tkinter as tk
        from qemugui.mac99_ui_machine import MachineEditor
        lib = model.Library(self.td.name)
        root = tk.Tk(); root.withdraw()
        self.roots.append(root)
        ed = MachineEditor(root, m, lib, "/q", on_save=lambda *a: None)
        ed.withdraw()
        return ed

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.roots = []

    def tearDown(self):
        for r in self.roots:
            r.destroy()
        self.td.cleanup()

    def test_unchecked_boot_into_ofw_means_auto_boot_true(self):
        m = Machine(name="t", prom_env=PromEnv(auto_boot=True))
        ed = self._editor(m)
        self.assertFalse(ed.boot_into_ofw_var.get())
        self.assertTrue(ed.collect().prom_env.auto_boot)

    def test_checked_boot_into_ofw_means_auto_boot_false(self):
        m = Machine(name="t", prom_env=PromEnv(auto_boot=False))
        ed = self._editor(m)
        self.assertTrue(ed.boot_into_ofw_var.get())
        self.assertFalse(ed.collect().prom_env.auto_boot)

    def test_unchecked_no_vga_driver_means_vga_ndrv_true(self):
        m = Machine(name="t", prom_env=PromEnv(vga_ndrv=True))
        ed = self._editor(m)
        self.assertFalse(ed.no_vga_driver_var.get())
        self.assertTrue(ed.collect().prom_env.vga_ndrv)

    def test_checked_no_vga_driver_means_vga_ndrv_false(self):
        m = Machine(name="t", prom_env=PromEnv(vga_ndrv=False))
        ed = self._editor(m)
        self.assertTrue(ed.no_vga_driver_var.get())
        self.assertFalse(ed.collect().prom_env.vga_ndrv)

    def test_toggling_the_boot_into_ofw_box_flips_the_saved_value(self):
        m = Machine(name="t", prom_env=PromEnv(auto_boot=True))
        ed = self._editor(m)
        ed.boot_into_ofw_var.set(True)
        self.assertFalse(ed.collect().prom_env.auto_boot)
        ed.boot_into_ofw_var.set(False)
        self.assertTrue(ed.collect().prom_env.auto_boot)

    def test_turning_the_gpu_on_checks_no_vga_driver(self):
        """"Make it follow the GPU choice sensibly" (user review,
        2026-09-14): the Rage 128 Pro needs OpenBIOS's driver kept out of
        the way, so switching the card on defaults the checkbox to match."""
        m = Machine(name="t", gpu=None, prom_env=PromEnv(vga_ndrv=True))
        ed = self._editor(m)
        self.assertFalse(ed.no_vga_driver_var.get())
        ed.gpu_on.set(True)
        ed._gpu_changed()
        self.assertTrue(ed.no_vga_driver_var.get())
        self.assertFalse(ed.collect().prom_env.vga_ndrv)

    def test_turning_the_gpu_off_unchecks_no_vga_driver(self):
        m = Machine(name="t", gpu=Gpu("card.rom"), prom_env=PromEnv(vga_ndrv=False))
        ed = self._editor(m)
        self.assertTrue(ed.no_vga_driver_var.get())
        ed.gpu_on.set(False)
        ed._gpu_changed()
        self.assertFalse(ed.no_vga_driver_var.get())
        self.assertTrue(ed.collect().prom_env.vga_ndrv)

    def test_toggling_the_gpu_does_not_overrule_a_hand_set_checkbox(self):
        """Only the toggle sets a starting point; a person can still flip
        the checkbox back afterwards and it stays put."""
        m = Machine(name="t", gpu=None, prom_env=PromEnv(vga_ndrv=True))
        ed = self._editor(m)
        ed.gpu_on.set(True)
        ed._gpu_changed()
        ed.no_vga_driver_var.set(False)          # a person overrides it
        self.assertTrue(ed.collect().prom_env.vga_ndrv)


@unittest.skipUnless(_tk_available(), "no display")
class BootCheckbox(unittest.TestCase):
    """The Drives tab's per-row "Boot" checkbox, mutually exclusive across
    the four ATA rows, which sets ``Machine.boot_slot`` (see
    mac99_model.py's module docstring)."""

    def _editor(self, m: Machine):
        import tkinter as tk
        from qemugui.mac99_ui_machine import MachineEditor
        lib = model.Library(self.td.name)
        root = tk.Tk(); root.withdraw()
        self.roots.append(root)
        ed = MachineEditor(root, m, lib, "/q", on_save=lambda *a: None)
        ed.withdraw()
        return ed

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.roots = []

    def tearDown(self):
        for r in self.roots:
            r.destroy()
        self.td.cleanup()

    def test_defaults_to_nothing_checked(self):
        m = Machine(name="t")
        ed = self._editor(m)
        self.assertFalse(any(row.boot.get() for row in ed.ata_rows))
        self.assertIsNone(ed.collect().boot_slot)

    def test_loads_the_marked_slot(self):
        m = Machine(name="t", boot_slot=2,
                    ata=[AtaDrive("disk", "/a.img"), None, AtaDrive("cdrom", "/c.iso"), None])
        ed = self._editor(m)
        self.assertEqual([row.boot.get() for row in ed.ata_rows], [False, False, True, False])
        self.assertEqual(ed.collect().boot_slot, 2)

    def test_checking_one_row_unchecks_the_others(self):
        m = Machine(name="t", ata=[AtaDrive("disk", "/a.img"), AtaDrive("disk", "/b.img"), None, None])
        ed = self._editor(m)
        ed.ata_rows[0].boot.set(True)
        ed.ata_rows[0]._boot_toggled()
        self.assertEqual(ed.collect().boot_slot, 0)
        ed.ata_rows[1].boot.set(True)
        ed.ata_rows[1]._boot_toggled()
        self.assertFalse(ed.ata_rows[0].boot.get())
        self.assertEqual(ed.collect().boot_slot, 1)

    def test_emptying_a_checked_slot_clears_boot(self):
        m = Machine(name="t", boot_slot=0, ata=[AtaDrive("disk", "/a.img"), None, None, None])
        ed = self._editor(m)
        self.assertTrue(ed.ata_rows[0].boot.get())
        ed.ata_rows[0].kind.set("Empty")
        ed.ata_rows[0]._kind_changed()
        self.assertFalse(ed.ata_rows[0].boot.get())
        self.assertIsNone(ed.collect().boot_slot)


@unittest.skipUnless(_tk_available(), "no display")
class NewDiskButton(unittest.TestCase):
    """The Drives tab's "New disk…" button, offered only when qemu-img
    sits next to the program (paths.qemu_img_binary), and its wiring of
    CreateDiskDialog's result back into the editor."""

    def _editor(self, m: Machine):
        import tkinter as tk
        from qemugui.mac99_ui_machine import MachineEditor
        lib = model.Library(self.td.name)
        root = tk.Tk(); root.withdraw()
        self.roots.append(root)
        ed = MachineEditor(root, m, lib, "/q", on_save=lambda *a: None)
        ed.withdraw()
        return ed

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.install_dir = tempfile.TemporaryDirectory()
        self.roots = []

    def tearDown(self):
        for r in self.roots:
            r.destroy()
        self.td.cleanup()
        self.install_dir.cleanup()
        paths.use_install_dir(None)

    def test_absent_without_qemu_img(self):
        paths.use_install_dir(self.install_dir.name)
        ed = self._editor(Machine(name="t"))
        self.assertIsNone(ed.new_disk_button)

    def test_present_with_qemu_img(self):
        (Path(self.install_dir.name) / paths.qemu_img_name()).write_text("")
        paths.use_install_dir(self.install_dir.name)
        ed = self._editor(Machine(name="t"))
        self.assertIsNotNone(ed.new_disk_button)

    def test_places_new_disk_in_the_chosen_ata_slot(self):
        import qemugui.mac99_ui_machine as ui_machine
        ed = self._editor(Machine(name="t"))

        class FakeDialog:
            def __init__(self, *_a, **_k):
                self.result = ("/new/disk.img", "raw", ("ata", 1))

        orig, ui_machine.CreateDiskDialog = ui_machine.CreateDiskDialog, FakeDialog
        try:
            ed._new_disk()
        finally:
            ui_machine.CreateDiskDialog = orig
        self.assertEqual(ed.ata_rows[1].file.get(), "/new/disk.img")
        self.assertEqual(ed.ata_rows[1].kind.get(), "Hard disk")

    def test_places_new_disk_as_usb_storage(self):
        import qemugui.mac99_ui_machine as ui_machine
        ed = self._editor(Machine(name="t"))

        class FakeDialog:
            def __init__(self, *_a, **_k):
                self.result = ("/new/stick.img", "raw", ("usb", None))

        orig, ui_machine.CreateDiskDialog = ui_machine.CreateDiskDialog, FakeDialog
        try:
            ed._new_disk()
        finally:
            ui_machine.CreateDiskDialog = orig
        self.assertEqual(ed.collect().usb_storage, [UsbStorage("/new/stick.img", "raw")])

    def test_cancelled_dialog_changes_nothing(self):
        import qemugui.mac99_ui_machine as ui_machine
        ed = self._editor(Machine(name="t", ata=[AtaDrive("disk", "/a.img"), None, None, None]))

        class FakeDialog:
            def __init__(self, *_a, **_k):
                self.result = None

        orig, ui_machine.CreateDiskDialog = ui_machine.CreateDiskDialog, FakeDialog
        try:
            ed._new_disk()
        finally:
            ui_machine.CreateDiskDialog = orig
        self.assertEqual(ed.ata_rows[0].file.get(), "/a.img")
        self.assertEqual(ed.collect().usb_storage, [])


@unittest.skipUnless(_tk_available(), "no display")
class ShareTab(unittest.TestCase):
    """The Shared folder tab: load/collect round trip for Machine.share."""

    def _editor(self, m: Machine):
        import tkinter as tk
        from qemugui.mac99_ui_machine import MachineEditor
        lib = model.Library(self.td.name)
        root = tk.Tk(); root.withdraw()
        self.roots.append(root)
        ed = MachineEditor(root, m, lib, "/q", on_save=lambda *a: None)
        ed.withdraw()
        return ed

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.roots = []

    def tearDown(self):
        for r in self.roots:
            r.destroy()
        self.td.cleanup()

    def test_defaults_to_no_shared_folder(self):
        from qemugui.mac99_model import Share
        ed = self._editor(Machine(name="t"))
        self.assertEqual(ed.share_folder_var.get(), "")
        self.assertEqual(ed.share_scope.get(), "guest-only")
        self.assertEqual(ed.collect().share, Share())

    def test_loads_and_collects_a_share(self):
        from qemugui.mac99_model import Share
        m = Machine(name="t", share=Share("/shared", "mac", "secret", "all-interfaces"))
        ed = self._editor(m)
        self.assertEqual(ed.share_folder_var.get(), "/shared")
        self.assertEqual(ed.share_user_var.get(), "mac")
        self.assertEqual(ed.share_password_var.get(), "secret")
        self.assertEqual(ed.share_scope.get(), "all-interfaces")
        self.assertEqual(ed.collect().share, m.share)


@unittest.skipUnless(_tk_available(), "no display")
class NoSystemChooserOnScreen(unittest.TestCase):
    """New machine asks for a name only -- there is no system-type chooser
    to find or to leave blank (user review, 2026-09-14)."""

    def test_the_machine_tab_has_no_system_widget(self):
        import tkinter as tk
        from qemugui.mac99_ui_machine import MachineEditor
        with tempfile.TemporaryDirectory() as td:
            lib = model.Library(td)
            m = model.new_machine("t")
            root = tk.Tk(); root.withdraw()
            try:
                ed = MachineEditor(root, m, lib, "/q", on_save=lambda *a: None)
                ed.withdraw()
                self.assertFalse(hasattr(ed, "system_var"))
            finally:
                root.destroy()


class _FakeRun:
    exit_code = None
    share = None

    def poll(self):
        return None

    def uptime(self):
        return 0.0


@unittest.skipUnless(_tk_available(), "no display")
class StartDispatch(unittest.TestCase):
    """On macOS every start goes through Terminal, sudo or not; on any other
    host the GUI runs the launcher itself and tracks the process."""

    def setUp(self):
        from qemugui import mac99_ui_main as ui
        self.ui = ui
        self.td = tempfile.TemporaryDirectory()
        paths.use_install_dir(self.td.name)
        self.saved = (paths.HOST_PLATFORM, ui.start_in_terminal, ui.start_machine)
        self.calls: list[str] = []
        ui.start_in_terminal = lambda m, d: (self.calls.append("terminal"), (Path(d) / "run.command", None))[1]
        ui.start_machine = lambda m, d: (self.calls.append("direct"), _FakeRun())[1]
        self.roots = []

    def tearDown(self):
        paths.HOST_PLATFORM, self.ui.start_in_terminal, self.ui.start_machine = self.saved
        for r in self.roots:
            r.destroy()
        paths.use_install_dir(None)
        self.td.cleanup()

    def _window(self, platform: str):
        paths.HOST_PLATFORM = platform
        w = self.ui.MainWindow(paths.Settings(), settings_path=Path(self.td.name) / "settings.json")
        w.withdraw()
        self.roots.append(w)
        w.library.save(model.new_machine("t"))
        w.refresh_list(select="t")
        return w

    def test_macos_starts_in_terminal_without_sudo(self):
        w = self._window("darwin")
        self.assertIsNone(w.start_selected())
        self.assertEqual(self.calls, ["terminal"])
        self.assertNotIn("t", w.running)
        self.assertIn("t", w.terminal_started)
        self.assertTrue(w.run_status.cget("text").startswith(self.ui.TERMINAL_STATUS))

    def test_other_hosts_run_the_launcher_directly(self):
        w = self._window("linux")
        self.assertIsNotNone(w.start_selected())
        self.assertEqual(self.calls, ["direct"])
        self.assertIn("t", w.running)
        self.assertNotIn("t", w.terminal_started)
        self.assertTrue(w.run_status.cget("text").startswith("Running"))


if __name__ == "__main__":
    unittest.main()
