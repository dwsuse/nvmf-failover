# nvmf-failover

Break NVMe-oF paths on purpose and check that the host recovers: every
path comes back, without duplicates, without failed connection units, and
without I/O errors.

It is written for testing [nvme-discoverd](https://github.com/linux-nvme/nvme-cli/tree/master/discoverd)
and the kernel's multipath against real arrays, but works for any
NVMe-oF host.

## What it does

- **Faults** on either side of a path:
  - `fc-link`: take the link of an lpfc port down and up
    (`/sys/class/scsi_host/hostN/link_state`).
  - `fc-lip`: reset the link of any FC port (`issue_lip`).
  - `pci-unbind`: unbind an HBA port's PCI function and bind it again, for
    drivers without a link control (qla2xxx).
  - `ontap-lif`: set a NetApp ONTAP LIF administratively down and up.
  - `ontap-lif-cycle`: every LIF of a vserver that carries a path of this
    host, one after the other.
  - `tcp-port`: drop what a TCP target port sends (nftables, in its own
    `inet nvmf_failover` table).
  - `brocade-port`: disable and enable the Brocade switch port a WWPN is
    logged in through, found in the switches' name servers. A port that
    other WWPNs use too (an array port with other hosts' NPIV logins) is
    refused unless `force = true`.
- **Monitoring** during a run: controller states per identity, ANA states
  per path, the nvme-discoverd journal, optionally fio I/O on a namespace.
- **A verdict** at the end, compared with the state before the run.
- **Discovery** of the array's configuration for this host: subsystems,
  namespaces, LIFs, and which of the host's controllers go through which LIF.
  Nothing about the array is kept in files here.

Every fault a run puts down is put up again at the end, also when a step
fails or the run is interrupted (Ctrl-C, SIGTERM).

## Install

Python 3.11 or later. paramiko is needed only for the ONTAP faults.

    pip install '.[ontap]'

On SLE 15 SP7: `zypper in python311-paramiko` and run it with `python3.11`.

## Configuration

`~/.config/nvmf-failover/config.toml`, readable by its owner only:

```toml
[ontap]
host = "10.0.0.1"
user = "admin"
password = "..."        # or password_env = "ONTAP_PASSWORD"
vserver = "svm-name"    # default for ontap-lif
```

See `config.toml.example` for the `[powerstore]` and `[[brocade]]`
sections. ssh host keys are trusted on first use and kept in
`~/.config/nvmf-failover/known_hosts`.

## Use

    nvmf-failover status                  # controllers, states, paths
    nvmf-failover discover                # the array's view, matched to the host
    nvmf-failover monitor                 # watch events until Ctrl-C
    nvmf-failover fault fc-link host3 down
    nvmf-failover fault fc-link host3 up
    nvmf-failover run scenarios/fc-link-long.toml --io /dev/nvme21n5

A run writes `events.log`, `events.jsonl`, `summary.json` and, with
`--io`, `fio.json` to `runs/<time>-<scenario>/`. The exit status is 0 if
the run passed.

`--io` reads only; `--io-write` also writes and destroys the data on the
namespace.

## Scenarios

TOML files, see `scenarios/` and the docstring of
`src/nvmf_failover/scenario.py`:

```toml
name = "lpfc link down 60 s"
repeat = 1
recover_timeout = 300

[[steps]]
fault = "fc-link"
host = "host3"
action = "down"     # down, up or bounce
hold = 60           # seconds to wait after the step
```

## Development

    python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
    .venv/bin/ruff check src tests && .venv/bin/ruff format --check src tests
    .venv/bin/mypy src tests
    .venv/bin/pytest --cov

The tests need no hardware: they run against a fake sysfs tree and fake
command and ONTAP shells. `pre-commit install` runs the checks on commit.
