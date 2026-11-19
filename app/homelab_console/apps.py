"""App bundles and the dependency manifests they carry.

A project can point at one built macOS application and list the upstream
repositories that application vendors.  The console launches the bundle and
compares what the bundle *claims* to carry with the newest upstream release;
it never downloads, builds or updates anything itself.

Finding a bundle is easy.  Knowing which 7-Zip is inside it is not: a project's
own version says nothing about its dependencies (SevenZip is 0.1.0 and vendors
7-Zip 26.06), so the only honest source is the bundle itself.  An app that
wants update flags ships ``Contents/Resources/homelab-app.json`` and rewrites
it after a successful self-update.  An app that does not gets the launch
button and an ``unknown`` version -- never a guess.

The manifest is deliberately small::

    {
      "schema": 1,
      "app": {"name": "SevenZip", "version": "0.1.0"},
      "dependencies": [
        {"repo": "ip7z/7zip", "version": "26.06", "path": "7zz"}
      ]
    }

Upstream tags come from the GitHub releases API with a tag fallback and a disk
cache: a project row is rebuilt every ten seconds and the API is not.
"""

from __future__ import annotations

import json
import logging
import os
import plistlib
import re
import threading
import time
import xml.parsers.expat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from . import util
from .releases import http_json

log = logging.getLogger("hlc.apps")

MANIFEST_NAME = "homelab-app.json"
MANIFEST_SCHEMA = 1
MAX_MANIFEST_BYTES = 128 * 1024
MAX_PLIST_BYTES = 1024 * 1024
MAX_DEPENDENCIES = 12
MAX_REPOS_WITH_APPS = 500
MAX_PATH_LENGTH = 400

# An owner/repository pair, as GitHub spells it.  Kept deliberately narrow: the
# value is interpolated into an API URL, so anything else must never get that far.
SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,38}/[A-Za-z0-9._-]{1,100}$")
_NUMBER_RE = re.compile(r"[0-9]+")

GITHUB_API = "https://api.github.com"

# Discovery is a courtesy, not a contract.  A project may contain any number of
# bundles (dependencies, samples, older builds), so the walk is bounded and the
# result is a ranked shortlist the operator confirms.
SCAN_MAX_DEPTH = 5
SCAN_LIMIT = 6
SCAN_BUDGET_S = 2.0
SCAN_SKIP = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "__pycache__",
        "node_modules",
        "Pods",
        "DerivedData",
    }
)

# A failed lookup is retried long before a successful one: a rate limit or a
# dropped connection must not freeze the column for six hours.
ERROR_TTL_S = 5 * 60


class AppError(ValueError):
    """A user-facing problem with an app path or dependency list."""


# ------------------------------------------------------------------ model


@dataclass(frozen=True)
class Bundle:
    """One macOS application bundle and the versions it says it carries."""

    path: str
    name: str
    identifier: str
    version: str
    executable: str
    dependencies: dict[str, str] = field(default_factory=dict)
    manifest: bool = False
    stamp: float = 0.0

    def public(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "name": self.name,
            "identifier": self.identifier,
            "version": self.version,
            "dependencies": dict(self.dependencies),
            "manifest": self.manifest,
        }

    def installed(self, slug: str) -> str:
        """Version this bundle claims for ``slug``, case-insensitively."""
        wanted = slug.casefold()
        for key, value in self.dependencies.items():
            if key.casefold() == wanted:
                return value
        return ""


@dataclass
class Release:
    """The newest upstream version known for one repository."""

    slug: str
    tag: str = ""
    name: str = ""
    url: str = ""
    published_at: str = ""
    source: str = ""
    error: str = ""
    fetched_at: int = 0

    @property
    def ok(self) -> bool:
        return bool(self.tag) and not self.error

    @classmethod
    def from_dict(cls, slug: str, raw: dict[str, Any]) -> "Release":
        def text(key: str) -> str:
            value = raw.get(key)
            return str(value) if isinstance(value, (str, int)) else ""

        try:
            fetched = int(raw.get("fetched_at") or 0)
        except (TypeError, ValueError):
            fetched = 0
        return cls(
            slug=slug,
            tag=text("tag"),
            name=text("name"),
            url=text("url"),
            published_at=text("published_at"),
            source=text("source"),
            error=text("error"),
            fetched_at=max(0, fetched),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "tag": self.tag,
            "name": self.name,
            "url": self.url,
            "published_at": self.published_at,
            "source": self.source,
            "error": self.error,
            "fetched_at": self.fetched_at,
        }


