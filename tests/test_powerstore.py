from typing import Any

from nvmf_failover.powerstore import discover, fc_ports, host_subsystem
from nvmf_failover.topology import controllers

from conftest import FakeSystem

HOSTNQN = "nqn.2014-08.org.nvmexpress:uuid:3133"
SUBNQN = "nqn.1988-11.com.dell:powerstore:00:f03028e73ef7D032D81E"


class FakeApi:
    """Answers like a PowerStore 3000T (trimmed)."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, str]]] = []

    def get(self, resource: str, **query: str) -> Any:
        self.requests.append((resource, query))
        if resource == "host":
            return [
                {
                    "id": "h1",
                    "name": "lully",
                    "host_initiators": [{"port_name": "nqn.1996-04.de.suse:lully"}],
                },
                {
                    "id": "h2",
                    "name": "dolin",
                    "host_initiators": [{"port_name": HOSTNQN}],
                },
            ]
        if resource == "host_volume_mapping":
            assert query["host_id"] == "eq.h2"
            return [{"volume_id": "v2"}, {"volume_id": "v1"}]
        if resource.startswith("volume/"):
            return {"name": f"dolin-disk-00{resource[-1]}"}
        if resource == "cluster":
            return [{"name": "pst3000-labs", "nvm_subsystem_nqn": SUBNQN}]
        if resource == "fc_port":
            return [
                {
                    "name": "NodeB-FEPort0",
                    "wwn_nvme": "58:cc:f0:99:49:28:06:da",
                    "is_link_up": True,
                    "node_id": "285deeddb9f0",
                    "port_index": 0,
                    "protocols": ["SCSI", "NVMe"],
                },
                {
                    "name": "NodeA-FEPort1",
                    "wwn_nvme": "58:CC:F0:91:49:29:06:DA",
                    "is_link_up": False,
                    "node_id": "37fd3782aa",
                    "port_index": 1,
                    "protocols": ["SCSI", "NVMe"],
                },
                {"name": "NodeA-FEPort9", "wwn_nvme": "", "protocols": ["SCSI"]},
            ]
        raise AssertionError(resource)


def test_host_subsystem_by_hostnqn() -> None:
    sub = host_subsystem(FakeApi(), HOSTNQN)
    assert sub is not None
    assert (sub.vserver, sub.name, sub.nqn) == (
        "powerstore:pst3000-labs",
        "dolin",
        SUBNQN,
    )
    assert sub.namespaces == ("dolin-disk-001", "dolin-disk-002")


def test_unknown_host_is_none() -> None:
    assert host_subsystem(FakeApi(), "nqn.other") is None


def test_fc_ports_only_nvme_ones_by_wwn_nvme() -> None:
    ports = fc_ports(FakeApi(), "powerstore:x")
    assert [(p.name, p.address, p.oper_up) for p in ports] == [
        ("NodeA-FEPort1", "58:cc:f0:91:49:29:06:da", False),
        ("NodeB-FEPort0", "58:cc:f0:99:49:28:06:da", True),
    ]


def test_discover_matches_controllers(system: FakeSystem) -> None:
    system.add_ctrl(
        "nvme9",
        "traddr=nn-0x58ccf090c92006da:pn-0x58ccf099492806da,host_traddr=nn-0x2:pn-0x1",
        nqn=SUBNQN,
    )
    sub, ports, paths = discover(FakeApi(), HOSTNQN, controllers(system))
    assert sub is not None and len(ports) == 2
    assert paths[("powerstore:pst3000-labs", "NodeB-FEPort0")] == ["nvme9"]
    assert paths[("powerstore:pst3000-labs", "NodeA-FEPort1")] == []
