"""MIDI between the guest's modem port and the host's MIDI devices.

QEMU serves the modem port (ESCC A) as a TCP server on 127.0.0.1; while the
machine runs, a bridge here connects to it and passes MIDI both ways, using
CoreMIDI on macOS and winmm on Windows through ctypes. In Mac OS 9, OMS
drives a MIDI interface on that port.
"""

from __future__ import annotations

import ctypes
import platform
import queue
import socket
import sys
import threading
import time

DEFAULT_PORT = 1234


# ---------------------------------------------------------------- MIDI bytes

def msg_len(status: int) -> int:
    """Bytes in a message with this status byte, 0 for SysEx or unknown."""
    if status < 0x80:
        return 0
    hi = status & 0xF0
    if hi in (0x80, 0x90, 0xA0, 0xB0, 0xE0):
        return 3
    if hi in (0xC0, 0xD0):
        return 2
    if status in (0xF1, 0xF3):
        return 2
    if status == 0xF2:
        return 3
    if status in (0xF6, 0xF8, 0xFA, 0xFB, 0xFC, 0xFE, 0xFF):
        return 1
    return 0


class Parser:
    """Splits the guest's byte stream into whole messages: running status,
    real-time bytes in the middle of anything, SysEx up to F7."""

    def __init__(self):
        self.running = 0
        self.msg: list[int] = []
        self.expected = 0
        self.sysex: list[int] | None = None
        self.out: list[bytes] = []

    def feed(self, data: bytes) -> list[bytes]:
        for b in data:
            self._byte(b)
        out, self.out = self.out, []
        return out

    def _byte(self, b: int):
        if b >= 0xF8:
            self.out.append(bytes([b]))
            return
        if self.sysex is not None:
            if b == 0xF7:
                self.sysex.append(b)
                self.out.append(bytes(self.sysex))
                self.sysex = None
                return
            if b < 0x80:
                self.sysex.append(b)
                return
            self.sysex = None       # a status byte ends an unterminated SysEx
        if b == 0xF0:
            self.sysex = [b]
            self.running = 0
            self.msg = []
            return
        if b >= 0x80:
            n = msg_len(b)
            self.running = b if b < 0xF0 else 0
            if n == 1:
                self.msg = []
                self.out.append(bytes([b]))
                return
            self.msg = [b] if n else []
            self.expected = n
            return
        if not self.msg:
            if not self.running:
                return
            self.msg = [self.running]
            self.expected = msg_len(self.running)
        self.msg.append(b)
        if len(self.msg) >= self.expected:
            self.out.append(bytes(self.msg))
            self.msg = []


def packet_payloads(read, align4: bool) -> list[bytes]:
    """The data of each packet in a CoreMIDI MIDIPacketList. read(offset, n)
    returns n bytes of the list. The list is packed to 4 bytes; on arm64
    each packet also starts on a 4-byte boundary (MIDIPacketNext)."""
    count = int.from_bytes(read(0, 4), "little")
    off = 4
    out = []
    for _ in range(count):
        length = int.from_bytes(read(off + 8, 2), "little")
        out.append(read(off + 10, length))
        off += 10 + length
        if align4:
            off = (off + 3) & ~3
    return out


# ---------------------------------------------------------------- backends

class Unsupported:
    def output_names(self) -> list[str]:
        return []

    def input_names(self) -> list[str]:
        return []

    def open_output(self, name: str):
        raise OSError("MIDI is not available on this computer")

    def open_input(self, name: str, put):
        raise OSError("MIDI is not available on this computer")


