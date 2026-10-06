"""Host forward ports below 1024 start the machine with sudo."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qemugui import mac99_command as command  # noqa: E402
from qemugui.mac99_model import Machine  # noqa: E402
from qemugui.mac99_model import HostFwd  # noqa: E402

QD = "/Applications/q"
MD = "/m"


def machine(*ports: str, mode: str = "user") -> Machine:
    m = Machine(name="L")
    m.network.mode = mode
    m.network.hostfwd = [HostFwd("tcp", p, "80" if p else "") for p in ports]
    return m


class LowPortSudo(unittest.TestCase):
    def test_low_port_needs_sudo(self):
        self.assertTrue(command.needs_sudo(machine("80"), "darwin"))
        self.assertTrue(command.needs_sudo(machine("8080", "22"), "linux"))

    def test_high_port_does_not(self):
        self.assertFalse(command.needs_sudo(machine("1024"), "darwin"))
        self.assertFalse(command.needs_sudo(machine("8080"), "darwin"))

    def test_empty_rows_ignored(self):
        self.assertFalse(command.needs_sudo(machine("", "8080"), "darwin"))

    def test_only_with_slirp(self):
        self.assertFalse(command.needs_sudo(machine("80", mode="none"), "darwin"))

    def test_windows_never(self):
        self.assertFalse(command.needs_sudo(machine("80"), "win32"))

    def test_launcher_has_sudo_prefix(self):
        lines = command.launcher_text(machine("80"), QD, MD, "darwin").splitlines()
        self.assertIn("sudo -v", lines)
        self.assertTrue(any(ln.startswith("sudo /Applications/q/") for ln in lines))
        text = command.launcher_text(machine("8080"), QD, MD, "darwin")
        self.assertNotIn("sudo", text)
