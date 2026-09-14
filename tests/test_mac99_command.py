"""Golden-output tests for qemugui.mac99_command / qemugui.mac99_model
(headless, no Tk).

Run:  python -m unittest discover -s tests
"""

from __future__ import annotations

import json
import re
import shlex
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from qemugui import mac99_command as command  # noqa: E402
from qemugui import mac99_model as model  # noqa: E402
from qemugui import paths, mac99_systems as systems  # noqa: E402
from qemugui.mac99_model import Machine, AtaDrive, UsbStorage, Gpu, Network, PromEnv  # noqa: E402

FIXTURES = HERE / "fixtures"

FIXTURE_QEMU_DIR = {
    "mac99-osx.json": "/Applications/qemu-system-ppc-smp-usb-rage-openbios-based",
    "mac99-os9.json": "/Applications/qemu-system-ppc-smp-usb-rage-openbios-based",
}

# The user's reference launch line, verbatim (given 2026-09-14). Two
# deliberate divergences from it, both explained where the comparison
# accounts for them below:
#  * this GUI does not emit adb-mouse.extended-protocol under via=pmu --
#    mac_newworld.c creates no ADB device in that mode, so the property has
#    nothing to attach to (has_adb() is False for "pmu").
#  * this GUI always wires a persistent nvram.img (macio-nvram.drive=nvr +
#    the paired -drive), which the reference line does not do -- mac99's
#    NVRAM is otherwise volatile (see mac99_model.py's module docstring).
USER_MAC99_OSX = r"""
./qemu-system-ppc \
-L ./pc-bios \
-M mac99,via=pmu \
-smp 4 \
-display sdl \
-m 1024 \
-boot c \
-vga none \
-global adb-mouse.extended-protocol=on \
-audiodev coreaudio,id=snd -global screamer.audiodev=snd \
-device ati-rage128-pro,romfile=./ati_rage128pro_136_agp.rom \
-drive file=/Volumes/Macdata/qemu/hd/10.4.img,format=raw,media=disk,index=0 \
-drive file=/Users/hsp/Downloads/Chessmaster9000.iso,format=raw,media=cdrom,index=2 \
-prom-env 'auto-boot?=true' \
-prom-env 'vga-ndrv?=false'
"""

INERT_UNDER_PMU = "adb-mouse.extended-protocol=on"


def user_tokens(text: str, qemu_dir: str) -> set[str]:
    toks = shlex.split(text.replace("\\\n", " "))
    assert toks[0] == "./qemu-system-ppc"
    out = []
    for i, t in enumerate(toks[1:], 1):
        prev = toks[i - 1]
        if prev in ("-L", "-bios") and not t.startswith("/"):
            t = f"{qemu_dir}/{t.lstrip('./')}"
        t = re.sub(r"romfile=(?!/)\.?/?([^,]+)", lambda mm: f"romfile={qemu_dir}/{mm.group(1)}", t)
        out.append(t)
    # drop the global that has nothing to attach to under via=pmu
    filtered = []
    skip_next = False
    for t in out:
        if skip_next:
            skip_next = False
            continue
        if t == INERT_UNDER_PMU:
            filtered.pop()  # drop the preceding "-global" too
            continue
        filtered.append(t)
    return set(filtered)


def load_fixture(name: str) -> Machine:
    return Machine.load(FIXTURES / name)


def gen_tokens(m: Machine, qemu_dir: str, platform: str = "darwin") -> set[str]:
    argv = command.build_argv(m, qemu_dir, str(FIXTURES), platform)
    toks = set(argv[1:])
    # the reference line has no persisted NVRAM; strip our addition for the
    # comparison, and check it separately (test_nvram_is_wired below).
    toks -= {"-drive", f"if=none,id=nvr,file={FIXTURES}/nvram.img,format=raw",
            "-global", "macio-nvram.drive=nvr"}
    return toks


class UserLauncher(unittest.TestCase):
    """The primary correctness test: the user's real mac99 OS X launch
    line, minus the two documented, deliberate divergences."""

    def test_mac99_osx(self):
        qd = FIXTURE_QEMU_DIR["mac99-osx.json"]
        got = gen_tokens(load_fixture("mac99-osx.json"), qd)
        want = user_tokens(USER_MAC99_OSX, qd)
        self.assertEqual(got, want)

    def test_binary_is_absolute_and_first(self):
        m = load_fixture("mac99-osx.json")
        argv = command.build_argv(m, "/Applications/qemu-system-ppc-smp-usb-rage-openbios-based",
                                  str(FIXTURES), "darwin")
        self.assertEqual(argv[0],
                         "/Applications/qemu-system-ppc-smp-usb-rage-openbios-based/qemu-system-ppc")

    def test_shell_rendering_shape(self):
        m = load_fixture("mac99-osx.json")
        text = command.launcher_text(m, "/q", str(FIXTURES), "darwin")
        lines = text.splitlines()
        self.assertEqual(lines[0], "#!/bin/bash")
        self.assertIn('cd "$(dirname "$0")"', lines)
        body = [ln for ln in lines if ln.startswith("-") or ln.startswith("/")]
        for ln in body[:-1]:
            self.assertTrue(ln.endswith(" \\"), ln)
        self.assertFalse(body[-1].endswith("\\"))
        self.assertIn("-M mac99,via=pmu \\", lines)
        self.assertIn("-nic user,model=sungem,mac=00:05:02:12:34:56 \\", lines)


