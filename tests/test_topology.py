from collections.abc import Iterator
from pathlib import Path

import pytest

from nvmf_failover.topology import Identity, by_identity, controllers

from conftest import NQN, FakeSystem, fc_addr


def test_reads_fabrics_controllers_sorted(system: FakeSystem) -> None:
    system.add_ctrl("nvme10", fc_addr("0x201800a09890f5bf"))
    system.add_ctrl(
        "nvme2",
        fc_addr("0x201900a09890f5bf"),
        state="connecting",
        paths={"nvme1c2n1": "optimized", "nvme1c2n3": "inaccessible"},
    )
    system.add_ctrl("nvme0", "", transport="pcie")

    ctrls = controllers(system)

    assert [c.name for c in ctrls] == ["nvme2", "nvme10"]
    assert ctrls[0].state == "connecting"
    assert ctrls[0].paths == {"nvme1c2n1": "optimized", "nvme1c2n3": "inaccessible"}
    assert ctrls[0].identity == Identity(
        "fc",
        NQN,
        "nn-0x201700a09890f5bf:pn-0x201900a09890f5bf",
        "nn-0x200000109b579ef5:pn-0x100000109b579ef5",
    )


def test_short_identity_names_the_ports(system: FakeSystem) -> None:
    system.add_ctrl("nvme1", fc_addr("0x201900a09890f5bf"))
    assert (
        controllers(system)[0].identity.short()
        == "subsystem.dolin 0x201900a09890f5bf <- 0x100000109b579ef5"
    )


def test_duplicates_share_an_identity(system: FakeSystem) -> None:
    system.add_ctrl("nvme1", fc_addr("0x201900a09890f5bf"))
    system.add_ctrl("nvme2", fc_addr("0x201900a09890f5bf"))
    system.add_ctrl("nvme3", fc_addr("0x201800a09890f5bf"))

    groups = by_identity(controllers(system))

    assert sorted(len(v) for v in groups.values()) == [1, 2]


def test_no_nvme_class_means_no_controllers(system: FakeSystem) -> None:
    assert controllers(system) == []


def test_connecting_controller_is_not_identified(system: FakeSystem) -> None:
    # what sysfs shows while nvme-fc is still connecting a new controller
    system.add_ctrl(
        "nvme11",
        fc_addr("0x2018"),
        state="connecting",
        nqn="(efault)",
        cntrltype="reserved",
    )
    (ctrl,) = controllers(system)
    assert not ctrl.identified
    assert ctrl.identity.subsysnqn == "" and ctrl.cntrltype == ""


def test_controller_deleted_during_the_scan_is_skipped(
    system: FakeSystem, monkeypatch: pytest.MonkeyPatch
) -> None:
    system.add_ctrl("nvme1", fc_addr("0x201900a09890f5bf"))
    system.add_ctrl("nvme2", fc_addr("0x201800a09890f5bf"))
    real = Path.iterdir

    def iterdir(self: Path) -> Iterator[Path]:
        # the kernel deletes nvme2 between listing /sys/class/nvme and here
        if self.name == "nvme2":
            raise FileNotFoundError(2, "No such file or directory", str(self))
        return real(self)

    monkeypatch.setattr(Path, "iterdir", iterdir)
    assert [c.name for c in controllers(system)] == ["nvme1"]
