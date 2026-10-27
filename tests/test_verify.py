import json
import stat
from pathlib import Path

import pytest

from nvmf_failover.config import ConfigError, load
from nvmf_failover.load import LoadResult, parse_report
from nvmf_failover.monitor import diff, live_by_identity, snapshot
from nvmf_failover.topology import Identity, controllers
from nvmf_failover.verify import failed_units, verdict, wait_recovered

from conftest import FakeSystem, fc_addr

A = Identity("fc", "nqn", "t1", "h1")
B = Identity("fc", "nqn", "t2", "h1")


def test_wait_recovered_until_all_back() -> None:
    states = iter([{A: ["nvme1"]}, {A: ["nvme1"], B: ["nvme9"]}])
    clock = iter([0.0, 0.0, 4.0])
    ok, seconds, now = wait_recovered(
        {A: ["nvme1"], B: ["nvme2"]},
        lambda: next(states),
        60,
        clock=lambda: next(clock),
        sleep=lambda s: None,
    )
    assert ok and seconds == 4.0 and now[B] == ["nvme9"]


def test_wait_recovered_times_out() -> None:
    clock = iter([0.0, 30.0, 61.0, 61.0])
    ok, _, _ = wait_recovered(
        {A: ["nvme1"]}, lambda: {}, 60, clock=lambda: next(clock), sleep=lambda s: None
    )
    assert not ok


def test_verdict_fails_on_any_problem() -> None:
    good = verdict({A: ["nvme1"]}, True, 3, {A: ["nvme4"]}, set(), set(), [], None)
    assert good.passed
    assert good.lines()[:2] == ["result: PASS", "all paths back after 3s"]

    dup = verdict(
        {A: ["nvme1"]}, True, 3, {A: ["nvme4", "nvme5"]}, set(), set(), [], None
    )
    assert not dup.passed
    assert any("duplicate" in line for line in dup.lines())

    missing = verdict(
        {A: ["nvme1"], B: ["nvme2"]}, False, 300, {A: ["nvme1"]}, set(), set(), [], None
    )
    assert missing.missing == [B] and not missing.passed

    units = verdict(
        {}, True, 0, {}, {"old.service"}, {"old.service", "new.service"}, [], None
    )
    assert units.new_failed_units == ["new.service"]

    io = verdict({}, True, 0, {}, set(), set(), [], LoadResult(3, 0, 0, 0))
    assert not io.passed


def test_failed_units_parses_systemctl(system: FakeSystem) -> None:
    argv = (
        "systemctl list-units nvme-discoverd-* --state=failed --all --no-legend --plain"
    )
    system.answers[argv] = (
        "nvme-discoverd-ab.service loaded failed failed x\n"
        "nvme-discoverd-cd.service loaded failed failed y\n"
    )
    assert failed_units(system) == {
        "nvme-discoverd-ab.service",
        "nvme-discoverd-cd.service",
    }


def test_snapshot_keys_paths_by_identity_and_nsid(system: FakeSystem) -> None:
    system.add_ctrl("nvme5", fc_addr("0x2019"), paths={"nvme1c5n3": "optimized"})
    before = snapshot(controllers(system))
    system.path("/sys/class/nvme/nvme5/state").write_text("connecting\n")
    system.path("/sys/class/nvme/nvme5/nvme1c5n3/ana_state").write_text(
        "inaccessible\n"
    )

    lines = diff(before, snapshot(controllers(system)))

    assert len(lines) == 2
    assert lines[0].startswith("ctrl ") and "nvme5:live -> nvme5:connecting" in lines[0]
    assert "nsid 3: optimized -> inaccessible" in lines[1]


def test_live_by_identity_skips_dead(system: FakeSystem) -> None:
    system.add_ctrl("nvme1", fc_addr("0x2019"))
    system.add_ctrl("nvme2", fc_addr("0x2018"), state="connecting")
    assert list(live_by_identity(system).values()) == [["nvme1"]]


