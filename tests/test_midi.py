import socket
import threading
import time
import tkinter as tk
import tempfile
import unittest

from qemugui import midi
from qemugui import mac99_command as command
from qemugui import mac99_model as model
from qemugui.mac99_model import Machine, Midi, HostFwd


def _tk_available() -> bool:
    try:
        r = tk.Tk()
    except tk.TclError:
        return False
    r.destroy()
    return True


class TheParser(unittest.TestCase):
    def test_whole_messages(self):
        p = midi.Parser()
        self.assertEqual(p.feed(bytes([0x90, 60, 100, 0x80, 60, 0, 0xC0, 5])),
                         [bytes([0x90, 60, 100]), bytes([0x80, 60, 0]), bytes([0xC0, 5])])

    def test_running_status(self):
        p = midi.Parser()
        self.assertEqual(p.feed(bytes([0x90, 60, 100, 62, 100, 64, 0])),
                         [bytes([0x90, 60, 100]), bytes([0x90, 62, 100]), bytes([0x90, 64, 0])])

    def test_split_across_reads(self):
        p = midi.Parser()
        self.assertEqual(p.feed(bytes([0x90, 60])), [])
        self.assertEqual(p.feed(bytes([100])), [bytes([0x90, 60, 100])])

    def test_real_time_inside_a_message_and_sysex(self):
        p = midi.Parser()
        self.assertEqual(p.feed(bytes([0x90, 60, 0xF8, 100, 0xF0, 1, 0xFE, 2, 0xF7])),
                         [bytes([0xF8]), bytes([0x90, 60, 100]), bytes([0xFE]),
                          bytes([0xF0, 1, 2, 0xF7])])

    def test_sysex_cancels_running_status(self):
        p = midi.Parser()
        self.assertEqual(p.feed(bytes([0x90, 60, 100, 0xF0, 1, 0xF7, 62, 100])),
                         [bytes([0x90, 60, 100]), bytes([0xF0, 1, 0xF7])])

    def test_unterminated_sysex_dropped_at_next_status(self):
        p = midi.Parser()
        self.assertEqual(p.feed(bytes([0xF0, 1, 2, 0x90, 60, 100])), [bytes([0x90, 60, 100])])


def packet_list(payloads, align4):
    out = bytearray(len(payloads).to_bytes(4, "little"))
    for data in payloads:
        out += (123).to_bytes(8, "little") + len(data).to_bytes(2, "little") + data
        if align4:
            out += b"\0" * (-len(out) % 4)
    return bytes(out)


class CoreMidiPacketLists(unittest.TestCase):
    """Packets in a list start on 4-byte boundaries on arm64 only."""

    PAYLOADS = [bytes([0x90, 60, 100]), bytes([0xF8]), bytes([0xF0, 1, 2, 3, 0xF7])]

    def read(self, buf):
        return lambda off, n: buf[off:off + n]

    def test_arm64(self):
        buf = packet_list(self.PAYLOADS, True)
        self.assertEqual(midi.packet_payloads(self.read(buf), True), self.PAYLOADS)

    def test_x86(self):
        buf = packet_list(self.PAYLOADS, False)
        self.assertEqual(midi.packet_payloads(self.read(buf), False), self.PAYLOADS)

    def test_unaligned_walk_misreads_an_arm64_list(self):
        buf = packet_list(self.PAYLOADS, True)
        self.assertNotEqual(midi.packet_payloads(self.read(buf), False), self.PAYLOADS)


class FakeOut:
    def __init__(self):
        self.sent = []
        self.closed = False

    def send(self, msg):
        self.sent.append(msg)

    def close(self):
        self.closed = True


class FakeIn:
    def __init__(self, put):
        self.put = put
        self.closed = False

    def close(self):
        self.closed = True


class FakeBackend:
    def __init__(self, outputs=("Synth",), inputs=("Keys",)):
        self.outputs, self.inputs = list(outputs), list(inputs)
        self.opened_out: list[FakeOut] = []
        self.opened_in: list[FakeIn] = []

    def output_names(self):
        return self.outputs

    def input_names(self):
        return self.inputs

    def open_output(self, name):
        if name not in self.outputs:
            raise OSError(f"MIDI output “{name}” is not connected")
        self.opened_out.append(FakeOut())
        return self.opened_out[-1]

    def open_input(self, name, put):
        if name not in self.inputs:
            raise OSError(f"MIDI input “{name}” is not connected")
        self.opened_in.append(FakeIn(put))
        return self.opened_in[-1]


