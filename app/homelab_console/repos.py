"""Local repo fleet: discovery, status, GitHub pushes and Gitea backup sync.

The Gitea half is ported from the standalone ``gitea-tools`` project (same
author) so the console can be the one place that answers "what is dirty, what
is unpushed, and what has not reached the backup yet".

Design rules:

- Discovery is recursive under each configured root, including the root itself.
  It stops at the first repository on a branch, skips hidden and generated
  directories, and lets the operator exclude individual repos afterwards.
- Gitea is a backup mirror, not a second everyday remote.  Ordinary
  ``git push`` still goes to GitHub only; the console pushes Gitea explicitly.
- Gitea ref checks are on demand.  There is no background network loop.
- A repo id is its path relative to the repo root, re-validated against a
  fresh discovery pass before any mutation.
- No shell strings: every git call is an argv list, with prompts disabled.
"""

from __future__ import annotations

import base64
import copy
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TYPE_CHECKING
from urllib import error as urlerror
from urllib import request as urlrequest
from urllib.parse import urlparse

from . import util
from .config import Settings

if TYPE_CHECKING:  # avoid importing the runner at module load time
    from .runner import TaskManager

log = logging.getLogger("hlc.repos")

DEFAULT_TIMEOUT = 120.0
TOKEN_RE = re.compile(r"^[A-Za-z0-9._~-]{20,300}$")
OWNER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
DISCOVERY_SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "build",
    "dist",
    "target",
}
MAX_DISCOVERY_DIRS = 20000
MAX_DISCOVERED_REPOS = 500
MAX_REPO_EXCLUSIONS = 500
MISSING_REPO_MARKERS = (
    "repository not found",
    "not found",
    "does not exist",
    "no such repository",
    "404",
)
AUTH_MARKERS = (
    "authentication failed",
    "could not read username",
    "could not read password",
    "terminal prompts disabled",
    "invalid username or password",
    "invalid credentials",
    "401",
    "403",
)
UNREACHABLE_MARKERS = (
    "could not resolve host",
    "connection refused",
    "connection timed out",
    "failed to connect",
    "network is unreachable",
    "operation timed out",
    "timed out",
)
STATUS_CAP = 500


class RepoError(RuntimeError):
    """A user-facing repo fleet error."""


class RepoQueueError(RepoError):
    """A queue finished with failures; carries the queue result for the run."""

    def __init__(self, message: str, run_result: dict[str, Any] | None = None):
        super().__init__(message)
        self.run_result = run_result or {}


@dataclass(frozen=True)
class GiteaConfig:
    root: Path | None = None
    url: str = ""
    owner: str = ""
    timeout: float = DEFAULT_TIMEOUT
    prune: bool = True
    force: bool = True

    @property
    def check_timeout(self) -> float:
        # A LAN Gitea answers in milliseconds.  Never let one unreachable
        # repo stall an on-demand check for the full push timeout.
        return max(5.0, min(self.timeout, 15.0))


# --------------------------------------------------------------- config ----


def repo_roots(settings: Settings) -> list[Path]:
    """Configured checkout roots, in order. ``repo_root`` is the legacy alias."""
    raw = settings.get("repo_roots")
    values: list[str] = []
    if isinstance(raw, (list, tuple)):
        values = [str(item).strip() for item in raw if str(item).strip()]
    elif isinstance(raw, str) and raw.strip():
        values = [raw.strip()]
    if not values:
        legacy = str(settings.get("repo_root") or "").strip()
        values = [legacy] if legacy else []
    roots: list[Path] = []
    for value in values:
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = settings.repo / path
        resolved = path.resolve()
        if resolved not in roots:
            roots.append(resolved)
    return roots


def repo_root(settings: Settings) -> Path | None:
    """The first configured checkout root (kept for legacy callers)."""
    roots = repo_roots(settings)
    return roots[0] if roots else None


def root_for_path(path: Path, roots: list[Path]) -> Path | None:
    resolved = Path(path).resolve()
    matches = [root for root in roots if resolved == root or root in resolved.parents]
    return max(matches, key=lambda root: len(root.parts)) if matches else None


def discover_identities(roots: list[Path]) -> list[tuple[Path, dict[str, str]]]:
    """Every repo under every root with a unique id, in stable order."""
    items: list[tuple[Path, dict[str, str]]] = []
    seen_paths: set[Path] = set()
    for root in roots:
        try:
            paths = discover_repos(root)
        except RepoError:
            continue
        for path in paths:
            resolved = path.resolve()
            if resolved in seen_paths:
                continue
            seen_paths.add(resolved)
            items.append((path, repo_identity(path, root)))
    counts = Counter(identity["id"] for _, identity in items)
    if any(count > 1 for count in counts.values()):
        items = [
            (path, {**identity, "id": str(path)} if counts[identity["id"]] > 1 else identity)
            for path, identity in items
        ]
    return items


def gitea_config(settings: Settings) -> GiteaConfig:
    url = str(settings.get("gitea_url") or "").strip().rstrip("/")
    owner = str(settings.get("gitea_owner") or "").strip()
    if not url and not owner:
        raise RepoError("Gitea is not configured — set a URL and owner in Settings")
    if not url:
        raise RepoError("Gitea URL is not configured")
    if not owner:
        raise RepoError("Gitea owner is not configured")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise RepoError("Gitea URL must be an http(s) URL with a hostname")
    if parsed.username or parsed.password:
        raise RepoError("Gitea URL must not contain credentials; use the token setting")
    if (
        parsed.scheme == "http"
        and parsed.hostname not in ("127.0.0.1", "localhost", "::1")
        and os.environ.get("HLC_GITEA_ALLOW_HTTP") != "1"
    ):
        raise RepoError(
            "Gitea URL must use https for a non-loopback host; the token is sent as a header. "
            "Set HLC_GITEA_ALLOW_HTTP=1 only for a trusted network."
        )
    if not OWNER_RE.match(owner):
        raise RepoError("Gitea owner looks invalid")
    try:
        timeout = float(settings.get("gitea_timeout_s") or DEFAULT_TIMEOUT)
    except (TypeError, ValueError) as exc:
        raise RepoError("Gitea timeout must be a number") from exc
    if timeout <= 0 or timeout > 900:
        raise RepoError("Gitea timeout must be between 1 and 900 seconds")
    return GiteaConfig(
        root=repo_root(settings),
        url=url,
        owner=owner,
        timeout=timeout,
        prune=bool(settings.get("gitea_prune", True)),
        force=bool(settings.get("gitea_force", True)),
    )


def resolve_gitea_token(settings: Settings) -> tuple[str, str]:
    """Return a validated ``(token, source)``.  Env wins over the setting."""
    for key in ("HLC_GITEA_TOKEN", "GITEA_TOKEN"):
        value = os.environ.get(key, "").strip()
        if value:
            return (value, key) if TOKEN_RE.match(value) else ("", "invalid")
    value = str(settings.values.get("gitea_token") or "").strip()
    if value:
        return (value, "settings") if TOKEN_RE.match(value) else ("", "invalid")
    return "", ""


def redact(text: str, token: str = "") -> str:
    value = str(text or "")
    if token:
        value = value.replace(token, "***")
    value = re.sub(r"(https?://)[^/\s@]+@", r"\1***@", value)
    value = re.sub(r"(Authorization:\s*(?:Basic|token)\s+)\S+", r"\1***", value, flags=re.I)
    return value[:900]


SAFE_GIT_CONFIG = (
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "protocol.ext.allow=never",
)

# Repo-local keys that can execute code, rewrite a destination or disable TLS.
# A repository that defines one is not safe for an action that carries the
# Gitea token or pushes source code; ask the operator to remove it or use a
# terminal.
_DANGEROUS_REPO_CONFIG = re.compile(
    r"^(?:"
    r"core\.(?:worktree|sshcommand)"
    r"|url\..*\.(?:insteadof|pushinsteadof)"
    r"|filter\..*\.(?:clean|smudge|process)"
    r"|gpg\..*"
    r"|http\.(?:proxy|sslverify)"
    r"|include(?:if)?\..*"
    r")$",
    re.IGNORECASE,
)


