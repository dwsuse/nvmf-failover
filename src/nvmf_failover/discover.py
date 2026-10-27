"""Read the array's configuration for this host and match it to the host.

Nothing about the array is configured by hand: the LIFs, subsystems and
namespaces come from the cluster shell, and a LIF is matched to the host's
controllers by its address (the WWPN of an FC LIF is the port name in the
controller's traddr, the IP address of a TCP LIF its traddr).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from .ontap import Shell, parse_fields
from .topology import Controller


@dataclass(frozen=True)
class Lif:
    vserver: str
    name: str
    protocol: str  # fc-nvme, nvme-tcp, ...
    address: str  # WWPN for FC, IP address for TCP
    admin_up: bool
    oper_up: bool
    node: str = ""
    port: str = ""


@dataclass(frozen=True)
class Subsystem:
    vserver: str
    name: str
    nqn: str = ""
    namespaces: tuple[str, ...] = ()


@dataclass
class Inventory:
    hostnqn: str
    lifs: list[Lif] = field(default_factory=list)
    subsystems: list[Subsystem] = field(default_factory=list)
    # LIF (vserver, name) -> names of the host's controllers through it
    paths: dict[tuple[str, str], list[str]] = field(default_factory=dict)

    def vservers(self) -> list[str]:
        return sorted({sub.vserver for sub in self.subsystems})

    def host_lifs(self, vserver: str | None = None) -> list[Lif]:
        """The LIFs of the host's vservers (or of @vserver), by name."""
        names = {vserver} if vserver else set(self.vservers())
        return sorted(
            (lif for lif in self.lifs if lif.vserver in names),
            key=lambda lif: (lif.vserver, lif.name),
        )


_NVME_PROTOCOLS = ("fc-nvme", "nvme-tcp")


def read_lifs(shell: Shell) -> list[Lif]:
    out = []
    for proto in _NVME_PROTOCOLS:
        text = shell.run(
            f"network interface show -data-protocol {proto} -fields "
            "vserver,lif,wwpn,address,status-admin,status-oper,curr-node,"
            "curr-port"
        )
        for row in parse_fields(text):
            address = row.get("wwpn", "-")
            if address in ("", "-"):
                address = row.get("address", "")
            out.append(
                Lif(
                    vserver=row["vserver"],
                    name=row["lif"],
                    protocol=proto,
                    address=address.lower(),
                    admin_up=row.get("status-admin") == "up",
                    oper_up=row.get("status-oper") == "up",
                    node=row.get("curr-node", ""),
                    port=row.get("curr-port", ""),
                )
            )
    return out


def read_subsystems(shell: Shell, hostnqn: str) -> list[Subsystem]:
    """The subsystems that list @hostnqn, with their namespaces."""
    hosts = parse_fields(
        shell.run("vserver nvme subsystem host show -fields vserver,subsystem,host-nqn")
    )
    mine = sorted(
        {
            (row["vserver"], row["subsystem"])
            for row in hosts
            if row.get("host-nqn") == hostnqn
        }
    )
    maps = parse_fields(
        shell.run("vserver nvme subsystem map show -fields vserver,subsystem,path")
    )
    subs = parse_fields(
        shell.run("vserver nvme subsystem show -fields vserver,subsystem,target-nqn")
    )
    nqns = {
        (row["vserver"], row["subsystem"]): row.get("target-nqn", "") for row in subs
    }
    out = []
    for vserver, name in mine:
        paths = tuple(
            sorted(
                row["path"]
                for row in maps
                if (row["vserver"], row["subsystem"]) == (vserver, name)
            )
        )
        out.append(Subsystem(vserver, name, nqns.get((vserver, name), ""), paths))
    return out


def lif_matches(lif: Lif, ctrl: Controller) -> bool:
    """Whether @ctrl's target address is @lif."""
    traddr = ctrl.identity.traddr.lower()
    if lif.protocol == "fc-nvme":
        wwpn = lif.address.replace(":", "")
        return traddr.rsplit("pn-0x", 1)[-1] == wwpn
    return traddr == lif.address


def match(
    lifs: Iterable[Lif], ctrls: Iterable[Controller]
) -> dict[tuple[str, str], list[str]]:
    ctrls = list(ctrls)
    return {
        (lif.vserver, lif.name): [c.name for c in ctrls if lif_matches(lif, c)]
        for lif in lifs
    }


def discover(shell: Shell, hostnqn: str, ctrls: Iterable[Controller]) -> Inventory:
    inv = Inventory(hostnqn=hostnqn)
    inv.subsystems = read_subsystems(shell, hostnqn)
    inv.lifs = read_lifs(shell)
    inv.paths = match(inv.host_lifs(), ctrls)
    return inv