class Options(unittest.TestCase):

    def base(self) -> Machine:
        return load_fixture("mac99-osx.json")

    def test_via_smp_gate(self):
        m = self.base()
        m.via = "cuda"
        m.smp = 1
        errors, _ = model.validate(m, None, "darwin", check_files=False)
        self.assertEqual(errors, [])
        m.smp = 2
        errors, _ = model.validate(m, None, "darwin", check_files=False)
        self.assertTrue(any("via pmu or pmu-adb" in e for e in errors))

    def test_os9_multi_cpu_warns(self):
        m = load_fixture("mac99-os9.json")
        m.smp = 2
        errors, warnings = model.validate(m, None, "darwin", check_files=False)
        self.assertEqual(errors, [])
        self.assertTrue(any("keyboard control" in w for w in warnings))

    def test_adb_mouse_global_gated_on_via(self):
        m = self.base()
        m.via = "pmu"
        argv = command.build_argv(m, "/q", "/m", "darwin")
        self.assertFalse(any("adb-mouse" in t for t in argv))
        m.via = "cuda"
        argv = command.build_argv(m, "/q", "/m", "darwin")
        self.assertIn("adb-mouse.extended-protocol=on", argv)
        m.via = "pmu-adb"
        argv = command.build_argv(m, "/q", "/m", "darwin")
        self.assertIn("adb-mouse.extended-protocol=on", argv)

    def test_no_gpu_means_no_vga_none_and_no_device(self):
        m = self.base()
        m.gpu = None
        argv = command.build_argv(m, "/q", "/m", "darwin")
        self.assertNotIn("none", [argv[i + 1] for i, t in enumerate(argv) if t == "-vga"])
        self.assertFalse(any("ati-rage128-pro" in t for t in argv))

    def test_gpu_without_romfile(self):
        m = self.base()
        m.gpu = Gpu(romfile=None)
        argv = command.build_argv(m, "/q", "/m", "darwin")
        self.assertIn("-vga", argv)
        self.assertEqual(argv[argv.index("-vga") + 1], "none")
        self.assertIn("ati-rage128-pro", argv)
        self.assertFalse(any("romfile" in t for t in argv))

    def test_nic_model_is_sungem(self):
        m = self.base()
        argv = command.build_argv(m, "/q", "/m", "darwin")
        self.assertEqual(argv[argv.index("-nic") + 1],
                         "user,model=sungem,mac=00:05:02:12:34:56")

    def test_nic_none(self):
        m = self.base()
        m.network = Network(mode="none")
        argv = command.build_argv(m, "/q", "/m", "darwin")
        self.assertEqual(argv[argv.index("-nic") + 1], "none")

    def test_no_scsi_no_floppy_options_exist(self):
        """There is nothing in this module that could emit either: the
        machine has neither (see the module docstring's ground truth)."""
        src = (HERE.parent / "qemugui" / "mac99_command.py").read_text()
        src_model = (HERE.parent / "qemugui" / "mac99_model.py").read_text()
        for gone in ("scsi-hd", "scsi-cd", "swim3", "SCSI_IDS", "Floppy"):
            self.assertNotIn(gone, src)
            self.assertNotIn(gone, src_model)

    def test_ata_index_explicit_for_every_slot(self):
        m = self.base()
        m.ata = [AtaDrive("disk", "/a.img"), AtaDrive("disk", "/b.img"),
                AtaDrive("cdrom", "/c.iso"), AtaDrive("cdrom", "/d.iso")]
        argv = command.build_argv(m, "", "/m", "darwin")
        drives = [t for t in argv if t.startswith("file=") and "nvram" not in t]
        self.assertEqual(drives, ["file=/a.img,format=raw,media=disk,index=0",
                                  "file=/b.img,format=raw,media=disk,index=1",
                                  "file=/c.iso,format=raw,media=cdrom,index=2",
                                  "file=/d.iso,format=raw,media=cdrom,index=3"])

    def test_usb_storage(self):
        m = self.base()
        m.usb_storage = [UsbStorage("/mem1.img", "raw"), UsbStorage("/mem2.img", "raw")]
        argv = command.build_argv(m, "", "/m", "darwin")
        self.assertIn("file=/mem1.img,format=raw,if=none,id=usbs0", argv)
        self.assertIn("usb-storage,drive=usbs0", argv)
        self.assertIn("file=/mem2.img,format=raw,if=none,id=usbs1", argv)
        self.assertIn("usb-storage,drive=usbs1", argv)

    def test_nvram_is_wired(self):
        m = self.base()
        argv = command.build_argv(m, "", "/machine-dir", "darwin")
        self.assertIn("if=none,id=nvr,file=/machine-dir/nvram.img,format=raw", argv)
        self.assertIn("macio-nvram.drive=nvr", argv)

    def test_prom_env_defaults(self):
        m = self.base()
        argv = command.build_argv(m, "", "/m", "darwin")
        self.assertIn("auto-boot?=true", argv)
        self.assertIn("vga-ndrv?=false", argv)
        self.assertFalse(any(t.startswith("boot-device=") for t in argv))

    def test_prom_env_boot_device_and_args(self):
        m = self.base()
        m.prom_env = PromEnv(auto_boot=False, vga_ndrv=True, boot_device="cd:,\\:tbxi",
                             boot_args="-v")
        argv = command.build_argv(m, "", "/m", "darwin")
        self.assertIn("auto-boot?=false", argv)
        self.assertIn("vga-ndrv?=true", argv)
        self.assertIn("boot-device=cd:,\\:tbxi", argv)
        self.assertIn("boot-args=-v", argv)

    def test_audio_none(self):
        m = self.base()
        m.audio = "none"
        argv = command.build_argv(m, "", "/m", "darwin")
        self.assertIn("none,id=snd", argv)
        self.assertIn("screamer.audiodev=snd", argv)

    def test_pc_bios_path_is_relative_to_qemu_dir(self):
        m = self.base()
        argv = command.build_argv(m, "/install", "/m", "darwin")
        self.assertEqual(argv[argv.index("-L") + 1], "/install/pc-bios")

    def test_firmware_override(self):
        m = self.base()
        m.firmware = "custom-openbios.bin"
        argv = command.build_argv(m, "/install", "/m", "darwin")
        self.assertIn("-bios", argv)
        self.assertEqual(argv[argv.index("-bios") + 1], "/install/custom-openbios.bin")

    def test_no_firmware_override_means_no_bios_flag(self):
        m = self.base()
        m.firmware = ""
        argv = command.build_argv(m, "/install", "/m", "darwin")
        self.assertNotIn("-bios", argv)

    def test_comma_in_path_is_escaped_for_qemu(self):
        m = self.base()
        m.ata[0] = AtaDrive("disk", "/Volumes/x/a,b.img")
        argv = command.build_argv(m, "", "/m", "darwin")
        self.assertIn("file=/Volumes/x/a,,b.img,format=raw,media=disk,index=0", argv)

    def test_extra_args_appended_verbatim(self):
        m = self.base()
        m.extra_args = "-qmp unix:/tmp/live.sock,server=on,wait=off"
        argv = command.build_argv(m, "", "/m", "darwin")
        self.assertEqual(argv[-2:], ["-qmp", "unix:/tmp/live.sock,server=on,wait=off"])