class CoreMidi:
    """macOS. One client per opened device; MIDISend copies the data, so the
    packet list may live on the stack of the call."""

    def __init__(self):
        cm = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreMIDI.framework/CoreMIDI")
        cf = ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        u32, vp = ctypes.c_uint32, ctypes.c_void_p
        cf.CFStringCreateWithCString.restype = vp
        cf.CFStringCreateWithCString.argtypes = [vp, ctypes.c_char_p, u32]
        cf.CFStringGetCString.restype = ctypes.c_bool
        cf.CFStringGetCString.argtypes = [vp, ctypes.c_char_p, ctypes.c_long, u32]
        cf.CFRelease.argtypes = [vp]
        cf.CFRelease.restype = None
        cm.MIDIClientCreate.argtypes = [vp, vp, vp, ctypes.POINTER(u32)]
        cm.MIDIOutputPortCreate.argtypes = [u32, vp, ctypes.POINTER(u32)]
        cm.MIDIInputPortCreate.argtypes = [u32, vp, vp, vp, ctypes.POINTER(u32)]
        cm.MIDIGetNumberOfDestinations.restype = ctypes.c_ulong
        cm.MIDIGetDestination.restype = u32
        cm.MIDIGetDestination.argtypes = [ctypes.c_ulong]
        cm.MIDIGetNumberOfSources.restype = ctypes.c_ulong
        cm.MIDIGetSource.restype = u32
        cm.MIDIGetSource.argtypes = [ctypes.c_ulong]
        cm.MIDIObjectGetStringProperty.argtypes = [u32, vp, ctypes.POINTER(vp)]
        cm.MIDISend.argtypes = [u32, u32, vp]
        cm.MIDIPortConnectSource.argtypes = [u32, u32, vp]
        cm.MIDIPortDisconnectSource.argtypes = [u32, u32]
        cm.MIDIPacketListInit.restype = vp
        cm.MIDIPacketListInit.argtypes = [vp]
        cm.MIDIPacketListAdd.restype = vp
        cm.MIDIPacketListAdd.argtypes = [vp, ctypes.c_ulong, vp, ctypes.c_uint64,
                                         ctypes.c_ulong, vp]
        cm.MIDIClientDispose.argtypes = [u32]
        cm.MIDIPortDispose.argtypes = [u32]
        self.cm, self.cf = cm, cf
        self.display_name = vp.in_dll(cm, "kMIDIPropertyDisplayName")
        self.read_proc_type = ctypes.CFUNCTYPE(None, vp, vp, vp)
        self.align4 = platform.machine() == "arm64"

    def _cfstr(self, s: str):
        return self.cf.CFStringCreateWithCString(None, s.encode("utf-8"), 0x08000100)

    def _name(self, endpoint: int) -> str:
        ref = ctypes.c_void_p()
        if self.cm.MIDIObjectGetStringProperty(endpoint, self.display_name,
                                               ctypes.byref(ref)) == 0 and ref.value:
            buf = ctypes.create_string_buffer(256)
            ok = self.cf.CFStringGetCString(ref.value, buf, 256, 0x08000100)
            self.cf.CFRelease(ref.value)
            if ok:
                return buf.value.decode("utf-8", "replace")
        return f"MIDI device {endpoint}"

    def _endpoints(self, count, get) -> list[tuple[str, int]]:
        out = []
        for i in range(count()):
            ep = get(i)
            if ep:
                out.append((self._name(ep), ep))
        return out

    def output_names(self) -> list[str]:
        return [n for n, _ in self._endpoints(self.cm.MIDIGetNumberOfDestinations,
                                              self.cm.MIDIGetDestination)]

    def input_names(self) -> list[str]:
        return [n for n, _ in self._endpoints(self.cm.MIDIGetNumberOfSources,
                                              self.cm.MIDIGetSource)]

    def _client(self, what: str) -> ctypes.c_uint32:
        client = ctypes.c_uint32()
        name = self._cfstr(f"Qemu-system-ppc G4 {what}")
        rc = self.cm.MIDIClientCreate(name, None, None, ctypes.byref(client))
        self.cf.CFRelease(name)
        if rc != 0:
            raise OSError(f"MIDIClientCreate failed ({rc})")
        return client

    def open_output(self, name: str):
        dest = dict(self._endpoints(self.cm.MIDIGetNumberOfDestinations,
                                    self.cm.MIDIGetDestination)).get(name)
        if not dest:
            raise OSError(f"MIDI output “{name}” is not connected")
        return _CoreMidiOut(self, dest)

    def open_input(self, name: str, put):
        src = dict(self._endpoints(self.cm.MIDIGetNumberOfSources,
                                   self.cm.MIDIGetSource)).get(name)
        if not src:
            raise OSError(f"MIDI input “{name}” is not connected")
        return _CoreMidiIn(self, src, put)


