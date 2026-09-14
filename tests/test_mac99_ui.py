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
from qemugui.mac99_model import Machine, PromEnv, Gpu  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
