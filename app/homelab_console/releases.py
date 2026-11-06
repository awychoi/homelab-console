"""Upstream release intelligence with a deliberately pullable-tag bias.

Two hard rules shape this module:

1. **Only propose tags that can actually be pulled.**  For Docker Hub images
   the tag list is the authority (GitHub release names often add a ``v`` the
   image never had).  GitHub releases are used for known GHCR products and
   explicit workspace overrides.
2. **Never auto-upgrade a compatibility-managed component.**  Immich's
   Postgres/Redis pins are controlled by Immich's release matrix, so they are
   reported as managed inside an Immich stack, never as updates.  Redis outside
   that context is an ordinary Docker Hub image.

Checks are cached on disk (default 6h) and every network failure is returned
as data, not an exception, so one rate-limited API cannot blank the dashboard.
"""

from __future__ import annotations

import json
import os
import re
import ssl
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from . import util
from .config import Settings

USER_AGENT = "homelab-console/0.1 (+https://github.com/awychoi/homelab-console)"
PRERELEASE_RE = re.compile(r"(?:rc|beta|alpha|dev|nightly|canary|preview|snapshot|edge)", re.IGNORECASE)
VARIANT_SUFFIXES = (
    "-alpine", "-slim", "-bookworm", "-bullseye", "-trixie", "-debian",
    "-nanoserver", "-windowsservercore", "-builder", "-rootless",
    "-arm64", "-amd64", "-armv7", "-armhf", "-386", "-ppc64le", "-s390x", "-riscv64", "-pc",
)
# Variants worth indexing per architecture.  These are the suffixes the console
# already understands when comparing a pin (see the variant map in
# ``_check_source``); anything else is treated as a different flavour.
PLATFORM_VARIANTS = ("", "-alpine", "-slim", "-bookworm", "-debian")

# ``uname -m`` and Docker Hub use different spellings for the same CPU.  A
# candidate tag is only offered to a host when its manifest list contains a
# linux image for that host's canonical architecture.
ARCH_ALIASES = {
    "x86_64": "amd64", "x86-64": "amd64", "amd64": "amd64",
    "aarch64": "arm64", "arm64": "arm64", "arm64v8": "arm64",
    "armv8": "arm64", "armv8l": "arm64",
    "armv7l": "armv7", "armv7": "armv7", "armhf": "armv7",
    "armv6l": "armv6", "armv6": "armv6",
    "i386": "386", "i486": "386", "i586": "386", "i686": "386", "x86": "386", "386": "386",
    "ppc64le": "ppc64le", "s390x": "s390x", "riscv64": "riscv64",
}

# Official Docker Images build one repository per architecture (for example
# ``arm64v8/caddy``) and then assemble the multi-arch ``library/caddy``
# manifest.  Treat those namespaces as Docker Hub sources in their own right,
# even though the image's OCI label points at the upstream GitHub project.
ARCH_NAMESPACES = {
    "amd64", "arm64v8", "arm32v5", "arm32v6", "arm32v7", "i386", "mips64le",
    "ppc64le", "riscv64", "s390x", "windows-amd64",
}


@dataclass(frozen=True)
class Source:
    key: str
    kind: str  # dockerhub | github | managed | unknown
    label: str
    hub_repo: str = ""
    github_repo: str = ""
    calendar_tags: bool = False
    group: str = ""
    guidance: str = ""
    managed_by: str = ""
    notes: str = ""
    suggested_github_repo: str = ""
    origin: str = ""  # custom | catalog | labels | auto | group | unknown

    def public(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "kind": self.kind,
            "label": self.label,
            "hub_repo": self.hub_repo,
            "github_repo": self.github_repo,
            "calendar_tags": self.calendar_tags,
            "group": self.group,
            "guidance": self.guidance,
            "managed_by": self.managed_by,
            "notes": self.notes,
            "suggested_github_repo": self.suggested_github_repo,
            "origin": self.origin,
        }


