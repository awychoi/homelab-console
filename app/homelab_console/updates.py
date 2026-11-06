"""Update planning and execution: pin bump -> push -> recreate -> verify.

The flow is intentionally conservative:

    snapshot repo files -> mutate pin -> validate compose in staging
    -> push -> pull only the affected services -> up -d -> wait for health
    -> on failure restore files, push, up -d again (compose-only rollback)

Data backups are NOT performed automatically.  Every target carries the
effective stack safety profile (built-in defaults plus any workspace override)
telling the operator what to back up; the UI requires an explicit
acknowledgement for critical stacks.  A profile may also enable a Pi-local
PostgreSQL dump from the update dialog.
"""

from __future__ import annotations

import ipaddress
import json
import re
import shlex
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import profiles, releases, remote, stacks, util
from .config import Settings
from .hosts import Host
from .store import Store

SNAPSHOT_RE = re.compile(r"^\d{8}-\d{6}-[a-z0-9-]+-[a-z0-9._-]+$")
IMAGE_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]*$")

# Built-in safety levels live in ``profiles`` so a workspace can override them
# per host/stack.  The aliases stay for callers that imported them directly.
SAFETY = profiles.BUILTIN_SAFETY
DEFAULT_SAFETY = profiles.DEFAULT_SAFETY


ACTION_LABELS = {
    "update": "Update & deploy",
    "pin-latest": "Pin to latest stable",
    "pin-digest": "Pin to running digest",
}


