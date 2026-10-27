"""I/O during a run, with fio: errors and stalls show a failed failover.

One fio process runs a job per device: "read" jobs read at random,
"verify" jobs write at random and read every block back against its
checksum, so data corruption shows as an error too (and destroys the
data on the device).
"""

from __future__ import annotations

import json
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path

MODES = ("read", "verify")


@dataclass(frozen=True)
class LoadResult:
    errors: int
    io_bytes: int
    max_latency_ms: float
    status: int


@dataclass(frozen=True)
class Job:
    device: str
    mode: str = "read"


def fio_argv(jobs: list[Job], output: Path, iodepth: int = 16) -> list[str]:
    """The fio command line: global options, then one section per job."""
    argv = [
        "fio",
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
    for i, job in enumerate(jobs):
        if job.mode not in MODES:
            raise ValueError(f"{job.mode}: not one of {', '.join(MODES)}")
        argv += [f"--name=job{i}", f"--filename={job.device}"]
        if job.mode == "read":
            argv.append("--rw=randread")
        else:
            argv += [
                "--rw=randwrite",
                "--verify=crc32c",
                "--verify_backlog=4096",
                "--verify_fatal=0",
                "--do_verify=1",
            ]
    return argv


class Load:
    """fio on the jobs' devices until stopped."""

    def __init__(self, jobs: list[Job], output: Path, iodepth: int = 16) -> None:
        self.jobs, self.output = jobs, output
        self.argv = fio_argv(jobs, output, iodepth)
        self._proc: subprocess.Popen[bytes] | None = None

    @property
    def devices(self) -> list[str]:
        return [f"{j.device} ({j.mode})" for j in self.jobs]

    def start(self) -> None:
        self._proc = subprocess.Popen(
            self.argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )

    def stop(self) -> LoadResult:
        """Stop fio (it writes its report on SIGINT) and read the report."""
        if self._proc is None:
            raise RuntimeError("load not started")
        self._proc.send_signal(signal.SIGINT)
        status = self._proc.wait(timeout=120)
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
