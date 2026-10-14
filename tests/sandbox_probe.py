#!/usr/bin/python3
"""Run a JSON observation plan inside the sandbox and report results as JSON on stdout."""

import collections.abc
import dataclasses
import enum
import errno
import json
import os
import socket
import stat
import sys
from pathlib import Path
from typing import ClassVar

NETWORK_TIMEOUT_SECONDS = 10.0
SERVER_GREETING = b"sandbox-probe"


class OpKind(enum.Enum):
    """Observation the probe performs on the sandbox."""

    EXISTS = enum.auto()
    LEXISTS = enum.auto()
    ISLINK = enum.auto()
    ISDIR = enum.auto()
    IS_CHAR_DEVICE = enum.auto()
    ACCESS_X = enum.auto()
    READLINK = enum.auto()
    READ = enum.auto()
    READ_HEX = enum.auto()
    MODE = enum.auto()
    LISTDIR = enum.auto()
    WRITE = enum.auto()
    RENAME_OVER = enum.auto()
    ENV = enum.auto()
    CWD = enum.auto()
    IDS = enum.auto()
    TCP_CONNECT = enum.auto()
    UDP_SEND = enum.auto()
    UNIX_CONNECT = enum.auto()
    SERVE = enum.auto()


def errno_name(exc: OSError) -> str:
    """Return the errno name of an OS error, or its message when it carries none."""
    if exc.errno is None:
        return str(exc)
    return errno.errorcode.get(exc.errno, str(exc))


def attempt(
    operation: collections.abc.Callable[[str], object],
) -> collections.abc.Callable[[str], str]:
    """Wrap an operation to return "ok" once it completes, or the errno name of the OS error it raises."""

    def run(arg: str) -> str:
        """Perform the operation and report its outcome."""
        try:
            operation(arg)
        except OSError as exc:
            return errno_name(exc)
        return "ok"

    return run


def rename_over(path: str) -> None:
    """Replace path with a temporary file."""
    target = Path(path)
    tmp = target.with_name(f"{target.name}.tmp")
    tmp.write_text("canary")
    try:
        tmp.replace(target)
    except OSError:
        tmp.unlink()
        raise


def split_address(address: str) -> tuple[str, int]:
    """Split a host:port string, whose host may be a bare IPv6 address."""
    host, _, port = address.rpartition(":")
    return host, int(port)


def connect(address: str) -> None:
    """Open and close a TCP connection to host:port."""
    socket.create_connection(
        split_address(address), timeout=NETWORK_TIMEOUT_SECONDS
    ).close()


def send_datagram(address: str) -> None:
    """Send a UDP datagram to an IPv4 host:port."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.sendto(b"probe", split_address(address))


def unix_connect(path: str) -> None:
    """Connect to a unix stream socket, abstract if path starts with a NUL."""
    with socket.socket(socket.AF_UNIX) as sock:
        sock.connect(path)


def serve(address: str) -> None:
    """Listen on host:port and greet the first client."""
    with socket.create_server(split_address(address)) as server:
        server.settimeout(NETWORK_TIMEOUT_SECONDS)
        connection, _ = server.accept()
        with connection:
            connection.sendall(SERVER_GREETING)


@dataclasses.dataclass(frozen=True, slots=True)
class Op:
    """A labelled observation to run inside the sandbox."""

    label: str
    kind: OpKind
    arg: str | Path = ""

    DISPATCH: ClassVar[
        dict[OpKind, collections.abc.Callable[[str], bool | str | list[str] | None]]
    ] = {
        OpKind.EXISTS: lambda arg: Path(arg).exists(),
        OpKind.LEXISTS: os.path.lexists,
        OpKind.ISLINK: lambda arg: Path(arg).is_symlink(),
        OpKind.ISDIR: lambda arg: Path(arg).is_dir(),
        OpKind.IS_CHAR_DEVICE: lambda arg: stat.S_ISCHR(Path(arg).stat().st_mode),
        OpKind.ACCESS_X: lambda arg: os.access(arg, os.X_OK),
        OpKind.READLINK: lambda arg: str(Path(arg).readlink()),
        OpKind.READ: lambda arg: Path(arg).read_text(),
        OpKind.READ_HEX: lambda arg: Path(arg).read_bytes().hex(),
        OpKind.MODE: lambda arg: oct(stat.S_IMODE(Path(arg).lstat().st_mode)),
        OpKind.LISTDIR: lambda arg: sorted(p.name for p in Path(arg).iterdir()),
        OpKind.WRITE: attempt(lambda arg: Path(arg).write_text("canary")),
        OpKind.RENAME_OVER: attempt(rename_over),
        OpKind.ENV: os.environ.get,
        OpKind.CWD: lambda _arg: str(Path.cwd()),
        OpKind.IDS: lambda _arg: [str(os.getuid()), str(os.getgid())],
        OpKind.TCP_CONNECT: attempt(connect),
        OpKind.UDP_SEND: attempt(send_datagram),
        OpKind.UNIX_CONNECT: attempt(unix_connect),
        OpKind.SERVE: attempt(serve),
    }

    def run(self) -> bool | str | list[str] | None:
        """Perform the observation and return its result."""
        return Op.DISPATCH[self.kind](str(self.arg))


def main() -> None:
    """Run the plan in the last argument, print the report, and exit with its code."""
    plan = json.loads(sys.argv[-1])
    report: dict[str, object] = {}
    for raw in plan["ops"]:
        op = Op(raw["label"], OpKind[raw["kind"]], raw["arg"])
        report[op.label] = op.run()
    print(json.dumps(report))
    sys.exit(plan["exit_code"])


if __name__ == "__main__":
    main()
