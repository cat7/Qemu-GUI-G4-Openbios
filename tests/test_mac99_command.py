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
from qemugui import paths  # noqa: E402
from qemugui.mac99_model import Machine, AtaDrive, UsbStorage, Gpu, Network, PromEnv  # noqa: E402

FIXTURES = HERE / "fixtures"

FIXTURE_QEMU_DIR = {
    "mac99-osx.json": "/Applications/qemu-system-ppc-smp-usb-rage-openbios-based",
    "mac99-os9.json": "/Applications/qemu-system-ppc-smp-usb-rage-openbios-based",
}

# The user's reference launch line, verbatim (given 2026-09-14). Three
# deliberate divergences from it, all explained where the comparison
# accounts for them below:
#  * this GUI does not emit adb-mouse.extended-protocol under via=pmu --
#    mac_newworld.c creates no ADB device in that mode, so the property has
#    nothing to attach to (has_adb() is False for "pmu").
#  * this GUI always wires a persistent nvram.img (macio-nvram.drive=nvr +
#    the paired -drive), which the reference line does not do -- mac99's
#    NVRAM is otherwise volatile (see mac99_model.py's module docstring).
#  * the reference line carries no explicit -nic at all, relying on QEMU's
#    own default NIC (mc->default_nic = "sungem", added automatically
#    because neither -nic nor -net none was given); this GUI is explicit
#    about it, the way the g3beige GUI is explicit about model=bmac.
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
-drive file=/images/10.4.img,format=raw,media=disk,index=0 \
-drive file=/images/Chessmaster9000.iso,format=raw,media=cdrom,index=2 \
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
    # the reference line has no explicit -nic; this GUI always emits one
    filtered += ["-nic", "user,model=sungem,mac=00:05:02:12:34:56"]
    return set(filtered)


def load_fixture(name: str) -> Machine:
    return Machine.load(FIXTURES / name)


