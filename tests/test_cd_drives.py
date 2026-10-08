"""CD images (cue, dmg), host optical drives and CD audio out."""
from __future__ import annotations

import plistlib
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qemugui import mac99_command as command  # noqa: E402
from qemugui import mac99_model as model  # noqa: E402
from qemugui import optical, paths  # noqa: E402
from qemugui.mac99_model import Machine, AtaDrive  # noqa: E402

QD = "/Applications/q"
MD = "/m"


def machine(*ata) -> Machine:
    m = Machine(name="C")
    m.ata = list(ata) + [None] * (4 - len(ata))
    return m


def drives(argv):
    return [argv[i + 1] for i, t in enumerate(argv) if t == "-drive" and "nvr" not in argv[i + 1]]


def devices(argv):
    return [argv[i + 1] for i, t in enumerate(argv) if t == "-device"]


def audiodevs(argv):
    return [argv[i + 1] for i, t in enumerate(argv) if t == "-audiodev"]


class Formats(unittest.TestCase):
    def test_dmg_and_cue_by_suffix(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            for name, fmt in (("a.dmg", "dmg"), ("a.CUE", "cue")):
                (Path(d) / name).write_bytes(b"x" * 600)
                self.assertEqual(paths.detect_format(str(Path(d) / name)), fmt)

    def test_stored_raw_dmg_is_corrected(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "a.dmg").write_bytes(b"x" * 600)
            self.assertEqual(paths.drive_format("raw", "a.dmg", d), "dmg")

    def test_new_disks_stay_raw_or_qcow2(self):
        self.assertEqual(model.FORMATS, ("raw", "qcow2"))


class CdImages(unittest.TestCase):
    def test_cue_cd_with_audio(self):
        m = machine(None, None, AtaDrive("cdrom", "/c/disc.cue", "cue"))
        argv = command.build_argv(m, QD, MD, "darwin")
        self.assertEqual(drives(argv), ["if=none,id=cd2,file=/c/disc.cue,format=cue,media=cdrom"])
        self.assertIn("ide-cd,drive=cd2,bus=ide.1,unit=0,audiodev=cdaudio", devices(argv))
        self.assertEqual(audiodevs(argv), ["coreaudio,id=snd", "coreaudio,id=cdaudio"])

    def test_cue_detected_from_a_raw_record(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "x.cue").write_text("FILE")
            m = machine(None, None, AtaDrive("cdrom", "x.cue", "raw"))
            argv = command.build_argv(m, QD, d, "darwin")
            self.assertIn("format=cue", drives(argv)[0])

    def test_dmg_cd_is_readonly(self):
        m = machine(None, None, AtaDrive("cdrom", "/c/x.dmg", "dmg"))
        argv = command.build_argv(m, QD, MD, "darwin")
        self.assertEqual(drives(argv),
                         ["if=none,id=cd2,file=/c/x.dmg,format=dmg,media=cdrom,readonly=on"])

    def test_dmg_cd_without_audio_keeps_drive_form(self):
        m = machine(None, None, AtaDrive("cdrom", "/c/x.dmg", "dmg"))
        m.cd_audio = False
        argv = command.build_argv(m, QD, MD, "darwin")
        self.assertEqual(drives(argv),
                         ["file=/c/x.dmg,format=dmg,media=cdrom,index=2,readonly=on"])
        self.assertEqual(audiodevs(argv), ["coreaudio,id=snd"])
        self.assertFalse(any(d.startswith("ide-cd") for d in devices(argv)))

    def test_dmg_hard_disk_is_snapshot(self):
        m = machine(AtaDrive("disk", "/c/x.dmg", "dmg"))
        argv = command.build_argv(m, QD, MD, "darwin")
        self.assertEqual(drives(argv), ["file=/c/x.dmg,format=dmg,media=disk,index=0,snapshot=on"])

    def test_raw_hard_disk_unchanged(self):
        m = machine(AtaDrive("disk", "/c/x.img", "raw"))
        argv = command.build_argv(m, QD, MD, "darwin")
        self.assertEqual(drives(argv), ["file=/c/x.img,format=raw,media=disk,index=0"])

    def test_slots_map_to_bus_and_unit(self):
        m = machine(None, None, AtaDrive("cdrom", "/a.iso"), AtaDrive("cdrom", "/b.iso"))
        devs = [d for d in devices(command.build_argv(m, QD, MD, "darwin")) if d.startswith("ide-cd")]
        self.assertEqual([d.split(",")[2:4] for d in devs],
                         [["bus=ide.1", "unit=0"], ["bus=ide.1", "unit=1"]])

    def test_no_cd_no_cd_audiodev(self):
        argv = command.build_argv(machine(AtaDrive("disk", "/a.img")), QD, MD, "darwin")
        self.assertEqual(audiodevs(argv), ["coreaudio,id=snd"])


class CdAudio(unittest.TestCase):
    def test_reuses_the_usb_audiodev(self):
        m = machine(None, None, AtaDrive("cdrom", "/a.iso"))
        m.usb_audio = True
        argv = command.build_argv(m, QD, MD, "darwin")
        self.assertEqual(audiodevs(argv), ["coreaudio,id=snd", "coreaudio,id=usb"])
        self.assertIn("ide-cd,drive=cd2,bus=ide.1,unit=0,audiodev=usb", devices(argv))

    def test_windows_backend(self):
        m = machine(None, None, AtaDrive("cdrom", "/a.iso"))
        argv = command.build_argv(m, "C:\\q", "C:\\m", "win32")
        self.assertIn("dsound,id=cdaudio", audiodevs(argv))

    def test_off_means_no_audiodev_property(self):
        m = machine(None, None, AtaDrive("cdrom", "/a.iso"))
        m.cd_audio = False
        self.assertFalse(any("audiodev=" in d for d in devices(command.build_argv(m, QD, MD, "darwin"))
                             if d.startswith("ide-cd")))

    def test_default_on_and_old_records_load_on(self):
        self.assertTrue(Machine().cd_audio)
        self.assertTrue(Machine.from_dict({"name": "x"}).cd_audio)
        self.assertFalse(Machine.from_dict({"name": "x", "cd_audio": False}).cd_audio)
        self.assertFalse(Machine.from_json(Machine(cd_audio=False).to_json()).cd_audio)


class HostDrive(unittest.TestCase):
    def test_detection(self):
        for f in ("/dev/disk5", "D:", "d:\\", "\\\\.\\E:"):
            self.assertTrue(paths.is_host_drive(f), f)
        for f in ("/a/disk5.iso", "disk5", "C:\\x.iso", ""):
            self.assertFalse(paths.is_host_drive(f), f)

    def test_macos_argv(self):
        m = machine(None, None, AtaDrive("cdrom", "/dev/disk5"))
        argv = command.build_argv(m, QD, MD, "darwin")
        self.assertEqual(drives(argv),
                         ["if=none,id=cd2,driver=host_cdrom,filename=/dev/disk5,media=cdrom"])
        self.assertIn("ide-cd,drive=cd2,bus=ide.1,unit=0,audiodev=cdaudio", devices(argv))

    def test_macos_host_drive_without_audio_still_a_device(self):
        m = machine(None, None, AtaDrive("cdrom", "/dev/disk5"))
        m.cd_audio = False
        argv = command.build_argv(m, QD, MD, "darwin")
        self.assertIn("ide-cd,drive=cd2,bus=ide.1,unit=0", devices(argv))

    def test_windows_argv(self):
        m = machine(None, None, AtaDrive("cdrom", "d:"))
        argv = command.build_argv(m, "C:\\q", "C:\\m", "win32")
        self.assertEqual(drives(argv), ["if=none,id=cd2,file=\\\\.\\D:,format=raw,media=cdrom"])

    def test_needs_sudo_on_macos_only(self):
        m = machine(None, None, AtaDrive("cdrom", "/dev/disk5"))
        self.assertTrue(command.needs_sudo(m, "darwin"))
        self.assertFalse(command.needs_sudo(m, "win32"))
        self.assertFalse(command.needs_sudo(machine(None, None, AtaDrive("cdrom", "/a.iso")),
                                            "darwin"))

    def test_launcher_unmounts_before_sudo(self):
        m = machine(None, None, AtaDrive("cdrom", "/dev/disk5"))
        text = command.launcher_text(m, QD, MD, "darwin")
        lines = text.splitlines()
        self.assertIn("diskutil unmountDisk /dev/disk5", lines)
        self.assertLess(lines.index("diskutil unmountDisk /dev/disk5"), lines.index("sudo -v"))
        self.assertTrue(any(ln.startswith("sudo /Applications/q/") for ln in lines))

    def test_plain_launcher_has_no_diskutil(self):
        m = machine(None, None, AtaDrive("cdrom", "/a.iso"))
        self.assertNotIn("diskutil", command.launcher_text(m, QD, MD, "darwin"))

    def test_bat_has_no_diskutil(self):
        m = machine(None, None, AtaDrive("cdrom", "D:"))
        self.assertNotIn("diskutil", command.launcher_text(m, "C:\\q", "C:\\m", "win32"))

    def test_not_checked_as_an_image_file(self):
        m = machine(None, None, AtaDrive("cdrom", "/dev/disk5"))
        self.assertEqual(m.image_paths(), [])
        _errors, warnings = model.validate(m, QD, "darwin", machine_dir=MD)
        self.assertFalse(any("disk5" in w for w in warnings))

    def test_a_hard_disk_entry_is_not_a_host_drive(self):
        self.assertEqual(model.host_drives(machine(AtaDrive("disk", "/dev/disk5"))), [])


class Listing(unittest.TestCase):
    def plist(self, d):
        return plistlib.dumps(d)

    def test_whole_disks(self):
        data = self.plist({"WholeDisks": ["disk0", "disk5", "x"]})
        self.assertEqual(optical.parse_whole_disks(data), ["disk0", "disk5"])
        self.assertEqual(optical.parse_whole_disks(b"junk"), [])

    def test_optical_info(self):
        d = optical.drive_from_info(self.plist({"DeviceIdentifier": "disk5",
                                                "OpticalMediaType": "CD-ROM",
                                                "VolumeName": "Chess"}))
        self.assertEqual((d.path, d.label), ("/dev/disk5", "Chess - CD-ROM"))

    def test_hard_disk_info_is_not_optical(self):
        self.assertIsNone(optical.drive_from_info(self.plist({"DeviceIdentifier": "disk0",
                                                              "MediaName": "APPLE SSD"})))
        self.assertIsNone(optical.drive_from_info(b"junk"))


if __name__ == "__main__":
    unittest.main()
