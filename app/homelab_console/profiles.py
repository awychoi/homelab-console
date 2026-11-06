"""Per-stack safety and backup metadata.

The original console keyed safety levels and database plans by the stack's
folder name, which only worked for the maintainer's fleet.  These built-ins
remain as defaults, but a workspace can override them per ``host/stack`` and
add a profile for any service.  Profiles are deliberately plain data (no shell
commands): the console validates every field before it is used.
"""

from __future__ import annotations

import re
from typing import Any

SAFETY_LEVELS = ("low", "medium", "high", "critical")
MAX_PROFILES = 200

HOST_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
STACK_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
CONTAINER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
SEGMENT_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
DOT_SEGMENTS = {".", ".."}
PREFIX_RE = re.compile(r"^[A-Za-z0-9._-]{0,64}$")

DEFAULT_SAFETY: dict[str, Any] = {
    "level": "medium",
    "data": [],
    "backup": "Snapshot the stack data directory before updating if the service stores state.",
    "dns_adjacent": False,
}

BUILTIN_SAFETY: dict[str, dict[str, Any]] = {
    "immich": {
        "level": "critical",
        "data": ["/mnt/raid/immich_data (library)", "Postgres data on RAID"],
        "backup": "Immich writes a scheduled DatabaseBackup dump to the RAID backups directory. Check the latest scheduled dump below; create a fresh on-demand copy if it is stale or missing. The photo library is additive and excluded.",
        "dns_adjacent": False,
    },
    "vault": {
        "level": "high",
        "data": ["/srv/vault/vw-data"],
        "backup": "Snapshot /srv/vault/vw-data (contains db.sqlite3) before updating.",
        "dns_adjacent": False,
    },
    "n8n": {
        "level": "high",
        "data": ["/srv/n8n/data"],
        "backup": "Snapshot /srv/n8n/data before updating; run one workflow afterwards.",
        "dns_adjacent": False,
    },
    "pihole": {
        "level": "high",
        "data": ["/srv/pihole/etc", "/srv/pihole/dnsmasq.d"],
        "backup": "Snapshot /srv/pihole before updating and verify dig resolution immediately after.",
        "dns_adjacent": True,
    },
    "unbound": {
        "level": "high",
        "data": [],
        "backup": "DNS-adjacent: confirm the sibling resolver still answers after the recreate.",
        "dns_adjacent": True,
    },
    "caddy": {
        "level": "medium",
        "data": ["caddy_data volume (internal CA and certificates)"],
        "backup": "Never prune the caddy_data volume; the internal CA for this host lives there.",
        "dns_adjacent": False,
    },
    "searxng": {
        "level": "low",
        "data": [],
        "backup": "No irreplaceable local state; the valkey cache is disposable.",
        "dns_adjacent": False,
    },
    "copyparty": {
        "level": "low",
        "data": ["/mnt/raid/myDATA (read-only from the container)"],
        "backup": "Library data is untouched by an image update; verify downloads afterwards.",
        "dns_adjacent": False,
    },
}

# PostgreSQL directory layout used by the update dialog.  The console only
# runs pg_dumpall; it never accepts an arbitrary command from a profile.
BUILTIN_DB_PLANS: dict[str, dict[str, str]] = {
    "immich": {
        "label": "Immich Postgres",
        "container": "immich_postgres",
        "user_key": "DB_USERNAME",
        "prefix": "immich-db",
        "upload_env": "UPLOAD_LOCATION",
        "scheduled_subdir": "backups",
        "scheduled_prefix": "immich-db-backup-",
        "console_subdir": "console-backups",
        "console_prefix": "console-immich-db-",
    },
}

DATABASE_DEFAULTS: dict[str, str] = {
    "label": "PostgreSQL",
    "container": "postgres",
    "user_key": "POSTGRES_USER",
    "upload_env": "DATA_DIR",
    "scheduled_subdir": "backups",
    "scheduled_prefix": "db-backup-",
    "console_subdir": "console-backups",
    "console_prefix": "console-db-",
}

_DATABASE_FIELDS = set(DATABASE_DEFAULTS) | {"prefix"}
_SAFETY_FIELDS = {"level", "data", "backup", "dns_adjacent"}


def profile_key(host: str, stack: str) -> str:
    return f"{host}/{stack}"


def validate_profile_key(key: Any) -> str:
    text = str(key or "").strip()
    host, sep, stack = text.partition("/")
    if not sep or not HOST_RE.match(host) or not STACK_RE.match(stack):
        raise ValueError("stack profile key must be <host>/<stack> with a valid alias and stack name")
    return text


def _clean_text(value: Any, field: str, limit: int, *, allow_empty: bool = True) -> str:
    text = str(value if value is not None else "").strip()
    if "\x00" in text or "\r" in text:
        raise ValueError(f"{field} contains invalid characters")
    if len(text) > limit:
        raise ValueError(f"{field} is too long (max {limit})")
    if not allow_empty and not text:
        raise ValueError(f"{field} is required")
    return text


def validate_safety(raw: Any, *, strict: bool = True) -> dict[str, Any]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("safety must be an object")
    unknown = set(raw) - _SAFETY_FIELDS
    if unknown and strict:
        raise ValueError(f"unknown safety field: {sorted(unknown)[0]}")
    values: dict[str, Any] = {}
    if "level" in raw:
        level = str(raw.get("level") or "").strip().lower()
        if level not in SAFETY_LEVELS:
            raise ValueError("safety.level must be low, medium, high or critical")
        values["level"] = level
    if "data" in raw:
        value = raw.get("data")
        if isinstance(value, str):
            value = [line.strip() for line in value.splitlines() if line.strip()]
        if not isinstance(value, (list, tuple)) or len(value) > 12:
            raise ValueError("safety.data must be a list of at most 12 paths")
        paths: list[str] = []
        for item in value:
            path = _clean_text(item, "safety.data", 300, allow_empty=False)
            if path not in paths:
                paths.append(path)
        values["data"] = paths
    if "backup" in raw:
        values["backup"] = _clean_text(raw.get("backup"), "safety.backup", 1000)
    if "dns_adjacent" in raw:
        values["dns_adjacent"] = bool(raw.get("dns_adjacent"))
    return values


