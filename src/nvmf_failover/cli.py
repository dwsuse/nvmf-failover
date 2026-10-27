"""nvmf-failover: break NVMe-oF paths on purpose and check the host recovers."""

from __future__ import annotations

import argparse
import contextlib
import json
import signal
import sys
import time
from collections.abc import Iterator
from dataclasses import asdict
from pathlib import Path
from types import FrameType

from . import __version__
from . import config as config_mod
from . import scenario as scenario_mod
from .brocade import SwitchPool
from .discover import Inventory, discover
from .faults import FaultError
from .load import Load, LoadResult
from .monitor import Event, Monitor, live_by_identity
from .ontap import OntapShell
from .system import System
from .topology import controllers
from .verify import failed_units, verdict, wait_recovered


def _hostnqn(system: System) -> str:
    return system.read("/etc/nvme/hostnqn")


@contextlib.contextmanager
def _context(
    cfg: config_mod.Config, system: System, with_inventory: bool = False
) -> Iterator[scenario_mod.Context]:
    """What building faults needs; the connections are closed afterwards."""
    pool = SwitchPool()
    shell: OntapShell | None = OntapShell(cfg.ontap) if cfg.ontap else None
    try:
        inv = (
            discover(shell, _hostnqn(system), controllers(system))
            if shell and with_inventory
            else None
        )
        yield scenario_mod.Context(
            system,
            shell,
            cfg.ontap.vserver if cfg.ontap else "",
            inv,
            switches=cfg.brocade,
            connect=pool,
        )
    finally:
        pool.close()
        if shell:
            shell.close()


def cmd_status(args: argparse.Namespace, system: System) -> int:
    for ctrl in controllers(system):
        if args.filter and args.filter not in ctrl.identity.short():
            continue
        paths = " ".join(f"{p}:{a}" for p, a in ctrl.paths.items())
        print(
            f"{ctrl.name:8} {ctrl.state:10} {ctrl.cntrltype:9} "
            f"{ctrl.identity.short()}  {paths}"
        )
    return 0


def cmd_discover(args: argparse.Namespace, system: System) -> int:
    cfg = config_mod.load(args.config)
    if cfg.ontap is None:
        print("no [ontap] in the configuration", file=sys.stderr)
        return 2
    with OntapShell(cfg.ontap) as shell:
        inv = discover(shell, args.hostnqn or _hostnqn(system), controllers(system))
    if args.json:
        print(json.dumps(_inventory_json(inv), indent=2))
        return 0
    print(f"host NQN: {inv.hostnqn}")
    for sub in inv.subsystems:
        print(f"subsystem {sub.vserver}/{sub.name} {sub.nqn}")
        for path in sub.namespaces:
            print(f"  namespace {path}")
    for lif in inv.host_lifs():
        state = f"{'up' if lif.admin_up else 'down'}/{'up' if lif.oper_up else 'down'}"
        ctrls = " ".join(inv.paths.get((lif.vserver, lif.name), [])) or "-"
        print(
            f"lif {lif.vserver}/{lif.name} {lif.protocol} {lif.address} "
            f"{state} {lif.node}:{lif.port} -> {ctrls}"
        )
    return 0


def _inventory_json(inv: Inventory) -> dict[str, object]:
    return {
        "hostnqn": inv.hostnqn,
        "subsystems": [asdict(sub) for sub in inv.subsystems],
        "lifs": [
            asdict(lif) | {"controllers": inv.paths.get((lif.vserver, lif.name), [])}
            for lif in inv.host_lifs()
        ],
    }


def cmd_fault(args: argparse.Namespace, system: System) -> int:
    cfg = config_mod.load(args.config)
    raw = {"fault": args.kind, "action": args.action}
    if args.kind in ("fc-link", "fc-lip", "pci-unbind"):
        raw["host"] = args.target
    elif args.kind == "ontap-lif":
        raw["lif"] = args.target
        if args.vserver:
            raw["vserver"] = args.vserver
    elif args.kind == "brocade-port":
        raw |= {"wwpn": args.target, "force": args.force}
    else:
        address, _, port = args.target.rpartition(":")
        raw |= {"address": address, "port": port or "4420"}
    with _context(cfg, system) as ctx:
        fault = scenario_mod.make_fault(raw, ctx)
        getattr(fault, args.action)()
    print(f"{args.action} {fault.key}")
    return 0


def cmd_monitor(args: argparse.Namespace, system: System) -> int:
    start = time.time()
    monitor = Monitor(
        system,
        journal_units=_journal_units(args),
        sink=lambda e: print(e.line(start), flush=True),
    )
    monitor.start()
    try:
        time.sleep(args.seconds) if args.seconds else signal.pause()
    except KeyboardInterrupt:
        pass
    finally:
        monitor.stop()
    return 0


def _journal_units(args: argparse.Namespace) -> tuple[str, ...]:
    return () if args.no_journal else ("nvme-discoverd*",)


