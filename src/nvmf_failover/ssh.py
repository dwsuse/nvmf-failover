"""ssh connections to the lab devices (paramiko, an optional dependency)."""

from __future__ import annotations

import socket
from pathlib import Path
from typing import Any

KNOWN_HOSTS = Path("~/.config/nvmf-failover/known_hosts")


def resolve(host: str) -> str:
    """An IPv4 address of @host if it has one, else @host.

    Lab names often resolve to an IPv6 address first that isn't routed
    from every host; the connection then hangs until it times out.
    """
    try:
        infos = socket.getaddrinfo(host, 22, socket.AF_INET, socket.SOCK_STREAM)
    except socket.gaierror:
        return host
    return str(infos[0][4][0]) if infos else host


def connect(
    host: str,
    user: str,
    password: str,
    timeout: float = 15,
    known_hosts: Path = KNOWN_HOSTS,
) -> Any:
    """A connected paramiko SSHClient, logged in with a password.

    Host keys are trusted on first use and kept in @known_hosts: a device
    whose key changed later is refused (paramiko's BadHostKeyException).
    """
    import paramiko

    known = known_hosts.expanduser()
    client = paramiko.SSHClient()
    if known.exists():
        client.load_host_keys(str(known))
    # trust on first use only: a key already in known_hosts must match
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())  # noqa: S507
    try:
        client.connect(
            resolve(host),
            username=user,
            password=password,
            look_for_keys=False,
            allow_agent=False,
            timeout=timeout,
        )
    except Exception:
        client.close()
        raise
    known.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    client.save_host_keys(str(known))
    return client


def run(client: Any, command: str, timeout: float = 60) -> tuple[str, str]:
    """stdout and stderr of @command."""
    _, stdout, stderr = client.exec_command(command, timeout=timeout)
    return (
        str(stdout.read().decode(errors="replace")),
        str(stderr.read().decode(errors="replace")),
    )
