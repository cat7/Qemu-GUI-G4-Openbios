"""Where everything is: the folder the program is installed in, the emulator
beside it, and the Machines folder beside it.

There is no configurable QEMU folder and no configurable machine library.
This program is a companion to one copy of ``qemu-system-ppc``: it lives in
the same folder as that program, and keeps its machines in a ``Machines``
folder next to itself.

No Tk in here. Platform strings follow ``sys.platform``: ``darwin``,
``win32``, anything else is treated as Linux/POSIX.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
from dataclasses import dataclass, asdict
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath

APP_NAME = "Qemu-system-ppc Mac99 openbios GUI"
HOST_PLATFORM = sys.platform  # "darwin" | "win32" | "linux"

MACHINES_DIR_NAME = "Machines"
SETTINGS_FILE_NAME = "settings.json"

# The folder holding the entry script, used when running from source.
SOURCE_ROOT = Path(__file__).resolve().parent.parent

# Set only by the tests and by tools/ scripts, which must not touch the real
# install. There is deliberately no user-facing setting, no command-line
# option and no environment variable for this.
_install_dir_override: Path | None = None


def use_install_dir(folder: Path | str | None) -> None:
    """Testing hook: pretend the program is installed in *folder*."""
    global _install_dir_override
    _install_dir_override = Path(folder).expanduser().resolve() if folder else None


def resolve_install_dir(*, frozen: bool, executable: str, source_root: str) -> Path:
    """The folder a person sees this program in. Three cases:

    * running from source -- the folder holding the entry script;
    * frozen inside a macOS application bundle -- ``sys.executable`` is
      ``.../Qemu-system-ppc GUI.app/Contents/MacOS/Qemu-system-ppc GUI``, several levels below the
      folder the bundle itself sits in, so walk up out of the ``.app``;
    * frozen as a plain executable (Windows, Linux) -- the folder holding
      the executable.

    Pure: takes the three inputs, touches no globals and no filesystem
    beyond resolving the path, so all three cases can be tested.
    """
    if not frozen:
        return Path(source_root).resolve()
    exe = Path(executable).resolve()
    for parent in exe.parents:
        if parent.suffix == ".app":
            return parent.parent
    return exe.parent


def install_dir() -> Path:
    """The one answer to "which folder am I installed in". Everything else --
    finding the emulator, finding Machines -- goes through this."""
    if _install_dir_override is not None:
        return _install_dir_override
    return resolve_install_dir(frozen=bool(getattr(sys, "frozen", False)),
                               executable=sys.executable, source_root=str(SOURCE_ROOT))


def machines_dir() -> Path:
    """Where the machines are kept: a folder next to the program. Never
    inside an application bundle, which must be treated as read-only."""
    return install_dir() / MACHINES_DIR_NAME


def settings_path() -> Path:
    """The GUI's own scrap of state (which machine was selected last)."""
    return machines_dir() / SETTINGS_FILE_NAME


def is_windows(platform: str = HOST_PLATFORM) -> bool:
    return platform.startswith("win")


def qemu_binary_name(platform: str = HOST_PLATFORM) -> str:
    return "qemu-system-ppc.exe" if is_windows(platform) else "qemu-system-ppc"


def qemu_img_name(platform: str = HOST_PLATFORM) -> str:
    return "qemu-img.exe" if is_windows(platform) else "qemu-img"


def launcher_name(platform: str = HOST_PLATFORM) -> str:
    return "run.bat" if is_windows(platform) else "run.command"


def qemu_binary(platform: str = HOST_PLATFORM) -> Path:
    return install_dir() / qemu_binary_name(platform)


def qemu_img_binary(platform: str = HOST_PLATFORM) -> Path:
    return install_dir() / qemu_img_name(platform)


def has_qemu(folder: Path | str | None, platform: str = HOST_PLATFORM) -> bool:
    if not folder:
        return False
    return (Path(folder) / qemu_binary_name(platform)).is_file()


def pure_path(p: str, platform: str = HOST_PLATFORM) -> PurePath:
    """A path object for *platform* without touching the filesystem."""
    return PureWindowsPath(p) if is_windows(platform) else PurePosixPath(p)


def join_path(base: str, name: str, platform: str = HOST_PLATFORM) -> str:
    """``base/name`` unless *name* is already absolute; rendered for *platform*."""
    n = pure_path(name, platform)
    if n.is_absolute():
        return str(n)
    return str(pure_path(base, platform) / n)


# ------------------------------------------------------- display defaults
#
# One home for the g3beige and mac99 GUIs both: neither machine record cares
# which host it is edited on, but the choices offered while editing, and the
# choice a brand new record starts with, do.

DISPLAYS = {"darwin": ("cocoa", "sdl"), "win32": ("sdl", "gtk"), "linux": ("sdl", "gtk")}


