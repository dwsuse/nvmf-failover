"""Access to the host: commands and sysfs, behind one object.

Everything that touches the machine goes through a System, so the tests can
replace it with a fake one rooted in a temporary directory.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


class CommandError(RuntimeError):
    """A command exited with a non-zero status."""

    def __init__(self, argv: list[str], status: int, output: str) -> None:
        super().__init__(f"{' '.join(argv)}: exit {status}: {output.strip()}")
        self.argv = argv
        self.status = status
        self.output = output


@dataclass
class System:
    """The host: @root is "/" except in the tests."""

    root: Path = Path("/")

    def path(self, path: str) -> Path:
        """@path ("/sys/...") below the root."""
        return self.root / path.lstrip("/")

    def read(self, path: str) -> str:
        """The stripped contents of @path, "" if it can't be read."""
        try:
            return self.path(path).read_text().strip()
        except OSError:
            return ""

    def write(self, path: str, value: str) -> None:
        """Write @value to @path, a sysfs attribute."""
        self.path(path).write_text(value)

    def run(self, argv: list[str], check: bool = True) -> str:
        """Run @argv, return its output (stdout and stderr)."""
        proc = subprocess.run(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
        if check and proc.returncode:
            raise CommandError(argv, proc.returncode, proc.stdout)
        return proc.stdout

    def ok(self, argv: list[str]) -> bool:
        """Whether @argv exits with status 0; its output is discarded."""
        proc = subprocess.run(
            argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False
        )
        return proc.returncode == 0