# -------------------------------------------------------------- bundles


def is_bundle(path: str | Path) -> bool:
    """True for an existing ``.app`` directory with a readable Info.plist."""
    candidate = Path(path)
    if candidate.suffix != ".app":
        return False
    try:
        if not candidate.is_dir():
            return False
    except OSError:
        return False
    return (candidate / "Contents" / "Info.plist").is_file()


def _read_plist(path: Path) -> dict[str, Any]:
    try:
        if path.stat().st_size > MAX_PLIST_BYTES:
            return {}
        with path.open("rb") as handle:
            data = plistlib.load(handle)
    except (OSError, ValueError, plistlib.InvalidFileException, xml.parsers.expat.ExpatError):
        return {}
    return data if isinstance(data, dict) else {}


def read_manifest(bundle_path: str | Path) -> dict[str, Any]:
    """Read ``Contents/Resources/homelab-app.json``; ``{}`` when absent or bad.

    A malformed manifest must not make the project unreadable.  The console
    falls back to "version unknown", which is a state the UI already explains.
    """
    path = Path(bundle_path) / "Contents" / "Resources" / MANIFEST_NAME
    try:
        if path.stat().st_size > MAX_MANIFEST_BYTES:
            log.warning("ignoring oversized app manifest: %s", path)
            return {}
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def manifest_dependencies(manifest: dict[str, Any]) -> dict[str, str]:
    """Normalise a manifest's dependency list to ``{owner/repo: version}``."""
    items = manifest.get("dependencies") if isinstance(manifest, dict) else None
    if not isinstance(items, list):
        return {}
    result: dict[str, str] = {}
    for item in items:
        if isinstance(item, dict):
            slug = str(item.get("repo") or "").strip()
            version = str(item.get("version") or "").strip()
        elif isinstance(item, str):
            slug = item.strip()
            version = ""
        else:
            continue
        if not SLUG_RE.match(slug):
            continue
        # First declaration wins; duplicates are a packaging mistake, not a
        # reason to show two conflicting installed versions.
        result.setdefault(slug, version)
        if len(result) >= MAX_DEPENDENCIES * 4:
            break
    return result


def bundle_stamp(path: str | Path) -> float:
    """Newest mtime that changes when the bundle or its manifest is replaced.

    The directory mtime alone is not enough: rewriting
    ``Contents/Resources/homelab-app.json`` after a self-update updates the
    Resources directory, not the ``.app`` root, so a cache keyed on the root
    would serve the old versions forever.
    """
    root = Path(path)
    stamp = 0.0
    for candidate in (
        root,
        root / "Contents",
        root / "Contents" / "Resources",
        root / "Contents" / "Info.plist",
        root / "Contents" / "Resources" / MANIFEST_NAME,
    ):
        try:
            stamp = max(stamp, candidate.stat().st_mtime)
        except OSError:
            continue
    return stamp


def read_bundle(path: str | Path) -> Bundle | None:
    """Describe a bundle, or ``None`` when the path is not a usable app."""
    root = Path(path)
    if not is_bundle(root):
        return None
    info_path = root / "Contents" / "Info.plist"
    info = _read_plist(info_path)
    if not info:
        return None
    name = str(
        info.get("CFBundleDisplayName")
        or info.get("CFBundleName")
        or root.stem
    ).strip() or root.stem
    identifier = str(info.get("CFBundleIdentifier") or "").strip()
    version = str(info.get("CFBundleShortVersionString") or info.get("CFBundleVersion") or "").strip()
    executable = str(info.get("CFBundleExecutable") or "").strip()
    manifest = read_manifest(root)
    dependencies = manifest_dependencies(manifest)
    if not version:
        block = manifest.get("app")
        if isinstance(block, dict):
            version = str(block.get("version") or "").strip()
    return Bundle(
        path=str(root),
        name=name,
        identifier=identifier,
        version=version,
        executable=executable,
        dependencies=dependencies,
        manifest=bool(manifest),
        stamp=bundle_stamp(root),
    )


