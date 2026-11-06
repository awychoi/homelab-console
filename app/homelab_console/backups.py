"""On-Pi PostgreSQL backups for a stack's update dialog.

Two different artifacts can live on the host:

* the stack's own backup job, read from ``<base>/<scheduled_subdir>``;
* optional fresh dumps the console writes under
  ``<base>/<console_subdir>`` so a pre-update copy is already on the Pi when
  it is needed for rollback.

Both are read from the host; no database dump travels through the Mac.
Profiles define the container, env keys and directory names; only
``pg_dumpall`` is executed, never an arbitrary command. The built-in Immich
plan is the default for a stack named ``immich``. Data volumes are never
included.
"""

from __future__ import annotations

import re
import shlex
import time
from pathlib import PurePosixPath
from typing import Any

from . import profiles, util
from .hosts import Host
from .sshkeys import ssh_argv
from .stacks import Stack


class BackupError(RuntimeError):
    pass


def plan_for(stack_name: str, settings=None, host: str = "") -> dict[str, str] | None:
    """Effective database plan: workspace profile, then the built-in default."""
    return profiles.effective_database_plan(settings, host, stack_name)


def _base_directory(stack: Stack, plan: dict[str, str]) -> str:
    location = str(stack.env.get(plan["upload_env"]) or "").strip()
    if not location:
        raise BackupError(f"{plan['upload_env']} is missing from the {stack.name} .env")
    if (
        not location.startswith("/")
        or ".." in PurePosixPath(location).parts
        or "\x00" in location
        or "\n" in location
        or "\r" in location
    ):
        raise BackupError(f"{plan['upload_env']} must be a plain absolute path")
    return location.rstrip("/")


def parse_scheduled_listing(output: str, kind: str = "scheduled") -> list[dict[str, Any]]:
    """Parse ``%T@|%s|%f`` lines from a host backup directory."""
    backups: list[dict[str, Any]] = []
    for line in str(output or "").splitlines():
        parts = line.strip().split("|", 2)
        if len(parts) != 3:
            continue
        stamp, size, name = parts
        try:
            created = int(float(stamp))
            length = int(size)
        except ValueError:
            continue
        if not name or length <= 0:
            continue
        version = ""
        match = re.search(r"-(v[^-]+)-pg([0-9.]+)\.sql\.gz$", name)
        if match:
            version = match.group(1)
        backups.append({
            "name": name,
            "bytes": length,
            "created": created,
            "created_iso": util.iso(created),
            "version": version,
            "kind": kind,
        })
    backups.sort(key=lambda item: item["created"], reverse=True)
    return backups


def _remote_backups(host: Host, directory: str, pattern: str, kind: str) -> dict[str, Any]:
    command = (
        f"find {shlex.quote(directory)} -maxdepth 1 -type f "
        f"-name {shlex.quote(pattern)} -printf '%T@|%s|%f\\n' 2>/dev/null || true"
    )
    result = util.run(ssh_argv(host) + [command], timeout=25)
    if result.returncode != 0:
        return {
            "available": False,
            "directory": directory,
            "backups": [],
            "latest": None,
            "error": (result.stderr or result.stdout or f"find exited {result.returncode}").strip()[:400],
        }
    backups = parse_scheduled_listing(result.stdout, kind=kind)
    for item in backups:
        item["path"] = f"{directory}/{item['name']}"
    return {
        "available": True,
        "directory": directory,
        "backups": backups[:8],
        "latest": backups[0] if backups else None,
    }


def list_scheduled_backups(host: Host, stack: Stack, settings=None) -> dict[str, Any]:
    """Read the stack's own scheduled database backups from the host."""
    plan = plan_for(stack.name, settings, stack.host)
    if plan is None:
        return {"available": False, "backups": [], "latest": None, "error": "no backup plan"}
    if host.local:
        return {"available": False, "backups": [], "latest": None, "error": "remote host required"}
    try:
        base = _base_directory(stack, plan)
    except BackupError as exc:
        return {"available": False, "backups": [], "latest": None, "error": str(exc)}
    directory = f"{base}/{plan['scheduled_subdir']}"
    return _remote_backups(host, directory, f"{plan['scheduled_prefix']}*.sql.gz", "scheduled")