CATALOG: dict[str, Source] = {
    "ghcr.io/immich-app/immich-server": Source(
        key="ghcr.io/immich-app/immich-server",
        kind="github",
        label="Immich",
        github_repo="immich-app/immich",
        group="immich",
        guidance="Coordinated Immich stack update. Back up the database (read-only smoke: /api/server/version) and review release notes.",
    ),
    "ghcr.io/immich-app/immich-machine-learning": Source(
        key="ghcr.io/immich-app/immich-machine-learning",
        kind="github",
        label="Immich ML",
        github_repo="immich-app/immich",
        group="immich",
        guidance="Keep aligned with the Immich server; the shared IMMICH_VERSION pin updates both.",
    ),
    "ghcr.io/immich-app/postgres": Source(
        key="ghcr.io/immich-app/postgres",
        kind="managed",
        label="Immich Postgres",
        managed_by="Immich compatibility matrix",
        guidance="Do not upgrade independently; follow Immich's release matrix.",
    ),
    "vaultwarden/server": Source(
        key="vaultwarden/server", kind="dockerhub", label="Vaultwarden",
        hub_repo="vaultwarden/server",
        guidance="Back up vaultwarden data before upgrading; verify login + an attachment afterwards.",
    ),
    "n8nio/n8n": Source(
        key="n8nio/n8n", kind="dockerhub", label="n8n",
        hub_repo="n8nio/n8n",
        guidance="Back up the n8n database, then run one workflow end-to-end after the update.",
    ),
    "caddy": Source(
        key="caddy", kind="dockerhub", label="Caddy",
        hub_repo="library/caddy",
        guidance="Preserve /data and /config so the internal CA and certificates survive the recreate.",
    ),
    "pihole/pihole": Source(
        key="pihole/pihole", kind="dockerhub", label="Pi-hole",
        hub_repo="pihole/pihole",
        guidance="DNS-adjacent: verify dig resolution and the dashboard after upgrading, one host at a time.",
    ),
    "copyparty/ac": Source(
        key="copyparty/ac", kind="dockerhub", label="Copyparty",
        hub_repo="copyparty/ac",
        guidance="Verify the library mount and a download after upgrading.",
    ),
    "madnuttah/unbound": Source(
        key="madnuttah/unbound", kind="dockerhub", label="Unbound",
        hub_repo="madnuttah/unbound",
        guidance="DNS-adjacent: confirm resolution through the sibling resolver after upgrading.",
    ),
    "searxng/searxng": Source(
        key="searxng/searxng", kind="dockerhub", label="SearXNG",
        hub_repo="searxng/searxng", calendar_tags=True,
        guidance="SearXNG publishes date tags; pin the tag, never :latest.",
    ),
    "traefik/whoami": Source(
        key="traefik/whoami", kind="dockerhub", label="whoami",
        hub_repo="traefik/whoami",
        guidance="Tiny diagnostic service; safe to update.",
    ),
}

# Repos that Docker Hub knows under a library/ namespace.
LIBRARY_ALIASES = {"nginx", "redis", "postgres", "caddy", "traefik", "alpine", "busybox"}


def normalize_key(repo: str) -> str:
    value = str(repo or "").strip().lower()
    for prefix in ("docker.io/", "index.docker.io/", "registry-1.docker.io/"):
        if value.startswith(prefix):
            value = value[len(prefix):]
    if value.startswith("docker.n8n.io/"):
        value = value[len("docker.n8n.io/"):]
    # lscr.io is LinuxServer.io's registry front; the images live on Docker Hub
    # under linuxserver/<name>.  Treating it as an arbitrary registry made every
    # LinuxServer stack fail its release check with a bogus Docker Hub lookup.
    if value.startswith("lscr.io/"):
        value = value[len("lscr.io/"):]
    return value.rstrip("/")


_SOURCE_KINDS = {"dockerhub", "github", "managed", "unknown"}
_IMAGE_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9._:/-]{0,254}$")
_HUB_REPO_RE = re.compile(r"^[a-z0-9][a-z0-9._/-]{0,254}$")
_GITHUB_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_GROUP_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,31}$")


def _registry_of(key: str) -> str:
    first = key.split("/", 1)[0]
    if "/" in key and ("." in first or ":" in first or first == "localhost"):
        return first
    return ""


def _suggest_github_repo(key: str) -> str:
    """A conservative suggestion for GHCR images, never an automatic source."""
    if not key.startswith("ghcr.io/"):
        return ""
    parts = key[len("ghcr.io/"):].split("/")
    if len(parts) == 2 and all(parts):
        return "/".join(parts)
    return ""


def _override_for(key: str, overrides: dict[str, dict[str, Any]] | None) -> dict[str, Any] | None:
    if not overrides:
        return None
    candidates = [key]
    if key.startswith("library/"):
        candidates.append(key[len("library/"):])
    else:
        candidates.append(f"library/{key}")
    for candidate in candidates:
        value = overrides.get(candidate)
        if isinstance(value, dict):
            return value
    return None


def _source_from_override(key: str, value: dict[str, Any]) -> Source:
    return Source(
        key=key,
        kind=str(value.get("kind") or "unknown"),
        label=str(value.get("label") or key),
        hub_repo=str(value.get("hub_repo") or ""),
        github_repo=str(value.get("github_repo") or ""),
        calendar_tags=bool(value.get("calendar_tags")),
        group=str(value.get("group") or ""),
        guidance=str(value.get("guidance") or ""),
        managed_by=str(value.get("managed_by") or ""),
        notes=str(value.get("notes") or "configured in Settings"),
        suggested_github_repo=_suggest_github_repo(key),
        origin="custom",
    )


def _unknown(key: str, notes: str = "no release source configured") -> Source:
    suggestion = _suggest_github_repo(key)
    if suggestion:
        notes += f"; if this is built from GitHub, try {suggestion}"
    return Source(
        key=key,
        kind="unknown",
        label=key,
        notes=notes,
        suggested_github_repo=suggestion,
        origin="unknown",
    )