def scan_apps(
    repo: str | Path,
    *,
    max_depth: int = SCAN_MAX_DEPTH,
    limit: int = SCAN_LIMIT,
    budget_s: float = SCAN_BUDGET_S,
) -> list[Bundle]:
    """Rank bundles inside a project, shallow and repo-named first.

    Bundles are leaves: the walk never descends into one, so helper apps inside
    ``Contents/Frameworks`` are not offered as choices.  Hidden directories and
    the usual dependency/build trees are pruned, which keeps a large checkout
    from spending the whole budget in ``node_modules``.
    """
    root = Path(repo)
    if not root.is_dir():
        return []
    deadline = time.monotonic() + max(0.1, float(budget_s))
    found: list[tuple[int, Bundle]] = []
    seen: set[str] = set()

    def walk(directory: Path, depth: int) -> None:
        if depth > max_depth or time.monotonic() > deadline:
            return
        try:
            with os.scandir(directory) as handle:
                entries = sorted(handle, key=lambda entry: entry.name)
        except OSError:
            return
        for entry in entries:
            if time.monotonic() > deadline:
                return
            name = entry.name
            if name.startswith(".") or name in SCAN_SKIP:
                continue
            try:
                if not entry.is_dir(follow_symlinks=False):
                    continue
            except OSError:
                continue
            path = Path(entry.path)
            if name.endswith(".app"):
                bundle = read_bundle(path)
                if bundle and bundle.path not in seen:
                    seen.add(bundle.path)
                    found.append((depth, bundle))
                continue
            walk(path, depth + 1)

    walk(root, 0)
    repo_name = root.name.casefold()

    def rank(item: tuple[int, Bundle]) -> tuple[int, int, float, str]:
        depth, bundle = item
        name = bundle.name.casefold()
        identifier = bundle.identifier.casefold()
        score = 0
        if repo_name and (repo_name in name or name in repo_name):
            score += 2
        if repo_name and repo_name in identifier:
            score += 1
        # A bundle named after the project wins, then the shallowest, then the
        # newest.  Depth alone would favour a helper app at the repo root over
        # the real build in dist/.
        return (-score, depth, -bundle.stamp, bundle.path)

    found.sort(key=rank)
    return [bundle for _, bundle in found[: max(1, limit)]]


# ------------------------------------------------------------ validation


def _clean_path(value: Any, *, label: str) -> str:
    text = str(value or "").strip()
    if not text or len(text) > MAX_PATH_LENGTH or "\x00" in text:
        raise AppError(f"{label} must be a path")
    path = Path(text).expanduser()
    if not path.is_absolute():
        raise AppError(f"{label} must be an absolute path")
    # Canonicalise paths that exist so a hand-edited config using /var/...
    # still matches the /private/var/... path discovery reports on macOS.
    try:
        if path.exists():
            path = path.resolve()
    except (OSError, RuntimeError):
        # RuntimeError covers a symlink loop; the typed path is still usable.
        pass
    return str(path)


def normalize_app_path(value: Any) -> str:
    path = _clean_path(value, label="app path")
    if Path(path).suffix != ".app":
        raise AppError("app path must point at a .app bundle")
    return path


def normalize_repo_path(value: Any) -> str:
    return _clean_path(value, label="project path")


def normalize_repo_apps(raw: Any) -> dict[str, str]:
    """Tolerant read of the ``repo_apps`` setting; invalid rows are dropped."""
    if not isinstance(raw, dict):
        return {}
    result: dict[str, str] = {}
    for key, value in raw.items():
        try:
            repo_path = normalize_repo_path(key)
            app_path = normalize_app_path(value)
        except AppError:
            continue
        result[repo_path] = app_path
        if len(result) >= MAX_REPOS_WITH_APPS:
            break
    return result


def normalize_dependency(raw: Any) -> dict[str, str] | None:
    slug = ""
    if isinstance(raw, str):
        slug = raw.strip()
    elif isinstance(raw, dict):
        slug = str(raw.get("repo") or "").strip()
    if not SLUG_RE.match(slug):
        return None
    owner, name = slug.split("/", 1)
    return {"repo": f"{owner}/{name}"}


