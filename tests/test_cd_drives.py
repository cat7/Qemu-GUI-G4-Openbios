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
    NAME = "drive:HL-DT-ST DVDRAM GP57EB40"

    def test_detection(self):
        for f in (self.NAME, "/dev/disk5", "/dev/cdrom", "D:", "d:\\", "\\\\.\\E:"):
            self.assertTrue(paths.is_host_drive(f), f)
        for f in ("/a/disk5.iso", "disk5", "C:\\x.iso", "", "drive:"):
            self.assertFalse(paths.is_host_drive(f), f)

    def test_macos_argv_names_the_drive(self):
        m = machine(None, None, AtaDrive("cdrom", self.NAME))
        argv = command.build_argv(m, QD, MD, "darwin")
        self.assertEqual(drives(argv), ["if=none,id=cd2,driver=host_cdrom,"
                                        "drive=HL-DT-ST DVDRAM GP57EB40,media=cdrom"])
        self.assertIn("ide-cd,drive=cd2,bus=ide.1,unit=0,audiodev=cdaudio", devices(argv))

    def test_drive_name_commas_escaped(self):
        m = machine(None, None, AtaDrive("cdrom", "drive:ACME CD,X"))
        self.assertIn("drive=ACME CD,,X,", drives(command.build_argv(m, QD, MD, "darwin"))[0])

    def test_old_disc_record_follows_the_first_drive(self):
        m = machine(None, None, AtaDrive("cdrom", "/dev/disk5"))
        argv = command.build_argv(m, QD, MD, "darwin")
        self.assertEqual(drives(argv),
                         ["if=none,id=cd2,driver=host_cdrom,filename=/dev/cdrom,media=cdrom"])

    def test_macos_host_drive_without_audio_still_a_device(self):
        m = machine(None, None, AtaDrive("cdrom", self.NAME))
        m.cd_audio = False
        argv = command.build_argv(m, QD, MD, "darwin")
        self.assertIn("ide-cd,drive=cd2,bus=ide.1,unit=0", devices(argv))

    def test_windows_argv(self):
        for f in ("d:", "D:\\", "\\\\.\\D:"):
            m = machine(None, None, AtaDrive("cdrom", f))
            argv = command.build_argv(m, "C:\\q", "C:\\m", "win32")
            self.assertEqual(drives(argv),
                             ["if=none,id=cd2,driver=host_cdrom,drive=D:,media=cdrom"], f)

    def test_records_from_the_other_host(self):
        mac = machine(None, None, AtaDrive("cdrom", self.NAME))
        self.assertIn("filename=/dev/cdrom", drives(command.build_argv(mac, "C:\\q", "C:\\m",
                                                                      "win32"))[0])
        win = machine(None, None, AtaDrive("cdrom", "D:"))
        self.assertIn("filename=/dev/cdrom", drives(command.build_argv(win, QD, MD, "darwin"))[0])

    def test_host_drive_needs_no_sudo(self):
        m = machine(None, None, AtaDrive("cdrom", self.NAME))
        self.assertFalse(command.needs_sudo(m, "darwin"))
        self.assertFalse(command.needs_sudo(m, "win32"))
        self.assertFalse(command.needs_sudo(machine(None, None, AtaDrive("cdrom", "/a.iso")),
                                            "darwin"))

    def test_launcher_runs_without_sudo_or_unmounting(self):
        for f in (self.NAME, "/dev/disk5"):
            text = command.launcher_text(machine(None, None, AtaDrive("cdrom", f)), QD, MD,
                                         "darwin")
            self.assertNotIn("diskutil", text)
            self.assertFalse(any(ln.startswith("sudo ") for ln in text.splitlines()))
            self.assertIn("/Applications/q/", text)
            if f == self.NAME:
                self.assertIn("'if=none,id=cd2,driver=host_cdrom,drive=HL-DT-ST DVDRAM GP57EB40,"
                              "media=cdrom'", text)

    def test_bat_has_no_diskutil(self):
        m = machine(None, None, AtaDrive("cdrom", "D:"))
        self.assertNotIn("diskutil", command.launcher_text(m, "C:\\q", "C:\\m", "win32"))

    def test_not_checked_as_an_image_file(self):
        for f in (self.NAME, "/dev/disk5"):
            m = machine(None, None, AtaDrive("cdrom", f))
            self.assertEqual(m.image_paths(), [])
            _errors, warnings = model.validate(m, QD, "darwin", machine_dir=MD)
            self.assertFalse(any("GP57EB40" in w or "disk5" in w for w in warnings), f)

    def test_record_round_trips(self):
        m = machine(None, None, AtaDrive("cdrom", self.NAME))
        back = Machine.from_json(m.to_json())
        self.assertEqual(back.ata[2].file, self.NAME)

    def test_a_hard_disk_entry_is_not_a_host_drive(self):
        self.assertEqual(model.host_drives(machine(AtaDrive("disk", "/dev/disk5"))), [])


def ioreg_node(vendor, product, media=None):
    node = {"IOObjectClass": "IODVDServices",
            "Device Characteristics": {"Vendor Name": vendor, "Product Name": product},
            "IORegistryEntryChildren": [{"IOObjectClass": "SCSITaskUserClientIniter"}]}
    if media:
        cls, bsd = media
        node["IORegistryEntryChildren"].append(
            {"IOObjectClass": "IODVDBlockStorageDriver", "IORegistryEntryChildren": [
                {"IOObjectClass": cls, "BSD Name": bsd, "Whole": True,
                 "IORegistryEntryChildren": [
                     {"IOObjectClass": "IOMedia", "BSD Name": bsd + "s1", "Whole": False}]}]})
    return node


class Listing(unittest.TestCase):
    def plist(self, d):
        return plistlib.dumps(d)

    def test_drive_with_and_without_disc(self):
        data = self.plist([ioreg_node("HL-DT-ST ", " DVDRAM  GP57EB40", ("IOCDMedia", "disk5")),
                           ioreg_node("MATSHITA", "DVD-R UJ-85J")])
        self.assertEqual(optical.parse_ioreg(data),
                         [("HL-DT-ST DVDRAM GP57EB40", "disk5", "CD"),
                          ("MATSHITA DVD-R UJ-85J", "", "")])

    def test_dvd_media_kind(self):
        data = self.plist([ioreg_node("A", "B", ("IODVDMedia", "disk9"))])
        self.assertEqual(optical.parse_ioreg(data), [("A B", "disk9", "DVD")])

    def test_junk_and_nameless(self):
        self.assertEqual(optical.parse_ioreg(b"junk"), [])
        self.assertEqual(optical.parse_ioreg(self.plist([{"IOObjectClass": "X"}, 3])), [])
        self.assertEqual(optical.parse_ioreg(self.plist([ioreg_node("", "")])), [])

    def test_disc_title(self):
        self.assertEqual(optical.disc_title(self.plist({"VolumeName": "Chess "})), "Chess")
        self.assertEqual(optical.disc_title(self.plist({"MediaName": "x"})), "")
        self.assertEqual(optical.disc_title(b"junk"), "")

    def test_drive_text(self):
        d = optical.OpticalDrive("drive:A B", "A B", "")
        self.assertEqual(d.text, "A B  -  no disc")
        self.assertEqual(optical.OpticalDrive("D:", "D:", "Chess").text, "D:  -  Chess")

    def test_name_matches_qemu_folding(self):
        self.assertEqual(optical.drive_name(" HL-DT-ST\t", "DVDRAM   GP57EB40 "),
                         "HL-DT-ST DVDRAM GP57EB40")


if __name__ == "__main__":
    unittest.main()