def default_display(platform: str = HOST_PLATFORM) -> str:
    """The first choice offered on this computer, which is what a new machine
    starts with: ``cocoa`` on a Mac, ``sdl`` anywhere else."""
    return DISPLAYS.get("win32" if is_windows(platform) else platform, ("sdl",))[0]


# --------------------------------------------------------- audio defaults

AUDIO_DEFAULT = {"darwin": "coreaudio", "win32": "dsound"}


def resolve_audio(audio: str, platform: str = HOST_PLATFORM) -> str:
    """"default" resolves to this host's native backend (coreaudio on a Mac,
    dsound on Windows); anything else (sdl, none) passes through unchanged."""
    if audio != "default":
        return audio
    return AUDIO_DEFAULT.get(platform, "sdl")


# ------------------------------------------------------- network backends

NETWORK_MODES = ("user", "none", "vmnet-bridged", "vmnet-shared", "vmnet-host", "tap")
# platform each mode is meant for (None = all); "darwin" | "win32"
NETWORK_MODE_PLATFORM = {"user": None, "none": None, "vmnet-bridged": "darwin",
                         "vmnet-shared": "darwin", "vmnet-host": "darwin", "tap": "win32"}
NETWORK_MODES_WITH_IFNAME = ("vmnet-bridged", "tap")
NETWORK_MODE_LABELS = {"user": "default (slirp)"}


def network_mode_label(mode: str) -> str:
    return NETWORK_MODE_LABELS.get(mode, mode)


def network_mode_by_label(label: str) -> str:
    for mode, shown in NETWORK_MODE_LABELS.items():
        if shown == label:
            return mode
    return label


def network_modes_for_host(platform: str = HOST_PLATFORM, current: str | None = None) -> list[str]:
    """Modes the editor offers on *platform*, plus whatever the record holds."""
    host = "win32" if is_windows(platform) else platform
    out = [m for m in NETWORK_MODES if NETWORK_MODE_PLATFORM[m] in (None, host)]
    if current and current not in out:
        out.append(current)
    return out


def network_labels_for_host(platform: str = HOST_PLATFORM, current: str | None = None) -> list[str]:
    return [network_mode_label(m) for m in network_modes_for_host(platform, current)]


def default_ifname(mode: str, platform: str = HOST_PLATFORM) -> str:
    if mode == "vmnet-bridged" and platform == "darwin":
        return "en0"
    return ""


def ifname_label(platform: str = HOST_PLATFORM) -> str:
    return "Vmnet host interface:" if platform == "darwin" else "Tap device name:"


AUDIO_BACKEND_LABELS = {"coreaudio": "CoreAudio", "dsound": "DirectSound", "sdl": "Default (sdl)"}


def default_audio_label(platform: str = HOST_PLATFORM) -> str:
    """What the "default" sound choice is called on this host."""
    backend = resolve_audio("default", platform)
    return AUDIO_BACKEND_LABELS.get(backend, backend)


def sudo_applies(needs_sudo: bool, platform: str = HOST_PLATFORM) -> bool:
    """vmnet-* launchers run the binary under sudo (macOS only; never in a
    .bat)."""
    return needs_sudo and not is_windows(platform)


# ---------------------------------------------------------- launcher text
#
# The pure string-handling shared by every ``*_command.py`` module: QEMU
# option escaping, extra-argument tokenising, grouping an argv into one
# option per output line, and the two launcher renderings (shell / .bat).
# What differs between machine families is only the header comment and which
# files a sudo run chowns back at the end -- both are passed in.

def qopt(value: str) -> str:
    """Escape a value for QEMU's key=value option parser (comma -> ,,)."""
    return str(value).replace(",", ",,")


def split_extra_args(text: str, platform: str = HOST_PLATFORM) -> list[str]:
    """Tokenise the free-text extra arguments line. On Windows backslashes are
    path separators, not escapes, so escape processing is disabled there."""
    text = (text or "").strip()
    if not text:
        return []
    lex = shlex.shlex(text, posix=True)
    lex.whitespace_split = True
    lex.commentchars = ""
    if is_windows(platform):
        lex.escape = ""
    return list(lex)


def group_options(argv: list[str]) -> list[list[str]]:
    """Group [binary, -opt, value, -opt, ...] into one list per option so
    each option lands on its own line in the rendered launcher."""
    groups: list[list[str]] = []
    for tok in argv[1:]:
        if tok.startswith("-") or not groups:
            groups.append([tok])
        else:
            groups[-1].append(tok)
    return groups


def quote_extra(token: str) -> str:
    """shlex.quote, plus single quotes around any token holding '='."""
    if "=" in token:
        return "'" + token.replace("'", "'\"'\"'") + "'"
    return shlex.quote(token)


def render_groups(argv: list[str], quote, quote_extra, extra: int) -> list[str]:
    """One line per option; the last *extra* tokens of argv are quoted with
    *quote_extra* instead of *quote*."""
    plain = len(argv) - extra
    out: list[str] = []
    n = 1
    for g in group_options(argv):
        out.append(" ".join((quote_extra if n + j >= plain else quote)(t) for j, t in enumerate(g)))
        n += len(g)
    return out