def _interrupt(signum: int, frame: FrameType | None) -> None:
    raise KeyboardInterrupt(f"signal {signum}")


def cmd_run(args: argparse.Namespace, system: System) -> int:
    cfg = config_mod.load(args.config)
    scen = scenario_mod.load(args.scenario)
    out = args.out / time.strftime(f"%Y%m%d-%H%M%S-{args.scenario.stem}")
    out.mkdir(parents=True)
    start = time.time()
    log = (out / "events.log").open("w")

    def sink(event: Event) -> None:
        line = event.line(start)
        print(line, flush=True)
        log.write(line + "\n")
        log.flush()

    signal.signal(signal.SIGTERM, _interrupt)
    with _context(cfg, system, with_inventory=True) as ctx:
        setup = scenario_mod.expand(scen.setup, ctx)
        steps = scenario_mod.expand(scen.steps, ctx)

        baseline = live_by_identity(system)
        failed_before = failed_units(system)
        monitor = Monitor(system, journal_units=_journal_units(args), sink=sink)
        load = Load(args.io, out / "fio.json", write=args.io_write) if args.io else None
        monitor.add(
            "step",
            f"scenario {scen.name}: {len(baseline)} live paths, "
            f"{len(steps)} steps x {scen.repeat}",
        )
        monitor.start()
        if load:
            load.start()
        runner = scenario_mod.Runner(time.sleep, lambda t: monitor.add("step", t))
        interrupted = False
        try:
            runner.run(setup, steps, scen.repeat)
        except KeyboardInterrupt:
            interrupted = True
            monitor.add("step", "interrupted, faults restored")
        monitor.add("step", "waiting for every path to come back")
        recovered, seconds, now = wait_recovered(
            baseline, lambda: live_by_identity(system), scen.recover_timeout
        )
        result: LoadResult | None = load.stop() if load else None
        monitor.stop()

    result_v = verdict(
        baseline,
        recovered,
        seconds,
        now,
        failed_before,
        failed_units(system),
        list(runner.down),
        result,
    )
    with (out / "events.jsonl").open("w") as f:
        monitor.dump(f)
    summary = {
        "scenario": scen.name,
        "passed": result_v.passed,
        "interrupted": interrupted,
        "lines": result_v.lines(),
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    log.close()
    print("\n".join(result_v.lines()))
    print(f"logs in {out}")
    return 0 if result_v.passed and not interrupted else 1


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="nvmf-failover", description=__doc__)
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument(
        "-c",
        "--config",
        type=Path,
        help=f"configuration file (default {config_mod.DEFAULT_PATH})",
    )
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("status", help="the fabrics controllers and their paths")
    s.add_argument(
        "filter", nargs="?", default="", help="only identities containing this text"
    )
    s.set_defaults(func=cmd_status)

    s = sub.add_parser(
        "discover",
        help="read the array configuration for "
        "this host and match it to the host's controllers",
    )
    s.add_argument("--json", action="store_true")
    s.add_argument("--hostnqn", help="the host's NQN (default: from /etc/nvme/hostnqn)")
    s.set_defaults(func=cmd_discover)

    s = sub.add_parser("fault", help="apply one fault by hand")
    s.add_argument(
        "kind", choices=[k for k in scenario_mod.FAULT_KINDS if k != "ontap-lif-cycle"]
    )
    s.add_argument("target", help="hostN, LIF name, WWPN, or ADDRESS[:PORT]")
    s.add_argument("action", choices=scenario_mod.ACTIONS)
    s.add_argument("--vserver", help="for ontap-lif, default from the config")
    s.add_argument(
        "--force",
        action="store_true",
        help="for brocade-port: disable a port other WWPNs use too",
    )
    s.set_defaults(func=cmd_fault)

    s = sub.add_parser(
        "monitor", help="print controller, path and daemon events until interrupted"
    )
    s.add_argument("--seconds", type=float, default=0)
    s.add_argument(
        "--no-journal",
        action="store_true",
        help="don't follow the nvme-discoverd journal",
    )
    s.set_defaults(func=cmd_monitor)

    s = sub.add_parser("run", help="run a scenario and check the recovery")
    s.add_argument("scenario", type=Path)
    s.add_argument("--io", metavar="DEVICE", help="run fio on this namespace")
    s.add_argument(
        "--io-write",
        action="store_true",
        help="write too (destroys the data on DEVICE)",
    )
    s.add_argument(
        "--out",
        type=Path,
        default=Path("runs"),
        help="directory for the run logs (default ./runs)",
    )
    s.add_argument(
        "--no-journal",
        action="store_true",
        help="don't follow the nvme-discoverd journal",
    )
    s.set_defaults(func=cmd_run)
    return p


def main(argv: list[str] | None = None, system: System | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return int(args.func(args, system or System()))
    except (config_mod.ConfigError, scenario_mod.ScenarioError, FaultError) as err:
        print(f"nvmf-failover: {err}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