def lookup(
    repo: str,
    labels: dict[str, str] | None = None,
    overrides: dict[str, dict[str, Any]] | None = None,
) -> Source:
    key = normalize_key(repo)
    if not key:
        return Source(key="", kind="unknown", label="", origin="unknown")
    override = _override_for(key, overrides)
    if override is not None:
        return _source_from_override(key, override)
    if key in CATALOG:
        return replace(CATALOG[key], origin="catalog")
    if key.startswith("library/") and key[len("library/"):] in CATALOG:
        return replace(CATALOG[key[len("library/"):]], origin="catalog")
    if "/" not in key:
        library = f"library/{key}"
        if library in CATALOG:
            return replace(CATALOG[library], origin="catalog")
        # A one-word official image: Docker Hub is the only sensible guess.
        return Source(
            key=key,
            kind="dockerhub",
            label=key,
            hub_repo=library,
            notes="auto-detected Docker Hub image",
            origin="auto",
        )
    namespace, _, name = key.partition("/")
    if namespace in ARCH_NAMESPACES and name:
        # Arch-specific official image.  Do not let its OCI source label turn
        # this into a GitHub release check: the Docker Hub tags are the truth.
        return Source(
            key=key,
            kind="dockerhub",
            label=name,
            hub_repo=key,
            notes="official architecture-specific Docker Hub image",
            origin="auto",
        )
    # OCI label auto-discovery.  Works for any registry; the label is only
    # present once the container has been deployed and inspected.
    if labels:
        source = labels.get("org.opencontainers.image.source") or ""
        match = re.search(r"github\.com/([^/]+/[^/#?]+)", source)
        if match:
            return Source(
                key=key,
                kind="github",
                label=key.rsplit("/", 1)[-1],
                github_repo=match.group(1).removesuffix(".git"),
                notes="release source discovered from the image's OCI labels",
                origin="labels",
            )
    registry = _registry_of(key)
    if registry:
        # Never guess Docker Hub for a different registry.  A failed check is
        # noisier than an honest "unmanaged" row with a suggestion.
        return _unknown(key, f"registry {registry} has no release source configured")
    if "/" in key:
        return Source(
            key=key,
            kind="dockerhub",
            label=key,
            hub_repo=key,
            notes="auto-detected Docker Hub image",
            origin="auto",
        )
    return _unknown(key)


def _clean_source_text(value: Any, field: str, limit: int, *, allow_empty: bool = False) -> str:
    text = str(value if value is not None else "").strip()
    if "\x00" in text or "\r" in text or "\n" in text:
        raise ValueError(f"{field} must be a single line")
    if len(text) > limit:
        raise ValueError(f"{field} is too long (max {limit})")
    if not allow_empty and not text:
        raise ValueError(f"{field} is required")
    return text


def validate_source_override(repo: Any, raw: Any, *, strict: bool = True) -> tuple[str, dict[str, Any]]:
    """Validate one user release source. Returns (canonical image key, value)."""
    key = normalize_key(str(repo or ""))
    if not key or not _IMAGE_KEY_RE.match(key) or ".." in key or "//" in key:
        raise ValueError("image must be a registry repository such as ghcr.io/owner/app or nginx")
    if not isinstance(raw, dict):
        raise ValueError("source must be an object")
    allowed = {"kind", "label", "hub_repo", "github_repo", "calendar_tags", "group", "guidance", "managed_by", "notes"}
    unknown = set(raw) - allowed
    if unknown and strict:
        raise ValueError(f"unknown source field: {sorted(unknown)[0]}")
    kind = str(raw.get("kind") or "").strip().lower()
    if kind not in _SOURCE_KINDS:
        raise ValueError("source kind must be dockerhub, github, managed or unknown")
    label = _clean_source_text(raw.get("label") or key, "label", 80)
    group = _clean_source_text(raw.get("group") or "", "group", 32, allow_empty=True)
    if group and not _GROUP_RE.match(group):
        raise ValueError("group must be lowercase letters/numbers/dots/dashes")
    guidance = _clean_source_text(raw.get("guidance") or "", "guidance", 500, allow_empty=True)
    managed_by = _clean_source_text(raw.get("managed_by") or "", "managed_by", 120, allow_empty=True)
    notes = _clean_source_text(raw.get("notes") or "configured in Settings", "notes", 300)
    value: dict[str, Any] = {
        "kind": kind,
        "label": label,
        "group": group,
        "calendar_tags": bool(raw.get("calendar_tags")),
        "guidance": guidance,
        "managed_by": managed_by,
        "notes": notes,
    }
    if kind == "dockerhub":
        hub_repo = str(raw.get("hub_repo") or "").strip().lower()
        if not hub_repo:
            if _registry_of(key):
                raise ValueError("Docker Hub source needs a hub_repo such as linuxserver/jellyfin")
            hub_repo = f"library/{key}" if "/" not in key else key
        if (
            not _HUB_REPO_RE.match(hub_repo)
            or hub_repo.startswith("/")
            or ".." in hub_repo
            or "//" in hub_repo
        ):
            raise ValueError("hub_repo must look like owner/name or library/name")
        value["hub_repo"] = hub_repo
        value["github_repo"] = ""
    elif kind == "github":
        github_repo = str(raw.get("github_repo") or _suggest_github_repo(key)).strip().removesuffix(".git")
        if not github_repo or not _GITHUB_REPO_RE.match(github_repo) or ".." in github_repo:
            raise ValueError("GitHub source needs github_repo as owner/repository")
        value["github_repo"] = github_repo
        value["hub_repo"] = ""
    elif kind == "managed":
        value["managed_by"] = managed_by or "compatibility-managed"
        value["hub_repo"] = ""
        value["github_repo"] = ""
    else:
        value["hub_repo"] = ""
        value["github_repo"] = ""
    return key, value