def gen_tokens(m: Machine, qemu_dir: str, platform: str = "darwin") -> set[str]:
    argv = command.build_argv(m, qemu_dir, str(FIXTURES), platform)
    rest = argv[1:]
    # the reference line has no persisted NVRAM; drop our addition (as
    # adjacent flag/value pairs, not from the token set -- "-drive" and
    # "-global" are shared with other options) and check it separately
    # (test_nvram_is_wired below).
    nvram_pairs = [("-drive", f"if=none,id=nvr,file={FIXTURES}/nvram.img,format=raw"),
                  ("-global", "macio-nvram.drive=nvr")]
    out = []
    i = 0
    while i < len(rest):
        pair = (rest[i], rest[i + 1]) if i + 1 < len(rest) else None
        if pair in nvram_pairs:
            i += 2
            continue
        out.append(rest[i])
        i += 1
    return set(out)


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

    def test_no_boot_slot_marked_emits_boot_c(self):
        m = self.base()
        self.assertIsNone(m.boot_slot)
        argv = command.build_argv(m, "", "/m", "darwin")
        self.assertEqual(argv[argv.index("-boot") + 1], "c")

    def test_boot_slot_on_a_cd_emits_boot_d(self):
        m = self.base()
        m.boot_slot = 2  # the fixture's cdrom
        argv = command.build_argv(m, "", "/m", "darwin")
        self.assertEqual(argv[argv.index("-boot") + 1], "d")

    def test_boot_slot_on_a_disk_emits_boot_c(self):
        m = self.base()
        m.boot_slot = 0  # the fixture's disk
        argv = command.build_argv(m, "", "/m", "darwin")
        self.assertEqual(argv[argv.index("-boot") + 1], "c")

    def test_boot_slot_out_of_range_falls_back_to_c(self):
        m = self.base()
        m.boot_slot = 9
        argv = command.build_argv(m, "", "/m", "darwin")
        self.assertEqual(argv[argv.index("-boot") + 1], "c")

    def test_boot_slot_on_an_empty_slot_warns(self):
        m = self.base()
        m.boot_slot = 1  # empty in the fixture
        _errors, warnings = model.validate(m, None, "darwin", check_files=False)
        self.assertTrue(any("is marked Boot but is empty" in w for w in warnings))

    def test_boot_slot_not_the_lowest_of_its_kind_warns(self):
        m = self.base()
        m.ata = [AtaDrive("disk", "/a.img"), AtaDrive("disk", "/b.img"), None, None]
        m.boot_slot = 1
        _errors, warnings = model.validate(m, None, "darwin", check_files=False)
        self.assertTrue(any("is marked Boot, but IDE 0 Master" in w for w in warnings))

    def test_boot_slot_already_the_lowest_of_its_kind_is_quiet(self):
        m = self.base()
        m.ata = [AtaDrive("disk", "/a.img"), AtaDrive("disk", "/b.img"), None, None]
        m.boot_slot = 0
        _errors, warnings = model.validate(m, None, "darwin", check_files=False)
        self.assertFalse(any("marked Boot" in w for w in warnings))

    def test_boot_slot_out_of_range_is_an_error(self):
        m = self.base()
        m.boot_slot = 9
        errors, _warnings = model.validate(m, None, "darwin", check_files=False)
        self.assertTrue(any("not a real drive position" in e for e in errors))

    def test_usb_storage(self):
        """Candidate only: not verified on any guest here, and there is no
        editor UI to add one (user review, 2026-09-14). Kept and tested at
        the record/command layer so a machine.json edited by hand still
        works, and so this is ready the day someone runs that test."""
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

    def test_there_is_no_firmware_override(self):
        """-L ./pc-bios is fixed by the distribution layout; there is
        nothing on this machine that needs a different OpenBIOS binary, so
        the option was removed rather than shipped unused (user review,
        2026-09-14)."""
        self.assertFalse(hasattr(Machine(), "firmware"))
        src = (HERE.parent / "qemugui" / "mac99_command.py").read_text()
        self.assertNotIn('"-bios"', src)          # "pc-bios" itself stays

    def test_comma_in_path_is_escaped_for_qemu(self):
        m = self.base()
        m.ata[0] = AtaDrive("disk", "/Volumes/x/a,b.img")
        argv = command.build_argv(m, "", "/m", "darwin")
        self.assertIn("file=/Volumes/x/a,,b.img,format=raw,media=disk,index=0", argv)

    def test_vnc_replaces_local_display(self):
        """Confirmed working end-to-end against this machine type (5900+N
        reachable) with exactly this combination: -display none, -vnc."""
        m = self.base()
        m.vnc = ":1"
        argv = command.build_argv(m, "", "/m", "darwin")
        self.assertEqual(argv[argv.index("-display") + 1], "none")
        self.assertEqual(argv[argv.index("-vnc") + 1], ":1")

    def test_no_vnc_means_normal_display_and_no_vnc_flag(self):
        m = self.base()
        m.vnc = ""
        argv = command.build_argv(m, "", "/m", "darwin")
        self.assertNotIn("-vnc", argv)
        self.assertNotEqual(argv[argv.index("-display") + 1], "none")

    def test_vnc_validation(self):
        m = self.base()
        m.vnc = "not a display"
        errors, _ = model.validate(m, None, "darwin", check_files=False)
        self.assertTrue(any("VNC display" in e for e in errors))
        m.vnc = ":1"
        errors, _ = model.validate(m, None, "darwin", check_files=False)
        self.assertEqual(errors, [])
        m.vnc = "127.0.0.1:9"
        errors, _ = model.validate(m, None, "darwin", check_files=False)
        self.assertEqual(errors, [])

    def test_extra_args_appended_verbatim(self):
        m = self.base()
        m.extra_args = "-qmp unix:/tmp/live.sock,server=on,wait=off"
        argv = command.build_argv(m, "", "/m", "darwin")
        self.assertEqual(argv[-2:], ["-qmp", "unix:/tmp/live.sock,server=on,wait=off"])


class ExtraArgsQuoting(unittest.TestCase):
    """The field is parsed like a shell and every token re-quoted for the
    launcher, so spaces, single quotes and '?' survive verbatim; the argv
    the GUI spawns directly is the same list on every host."""

    TEXT = "-prom-env 'boot-args=-v -x' -name \"it's\" -x 'a?b'"
    WANT = ["-prom-env", "boot-args=-v -x", "-name", "it's", "-x", "a?b"]

    def machine(self) -> Machine:
        m = load_fixture("mac99-osx.json")
        m.extra_args = self.TEXT
        return m

    def test_argv_is_the_same_on_every_host(self):
        for platform in ("darwin", "win32", "linux"):
            argv = command.build_argv(self.machine(), "", "/m", platform)
            self.assertEqual(argv[-6:], self.WANT, platform)

    def test_stored_string_is_verbatim(self):
        m = self.machine()
        self.assertEqual(Machine.from_json(m.to_json()).extra_args, self.TEXT)

    def test_bat_requotes_each_token(self):
        argv = command.build_argv(self.machine(), r"C:\q", r"C:\m", "win32")
        lines = command.render_bat(argv).split("\r\n")
        self.assertIn('-prom-env "boot-args=-v -x" ^', lines)
        self.assertIn("-name it's ^", lines)
        self.assertIn("-x a?b", lines)

    @unittest.skipIf(paths.is_windows(), "needs bash")
    def test_run_command_reproduces_the_argv_when_executed(self):
        import subprocess
        with tempfile.TemporaryDirectory() as td:
            qd = Path(td) / "q"
            qd.mkdir()
            fake = qd / paths.qemu_binary_name("darwin")
            fake.write_text('#!/bin/bash\nfor a in "$@"; do printf "%s\\n" "$a"; done\n')
            fake.chmod(0o755)
            path, argv = command.write_launcher(self.machine(), str(qd), td, "darwin")
            self.assertIn("-prom-env 'boot-args=-v -x' \\\n", path.read_text())
            got = subprocess.run([str(path)], capture_output=True, text=True, check=True)
            self.assertEqual(got.stdout.splitlines(), argv[1:])
            self.assertEqual(got.stdout.splitlines()[-6:], self.WANT)


