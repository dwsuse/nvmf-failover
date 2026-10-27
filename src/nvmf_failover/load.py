"""I/O during a run, with fio: errors and stalls show a failed failover."""

from __future__ import annotations

import json
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class LoadResult:
    errors: int
    io_bytes: int
    max_latency_ms: float
    status: int


class Load:
    """fio reading (or writing) a namespace until stopped."""

    def __init__(
        self, device: str, output: Path, write: bool = False, iodepth: int = 16
    ) -> None:
        self.output = output
        rw = "randrw" if write else "randread"
        self.argv = [
            "fio",
            "--name=failover",
            f"--filename={device}",
            f"--rw={rw}",
            "--bs=4k",
            "--direct=1",
            "--ioengine=libaio",
            f"--iodepth={iodepth}",
            "--time_based",
            "--runtime=7d",
            "--output-format=json",
            f"--output={output}",
            "--continue_on_error=all",
        ]
        self._proc: subprocess.Popen[bytes] | None = None

    def start(self) -> None:
        self._proc = subprocess.Popen(
            self.argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )

    def stop(self) -> LoadResult:
        """Stop fio (it writes its report on SIGINT) and read the report."""
        if self._proc is None:
            raise RuntimeError("load not started")
        self._proc.send_signal(signal.SIGINT)
        status = self._proc.wait(timeout=60)
        return parse_report(self.output.read_text(), status)


def parse_report(text: str, status: int = 0) -> LoadResult:
    """The errors, bytes and worst completion latency of a fio JSON report."""
    # fio may print notes before the JSON document
    data = json.loads(text[text.index("{") :])
    errors = io_bytes = 0
    worst_ns = 0.0
    for job in data.get("jobs", []):
        errors += int(job.get("total_err", job.get("error", 0)))
        for direction in ("read", "write"):
            stats = job.get(direction, {})
            io_bytes += int(stats.get("io_bytes", 0))
            worst_ns = max(worst_ns, float(stats.get("clat_ns", {}).get("max", 0)))
    return LoadResult(errors, io_bytes, worst_ns / 1e6, status)