def normalize_dependencies(raw: Any) -> list[dict[str, str]]:
    """Tolerant read of one project's dependency list."""
    if not isinstance(raw, list):
        return []
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw:
        dependency = normalize_dependency(item)
        if dependency is None:
            continue
        key = dependency["repo"].casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(dependency)
        if len(result) >= MAX_DEPENDENCIES:
            break
    return result


def validate_dependencies(raw: Any) -> list[dict[str, str]]:
    """Strict variant used for writes: reject what would silently vanish."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise AppError("dependencies must be a list of repositories")
    if len(raw) > MAX_DEPENDENCIES:
        raise AppError(f"at most {MAX_DEPENDENCIES} dependencies per project")
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw:
        dependency = normalize_dependency(item)
        if dependency is None:
            raise AppError("each dependency must be an owner/repository, like ip7z/7zip")
        key = dependency["repo"].casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(dependency)
    return result


def normalize_repo_dependencies(raw: Any) -> dict[str, list[dict[str, str]]]:
    if not isinstance(raw, dict):
        return {}
    result: dict[str, list[dict[str, str]]] = {}
    for key, value in raw.items():
        try:
            repo_path = normalize_repo_path(key)
        except AppError:
            continue
        dependencies = normalize_dependencies(value)
        if not dependencies:
            continue
        result[repo_path] = dependencies
        if len(result) >= MAX_REPOS_WITH_APPS:
            break
    return result


# ---------------------------------------------------------------- versions


def version_numbers(value: str) -> tuple[int, ...]:
    """``"v26.06"`` -> ``(26, 6)``; non-numeric text yields ``()``."""
    return tuple(int(part) for part in _NUMBER_RE.findall(str(value or ""))[:8])


def compare_versions(left: str, right: str) -> int:
    """``-1`` when left is older, ``0`` when equal or unparsable, ``1`` newer.

    Unparsable input returns 0 on purpose: "unknown" must never be reported as
    "behind", so the caller treats an equal comparison as no evidence.
    """
    a = version_numbers(left)
    b = version_numbers(right)
    if not a or not b:
        return 0
    width = max(len(a), len(b))
    a = a + (0,) * (width - len(a))
    b = b + (0,) * (width - len(b))
    return (a > b) - (a < b)


def dependency_status(installed: str, release: Release | None) -> str:
    """One of ``current``, ``behind``, ``unknown``, ``error``.

    ``unknown`` is the honest answer when either side is missing: the app did
    not ship a manifest, or the upstream release check has not succeeded yet.
    """
    if release is None:
        return "unknown"
    if release.error:
        return "error"
    if not release.ok:
        return "unknown"
    if not installed:
        return "unknown"
    return "behind" if compare_versions(installed, release.tag) < 0 else "current"


# ------------------------------------------------------------------ cache


def _auth_headers(token: str = "") -> dict[str, str]:
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def fetch_release(slug: str, token: str = "", timeout: float = 15) -> Release:
    """Newest release for ``slug``; falls back to the newest tag.

    Not every project publishes releases (tags alone are common), and GitHub
    answers 404 for both "no releases" and "no repository".  The tag fallback
    separates those cases without a second endpoint for the common path.
    """
    release = Release(slug=slug, fetched_at=util.now_ts())
    if not SLUG_RE.match(slug):
        release.error = "invalid repository name"
        return release
    headers = _auth_headers(token)
    try:
        status, payload, response_headers = http_json(
            f"{GITHUB_API}/repos/{slug}/releases/latest", headers, timeout
        )
    except (RuntimeError, OSError) as exc:
        release.error = f"could not reach GitHub: {exc}"
        return release
    if status == 200 and isinstance(payload, dict):
        tag = str(payload.get("tag_name") or "").strip()
        if tag:
            release.tag = tag
            release.name = str(payload.get("name") or tag).strip()
            release.url = str(payload.get("html_url") or f"https://github.com/{slug}/releases").strip()
            release.published_at = str(payload.get("published_at") or "").strip()
            release.source = "release"
            return release
    if status in (401, 403):
        remaining = str(response_headers.get("x-ratelimit-remaining") or "").strip()
        if status == 403 and remaining == "0":
            release.error = "GitHub rate limit reached — add a token in Settings"
        else:
            release.error = "GitHub refused the request (check the token)"
        return release
    if status not in (200, 404):
        release.error = f"GitHub returned HTTP {status}"
        return release
    # No releases, or no repository: ask for tags before calling it an error.
    try:
        tag_status, tag_payload, _ = http_json(
            f"{GITHUB_API}/repos/{slug}/tags?per_page=1", headers, timeout
        )
    except (RuntimeError, OSError) as exc:
        release.error = f"could not reach GitHub: {exc}"
        return release
    if tag_status == 200 and isinstance(tag_payload, list) and tag_payload:
        first = tag_payload[0] if isinstance(tag_payload[0], dict) else {}
        tag = str(first.get("name") or "").strip()
        if tag:
            release.tag = tag
            release.name = tag
            release.url = f"https://github.com/{slug}/tags"
            release.source = "tag"
            return release
    if tag_status == 404:
        release.error = "repository not found"
    else:
        release.error = f"no releases or tags found (HTTP {tag_status})"
    return release


class ReleaseCache:
    """Disk cache of upstream versions, shared by every project row.

    The cache is read on the scan thread and written on the check thread.  The
    lock only covers the dictionary; network calls happen outside it so a slow
    GitHub response cannot stall a project scan.
    """

    def __init__(self, path: Path, ttl: int = 6 * 3600):
        self.path = Path(path)
        self.ttl = max(60, int(ttl or 0)) if ttl else 6 * 3600
        self._lock = threading.Lock()
        self._rows: dict[str, Release] | None = None

    # -- storage ------------------------------------------------------
    def _load(self) -> dict[str, Release]:
        if self._rows is not None:
            return self._rows
        raw = util.json_load(self.path, {})
        rows: dict[str, Release] = {}
        if isinstance(raw, dict):
            for slug, value in raw.items():
                if isinstance(value, dict) and SLUG_RE.match(str(slug)):
                    rows[str(slug)] = Release.from_dict(str(slug), value)
        self._rows = rows
        return rows

    def save(self) -> None:
        with self._lock:
            rows = self._load()
            payload = {slug: row.to_dict() for slug, row in rows.items()}
        try:
            util.json_dump(self.path, payload, mode=0o600)
        except OSError:
            log.warning("could not write the app release cache", exc_info=True)

    # -- reads --------------------------------------------------------
    def get(self, slug: str) -> Release | None:
        with self._lock:
            return self._load().get(slug)

    def rows(self, slugs: Iterable[str]) -> dict[str, Release]:
        with self._lock:
            stored = self._load()
            return {slug: stored[slug] for slug in slugs if slug in stored}

    def is_fresh(self, slug: str, *, now: int | None = None) -> bool:
        release = self.get(slug)
        if release is None:
            return False
        moment = util.now_ts() if now is None else now
        age = max(0, moment - release.fetched_at)
        return age < (ERROR_TTL_S if release.error else self.ttl)

    # -- writes -------------------------------------------------------
    def ensure(
        self,
        slugs: Iterable[str],
        token: str = "",
        *,
        force: bool = False,
        ttl: int | None = None,
    ) -> dict[str, Release]:
        """Fetch every missing or stale slug, then return the current rows."""
        wanted = list(dict.fromkeys(slug for slug in slugs if SLUG_RE.match(slug)))
        if not wanted:
            return {}
        moment = util.now_ts()
        pending: list[str] = []
        with self._lock:
            stored = self._load()
            for slug in wanted:
                release = stored.get(slug)
                if force or release is None:
                    pending.append(slug)
                    continue
                age = max(0, moment - release.fetched_at)
                limit = ERROR_TTL_S if release.error else (self.ttl if ttl is None else max(60, ttl))
                if age >= limit:
                    pending.append(slug)
        for slug in pending:
            release = fetch_release(slug, token)
            with self._lock:
                self._load()[slug] = release
        if pending:
            self.save()
        return self.rows(wanted)