def normalize_source_overrides(raw: Any, *, strict: bool = False) -> dict[str, dict[str, Any]]:
    if not isinstance(raw, dict):
        if strict:
            raise ValueError("release_sources must be an object")
        return {}
    overrides: dict[str, dict[str, Any]] = {}
    for repo, value in raw.items():
        if len(overrides) >= 100:
            if strict:
                raise ValueError("at most 100 release sources are supported")
            break
        try:
            key, source = validate_source_override(repo, value, strict=strict)
        except ValueError:
            if strict:
                raise
            continue
        overrides[key] = source
    return overrides


def group_managed_source(repo: str, groups: Iterable[str], overrides: dict[str, dict[str, Any]] | None = None) -> Source | None:
    """Compatibility-managed images are scoped to a declared group.

    Redis is used by many stacks outside Immich, so a global Redis mapping was
    wrong.  Only a stack that also contains an Immich image inherits this.
    """
    key = normalize_key(repo)
    if _override_for(key, overrides) is not None:
        return None  # an explicit source always wins
    if "immich" in set(groups) and key in ("redis", "library/redis"):
        return Source(
            key=key,
            kind="managed",
            label="Redis (Immich)",
            managed_by="Immich compatibility matrix",
            guidance="Used by Immich; follow Immich compatibility guidance, not Redis upstream.",
            origin="group",
        )
    return None


# --------------------------------------------------------------- versions


def version_numbers(text: str | None) -> tuple[int, ...] | None:
    if not text:
        return None
    value = text.strip()
    value = re.sub(r"^[A-Za-z][A-Za-z0-9_.-]*@", "", value)  # n8n@2.4.0
    value = re.sub(r"^(?:v|V)", "", value)
    value = re.sub(r"^release[-_/]?", "", value, flags=re.IGNORECASE)
    match = re.search(r"\d+(?:\.\d+){0,3}", value)
    if not match:
        return None
    parts = [int(part) for part in match.group(0).split(".")]
    while len(parts) < 4:
        parts.append(0)
    return tuple(parts[:4])


def compare_versions(current: str | None, latest: str | None) -> int | None:
    left = version_numbers(current)
    right = version_numbers(latest)
    if left is None or right is None:
        return None
    return (right > left) - (right < left)


def status_for(current: str | None, latest: str | None, *, digest_only: bool = False) -> str:
    if digest_only:
        return "digest-pinned"
    if not current or current.lower() in ("latest", "release", "stable", "main", "master"):
        return "unpinned"
    if not latest:
        return "unknown"
    result = compare_versions(current, latest)
    if result is None:
        return "unknown"
    if result > 0:
        return "update-available"
    if result == 0:
        return "current"
    return "ahead-of-stable"


def parse_timestamp(value: str | None) -> int:
    """Parse an ISO-8601 upstream timestamp into a Unix epoch (0 if absent)."""
    if not value:
        return 0
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp())
    except (TypeError, ValueError, OverflowError):
        return 0


def variant_of(tag: str) -> str:
    lowered = tag.lower()
    for suffix in VARIANT_SUFFIXES:
        if lowered.endswith(suffix):
            return suffix
    return ""


def _stable_candidates(tags: list[dict[str, Any]], current: str, calendar: bool) -> list[dict[str, Any]]:
    candidates = []
    current_variant = variant_of(current)
    for index, tag in enumerate(tags):
        name = str(tag.get("name") or "").strip()
        if not name:
            continue
        if calendar:
            if not re.match(r"^\d{4}\.\d{1,2}\.\d{1,2}(?:-|$)", name):
                continue
            if PRERELEASE_RE.search(name):
                continue
        else:
            if not re.match(r"^v?\d+(?:\.\d+){1,3}(?:-[A-Za-z0-9.]+)?$", name):
                continue
            if PRERELEASE_RE.search(name):
                continue
        variant = variant_of(name)
        if current_variant:
            if variant != current_variant:
                continue
        else:
            if variant:
                continue
        numbers = version_numbers(name)
        if numbers is None:
            continue
        candidates.append({"name": name, "numbers": numbers, "index": index, "variant": variant})
    return candidates