class UpdateError(RuntimeError):
    def __init__(self, message: str, *, rolled_back: bool = False, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.rolled_back = rolled_back
        self.details = details or {}


# --------------------------------------------------------------- planning


def safety_for(stack_name: str, settings=None, host: str = "") -> dict[str, Any]:
    """Effective safety for one stack, including a workspace profile."""
    return profiles.effective_safety(settings, host, stack_name)


def _format_latest(current: str, latest: str, source_kind: str) -> str:
    value = latest.strip()
    value = re.sub(r"^[A-Za-z][A-Za-z0-9_.-]*@", "", value)  # n8n@2.4.0 -> 2.4.0
    value = re.sub(r"^release[-_/]", "", value, flags=re.IGNORECASE)
    if source_kind == "github":
        if current.startswith("v") and not value.startswith("v"):
            return "v" + value
        if current and not current.startswith("v") and value.startswith("v"):
            return value[1:]
    return value


def _choose_latest(entry: releases.ReleaseEntry, current: str) -> str:
    variants = entry.extra.get("latest_by_variant") if entry.extra else None
    if isinstance(variants, dict) and variants:
        variant = releases.variant_of(current)
        key = variant.lstrip("-") or "plain"
        return str(variants.get(key) or variants.get("plain") or entry.latest or "")
    return entry.latest or ""


def _latest_published(entry: releases.ReleaseEntry, current: str) -> int:
    if not entry:
        return 0
    variants = entry.extra.get("latest_published_by_variant") if entry.extra else None
    if isinstance(variants, dict) and variants:
        variant = releases.variant_of(current)
        key = variant.lstrip("-") or "plain"
        stamp = variants.get(key) or variants.get("plain")
        if stamp:
            return int(stamp)
    return int(entry.published_at or 0)


def _env_file_for(stack: stacks.Stack, service: stacks.ServiceSpec, key: str) -> str:
    candidates = [".env", *service.env_files]
    seen = set()
    safe_candidates: list[str] = []
    for candidate in candidates:
        try:
            rel = stacks.validate_rel_path(candidate).as_posix()
        except stacks.StackError:
            # A compose env_file that escapes the stack is ignored rather than
            # followed.  The update target then stays on a normal .env.
            continue
        if rel in seen:
            continue
        seen.add(rel)
        safe_candidates.append(rel)
        if key in stacks.parse_env(stack.files.get(rel, "")):
            return rel
    if ".env" in stack.files:
        return ".env"
    return safe_candidates[0] if safe_candidates else ".env"


def _digest_for(container: dict[str, Any] | None, repo: str) -> str:
    if not container:
        return ""
    target = releases.normalize_key(repo)
    for ref in container.get("repo_digests") or []:
        base, _, digest = str(ref).partition("@")
        if not digest:
            continue
        try:
            image = stacks.parse_image(base)
        except Exception:
            continue
        if releases.normalize_key(image.repo) == target:
            return digest
    return str(container.get("digest") or "")


def build_targets(
    stack: stacks.Stack,
    runtime: dict[str, Any] | None,
    release_service: releases.ReleaseService,
    host_arch: str = "",
) -> list[dict[str, Any]]:
    """One entry per updatable unit (shared env pins are collapsed).

    ``host_arch`` is the target host's ``uname -m``.  When Docker Hub exposes
    platform metadata, only tags with a linux image for that CPU become
    actionable; a newer tag that still lacks an arm64/amd64 build is reported
    separately instead of being pushed and failing on pull.
    """
    arch = releases.normalize_arch(host_arch)
    containers = {}
    labels_by_image: dict[str, dict[str, str]] = {}
    for container in (runtime or {}).get("containers", []):
        service = str(container.get("service") or "")
        if service:
            containers[service] = container
        if container.get("image"):
            labels_by_image[str(container["image"])] = container.get("image_labels") or {}
    settings = getattr(release_service, "settings", None)
    groups = release_service.stack_groups(stack, labels_by_image)
    safety = safety_for(stack.name, settings, stack.host)
    rows: list[dict[str, Any]] = []
    merged_env: dict[tuple[str, str], dict[str, Any]] = {}

    for service in stack.services:
        image = service.image
        if image is None:
            continue
        container = containers.get(service.name)
        labels = (container or {}).get("image_labels") or {}
        source = release_service.source_for(image.repo, labels, groups)
        entry = release_service.entries.get(source.key)
        running_tag = str((container or {}).get("image_tag") or "")
        running_digest = _digest_for(container, image.repo)
        mechanism = "env" if image.variable else "image"
        file = ""
        key = ""
        current = ""
        digest_only = False
        if mechanism == "env":
            key = image.variable or ""
            file = _env_file_for(stack, service, key)
            current = stack.env.get(key, "")
        else:
            key = str(service.image_line if service.image_line is not None else "")
            file = stack.compose_file
            current = image.tag if image.explicit_tag else ""
            digest_only = bool(image.digest) and not image.explicit_tag

        latest_upstream = _choose_latest(entry, current or running_tag) if entry and not entry.error else ""
        latest = latest_upstream
        latest_published = (
            _latest_published(entry, current or running_tag) if latest_upstream else 0
        )
        platform_limited = False
        platform_unavailable = False
        if arch and latest_upstream:
            compatible, compatible_published, known = releases.tag_latest_for_arch(
                entry, current or running_tag, arch
            )
            if known:
                if compatible:
                    latest = compatible
                    if compatible == latest_upstream:
                        latest_published = latest_published or compatible_published
                    else:
                        latest_published = compatible_published
                else:
                    latest = ""
                    latest_published = 0
                    platform_unavailable = True
                platform_limited = bool(
                    not compatible
                    or (releases.compare_versions(compatible, latest_upstream) or 0) > 0
                )
        latest_age = max(0, util.now_ts() - latest_published) if latest_published else 0
        if source.kind == "managed":
            status = "managed"
        elif source.kind == "unknown":
            status = "unmanaged"
        elif entry is None:
            status = "not-checked"
        elif entry.error and not latest:
            status = "check-failed"
        elif platform_unavailable:
            status = "arch-unavailable"
        else:
            status = releases.status_for(current, latest, digest_only=digest_only)

        formatted = _format_latest(current, latest, source.kind) if latest else ""
        if mechanism == "image" and formatted:
            new_value = f"{image.repo}:{formatted}"
        else:
            new_value = formatted
        pin_latest = ""
        if latest:
            pin_latest = f"{image.repo}:{formatted}" if mechanism == "image" else formatted
        digest_value = f"{image.repo}@{running_digest}" if running_digest and mechanism == "image" else ""

        actions: list[str] = []
        if status == "update-available":
            actions.append("update")
        if status in ("unpinned", "digest-pinned") and pin_latest:
            actions.append("pin-latest")
        if (
            digest_value
            and mechanism == "image"
            and status == "unpinned"
            and source.kind not in ("managed", "unknown")
        ):
            actions.append("pin-digest")

        drift = bool(
            running_tag
            and current
            and running_tag.lower() not in ("latest", "<none>")
            and current.lower() not in ("latest", "<none>")
            and running_tag != current
        )
        notes = []
        if entry and entry.notes:
            notes.append(entry.notes)
        elif source.notes:
            notes.append(source.notes)
        if drift:
            notes.append(f"running image is {running_tag}; repo pins {current}")
        if running_tag.lower() == "latest":
            notes.append("container is running the :latest tag — not reproducible")
        if safety["dns_adjacent"]:
            notes.append("DNS-adjacent: keep the change window short and check resolution")
        if status == "ahead-of-stable":
            notes.append("installed version is ahead of the latest stable tag")
        if platform_limited:
            target_tag = f"linux/{arch}"
            if latest:
                notes.append(
                    f"upstream {latest_upstream} has no {target_tag} image; "
                    f"{latest} is the newest usable tag for this host"
                )
            else:
                notes.append(
                    f"upstream {latest_upstream} has no {target_tag} image; "
                    "no compatible build is published yet"
                )

        row = {
            "host": stack.host,
            "stack": stack.name,
            "service": service.name,
            "services_affected": [service.name],
            "mechanism": mechanism,
            "file": file,
            "key": key,
            "current": current,
            "current_display": current or ("(not set)" if mechanism == "env" else "(no tag)"),
            "running_tag": running_tag,
            "running_digest": running_digest,
            "latest": latest,
            "upstream_latest": latest_upstream,
            "platform": arch,
            "platform_limited": platform_limited,
            "latest_published": latest_published,
            "latest_published_iso": util.iso(latest_published) if latest_published else "",
            "latest_age_s": latest_age,
            "status": status,
            "source": source.public(),
            "checked_at": entry.checked_at if entry else 0,
            "checked_iso": util.iso(entry.checked_at) if entry and entry.checked_at else "",
            "url": entry.url if entry else "",
            "error": entry.error if entry else "",
            "guidance": entry.guidance if entry else source.guidance,
            "managed_by": source.managed_by,
            "new_value": new_value,
            "pin_latest": pin_latest,
            "digest_value": digest_value,
            "actions": actions,
            "action_labels": {action: ACTION_LABELS[action] for action in actions},
            "drift": drift,
            "notes": notes,
            "safety": safety,
            "image": service.image_raw,
        }
        if mechanism == "env":
            merge_key = (file, key)
            if merge_key in merged_env:
                existing = merged_env[merge_key]
                existing["services_affected"].append(service.name)
                # A shared pin is only updatable if every affected service's
                # release source agreed; Immich server + ML track one repo.
                if not actions and existing["actions"]:
                    pass
                continue
            merged_env[merge_key] = row
            rows.append(row)
        else:
            rows.append(row)

    order = {"update-available": 0, "unpinned": 1, "digest-pinned": 2, "check-failed": 3,
             "arch-unavailable": 4, "unknown": 5, "not-checked": 6, "ahead-of-stable": 7,
             "current": 8, "managed": 9, "unmanaged": 10}
    rows.sort(key=lambda row: (order.get(str(row["status"]), 9), row["service"]))
    for row in rows:
        row["review_hashes"] = {
            action: review_hash(row, action) for action in (row.get("actions") or [])
        }
    return rows


# ---------------------------------------------------------------- mutation


def review_value(target: dict[str, Any], action: str) -> str:
    if action == "update":
        return str(target.get("new_value") or "")
    if action == "pin-latest":
        return str(target.get("pin_latest") or "")
    if action == "pin-digest":
        return str(target.get("digest_value") or "")
    return ""


def review_hash(target: dict[str, Any], action: str) -> str:
    """Bind an update action to the exact mutation the UI reviewed.

    This is the update-flow equivalent of the deploy plan hash.  It is not a
    secret or a substitute for the session token; it exists so a target that
    changed between the dialog and the apply is rejected.
    """
    payload = {
        "host": target.get("host", ""),
        "stack": target.get("stack", ""),
        "service": target.get("service", ""),
        "services_affected": sorted(str(item) for item in (target.get("services_affected") or [])),
        "mechanism": target.get("mechanism", ""),
        "file": target.get("file", ""),
        "key": target.get("key", ""),
        "current": target.get("current", ""),
        "action": action,
        "value": review_value(target, action),
    }
    return util.sha256_text(util.stable_json(payload))


def replace_image(text: str, service_name: str, new_ref: str) -> str:
    if not IMAGE_REF_RE.match(new_ref):
        raise UpdateError(f"refusing suspicious image reference: {new_ref!r}")
    services, _, error = stacks.parse_compose(text)
    if error:
        raise UpdateError(error)
    spec = next((item for item in services if item.name == service_name), None)
    if spec is None or spec.image_line is None:
        raise UpdateError(f"service {service_name!r} has no image line")
    lines = text.splitlines()
    line = lines[spec.image_line]
    match = re.match(r"^(\s*image\s*:\s*)([\"']?)([^\"'\s#]+)\2(\s*(?:#.*)?)$", line)
    if not match:
        raise UpdateError(f"cannot safely rewrite image line: {line!r}")
    _, quote, _, suffix = match.groups()
    lines[spec.image_line] = f"{match.group(1)}{quote}{new_ref}{quote}{suffix}"
    return "\n".join(lines) + ("\n" if text.endswith("\n") else "")


def replace_env_value(text: str, key: str, new_value: str) -> str:
    if "\n" in new_value or "\r" in new_value:
        raise UpdateError("env value must be a single line")
    pattern = re.compile(rf"^(\s*{re.escape(key)}\s*=\s*)(.*)$")
    lines = text.splitlines()
    for index, line in enumerate(lines):
        match = pattern.match(line)
        if not match:
            continue
        prefix, rest = match.groups()
        value, comment = _split_value_comment(rest)
        quote = value[0] if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"" else ""
        suffix = ""
        if comment:
            suffix = " " + comment
        lines[index] = f"{prefix}{quote}{new_value}{quote}{suffix}"
        return "\n".join(lines) + ("\n" if text.endswith("\n") else "")
    # No existing line: append, which is valid for compose .env resolution.
    lines.append(f"{key}={new_value}")
    return "\n".join(lines) + "\n"


def _split_value_comment(rest: str) -> tuple[str, str]:
    quote = ""
    for index, char in enumerate(rest):
        if char in "'\"":
            if not quote:
                quote = char
            elif quote == char:
                quote = ""
        elif char == "#" and not quote and (index == 0 or rest[index - 1].isspace()):
            return rest[:index].rstrip(), rest[index:].rstrip()
    return rest.strip(), ""


@dataclass
class Mutation:
    file: str
    old_content: str
    new_content: str
    created: bool
    service: str
    action: str
    value: str

    def summary(self) -> dict[str, Any]:
        return {
            "file": self.file,
            "created": self.created,
            "service": self.service,
            "action": self.action,
            "value": self.value,
        }


def plan_mutation(stack: stacks.Stack, target: dict[str, Any], action: str) -> Mutation:
    if action == "update":
        value = target.get("new_value") or ""
    elif action == "pin-latest":
        value = target.get("pin_latest") or ""
    elif action == "pin-digest":
        value = target.get("digest_value") or ""
    else:
        raise UpdateError(f"unknown update action: {action}")
    if not value:
        raise UpdateError(f"no {action} value is available for {stack.name}/{target.get('service')}")
    file = str(target.get("file") or stack.compose_file)
    try:
        stacks.validate_rel_path(file)
    except stacks.StackError as exc:
        raise UpdateError(f"unsafe update path: {file!r}") from exc
    old = stack.files.get(file)
    created = old is None
    if target.get("mechanism") == "env":
        new = replace_env_value(old or "", str(target.get("key")), value)
    else:
        new = replace_image(stack.text if file == stack.compose_file else (old or ""), str(target.get("service")), value)
    if new == old:
        raise UpdateError("the computed change is a no-op")
    return Mutation(file=file, old_content=old or "", new_content=new, created=created,
                    service=str(target.get("service")), action=action, value=value)


def _diff_text(old: str, new: str, limit: int = 40) -> list[str]:
    import difflib

    return list(
        difflib.unified_diff(
            old.splitlines(), new.splitlines(), fromfile="before", tofile="after", lineterm="", n=2
        )
    )[:limit]


# ---------------------------------------------------------------- snapshots


def _snapshot_root(settings: Settings) -> Path:
    return util.ensure_dir(settings.backups_dir, 0o700)


def create_snapshot(settings: Settings, stack: stacks.Stack, target: dict[str, Any], action: str) -> dict[str, Any]:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    snapshot_id = f"{stamp}-{stack.host}-{stack.name}"
    root = _snapshot_root(settings) / snapshot_id
    if root.exists():
        snapshot_id = f"{snapshot_id}-{int(time.time() * 1000) % 1000}"
        root = _snapshot_root(settings) / snapshot_id
    files_root = root / "files"
    util.ensure_dir(files_root, 0o700)
    for rel, text in stack.files.items():
        destination = files_root / rel
        util.ensure_dir(destination.parent, 0o700)
        util.atomic_write_text(destination, text, mode=0o600)
    target_value = {
        "update": target.get("new_value"),
        "pin-latest": target.get("pin_latest"),
        "pin-digest": target.get("digest_value"),
    }.get(action) or ""
    manifest_target = {**target, "value": target_value}
    manifest = {
        "snapshot_id": snapshot_id,
        "created": util.now_ts(),
        "created_iso": util.iso(),
        "host": stack.host,
        "stack": stack.name,
        "files": sorted(stack.files),
        "hashes": stack.hashes,
        "target": manifest_target,
        "action": action,
        "git": stacks.git_status(settings),
        "reason": "pre-update snapshot",
    }
    util.json_dump(root / "manifest.json", manifest, mode=0o600)
    _prune_snapshots(settings, stack, keep=12)
    return {"id": snapshot_id, "path": str(root), "manifest": manifest}


def _prune_snapshots(settings: Settings, stack: stacks.Stack, keep: int = 12) -> None:
    root = _snapshot_root(settings)
    candidates = []
    for child in root.iterdir():
        if not child.is_dir() or not SNAPSHOT_RE.match(child.name):
            continue
        manifest = util.json_load(child / "manifest.json", {}) or {}
        if manifest.get("host") == stack.host and manifest.get("stack") == stack.name:
            candidates.append(child)
    for child in sorted(candidates, reverse=True)[keep:]:
        import shutil

        shutil.rmtree(child, ignore_errors=True)


def list_snapshots(settings: Settings, host: str, stack: str) -> list[dict[str, Any]]:
    root = _snapshot_root(settings)
    rows = []
    if not root.is_dir():
        return rows
    for child in root.iterdir():
        if not child.is_dir() or not SNAPSHOT_RE.match(child.name):
            continue
        manifest = util.json_load(child / "manifest.json", {}) or {}
        if manifest.get("host") != host or manifest.get("stack") != stack:
            continue
        rows.append(
            {
                "id": child.name,
                "created": manifest.get("created", 0),
                "created_iso": manifest.get("created_iso", ""),
                "action": manifest.get("action", ""),
                "target": manifest.get("target", {}),
                "files": manifest.get("files", []),
            }
        )
    return sorted(rows, key=lambda row: row["created"], reverse=True)


def restore_snapshot_files(settings: Settings, snapshot_id: str, stack: stacks.Stack) -> list[str]:
    if not SNAPSHOT_RE.match(snapshot_id):
        raise UpdateError("invalid snapshot id")
    root = _snapshot_root(settings) / snapshot_id
    manifest = util.json_load(root / "manifest.json", {}) or {}
    if manifest.get("host") != stack.host or manifest.get("stack") != stack.name:
        raise UpdateError("snapshot does not belong to this stack")
    files_root = root / "files"
    restored = []
    for rel in manifest.get("files", []):
        try:
            target = stacks.safe_stack_path(stack.path, str(rel))
            source = stacks.safe_stack_path(files_root, str(rel))
        except stacks.StackError:
            continue
        if not source.is_file() or source.is_symlink():
            continue
        util.atomic_write_text(target, source.read_text(encoding="utf-8"))
        restored.append(str(rel))
    # Anything the mutation created (e.g. a new .env) is not in the snapshot.
    target = manifest.get("target") or {}
    created = manifest.get("created_files") or []
    if target.get("file") and target.get("file") not in manifest.get("files", []) and target.get("file") not in created:
        created.append(target["file"])
    for rel in created:
        if not rel or rel in manifest.get("files", []):
            continue
        try:
            target = stacks.safe_stack_path(stack.path, str(rel))
        except stacks.StackError:
            continue
        target.unlink(missing_ok=True)
    return restored


# ------------------------------------------------------------------ health


def compose_ps(host: Host, stack_name: str, remote_root: str) -> list[dict[str, Any]]:
    directory = f"{remote_root.rstrip('/')}/{stack_name}"
    result = remote.remote_shell(
        host,
        f"cd {shlex.quote(directory)} && docker compose ps --format json",
        timeout=30,
    )
    output = (result.stdout or "").strip()
    if not output:
        return []
    rows: list[dict[str, Any]] = []
    try:
        if output.startswith("["):
            payload = json.loads(output)
            rows = payload if isinstance(payload, list) else []
        else:
            for line in output.splitlines():
                line = line.strip()
                if line.startswith("{"):
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        continue
    except ValueError:
        return []
    return rows


def _tcp_ports(specs: list[stacks.ServiceSpec]) -> list[tuple[str, str]]:
    checks = []
    for spec in specs:
        if not stacks.SERVICE_NAME_RE.match(spec.name or ""):
            continue
        for port in spec.ports:
            if port.get("protocol") != "tcp":
                continue
            host_port = port.get("host_port") or ""
            if not host_port.isdigit() or not 0 < int(host_port) < 65536:
                continue
            address = port.get("host_ip") or "127.0.0.1"
            if address in ("0.0.0.0", "::", ""):
                address = "127.0.0.1"
            try:
                parsed = ipaddress.ip_address(address)
            except ValueError:
                continue
            if parsed.version != 4 or parsed.is_unspecified:
                continue
            checks.append((spec.name, f"{parsed}:{host_port}"))
    return checks


def _tcp_probe_fragment(address: str) -> str:
    """Return the bash fragment that opens and closes a TCP connection.

    ``/dev/tcp`` is only special as a redirection target: running the path as
    a command word makes bash look for a file and fail with rc 127 even when
    the port is open.  ``exec 3<>`` is the redirection form, and the fd closes
    when the short-lived shell exits.  (Regression: v3.3.6 updates rolled back
    while every container was healthy because of exactly that mistake.)
    """
    host, _, port = address.rpartition(":")
    probe = host or "127.0.0.1"
    if probe in ("0.0.0.0", "::"):
        probe = "127.0.0.1"
    return f"exec 3<>/dev/tcp/{probe}/{port}"


def _port_probe(host: Host, ports: list[tuple[str, str]]) -> dict[str, Any]:
    if not ports or host.local:
        return {"checked": 0, "ok": True, "failures": []}
    script_parts = []
    for service, address in ports:
        _, _, port = address.rpartition(":")
        target = shlex.quote(_tcp_probe_fragment(address))
        ok_label = shlex.quote(f"{service}:{port}:OK")
        fail_label = shlex.quote(f"{service}:{port}:FAIL")
        script_parts.append(
            f"if timeout 3 bash -c {target} 2>/dev/null; then "
            f"echo {ok_label}; else echo {fail_label}; fi"
        )
    result = remote.remote_shell(host, "; ".join(script_parts), timeout=20)
    failures = []
    for line in (result.stdout or "").splitlines():
        line = line.strip()
        if line.endswith(":FAIL"):
            failures.append(line.rsplit(":", 1)[0])
    return {"checked": len(ports), "ok": not failures, "failures": failures}


def describe_health(health: dict[str, Any]) -> str:
    """One-line health summary, including a failed published-port probe."""
    states = health.get("states") or {}
    summary = ", ".join(f"{key}={value}" for key, value in states.items()) or "no services"
    failures = health.get("port_failures") or []
    if failures:
        summary += f"; port probe failed: {', '.join(str(item) for item in failures)}"
    return summary


def wait_healthy(
    host: Host,
    stack: stacks.Stack,
    remote_root: str,
    log: Callable[[str], None],
    *,
    timeout: int = 120,
    poll: int = 5,
) -> dict[str, Any]:
    expected = set(stack.service_names)
    deadline = time.time() + max(20, timeout)
    last_report = 0.0
    last: dict[str, Any] = {}
    specs = stack.services
    ports = _tcp_ports(specs)
    while time.time() < deadline:
        rows = compose_ps(host, stack.name, remote_root)
        by_service = {}
        for row in rows:
            by_service[str(row.get("Service") or row.get("Name") or "")] = row
        states = {}
        all_running = True
        all_healthy = True
        for service in expected:
            row = by_service.get(service)
            if row is None:
                states[service] = "missing"
                all_running = False
                all_healthy = False
                continue
            state = str(row.get("State") or "").lower()
            health = str(row.get("Health") or "").lower()
            states[service] = f"{state}{('/' + health) if health else ''}"
            if state != "running":
                all_running = False
                all_healthy = False
            elif health in ("unhealthy", "starting"):
                all_healthy = False
        last = {"services": states, "ports": [], "port_failures": []}
        if all_running and all_healthy:
            probe = _port_probe(host, ports)
            last["ports"] = probe
            if probe["ok"]:
                log(f"   healthy: {', '.join(f'{k}={v}' for k, v in states.items())}")
                return {"ok": True, "states": states, "ports": probe}
            last["port_failures"] = probe["failures"]
        if time.time() - last_report > 15:
            detail = ""
            if last.get("port_failures"):
                detail = f" · ports failing: {', '.join(str(item) for item in last['port_failures'])}"
            log(f"   waiting: {', '.join(f'{k}={v}' for k, v in states.items())}{detail}")
            last_report = time.time()
        time.sleep(poll)
    return {
        "ok": False,
        "states": last.get("services", {}),
        "ports": last.get("ports", []),
        "port_failures": last.get("port_failures", []),
        "timeout": timeout,
    }


# ------------------------------------------------------------------ deploy


def _log_output(result, log: Callable[[str], None], prefix: str = "   ") -> None:
    for line in ((result.stdout or "") + (result.stderr or "")).splitlines():
        if line.strip():
            log(prefix + line.strip()[:500])


def _push(host: Host, stack: stacks.Stack, remote_root: str, log: Callable[[str], None]) -> dict[str, Any]:
    log(f"   pushing {len(stack.files)} file(s) to {remote_root}/{stack.name}")
    result = stacks.push_files(host, stack.name, stack.files, remote_root, base=stack.path)
    if result.get("repaired"):
        log("   repaired remote file ownership (a container had chowned the stack directory)")
    if not result.get("ok"):
        raise UpdateError(
            f"push failed: {result.get('error')}",
            details={"written": bool(result.get("written"))},
        )
    for rel in sorted(stack.files):
        log(f"   → {rel}")
    return result


def _compose_recreate(host: Host, stack: stacks.Stack, remote_root: str, services: list[str], log: Callable[[str], None]) -> None:
    directory = f"{remote_root.rstrip('/')}/{stack.name}"
    quoted = " ".join(shlex.quote(service) for service in services if service)
    pull_cmd = f"cd {shlex.quote(directory)} && docker compose pull {quoted}".strip()
    log(f"   $ docker compose pull {quoted}")
    pulled = remote.remote_shell(host, pull_cmd, timeout=600)
    _log_output(pulled, log)
    if pulled.returncode != 0:
        detail = f"{pulled.stdout or ''}\n{pulled.stderr or ''}"
        if "no matching manifest" in detail:
            log("   ! the tag has no image for this host's CPU architecture")
        raise UpdateError(f"docker compose pull failed (rc={pulled.returncode})")
    up_cmd = f"cd {shlex.quote(directory)} && docker compose up -d --remove-orphans"
    log("   $ docker compose up -d --remove-orphans")
    up = remote.remote_shell(host, up_cmd, timeout=300)
    _log_output(up, log)
    if up.returncode != 0:
        raise UpdateError(f"docker compose up failed (rc={up.returncode})")


def _compose_up(host: Host, stack: stacks.Stack, remote_root: str, log: Callable[[str], None]) -> None:
    directory = f"{remote_root.rstrip('/')}/{stack.name}"
    result = remote.remote_shell(
        host, f"cd {shlex.quote(directory)} && docker compose up -d --remove-orphans", timeout=300
    )
    _log_output(result, log)
    if result.returncode != 0:
        raise UpdateError(f"docker compose up failed (rc={result.returncode})")


def perform_update(
    settings: Settings,
    store: Store,
    host: Host,
    stack: stacks.Stack,
    target: dict[str, Any],
    *,
    action: str,
    log: Callable[[str], None],
    acknowledge: bool = False,
) -> dict[str, Any]:
    safety = target.get("safety") or DEFAULT_SAFETY
    if safety.get("level") == "critical" and not acknowledge:
        raise UpdateError(
            f"{stack.host}/{stack.name} is a critical stack — confirm the data backup acknowledgement first",
            details={"requires_acknowledge": True, "safety": safety},
        )
    if action not in (target.get("actions") or []):
        raise UpdateError(f"action {action!r} is not available for this target (stale plan?)")
    mutation = plan_mutation(stack, target, action)
    snapshot = create_snapshot(settings, stack, target, action)
    log(f">> update {stack.host}/{stack.name} · {target.get('service')} · {action}")
    log(f"   snapshot: {snapshot['id']}")
    log(f"   {mutation.file}: {mutation.value}")
    for line in _diff_text(mutation.old_content, mutation.new_content):
        log("   " + line)

    created = [mutation.file] if mutation.created else []
    host_written = False
    manifest_path = Path(snapshot["path"]) / "manifest.json"
    manifest = snapshot["manifest"]
    manifest["created_files"] = created
    util.json_dump(manifest_path, manifest, mode=0o600)

    try:
        stacks.safe_stack_path(stack.path, mutation.file)
        util.atomic_write_text(
            stacks.safe_stack_path(stack.path, mutation.file),
            mutation.new_content,
            mode=None if not mutation.created else 0o600,
        )
        updated = stacks.get_stack(settings, stack.host, stack.name)
        if updated.parse_error:
            raise UpdateError(f"compose parse failed after edit: {updated.parse_error}")
        log(">> validating compose in a staging directory on the host")
        validation = stacks.validate_remote(host, updated.files, settings.remote_root)
        if not validation.get("ok"):
            raise UpdateError(f"docker compose validation failed: {validation.get('error')}")
        log(f"   compose is valid ({', '.join(validation.get('services') or [])})")

        log(">> pushing stack files")
        _push(host, updated, settings.remote_root, log)
        host_written = True
        log(">> pulling affected images and recreating")
        _compose_recreate(host, updated, settings.remote_root, list(target.get("services_affected") or []), log)
        log(">> waiting for health")
        health = wait_healthy(
            host, updated, settings.remote_root, log, timeout=int(settings.get("health_timeout_s", 120))
        )
        if not health.get("ok"):
            raise UpdateError(
                f"health check did not pass within {health.get('timeout')}s: {describe_health(health)}",
                details={"health": health},
            )
        summary = {
            "target": target,
            "snapshot": snapshot["id"],
            "file": mutation.file,
            "value": mutation.value,
            "health": health,
            "rolled_back": False,
        }
        log(f"✓ {stack.host}/{stack.name} updated — commit the repo change when ready")
        store.add_event(
            "update",
            "success",
            f"{stack.host}/{stack.name}: {target.get('service')} {target.get('current_display')} → {mutation.value}",
            host=stack.host,
            svc=stack.name,
            details=summary,
        )
        return summary
    except Exception as exc:
        log(f"! {exc} — rolling back compose files")
        if not host_written:
            host_written = bool(getattr(exc, "details", {}).get("written"))
        rolled_back = False
        try:
            restore_snapshot_files(settings, snapshot["id"], stack)
            restored = stacks.get_stack(settings, stack.host, stack.name)
            if not host_written:
                rolled_back = True
                log("✓ repo files restored; no stack file was rewritten on the host")
            else:
                validation = stacks.validate_remote(host, restored.files, settings.remote_root)
                if validation.get("ok"):
                    restore_push = stacks.push_files(
                        host, stack.name, restored.files, settings.remote_root, base=restored.path
                    )
                    if not restore_push.get("ok"):
                        raise UpdateError(f"rollback push failed: {restore_push.get('error')}")
                    _compose_up(host, restored, settings.remote_root, log)
                    rolled_back = True
                    log("✓ rollback complete (compose-only; container data was not restored)")
                else:
                    log(f"! rollback validation failed: {validation.get('error')}")
        except Exception as rollback_exc:
            log(f"✗ rollback failed: {rollback_exc}")
        store.add_event(
            "update",
            "failed",
            f"{stack.host}/{stack.name}: update failed — {exc}"
            + (" (rolled back)" if rolled_back else " (rollback incomplete)"),
            host=stack.host,
            svc=stack.name,
            details={"error": str(exc), "rolled_back": rolled_back, "snapshot": snapshot["id"], **getattr(exc, "details", {})},
        )
        raise UpdateError(str(exc), rolled_back=rolled_back, details=getattr(exc, "details", {})) from exc


def rollback_to_snapshot(
    settings: Settings,
    store: Store,
    host: Host,
    stack: stacks.Stack,
    snapshot_id: str,
    log: Callable[[str], None],
) -> dict[str, Any]:
    log(f">> rollback {stack.host}/{stack.name} to snapshot {snapshot_id}")
    restore_snapshot_files(settings, snapshot_id, stack)
    restored = stacks.get_stack(settings, stack.host, stack.name)
    validation = stacks.validate_remote(host, restored.files, settings.remote_root)
    if not validation.get("ok"):
        raise UpdateError(f"restored compose did not validate: {validation.get('error')}")
    push = stacks.push_files(host, stack.name, restored.files, settings.remote_root, base=restored.path)
    if not push.get("ok"):
        raise UpdateError(f"rollback push failed: {push.get('error')}")
    _compose_up(host, restored, settings.remote_root, log)
    health = wait_healthy(
        host, restored, settings.remote_root, log, timeout=int(settings.get("health_timeout_s", 120))
    )
    if not health.get("ok"):
        raise UpdateError(f"rollback containers did not become healthy: {describe_health(health)}")
    store.add_event(
        "rollback",
        "success",
        f"{stack.host}/{stack.name}: rolled back to {snapshot_id}",
        host=stack.host,
        svc=stack.name,
        details={"snapshot": snapshot_id, "health": health},
    )
    log("✓ rollback complete")
    return {"snapshot": snapshot_id, "health": health}


def targets_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    return {
        "total": len(rows),
        "updates": counts.get("update-available", 0),
        "unpinned": counts.get("unpinned", 0),
        "managed": counts.get("managed", 0),
        "arch_unavailable": counts.get("arch-unavailable", 0),
        "unknown": counts.get("unmanaged", 0) + counts.get("check-failed", 0) + counts.get("not-checked", 0),
        "counts": counts,
    }
