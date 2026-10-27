"""The configuration file: where the arrays and switches are, how to log in.

See config.toml.example. Keep the file readable by its owner only: it holds
passwords.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_PATH = Path("~/.config/nvmf-failover/config.toml")


class ConfigError(ValueError):
    """The configuration is missing or invalid."""


@dataclass(frozen=True)
class OntapConfig:
    host: str
    user: str
    password: str
    vserver: str = ""


@dataclass(frozen=True)
class PowerStoreConfig:
    host: str
    user: str
    password: str
    verify_tls: bool = True


@dataclass(frozen=True)
class BrocadeConfig:
    name: str
    host: str
    user: str
    password: str
    read_user: str = ""
    read_password: str = ""

    def reader(self) -> tuple[str, str]:
        """The account for read-only commands: the read user if configured."""
        if self.read_user:
            return self.read_user, self.read_password
        return self.user, self.password


@dataclass(frozen=True)
class Config:
    ontap: OntapConfig | None = None
    powerstore: PowerStoreConfig | None = None
    brocade: list[BrocadeConfig] = field(default_factory=list)


def load(path: Path | None = None) -> Config:
    """The configuration at @path, or an empty one if the default is missing."""
    explicit = path is not None
    path = (path or DEFAULT_PATH).expanduser()
    if not path.exists():
        if explicit:
            raise ConfigError(f"{path}: no such file")
        return Config()
    if path.stat().st_mode & 0o077:
        raise ConfigError(f"{path}: readable by others, chmod 600 it")
    try:
        data = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as err:
        raise ConfigError(f"{path}: {err}") from err
    return Config(
        ontap=_ontap(_table(data, "ontap", path), path),
        powerstore=_powerstore(_table(data, "powerstore", path), path),
        brocade=_brocade(data.get("brocade", []), path),
    )


def _table(data: dict[str, Any], name: str, path: Path) -> dict[str, Any] | None:
    section = data.get(name)
    if section is not None and not isinstance(section, dict):
        raise ConfigError(f"{path}: [{name}] must be a table")
    return section


def _need(section: dict[str, Any], key: str, where: str, path: Path) -> str:
    if key not in section:
        raise ConfigError(f"{path}: {where} needs {key}")
    return str(section[key])


def _password(
    section: dict[str, Any], prefix: str, where: str, path: Path, required: bool = True
) -> str:
    """@prefix (password) from the file, or from the variable @prefix_env."""
    if prefix in section:
        return str(section[prefix])
    env_key = f"{prefix}_env"
    if env_key in section:
        env = str(section[env_key])
        if env not in os.environ:
            raise ConfigError(f"{path}: ${env} is not set")
        return os.environ[env]
    if required:
        raise ConfigError(f"{path}: {where} needs {prefix} or {env_key}")
    return ""


def _ontap(section: dict[str, Any] | None, path: Path) -> OntapConfig | None:
    if section is None:
        return None
    return OntapConfig(
        host=_need(section, "host", "[ontap]", path),
        user=_need(section, "user", "[ontap]", path),
        password=_password(section, "password", "[ontap]", path),
        vserver=str(section.get("vserver", "")),
    )


def _powerstore(section: dict[str, Any] | None, path: Path) -> PowerStoreConfig | None:
    if section is None:
        return None
    return PowerStoreConfig(
        host=_need(section, "host", "[powerstore]", path),
        user=_need(section, "user", "[powerstore]", path),
        password=_password(section, "password", "[powerstore]", path),
        verify_tls=bool(section.get("verify_tls", True)),
    )


def _brocade(sections: object, path: Path) -> list[BrocadeConfig]:
    if not isinstance(sections, list):
        raise ConfigError(f"{path}: brocade must be [[brocade]] tables")
    out = []
    for i, section in enumerate(sections):
        where = f"[[brocade]] {i + 1}"
        if not isinstance(section, dict):
            raise ConfigError(f"{path}: {where} must be a table")
        host = _need(section, "host", where, path)
        out.append(
            BrocadeConfig(
                name=str(section.get("name", host)),
                host=host,
                user=_need(section, "user", where, path),
                password=_password(section, "password", where, path),
                read_user=str(section.get("read_user", "")),
                read_password=_password(
                    section, "read_password", where, path, required=False
                ),
            )
        )
    return out
