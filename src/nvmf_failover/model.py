"""The paths of the host as chains: host port, switch port, target port,
array node, subsystem.

A scenario selects what to break by these attributes, never by an
interface name; the resolver turns that into faults.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from .brocade import BrocadeError, normalize_wwpn, parse_nsshow
from .brocade import Shell as SwitchShell
from .config import BrocadeConfig
from .discover import Inventory, Lif
from .system import System
from .topology import Controller, controllers


@dataclass(frozen=True)
class SwitchPort:
    switch: str
    index: int
    shared: bool  # other WWPNs are logged in through it too


@dataclass(frozen=True)
class HostPort:
    name: str  # scsi host, e.g. host3
    wwpn: str  # aa:bb:... form
    driver: str
    switch_port: SwitchPort | None = None


@dataclass(frozen=True)
class TargetPort:
    array: str  # "netapp" or "powerstore"
    group: str  # ONTAP vserver, PowerStore cluster
    name: str  # LIF or FC port name
    address: str  # WWPN (aa:bb:...) or IP address
    node: str
    switch_port: SwitchPort | None = None

    @property
    def key(self) -> str:
        return f"{self.group}/{self.name}"


@dataclass(frozen=True)
class Path:
    ctrl: Controller
    host_port: HostPort | None
    target_port: TargetPort | None

    @property
    def subsysnqn(self) -> str:
        return self.ctrl.identity.subsysnqn


@dataclass
class Model:
    paths: list[Path] = field(default_factory=list)
    host_ports: list[HostPort] = field(default_factory=list)
    target_ports: list[TargetPort] = field(default_factory=list)
    # subsystem NQN -> name on the array (ONTAP subsystem, PowerStore host)
    subsystem_names: dict[str, str] = field(default_factory=dict)

    def select(
        self, subsystem: str = "*", array: str = "*", node: str = "*"
    ) -> list[Path]:
        """The I/O paths matching the globs (subsystem: NQN or array name)."""
        out = []
        for path in self.paths:
            nqn = path.subsysnqn
            names = {nqn, self.subsystem_names.get(nqn, "")}
            tp = path.target_port
            if not any(fnmatch.fnmatch(n, subsystem) for n in names if n):
                continue
            if array != "*" and (tp is None or not fnmatch.fnmatch(tp.array, array)):
                continue
            if node != "*" and (tp is None or not fnmatch.fnmatch(tp.node, node)):
                continue
            out.append(path)
        return out


def _wwpn(addr: str) -> str:
    """The WWPN of an FC address (nn-0x..:pn-0x..), "" for anything else."""
    if "pn-0x" not in addr:
        return ""
    try:
        return normalize_wwpn(addr.rsplit("pn-", 1)[-1])
    except BrocadeError:
        return ""


def host_ports(system: System) -> list[HostPort]:
    out = []
    base = system.path("/sys/class/fc_host")
    if not base.is_dir():
        return []
    for entry in sorted(base.iterdir()):
        try:
            wwpn = normalize_wwpn(
                system.read(f"/sys/class/fc_host/{entry.name}/port_name")
            )
        except BrocadeError:
            continue
        driver = system.read(f"/sys/class/scsi_host/{entry.name}/proc_name")
        out.append(HostPort(entry.name, wwpn, driver if driver != "(null)" else ""))
    return out


def _array(lif: Lif) -> str:
    return "powerstore" if lif.vserver.startswith("powerstore:") else "netapp"


def target_ports(inv: Inventory) -> list[TargetPort]:
    out = []
    for lif in inv.lifs:
        address = lif.address
        if lif.protocol == "fc-nvme":
            try:
                address = normalize_wwpn(address)
            except BrocadeError:
                continue
        out.append(TargetPort(_array(lif), lif.vserver, lif.name, address, lif.node))
    return out


SwitchConnect = Callable[[BrocadeConfig], SwitchShell]


def switch_ports(
    switches: Iterable[BrocadeConfig], connect: SwitchConnect, wwpns: set[str]
) -> dict[str, SwitchPort]:
    """Where each of @wwpns is logged in, from the switches' name servers."""
    out: dict[str, SwitchPort] = {}
    for cfg in switches:
        entries = parse_nsshow(connect(cfg).run("nsshow"))
        per_port: dict[int, int] = {}
        for entry in entries:
            per_port[entry.port_index] = per_port.get(entry.port_index, 0) + 1
        for entry in entries:
            if entry.wwpn in wwpns and entry.wwpn not in out:
                out[entry.wwpn] = SwitchPort(
                    cfg.name, entry.port_index, per_port[entry.port_index] > 1
                )
    return out


def build(
    system: System,
    inv: Inventory,
    switches: Iterable[BrocadeConfig] = (),
    connect: SwitchConnect | None = None,
) -> Model:
    hps = host_ports(system)
    tps = target_ports(inv)
    if connect is not None:
        located = switch_ports(
            switches, connect, {hp.wwpn for hp in hps} | {tp.address for tp in tps}
        )
        hps = [HostPort(h.name, h.wwpn, h.driver, located.get(h.wwpn)) for h in hps]
        tps = [
            TargetPort(
                t.array, t.group, t.name, t.address, t.node, located.get(t.address)
            )
            for t in tps
        ]
    by_host = {hp.wwpn: hp for hp in hps}
    by_target = {tp.address: tp for tp in tps}
    paths = []
    for ctrl in controllers(system):
        if ctrl.cntrltype != "io" or not ctrl.identified:
            continue
        ident = ctrl.identity
        target = by_target.get(_wwpn(ident.traddr)) or by_target.get(ident.traddr)
        paths.append(Path(ctrl, by_host.get(_wwpn(ident.host_traddr)), target))
    names = {sub.nqn: sub.name for sub in inv.subsystems if sub.nqn}
    return Model(paths, hps, tps, names)