def _git(
    repo: Path,
    *args: str,
    timeout: float = 30,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    merged = {
        "GIT_TERMINAL_PROMPT": "0",
        "LC_ALL": "C",
        "LANG": "C",
        **(env or {}),
    }
    return util.run(
        ["git", *SAFE_GIT_CONFIG, "-C", str(repo), *args],
        timeout=timeout,
        env=merged,
        input_text=input_text,
    )


def _ensure_repo_config_safe(repo: Path) -> None:
    result = _git(repo, "config", "--local", "--list", "-z", timeout=10)
    if result.returncode != 0:
        return
    bad: set[str] = set()
    for record in result.stdout.split("\0"):
        if not record:
            continue
        key, _sep, value = record.partition("\n")
        key = key.strip()
        if _DANGEROUS_REPO_CONFIG.match(key):
            bad.add(key)
            continue
        # A shell/absolute credential helper is executable config.  Common
        # helpers such as osxkeychain/store/cache are allowed.
        if key.lower() == "credential.helper" and (
            value.startswith("!") or value.startswith("/")
        ):
            bad.add(key)
    if bad:
        raise RepoError(
            "repository defines unsafe local Git configuration "
            f"({', '.join(sorted(bad)[:4])}{' …' if len(bad) > 4 else ''}); "
            "remove it or use a terminal"
        )


def _gitea_env(cfg: GiteaConfig, token: str) -> dict[str, str]:
    env = {"GIT_TERMINAL_PROMPT": "0"}
    if not token:
        return env
    # Keep the token out of argv (visible in ps) by using git's env-config
    # mechanism.  Gitea accepts the access token as the password.  Scope the
    # header to this Gitea base so a repo-local url.*.insteadOf rewrite cannot
    # carry it to a different host.
    basic = base64.b64encode(f"{cfg.owner}:{token}".encode("utf-8")).decode("ascii")
    base = cfg.url.rstrip("/") + "/"
    env.update(
        {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": f"http.{base}.extraHeader",
            "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}",
        }
    )
    return env


def _assert_push_destination(repo: Path, origin: str) -> None:
    """Refuse a branch whose push would not go to the labelled origin."""
    if not origin:
        raise RepoError("no origin remote")
    branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD", timeout=10).stdout.strip()
    if not branch or branch == "HEAD":
        raise RepoError("HEAD is detached; check out a branch first")
    for key in (f"branch.{branch}.remote", f"branch.{branch}.pushRemote"):
        value = _git(repo, "config", "--get", key, timeout=10).stdout.strip()
        if value and value != "origin":
            raise RepoError(
                f"{key} points at {value!r}, not origin; refusing a push to a different remote"
            )
    pushurl = _git(repo, "config", "--get", "remote.origin.pushurl", timeout=10).stdout.strip()
    if pushurl and pushurl != origin:
        raise RepoError("remote.origin.pushurl differs from origin; refusing the push")
    push_default = _git(repo, "config", "--get", "remote.pushDefault", timeout=10).stdout.strip()
    if push_default and push_default != "origin":
        raise RepoError("remote.pushDefault points away from origin; refusing the push")


# ------------------------------------------------------------ url/repo ----


def github_repo_from_url(url: str) -> tuple[str, str] | None:
    """Return ``(owner, repo)`` for a github.com URL, else ``None``."""
    value = (url or "").strip()
    if not value:
        return None
    if "://" in value:
        parsed = urlparse(value)
        host = (parsed.hostname or "").lower()
        path = parsed.path
    else:
        if ":" not in value:
            return None
        host_part, path = value.split(":", 1)
        host = host_part.rsplit("@", 1)[-1].lower()
    if host == "www.github.com":
        host = "github.com"
    if host != "github.com":
        return None
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    parts = [part for part in path.split("/") if part]
    if len(parts) != 2:
        return None
    return parts[0], parts[1]


def github_url(owner: str, name: str) -> str:
    return f"https://github.com/{owner}/{name}"


def gitea_repo_url(base: str, owner: str, name: str) -> str:
    return f"{base.rstrip('/')}/{owner}/{name}.git"


def gitea_target(origin: str, cfg: GiteaConfig) -> tuple[str | None, str, tuple[str, str] | None]:
    """Return ``(target, skip_reason, github)``."""
    parsed = github_repo_from_url(origin)
    if parsed is None:
        return None, "origin is not a github.com URL", None
    owner, name = parsed
    if owner.lower() != cfg.owner.lower():
        return None, f"github owner {owner} does not match gitea owner {cfg.owner}", parsed
    return gitea_repo_url(cfg.url, cfg.owner, name), "", parsed


def discover_repos(root: Path) -> list[Path]:
    """Find every git repo under ``root``, including ``root`` itself.

    The walk stops at the first repository on each branch, so a repo's
    submodules or nested checkouts are not listed separately. Hidden and
    generated directories are pruned. Roots are commonly a folder of repos,
    but a single checkout is valid too.
    """
    root = root.expanduser()
    if not root.is_dir():
        raise RepoError(f"repo root not found: {root}")
    try:
        root = root.resolve()
    except OSError:
        pass
    try:
        for _ in root.iterdir():
            break
    except StopIteration:
        return []
    except OSError as exc:
        raise RepoError(f"cannot read repo root {root}: {exc}") from exc
    found: list[Path] = []
    seen: set[Path] = set()
    visited = 0
    for current, dirs, _files in os.walk(root, topdown=True, followlinks=False):
        visited += 1
        if visited > MAX_DISCOVERY_DIRS:
            break
        dirs[:] = sorted(
            name
            for name in dirs
            if not name.startswith(".") and name not in DISCOVERY_SKIP_DIRS
        )
        current_path = Path(current)
        if (current_path / ".git").exists():
            resolved = current_path.resolve()
            if resolved not in seen:
                seen.add(resolved)
                found.append(resolved)
                if len(found) >= MAX_DISCOVERED_REPOS:
                    break
            dirs[:] = []
    return found


def repo_identity(path: Path, root: Path) -> dict[str, str]:
    rel = path.relative_to(root).as_posix()
    if rel == ".":
        rel = path.name or "."
    parts = rel.split("/")
    return {
        "id": rel,
        "name": path.name,
        "rel": rel,
        "container": parts[0] if len(parts) > 1 else "",
        "path": str(path),
        "root": str(root),
    }


def _discovery_entries(settings: Settings) -> tuple[list[Path], list[dict[str, Any]], list[str]]:
    """Discover every repo under every root, with the current selection state."""
    roots = repo_roots(settings)
    exclusions = settings.get("repo_exclusions") or {}
    entries: list[dict[str, Any]] = []
    errors: list[str] = []
    seen: set[str] = set()
    for root in roots:
        root_text = str(root)
        try:
            paths = discover_repos(root)
        except RepoError as exc:
            errors.append(str(exc))
            continue
        excluded = set(exclusions.get(root_text) or [])
        for path in paths:
            resolved = str(path.resolve())
            if resolved in seen:
                continue
            seen.add(resolved)
            identity = repo_identity(path, root)
            entries.append(
                {
                    **identity,
                    "root_repo": path == root,
                    "selected": identity["rel"] not in excluded,
                }
            )
    return roots, entries, errors


def validate_repo_exclusions(
    raw: Any,
    roots: list[Path],
    discovered: list[dict[str, Any]],
) -> dict[str, list[str]]:
    """Validate a {root: [repo-relative ids]} exclusion map."""
    if not isinstance(raw, dict):
        raise RepoError("exclusions must be an object keyed by repo root")
    roots_by_text = {str(root): root for root in roots}
    allowed = {(entry["root"], entry["rel"]) for entry in discovered}
    cleaned: dict[str, list[str]] = {}
    for root_text, ids in raw.items():
        root_key = str(root_text or "").strip()
        if root_key not in roots_by_text:
            raise RepoError(f"unknown repo root: {root_key or '(empty)'}")
        if not isinstance(ids, (list, tuple)):
            raise RepoError("each repo root needs a list of excluded repository ids")
        keep: list[str] = []
        for item in ids:
            rel = str(item or "").strip()
            if not rel:
                continue
            if (root_key, rel) not in allowed:
                # A repo can disappear between the scan and the save. Ignore
                # that stale id instead of blocking the whole selection.
                continue
            if rel not in keep:
                keep.append(rel)
            if len(keep) >= MAX_REPO_EXCLUSIONS:
                break
        if keep:
            cleaned[root_key] = keep
    return cleaned


# --------------------------------------------------------------- status ---


def _parse_status(text: str) -> dict[str, Any]:
    """Parse ``git status --porcelain=v2 --branch -z``."""
    records = text.split("\0")
    branch = ""
    head = ""
    upstream = ""
    ahead: int | None = None
    behind: int | None = None
    entries: list[dict[str, Any]] = []
    total_entries = 0
    truncated = False
    index = 0
    while index < len(records):
        line = records[index]
        index += 1
        if not line:
            continue
        if line.startswith("# "):
            header = line[2:]
            key, _, value = header.partition(" ")
            if key == "branch.head":
                branch = value
            elif key == "branch.oid":
                head = value
            elif key == "branch.upstream":
                upstream = value
            elif key == "branch.ab":
                match = re.match(r"^\+(\d+)\s+-(\d+)$", value)
                if match:
                    ahead, behind = int(match.group(1)), int(match.group(2))
            continue
        if line.startswith("1 "):
            parts = line.split(" ", 8)
            if len(parts) < 9:
                continue
            code, path = parts[1], parts[8]
            orig = ""
        elif line.startswith("2 "):
            parts = line.split(" ", 9)
            if len(parts) < 10:
                continue
            code, path = parts[1], parts[9]
            orig = records[index] if index < len(records) else ""
            index += 1
        elif line.startswith("u "):
            parts = line.split(" ", 10)
            if len(parts) < 11:
                continue
            code, path, orig = parts[1], parts[10], ""
        elif line.startswith("? "):
            code, path, orig = "??", line[2:], ""
        else:
            continue
        if not path:
            continue
        total_entries += 1
        staged = bool(code and code[0] not in (" ", ".", "?", "!"))
        unstaged = bool(len(code) > 1 and code[1] not in (" ", ".", "?", "!"))
        untracked = code == "??"
        if len(entries) < STATUS_CAP:
            entries.append(
                {
                    "code": code.strip() or code,
                    "code_raw": code,
                    "path": path,
                    "orig_path": orig,
                    "staged": staged,
                    "unstaged": unstaged,
                    "untracked": untracked,
                }
            )
        else:
            truncated = True
    return {
        "branch": branch,
        "head": head,
        "upstream": upstream,
        "ahead": ahead,
        "behind": behind,
        "entries": entries,
        "total_entries": total_entries,
        "entries_truncated": truncated,
    }


def _ahead_behind(repo: Path, upstream: str) -> tuple[int | None, int | None]:
    result = _git(repo, "rev-list", "--left-right", "--count", f"HEAD...{upstream}", timeout=15)
    if result.returncode != 0:
        return None, None
    parts = result.stdout.strip().split()
    if len(parts) != 2:
        return None, None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None, None


def _last_commit(repo: Path) -> dict[str, Any]:
    result = _git(
        repo,
        "log",
        "-1",
        "--format=%h%x00%at%x00%ad%x00%s",
        "--date=format:%Y-%m-%d %H:%M",
        timeout=15,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return {}
    parts = result.stdout.rstrip("\n").split("\0")
    if len(parts) < 4:
        return {}
    try:
        at = int(parts[1])
    except ValueError:
        at = 0
    return {"sha": parts[0], "at": at, "date": parts[2], "subject": parts[3]}


def _origin(repo: Path) -> str:
    result = _git(repo, "remote", "get-url", "origin", timeout=10)
    if result.returncode != 0:
        return ""
    lines = result.stdout.strip().splitlines()
    return lines[0].strip() if lines else ""


def _uses_lfs(repo: Path) -> bool:
    attributes = repo / ".gitattributes"
    try:
        if attributes.is_symlink() or not attributes.is_file() or attributes.stat().st_size > 1_000_000:
            return False
        with attributes.open("r", encoding="utf-8", errors="replace") as handle:
            text = handle.read(1_000_000)
    except OSError:
        return False
    return "filter=lfs" in text.replace(" ", "")


def _ignored_paths(repo: Path) -> set[str]:
    """Tracked paths that match a .gitignore pattern.

    ``git status`` only hides *untracked* ignored files; a tracked file can
    become ignored later and still show up as modified.  The commit surface
    must hide those too, so list the index entries that match the ignore rules.
    """
    result = _git(repo, "ls-files", "-ci", "--exclude-standard", "-z", timeout=20)
    if result.returncode != 0:
        return set()
    return {path for path in result.stdout.split("\0") if path}


def is_shallow(repo: Path) -> bool:
    result = _git(repo, "rev-parse", "--is-shallow-repository", timeout=10)
    return result.returncode == 0 and result.stdout.strip() == "true"


def repo_row(path: Path, root: Path, cfg: GiteaConfig) -> dict[str, Any]:
    identity = repo_identity(path, root)
    status = _git(
        path,
        "status",
        "--porcelain=v2",
        "--branch",
        "-z",
        "--untracked-files=normal",
        timeout=25,
    )
    if status.returncode != 0:
        return {
            **identity,
            "error": redact((status.stderr or status.stdout).strip()) or "git status failed",
            "branch": "",
            "detached": False,
            "head": "",
            "upstream": "",
            "tracking": False,
            "ahead": None,
            "behind": None,
            "new_branch": False,
            "dirty": False,
            "dirty_count": 0,
            "ignored_count": 0,
            "entries": [],
            "entries_truncated": False,
            "entries_total": 0,
            "last_commit": {},
            "origin": "",
            "github": None,
            "lfs": False,
            "gitea": {"status": "error", "detail": "cannot read repository", "checked_at": 0},
            "needs": {"commit": False, "push": False, "gitea": False},
        }

    parsed = _parse_status(status.stdout)
    branch = parsed["branch"]
    detached = branch == "(detached)"
    upstream = parsed["upstream"]
    ahead = parsed["ahead"]
    behind = parsed["behind"]
    origin = _origin(path)
    github = github_repo_from_url(origin)

    if not detached and not upstream and branch:
        # No configured upstream: fall back to origin/<branch> when it exists,
        # so "commits to push" still works before the first -u push.
        candidate = f"origin/{branch}"
        verify = _git(path, "rev-parse", "--verify", "--quiet", candidate, timeout=10)
        if verify.returncode == 0:
            upstream = candidate
            ahead, behind = _ahead_behind(path, candidate)

    new_branch = bool(origin and not upstream and not detached and parsed["head"])
    if ahead is None and upstream and not detached:
        ahead, behind = _ahead_behind(path, upstream)
    has_commits = bool(parsed["head"] and parsed["head"] != "(initial)")

    needs_push = bool(origin and not detached and has_commits and (
        (ahead is not None and ahead > 0) or (ahead is None and new_branch)
    ))
    entries = parsed["entries"]
    ignored_paths = _ignored_paths(path)
    for entry in entries:
        entry["ignored"] = entry["path"] in ignored_paths
    selectable_entries = [entry for entry in entries if not entry.get("ignored")]
    ignored_entries = [entry for entry in entries if entry.get("ignored")]
    dirty = bool(entries)
    dirty_count = len(selectable_entries)
    ignored_count = len(ignored_entries)
    target, skip_reason, github_pair = gitea_target(origin, cfg)
    if github_pair and not github:
        github = github_pair
    lfs = _uses_lfs(path)
    if lfs:
        gitea = {
            "status": "skip",
            "detail": "Git LFS repo — handle separately",
            "target": target,
            "checked_at": 0,
        }
    elif skip_reason:
        gitea = {
            "status": "skip",
            "detail": skip_reason,
            "target": target,
            "checked_at": 0,
        }
    else:
        gitea = {
            "status": "unknown",
            "detail": "not checked",
            "target": target,
            "checked_at": 0,
        }

    return {
        **identity,
        "error": "",
        "branch": branch,
        "detached": detached,
        "head": parsed["head"],
        "upstream": upstream,
        "tracking": bool(parsed["upstream"]),
        "ahead": ahead,
        "behind": behind,
        "new_branch": new_branch,
        "dirty": dirty,
        "dirty_count": dirty_count,
        "ignored_count": ignored_count,
        "entries": entries,
        "entries_truncated": parsed["entries_truncated"],
        "entries_total": parsed["total_entries"],
        "last_commit": _last_commit(path),
        "origin": redact(origin),
        "github": ({"owner": github[0], "repo": github[1], "url": github_url(*github)} if github else None),
        "lfs": lfs,
        "gitea": gitea,
        "needs": {
            "commit": dirty_count > 0,
            "push": needs_push,
            "gitea": gitea["status"] in ("behind", "missing"),
        },
    }


# --------------------------------------------------------------- gitea ----


@dataclass
class RemoteProbe:
    kind: str = "ok"  # ok | missing | auth | unreachable | error
    exists: bool = False
    refs: dict[str, str] = field(default_factory=dict)
    detail: str = ""


def local_refs(repo: Path) -> dict[str, str]:
    result = _git(
        repo,
        "for-each-ref",
        "--format=%(refname)%00%(objectname)",
        "refs/heads",
        "refs/tags",
        timeout=20,
    )
    if result.returncode != 0:
        raise RepoError(f"git for-each-ref failed: {redact((result.stderr or result.stdout).strip())}")
    refs: dict[str, str] = {}
    for line in result.stdout.splitlines():
        ref, separator, sha = line.partition("\0")
        if separator and ref and sha:
            refs[ref] = sha
    return refs


def parse_ls_remote(output: str) -> dict[str, str]:
    refs: dict[str, str] = {}
    for line in output.splitlines():
        if not line.strip():
            continue
        sha, separator, ref = line.partition("\t")
        if not separator or ref.endswith("^{}"):
            continue
        refs[ref] = sha
    return refs


def looks_like_missing_repo(detail: str) -> bool:
    lowered = detail.lower()
    return any(marker in lowered for marker in MISSING_REPO_MARKERS)


def _probe_remote(repo: Path, target: str, cfg: GiteaConfig, token: str) -> RemoteProbe:
    result = _git(
        repo,
        "ls-remote",
        "--heads",
        "--tags",
        target,
        timeout=cfg.check_timeout,
        env=_gitea_env(cfg, token),
    )
    detail = redact((result.stderr or result.stdout).strip(), token)
    if result.returncode == 0:
        return RemoteProbe(kind="ok", exists=True, refs=parse_ls_remote(result.stdout))
    if looks_like_missing_repo(detail):
        return RemoteProbe(kind="missing", exists=False, detail=detail)
    lowered = detail.lower()
    if any(marker in lowered for marker in AUTH_MARKERS):
        return RemoteProbe(kind="auth", exists=False, detail=detail)
    if any(marker in lowered for marker in UNREACHABLE_MARKERS):
        return RemoteProbe(kind="unreachable", exists=False, detail=detail)
    return RemoteProbe(kind="error", exists=False, detail=detail or "could not read Gitea")


def _compare_refs(local: dict[str, str], remote: dict[str, str]) -> dict[str, Any]:
    missing = sorted(ref for ref in local if ref not in remote)
    changed = sorted(ref for ref in local if ref in remote and remote[ref] != local[ref])
    extra = sorted(ref for ref in remote if ref not in local)
    return {
        "missing": missing,
        "changed": changed,
        "extra": extra,
        "synced": not missing and not changed and not extra,
        "push_count": len(missing) + len(changed),
        "prune_count": len(extra),
    }


def _counts_payload(local: dict[str, str], comparison: dict[str, Any] | None) -> dict[str, Any]:
    payload = {"local": len(local), "remote": 0, "push": 0, "prune": 0, "changed": 0}
    if comparison:
        payload.update(
            {
                "remote": len(local) - len(comparison["missing"]) + len(comparison["extra"]),
                "push": comparison["push_count"],
                "prune": comparison["prune_count"],
                "changed": len(comparison["changed"]),
            }
        )
    return payload


def check_gitea(repo: Path, cfg: GiteaConfig, token: str) -> dict[str, Any]:
    """Compare local refs with Gitea without changing anything."""
    try:
        _ensure_repo_config_safe(repo)
    except RepoError as exc:
        return {"status": "error", "detail": str(exc), "target": None, "checked_at": 0}
    origin = _origin(repo)
    target, skip_reason, _ = gitea_target(origin, cfg)
    if _uses_lfs(repo):
        return {"status": "skip", "detail": "Git LFS repo — handle separately", "target": target, "checked_at": 0}
    if skip_reason:
        return {"status": "skip", "detail": skip_reason, "target": target, "checked_at": 0}
    try:
        local = local_refs(repo)
    except RepoError as exc:
        return {"status": "error", "detail": str(exc), "target": target, "checked_at": 0}
    if not local:
        return {"status": "skip", "detail": "no branches or tags", "target": target, "checked_at": 0}
    probe = _probe_remote(repo, target or "", cfg, token)
    checked = util.now_ts()
    if probe.kind == "missing":
        return {
            "status": "missing",
            "detail": "not in Gitea — a sync will create it",
            "target": target,
            "checked_at": checked,
            "counts": _counts_payload(local, None),
        }
    if probe.kind in ("auth", "unreachable", "error"):
        detail = probe.detail or "could not read Gitea"
        if probe.kind == "auth":
            detail = "Gitea token needed (Settings → Repo fleet)"
        return {
            "status": probe.kind,
            "detail": detail,
            "target": target,
            "checked_at": checked,
            "counts": _counts_payload(local, None),
        }
    comparison = _compare_refs(local, probe.refs)
    if comparison["synced"]:
        return {
            "status": "synced",
            "detail": "all refs match Gitea",
            "target": target,
            "checked_at": checked,
            "counts": _counts_payload(local, comparison),
        }
    bits = []
    if comparison["push_count"]:
        bits.append(f"{comparison['push_count']} ref(s) to sync")
    if comparison["prune_count"]:
        bits.append(f"{comparison['prune_count']} extra on Gitea")
    return {
        "status": "behind",
        "detail": " · ".join(bits) or "refs differ",
        "target": target,
        "checked_at": checked,
        "counts": _counts_payload(local, comparison),
        "comparison": {
            "missing": comparison["missing"][:100],
            "changed": comparison["changed"][:100],
            "extra": comparison["extra"][:100],
        },
    }


def has_common_history(
    repo: Path,
    target: str,
    local: dict[str, str],
    remote: dict[str, str],
    cfg: GiteaConfig,
    token: str,
) -> bool | None:
    """True if Gitea shares history, False if unrelated, None if shallow."""
    if is_shallow(repo):
        return None
    prefix = f"refs/gitea-sync-check/{os.getpid()}-{time.time_ns()}"
    refspecs: list[str] = []
    if any(ref.startswith("refs/heads/") for ref in remote):
        refspecs.append(f"+refs/heads/*:{prefix}/heads/*")
    if any(ref.startswith("refs/tags/") for ref in remote):
        refspecs.append(f"+refs/tags/*:{prefix}/tags/*")
    if not refspecs:
        return False
    try:
        fetched = _git(
            repo,
            "fetch",
            "--no-tags",
            target,
            *refspecs,
            timeout=cfg.timeout,
            env=_gitea_env(cfg, token),
        )
        if fetched.returncode != 0:
            raise RepoError(f"could not fetch Gitea refs for the duplicate check: {redact(fetched.stderr, token)}")
        refs = _git(repo, "for-each-ref", "--format=%(refname)", prefix, timeout=20)
        temp_refs = [line for line in refs.stdout.splitlines() if line]
        if not temp_refs:
            raise RepoError("no Gitea refs fetched for the duplicate check")
        for temp_ref in temp_refs:
            for local_ref in local:
                merge = _git(repo, "merge-base", temp_ref, local_ref, timeout=min(cfg.timeout, 30))
                if merge.returncode == 0:
                    return True
                if merge.returncode != 1:
                    raise RepoError(
                        f"merge-base check failed: {redact((merge.stderr or merge.stdout).strip(), token)}"
                    )
        return False
    finally:
        try:
            remaining = _git(repo, "for-each-ref", "--format=%(refname)", prefix, timeout=20)
            for temp_ref in remaining.stdout.splitlines():
                if temp_ref:
                    _git(repo, "update-ref", "-d", temp_ref, timeout=10)
        except RepoError:
            pass


def push_to_gitea(repo: Path, target: str, cfg: GiteaConfig, token: str) -> None:
    args = ["push", "--no-verify"]
    if cfg.prune:
        args.append("--prune")
    if cfg.force:
        args.append("--force")
    args += [target, "refs/heads/*:refs/heads/*", "refs/tags/*:refs/tags/*"]
    result = _git(repo, *args, timeout=cfg.timeout, env=_gitea_env(cfg, token))
    if result.returncode != 0:
        detail = redact((result.stderr or result.stdout).strip(), token)
        raise RepoError(f"Gitea sync failed: {detail}")


def sync_review_hash(
    target: str | None,
    local: dict[str, str],
    remote_refs: dict[str, str],
    cfg: GiteaConfig,
    status: str,
) -> str:
    """Bind a force/prune mirror to the refs the operator previewed."""
    payload = {
        "target": target or "",
        "local": local,
        "remote": remote_refs,
        "prune": bool(cfg.prune),
        "force": bool(cfg.force),
        "status": status,
    }
    return util.sha256_text(util.stable_json(payload))


def sync_repo(
    repo: Path,
    cfg: GiteaConfig,
    token: str,
    *,
    dry_run: bool = False,
    review_hash: str = "",
) -> dict[str, Any]:
    """Mirror local heads/tags to Gitea.  Returns a result dict, never raises."""
    try:
        _ensure_repo_config_safe(repo)
    except RepoError as exc:
        return {"ok": False, "status": "error", "detail": str(exc), "target": None}
    origin = _origin(repo)
    target, skip_reason, _ = gitea_target(origin, cfg)
    base = {"target": target, "dry_run": dry_run, "ok": False}
    if _uses_lfs(repo):
        return {**base, "ok": True, "status": "skip", "detail": "Git LFS repo — handle separately"}
    if skip_reason:
        return {**base, "ok": True, "status": "skip", "detail": skip_reason}
    try:
        local = local_refs(repo)
    except RepoError as exc:
        return {**base, "status": "error", "detail": str(exc)}
    if not local:
        return {**base, "ok": True, "status": "skip", "detail": "no branches or tags"}
    try:
        probe = _probe_remote(repo, target or "", cfg, token)
    except RepoError as exc:
        return {**base, "status": "error", "detail": str(exc)}
    if probe.kind == "auth":
        return {**base, "status": "error", "detail": "Gitea token needed (Settings → Repo fleet)"}
    if probe.kind == "unreachable":
        return {**base, "status": "error", "detail": probe.detail or "Gitea is unreachable"}
    if probe.kind == "error":
        return {**base, "status": "error", "detail": probe.detail or "could not read Gitea"}

    preview_status = "would-create" if probe.kind == "missing" else "would-sync"
    current_review = sync_review_hash(target, local, probe.refs, cfg, preview_status)
    if review_hash and review_hash != current_review:
        return {
            **base,
            "status": "stale",
            "detail": "the Gitea refs changed since the preview — run the dry run again",
            "review_hash": current_review,
        }

    if probe.kind == "missing":
        if dry_run:
            return {
                **base,
                "ok": True,
                "status": "would-create",
                "detail": f"would create {target}",
                "counts": _counts_payload(local, None),
                "review_hash": current_review,
            }
        try:
            push_to_gitea(repo, target or "", cfg, token)
        except RepoError as exc:
            return {**base, "status": "error", "detail": str(exc)}
        return {
            **base,
            "ok": True,
            "status": "created",
            "detail": f"created {target}",
            "counts": _counts_payload(local, None),
        }

    if probe.refs == local:
        return {
            **base,
            "ok": True,
            "status": "up-to-date",
            "detail": f"{len(local)} ref(s) already match",
            "counts": _counts_payload(local, _compare_refs(local, probe.refs)),
        }

    comparison = _compare_refs(local, probe.refs)
    if dry_run:
        pieces = [f"would sync {comparison['push_count']} ref(s)"]
        if comparison["prune_count"]:
            pieces.append(f"prune {comparison['prune_count']} extra")
        pieces.append("history check skipped")
        return {
            **base,
            "ok": True,
            "status": "would-sync",
            "detail": "; ".join(pieces),
            "counts": _counts_payload(local, comparison),
            "comparison": {
                "missing": comparison["missing"][:100],
                "changed": comparison["changed"][:100],
                "extra": comparison["extra"][:100],
            },
            "review_hash": current_review,
        }

    if not probe.refs:
        try:
            push_to_gitea(repo, target or "", cfg, token)
        except RepoError as exc:
            return {**base, "status": "error", "detail": str(exc)}
        return {
            **base,
            "ok": True,
            "status": "synced",
            "detail": "initialized an empty Gitea repo",
            "counts": _counts_payload(local, comparison),
        }

    try:
        common = has_common_history(repo, target or "", local, probe.refs, cfg, token)
    except RepoError as exc:
        return {**base, "status": "error", "detail": str(exc)}
    if common is None:
        return {**base, "ok": True, "status": "skip", "detail": "shallow clone; cannot verify Gitea history"}
    if not common:
        return {
            **base,
            "ok": True,
            "status": "skip",
            "detail": "Gitea repo has unrelated history; rename or delete it in Gitea, then sync",
        }
    try:
        push_to_gitea(repo, target or "", cfg, token)
    except RepoError as exc:
        return {**base, "status": "error", "detail": str(exc)}
    pieces = [f"synced {len(local)} ref(s)"]
    if comparison["prune_count"]:
        pieces.append(f"pruned {comparison['prune_count']}")
    return {
        **base,
        "ok": True,
        "status": "synced",
        "detail": "; ".join(pieces),
        "counts": _counts_payload(local, comparison),
    }


# -------------------------------------------------------- connectivity ----

def _empty_connectivity() -> dict[str, Any]:
    return {
        "state": "never",
        "checked_at": 0,
        "github": {"state": "unknown", "checked_at": 0, "latency_ms": 0, "detail": "not checked"},
        "gitea": {"state": "unknown", "checked_at": 0, "latency_ms": 0, "detail": "not checked", "version": ""},
    }


def _resolved_addresses(url: str) -> str:
    """What this process actually resolves the URL's host to.

    "Unreachable" is otherwise indistinguishable from DNS, routing, a firewall
    and a dead service. In a packaged app the answer is often not what the
    terminal sees, so report it next to the error.
    """
    parsed = urlparse(url)
    host = parsed.hostname
    if not host:
        return ""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        return f"resolve failed: {exc}"
    addresses: list[str] = []
    for info in infos:
        address = info[4][0]
        if address not in addresses:
            addresses.append(address)
    return f"resolved {', '.join(addresses)}" if addresses else "resolved nothing"


def _probe_http(url: str, *, method: str = "GET", timeout: float = 6, headers: dict[str, str] | None = None) -> dict[str, Any]:
    started = time.monotonic()
    request_headers = {"User-Agent": "HomelabConsole/0.1", **(headers or {})}
    request = urlrequest.Request(url, method=method, headers=request_headers)
    try:
        with util.urlopen(request, timeout=timeout) as response:
            body = b"" if method == "HEAD" else response.read(65536)
            return {
                "reachable": True,
                "status": int(response.getcode() or 0),
                "latency_ms": int((time.monotonic() - started) * 1000),
                "body": body,
                "error": "",
            }
    except urlerror.HTTPError as exc:
        body = b"" if method == "HEAD" else exc.read(65536)
        return {
            "reachable": True,
            "status": int(exc.code or 0),
            "latency_ms": int((time.monotonic() - started) * 1000),
            "body": body,
            "error": str(exc),
        }
    except Exception as exc:  # DNS, TLS, timeout, offline
        resolved = _resolved_addresses(url)
        return {
            "reachable": False,
            "status": 0,
            "latency_ms": int((time.monotonic() - started) * 1000),
            "body": b"",
            "error": f"{type(exc).__name__}: {exc}" + (f" ({resolved})" if resolved else ""),
        }


def probe_github() -> dict[str, Any]:
    result = _probe_http("https://github.com", method="HEAD", timeout=6)
    checked = util.now_ts()
    if not result["reachable"]:
        return {
            "state": "down",
            "checked_at": checked,
            "latency_ms": result["latency_ms"],
            "detail": result["error"] or "unreachable",
        }
    state = "up" if result["status"] < 500 else "down"
    return {
        "state": state,
        "checked_at": checked,
        "latency_ms": result["latency_ms"],
        "detail": f"HTTP {result['status']} · {result['latency_ms']} ms",
    }


def probe_gitea(cfg: GiteaConfig, token: str) -> dict[str, Any]:
    headers = {"Authorization": f"token {token}"} if token else {}
    result = _probe_http(f"{cfg.url}/api/v1/version", timeout=6, headers=headers)
    checked = util.now_ts()
    base = {
        "checked_at": checked,
        "latency_ms": result["latency_ms"],
        "version": "",
        "detail": "",
    }
    if not result["reachable"]:
        return {**base, "state": "down", "detail": redact(result["error"], token) or "unreachable"}
    status = result["status"]
    if status in (401, 403):
        return {**base, "state": "auth", "detail": f"HTTP {status} — token needed or invalid"}
    if status >= 500:
        return {**base, "state": "down", "detail": f"HTTP {status} · {result['latency_ms']} ms"}
    if status >= 400:
        return {**base, "state": "error", "detail": f"HTTP {status} — check the Gitea URL"}
    version = ""
    if result["body"]:
        try:
            payload = json.loads(result["body"].decode("utf-8", errors="replace"))
            version = str(payload.get("version") or "")
        except (ValueError, AttributeError):
            version = ""
    return {
        **base,
        "state": "up",
        "version": version,
        "detail": f"HTTP {status} · {result['latency_ms']} ms" + (f" · v{version}" if version else ""),
    }


# --------------------------------------------------------------- queue ----


@dataclass
class QueueItem:
    repo_id: str
    name: str
    github: str = "pending"  # pending | running | ok | skipped | failed
    gitea: str = "pending"
    github_detail: str = ""
    gitea_detail: str = ""
    status: str = "queued"  # queued | running | ok | partial | failed | skipped
    error: str = ""

    def public(self) -> dict[str, Any]:
        return {
            "id": self.repo_id,
            "name": self.name,
            "github": self.github,
            "gitea": self.gitea,
            "github_detail": self.github_detail,
            "gitea_detail": self.gitea_detail,
            "status": self.status,
            "error": self.error,
        }


@dataclass
class RepoQueue:
    id: str
    items: list[QueueItem]
    status: str = "queued"
    created: int = field(default_factory=util.now_ts)
    started: int = 0
    finished: int = 0
    run_id: str = ""
    error: str = ""

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status,
            "created": self.created,
            "started": self.started,
            "finished": self.finished,
            "run_id": self.run_id,
            "error": self.error,
            "items": [item.public() for item in self.items],
            "completed": sum(1 for item in self.items if item.status in ("ok", "failed", "partial", "skipped")),
            "total": len(self.items),
        }


# --------------------------------------------------------------- fleet ----


class RepoFleet:
    def __init__(self, settings: Settings, store: Any, tasks: "TaskManager | None" = None):
        self.settings = settings
        self.store = store
        self.tasks = tasks
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._gitea_thread: threading.Thread | None = None
        self._gitea_checking = False
        self._gitea_meta: dict[str, Any] = {"state": "never", "checked_at": 0, "checked": 0, "errors": 0, "message": ""}
        self._connectivity = _empty_connectivity()
        self._connectivity_checking = False
        self._connectivity_thread: threading.Thread | None = None
        roots = repo_roots(settings)
        self._snapshot: dict[str, Any] = {
            "root": str(roots[0]) if roots else "",
            "roots": [str(root) for root in roots],
            "root_configured": bool(roots),
            "root_error": "",
            "gitea_config_error": "",
            "repos": [],
            "discovered": [],
            "exclusions": {},
            "discovery_errors": [],
            "summary": {},
            "generated_at": 0,
            "scanning": False,
        }
        self._queue: RepoQueue | None = None
        self._repo_locks: dict[str, threading.Lock] = {}
        self._locks_lock = threading.Lock()

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="hlc-repos", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=5)
        if self._gitea_thread:
            self._gitea_thread.join(timeout=5)
        if self._connectivity_thread:
            self._connectivity_thread.join(timeout=5)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.refresh()
            except Exception:
                log.exception("repo scan failed")
            try:
                interval = int(self.settings.get("repo_poll_interval_s", 10))
            except (TypeError, ValueError):
                interval = 10
            interval = max(5, min(120, interval))
            self._wake.wait(interval)
            self._wake.clear()

    # ---------------------------------------------------------------- state
    def request_refresh(self) -> None:
        self._wake.set()
        if not (self._thread and self._thread.is_alive()):
            try:
                self.refresh()
            except Exception:
                log.exception("repo scan failed")

    def refresh(self) -> dict[str, Any]:
        with self._lock:
            self._snapshot["scanning"] = True
        try:
            roots = repo_roots(self.settings)
            if not roots:
                with self._lock:
                    self._snapshot = {
                        "root": "",
                        "roots": [],
                        "root_configured": False,
                        "root_error": "",
                        "gitea_config_error": "",
                        "repos": [],
                        "discovered": [],
                        "exclusions": {},
                        "discovery_errors": [],
                        "summary": self._summary([], []),
                        "generated_at": util.now_ts(),
                        "scanning": False,
                    }
                    return copy.deepcopy(self._snapshot)
            gitea_error = ""
            gitea_unconfigured = False
            try:
                cfg = gitea_config(self.settings)
            except RepoError as exc:
                # A bad Gitea setting must not hide the local repo fleet. Empty
                # URL and owner is not an error: Gitea is simply optional.
                cfg = GiteaConfig(root=roots[0])
                if str(self.settings.get("gitea_url") or "").strip() or str(
                    self.settings.get("gitea_owner") or ""
                ).strip():
                    gitea_error = str(exc)
                else:
                    gitea_unconfigured = True
            discovered_entries: list[dict[str, Any]]
            _, discovered_entries, root_errors = _discovery_entries(self.settings)
            selected = [
                (Path(entry["path"]), Path(entry["root"]))
                for entry in discovered_entries
                if entry.get("selected")
            ]
            rows: list[dict[str, Any]] = []
            if selected:
                workers = min(4, max(1, len(selected)))
                with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="hlc-repo-scan") as pool:
                    rows = list(pool.map(lambda item: repo_row(item[0], item[1], cfg), selected))
            # Two roots can contain the same relative repo name; fall back to
            # the absolute path as the id so selection and actions stay unique.
            counts = Counter(row.get("id") for row in rows)
            for row in rows:
                if counts.get(row.get("id"), 0) > 1:
                    row["id"] = row.get("path") or row["id"]
            if gitea_error:
                for row in rows:
                    row["gitea"] = {"status": "error", "detail": gitea_error, "target": None, "checked_at": 0}
                    row.setdefault("needs", {})["gitea"] = False
            elif gitea_unconfigured:
                for row in rows:
                    row["gitea"] = {
                        "status": "skip",
                        "detail": "Gitea is not configured",
                        "target": None,
                        "checked_at": 0,
                    }
                    row.setdefault("needs", {})["gitea"] = False
            with self._lock:
                old = {row.get("id"): row for row in self._snapshot.get("repos", [])}
                for row in rows:
                    previous = old.get(row["id"])
                    if not previous:
                        continue
                    previous_gitea = previous.get("gitea") or {}
                    if (
                        not gitea_error
                        and previous.get("head")
                        and previous.get("head") == row.get("head")
                        and previous_gitea.get("status") not in ("unknown", "checking", None)
                    ):
                        row["gitea"] = previous_gitea
                self._snapshot = {
                    "root": str(roots[0]),
                    "roots": [str(root) for root in roots],
                    "root_configured": True,
                    "root_error": "; ".join(root_errors),
                    "gitea_config_error": gitea_error,
                    "repos": rows,
                    "discovered": discovered_entries,
                    "exclusions": copy.deepcopy(self.settings.get("repo_exclusions") or {}),
                    "discovery_errors": root_errors,
                    "summary": self._summary(rows, discovered_entries),
                    "generated_at": util.now_ts(),
                    "scanning": False,
                }
                self._refresh_needs(rows)
                snapshot = self._snapshot
            return snapshot
        finally:
            with self._lock:
                self._snapshot["scanning"] = False

    @staticmethod
    def _summary(rows: list[dict[str, Any]], discovered: list[dict[str, Any]]) -> dict[str, Any]:
        summary = _summarise(rows)
        summary["discovered"] = len(discovered)
        summary["excluded"] = sum(1 for entry in discovered if not entry.get("selected"))
        return summary

    def _refresh_needs(self, rows: list[dict[str, Any]]) -> None:
        for row in rows:
            gitea = row.get("gitea") or {}
            row.setdefault("needs", {})
            row["needs"]["gitea"] = gitea.get("status") in ("behind", "missing")
        self._snapshot["summary"] = self._summary(rows, self._snapshot.get("discovered") or [])

    def discovered(self) -> dict[str, Any]:
        """Return every discovered repo and whether Repo street manages it."""
        roots, entries, errors = _discovery_entries(self.settings)
        return {
            "root_configured": bool(roots),
            "roots": [str(root) for root in roots],
            "repos": entries,
            "errors": errors,
            "exclusions": copy.deepcopy(self.settings.get("repo_exclusions") or {}),
        }

    def set_repo_exclusions(self, raw: Any) -> dict[str, Any]:
        """Persist a {root: [relative ids]} exclusion map and rescan."""
        roots, entries, _errors = _discovery_entries(self.settings)
        if not roots:
            raise RepoError("repo roots are not configured — choose a folder in Settings")
        cleaned = validate_repo_exclusions(raw, roots, entries)
        self.settings.update({"repo_exclusions": cleaned})
        state = self.refresh()
        return {
            "roots": [str(root) for root in roots],
            "discovered": state.get("discovered", []),
            "exclusions": cleaned,
            "errors": state.get("discovery_errors", []),
        }

    def state(self) -> dict[str, Any]:
        with self._lock:
            payload = copy.deepcopy(self._snapshot)
            payload["gitea"] = copy.deepcopy(self._gitea_meta)
            payload["connectivity"] = copy.deepcopy(self._connectivity)
            payload["queue"] = self._queue.public() if self._queue else None
            payload["vscode"] = {"available": vscode_available()}
        configured = self.gitea_configured()
        if not configured and payload["gitea"].get("state") != "checking":
            payload["gitea"] = {
                "state": "unconfigured",
                "checked_at": payload["gitea"].get("checked_at", 0),
                "checked": 0,
                "errors": 0,
                "message": "Gitea is not configured — set a URL and owner in Settings",
            }
        payload["gitea_configured"] = configured
        return payload

    def get_queue(self, queue_id: str) -> dict[str, Any] | None:
        with self._lock:
            if self._queue and self._queue.id == queue_id:
                return self._queue.public()
            return None

    def _rows(self) -> list[dict[str, Any]]:
        with self._lock:
            return copy.deepcopy(self._snapshot.get("repos", []))

    def _row(self, repo_id: str) -> dict[str, Any] | None:
        with self._lock:
            return next((row for row in self._snapshot.get("repos", []) if row.get("id") == repo_id), None)

    def _set_gitea(self, repo_id: str, result: dict[str, Any]) -> None:
        now = util.now_ts()
        with self._lock:
            row = next((item for item in self._snapshot.get("repos", []) if item.get("id") == repo_id), None)
            if not row:
                return
            previous = row.get("gitea") or {}
            status = result.get("status") or "error"
            if status in ("created", "up-to-date"):
                status = "synced"
            gitea = {
                "status": status,
                "detail": result.get("detail") or "",
                "target": result.get("target") or previous.get("target"),
                "checked_at": result.get("checked_at") or now,
            }
            if result.get("counts"):
                gitea["counts"] = result["counts"]
            if result.get("comparison"):
                gitea["comparison"] = result["comparison"]
            row["gitea"] = gitea
            row.setdefault("needs", {})["gitea"] = gitea["status"] in ("behind", "missing")
            self._snapshot["summary"] = self._summary(
                self._snapshot.get("repos", []), self._snapshot.get("discovered") or []
            )

    def check_gitea_repo(self, repo_id: str) -> dict[str, Any] | None:
        """Refresh one repo's Gitea comparison without checking the whole fleet.

        Local actions (commit, GitHub push) invalidate only that repo's cached
        comparison. A single ``ls-remote`` is enough to put the row back to
        ``synced``/``behind``/``missing`` instead of leaving it at "not checked"
        until someone presses Check Gitea.
        """
        if self._stop.is_set():
            return None
        row = self._row(repo_id)
        if not row:
            return None
        gitea = row.get("gitea") or {}
        if not gitea.get("target") or gitea.get("status") in ("skip", "checking"):
            return None
        token, _ = resolve_gitea_token(self.settings)
        try:
            cfg = gitea_config(self.settings)
            with self._lock_for(repo_id):
                result = check_gitea(Path(row["path"]), cfg, token)
        except Exception as exc:
            result = {
                "status": "error",
                "detail": redact(str(exc), token),
                "target": gitea.get("target"),
                "checked_at": util.now_ts(),
            }
        if self._stop.is_set():
            return None
        self._set_gitea(repo_id, result)
        return result

    def recheck_gitea_async(self, repo_id: str) -> bool:
        """Kick off a one-repo Gitea check after a local action."""
        if not self.settings.get("gitea_check_on_start", True):
            return False
        row = self._row(repo_id)
        if not row or not (row.get("gitea") or {}).get("target"):
            return False
        if (row.get("gitea") or {}).get("status") in ("skip", "auth", "unreachable", "error", "checking"):
            return False
        thread = threading.Thread(
            target=self._recheck_gitea,
            args=(repo_id,),
            name=f"hlc-gitea-recheck-{repo_id.replace('/', '-')}",
            daemon=True,
        )
        thread.start()
        return True

    def _recheck_gitea(self, repo_id: str) -> None:
        try:
            result = self.check_gitea_repo(repo_id)
            if result and result.get("status") in ("auth", "unreachable", "error"):
                log.warning("post-action Gitea check for %s: %s", repo_id, result.get("detail"))
        except Exception:
            log.exception("post-action Gitea check failed for %s", repo_id)

    def _after_action(self, repo_id: str, *, connectivity: bool = False) -> None:
        """Refresh the remote-facing status for a repo the user just touched."""
        self.recheck_gitea_async(repo_id)
        if connectivity:
            self.check_connectivity_async()

    # -------------------------------------------------------------- gitea --
    def gitea_configured(self) -> bool:
        """True when Gitea has a usable URL and owner — no network involved.

        Deliberately does not require a stored token: git can authenticate
        through the credential helper, which is how many setups check refs. A
        check that fails for auth reasons still lands in the attention list, so
        it cannot block the boot screen.
        """
        try:
            gitea_config(self.settings)
        except RepoError:
            return False
        return True

    def config_changed(self) -> None:
        with self._lock:
            for row in self._snapshot.get("repos", []):
                gitea = row.get("gitea") or {}
                if gitea.get("status") != "skip":
                    row["gitea"] = {
                        "status": "unknown",
                        "detail": "configuration changed — check again",
                        "target": gitea.get("target"),
                        "checked_at": 0,
                    }
                    row.setdefault("needs", {})["gitea"] = False
            self._gitea_meta = {"state": "never", "checked_at": 0, "checked": 0, "errors": 0, "message": "configuration changed"}
            self._connectivity = _empty_connectivity()
            self._snapshot["summary"] = self._summary(
                self._snapshot.get("repos", []), self._snapshot.get("discovered") or []
            )
        # Settings changes must be visible in the response, not one poll later.
        self.refresh()

    def check_gitea_async(self) -> bool:
        if not self.gitea_configured():
            with self._lock:
                self._gitea_meta = {
                    "state": "unconfigured",
                    "checked_at": self._gitea_meta.get("checked_at", 0),
                    "checked": 0,
                    "errors": 0,
                    "message": "Gitea is not configured — set a URL and owner in Settings",
                }
            return False
        with self._lock:
            if self._gitea_checking:
                return False
            self._gitea_checking = True
            self._gitea_meta = {
                "state": "checking",
                "checked_at": self._gitea_meta.get("checked_at", 0),
                "checked": 0,
                "errors": 0,
                "message": "reading Gitea refs…",
            }
            thread = threading.Thread(target=self._check_gitea, name="hlc-gitea-check", daemon=True)
            self._gitea_thread = thread
        thread.start()
        return True

    def _check_gitea(self) -> None:
        try:
            cfg = gitea_config(self.settings)
            token, _ = resolve_gitea_token(self.settings)
            rows = [row for row in self._rows() if (row.get("gitea") or {}).get("status") != "skip"]
            checked = 0
            errors = 0

            def probe(row: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
                try:
                    result = check_gitea(Path(row["path"]), cfg, token)
                except Exception as exc:  # one bad repo must not abort the check
                    return row, {"status": "error", "detail": redact(str(exc), token)}
                if result.get("status") == "unreachable":
                    # The git error hides which address this process tried;
                    # that is the whole question when the terminal disagrees.
                    resolved = _resolved_addresses(cfg.url)
                    if resolved:
                        result["detail"] = f"{result.get('detail', '')} ({resolved})".strip()
                return row, result

            # Ref checks are independent `git ls-remote` calls over the tailnet;
            # four at a time keeps a startup check off the boot screen. The
            # results are applied serially under the fleet lock.
            workers = min(4, max(1, len(rows)))
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="hlc-gitea-probe") as pool:
                for row, result in pool.map(probe, rows):
                    self._set_gitea(row["id"], result)
                    checked += 1
                    if result.get("status") in ("auth", "unreachable", "error"):
                        errors += 1
            with self._lock:
                self._gitea_meta = {
                    "state": "partial" if errors else "done",
                    "checked_at": util.now_ts(),
                    "checked": checked,
                    "errors": errors,
                    "message": f"{checked} repo(s) checked" + (f"; {errors} error(s)" if errors else ""),
                }
        except Exception as exc:
            log.exception("Gitea check failed")
            with self._lock:
                self._gitea_meta = {
                    "state": "error",
                    "checked_at": util.now_ts(),
                    "checked": 0,
                    "errors": 1,
                    "message": redact(str(exc)),
                }
        finally:
            with self._lock:
                self._gitea_checking = False

    # ------------------------------------------------------- connectivity
    def check_connectivity_async(self) -> bool:
        with self._lock:
            if self._connectivity_checking:
                return False
            self._connectivity_checking = True
            self._connectivity = {
                **self._connectivity,
                "state": "checking",
                "github": {**self._connectivity.get("github", {}), "state": "checking"},
                "gitea": {**self._connectivity.get("gitea", {}), "state": "checking"},
            }
            thread = threading.Thread(target=self._check_connectivity, name="hlc-connectivity", daemon=True)
            self._connectivity_thread = thread
        thread.start()
        return True

    def _check_connectivity(self) -> None:
        try:
            github = probe_github()
            try:
                cfg = gitea_config(self.settings)
                token, _ = resolve_gitea_token(self.settings)
                gitea = probe_gitea(cfg, token)
            except RepoError as exc:
                gitea = {
                    "state": "unconfigured",
                    "checked_at": util.now_ts(),
                    "latency_ms": 0,
                    "detail": str(exc),
                    "version": "",
                }
            with self._lock:
                self._connectivity = {
                    "state": "done",
                    "checked_at": util.now_ts(),
                    "github": github,
                    "gitea": gitea,
                }
        finally:
            with self._lock:
                self._connectivity_checking = False

    # -------------------------------------------------------------- actions
    def _lock_for(self, repo_id: str) -> threading.Lock:
        with self._locks_lock:
            lock = self._repo_locks.get(repo_id)
            if lock is None:
                lock = threading.Lock()
                self._repo_locks[repo_id] = lock
            return lock

    def resolve(self, repo_id: str) -> tuple[Path, dict[str, str]]:
        # Local actions (commit, push, open) only need the checkout roots; the
        # Gitea URL/owner is optional and must not gate them.
        roots = repo_roots(self.settings)
        if not roots:
            raise RepoError("repo roots are not configured — choose a folder in Settings")
        matches = discover_identities(roots)
        exact = [(path, identity) for path, identity in matches if identity["id"] == repo_id]
        if len(exact) == 1:
            return exact[0]
        if len(exact) > 1:
            raise RepoError(f"repository id is ambiguous: {repo_id!r}")
        # Selections can outlive a roots change; fall back to the relative name
        # only when it identifies exactly one checkout.
        for predicate in (
            lambda path, identity: identity["rel"] == repo_id,
            lambda path, identity: str(path) == repo_id,
        ):
            candidates = [(path, identity) for path, identity in matches if predicate(path, identity)]
            if len(candidates) == 1:
                return candidates[0]
            if len(candidates) > 1:
                raise RepoError(f"repository id is ambiguous: {repo_id!r}")
        raise RepoError(
            f"repository not found under {', '.join(str(root) for root in roots)}: {repo_id or '(empty)'}"
        )

    def open_in_vscode(self, repo_id: str) -> dict[str, Any]:
        path, identity = self.resolve(repo_id)
        code = _find_vscode()
        if not code:
            raise RepoError(
                "VS Code command not found. Install the 'code' command in PATH, "
                "or install Visual Studio Code in /Applications."
            )
        try:
            subprocess.Popen(
                [code, "-n", str(path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                env=util.child_env(),
                start_new_session=True,
            )
        except OSError as exc:
            raise RepoError(f"could not open VS Code: {exc}") from exc
        self.store.add_event("repo", "success", f"opened {identity['rel']} in VS Code")
        return {"ok": True, "path": str(path)}

    def refresh_after_commit(self, path: Path) -> bool:
        """Recheck Gitea after a commit made outside Repo street.

        The console's stack commit writes to the same checkout that Repo
        street scans. Refresh the rows, map the checkout back to its repo id,
        and refresh that row's Gitea comparison.
        """
        self.refresh()
        try:
            resolved = Path(path).resolve()
            for candidate, identity in discover_identities(repo_roots(self.settings)):
                if candidate == resolved:
                    return self.recheck_gitea_async(identity["id"])
        except (RepoError, ValueError):
            return False
        return False

    def commit(self, repo_id: str, files: Any, message: str) -> dict[str, Any]:
        path, identity = self.resolve(repo_id)
        _ensure_repo_config_safe(path)
        message = str(message or "").strip()
        if not message or len(message) > 200 or message.startswith("-") or "\x00" in message:
            raise RepoError("commit message must be 1–200 characters and not start with '-'")
        if not isinstance(files, list) or not files:
            raise RepoError("select at least one file to commit")
        selected: list[str] = []
        for raw in files:
            value = str(raw or "").strip()
            if not value or value.startswith("/") or ".." in Path(value).parts or "\x00" in value:
                raise RepoError(f"invalid file path: {value!r}")
            if util.is_secret_file(value):
                raise RepoError(f"{value} is a .env-family secret and cannot be committed from the console")
            if value not in selected:
                selected.append(value)
        roots = repo_roots(self.settings)
        root = root_for_path(path, roots) or path.parent
        status = repo_row(path, root, GiteaConfig(root=root))
        if status.get("error"):
            raise RepoError(status["error"])
        by_path = {entry["path"]: entry for entry in status.get("entries", [])}
        missing = [value for value in selected if value not in by_path]
        if missing:
            raise RepoError(
                "the working tree changed since this view loaded; refresh and reselect "
                f"({', '.join(missing[:3])})"
            )
        paths: list[str] = []
        for value in selected:
            entry = by_path[value]
            if entry.get("ignored"):
                raise RepoError(f"{value} is ignored by .gitignore and cannot be committed from the console")
            paths.append(entry["path"])
            if entry.get("orig_path") and entry["orig_path"] not in paths:
                paths.append(entry["orig_path"])
        with self._lock_for(repo_id):
            add = _git(path, "add", "-A", "--", *paths, timeout=60)
            if add.returncode != 0:
                raise RepoError(f"git add failed: {redact((add.stderr or add.stdout).strip())}")
            commit = _git(path, "commit", "-m", message, "--", *paths, timeout=180)
            if commit.returncode != 0:
                detail = redact((commit.stderr or commit.stdout).strip())
                raise RepoError(f"git commit failed: {detail[:900]}")
        self.store.add_event("repo", "success", f"committed {identity['rel']}: {message}")
        self.refresh()
        self._after_action(repo_id)
        return {
            "ok": True,
            "commit": (commit.stdout.strip().splitlines() or [""])[0],
            "files": selected,
            "repo": identity["rel"],
        }

    def push_github(self, repo_id: str, *, post_action: bool = True) -> dict[str, Any]:
        path, identity = self.resolve(repo_id)
        try:
            _ensure_repo_config_safe(path)
        except RepoError as exc:
            return {"ok": False, "status": "error", "detail": str(exc)}
        with self._lock_for(repo_id):
            roots = repo_roots(self.settings)
            root = root_for_path(path, roots) or path.parent
            status = repo_row(path, root, GiteaConfig(root=root))
            if status.get("error"):
                return {"ok": False, "status": "error", "detail": status["error"]}
            if status.get("detached"):
                return {"ok": False, "status": "error", "detail": "HEAD is detached; check out a branch first"}
            origin_url = _origin(path)
            if not origin_url:
                return {"ok": False, "status": "error", "detail": "no origin remote"}
            if not status.get("needs", {}).get("push") and status.get("ahead") == 0:
                return {"ok": True, "status": "up-to-date", "detail": "already up to date"}
            try:
                _assert_push_destination(path, origin_url)
            except RepoError as exc:
                return {"ok": False, "status": "error", "detail": str(exc)}
            upstream = status.get("upstream") or ""
            if status.get("tracking") and upstream:
                command = ["push", "--no-verify"]
            else:
                command = ["push", "--no-verify", "-u", "origin", "HEAD"]
            result = _git(path, *command, timeout=180)
            if result.returncode != 0:
                detail = redact((result.stderr or result.stdout).strip())
                lowered = detail.lower()
                if any(marker in lowered for marker in AUTH_MARKERS):
                    detail = (
                        "GitHub authentication failed. Run `git push` once in a terminal "
                        f"to unlock the credential helper, or check the remote. ({detail})"
                    )
                return {"ok": False, "status": "error", "detail": detail[:900] or "git push failed"}
        branch_label = "GitHub" if status.get("github") else "origin"
        detail = f"pushed the current branch to {branch_label.lower()}"
        self.store.add_event("repo", "success", f"pushed {identity['rel']} to {branch_label}")
        self.refresh()
        if post_action:
            self._after_action(repo_id, connectivity=True)
        return {"ok": True, "status": "pushed", "detail": detail}

    def sync_gitea(
        self,
        repo_id: str,
        dry_run: bool = False,
        *,
        review_hash: str = "",
        post_action: bool = True,
    ) -> dict[str, Any]:
        path, identity = self.resolve(repo_id)
        try:
            cfg = gitea_config(self.settings)
        except RepoError as exc:
            return {"ok": False, "status": "unconfigured", "detail": str(exc)}
        token, _ = resolve_gitea_token(self.settings)
        with self._lock_for(repo_id):
            result = sync_repo(path, cfg, token, dry_run=dry_run, review_hash=review_hash)
        if not dry_run:
            self._set_gitea(repo_id, result)
            if result.get("ok"):
                self.store.add_event(
                    "repo", "success", f"Gitea {result.get('status')} for {identity['rel']}: {result.get('detail')}"
                )
            elif result.get("status") not in ("skip",):
                self.store.add_event(
                    "repo", "warning", f"Gitea sync failed for {identity['rel']}: {result.get('detail')}"
                )
            self.refresh()
            if post_action:
                self.check_connectivity_async()
        return result

    # ---------------------------------------------------------------- queue
    def default_queue_ids(self) -> list[str]:
        ids = []
        for row in self._rows():
            gitea = row.get("gitea") or {}
            needs_push = bool(row.get("needs", {}).get("push"))
            gitea_behind = bool(gitea.get("target") and gitea.get("status") in ("behind", "missing"))
            if needs_push or gitea_behind:
                ids.append(row["id"])
        return ids

    def queue_push_both(self, repo_ids: list[str] | None = None) -> dict[str, Any]:
        if self.tasks is None:
            raise RepoError("queue runner is not available")
        with self._lock:
            if self._queue and self._queue.status in ("queued", "running"):
                raise RepoError("a repo push queue is already running")
        rows = self._rows()
        if not rows:
            raise RepoError("no repositories discovered")
        wanted = [str(item) for item in (repo_ids or []) if str(item)]
        if not wanted:
            wanted = self.default_queue_ids()
        by_id = {row["id"]: row for row in rows}
        unknown = [item for item in wanted if item not in by_id]
        if unknown:
            raise RepoError(f"unknown repository: {unknown[0]}")
        items: list[QueueItem] = []
        for repo_id in dict.fromkeys(wanted):
            row = by_id[repo_id]
            gitea = row.get("gitea") or {}
            github_state = "pending" if row.get("needs", {}).get("push") else "skipped"
            github_detail = "" if github_state == "pending" else "up to date"
            if not row.get("origin"):
                github_state = "skipped"
                github_detail = "no origin remote"
            gitea_state = "pending"
            gitea_detail = ""
            if not gitea.get("target"):
                gitea_state = "skipped"
                gitea_detail = gitea.get("detail") or "not eligible for Gitea"
            elif gitea.get("status") == "skip":
                gitea_state = "skipped"
                gitea_detail = gitea.get("detail") or "skipped"
            elif gitea.get("status") == "synced" and not row.get("needs", {}).get("push"):
                gitea_state = "skipped"
                gitea_detail = "already in sync"
            items.append(
                QueueItem(
                    repo_id=repo_id,
                    name=row.get("rel") or row.get("name") or repo_id,
                    github=github_state,
                    gitea=gitea_state,
                    github_detail=github_detail,
                    gitea_detail=gitea_detail,
                )
            )
        if not any(item.github == "pending" or item.gitea == "pending" for item in items):
            raise RepoError("nothing to queue for the selected repositories")
        queue_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{os.urandom(3).hex()}"
        queue = RepoQueue(id=queue_id, items=items)
        with self._lock:
            self._queue = queue

        def work(run: Any) -> dict[str, Any]:
            queue.started = util.now_ts()
            queue.status = "running"
            queue.run_id = run.id
            failures = 0
            run.emit(f">> repo fleet push queue · {len(items)} repo(s)")
            for item in items:
                if self._stop.is_set():
                    item.status = "skipped"
                    item.error = "console stopping"
                    continue
                item.status = "running"
                row = self._row(item.repo_id) or {}
                run.emit(f">> {item.name}")
                if item.github == "pending":
                    item.github = "running"
                    run.emit("   github: pushing current branch")
                    pushed = self.push_github(item.repo_id, post_action=False)
                    item.github = "ok" if pushed.get("ok") else "failed"
                    item.github_detail = str(pushed.get("detail") or "")
                    run.emit(("   ✓ " if pushed.get("ok") else "   ✗ ") + f"github: {item.github_detail}")
                else:
                    run.emit(f"   github: skipped ({item.github_detail})")
                if item.gitea == "pending":
                    item.gitea = "running"
                    run.emit("   gitea: mirroring heads and tags")
                    synced = self.sync_gitea(item.repo_id, dry_run=False, post_action=False)
                    item.gitea = "ok" if synced.get("ok") else "failed"
                    item.gitea_detail = str(synced.get("detail") or "")
                    run.emit(("   ✓ " if synced.get("ok") else "   ✗ ") + f"gitea: {item.gitea_detail}")
                else:
                    run.emit(f"   gitea: skipped ({item.gitea_detail})")
                if item.github == "failed" or item.gitea == "failed":
                    failures += 1
                    item.status = "failed" if item.github == "failed" and item.gitea == "failed" else "partial"
                    item.error = "; ".join(
                        value for value in (item.github_detail if item.github == "failed" else "", item.gitea_detail if item.gitea == "failed" else "") if value
                    )
                elif item.github == "skipped" and item.gitea == "skipped":
                    item.status = "skipped"
                else:
                    item.status = "ok"
                run.emit(f"   → {item.status}")
            queue.finished = util.now_ts()
            queue.status = "done" if failures == 0 else "partial"
            queue.error = "" if failures == 0 else f"{failures} repo operation(s) failed"
            run.emit(f"✓ queue complete · {len(items) - failures}/{len(items)} repo(s) clean")
            if failures:
                raise RepoQueueError(queue.error, run_result={"queue": queue.public()})
            return {"queue": queue.public()}

        run = self.tasks.start("repo-push", work, title="repo fleet: GitHub + Gitea")
        queue.run_id = run.id
        return {"ok": True, "run_id": run.id, "queue": queue.public()}


# ------------------------------------------------------------- helpers ----


def _summarise(rows: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(rows)
    dirty_repos = sum(1 for row in rows if row.get("needs", {}).get("commit"))
    dirty_files = sum(int(row.get("dirty_count") or 0) for row in rows)
    ignored_files = sum(int(row.get("ignored_count") or 0) for row in rows)
    ahead_repos = sum(1 for row in rows if row.get("needs", {}).get("push"))
    ahead_commits = sum(int(row.get("ahead") or 0) for row in rows if row.get("needs", {}).get("push"))
    behind_repos = sum(1 for row in rows if int(row.get("behind") or 0) > 0)
    behind_commits = sum(int(row.get("behind") or 0) for row in rows)
    gitea_checked = sum(
        1 for row in rows
        if (row.get("gitea") or {}).get("status") in ("synced", "behind", "missing", "auth", "unreachable", "error")
    )
    gitea_behind = sum(1 for row in rows if (row.get("gitea") or {}).get("status") in ("behind", "missing"))
    gitea_errors = sum(1 for row in rows if (row.get("gitea") or {}).get("status") in ("auth", "unreachable", "error"))
    unknown = sum(1 for row in rows if (row.get("gitea") or {}).get("status") == "unknown")
    attention = 0
    clean = 0
    for row in rows:
        gitea_status = (row.get("gitea") or {}).get("status")
        needs = row.get("needs") or {}
        bad = bool(
            needs.get("commit")
            or needs.get("push")
            or int(row.get("behind") or 0) > 0
            or gitea_status in ("behind", "missing", "auth", "unreachable", "error")
        )
        if bad:
            attention += 1
        if (
            not needs.get("commit")
            and not needs.get("push")
            and int(row.get("behind") or 0) == 0
            and gitea_status in ("synced", "skip")
        ):
            clean += 1
    return {
        "total": total,
        "github": sum(1 for row in rows if row.get("github")),
        "dirty_repos": dirty_repos,
        "dirty_files": dirty_files,
        "ignored_files": ignored_files,
        "ahead_repos": ahead_repos,
        "ahead_commits": ahead_commits,
        "behind_repos": behind_repos,
        "behind_commits": behind_commits,
        "gitea_checked": gitea_checked,
        "gitea_behind": gitea_behind,
        "gitea_errors": gitea_errors,
        "gitea_unknown": unknown,
        "attention": attention,
        "clean": clean,
    }


def _find_vscode() -> str | None:
    found = shutil.which("code")
    if found:
        return found
    candidates = [
        Path("/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code"),
        Path.home() / "Applications/Visual Studio Code.app/Contents/Resources/app/bin/code",
    ]
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def vscode_available() -> bool:
    return _find_vscode() is not None