def pick_latest_tag(tags: list[dict[str, Any]], current: str, *, calendar: bool = False) -> str | None:
    candidates = _stable_candidates(tags, current, calendar)
    if not candidates:
        return None
    # Highest version wins; for equal numeric versions prefer the plain tag,
    # then the most recently pushed (earlier in Docker Hub's last_updated order).
    top = max(candidate["numbers"] for candidate in candidates)
    tied = [candidate for candidate in candidates if candidate["numbers"] == top]
    plain = [candidate for candidate in tied if not candidate["variant"]]
    pool = plain or tied

    # Prefer a tag with no extra suffix at all (n8n "2.45.4" over
    # "2.45.4-e0c02a6"), then the most recently pushed (Docker Hub order).
    def score(candidate: dict[str, Any]) -> tuple[int, int]:
        exact = 0 if re.match(r"^v?\d+(?:\.\d+){1,3}$", candidate["name"]) else 1
        return (exact, candidate["index"])

    return min(pool, key=score)["name"]


def normalize_arch(value: str | None) -> str:
    """Canonical CPU name for a host (``uname -m``) or image platform."""
    text = str(value or "").strip().lower()
    if not text:
        return ""
    return ARCH_ALIASES.get(text, text)


def variant_key(current: str) -> str:
    return variant_of(current).lstrip("-") or "plain"


def _tag_arches(tag: dict[str, Any]) -> set[str]:
    """Linux architectures present in one Docker Hub tag's manifest list."""
    arches: set[str] = set()
    for image in tag.get("images") or []:
        if not isinstance(image, dict):
            continue
        if str(image.get("os") or "").strip().lower() != "linux":
            continue
        arch = normalize_arch(image.get("architecture"))
        variant = str(image.get("variant") or "").strip().lower()
        if not arch or arch in ("unknown", "none"):
            continue
        if arch == "arm":
            if variant in ("8", "v8"):
                arch = "arm64"
            elif variant:
                arch = "armv" + variant.lstrip("v")
        arches.add(arch)
    return arches


def platform_tag_index(
    tags: list[dict[str, Any]], *, calendar: bool = False
) -> tuple[dict[str, str], dict[str, int]]:
    """Newest stable tag per flavour *and* CPU architecture.

    Docker Hub publishes one tag whose manifest list may cover several
    architectures.  A release that is still building can expose a newer tag
    for amd64 while arm64 is absent; indexing the tag's ``images`` array keeps
    such a tag from becoming an update target on the wrong host.
    """
    by_name = {str(tag.get("name") or ""): tag for tag in tags if tag.get("name")}
    arches_by_name = {name: _tag_arches(tag) for name, tag in by_name.items()}
    arches = sorted(set().union(*arches_by_name.values())) if arches_by_name else []
    latest: dict[str, str] = {}
    published: dict[str, int] = {}
    if not arches:
        return latest, published
    for suffix in PLATFORM_VARIANTS:
        key = suffix.lstrip("-") or "plain"
        for arch in arches:
            subset = [tag for tag in tags if arch in arches_by_name.get(str(tag.get("name") or ""), set())]
            chosen = pick_latest_tag(subset, f"v0.0.0{suffix}", calendar=calendar)
            if not chosen:
                continue
            latest[f"{key}:{arch}"] = chosen
            stamp = parse_timestamp((by_name.get(chosen) or {}).get("last_updated"))
            if stamp:
                published[f"{key}:{arch}"] = stamp
    return latest, published


def tag_latest_for_arch(
    entry: "ReleaseEntry | None", current: str, arch: str
) -> tuple[str, int, bool]:
    """Best tag for one host architecture.

    Returns ``(tag, published_at, known)``.  ``known`` is False when the cached
    release predates platform indexing or Docker Hub returned no usable image
    data; callers must then keep the unfiltered behaviour.
    """
    data = entry.extra.get("latest_by_platform") if entry else None
    if not isinstance(data, dict) or not data or not arch:
        return "", 0, False
    key = f"{variant_key(current)}:{arch}"
    tag = str(data.get(key) or "")
    stamps = entry.extra.get("latest_published_by_platform") if entry else None
    stamp = int((stamps or {}).get(key) or 0) if tag else 0
    return tag, stamp, True


# ------------------------------------------------------------------ http


def http_json(url: str, headers: dict[str, str] | None = None, timeout: float = 20) -> tuple[int, Any, dict[str, str]]:
    """Fetch JSON with urllib, falling back to curl for CA-store-less Pythons."""
    request_headers = {"User-Agent": USER_AGENT, "Accept": "application/json", **(headers or {})}
    request = urllib.request.Request(url, headers=request_headers)
    try:
        with util.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
            return response.status, json.loads(body or "null"), {k.lower(): v for k, v in response.headers.items()}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(body or "null")
        except ValueError:
            payload = body
        return exc.code, payload, {k.lower(): v for k, v in (exc.headers or {}).items()}
    except (urllib.error.URLError, ssl.SSLError, OSError):
        return _curl_json(url, request_headers, timeout)