def wait_for(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


class FakeQemu:
    """QEMU's side of -serial tcp:...,server=on: one client at a time."""

    def __init__(self, port=0):
        self.srv = socket.create_server(("127.0.0.1", port))
        self.port = self.srv.getsockname()[1]
        self.conn = None
        self.got = bytearray()
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        try:
            self.conn, _ = self.srv.accept()
        except OSError:
            return
        self.conn.settimeout(0.1)
        while True:
            try:
                data = self.conn.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                return
            if not data:
                return
            self.got += data

    def close(self):
        for s in (self.conn, self.srv):
            if s:
                s.close()


class TheBridge(unittest.TestCase):
    def setUp(self):
        self.be = FakeBackend()
        self.qemu = FakeQemu()
        self.bridge = None

    def tearDown(self):
        if self.bridge:
            self.bridge.stop()
        self.qemu.close()

    def start(self, out="Synth", inp="Keys"):
        self.bridge = midi.Bridge(self.qemu.port, out, inp, be=self.be).start()
        self.assertTrue(wait_for(lambda: self.qemu.conn is not None and self.be.opened_out
                                 or self.be.opened_in))
        self.assertTrue(wait_for(lambda: self.bridge.status.startswith("connected")))

    def test_guest_to_host(self):
        self.start()
        self.qemu.conn.sendall(bytes([0x90, 60, 100, 62, 100]))
        self.assertTrue(wait_for(lambda: len(self.be.opened_out[0].sent) == 2))
        self.assertEqual(self.be.opened_out[0].sent, [bytes([0x90, 60, 100]),
                                                      bytes([0x90, 62, 100])])

    def test_host_to_guest(self):
        self.start()
        self.be.opened_in[0].put(bytes([0xB0, 7, 64]))
        self.be.opened_in[0].put(bytes([0xF0, 0x7E, 0xF7]))
        self.assertTrue(wait_for(lambda: len(self.qemu.got) == 6))
        self.assertEqual(bytes(self.qemu.got), bytes([0xB0, 7, 64, 0xF0, 0x7E, 0xF7]))

    def test_status_names_the_devices(self):
        self.start()
        self.assertEqual(self.bridge.status, "connected, out to “Synth”, in from “Keys”")

    def test_missing_device_is_reported_and_the_other_still_works(self):
        self.start(out="Gone")
        self.assertIn("MIDI output “Gone” is not connected", self.bridge.status)
        self.be.opened_in[0].put(bytes([0xC0, 1]))
        self.assertTrue(wait_for(lambda: bytes(self.qemu.got) == bytes([0xC0, 1])))

    def test_waits_for_the_machine_and_comes_back(self):
        port = self.qemu.port
        self.qemu.close()
        self.bridge = midi.Bridge(port, "Synth", "", be=self.be)
        self.bridge.RETRY = 0.05
        self.bridge.start()
        time.sleep(0.2)
        self.assertTrue(self.bridge.status.startswith("waiting"))
        self.qemu = FakeQemu(port)
        self.assertTrue(wait_for(lambda: self.bridge.status.startswith("connected")))
        self.qemu.close()      # the machine quits: devices close, the bridge waits again
        self.assertTrue(wait_for(lambda: self.bridge.status.startswith("waiting")))
        self.assertTrue(self.be.opened_out[0].closed)
        self.qemu = FakeQemu(port)
        self.assertTrue(wait_for(lambda: len(self.be.opened_out) == 2))

    def test_stop_closes_the_devices(self):
        self.start()
        self.bridge.stop()
        self.assertTrue(self.be.opened_out[0].closed)
        self.assertTrue(self.be.opened_in[0].closed)
        self.bridge = None


class TheMachineRecord(unittest.TestCase):
    def test_off_by_default(self):
        m = Machine()
        self.assertFalse(m.midi.enabled)
        self.assertEqual(m.midi.port, 1234)
        self.assertNotIn("-serial", command.build_argv(m, "", "/m", "darwin"))

    def test_saved_and_loaded(self):
        m = Machine(name="t", midi=Midi(True, 4321, "Synth", "Keys"))
        self.assertEqual(Machine.from_dict(m.to_dict()).midi, Midi(True, 4321, "Synth", "Keys"))
        self.assertEqual(Machine.from_dict({"name": "t"}).midi, Midi())

    def test_modem_port_on_localhost(self):
        m = Machine(name="t", midi=Midi(True, 4321, "Synth", ""))
        for platform in ("darwin", "win32"):
            argv = command.build_argv(m, "", "/m", platform)
            i = argv.index("-serial")
            self.assertEqual(argv[i + 1], "tcp:127.0.0.1:4321,server=on,wait=off")
            self.assertEqual(argv.count("-serial"), 1)

    def test_validation(self):
        def check(mi, hostfwd=()):
            m = Machine(name="t", midi=mi)
            m.network.hostfwd = list(hostfwd)
            return model.validate(m, None, check_files=False)
        errors, warnings = check(Midi(True, 80, "Synth", ""))
        self.assertTrue(any("MIDI port" in e for e in errors))
        errors, warnings = check(Midi(True, 1234, "", ""))
        self.assertFalse([e for e in errors if "MIDI" in e])
        self.assertTrue(any("MIDI" in w for w in warnings))
        errors, _w = check(Midi(True, 1234, "Synth", ""), [HostFwd("tcp", "1234", "80")])
        self.assertTrue(any("1234" in e for e in errors))
        errors, warnings = check(Midi(False, 80, "", ""))
        self.assertFalse([t for t in errors + warnings if "MIDI" in t])


@unittest.skipUnless(_tk_available(), "no display")
class TheMidiTab(unittest.TestCase):
    def setUp(self):
        from qemugui.mac99_ui_machine import MachineEditor
        self.td = tempfile.TemporaryDirectory()
        self.root = tk.Tk()
        self.root.withdraw()
        self.saved = midi._backend
        midi._backend = FakeBackend(outputs=("Synth", "Other"), inputs=("Keys",))
        self.Editor = MachineEditor

    def tearDown(self):
        midi._backend = self.saved
        self.root.destroy()
        self.td.cleanup()

    def editor(self, m):
        ed = self.Editor(self.root, m, model.Library(self.td.name), "/q", on_save=lambda *a: None)
        ed.withdraw()
        return ed

    def test_off_by_default_and_disabled(self):
        ed = self.editor(Machine(name="t"))
        self.assertFalse(ed.midi_on.get())
        self.assertEqual(str(ed.midi_out_cb.cget("state")), "disabled")
        self.assertEqual(ed.collect().midi, Midi(False, 1234, "", ""))

    def test_lists_the_devices(self):
        ed = self.editor(Machine(name="t"))
        self.assertEqual(list(ed.midi_out_cb.cget("values")), ["(none)", "Synth", "Other"])
        self.assertEqual(list(ed.midi_in_cb.cget("values")), ["(none)", "Keys"])

    def test_loads_and_collects(self):
        ed = self.editor(Machine(name="t", midi=Midi(True, 4321, "Other", "Keys")))
        self.assertEqual(str(ed.midi_out_cb.cget("state")), "readonly")
        self.assertEqual(ed.collect().midi, Midi(True, 4321, "Other", "Keys"))
        ed.midi_in_var.set("(none)")
        self.assertEqual(ed.collect().midi.input, "")

    def test_keeps_a_device_that_is_not_plugged_in(self):
        ed = self.editor(Machine(name="t", midi=Midi(True, 1234, "Away", "")))
        self.assertIn("Away", list(ed.midi_out_cb.cget("values")))
        self.assertEqual(ed.collect().midi.output, "Away")


class StartingTheBridge(unittest.TestCase):
    def test_only_when_on_with_a_device(self):
        from qemugui import mac99_ui_main as ui
        self.assertIsNone(ui.start_midi(Machine(name="t")))
        self.assertIsNone(ui.start_midi(Machine(name="t", midi=Midi(True, 1234, "", ""))))
        saved = midi._backend
        midi._backend = FakeBackend()
        try:
            b = ui.start_midi(Machine(name="t", midi=Midi(True, 1, "Synth", "")))
            self.assertIsInstance(b, midi.Bridge)
            b.stop()
        finally:
            midi._backend = saved


if __name__ == "__main__":
    unittest.main()
