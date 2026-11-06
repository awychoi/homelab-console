"""Fleet inventory: hosts.json compatibility and ~/.ssh/config ergonomics.

``scripts/hosts.json`` remains the tool-canonical map (deploy.py, fleet_run.py
and update_run.py read it), so the app reads *and writes that exact file*.
Unknown keys are preserved.  Only additive fields are introduced:

    port, key, role, tags, added

A host entry may be ``{"local": true}`` for the Mac itself, matching the
existing deploy.py behaviour.
"""

from __future__ import annotations

import json
import re
import shutil
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import util
from .config import Settings

ALIAS_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
USER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9._-]{0,31}$")
ADDRESS_RE = re.compile(r"^[A-Za-z0-9._:-]+$")

HOST_KEY_ORDER = ("user", "host", "port", "local", "key", "role", "tags", "notes", "added")
META_SUFFIX = "_"


class HostError(ValueError):
    pass


@dataclass
class Host:
    alias: str
    user: str = "pi"
    address: str = ""
    port: int = 22
    local: bool = False
    key: str = ""
    role: str = ""
    tags: list[str] = field(default_factory=list)
    notes: str = ""
    added: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def target(self) -> str:
        return "local" if self.local else f"{self.user}@{self.address}"

    @property
    def label(self) -> str:
        return self.alias

    @property
    def key_path(self) -> Path | None:
        return Path(self.key).expanduser() if self.key else None

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"user": self.user}
        if self.local:
            payload = {"local": True}
        else:
            payload["host"] = self.address
        if self.port and self.port != 22:
            payload["port"] = self.port
        if self.key:
            payload["key"] = self.key
        if self.role:
            payload["role"] = self.role
        if self.tags:
            payload["tags"] = list(self.tags)
        if self.notes:
            payload["notes"] = self.notes
        if self.added:
            payload["added"] = self.added
        # Unknown keys other scripts may care about survive round-trips.
        for key, value in self.extra.items():
            payload.setdefault(key, value)
        return payload

    def public(self) -> dict[str, Any]:
        return {
            "alias": self.alias,
            "user": self.user,
            "address": self.address,
            "port": self.port,
            "local": self.local,
            "key": self.key,
            "role": self.role,
            "tags": self.tags,
            "notes": self.notes,
            "added": self.added,
            "target": self.target,
        }


def validate_alias(alias: str) -> str:
    alias = (alias or "").strip().lower()
    if not ALIAS_RE.match(alias):
        raise HostError("alias must be lowercase letters/numbers/dashes, starting with a letter (max 32)")
    return alias


def validate_address(address: str) -> str:
    address = (address or "").strip()
    if not address or not ADDRESS_RE.match(address) or address.startswith("-") or ".." in address:
        raise HostError("address must be a plain hostname or IP (no spaces, schemes or dashes first)")
    if len(address) > 253:
        raise HostError("address is too long")
    return address


def validate_user(user: str) -> str:
    user = (user or "").strip()
    if not USER_RE.match(user):
        raise HostError("ssh user must be letters/numbers/underscore/dash/dot, starting with a letter or underscore")
    return user


def validate_key_path(key: str) -> str:
    text = str(key or "").strip()
    if any(char in text for char in ("\x00", "\r", "\n")):
        raise HostError("identity file path contains control characters")
    return text


def _tags(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [part.strip() for part in value.split(",") if part.strip()]
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item)[:32] for item in value if str(item).strip()][:12]


def validate_port(port: Any) -> int:
    try:
        value = int(port)
    except (TypeError, ValueError) as exc:
        raise HostError("port must be a number") from exc
    if not 1 <= value <= 65535:
        raise HostError("port must be between 1 and 65535")
    return value


