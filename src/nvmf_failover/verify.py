"""Did the host recover? Compare the end of a run with its start."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from .load import LoadResult
from .system import System
from .topology import Identity


@dataclass
class Verdict:
    recovered: bool
    recover_seconds: float
    missing: list[Identity] = field(default_factory=list)
    duplicates: dict[Identity, list[str]] = field(default_factory=dict)
    new_failed_units: list[str] = field(default_factory=list)
    restore_failures: list[str] = field(default_factory=list)
    load: LoadResult | None = None
    failures: list[str] = field(default_factory=list)  # from [expect]

    @property
    def passed(self) -> bool:
        return (
            self.recovered
            and not self.duplicates
            and not self.new_failed_units
            and not self.restore_failures
            and not self.failures
            and (self.load is None or self.load.errors == 0)
        )

    def lines(self) -> list[str]:
        out = [f"result: {'PASS' if self.passed else 'FAIL'}"]
        if self.recovered:
            out.append(f"all paths back after {self.recover_seconds:.0f}s")
        for ident in self.missing:
            out.append(f"not back: {ident.short()}")
        for ident, names in self.duplicates.items():
            out.append(f"duplicate: {ident.short()}: {' '.join(names)}")
        out += [f"new failed unit: {unit}" for unit in self.new_failed_units]
        out += [f"restore failed: {key}" for key in self.restore_failures]
        out += [f"expectation: {text}" for text in self.failures]
        if self.load:
            out.append(
                f"I/O: {self.load.errors} errors, "
                f"{self.load.io_bytes / 2**20:.0f} MiB, worst latency "
                f"{self.load.max_latency_ms:.0f} ms"
            )
        return out


def failed_units(system: System, pattern: str = "nvme-discoverd-*") -> set[str]:
    out = system.run(
        [
            "systemctl",
            "list-units",
            pattern,
            "--state=failed",
            "--all",
            "--no-legend",
            "--plain",
        ],
        check=False,
    )
    return {line.split()[0] for line in out.splitlines() if line.strip()}


def wait_recovered(
    baseline: dict[Identity, list[str]],
    current: Callable[[], dict[Identity, list[str]]],
    timeout: float,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[bool, float, dict[Identity, list[str]]]:
    """Wait until every baseline identity is live again."""
    start = clock()
    while True:
        now = current()
        if all(now.get(ident) for ident in baseline):
            return True, clock() - start, now
        if clock() - start >= timeout:
            return False, clock() - start, now
        sleep(2)


def verdict(
    baseline: dict[Identity, list[str]],
    recovered: bool,
    seconds: float,
    now: dict[Identity, list[str]],
    failed_before: set[str],
    failed_after: set[str],
    restore_failures: list[str],
    load: LoadResult | None,
) -> Verdict:
    return Verdict(
        recovered=recovered,
        recover_seconds=seconds,
        missing=sorted(ident for ident in baseline if not now.get(ident)),
        duplicates={ident: names for ident, names in now.items() if len(names) > 1},
        new_failed_units=sorted(failed_after - failed_before),
        restore_failures=restore_failures,
        load=load,
    )
