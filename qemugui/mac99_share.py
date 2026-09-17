"""The shared folder: an FTP server the emulated Mac reaches at 10.0.2.2.

No Tk in here. Under user (slirp) networking the guest's connection to
10.0.2.2 arrives on the host's loopback, and slirp has no FTP helper, so
the server runs passive-only and advertises 10.0.2.2 to loopback clients.
Under vmnet the guest connects to the host's own address instead.

Ported from :mod:`qemugui.share` (the g3beige GUI's own shared folder,
branch ``g3-share``); mechanically identical except for one thing that
does not apply here: that module picks Mac Roman vs UTF-8 filename
encoding from the g3beige machine's ``system`` profile (a "which Mac OS
era" choice this machine has none of -- see mac99_model.py's module
docstring, "no governor and no system-type profile"). mac99 guests always
speak UTF-8 filenames.
"""

from __future__ import annotations

import logging
import socket
import subprocess
import threading
import warnings
from pathlib import Path

from . import paths
from .mac99_model import Machine, Share

GUEST_HOST_ADDR = "10.0.2.2"
PORTS = (21, 2121)
PASSIVE_PORTS = range(50000, 50016)
FULL_PERMS = "elradfmwMT"
ANONYMOUS_NAMES = ("anonymous", "ftp")
NO_ACTIVE_MODE = "502 Active mode is not available here; use passive mode."
ACTIVE_MODE_LOG = ("client asked for active mode (%s); only passive mode works "
                   "through the emulator's network")
HOST_IP_PLACEHOLDER = "<host IP>"
# what vmnet names its bridges on macOS
VMNET_BRIDGE = {"vmnet-shared": "bridge100", "vmnet-host": "bridge101"}
ENCODING = "utf8"


class ShareError(Exception):
    pass


def share_url(host: str, port: int) -> str:
    return f"ftp://{host}/" if port == 21 else f"ftp://{host}:{port}/"


def interface_address(ifname: str, platform: str = paths.HOST_PLATFORM) -> str | None:
    """The IPv4 address of one host interface, or None."""
    if paths.is_windows(platform) or not ifname:
        return None
    try:
        out = subprocess.run(["ifconfig", ifname], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "inet":
            return parts[1]
    return None


def guest_side_host(network_mode: str, ifname: str = "") -> str:
    """The address the guest connects to for *network_mode*."""
    if network_mode == "user":
        return GUEST_HOST_ADDR
    if network_mode == "vmnet-bridged":
        return interface_address(ifname) or HOST_IP_PLACEHOLDER
    if network_mode in VMNET_BRIDGE:
        return interface_address(VMNET_BRIDGE[network_mode]) or HOST_IP_PLACEHOLDER
    return HOST_IP_PLACEHOLDER


def _log(log_path: Path | None) -> logging.Logger:
    """The library's own logger: the whole dialog into last-run.log when
    there is one, otherwise only its errors, on stderr."""
    logger = logging.getLogger("pyftpdlib")
    logger.propagate = False
    for h in list(logger.handlers):
        logger.removeHandler(h)
        h.close()
    handler: logging.Handler
    if log_path and Path(log_path).is_file():
        handler = logging.FileHandler(log_path, encoding="utf-8")
        logger.setLevel(logging.DEBUG)
    else:
        handler = logging.StreamHandler()
        logger.setLevel(logging.WARNING)
    handler.setFormatter(logging.Formatter("[share] %(message)s"))
    logger.addHandler(handler)
    return logger


class ShareServer:
    """One running server; ``stop()`` ends it."""

    def __init__(self, server, bind_host: str, port: int, thread: threading.Thread,
                 stop_event: threading.Event, encoding: str):
        self._server = server
        self.bind_host = bind_host
        self.port = port
        self.encoding = encoding
        self._thread = thread
        self._stop = stop_event

    @property
    def address(self) -> tuple[str, int]:
        return self.bind_host, self.port

    def url_for(self, network_mode: str, ifname: str = "") -> str:
        return share_url(guest_side_host(network_mode, ifname), self.port)

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._thread.join(timeout)


def _bind_host(share: Share) -> str:
    return "0.0.0.0" if share.scope == "all-interfaces" else "127.0.0.1"


def check_share(share: Share) -> str | None:
    """A reason the server cannot start, or None."""
    if not share.enabled:
        return "No shared folder chosen."
    if not Path(share.folder).expanduser().is_dir():
        return "The shared folder is not a folder that exists."
    if share.scope == "all-interfaces" and not share.password:
        return "Sharing on all interfaces needs a password."
    if share.password and not share.user.strip():
        return "The shared folder has no user name."
    return None


def start_share(m: Machine, log_path: Path | None = None, ports=PORTS,
                bind_host: str | None = None) -> ShareServer:
    """Serve ``m.share.folder``; raises ShareError when it cannot."""
    share = m.share
    why = check_share(share)
    if why:
        raise ShareError(why)
    from pyftpdlib.authorizers import DummyAuthorizer
    from pyftpdlib.handlers import FTPHandler
    from pyftpdlib.ioloop import IOLoop
    from pyftpdlib.servers import FTPServer

    folder = str(Path(share.folder).expanduser())

    class Authorizer(DummyAuthorizer):
        aliases: tuple = ()

        def validate_authentication(self, username, password, handler):
            if username in self.aliases:
                return
            super().validate_authentication(username, password, handler)

    authorizer = Authorizer()
    if share.password:
        authorizer.add_user(share.user.strip(), share.password, folder, perm=FULL_PERMS)
    else:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            authorizer.add_anonymous(folder, perm=FULL_PERMS)
        for name in ANONYMOUS_NAMES[1:] + (share.user.strip(),):
            if name and not authorizer.has_user(name):
                authorizer.add_user(name, "", folder, perm=FULL_PERMS)
        authorizer.aliases = tuple(authorizer.user_table)

    class Handler(FTPHandler):
        def ftp_PORT(self, line):
            self.log(ACTIVE_MODE_LOG % "PORT")
            self.respond(NO_ACTIVE_MODE)

        def ftp_EPRT(self, line):
            self.log(ACTIVE_MODE_LOG % "EPRT")
            self.respond(NO_ACTIVE_MODE)

    Handler.authorizer = authorizer
    Handler.masquerade_address_map = {"127.0.0.1": GUEST_HOST_ADDR}
    Handler.passive_ports = PASSIVE_PORTS
    Handler.encoding = ENCODING
    Handler.banner = "Qemu-system-ppc Mac99 openbios GUI shared folder"

    host = bind_host if bind_host is not None else _bind_host(share)
    server = None
    last: OSError | None = None
    for port in ports:
        try:
            server = FTPServer((host, port), Handler, ioloop=IOLoop())
            break
        except OSError as e:
            last = e
    if server is None:
        raise ShareError(f"No port for the shared folder: {last}")
    port = server.address[1]
    logger = _log(log_path)
    stop_event = threading.Event()

    def run():
        try:
            while not stop_event.is_set():
                server.ioloop.loop(0.5, False)
        except Exception as e:      # noqa: BLE001
            logger.error("server stopped: %s", e)
        finally:
            try:
                server.close_all()
            except Exception as e:      # noqa: BLE001
                logger.error("close: %s", e)

    thread = threading.Thread(target=run, name=f"share-{m.name}", daemon=True)
    thread.start()
    logger.info("serving %s on %s:%d as %s", folder, host, port,
                share.user if share.password else "anonymous")
    return ShareServer(server, host, port, thread, stop_event, ENCODING)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