USB_AUDIO = ["-device", "usb-audio,audiodev=snd"]


def pairs(argv: list[str]) -> list[list[str]]:
    return command.group_options(argv)


class UsbAudio(unittest.TestCase):

    def base(self) -> Machine:
        m = load_fixture("mac99-osx.json")
        m.extra_args = ""
        return m

    def test_off_by_default_and_absent(self):
        m = self.base()
        self.assertFalse(m.usb_audio)
        self.assertFalse(Machine().usb_audio)
        self.assertNotIn(USB_AUDIO, pairs(command.build_argv(m, "", "/m", "darwin")))

    def test_on_appends_after_the_audiodev(self):
        m = self.base()
        m.usb_audio = True
        for platform in ("darwin", "win32"):
            p = pairs(command.build_argv(m, "", "/m", platform))
            self.assertIn(USB_AUDIO, p)
            self.assertEqual(p[p.index(USB_AUDIO) - 1], ["-global", "screamer.audiodev=snd"])

    def test_not_doubled_when_extra_args_already_has_one(self):
        m = self.base()
        m.usb_audio = True
        m.extra_args = "-device usb-audio,audiodev=snd"
        argv = command.build_argv(m, "", "/m", "darwin")
        self.assertEqual(pairs(argv).count(USB_AUDIO), 1)
        self.assertEqual(argv[-2:], USB_AUDIO)
        m.extra_args = "-device usb-audio"
        argv = command.build_argv(m, "", "/m", "darwin")
        self.assertEqual(sum(1 for t in argv if t.startswith("usb-audio")), 1)

    def test_json_round_trip_and_missing_key(self):
        m = self.base()
        m.usb_audio = True
        again = Machine.from_json(m.to_json())
        self.assertTrue(again.usb_audio)
        self.assertEqual(again, m)
        d = json.loads(m.to_json())
        del d["usb_audio"]
        self.assertFalse(Machine.from_dict(d).usb_audio)

    def test_both_launchers(self):
        m = self.base()
        m.usb_audio = True
        mac = command.launcher_text(m, "/q", "/m", "darwin")
        self.assertIn("-device usb-audio,audiodev=snd \\\n", mac)
        bat = command.launcher_text(m, r"C:\q", r"C:\m", "win32")
        self.assertIn('-device "usb-audio,audiodev=snd" ^\r\n', bat)
        m.usb_audio = False
        self.assertNotIn("usb-audio", command.launcher_text(m, "/q", "/m", "darwin"))
        self.assertNotIn("usb-audio", command.launcher_text(m, r"C:\q", r"C:\m", "win32"))


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
        m = Machine(name="Every field", via="cuda", ram_mb=1536, smp=1,
                    display="cocoa", vnc=":2", audio="none",
                    gpu=Gpu("card.rom"),
                    network=Network("user", "00:11:22:33:44:55"),
                    boot_slot=2,
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
        m = model.new_machine("Fresh")
        for field, value in model.file_fields(m).items():
            self.assertEqual(value, "", f"{field} was filled in")
        self.assertIsNone(m.gpu)
        self.assertEqual(m.notes, "")

    def test_a_new_machine_needs_nothing_before_it_can_start(self):
        """Unlike g3beige, mac99's firmware is bundled with the
        distribution, not an Apple ROM the person must supply."""
        m = model.new_machine("Fresh")
        self.assertEqual(model.start_blockers(m), [])

    def test_a_new_machine_defaults_to_one_cpu_and_via_pmu(self):
        """There is no system-type selection any more (no governor on this
        machine, nothing to select): every new machine starts the same way,
        and whoever wants more CPUs turns it up themselves (user review,
        2026-09-14)."""
        m = model.new_machine("t")
        self.assertEqual((m.smp, m.via, m.ram_mb, m.boot_slot), (1, "pmu", 512, None))

    def test_there_is_no_system_type_left(self):
        self.assertFalse(hasattr(Machine(), "system"))
        src = (HERE.parent / "qemugui" / "mac99_ui_machine.py").read_text()
        for gone in ("System:", "system_var", "_system_chosen"):
            self.assertNotIn(gone, src, gone)


class LibraryOps(unittest.TestCase):

    def test_create_save_duplicate_delete(self):
        with tempfile.TemporaryDirectory() as td:
            lib = model.Library(td)
            m = model.new_machine("Mac OS X")
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

    def test_write_launcher_creates_nvram_if_missing(self):
        """QEMU's raw file driver does not create a missing file: without
        this, the very first Start on a fresh machine fails with "Could not
        open nvram.img" (reproduced against the real binary before this
        fix)."""
        with tempfile.TemporaryDirectory() as td:
            m = load_fixture("mac99-osx.json")
            nvram = Path(td) / "nvram.img"
            self.assertFalse(nvram.exists())
            command.write_launcher(m, "/q", td, "darwin")
            self.assertTrue(nvram.is_file())
            self.assertEqual(nvram.stat().st_size, model.NVRAM_SIZE)

    def test_write_launcher_never_overwrites_existing_nvram(self):
        with tempfile.TemporaryDirectory() as td:
            m = load_fixture("mac99-osx.json")
            nvram = Path(td) / "nvram.img"
            nvram.write_bytes(b"\x01" * model.NVRAM_SIZE)
            command.write_launcher(m, "/q", td, "darwin")
            self.assertEqual(nvram.read_bytes(), b"\x01" * model.NVRAM_SIZE)


class WindowsParity(unittest.TestCase):
    """The same host-platform mechanics as qemugui.command / qemugui.model
    (see tests/test_command.py), now exercised for mac99: one argv, two
    renderings, and a network/audio backend choice restricted per host.
    Forcing platform="win32" here never touches this machine, which is
    always darwin -- see paths.py's own platform-string contract."""

    def test_windows_launcher(self):
        with tempfile.TemporaryDirectory() as td:
            m = load_fixture("mac99-osx.json")
            path, argv = command.write_launcher(m, r"C:\mac99", td, "win32")
            self.assertEqual(path.name, "run.bat")
            self.assertEqual(argv[0], r"C:\mac99\qemu-system-ppc.exe")
            # read the raw bytes: text-mode reads would normalise \r\n away
            raw = path.read_bytes().decode("utf-8")
            self.assertIn(" ^\r\n", raw)
            self.assertNotIn(" \\\r\n", raw)
            self.assertNotIn("sudo", raw)
            self.assertIn("dsound,id=snd", raw)
            self.assertNotIn("coreaudio", raw)

        offered = model.network_modes_for_host("win32")
        self.assertIn("tap", offered)
        for gone in ("vmnet-bridged", "vmnet-shared", "vmnet-host"):
            self.assertNotIn(gone, offered)

    def test_macos_launcher(self):
        with tempfile.TemporaryDirectory() as td:
            m = load_fixture("mac99-osx.json")
            path, argv = command.write_launcher(m, "/Applications/qemu99", td, "darwin")
            self.assertEqual(path.name, "run.command")
            self.assertEqual(argv[0], "/Applications/qemu99/qemu-system-ppc")
            raw = path.read_bytes().decode("utf-8")
            self.assertIn(" \\\n", raw)
            self.assertNotIn(" ^\r\n", raw)
            self.assertNotIn(".exe", raw)
            self.assertIn("coreaudio,id=snd", raw)
            self.assertNotIn("dsound", raw)

        offered = model.network_modes_for_host("darwin")
        for present in ("vmnet-bridged", "vmnet-shared", "vmnet-host"):
            self.assertIn(present, offered)
        self.assertNotIn("tap", offered)

    def test_audio_backend_resolves_per_host(self):
        m = load_fixture("mac99-osx.json")
        self.assertIn("coreaudio,id=snd", command.build_argv(m, "", "/m", "darwin"))
        self.assertIn("dsound,id=snd", command.build_argv(m, "", "/m", "win32"))


if __name__ == "__main__":
    unittest.main()
