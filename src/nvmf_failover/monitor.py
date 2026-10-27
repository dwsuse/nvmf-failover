"""What happens during a run: controller and path changes, the daemon's log."""

from __future__ import annotations

import json
import re
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import IO

from .system import System
from .topology import Controller, Identity, controllers

# Lines of the discoverd journal that say nothing about a failover
NOISE = (re.compile(r"other-interface self entry"),)


@dataclass(frozen=True)
class Event:
    time: float
    source: str  # "topology", "path", "journal", "step"
    text: str

    def line(self, start: float) -> str:
        return f"{self.time - start:9.1f}s {self.source:8} {self.text}"


def snapshot(ctrls: list[Controller]) -> dict[str, str]:
    """State per controller and ANA state per path, keyed for diffing."""
    out = {}
    for ctrl in ctrls:
        if not ctrl.identified:
            # a controller still connecting: its identity is unknown yet
            out[f"ctrl {ctrl.name} (not identified yet)"] = ctrl.state
            continue
        ident = ctrl.identity.short()
        out[f"ctrl {ident} [{ctrl.cntrltype}]"] = f"{ctrl.name}:{ctrl.state}"
        # nvme<S>c<C>n<N>: the controller number changes on a reconnect,
        # the identity and the namespace ID don't
        for path, ana in ctrl.paths.items():
            out[f"path {ident} nsid {path.rsplit('n', 1)[-1]}"] = ana or "-"
    return out


def diff(old: dict[str, str], new: dict[str, str]) -> list[str]:
    """One line per key whose value changed, appeared or disappeared."""
    lines = []
    for key in sorted(old.keys() | new.keys()):
        before, after = old.get(key), new.get(key)
        if before != after:
            lines.append(f"{key}: {before or 'gone'} -> {after or 'gone'}")
    return lines


class Monitor:
    """Polls the topology and follows the journal until stopped."""

    def __init__(
        self,
        system: System,
        interval: float = 1.0,
        journal_units: tuple[str, ...] = ("nvme-discoverd*",),
        sink: Callable[[Event], None] | None = None,
    ) -> None:
        self.system, self.interval = system, interval
        self.journal_units = journal_units
        self.events: list[Event] = []
        self._sink = sink
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._journal: subprocess.Popen[str] | None = None

    def add(self, source: str, text: str) -> None:
        event = Event(time.time(), source, text)
        with self._lock:
            self.events.append(event)
        if self._sink:
            self._sink(event)

    def start(self) -> None:
        self._threads.append(threading.Thread(target=self._poll, daemon=True))
        if self.journal_units:
            argv = ["journalctl", "-f", "-n", "0", "-o", "json"]
            for unit in self.journal_units:
                argv += ["-u", unit]
            self._journal = subprocess.Popen(
                argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True
            )
            self._threads.append(
                threading.Thread(
                    target=self._follow, args=(self._journal.stdout,), daemon=True
                )
            )
        for thread in self._threads:
            thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._journal:
            self._journal.terminate()
            self._journal.wait(timeout=5)
        for thread in self._threads:
            thread.join(timeout=5)

    def _poll(self) -> None:
        last = snapshot(controllers(self.system))
        while not self._stop.wait(self.interval):
            now = snapshot(controllers(self.system))
            for line in diff(last, now):
                self.add("path" if line.startswith("path") else "topology", line)
            last = now

    def _follow(self, stream: IO[str] | None) -> None:
        if stream is None:
            return
        for raw in stream:
            try:
                entry = json.loads(raw)
            except ValueError:
                continue
            text = str(entry.get("MESSAGE", ""))
            if any(pattern.search(text) for pattern in NOISE):
                continue
            unit = entry.get("_SYSTEMD_UNIT") or entry.get("UNIT") or ""
            self.add("journal", f"{unit}: {text}" if unit else text)

    def dump(self, out: IO[str]) -> None:
        """The events as JSON lines."""
        with self._lock:
            for event in self.events:
                out.write(json.dumps(asdict(event)) + "\n")


def live_by_identity(system: System) -> dict[Identity, list[str]]:
    """The live controllers of each identity."""
    out: dict[Identity, list[str]] = {}
    for ctrl in controllers(system):
        if ctrl.state == "live" and ctrl.identified:
            out.setdefault(ctrl.identity, []).append(ctrl.name)
    return out
