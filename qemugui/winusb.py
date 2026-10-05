"""Host USB devices on Windows. QEMU's usb-host (libusb) can open a device
only while it is on Windows' own WinUSB driver. ``winusb-switch.exe``, next
to ``qemu-system-ppc64.exe``, moves one device there and back:

* ``list`` runs unelevated and prints one JSON object per device;
* ``bind`` / ``unbind`` need administrator rights, so they run through
  ShellExecuteEx "runas" (one UAC prompt) and write their result into a
  file (``--out``), since an elevated process's output cannot be read.

No Tk in here.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import paths

HELPER = "winusb-switch.exe"

# Exit codes of winusb-switch
DONE, FAILED, USAGE, REFUSED, NOT_ADMIN, IN_USE = 0, 1, 2, 3, 4, 5
PENDING = 3010      # done once the device is replugged

ERROR_CANCELLED = 1223              # the UAC prompt was declined
CREATE_NO_WINDOW = 0x08000000

ID_RE = re.compile(r"^[0-9a-f]{4}:[0-9a-f]{4}$")

WINUSB_STATE = "WinUSB (ready for QEMU)"
WINDOWS_STATE = "Windows driver"
NOT_READY_NOTE = "not passed through: owned by Windows"


@dataclass
class WinDevice:
    id: str
    instance: str
    description: str = ""
    product: str = ""
    speed: str = ""
    cls: str = ""
    class_guid: str = ""
    service: str = ""
    inf: str = ""
    driver: str = ""
    provider: str = ""          # "winusb-switch": its own signed package
    composite: bool = False
    winusb: bool = False
    problem: int = 0
    functions: list = field(default_factory=list)
    refuse: str = ""

    @property
    def label(self) -> str:
        return self.product or self.description or f"USB device {self.id}"

    @property
    def switchable(self) -> bool:
        return not self.refuse

    @property
    def state(self) -> str:
        if self.winusb:
            return WINUSB_STATE
        return f"{WINDOWS_STATE} ({self.service})" if self.service else WINDOWS_STATE


class DeviceList(list):
    """Devices from `list`, with its warnings: third-party filters or
    drivers (UsbDk, libusbK, libusb0) that can take devices away."""
    warnings: list = []


def warnings_text(devices) -> str:
    """The list's warnings in one line, "" if none."""
    return "; ".join(str(w) for w in getattr(devices, "warnings", []) or [])


def _obj(line: str) -> dict:
    o = json.loads(line)
    if not isinstance(o, dict):
        raise ValueError(f"not a JSON object: {line!r}")
    return o


def parse_list(text: str) -> list[WinDevice]:
    """`winusb-switch list` output -> devices (with .warnings). ValueError
    on a bad line."""
    out = DeviceList()
    out.warnings = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        o = _obj(line)
        if o.get("ok") is False:
            raise ValueError(o.get("error") or "list failed")
        if "id" not in o and isinstance(o.get("warnings"), list):
            out.warnings += [str(w) for w in o["warnings"]]
            continue
        dev_id = str(o.get("id", "")).lower()
        if not ID_RE.match(dev_id) or not o.get("instance"):
            raise ValueError(f"bad device line: {line!r}")
        funcs = o.get("functions") or []
        if not isinstance(funcs, list):
            raise ValueError(f"bad functions: {line!r}")
        out.append(WinDevice(
            id=dev_id,
            instance=str(o["instance"]),
            description=str(o.get("description", "")),
            product=str(o.get("product", "")),
            speed=str(o.get("speed", "")),
            cls=str(o.get("class", "")),
            class_guid=str(o.get("class_guid", "")),
            service=str(o.get("service", "")),
            inf=str(o.get("inf", "")),
            driver=str(o.get("driver", "")),
            provider=str(o.get("provider", "")),
            composite=bool(o.get("composite", False)),
            winusb=bool(o.get("winusb", False)),
            problem=int(o.get("problem", 0)),
            functions=[f for f in funcs if isinstance(f, dict)],
            refuse=str(o.get("refuse", "")),
        ))
    return out


