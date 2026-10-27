import json
from pathlib import Path

import pytest

from nvmf_failover.faults import (
    FaultError,
    FcLinkFault,
    FcLipFault,
    PciUnbindFault,
    TcpPortFault,
)

from conftest import FakeSystem


def test_fc_link_writes_link_state(system: FakeSystem) -> None:
    system.add_scsi_host("host3", "lpfc")
    fault = FcLinkFault(system, "host3")
    state = system.path("/sys/class/scsi_host/host3/link_state")

    fault.down()
    assert state.read_text() == "down"
    fault.up()
    assert state.read_text() == "up"
    assert fault.key == "fc-link:host3"


def test_fc_link_refuses_other_drivers(system: FakeSystem) -> None:
    system.add_scsi_host("host2", "qla2xxx")
    with pytest.raises(FaultError, match="qla2xxx can't hold"):
        FcLinkFault(system, "host2")


def test_fc_link_refuses_bad_host_names(system: FakeSystem) -> None:
    with pytest.raises(FaultError, match="not a SCSI host"):
        FcLinkFault(system, "../../etc")


def test_fc_lip_only_bounces(system: FakeSystem) -> None:
    system.add_scsi_host("host2", "qla2xxx")
    fault = FcLipFault(system, "host2")
    fault.bounce()
    assert system.path("/sys/class/fc_host/host2/issue_lip").read_text() == "1"
    with pytest.raises(FaultError, match="no down state"):
        fault.down()


def _pci_host(system: FakeSystem, host: str, pci: str, driver: str) -> None:
    dev = system.path(f"/sys/devices/pci0000:80/{pci}/{host}")
    dev.mkdir(parents=True)
    drv = system.path(f"/sys/bus/pci/drivers/{driver}")
    drv.mkdir(parents=True)
    devices = system.path("/sys/bus/pci/devices")
    devices.mkdir(parents=True)
    (devices / pci).symlink_to(dev.parent)
    (dev.parent / "driver").symlink_to(drv)
    scsi = system.path(f"/sys/class/scsi_host/{host}")
    scsi.mkdir(parents=True)
    (scsi / "device").symlink_to(dev)


def test_pci_unbind_finds_function_and_driver(system: FakeSystem) -> None:
    _pci_host(system, "host2", "0000:81:00.0", "qla2xxx")
    fault = PciUnbindFault(system, "host2")
    assert (fault.pci, fault.driver) == ("0000:81:00.0", "qla2xxx")

    fault.down()
    unbind = system.path("/sys/bus/pci/drivers/qla2xxx/unbind")
    assert unbind.read_text() == "0000:81:00.0"

    # still bound (the fake doesn't unbind): up() leaves it alone
    fault.up()
    assert not system.path("/sys/bus/pci/drivers/qla2xxx/bind").exists()

    system.path("/sys/devices/pci0000:80/0000:81:00.0/driver").unlink()
    fault.up()
    bind = system.path("/sys/bus/pci/drivers/qla2xxx/bind")
    assert bind.read_text() == "0000:81:00.0"


def _nft_chain(*comments: str) -> str:
    rules = [{"rule": {"handle": i + 7, "comment": c}} for i, c in enumerate(comments)]
    return json.dumps({"nftables": [{"metainfo": {}}, *rules]})


def test_tcp_port_adds_one_rule_in_its_own_table(system: FakeSystem) -> None:
    system.failing.add("nft list table inet nvmf_failover")
    fault = TcpPortFault(system, "10.100.128.29", 4420)

    fault.down()

    adds = [c for c in system.commands if c[:2] == ["nft", "add"]]
    assert adds[0] == ["nft", "add", "table", "inet", "nvmf_failover"]
    assert "ip" in adds[-1] and "10.100.128.29" in adds[-1]
    assert adds[-1][-2:] == ["comment", '"tcp-port:10.100.128.29:4420"']


def test_tcp_port_up_deletes_only_its_rule(system: FakeSystem) -> None:
    fault = TcpPortFault(system, "10.100.128.29", 4421)
    chain = "nft -j -a list chain inet nvmf_failover input"
    system.answers[chain] = _nft_chain(
        "tcp-port:10.100.128.29:4420", "tcp-port:10.100.128.29:4421"
    )
    system.answers["nft -j list table inet nvmf_failover"] = _nft_chain(
        "tcp-port:10.100.128.29:4420"
    )

    fault.up()

    deletes = [c for c in system.commands if c[:2] == ["nft", "delete"]]
    assert deletes == [
        ["nft", "delete", "rule", "inet", "nvmf_failover", "input", "handle", "8"]
    ]


def test_tcp_port_ipv6_uses_ip6(system: FakeSystem, tmp_path: Path) -> None:
    system.failing.add("nft list table inet nvmf_failover")
    TcpPortFault(system, "fd00::5", 4420).down()
    rule = next(c for c in system.commands if c[:3] == ["nft", "add", "rule"])
    assert rule[6:8] == ["ip6", "saddr"]