def _curl_quote(value: str) -> str:
    if "\n" in value or "\r" in value or "\x00" in value:
        raise RuntimeError("refusing a curl config value with control characters")
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _curl_json(url: str, headers: dict[str, str], timeout: float) -> tuple[int, Any, dict[str, str]]:
    if not util.which("curl"):
        raise RuntimeError("TLS failed and curl is not available")
    sensitive = any(key.lower() in ("authorization", "proxy-authorization", "cookie") for key in headers)
    lines = ["silent", "show-error", f"max-time = {int(timeout)}"]
    if sensitive:
        # Do not let curl follow a redirect with an Authorization header.
        lines.append("max-redirs = 0")
    else:
        lines.append("location")
    for key, value in headers.items():
        lines.append(f'header = "{_curl_quote(key)}: {_curl_quote(value)}"')
    lines.append(f'url = "{_curl_quote(url)}"')
    lines.append('write-out = "\\n__HLC_HTTP__%{http_code}"')
    result = util.run(["curl", "--config", "-"], input_text="\n".join(lines) + "\n", timeout=timeout + 5)
    body = result.stdout
    status = 0
    if "__HLC_HTTP__" in body:
        body, _, code = body.rpartition("__HLC_HTTP__")
        body = body.rstrip("\n")
        try:
            status = int(code.strip())
        except ValueError:
            status = 0
    if result.returncode != 0 and not status:
        raise RuntimeError(result.stderr.strip() or "curl failed")
    try:
        payload = json.loads(body or "null")
    except ValueError:
        payload = body
    return status, payload, {}


# --------------------------------------------------------------- sources


def dockerhub_tags(repo: str, pages: int = 5, page_size: int = 100) -> list[dict[str, Any]]:
    namespace, name = repo.split("/", 1) if "/" in repo else ("library", repo)
    url = (
        f"https://hub.docker.com/v2/repositories/{namespace}/{name}/tags"
        f"?page_size={page_size}&ordering=last_updated"
    )
    collected: list[dict[str, Any]] = []
    for _ in range(pages):
        status, payload, _ = http_json(url)
        if status != 200 or not isinstance(payload, dict):
            detail = payload.get("message") if isinstance(payload, dict) else str(payload)[:200]
            raise RuntimeError(f"Docker Hub returned HTTP {status}: {detail}")
        results = payload.get("results") or []
        collected.extend(item for item in results if item.get("name"))
        url = str(payload.get("next") or "")
        if not url:
            break
        if len(collected) >= page_size * 3:
            break
    return collected


def github_latest(repo: str, token: str = "") -> dict[str, str]:
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    status, payload, _ = http_json(f"https://api.github.com/repos/{repo}/releases/latest", headers)
    if status == 200 and isinstance(payload, dict) and payload.get("tag_name"):
        return {
            "tag": str(payload["tag_name"]),
            "url": str(payload.get("html_url") or f"https://github.com/{repo}/releases"),
            "published": str(payload.get("published_at") or ""),
        }
    if status == 404:
        # No releases: fall back to tags and pick the highest stable version.
        status, payload, _ = http_json(f"https://api.github.com/repos/{repo}/tags?per_page=100", headers)
        if status == 200 and isinstance(payload, list):
            tags = [str(item.get("name") or "") for item in payload if item.get("name")]
            stable = [tag for tag in tags if version_numbers(tag) and not PRERELEASE_RE.search(tag)]
            if stable:
                best = max(stable, key=lambda tag: version_numbers(tag) or (0,))
                return {"tag": best, "url": f"https://github.com/{repo}/tags", "published": ""}
        raise RuntimeError(f"GitHub repository {repo} has no usable releases or tags")
    if status in (403, 429):
        raise RuntimeError("GitHub rate limit hit — add a token in Settings or retry later")
    detail = payload.get("message") if isinstance(payload, dict) else str(payload)[:200]
    raise RuntimeError(f"GitHub returned HTTP {status}: {detail}")


# ------------------------------------------------------------- cache type