class _CoreMidiOut:
    CHUNK = 4096

    def __init__(self, be: CoreMidi, dest: int):
        self.be, self.dest = be, dest
        self.client = be._client("MIDI out")
        self.port = ctypes.c_uint32()
        pname = be._cfstr("out")
        rc = be.cm.MIDIOutputPortCreate(self.client.value, pname, ctypes.byref(self.port))
        be.cf.CFRelease(pname)
        if rc != 0:
            be.cm.MIDIClientDispose(self.client.value)
            raise OSError(f"MIDIOutputPortCreate failed ({rc})")

    def send(self, msg: bytes):
        cm = self.be.cm
        for i in range(0, len(msg), self.CHUNK):
            part = msg[i:i + self.CHUNK]
            data = (ctypes.c_uint8 * len(part)).from_buffer_copy(part)
            size = len(part) + 64
            buf = (ctypes.c_uint8 * size)()
            plist = ctypes.cast(buf, ctypes.c_void_p)
            pkt = cm.MIDIPacketListInit(plist)
            if cm.MIDIPacketListAdd(plist, size, pkt, 0, len(part),
                                    ctypes.cast(data, ctypes.c_void_p)):
                cm.MIDISend(self.port.value, self.dest, plist)

    def close(self):
        if self.port.value:
            self.be.cm.MIDIPortDispose(self.port.value)
            self.port.value = 0
        if self.client.value:
            self.be.cm.MIDIClientDispose(self.client.value)
            self.client.value = 0


class _CoreMidiIn:
    def __init__(self, be: CoreMidi, src: int, put):
        self.be, self.src, self.put = be, src, put
        self.client = be._client("MIDI in")
        self.port = ctypes.c_uint32()
        self._proc = be.read_proc_type(self._read)
        pname = be._cfstr("in")
        rc = be.cm.MIDIInputPortCreate(self.client.value, pname, self._proc, None,
                                       ctypes.byref(self.port))
        be.cf.CFRelease(pname)
        if rc != 0:
            be.cm.MIDIClientDispose(self.client.value)
            raise OSError(f"MIDIInputPortCreate failed ({rc})")
        be.cm.MIDIPortConnectSource(self.port.value, src, None)

    def _read(self, plist, _ref, _src):
        # CoreMIDI's own thread; an exception here would only be printed
        try:
            if not plist:
                return
            for data in packet_payloads(lambda off, n: ctypes.string_at(plist + off, n),
                                        self.be.align4):
                if data:
                    self.put(data)
        except Exception:
            pass

    def close(self):
        if self.port.value:
            self.be.cm.MIDIPortDisconnectSource(self.port.value, self.src)
            self.be.cm.MIDIPortDispose(self.port.value)
            self.port.value = 0
        if self.client.value:
            self.be.cm.MIDIClientDispose(self.client.value)
            self.client.value = 0


class WinMidi:
    """Windows. The input callback must not call into the system, so it only
    queues; SysEx buffers go back to the driver from the bridge's sender
    thread (take())."""

    MIM_DATA, MIM_LONGDATA = 0x3C3, 0x3C4
    CALLBACK_FUNCTION = 0x00030000
    MHDR_DONE = 0x1
    MIDIERR_STILLPLAYING = 65
    SYSEX_BUFFERS, SYSEX_SIZE = 4, 4096

    def __init__(self):
        from ctypes import wintypes as wt
        mm = ctypes.WinDLL("winmm")
        up, vp = ctypes.c_size_t, ctypes.c_void_p

        class OutCaps(ctypes.Structure):
            _fields_ = [("wMid", wt.WORD), ("wPid", wt.WORD), ("vDriverVersion", wt.UINT),
                        ("szPname", ctypes.c_wchar * 32), ("wTechnology", wt.WORD),
                        ("wVoices", wt.WORD), ("wNotes", wt.WORD),
                        ("wChannelMask", wt.WORD), ("dwSupport", wt.DWORD)]

        class InCaps(ctypes.Structure):
            _fields_ = [("wMid", wt.WORD), ("wPid", wt.WORD), ("vDriverVersion", wt.UINT),
                        ("szPname", ctypes.c_wchar * 32), ("dwSupport", wt.DWORD)]

        class Hdr(ctypes.Structure):
            _fields_ = [("lpData", vp), ("dwBufferLength", wt.DWORD),
                        ("dwBytesRecorded", wt.DWORD), ("dwUser", up), ("dwFlags", wt.DWORD),
                        ("lpNext", vp), ("reserved", up), ("dwOffset", wt.DWORD),
                        ("dwReserved", up * 8)]

        hp = ctypes.POINTER(vp)
        mm.midiOutGetNumDevs.restype = wt.UINT
        mm.midiInGetNumDevs.restype = wt.UINT
        mm.midiOutGetDevCapsW.argtypes = [up, vp, wt.UINT]
        mm.midiInGetDevCapsW.argtypes = [up, vp, wt.UINT]
        mm.midiOutOpen.argtypes = [hp, wt.UINT, up, up, wt.DWORD]
        mm.midiInOpen.argtypes = [hp, wt.UINT, vp, up, wt.DWORD]
        mm.midiOutShortMsg.argtypes = [vp, wt.DWORD]
        for f in ("midiOutPrepareHeader", "midiOutUnprepareHeader", "midiOutLongMsg",
                  "midiInPrepareHeader", "midiInUnprepareHeader", "midiInAddBuffer"):
            getattr(mm, f).argtypes = [vp, vp, wt.UINT]
        for f in ("midiOutClose", "midiOutReset", "midiInStart", "midiInStop", "midiInReset",
                  "midiInClose"):
            getattr(mm, f).argtypes = [vp]
        self.mm, self.OutCaps, self.InCaps, self.Hdr = mm, OutCaps, InCaps, Hdr
        self.proc_type = ctypes.WINFUNCTYPE(None, vp, wt.UINT, up, up, up)

    def _names(self, count, caps_fn, caps_type) -> list[str]:
        out = []
        caps = caps_type()
        for i in range(count()):
            if caps_fn(i, ctypes.byref(caps), ctypes.sizeof(caps)) == 0:
                out.append(caps.szPname)
        return out

    def output_names(self) -> list[str]:
        return self._names(self.mm.midiOutGetNumDevs, self.mm.midiOutGetDevCapsW, self.OutCaps)

    def input_names(self) -> list[str]:
        return self._names(self.mm.midiInGetNumDevs, self.mm.midiInGetDevCapsW, self.InCaps)

    def open_output(self, name: str):
        names = self.output_names()
        if name not in names:
            raise OSError(f"MIDI output “{name}” is not connected")
        return _WinMidiOut(self, names.index(name))

    def open_input(self, name: str, put):
        names = self.input_names()
        if name not in names:
            raise OSError(f"MIDI input “{name}” is not connected")
        return _WinMidiIn(self, names.index(name), put)