# Ask for the password ONCE. Without the keep-alive, sudo's ticket expires
# during any run longer than its timeout (5 minutes by default) and the chown
# below prompts a second time, in the middle of the guest's own output.
SUDO_KEEPALIVE = ('sudo -v\n'
                  'while true; do sudo -n true; sleep 60; '
                  'kill -0 "$$" 2>/dev/null || exit; done &\n'
                  'SUDO_KEEPALIVE_PID=$!')


def sudo_chown_line(owned_files: tuple[str, ...]) -> str:
    return ('kill "$SUDO_KEEPALIVE_PID" 2>/dev/null\n'
           f'sudo -n chown "${{SUDO_USER:-$(id -un)}}" {" ".join(owned_files)} 2>/dev/null')


def render_shell(argv: list[str], header_note: str, owned_files: tuple[str, ...] = (),
                 sudo: bool = False, extra: int = 0) -> str:
    lines = ["#!/bin/bash",
             f"# {header_note}",
             'cd "$(dirname "$0")"',
             ""]
    if sudo:
        lines += [SUDO_KEEPALIVE, ""]
    lines.append(("sudo " if sudo else "") + shlex.quote(argv[0]) + " \\")
    body = render_groups(argv, shlex.quote, quote_extra, extra)
    for i, ln in enumerate(body):
        cont = " \\" if i < len(body) - 1 else ""
        lines.append(ln + cont)
    if sudo:
        lines += ["", sudo_chown_line(owned_files)]
    return "\n".join(lines) + "\n"


def bat_quote(token: str, extra: bool = False) -> str:
    """cmd.exe quoting: whole-token double quotes when the token contains a
    space or a comma (contract rule), or '=' for an extra argument; '%' must
    be doubled in a .bat file."""
    t = token.replace("%", "%%")
    if (" " in t or "," in t or (extra and "=" in t)) and not (t.startswith('"') and t.endswith('"')):
        return f'"{t}"'
    return t


def bat_quote_extra(token: str) -> str:
    return bat_quote(token, extra=True)


def render_bat(argv: list[str], header_note: str, extra: int = 0) -> str:
    lines = ["@echo off",
             f"rem {header_note}",
             'cd /d "%~dp0"',
             "",
             bat_quote(argv[0]) + " ^"]
    body = render_groups(argv, bat_quote, bat_quote_extra, extra)
    for i, ln in enumerate(body):
        cont = " ^" if i < len(body) - 1 else ""
        lines.append(ln + cont)
    return "\r\n".join(lines) + "\r\n"


def render_launcher(argv: list[str], header_note: str, platform: str = HOST_PLATFORM,
                    sudo: bool = False, owned_files: tuple[str, ...] = (),
                    extra: int = 0) -> str:
    """The .bat never gets sudo; *sudo* only affects the shell rendering."""
    if is_windows(platform):
        return render_bat(argv, header_note, extra)
    return render_shell(argv, header_note, owned_files, sudo, extra)


def browse_start_dir(current: str | None, fallback: Path | str | None = None) -> Path:
    """Folder a file dialog should open in: the folder of whatever is already
    filled in, else *fallback* (normally this machine's own folder), else the
    home folder. No guessed image libraries: nothing is ever pre-filled."""
    for cand in (current, fallback):
        if not cand:
            continue
        p = Path(str(cand)).expanduser()
        d = p if p.is_dir() else p.parent
        if d.is_dir():
            return d
    return Path.home()


# ------------------------------------------------------------ startup check

MISSING_QEMU_MESSAGE = """\
To run this GUI, put it in a folder containing a "{binary}" application.\
"""

UNWRITABLE_MESSAGE = """The "{name}" folder next to this program could not be created: {reason}"""


def startup_problem(platform: str = HOST_PLATFORM) -> str | None:
    """A reason the program cannot run where it is, or None."""
    folder = install_dir()
    if not has_qemu(folder, platform):
        return MISSING_QEMU_MESSAGE.format(binary=qemu_binary_name(platform), folder=f"    {folder}")
    machines = machines_dir()
    try:
        machines.mkdir(parents=True, exist_ok=True)
        probe = machines / ".write-test"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
    except OSError as e:
        return UNWRITABLE_MESSAGE.format(name=MACHINES_DIR_NAME, reason=e)
    return None


# ---------------------------------------------------------------- settings

@dataclass
class Settings:
    """The only thing worth remembering between runs: the selected machine."""

    last_machine: str = ""

    @classmethod
    def load(cls, path: Path | None = None) -> "Settings":
        path = path or settings_path()
        s = cls()
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return s
        v = data.get("last_machine") if isinstance(data, dict) else None
        if isinstance(v, str):
            s.last_machine = v
        return s

    def save(self, path: Path | None = None) -> None:
        path = Path(path or settings_path())
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2) + "\n", encoding="utf-8")