def parse_result(text: str) -> dict:
    """`bind`/`unbind` output -> its one object."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) != 1:
        raise ValueError(f"expected one line, got {len(lines)}")
    o = _obj(lines[0])
    if o.get("op") not in ("bind", "unbind") or "ok" not in o:
        raise ValueError(f"bad result: {lines[0]!r}")
    return o


def helper_path(folder: Path | str | None = None) -> Path:
    """Beside qemu-system-ppc64.exe, which is the folder this GUI runs from."""
    return Path(folder or paths.install_dir()) / HELPER


def missing_message() -> str:
    return f"{HELPER} is not next to {paths.qemu_binary_name('win32')}."


def list_devices(folder: Path | str | None = None) -> tuple[list[WinDevice], str]:
    """(devices, "") or ([], why not). Unelevated; never changes a driver."""
    exe = helper_path(folder)
    if not exe.is_file():
        return [], missing_message()
    try:
        r = subprocess.run([str(exe), "list"], capture_output=True, timeout=60,
                           creationflags=CREATE_NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as e:
        return [], f"{HELPER} list failed: {e}"
    try:
        return parse_list(r.stdout.decode("utf-8", "replace")), ""
    except ValueError as e:
        return [], f"{HELPER} list failed: {e}"


def owned_ids(folder: Path | str | None = None) -> tuple[set[str] | None, str]:
    """Ids of the devices QEMU owns now (on WinUSB), or (None, why not)."""
    devices, why = list_devices(folder)
    if why:
        return None, why
    return {d.id for d in devices if d.winusb}, ""


def passthrough_note(chosen: list[str], owned: set[str] | None, why: str) -> str:
    """What the launcher leaves out of the ticked devices, and why; "" if none."""
    if not chosen:
        return ""
    if owned is None:
        return f"No USB device is passed through: {why}"
    left = [i for i in chosen if i not in owned]
    if not left:
        return ""
    return ("Not passed through, owned by Windows (Give to QEMU first): "
            + ", ".join(left))


def shell_execute_runas(exe: str, params: str) -> int | None:
    """Run *exe* elevated (one UAC prompt), hidden, and wait for it. Its exit
    code, or None when the prompt was declined."""
    import ctypes
    from ctypes import wintypes

    class SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("fMask", ctypes.c_ulong),
                    ("hwnd", wintypes.HWND), ("lpVerb", wintypes.LPCWSTR),
                    ("lpFile", wintypes.LPCWSTR), ("lpParameters", wintypes.LPCWSTR),
                    ("lpDirectory", wintypes.LPCWSTR), ("nShow", ctypes.c_int),
                    ("hInstApp", wintypes.HINSTANCE), ("lpIDList", ctypes.c_void_p),
                    ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY),
                    ("dwHotKey", wintypes.DWORD), ("hIconOrMonitor", wintypes.HANDLE),
                    ("hProcess", wintypes.HANDLE)]

    see_mask_nocloseprocess, see_mask_noasync, sw_hide = 0x40, 0x100, 0
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(SHELLEXECUTEINFOW)]
    shell32.ShellExecuteExW.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    sei = SHELLEXECUTEINFOW()
    sei.cbSize = ctypes.sizeof(sei)
    sei.fMask = see_mask_nocloseprocess | see_mask_noasync
    sei.lpVerb = "runas"
    sei.lpFile = exe
    sei.lpParameters = params
    sei.nShow = sw_hide
    if not shell32.ShellExecuteExW(ctypes.byref(sei)):
        err = ctypes.get_last_error()
        if err == ERROR_CANCELLED:
            return None
        raise OSError(err, ctypes.FormatError(err))
    if not sei.hProcess:
        return None
    try:
        kernel32.WaitForSingleObject(sei.hProcess, 0xFFFFFFFF)
        code = wintypes.DWORD(0)
        kernel32.GetExitCodeProcess(sei.hProcess, ctypes.byref(code))
        return int(code.value)
    finally:
        kernel32.CloseHandle(sei.hProcess)


def run_elevated(op: str, dev_id: str, folder: Path | str | None = None) -> dict:
    """``bind``/``unbind`` one device, elevated. Always a result dict: the
    helper's own, or {"ok": False, "cancelled": True} / {"error": ...}; with
    "exit" set when the helper ran."""
    exe = helper_path(folder)
    if not exe.is_file():
        return {"op": op, "id": dev_id, "ok": False, "error": missing_message()}
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "result.json"
        try:
            code = shell_execute_runas(str(exe), f'{op} {dev_id} --out "{out}"')
        except OSError as e:
            return {"op": op, "id": dev_id, "ok": False, "error": str(e)}
        if code is None:
            return {"op": op, "id": dev_id, "ok": False, "cancelled": True}
        try:
            res = parse_result(out.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            res = {"op": op, "id": dev_id, "ok": False,
                   "error": f"{HELPER} gave no result (exit code {code})"}
    res["exit"] = code
    return res


def outcome_text(res: dict) -> str:
    """One line for the editor, from a run_elevated result."""
    if res.get("cancelled"):
        return "Nothing changed: the administrator prompt was cancelled."
    if res.get("refuse") == "in use" or res.get("exit") == IN_USE:
        return ("Nothing changed: the device is in use. Quit the program using it "
                "(QEMU, Camera app) first.")
    if res.get("ok"):
        if res.get("status") == "pending_reboot":
            return "Replug the device to finish."
        if res.get("op") == "bind":
            return ("Already on WinUSB, ready for QEMU." if res.get("already")
                    else "Given to QEMU: the device is on WinUSB.")
        service = res.get("service") or "its own driver"
        return f"Given back to Windows ({service})."
    if res.get("refuse"):
        return f"Refused: {res['refuse']}."
    detail = res.get("detail") or {}
    msg = detail.get("message") if isinstance(detail, dict) else ""
    return f"Failed: {res.get('error') or 'unknown error'}" + (f" ({msg})" if msg else "") + "."