def _validate_name(value: Any, field: str, pattern: re.Pattern[str], *, allow_empty: bool = False) -> str:
    text = _clean_text(value, field, 128, allow_empty=allow_empty)
    if text and not pattern.match(text):
        raise ValueError(f"{field} has an invalid format")
    return text


def validate_database(raw: Any, *, strict: bool = True) -> dict[str, str] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("database must be an object or null")
    unknown = set(raw) - _DATABASE_FIELDS
    if unknown and strict:
        raise ValueError(f"unknown database field: {sorted(unknown)[0]}")
    values = dict(DATABASE_DEFAULTS)
    value = _validate_name(raw.get("label", values["label"]), "database.label", re.compile(r"^.{1,80}$"))
    values["label"] = value
    values["container"] = _validate_name(
        raw.get("container", values["container"]), "database.container", CONTAINER_RE
    )
    values["user_key"] = _validate_name(
        raw.get("user_key", values["user_key"]), "database.user_key", ENV_KEY_RE
    )
    values["upload_env"] = _validate_name(
        raw.get("upload_env", values["upload_env"]), "database.upload_env", ENV_KEY_RE
    )
    for field in ("scheduled_subdir", "console_subdir"):
        values[field] = _validate_name(raw.get(field, values[field]), f"database.{field}", SEGMENT_RE)
        if values[field] in DOT_SEGMENTS:
            raise ValueError(f"database.{field} must not be a dot segment")
    for field in ("scheduled_prefix", "console_prefix", "prefix"):
        if field == "prefix" and field not in raw and field not in values:
            continue
        values[field] = _validate_name(raw.get(field, values.get(field, "")), f"database.{field}", PREFIX_RE, allow_empty=True)
    return values


def validate_stack_profile(raw: Any, *, strict: bool = True) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("profile must be an object")
    unknown = set(raw) - {"safety", "database"}
    if unknown and strict:
        raise ValueError(f"unknown profile field: {sorted(unknown)[0]}")
    profile: dict[str, Any] = {}
    if "safety" in raw:
        profile["safety"] = validate_safety(raw.get("safety"), strict=strict)
    if "database" in raw:
        profile["database"] = validate_database(raw.get("database"), strict=strict)
    if not profile:
        raise ValueError("profile must include safety or database")
    return profile


def normalize_stack_profiles(raw: Any, *, strict: bool = False) -> dict[str, dict[str, Any]]:
    if not isinstance(raw, dict):
        if strict:
            raise ValueError("stack_profiles must be an object")
        return {}
    profiles: dict[str, dict[str, Any]] = {}
    for key, value in raw.items():
        if len(profiles) >= MAX_PROFILES:
            if strict:
                raise ValueError(f"at most {MAX_PROFILES} stack profiles are supported")
            break
        try:
            clean_key = validate_profile_key(key)
            profile = validate_stack_profile(value, strict=strict)
        except ValueError:
            if strict:
                raise
            continue
        profiles[clean_key] = profile
    return profiles


def _custom_profile(settings: Any, host: str, stack_name: str) -> dict[str, Any] | None:
    if settings is None:
        return None
    raw = settings.get("stack_profiles") if hasattr(settings, "get") else None
    if not isinstance(raw, dict):
        return None
    value = raw.get(profile_key(host, stack_name))
    if not isinstance(value, dict):
        return None
    try:
        return validate_stack_profile(value, strict=False)
    except ValueError:
        return None


def builtin_database_plan(stack_name: str) -> dict[str, str] | None:
    plan = BUILTIN_DB_PLANS.get(stack_name)
    return dict(plan) if plan else None


def effective_database_plan(settings: Any, host: str, stack_name: str) -> dict[str, str] | None:
    custom = _custom_profile(settings, host, stack_name)
    if custom is not None and "database" in custom:
        value = custom["database"]
        return dict(value) if value else None
    return builtin_database_plan(stack_name)


def effective_safety(settings: Any, host: str, stack_name: str) -> dict[str, Any]:
    safety = {**DEFAULT_SAFETY, **BUILTIN_SAFETY.get(stack_name, {})}
    custom = _custom_profile(settings, host, stack_name)
    if custom and custom.get("safety"):
        safety.update(custom["safety"])
    plan = effective_database_plan(settings, host, stack_name)
    safety["database_backup"] = plan is not None
    safety["database_label"] = plan.get("label", "") if plan else ""
    return safety


def profile_view(settings: Any, host: str, stack_name: str) -> dict[str, Any]:
    custom = _custom_profile(settings, host, stack_name)
    database_source = "none"
    if custom is not None and "database" in custom:
        database_source = "custom"
    elif builtin_database_plan(stack_name):
        database_source = "built-in"
    return {
        "key": profile_key(host, stack_name),
        "custom": custom is not None,
        "custom_profile": custom,
        "safety": effective_safety(settings, host, stack_name),
        "database": effective_database_plan(settings, host, stack_name),
        "database_source": database_source,
        "builtin_database": builtin_database_plan(stack_name),
    }