class WindowsRendering(unittest.TestCase):

    def test_bat_shape(self):
        m = load_fixture("mac99-osx.json")
        argv = command.build_argv(m, r"C:\mac99", r"C:\Machines\OSX", "win32")
        text = command.render_bat(argv)
        self.assertEqual(argv[0], r"C:\mac99\qemu-system-ppc.exe")
        self.assertIn('cd /d "%~dp0"', text)
        self.assertIn("@echo off", text)
        self.assertIn(" ^\r\n", text)
        self.assertNotIn(" \\\r\n", text)
        body = [ln for ln in text.split("\r\n") if ln.startswith("-")]
        self.assertFalse(body[-1].endswith("^"))


class Networking(unittest.TestCase):

    def nic(self, mode, ifname="", platform="darwin"):
        m = load_fixture("mac99-osx.json")
        m.network = Network(mode, "00:05:02:12:34:56", ifname)
        argv = command.build_argv(m, "/q", "/m", platform)
        return argv[argv.index("-nic") + 1]

    def test_vmnet_bridged(self):
        self.assertEqual(self.nic("vmnet-bridged", "en0"),
                         "vmnet-bridged,ifname=en0,model=sungem,mac=00:05:02:12:34:56")

    def test_vmnet_shared(self):
        self.assertEqual(self.nic("vmnet-shared"), "vmnet-shared,model=sungem,mac=00:05:02:12:34:56")

    def test_tap(self):
        self.assertEqual(self.nic("tap", "TAP-Windows Adapter V9", "win32"),
                         "tap,ifname=TAP-Windows Adapter V9,model=sungem,mac=00:05:02:12:34:56")

    def test_vmnet_command_has_sudo_prefix_and_chown_tail(self):
        m = load_fixture("mac99-osx.json")
        m.network = Network("vmnet-bridged", "00:05:02:12:34:56", "en0")
        text = command.launcher_text(m, "/Applications/qemu-system-ppc-smp-usb-rage-openbios-based",
                                     "/m", "darwin")
        lines = text.splitlines()
        self.assertTrue(any(ln.startswith("sudo /Applications/") for ln in lines))
        self.assertTrue(lines[-1].startswith("sudo -n chown "), lines[-1])
        self.assertIn("nvram.img", lines[-1])
        self.assertNotIn("pram.img", lines[-1])

    def test_bat_never_has_sudo(self):
        m = load_fixture("mac99-osx.json")
        m.network = Network("tap", "00:05:02:12:34:56", "TAP-Windows Adapter V9")
        text = command.launcher_text(m, r"C:\q", r"C:\m", "win32")
        self.assertNotIn("sudo", text)