def list_console_backups(host: Host, stack: Stack, settings=None) -> dict[str, Any]:
    """Read console-created pre-update dumps from the Pi."""
    plan = plan_for(stack.name, settings, stack.host)
    if plan is None:
        return {"available": False, "backups": [], "latest": None, "error": "no backup plan"}
    if host.local:
        return {"available": False, "backups": [], "latest": None, "error": "remote host required"}
    try:
        base = _base_directory(stack, plan)
    except BackupError as exc:
        return {"available": False, "backups": [], "latest": None, "error": str(exc)}
    directory = f"{base}/{plan['console_subdir']}"
    return _remote_backups(host, directory, f"{plan['console_prefix']}*.sql.gz", "console")


def summary(host: Host, stack: Stack, settings=None) -> dict[str, Any]:
    """Both backup sources for the update dialog."""
    plan = plan_for(stack.name, settings, stack.host)
    console = list_console_backups(host, stack, settings)
    return {
        "available": bool(console.get("available")),
        "label": plan["label"] if plan else "",
        "console": console,
        "scheduled": list_scheduled_backups(host, stack, settings),
    }


def dump_database(host: Host, stack: Stack, *, log=None, settings=None) -> dict[str, Any]:
    """Write a validated pg_dumpall on the Pi so rollback needs no Mac copy."""
    say = log or (lambda _message: None)
    plan = plan_for(stack.name, settings, stack.host)
    if plan is None:
        raise BackupError(f"no database backup plan for {stack.name}")
    if host.local:
        raise BackupError("database backups are only supported for remote hosts")
    user = str(stack.env.get(plan["user_key"]) or "").strip()
    if not user:
        raise BackupError(f"{plan['user_key']} is missing from the {stack.name} .env")
    base = _base_directory(stack, plan)
    directory = f"{base}/{plan['console_subdir']}"
    filename = f"{plan['console_prefix']}{time.strftime('%Y%m%dT%H%M%S')}.sql.gz"
    final = f"{directory}/{filename}"
    temporary = f"{directory}/.{filename}.part"

    script = f"""set -e
set -o pipefail
dir={shlex.quote(directory)}
final={shlex.quote(final)}
tmp={shlex.quote(temporary)}
mkdir -p "$dir"
chmod 700 "$dir" 2>/dev/null || true
trap 'rm -f "$tmp"' EXIT
docker exec {shlex.quote(plan['container'])} pg_dumpall --clean --if-exists -U {shlex.quote(user)} | gzip > "$tmp"
gzip -t "$tmp"
chmod 600 "$tmp"
mv "$tmp" "$final"
trap - EXIT
stat -c '%s|%Y|%n' "$final"
"""
    say(f"writing pg_dumpall to {host.alias}:{final}")
    result = util.run(ssh_argv(host) + [f"bash -lc {shlex.quote(script)}"], timeout=900)
    if result.returncode != 0:
        raise BackupError(
            f"pg_dumpall failed (rc={result.returncode}): "
            f"{(result.stderr or result.stdout or 'no error output').strip()[-600:]}"
        )
    output = (result.stdout or "").strip().splitlines()
    if not output:
        raise BackupError("pg_dumpall finished without a file result")
    try:
        size, created, path = output[-1].split("|", 2)
        backup = {
            "name": filename,
            "path": path,
            "bytes": int(size),
            "created": int(created),
            "created_iso": util.iso(int(created)),
            "kind": "console",
        }
    except (ValueError, IndexError) as exc:
        raise BackupError(f"could not parse the backup result: {exc}") from exc
    say(f"saved {path} ({backup['bytes']} bytes)")
    return {
        "backup": backup,
        "label": plan["label"],
        "storage": directory,
    }
