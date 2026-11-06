"""Workspace backup and restore.

The archive is intentionally small and explicit:

    manifest.json       app version, workspace, repo roots, counts
    hosts.json          fleet inventory
    settings.json       non-secret settings (tokens are never included)
    stacks/...          compose files, configs and .env secrets

Local state (SQLite, run logs, caches) is not included. Restoring first writes
a pre-restore backup into ``.hlc/backups/`` and only then replaces hosts,
stacks and settings, so a bad archive cannot leave the workspace half-written.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import urllib.parse
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from . import profiles, releases, util
from .config import VERSION, normalize_repo_exclusions

MAX_ARCHIVE_BYTES = 2 * 1024 * 1024 * 1024
MAX_EXTRACT_BYTES = 4 * 1024 * 1024 * 1024
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MANIFEST = "manifest.json"
HOSTS = "hosts.json"
SETTINGS = "settings.json"
STACKS = "stacks"

# Never write these into (or restore from) a backup.
SECRET_KEYS = {"github_token", "gitea_token"}
SETTINGS_KEYS = (
    "poll_interval_s",
    "release_ttl_s",
    "release_sources",
    "stack_profiles",
    "history_days",
    "remote_root",
    "repo_root",
    "repo_roots",
    "repo_exclusions",
    "gitea_url",
    "gitea_owner",
    "gitea_timeout_s",
    "gitea_prune",
    "gitea_force",
    "gitea_check_on_start",
    "repo_poll_interval_s",
    "auto_update_enabled",
    "health_timeout_s",
    "theme",
    "check_releases_on_start",
)
SKIP_NAMES = {".DS_Store", "__pycache__"}
SKIP_SUFFIXES = {".pyc", ".pyo"}


class BackupError(RuntimeError):
    pass


def _default_name() -> str:
    return f"hlc-backup-{time.strftime('%Y%m%d-%H%M%S')}.zip"


def _destination_path(settings, destination: str | Path | None) -> Path:
    if destination:
        path = Path(str(destination)).expanduser()
        if path.is_dir():
            path = path / _default_name()
    else:
        path = settings.state_dir / "backups" / _default_name()
    if path.suffix.lower() != ".zip":
        raise BackupError("backup destination must end in .zip")
    if len(str(path)) > 500:
        raise BackupError("backup path is too long")
    if not path.is_absolute():
        raise BackupError("backup destination must be an absolute path")
    return path


def _settings_payload(settings) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key in SETTINGS_KEYS:
        value = settings.get(key)
        if key in SECRET_KEYS:
            continue
        if key == "repo_roots":
            value = [str(item) for item in (value or [])]
        payload[key] = value
    return payload


def _stack_files(settings) -> list[Path]:
    root = settings.stacks_dir
    if not root.is_dir():
        return []
    files: list[Path] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        if any(part in SKIP_NAMES for part in path.parts):
            continue
        if path.suffix in SKIP_SUFFIXES:
            continue
        files.append(path)
    return files


def create_backup(settings, destination: str | Path | None = None) -> dict[str, Any]:
    path = _destination_path(settings, destination)
    files = _stack_files(settings)
    hosts = settings.hosts_file if settings.hosts_file.is_file() else None
    util.ensure_dir(path.parent, 0o700)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    os.close(fd)
    tmp_path = Path(temporary)
    created = util.iso()
    try:
        with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            manifest = {
                "app": "Homelab Console",
                "version": VERSION,
                "created_at": created,
                "workspace": str(settings.repo),
                "repo_roots": [str(item) for item in (settings.get("repo_roots") or [])],
                "counts": {"stacks": len({p.parent for p in files}), "files": len(files)},
            }
            archive.writestr(MANIFEST, json.dumps(manifest, indent=2) + "\n")
            archive.writestr(SETTINGS, json.dumps(_settings_payload(settings), indent=2) + "\n")
            if hosts is not None:
                archive.write(hosts, HOSTS)
            for source in files:
                archive.write(source, f"{STACKS}/{source.relative_to(settings.stacks_dir).as_posix()}")
        tmp_path.chmod(0o600)
        os.replace(tmp_path, path)
    finally:
        tmp_path.unlink(missing_ok=True)
    stat = path.stat()
    return {
        "ok": True,
        "path": str(path),
        "name": path.name,
        "bytes": stat.st_size,
        "created_at": created,
        "stacks": len({p.parent for p in files}),
        "files": len(files),
        "includes_secrets": any(util.is_secret_file(p.name) for p in files),
    }


def _safe_extract(archive: zipfile.ZipFile, target: Path) -> None:
    total = 0
    for info in archive.infolist():
        name = info.filename
        if not name or name.endswith("/"):
            continue
        pure = PurePosixPath(name)
        if pure.is_absolute() or ".." in pure.parts or name.startswith("."):
            raise BackupError(f"archive contains an unsafe path: {name}")
        if info.file_size > MAX_EXTRACT_BYTES:
            raise BackupError("archive member is too large")
        destination = target / pure
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Do not trust the central-directory size: count the bytes that are
        # actually decompressed, so a forged header cannot expand past the cap.
        written = 0
        with archive.open(info) as source, destination.open("wb") as sink:
            while True:
                chunk = source.read(64 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                total += len(chunk)
                if written > MAX_EXTRACT_BYTES or total > MAX_EXTRACT_BYTES:
                    raise BackupError("archive expands beyond the size limit")
                sink.write(chunk)


def _read_member(archive: zipfile.ZipFile, name: str, limit: int = MAX_MANIFEST_BYTES) -> bytes:
    with archive.open(name) as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise BackupError(f"archive member is too large: {name}")
    return data


def _validated_settings(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise BackupError("archive settings.json is not an object")
    values: dict[str, Any] = {}
    for key in SETTINGS_KEYS:
        if key not in payload or key in SECRET_KEYS:
            continue
        value = payload[key]
        if key == "repo_roots":
            if not isinstance(value, list) or len(value) > 20:
                raise BackupError("archive has an invalid repo_roots list")
            roots: list[str] = []
            for item in value:
                raw = str(item or "").strip()
                if not raw:
                    continue
                path = Path(raw).expanduser()
                if not path.is_absolute():
                    raise BackupError("archive repo roots must be absolute")
                roots.append(str(path.resolve()))
            values[key] = roots
        elif key == "repo_exclusions":
            try:
                values[key] = normalize_repo_exclusions(value)
            except (TypeError, ValueError) as exc:
                raise BackupError("archive has invalid repo_exclusions") from exc
        elif key == "release_sources":
            try:
                values[key] = releases.normalize_source_overrides(value, strict=True)
            except ValueError as exc:
                raise BackupError(f"archive has invalid release sources: {exc}") from exc
        elif key == "stack_profiles":
            try:
                values[key] = profiles.normalize_stack_profiles(value, strict=True)
            except ValueError as exc:
                raise BackupError(f"archive has invalid stack profiles: {exc}") from exc
        elif key == "theme":
            if value not in ("dark", "light", "system"):
                raise BackupError("archive has an invalid theme")
            values[key] = value
        elif key == "repo_root":
            raw = str(value or "").strip()
            if raw:
                path = Path(raw).expanduser()
                if not path.is_absolute():
                    raise BackupError("archive repo_root must be absolute")
                values[key] = str(path.resolve())
            else:
                values[key] = ""
        elif key in ("remote_root",):
            text = str(value or "").strip()
            if not text.startswith("/") or " " in text or ".." in text:
                raise BackupError("archive has an invalid remote_root")
            values[key] = text
        elif key in ("gitea_url",):
            text = str(value or "").strip()
            if text:
                parsed = urllib.parse.urlparse(text)
                if (
                    parsed.scheme not in ("http", "https")
                    or not parsed.hostname
                    or parsed.username
                    or parsed.password
                ):
                    raise BackupError("archive has an invalid gitea_url")
                if (
                    parsed.scheme == "http"
                    and parsed.hostname not in ("127.0.0.1", "localhost", "::1")
                    and os.environ.get("HLC_GITEA_ALLOW_HTTP") != "1"
                ):
                    raise BackupError("archive gitea_url must use https off loopback")
            values[key] = text
        elif isinstance(value, (int, float, bool, str)):
            values[key] = value
    if not values.get("repo_roots") and values.get("repo_root"):
        values["repo_roots"] = [values["repo_root"]]
    if values.get("repo_roots"):
        values["repo_root"] = values["repo_roots"][0]
    if values.get("repo_exclusions"):
        allowed_roots = set(values.get("repo_roots") or [])
        values["repo_exclusions"] = {
            root: ids for root, ids in values["repo_exclusions"].items() if root in allowed_roots
        }
    return values


def restore_backup(settings, archive_path: str | Path) -> dict[str, Any]:
    source = Path(str(archive_path)).expanduser()
    if not source.is_absolute() or not source.is_file():
        raise BackupError("archive must be an existing absolute file")
    if source.stat().st_size > MAX_ARCHIVE_BYTES:
        raise BackupError("archive is too large")
    with tempfile.TemporaryDirectory(prefix="hlc-restore-", dir=str(settings.state_dir)) as temporary:
        staging = Path(temporary)
        try:
            with zipfile.ZipFile(source) as archive:
                names = set(archive.namelist())
                if MANIFEST not in names or HOSTS not in names:
                    raise BackupError("not a Homelab Console backup (missing manifest or hosts.json)")
                manifest = json.loads(_read_member(archive, MANIFEST))
                if not isinstance(manifest, dict) or manifest.get("app") != "Homelab Console":
                    raise BackupError("not a Homelab Console backup")
                _safe_extract(archive, staging)
        except zipfile.BadZipFile as exc:
            raise BackupError("archive is not a valid zip file") from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BackupError("archive manifest is invalid") from exc

        hosts_path = staging / HOSTS
        try:
            hosts_text = hosts_path.read_text(encoding="utf-8")
            if len(hosts_text) > MAX_MANIFEST_BYTES:
                raise ValueError("hosts.json is too large")
            hosts = json.loads(hosts_text)
        except (OSError, ValueError) as exc:
            raise BackupError("archive hosts.json is invalid") from exc
        if not isinstance(hosts, dict):
            raise BackupError("archive hosts.json must be an object")
        extracted_stacks = staging / STACKS
        if not extracted_stacks.is_dir():
            raise BackupError("archive does not contain a stacks/ directory")
        try:
            settings_text = (staging / SETTINGS).read_text(encoding="utf-8")
            if len(settings_text) > MAX_MANIFEST_BYTES:
                raise ValueError("settings.json is too large")
            settings_payload = json.loads(settings_text)
        except (OSError, ValueError):
            settings_payload = {}
        restored_settings = _validated_settings(settings_payload)

        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup_dir = util.ensure_dir(settings.state_dir / "backups", 0o700)
        pre_restore = backup_dir / f"pre-restore-{stamp}.zip"
        create_backup(settings, pre_restore)

        old_stacks = backup_dir / f"stacks-pre-restore-{stamp}"
        moved = False
        try:
            if settings.stacks_dir.exists():
                shutil.move(str(settings.stacks_dir), str(old_stacks))
                moved = True
            shutil.copytree(extracted_stacks, settings.stacks_dir)
            util.atomic_write_text(
                settings.hosts_file,
                json.dumps(hosts, indent=4, sort_keys=False) + "\n",
            )
            if restored_settings:
                settings.update(restored_settings)
            for path in settings.stacks_dir.rglob("*"):
                if not path.is_file() or path.is_symlink() or not util.is_secret_file(path.name):
                    continue
                try:
                    path.chmod(0o600)
                except OSError:
                    pass
        except Exception:
            # Put the previous stacks back if anything fails mid-restore.
            if moved and old_stacks.exists():
                shutil.rmtree(settings.stacks_dir, ignore_errors=True)
                shutil.move(str(old_stacks), str(settings.stacks_dir))
            raise

    return {
        "ok": True,
        "restored_at": util.iso(),
        "source": str(source),
        "pre_restore_backup": str(pre_restore),
        "hosts": len(hosts),
        "stacks": sum(
            1
            for host in settings.stacks_dir.iterdir()
            if host.is_dir()
            for stack in host.iterdir()
            if stack.is_dir()
        )
        if settings.stacks_dir.exists()
        else 0,
        "settings_restored": sorted(restored_settings.keys()),
    }
