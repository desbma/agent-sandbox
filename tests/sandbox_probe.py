#!/usr/bin/python3
"""Run a JSON observation plan inside the sandbox and report results as JSON on stdout."""

import collections.abc
import dataclasses
import enum
import errno
import json
import os
import stat
import sys
from pathlib import Path
from typing import ClassVar


class OpKind(enum.Enum):
    """Observation the probe performs on a path or environment variable."""

    EXISTS = enum.auto()
    LEXISTS = enum.auto()
    ISLINK = enum.auto()
    ISDIR = enum.auto()
    IS_CHAR_DEVICE = enum.auto()
    ACCESS_X = enum.auto()
    READLINK = enum.auto()
    READ = enum.auto()
    MODE = enum.auto()
    LISTDIR = enum.auto()
    WRITE = enum.auto()
    RENAME_OVER = enum.auto()
    ENV = enum.auto()
    CWD = enum.auto()


def errno_name(exc: OSError) -> str:
    """Return the errno name of an OS error, or its message when it carries none."""
    if exc.errno is None:
        return str(exc)
    return errno.errorcode.get(exc.errno, str(exc))


def attempt_write(path: str) -> str:
    """Try to create a file at path, returning "ok" or the errno name."""
    try:
        Path(path).write_text("canary")
    except OSError as exc:
        return errno_name(exc)
    return "ok"


def attempt_rename_over(path: str) -> str:
    """Try to replace path with a temporary file, returning "ok" or the errno name."""
    target = Path(path)
    tmp = target.with_name(f"{target.name}.tmp")
    tmp.write_text("canary")
    try:
        tmp.replace(target)
    except OSError as exc:
        tmp.unlink()
        return errno_name(exc)
    return "ok"


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
        OpKind.MODE: lambda arg: oct(stat.S_IMODE(Path(arg).lstat().st_mode)),
        OpKind.LISTDIR: lambda arg: sorted(p.name for p in Path(arg).iterdir()),
        OpKind.WRITE: attempt_write,
        OpKind.RENAME_OVER: attempt_rename_over,
        OpKind.ENV: os.environ.get,
        OpKind.CWD: lambda _arg: str(Path.cwd()),
    }

    def run(self) -> bool | str | list[str] | None:
        """Perform the observation and return its result."""
        return Op.DISPATCH[self.kind](str(self.arg))


def main() -> None:
    """Read the plan from argv, run each op, print the report, and exit with its code."""
    plan = json.loads(sys.argv[1])
    report: dict[str, object] = {}
    for raw in plan["ops"]:
        op = Op(raw["label"], OpKind[raw["kind"]], raw["arg"])
        report[op.label] = op.run()
    print(json.dumps(report))
    sys.exit(plan["exit_code"])


if __name__ == "__main__":
    main()
