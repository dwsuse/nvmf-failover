from nvmf_failover.config import BrocadeConfig
from nvmf_failover.discover import Inventory, Lif, Subsystem
from nvmf_failover.model import Model, SwitchPort, build

from conftest import FakeSystem

H3 = "0x100000109b579ef5"  # lpfc host3
H2 = "0x21000024ff7fa448"  # qla2xxx host2
NETAPP = "nqn.1992-08.com.netapp:sn.1:subsystem.dolin-lpfc"
DELL = "nqn.1988-11.com.dell:powerstore:00:f030"


def fc(target: str, host: str) -> str:
    return f"traddr=nn-0x2:pn-{target},host_traddr=nn-0x1:pn-{host}"


def inventory() -> Inventory:
    return Inventory(
        hostnqn="h",
        lifs=[
            Lif(
                "svm-lpfc",
                "lif_1",
                "fc-nvme",
                "20:18:00:a0:98:90:f5:bf",
                True,
                True,
                "node-1",
                "1a",
            ),
            Lif(
                "svm-lpfc",
                "lif_2",
                "fc-nvme",
                "20:19:00:a0:98:90:f5:bf",
                True,
                True,
                "node-2",
                "1b",
            ),
            Lif(
                "powerstore:pst",
                "NodeB-FEPort0",
                "fc-nvme",
                "58:cc:f0:99:49:28:06:da",
                True,
                True,
                "285deedd",
                "0",
            ),
        ],
        subsystems=[
            Subsystem("svm-lpfc", "dolin-lpfc-subsystem", NETAPP),
            Subsystem("powerstore:pst", "dolin", DELL),
        ],
    )


def world(system: FakeSystem) -> None:
    system.add_scsi_host("host3", "lpfc", wwpn=H3)
    system.add_scsi_host("host2", "(null)", wwpn=H2)
    system.add_pci("host2", "0000:81:00.0", "qla2xxx")
    system.add_ctrl("nvme1", fc("0x201800a09890f5bf", H3), nqn=NETAPP)
    system.add_ctrl("nvme2", fc("0x201900a09890f5bf", H3), nqn=NETAPP)
    system.add_ctrl("nvme3", fc("0x201800a09890f5bf", H2), nqn=NETAPP)
    system.add_ctrl("nvme4", fc("0x58ccf099492806da", H3), nqn=DELL)
    system.add_ctrl(
        "nvme9",
        fc("0x201800a09890f5bf", H3),
        nqn="nqn.2014-08.org.nvmexpress.discovery",
        cntrltype="discovery",
    )
    system.add_ctrl(
        "nvme10", fc("0x201800a09890f5bf", H3), state="connecting", nqn="(efault)"
    )


def test_paths_link_host_and_target_ports(system: FakeSystem) -> None:
    world(system)
    model = build(system, inventory())

    links = {
        p.ctrl.name: (
            p.host_port.name if p.host_port else None,
            p.target_port.key if p.target_port else None,
        )
        for p in model.paths
    }
    # only identified I/O controllers are paths
    assert links == {
        "nvme1": ("host3", "svm-lpfc/lif_1"),
        "nvme2": ("host3", "svm-lpfc/lif_2"),
        "nvme3": ("host2", "svm-lpfc/lif_1"),
        "nvme4": ("host3", "powerstore:pst/NodeB-FEPort0"),
    }
    drivers = {hp.name: hp.driver for hp in model.host_ports}
    assert drivers == {"host2": "", "host3": "lpfc"}


def _names(model: Model, **sel: str) -> list[str]:
    return [p.ctrl.name for p in model.select(**sel)]


def test_select_by_subsystem_array_and_node(system: FakeSystem) -> None:
    world(system)
    model = build(system, inventory())
    # the name on the array or the NQN
    assert _names(model, subsystem="*dolin-lpfc*") == ["nvme1", "nvme2", "nvme3"]
    assert _names(model, subsystem="*powerstore*") == ["nvme4"]
    assert _names(model, array="powerstore") == ["nvme4"]
    assert _names(model, array="netapp", node="node-2") == ["nvme2"]
    assert _names(model) == ["nvme1", "nvme2", "nvme3", "nvme4"]


NSSHOW = """\
 N    600100;      3;10:00:00:10:9b:57:9e:f5;20:00:00:10:9b:57:9e:f5; 0x00000003
    Device type: Physical Initiator
    Port Index: 1
 N    601100;      3;20:00:00:a0:98:90:f5:bf;20:00:00:a0:98:90:f5:bf; 0x00000003
    Device type: Physical Target
    Port Index: 17
 N    601101;      3;20:18:00:a0:98:90:f5:bf;20:17:00:a0:98:90:f5:bf; 0x00000003
    Device type: NPIV Target
    Port Index: 17
 N    601102;      3;20:19:00:a0:98:90:f5:bf;20:17:00:a0:98:90:f5:bf; 0x00000003
    Device type: NPIV Target
    Port Index: 17
"""


class Switch:
    def run(self, command: str) -> str:
        return NSSHOW


def test_switch_ports_are_located_and_marked_shared(system: FakeSystem) -> None:
    world(system)
    brc = BrocadeConfig("brc4", "h", "admin", "p")
    model = build(system, inventory(), [brc], lambda cfg: Switch())

    hosts = {hp.name: hp.switch_port for hp in model.host_ports}
    assert hosts["host3"] == SwitchPort("brc4", 1, shared=False)
    assert hosts["host2"] is None  # not on this switch
    targets = {tp.name: tp.switch_port for tp in model.target_ports}
    assert targets["lif_1"] == SwitchPort("brc4", 17, shared=True)
    assert targets["NodeB-FEPort0"] is None
