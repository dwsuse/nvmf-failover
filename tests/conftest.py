"""A fake host: a sysfs tree in a temporary directory, commands recorded."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from nvmf_failover.system import System

HOST_WWPN = "nn-0x200000109b579ef5:pn-0x100000109b579ef5"
NQN = "nqn.1992-08.com.netapp:sn.1234:subsystem.dolin"


def fc_addr(target_pn: str, host: str = HOST_WWPN) -> str:
    return f"traddr=nn-0x201700a09890f5bf:pn-{target_pn},host_traddr={host}"


@dataclass
class FakeSystem(System):
    """System whose commands are recorded and answered from a table."""

    commands: list[list[str]] = field(default_factory=list)
    answers: dict[str, str] = field(default_factory=dict)
    failing: set[str] = field(default_factory=set)

    def run(self, argv: list[str], check: bool = True) -> str:
        self.commands.append(argv)
        return self.answers.get(" ".join(argv), "")

    def ok(self, argv: list[str]) -> bool:
        self.commands.append(argv)
        return " ".join(argv) not in self.failing

    def add_ctrl(
        self,
        name: str,
        address: str,
        state: str = "live",
        nqn: str = NQN,
        transport: str = "fc",
        cntrltype: str = "io",
        paths: dict[str, str] | None = None,
    ) -> None:
        base = self.path(f"/sys/class/nvme/{name}")
        base.mkdir(parents=True)
        for attr, value in (
            ("transport", transport),
            ("address", address),
            ("state", state),
            ("subsysnqn", nqn),
            ("cntrltype", cntrltype),
        ):
            (base / attr).write_text(value + "\n")
        for path, ana in (paths or {}).items():
            (base / path).mkdir()
            (base / path / "ana_state").write_text(ana + "\n")

    def add_scsi_host(
        self, host: str, driver: str, fc: bool = True, wwpn: str = ""
    ) -> None:
        scsi = self.path(f"/sys/class/scsi_host/{host}")
        scsi.mkdir(parents=True)
        (scsi / "proc_name").write_text(driver + "\n")
        (scsi / "link_state").write_text("Link Up - Ready\n")
        if fc:
            fch = self.path(f"/sys/class/fc_host/{host}")
            fch.mkdir(parents=True)
            (fch / "issue_lip").write_text("")
            (fch / "port_name").write_text((wwpn or "0x0") + "\n")

    def add_pci(self, host: str, pci: str, driver: str) -> None:
        """Put scsi host @host on PCI function @pci, bound to @driver."""
        dev = self.path(f"/sys/devices/pci0000:80/{pci}/{host}")
        dev.mkdir(parents=True)
        drv = self.path(f"/sys/bus/pci/drivers/{driver}")
        drv.mkdir(parents=True, exist_ok=True)
        devices = self.path("/sys/bus/pci/devices")
        devices.mkdir(parents=True, exist_ok=True)
        (devices / pci).symlink_to(dev.parent)
        (dev.parent / "driver").symlink_to(drv)
        (self.path(f"/sys/class/scsi_host/{host}") / "device").symlink_to(dev)


@pytest.fixture
def system(tmp_path: Path) -> FakeSystem:
    return FakeSystem(root=tmp_path)