def test_fio_report() -> None:
    report = "note: something\n" + json.dumps(
        {
            "jobs": [
                {
                    "total_err": 2,
                    "read": {"io_bytes": 4096, "clat_ns": {"max": 2.5e9}},
                    "write": {"io_bytes": 1024, "clat_ns": {"max": 1e6}},
                }
            ]
        }
    )
    result = parse_report(report)
    assert (result.errors, result.io_bytes, result.max_latency_ms) == (2, 5120, 2500.0)


def test_config_password_from_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "c.toml"
    path.write_text('[ontap]\nhost = "h"\nuser = "u"\npassword_env = "PW"\n')
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    monkeypatch.setenv("PW", "secret")
    cfg = load(path)
    assert cfg.ontap is not None and cfg.ontap.password == "secret"


def test_config_must_be_private(tmp_path: Path) -> None:
    path = tmp_path / "c.toml"
    path.write_text('[ontap]\nhost = "h"\nuser = "u"\npassword = "p"\n')
    path.chmod(0o644)
    with pytest.raises(ConfigError, match="chmod 600"):
        load(path)


def test_config_missing_default_is_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    assert load().ontap is None
    with pytest.raises(ConfigError, match="no such file"):
        load(tmp_path / "nope.toml")


def test_config_example_loads(tmp_path: Path) -> None:
    example = Path(__file__).parent.parent / "config.toml.example"
    path = tmp_path / "c.toml"
    path.write_text(example.read_text())
    path.chmod(0o600)
    cfg = load(path)
    assert cfg.ontap is not None and cfg.ontap.vserver == "nvme-svm-dolin-lpfc"
    assert cfg.powerstore is not None and not cfg.powerstore.verify_tls
    assert [b.name for b in cfg.brocade] == ["RD-LABS-BRC2"]
    assert cfg.brocade[0].reader()[0] == "user"


def test_config_brocade_without_read_user_uses_admin(tmp_path: Path) -> None:
    path = tmp_path / "c.toml"
    path.write_text('[[brocade]]\nhost = "h"\nuser = "admin"\npassword = "p"\n')
    path.chmod(0o600)
    (switch,) = load(path).brocade
    assert switch.name == "h" and switch.reader() == ("admin", "p")


def test_snapshot_reports_unidentified_by_name(system: FakeSystem) -> None:
    system.add_ctrl(
        "nvme11",
        fc_addr("0x2018"),
        state="connecting",
        nqn="(efault)",
        cntrltype="reserved",
    )
    snap = snapshot(controllers(system))
    assert snap == {"ctrl nvme11 (not identified yet)": "connecting"}
    assert live_by_identity(system) == {}


def test_live_by_identity_ignores_discovery_controllers(system: FakeSystem) -> None:
    system.add_ctrl("nvme1", fc_addr("0x2019"))
    system.add_ctrl(
        "nvme2",
        fc_addr("0x2019"),
        nqn="nqn.2014-08.org.nvmexpress.discovery",
        cntrltype="discovery",
    )
    assert list(live_by_identity(system).values()) == [["nvme1"]]


def test_fio_argv_one_section_per_job(tmp_path: Path) -> None:
    from nvmf_failover.load import Job, fio_argv

    argv = fio_argv([Job("/dev/a"), Job("/dev/b", "verify")], tmp_path / "f.json")
    a, b = argv.index("--name=job0"), argv.index("--name=job1")
    assert argv[a + 1 : b] == ["--filename=/dev/a", "--rw=randread"]
    assert argv[b + 1 : b + 4] == [
        "--filename=/dev/b",
        "--rw=randwrite",
        "--verify=crc32c",
    ]
    assert "--continue_on_error=all" in argv[:a]
    with pytest.raises(ValueError, match="not one of"):
        fio_argv([Job("/dev/a", "trim")], tmp_path / "f.json")


def test_fio_report_sums_jobs() -> None:
    report = json.dumps(
        {
            "jobs": [
                {"total_err": 1, "read": {"io_bytes": 10}},
                {"total_err": 2, "write": {"io_bytes": 5, "clat_ns": {"max": 3e6}}},
            ]
        }
    )
    assert parse_report(report) == LoadResult(3, 15, 3.0, 0)