@dataclass
class ReleaseEntry:
    key: str
    kind: str
    label: str
    latest: str = ""
    published_at: int = 0
    url: str = ""
    checked_at: int = 0
    error: str = ""
    source_name: str = ""
    guidance: str = ""
    managed_by: str = ""
    notes: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def public(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "kind": self.kind,
            "label": self.label,
            "latest": self.latest,
            "published_at": self.published_at,
            "published_iso": util.iso(self.published_at) if self.published_at else "",
            "url": self.url,
            "checked_at": self.checked_at,
            "checked_iso": util.iso(self.checked_at) if self.checked_at else "",
            "error": self.error,
            "source": self.source_name,
            "guidance": self.guidance,
            "managed_by": self.managed_by,
            "notes": self.notes,
            **self.extra,
        }

    def to_json(self) -> dict[str, Any]:
        return {
            "key": self.key, "kind": self.kind, "label": self.label, "latest": self.latest,
            "published_at": self.published_at,
            "url": self.url, "checked_at": self.checked_at, "error": self.error,
            "source_name": self.source_name, "guidance": self.guidance,
            "managed_by": self.managed_by, "notes": self.notes, "extra": self.extra,
        }

    @classmethod
    def from_json(cls, value: dict[str, Any]) -> "ReleaseEntry":
        return cls(
            key=str(value.get("key", "")), kind=str(value.get("kind", "unknown")),
            label=str(value.get("label", "")), latest=str(value.get("latest", "")),
            published_at=int(value.get("published_at", 0) or 0)
            or parse_timestamp((value.get("extra") or {}).get("published")),
            url=str(value.get("url", "")), checked_at=int(value.get("checked_at", 0) or 0),
            error=str(value.get("error", "")), source_name=str(value.get("source_name", "")),
            guidance=str(value.get("guidance", "")), managed_by=str(value.get("managed_by", "")),
            notes=str(value.get("notes", "")), extra=value.get("extra") or {},
        )