class _WinMidiOut:
    def __init__(self, be: WinMidi, index: int):
        self.be = be
        self.h = ctypes.c_void_p()
        rc = be.mm.midiOutOpen(ctypes.byref(self.h), index, 0, 0, 0)
        if rc != 0:
            raise OSError(f"midiOutOpen failed ({rc})")

    def send(self, msg: bytes):
        mm = self.be.mm
        if msg[0] != 0xF0:
            word = 0
            for i, b in enumerate(msg[:3]):
                word |= b << (8 * i)
            mm.midiOutShortMsg(self.h, word)
            return
        buf = ctypes.create_string_buffer(msg, len(msg))
        hdr = self.be.Hdr()
        hdr.lpData = ctypes.cast(buf, ctypes.c_void_p)
        hdr.dwBufferLength = len(msg)
        size = ctypes.sizeof(hdr)
        if mm.midiOutPrepareHeader(self.h, ctypes.byref(hdr), size) != 0:
            return
        if mm.midiOutLongMsg(self.h, ctypes.byref(hdr), size) == 0:
            # 31250 baud is ~3 kB/s; the driver clears the buffer when it is sent
            deadline = time.monotonic() + 2 + len(msg) / 2000
            while not hdr.dwFlags & self.be.MHDR_DONE and time.monotonic() < deadline:
                time.sleep(0.002)
        while (mm.midiOutUnprepareHeader(self.h, ctypes.byref(hdr), size)
               == self.be.MIDIERR_STILLPLAYING):
            mm.midiOutReset(self.h)

    def close(self):
        if self.h:
            self.be.mm.midiOutReset(self.h)
            self.be.mm.midiOutClose(self.h)
            self.h = ctypes.c_void_p()