class HostStore:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.path = settings.hosts_file
        self._raw: dict[str, Any] = {}
        self._hosts: dict[str, Host] = {}
        self._lock = threading.RLock()
        self.load()

    # ---- loading -------------------------------------------------------
    def load(self) -> None:
        raw = util.json_load(self.path, None)
        if raw is None and self.path.exists():
            raise HostError(f"hosts.json is not valid JSON: {self.path}")
        raw = raw if isinstance(raw, dict) else {}
        self._raw = raw
        hosts: dict[str, Host] = {}
        for alias, value in raw.items():
            if alias.endswith(META_SUFFIX) or not isinstance(value, dict):
                continue
            try:
                host = self._parse(alias, value)
                hosts[host.alias] = host
            except HostError:
                # Keep the file usable even if a row is malformed; it will be
                # visible as missing in the UI rather than crashing the fleet.
                continue
        self._hosts = hosts

    @staticmethod
    def _parse(alias: str, value: dict[str, Any]) -> Host:
        alias = str(alias or "").strip().lower()
        if not ALIAS_RE.match(alias):
            raise HostError("host alias has an invalid format")
        known = set(HOST_KEY_ORDER)
        extra = {k: v for k, v in value.items() if k not in known}
        local = bool(value.get("local"))
        address = "" if local else validate_address(str(value.get("host", "")))
        return Host(
            alias=alias,
            user=validate_user(str(value.get("user", "pi"))),
            address=address or ("127.0.0.1" if local else ""),
            port=validate_port(value.get("port", 22)),
            local=local,
            key=validate_key_path(str(value.get("key", "") or "")),
            role=str(value.get("role", "") or "")[:80],
            tags=[str(t)[:32] for t in (value.get("tags") or []) if isinstance(t, str)][:12],
            notes=str(value.get("notes", "") or "")[:500],
            added=str(value.get("added", "") or ""),
            extra=extra,
        )

    # ---- access --------------------------------------------------------
    def all(self) -> list[Host]:
        with self._lock:
            return [self._hosts[key] for key in self._hosts]

    def aliases(self) -> list[str]:
        with self._lock:
            return list(self._hosts)

    def get(self, alias: str) -> Host:
        with self._lock:
            host = self._hosts.get(alias)
        if host is None:
            raise HostError(f"unknown host '{alias}'")
        return host

    def remote(self) -> list[Host]:
        return [host for host in self.all() if not host.local]

    # ---- mutation ------------------------------------------------------
    def add(
        self,
        alias: str,
        address: str,
        user: str = "pi",
        port: int = 22,
        *,
        key: str = "",
        role: str = "",
        tags: list[str] | None = None,
        notes: str = "",
    ) -> Host:
        alias = validate_alias(alias)
        if alias in self._hosts:
            raise HostError(f"host '{alias}' already exists")
        host = Host(
            alias=alias,
            user=validate_user(user),
            address=validate_address(address),
            port=validate_port(port),
            key=validate_key_path(key),
            role=role[:80],
            tags=_tags(tags),
            notes=notes[:500],
            added=util.iso(),
        )
        self._hosts[alias] = host
        self.save()
        return host

    def update(self, alias: str, **changes: Any) -> Host:
        host = self.get(alias)
        if "address" in changes and not host.local:
            host.address = validate_address(str(changes["address"]))
        if "user" in changes:
            host.user = validate_user(str(changes["user"]))
        if "port" in changes:
            host.port = validate_port(changes["port"])
        if "key" in changes:
            host.key = validate_key_path(str(changes["key"] or ""))
        if "role" in changes:
            host.role = str(changes["role"] or "")[:80]
        if "tags" in changes:
            host.tags = [str(t)[:32] for t in (changes["tags"] or []) if isinstance(t, str)][:12]
        if "notes" in changes:
            host.notes = str(changes["notes"] or "")[:500]
        self.save()
        return host

    def remove(self, alias: str) -> None:
        self.get(alias)
        del self._hosts[alias]
        self.save()

    def save(self) -> None:
        with self._lock:
            self._save_locked()

    def _save_locked(self) -> None:
        payload: dict[str, Any] = {}
        # Preserve only top-level metadata (keys ending in "_", e.g.
        # "location_").  Never copy stale host entries back from _raw, or a
        # removed host would reappear on the next save.
        for key, value in self._raw.items():
            if key not in self._hosts and not isinstance(value, dict):
                payload[key] = value
        for alias in self._hosts:
            payload[alias] = self._hosts[alias].to_json()
        util.atomic_write_text(
            self.path,
            json.dumps(payload, indent=4, sort_keys=False) + "\n",
        )
        self._raw = payload

    def git_status(self) -> dict[str, Any]:
        result = util.run(
            [
                "git",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "protocol.ext.allow=never",
                "-C",
                str(self.settings.repo),
                "status",
                "--porcelain",
                "--",
                "scripts/hosts.json",
            ],
            timeout=10,
        )
        return {"dirty": bool(result.stdout.strip()), "porcelain": result.stdout.strip()}


# --------------------------------------------------------------- ssh config