class JsonRoundTrip(unittest.TestCase):

    def test_fixtures_round_trip(self):
        for f in sorted(FIXTURES.glob("mac99-*.json")):
            m = Machine.load(f)
            again = Machine.from_json(m.to_json())
            self.assertEqual(m, again, f.name)
            self.assertEqual(json.loads(m.to_json())["schema"], model.SCHEMA)

    def test_full_record_round_trip(self):
        m = Machine(name="Every field", system="macosx", via="cuda", ram_mb=1536, smp=1,
                    firmware="custom.bin", display="cocoa", audio="none",
                    gpu=Gpu("card.rom"),
                    network=Network("user", "00:11:22:33:44:55"),
                    ata=[AtaDrive("disk", "/a.img", "qcow2"), None, AtaDrive("cdrom", "/c.iso"), None],
                    usb_storage=[UsbStorage("/mem.img", "raw")],
                    prom_env=PromEnv(False, True, "cd:,\\:tbxi", "-v"),
                    extra_args="-qmp none", notes="n\u00f6tes")
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "machine.json"
            m.save(p)
            self.assertEqual(Machine.load(p), m)


class NothingIsChosenForYou(unittest.TestCase):

    def test_a_new_machine_has_every_file_field_empty(self):
        for system_id in systems.system_ids():
            m = model.new_machine("Fresh", system_id)
            for field, value in model.file_fields(m).items():
                self.assertEqual(value, "", f"{system_id}: {field} was filled in")
            self.assertIsNone(m.gpu)
            self.assertEqual(m.notes, "")

    def test_a_new_machine_needs_nothing_before_it_can_start(self):
        """Unlike g3beige, mac99's firmware is bundled with the
        distribution, not an Apple ROM the person must supply."""
        m = model.new_machine("Fresh", "macos9")
        self.assertEqual(model.start_blockers(m), [])

    def test_system_seeds_smp_and_via(self):
        m9 = model.new_machine("nine", "macos9")
        self.assertEqual((m9.smp, m9.via), (1, "pmu"))
        mx = model.new_machine("ten-four", "macosx")
        self.assertEqual((mx.smp, mx.via), (4, "pmu"))


class LibraryOps(unittest.TestCase):

    def test_create_save_duplicate_delete(self):
        with tempfile.TemporaryDirectory() as td:
            lib = model.Library(td)
            m = model.new_machine("Mac OS X", "macosx")
            lib.save(m)
            (lib.folder("Mac OS X") / "nvram.img").write_bytes(b"\0" * 8192)
            self.assertEqual(lib.names(), ["Mac OS X"])
            self.assertEqual(lib.saved_settings_status("Mac OS X"), {"nvram.img": 8192})
            d = lib.duplicate("Mac OS X", "Mac OS X copy")
            self.assertTrue((lib.folder("Mac OS X copy") / "nvram.img").is_file())
            self.assertEqual(lib.clear_saved_settings("Mac OS X"), ["nvram.img"])
            lib.delete("Mac OS X copy")
            self.assertEqual(lib.names(), ["Mac OS X"])

    def test_write_launcher_is_executable_and_regenerated(self):
        with tempfile.TemporaryDirectory() as td:
            m = load_fixture("mac99-osx.json")
            path, argv = command.write_launcher(m, "/q", td, "darwin")
            self.assertEqual(path.name, "run.command")
            self.assertTrue(path.stat().st_mode & 0o111)
            self.assertIn("Do not edit", path.read_text())
            m.ram_mb = 2048
            path2, _ = command.write_launcher(m, "/q", td, "darwin")
            self.assertIn("-m 2048", path2.read_text())


if __name__ == "__main__":
    unittest.main()