class _WinMidiIn:
    def __init__(self, be: WinMidi, index: int, put):
        self.be, self.put = be, put
        self.h = ctypes.c_void_p()
        self.closing = False
        self._proc = be.proc_type(self._callback)
        rc = be.mm.midiInOpen(ctypes.byref(self.h), index,
                              ctypes.cast(self._proc, ctypes.c_void_p), 0,
                              be.CALLBACK_FUNCTION)
        if rc != 0:
            raise OSError(f"midiInOpen failed ({rc})")
        self.bufs, self.hdrs = [], {}
        for _ in range(be.SYSEX_BUFFERS):
            buf = ctypes.create_string_buffer(be.SYSEX_SIZE)
            hdr = be.Hdr()
            hdr.lpData = ctypes.cast(buf, ctypes.c_void_p)
            hdr.dwBufferLength = be.SYSEX_SIZE
            be.mm.midiInPrepareHeader(self.h, ctypes.byref(hdr), ctypes.sizeof(hdr))
            be.mm.midiInAddBuffer(self.h, ctypes.byref(hdr), ctypes.sizeof(hdr))
            self.bufs.append(buf)
            self.hdrs[ctypes.addressof(hdr)] = hdr
        be.mm.midiInStart(self.h)

    def _callback(self, _h, msg, _inst, p1, _p2):
        if msg == self.be.MIM_DATA:
            status = p1 & 0xFF
            n = msg_len(status)
            if n:
                self.put(bytes([status, (p1 >> 8) & 0xFF, (p1 >> 16) & 0xFF][:n]))
        elif msg == self.be.MIM_LONGDATA:
            self.put(p1)            # a SysEx header, see take()

    def take(self, addr: int) -> bytes:
        """The SysEx bytes in a returned buffer, which goes back to the driver."""
        hdr = self.hdrs.get(addr)
        if hdr is None:
            return b""
        data = ctypes.string_at(hdr.lpData, hdr.dwBytesRecorded) if hdr.dwBytesRecorded else b""
        if not self.closing:
            hdr.dwBytesRecorded = 0
            self.be.mm.midiInAddBuffer(self.h, ctypes.byref(hdr), ctypes.sizeof(hdr))
        return data

    def close(self):
        if not self.h:
            return
        mm = self.be.mm
        self.closing = True
        mm.midiInStop(self.h)
        mm.midiInReset(self.h)          # returns the SysEx buffers
        for hdr in self.hdrs.values():
            mm.midiInUnprepareHeader(self.h, ctypes.byref(hdr), ctypes.sizeof(hdr))
        mm.midiInClose(self.h)
        self.h = ctypes.c_void_p()


_backend = None


def backend():
    global _backend
    if _backend is None:
        try:
            if sys.platform == "darwin":
                _backend = CoreMidi()
            elif sys.platform == "win32":
                _backend = WinMidi()
            else:
                _backend = Unsupported()
        except OSError:
            _backend = Unsupported()
    return _backend


# ---------------------------------------------------------------- the bridge

class Bridge:
    """Connects to the machine's modem port whenever it is there and passes
    MIDI both ways until stop(). A machine that quits and starts again is
    picked up again."""

    RETRY = 1.0

    def __init__(self, port: int, output: str, input: str, be=None, host: str = "127.0.0.1"):
        self.port, self.output, self.input, self.host = port, output, input, host
        self.be = be or backend()
        self._stop = threading.Event()
        self._status = f"waiting for the machine on port {port}"
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, name="midi-bridge", daemon=True)

    def start(self) -> "Bridge":
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=3)

    @property
    def status(self) -> str:
        with self._lock:
            return self._status

    def _set(self, text: str):
        with self._lock:
            self._status = text

    def _run(self):
        while not self._stop.is_set():
            try:
                sock = socket.create_connection((self.host, self.port), timeout=1)
            except OSError:
                self._stop.wait(self.RETRY)
                continue
            try:
                self._session(sock)
            finally:
                sock.close()
            if not self._stop.is_set():
                self._set(f"waiting for the machine on port {self.port}")

    def _session(self, sock: socket.socket):
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.settimeout(0.2)
        to_guest: queue.SimpleQueue = queue.SimpleQueue()
        out = inp = None
        problems = []
        if self.output:
            try:
                out = self.be.open_output(self.output)
            except OSError as e:
                problems.append(str(e))
        if self.input:
            try:
                inp = self.be.open_input(self.input, to_guest.put)
            except OSError as e:
                problems.append(str(e))
        parts = [p for p in (f"out to “{self.output}”" if out else "",
                             f"in from “{self.input}”" if inp else "") if p]
        self._set("; ".join([", ".join(["connected"] + parts)] + problems))
        done = threading.Event()
        sender = threading.Thread(target=self._send, args=(sock, to_guest, inp, done),
                                  name="midi-to-guest", daemon=True)
        sender.start()
        parser = Parser()
        try:
            while not self._stop.is_set() and not done.is_set():
                try:
                    data = sock.recv(4096)
                except socket.timeout:
                    continue
                if not data:
                    return
                if out is not None:
                    for msg in parser.feed(data):
                        out.send(msg)
        except OSError:
            return
        finally:
            done.set()
            for dev in (inp, out):
                if dev is not None:
                    try:
                        dev.close()
                    except OSError:
                        pass
            sender.join(timeout=1)

    def _send(self, sock, to_guest, inp, done):
        while not done.is_set() and not self._stop.is_set():
            try:
                item = to_guest.get(timeout=0.2)
            except queue.Empty:
                continue
            data = inp.take(item) if isinstance(item, int) else item
            if not data:
                continue
            try:
                sock.sendall(data)
            except OSError:
                done.set()
                return