CONFIG_MARKER = "# managed by Homelab Console"

STANZA_KEYS = {"hostname", "user", "port", "identityfile"}


def _ssh_dir() -> Path:
    return util.ssh_home()


def ssh_config_path() -> Path:
    return _ssh_dir() / "config"


def _stanza_bounds(lines: list[str], alias: str) -> tuple[int, int] | None:
    """Return [start, end) of an existing Host stanza for alias, if any."""
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.lower().startswith("host ") and not stripped.lower().startswith("hostname"):
            patterns = stripped.split()[1:]
            if alias in patterns:
                end = len(lines)
                for probe in range(index + 1, len(lines)):
                    candidate = lines[probe].strip()
                    if candidate and not lines[probe][:1].isspace() and not candidate.startswith("#"):
                        end = probe
                        break
                return index, end
    return None


def upsert_ssh_config(host: Host) -> dict[str, Any]:
    """Make ``ssh <alias>`` work, matching the user's fingers-canonical map.

    Existing stanzas are edited in place (first-match-wins in OpenSSH), new
    aliases are appended.  The file is backed up first.
    """
    if host.local:
        return {"changed": False, "path": str(ssh_config_path()), "reason": "local host"}
    directory = _ssh_dir()
    util.ensure_dir(directory, 0o700)
    if any(char in host.key for char in ("\x00", "\r", "\n")):
        raise HostError("identity file path contains control characters")
    path = ssh_config_path()
    original = util.read_text(path)
    lines = original.splitlines()

    desired: list[str] = [f"  HostName {host.address}", f"  User {host.user}", f"  Port {host.port}"]
    if host.key:
        desired += [f"  IdentityFile {host.key}", "  IdentitiesOnly yes"]

    bounds = _stanza_bounds(lines, host.alias)
    changed = False
    if bounds:
        start, end = bounds
        stanza = lines[start:end]
        rewritten: list[str] = []
        seen: set[str] = set()
        for line in stanza:
            key = line.strip().split()[0].lower() if line.strip() and not line.strip().startswith("#") else ""
            if key in STANZA_KEYS:
                if key in seen:
                    rewritten.append(line)
                    continue
                seen.add(key)
                if key == "hostname":
                    rewritten.append(f"  HostName {host.address}")
                elif key == "user":
                    rewritten.append(f"  User {host.user}")
                elif key == "port":
                    rewritten.append(f"  Port {host.port}")
                elif key == "identityfile":
                    if host.key:
                        rewritten.append(f"  IdentityFile {host.key}")
                    else:
                        rewritten.append(line)
            else:
                rewritten.append(line)
        if host.key and "identityfile" not in seen:
            insert_at = len(rewritten)
            while insert_at > 1 and not rewritten[insert_at - 1].strip():
                insert_at -= 1
            rewritten[insert_at:insert_at] = [f"  IdentityFile {host.key}", "  IdentitiesOnly yes"]
        host_line = stanza[0]
        patterns = host_line.split()[1:]
        if host.alias not in patterns:
            rewritten[0] = "Host " + " ".join([host.alias, *patterns])
        if rewritten != stanza:
            lines = lines[:start] + rewritten + lines[end:]
            changed = True
    else:
        if lines and lines[-1].strip():
            lines.append("")
        lines += [CONFIG_MARKER, f"Host {host.alias}", *desired]
        changed = True

    if changed:
        backup = None
        if path.exists():
            backup = path.with_name(f"config.hlc-backup-{util.now_ts()}")
            shutil.copy2(path, backup)
            backup.chmod(0o600)
            backups = sorted(path.parent.glob("config.hlc-backup-*"))
            for stale in backups[:-3]:
                stale.unlink(missing_ok=True)
        util.atomic_write_text(path, "\n".join(lines) + "\n", mode=0o600)
        return {"changed": True, "path": str(path), "backup": str(backup) if backup else None}
    return {"changed": False, "path": str(path)}


def ssh_config_entry(alias: str) -> dict[str, str] | None:
    """Effective-looking stanza for an alias, for display only."""
    lines = util.read_text(ssh_config_path()).splitlines()
    bounds = _stanza_bounds(lines, alias)
    if not bounds:
        return None
    start, end = bounds
    values: dict[str, str] = {}
    for line in lines[start + 1 : end]:
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and not parts[0].startswith("#"):
            values[parts[0].lower()] = parts[1]
    return values
