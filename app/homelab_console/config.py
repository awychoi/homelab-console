"""Process-wide configuration for the Homelab Console.

The app is repo-centric on purpose: the git checkout under ``stacks/`` stays
the single source of truth.  Only operational bookkeeping (history, run logs,
caches, snapshots) lives in the gitignored state dir.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import util

VERSION = "0.1.0"
APP_NAME = "Homelab Console"

WORKSPACE_FILE = "workspace.txt"

# Written into a workspace the app creates, so a later `git init` cannot commit
# local state (which can hold tokens) or per-stack .env secrets. The console
# treats `.env`, `.env.*` and `*.env` as secret files, so all three patterns
# must stay in sync here, in the repo root and in examples/workspace.
WORKSPACE_GITIGNORE = """# local console state and backups
.hlc/
backups/

# per-stack secrets — never commit (.env, .env.local, prod.env, ...)
**/.env
**/.env.*
**/*.env

# tooling noise
__pycache__/
*.pyc
.DS_Store
"""

MAX_REPO_EXCLUSIONS = 500


def normalize_repo_exclusions(raw: Any) -> dict[str, list[str]]:
    """Validate the per-root list of projects hidden from Repos."""
    if not isinstance(raw, dict):
        return {}
    cleaned: dict[str, list[str]] = {}
    for raw_root, raw_ids in raw.items():
        root_text = str(raw_root or "").strip()
        if not root_text:
            continue
        root = Path(root_text).expanduser()
        if not root.is_absolute() or not isinstance(raw_ids, (list, tuple)):
            continue
        ids: list[str] = []
        for item in raw_ids:
            text = str(item or "").strip()
            if not text or text.startswith("/") or "\x00" in text:
                continue
            parts = Path(text).parts
            if any(part in ("", ".", "..") for part in parts):
                continue
            if text not in ids:
                ids.append(text)
            if len(ids) >= MAX_REPO_EXCLUSIONS:
                break
        if ids:
            try:
                cleaned[str(root.resolve())] = ids
            except OSError:
                continue
    return cleaned


def _looks_like_repo(path: Path) -> bool:
    return (path / "stacks").is_dir() and (path / "scripts").is_dir()


def default_workspace() -> Path:
    """Where a packaged app keeps stacks, hosts.json and .hlc."""
    return util.user_data_dir() / "workspace"


def _workspace_override() -> Path | None:
    """An explicit workspace, from the env or the packaged app's pointer file."""
    env = os.environ.get("HLC_WORKSPACE")
    if env:
        return Path(env).expanduser()
    pointer = util.user_data_dir() / WORKSPACE_FILE
    try:
        text = pointer.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return Path(text).expanduser() if text else None


def ensure_workspace(path: Path) -> Path:
    """Create the minimum a console needs to start where there is no checkout."""
    path = path.expanduser().resolve()
    util.ensure_dir(path / "stacks")
    util.ensure_dir(path / "scripts")
    hosts_file = path / "scripts" / "hosts.json"
    if not hosts_file.exists():
        util.json_dump(hosts_file, {"location_": "scripts/hosts.json"})
    gitignore = path / ".gitignore"
    if not gitignore.exists():
        util.atomic_write_text(gitignore, WORKSPACE_GITIGNORE)
    return path


def _find_repo() -> Path:
    override = os.environ.get("HLC_REPO")
    if override:
        return Path(override).expanduser().resolve()
    # A packaged app has no checkout next to it, so an explicit workspace or
    # the pointer file is the only way to reach an existing fleet repo.
    explicit = _workspace_override()
    if explicit:
        explicit = explicit.expanduser().resolve()
        # An older pointer names the app checkout root while the fleet lives in
        # its workspace/ folder. Resolve the nested workspace from any pointer,
        # not just one that equals the running checkout (a packaged app has a
        # different REPO_ROOT, which used to send this case to the empty
        # default workspace).
        nested = explicit / "workspace"
        if _looks_like_repo(nested):
            return nested
        # Migration: an older pointer may name the app checkout itself. After
        # the app/workspace split the fleet lives in workspace/, so never seed
        # stacks/ and scripts/ into the checkout just because it was recorded.
        if explicit == util.REPO_ROOT:
            workspace = util.REPO_ROOT / "workspace"
            if _looks_like_repo(workspace):
                return workspace
            if _looks_like_repo(util.REPO_ROOT):
                return util.REPO_ROOT
            return ensure_workspace(default_workspace())
        # A stale pointer/env at a non-empty folder that is not a workspace
        # must not be seeded with stacks/ and scripts/ behind the user's back.
        partial = (explicit / "stacks").is_dir() or (explicit / "scripts").is_dir()
        if explicit.exists() and not partial:
            try:
                if any(explicit.iterdir()):
                    return ensure_workspace(default_workspace())
            except OSError:
                pass
        return ensure_workspace(explicit)
    candidate = util.REPO_ROOT
    # A source checkout keeps the private workspace at workspace/ (a separate,
    # gitignored checkout); prefer it over a stale layout with stacks/ at the root.
    for probe in (candidate, candidate / "workspace"):
        if _looks_like_repo(probe):
            return probe
    # Fall back to cwd when the package is vendored elsewhere.
    current = Path.cwd().resolve()
    for parent in (current, *current.parents):
        if _looks_like_repo(parent):
            return parent
    # Last resort: start empty instead of failing. The UI can add hosts and
    # stacks from here; nothing else in the console requires a git checkout.
    return ensure_workspace(default_workspace())


DEFAULTS: dict[str, Any] = {
    "poll_interval_s": 30,
    "release_ttl_s": 6 * 3600,
    "release_sources": {},
    "stack_profiles": {},
    "history_days": 30,
    "remote_root": "/opt/stacks",
    "github_token": "",
    "gitea_token": "",
    "repo_root": "",
    "repo_roots": [],
    "repo_deep_roots": [],
    "repo_exclusions": {},
    "repo_nested_selected": {},
    "gitea_url": "",
    "gitea_owner": "",
    "gitea_timeout_s": 120,
    "gitea_prune": True,
    "gitea_force": True,
    "gitea_check_on_start": True,
    "repo_poll_interval_s": 10,
    "auto_update_enabled": False,
    "health_timeout_s": 90,
    "theme": "system",
    "check_releases_on_start": True,
}


@dataclass
class Settings:
    repo: Path
    state_dir: Path
    values: dict[str, Any] = field(default_factory=dict)

    # ---- paths ---------------------------------------------------------
    @property
    def hosts_file(self) -> Path:
        return self.repo / "scripts" / "hosts.json"

    @property
    def stacks_dir(self) -> Path:
        return self.repo / "stacks"

    @property
    def config_file(self) -> Path:
        return self.state_dir / "config.json"

    @property
    def db_file(self) -> Path:
        return self.state_dir / "console.db"

    @property
    def runs_dir(self) -> Path:
        return self.state_dir / "runs"

    @property
    def cache_dir(self) -> Path:
        return self.state_dir / "cache"

    @property
    def backups_dir(self) -> Path:
        return self.state_dir / "snapshots"

    @property
    def remote_root(self) -> str:
        return str(self.values.get("remote_root") or "/opt/stacks").rstrip("/") or "/opt/stacks"

    # ---- accessors -----------------------------------------------------
    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, DEFAULTS.get(key, default))

    def set(self, key: str, value: Any) -> None:
        if key not in DEFAULTS:
            raise KeyError(f"unknown setting: {key}")
        self.values[key] = value
        self.save()

    def update(self, values: dict[str, Any]) -> None:
        for key, value in values.items():
            if key not in DEFAULTS:
                raise KeyError(f"unknown setting: {key}")
            self.values[key] = value
        self.save()

    @property
    def github_token(self) -> str:
        return (
            os.environ.get("HLC_GITHUB_TOKEN")
            or os.environ.get("GITHUB_TOKEN")
            or os.environ.get("GH_TOKEN")
            or str(self.values.get("github_token") or "")
        )

    @property
    def gitea_token(self) -> str:
        return (
            os.environ.get("HLC_GITEA_TOKEN")
            or os.environ.get("GITEA_TOKEN")
            or str(self.values.get("gitea_token") or "")
        )

    def gitea_token_source(self) -> str:
        """Where the effective token came from, for the settings UI.

        The macOS shell reads its Keychain item and passes it as
        ``HLC_GITEA_TOKEN`` alongside ``HLC_APP=1``, so "environment" there
        means the Keychain, not a shell export.
        """
        if os.environ.get("HLC_GITEA_TOKEN", "").strip():
            return "macos-app" if os.environ.get("HLC_APP") else "environment"
        if os.environ.get("GITEA_TOKEN", "").strip():
            return "environment"
        if str(self.values.get("gitea_token") or "").strip():
            return "config"
        return ""

    # ---- persistence ---------------------------------------------------
    @classmethod
    def load(cls, repo: Path | None = None) -> "Settings":
        repo = (repo or _find_repo()).resolve()
        state = Path(os.environ.get("HLC_STATE_DIR") or (repo / ".hlc")).expanduser().resolve()
        util.ensure_dir(state, 0o700)
        settings = cls(repo=repo, state_dir=state)
        raw = util.json_load(state / "config.json", {}) or {}
        merged = {**DEFAULTS}
        if isinstance(raw, dict):
            merged.update({k: v for k, v in raw.items() if k in DEFAULTS})
        if merged.get("theme") not in ("dark", "light", "system"):
            merged["theme"] = "dark"  # migrate the old "carbon" theme
        # One legacy root is still accepted; the UI and discovery use the list.
        raw_roots = merged.get("repo_roots")
        if isinstance(raw_roots, str):
            raw_roots = [raw_roots]
        roots = [str(item).strip() for item in raw_roots if str(item).strip()] if isinstance(raw_roots, list) else []
        legacy = str(merged.get("repo_root") or "").strip()
        if not roots and legacy:
            roots = [legacy]
        merged["repo_roots"] = roots
        merged["repo_root"] = roots[0] if roots else ""
        raw_deep = merged.get("repo_deep_roots")
        merged["repo_deep_roots"] = (
            [str(item).strip() for item in raw_deep if str(item).strip()]
            if isinstance(raw_deep, list)
            else []
        )
        merged["repo_exclusions"] = normalize_repo_exclusions(merged.get("repo_exclusions"))
        merged["repo_nested_selected"] = normalize_repo_exclusions(
            merged.get("repo_nested_selected")
        )
        # Workspace-owned service metadata is normalized on read so a hand
        # edit cannot inject an invalid source or profile into the app.
        try:
            from . import profiles

            merged["stack_profiles"] = profiles.normalize_stack_profiles(merged.get("stack_profiles"))
        except (ImportError, ValueError):
            merged["stack_profiles"] = {}
        try:
            from . import releases

            merged["release_sources"] = releases.normalize_source_overrides(merged.get("release_sources"))
        except (ImportError, ValueError):
            merged["release_sources"] = {}
        settings.values = merged
        for path in (settings.runs_dir, settings.cache_dir, settings.backups_dir):
            util.ensure_dir(path, 0o700)
        return settings

    def save(self) -> None:
        util.json_dump(self.config_file, self.values, mode=0o600)

    def public(self) -> dict[str, Any]:
        """Settings safe to hand to the browser (no token, masked secrets)."""
        payload = {
            k: v for k, v in self.values.items()
            if k not in ("github_token", "gitea_token")
        }
        payload["github_token_set"] = bool(self.github_token)
        payload["gitea_token_set"] = bool(self.gitea_token)
        payload["gitea_token_source"] = self.gitea_token_source()
        payload["app_managed"] = bool(os.environ.get("HLC_APP"))
        return payload


class TokenRegistry:
    """Per-process bearer token injected into the SPA.

    The server binds loopback, but a random token still blocks the classic
    "malicious website POSTs to localhost" attack against a tool that can
    deploy containers.  ``HLC_TOKEN`` makes headless verification easy.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._token = os.environ.get("HLC_TOKEN") or os.urandom(24).hex()

    @property
    def token(self) -> str:
        return self._token

    def valid(self, candidate: str | None) -> bool:
        if not candidate:
            return False
        import hmac

        return hmac.compare_digest(candidate, self._token)


_settings: Settings | None = None
_settings_lock = threading.Lock()


def get_settings() -> Settings:
    global _settings
    with _settings_lock:
        if _settings is None:
            _settings = Settings.load()
        return _settings


def reset_settings(settings: Settings) -> None:
    """Test hook / CLI override."""
    global _settings
    with _settings_lock:
        _settings = settings