class ReleaseService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.cache_file = settings.cache_dir / "releases.json"
        self.lock = threading.Lock()
        self.entries: dict[str, ReleaseEntry] = {}
        self.last_refresh = 0
        self.overrides = normalize_source_overrides(settings.get("release_sources"))
        self._load()

    def reload_overrides(self) -> None:
        """Re-read workspace release sources (used after Settings saves)."""
        self.overrides = normalize_source_overrides(self.settings.get("release_sources"))

    def has_override(self, repo: str) -> bool:
        return _override_for(normalize_key(repo), self.overrides) is not None

    def source_for(
        self,
        repo: str,
        labels: dict[str, str] | None = None,
        groups: Iterable[str] = (),
    ) -> Source:
        """Resolve one image, applying custom sources and group context."""
        source = lookup(repo, labels, self.overrides)
        grouped = group_managed_source(repo, groups, self.overrides)
        return grouped or source

    def stack_groups(self, stack, labels_by_image: dict[str, dict[str, str]] | None = None) -> set[str]:
        """Groups declared by the images in a stack (e.g. immich)."""
        groups: set[str] = set()
        labels_by_image = labels_by_image or {}
        for service in getattr(stack, "services", []):
            image = getattr(service, "image", None)
            if image is None:
                continue
            source = lookup(image.repo, labels_by_image.get(service.image_raw) or {}, self.overrides)
            if source.group:
                groups.add(source.group)
        return groups

    def _load(self) -> None:
        raw = util.json_load(self.cache_file, {}) or {}
        entries = raw.get("entries", {}) if isinstance(raw, dict) else {}
        for key, value in entries.items():
            if isinstance(value, dict):
                try:
                    self.entries[key] = ReleaseEntry.from_json(value)
                except Exception:
                    continue

    def _save(self) -> None:
        payload = {"entries": {key: entry.to_json() for key, entry in self.entries.items()}}
        util.json_dump(self.cache_file, payload, mode=0o600)

    # ---- checking ------------------------------------------------------
    def refresh(
        self,
        requests: Iterable[dict[str, Any]],
        *,
        force: bool = False,
        progress: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        """Check the unique sources behind ``requests``.

        Each request is ``{"repo": <image repo>, "current": <tag>, "labels":
        {...}, "groups": [...]}``.  Returns a summary with per-key entries.
        """
        say = progress or (lambda _message: None)
        ttl = int(self.settings.get("release_ttl_s", 6 * 3600))
        now = util.now_ts()
        offline = os.environ.get("HLC_OFFLINE") == "1"
        wanted: dict[str, Source] = {}
        for request in requests:
            source = self.source_for(
                str(request.get("repo") or ""),
                request.get("labels") or {},
                request.get("groups") or (),
            )
            existing = wanted.get(source.key)
            # A redis image can appear in an Immich stack (managed) and a
            # normal stack (checked).  The checked context wins so the
            # release is still fetched for the stack that can use it.
            if existing is None or (existing.kind == "managed" and source.kind != "managed"):
                wanted[source.key] = source

        tasks: list[Source] = []
        removed = 0
        for source in wanted.values():
            if source.kind in ("managed", "unknown"):
                # Unmanaged images are not a failed check; drop any stale
                # entry from before the source was configured.
                with self.lock:
                    if self.entries.pop(source.key, None) is not None:
                        removed += 1
                continue
            cached = self.entries.get(source.key)
            # Old Docker Hub cache entries predate release-age capture; refresh
            # those once instead of showing "age unknown" until the 6h TTL.
            missing_age = bool(
                cached
                and cached.kind == "dockerhub"
                and cached.latest
                and not cached.published_at
            )
            # Entries written before platform indexing also need one refresh so
            # an arm64 host stops seeing amd64-only tags as updates.
            missing_platforms = bool(
                cached
                and cached.kind == "dockerhub"
                and cached.latest
                and "latest_by_platform" not in cached.extra
            )
            if cached and not force and not missing_age and not missing_platforms and now - cached.checked_at < ttl:
                continue
            if cached and cached.error and now - cached.checked_at < 600:
                continue
            tasks.append(source)

        results: dict[str, Any] = {}
        if tasks and offline:
            for source in tasks:
                cached = self.entries.get(source.key)
                if cached:
                    cached.error = "offline mode: showing cached value"
                    results[source.key] = cached
            return {"checked": 0, "cached": len(results), "errors": ["offline mode"]}

        if tasks:
            say(f"checking {len(tasks)} upstream source(s)")
        done = 0
        with ThreadPoolExecutor(max_workers=min(5, max(1, len(tasks)))) as pool:
            futures = {pool.submit(self._check_source, source): source for source in tasks}
            for future in as_completed(futures):
                source = futures[future]
                done += 1
                try:
                    entry = future.result()
                except Exception as exc:  # defensive: _check_source catches, but never lose a task
                    entry = ReleaseEntry(source.key, source.kind, source.label, error=str(exc), checked_at=now)
                with self.lock:
                    self.entries[source.key] = entry
                say(f"[{done}/{len(tasks)}] {source.label}: {entry.latest or entry.error or 'no release found'}")
        if tasks or removed:
            with self.lock:
                self._save()
        self.last_refresh = now
        return {
            "checked": len(tasks),
            "cached": 0,
            "errors": [entry.error for entry in self.entries.values() if entry.error],
        }

    def _check_source(self, source: Source) -> ReleaseEntry:
        now = util.now_ts()
        entry = ReleaseEntry(
            key=source.key,
            kind=source.kind,
            label=source.label,
            source_name="",
            guidance=source.guidance,
            managed_by=source.managed_by,
            notes=source.notes,
            checked_at=now,
        )
        if source.kind == "managed":
            entry.source_name = source.managed_by
            return entry
        try:
            if source.kind == "github":
                payload = github_latest(source.github_repo, self.settings.github_token)
                entry.latest = payload["tag"]
                entry.published_at = parse_timestamp(payload.get("published"))
                entry.url = payload["url"]
                entry.source_name = f"GitHub · {source.github_repo}"
                entry.extra["published"] = payload.get("published", "")
            elif source.kind == "dockerhub":
                tags = dockerhub_tags(source.hub_repo or source.key)
                entry.source_name = f"Docker Hub · {source.hub_repo or source.key}"
                entry.url = f"https://hub.docker.com/r/{source.hub_repo or source.key}/tags"
                # Cache the latest tag for each common variant so a stack
                # pinned to -alpine is compared against -alpine, not plain.
                variants: dict[str, str] = {}
                published: dict[str, int] = {}
                by_name = {str(tag.get("name") or ""): tag for tag in tags if tag.get("name")}
                for suffix in PLATFORM_VARIANTS:
                    chosen = pick_latest_tag(tags, f"v0.0.0{suffix}", calendar=source.calendar_tags)
                    if chosen:
                        key_name = suffix.lstrip("-") or "plain"
                        variants[key_name] = chosen
                        stamp = parse_timestamp((by_name.get(chosen) or {}).get("last_updated"))
                        if stamp:
                            published[key_name] = stamp
                entry.extra["latest_by_variant"] = variants
                entry.extra["latest_published_by_variant"] = published
                by_platform, published_by_platform = platform_tag_index(tags, calendar=source.calendar_tags)
                entry.extra["latest_by_platform"] = by_platform
                entry.extra["latest_published_by_platform"] = published_by_platform
                entry.extra["tag_count"] = len(tags)
                entry.latest = variants.get("plain") or next(iter(variants.values()), "")
                entry.published_at = published.get("plain") or next(iter(published.values()), 0)
                if not entry.latest:
                    raise RuntimeError("no stable tag matched the image's convention")
            elif source.kind == "unknown":
                # Unmanaged is an honest state, not a failed check.  The
                # user can add a source in Settings; until then there is
                # nothing useful to fetch.
                entry.source_name = "unmanaged"
            else:
                entry.error = f"unsupported release source: {source.kind}"
        except Exception as exc:
            entry.error = str(exc)[:400]
        return entry

    def entry_for(
        self, repo: str, labels: dict[str, str] | None = None, groups: Iterable[str] = ()
    ) -> tuple[Source, ReleaseEntry | None]:
        source = self.source_for(repo, labels, groups)
        return source, self.entries.get(source.key)

    def status(self) -> dict[str, Any]:
        return {
            "checked_at": self.last_refresh,
            "checked_iso": util.iso(self.last_refresh) if self.last_refresh else "",
            "sources": len(self.entries),
            "errors": sum(1 for entry in self.entries.values() if entry.error),
        }
