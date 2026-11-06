"""Stack model: the repo side of the Mac -> Pi deploy contract.

This mirrors ``scripts/deploy.py`` semantics deliberately:

* repo layout  ``stacks/<host>/<service>/``
* remote layout ``/opt/stacks/<service>/``
* a push is a tar stream, never an in-place edit on the Pi
* overwriting a remote file is "destructive" and needs an explicit gate
* added-only files may flow without a prompt (the UI still shows them)

On top of that it adds what the console needs: a line-based Compose parser
(no PyYAML dependency, comments preserved when pins are bumped), sha256
comparison against the host for drift, and a staging validation that runs
``docker compose config`` in a temp dir *before* touching the live stack.
"""

from __future__ import annotations

import base64
import io
import os
import re
import shlex
import stat
import tarfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from . import remote, util
from .config import Settings
from .hosts import Host

COMPOSE_NAMES = ("docker-compose.yml", "docker-compose.yaml", "compose.yaml", "compose.yml")
STACK_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
SERVICE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_FILES = 400
JUNK = (".DS_Store", ".gitkeep")
IMAGE_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class StackError(ValueError):
    pass


def validate_rel_path(rel: str) -> PurePosixPath:
    """Return a safe stack-relative path or raise.

    Compose files and the API both feed path strings into file operations.  A
    relative path with a dot segment, an absolute path, a NUL/newline or a
    missing basename is never valid inside a stack.
    """
    text = str(rel or "")
    if "\x00" in text or "\n" in text or "\r" in text:
        raise StackError("invalid file path")
    path = PurePosixPath(text)
    if path.is_absolute() or not path.name or any(part in ("", ".", "..") for part in path.parts):
        raise StackError("invalid file path")
    if any(part == ".git" or part.startswith(".git") for part in path.parts):
        raise StackError("invalid file path")
    return path


def safe_stack_path(stack_path: Path, rel: str) -> Path:
    """Resolve *rel* under *stack_path*, refusing symlink/dot-dot escapes."""
    rel_path = validate_rel_path(rel)
    root = stack_path.resolve()
    target = (stack_path / rel_path.as_posix()).resolve()
    if not str(target).startswith(str(root) + os.sep):
        raise StackError("file path escapes the stack directory")
    return target


def is_secret_path(rel: str) -> bool:
    return util.is_secret_file(rel)


# --------------------------------------------------------------- images ----


@dataclass(frozen=True)
class ImageRef:
    raw: str
    repo: str
    tag: str
    digest: str
    registry: str
    variable: str | None = None
    default_tag: str | None = None
    explicit_tag: bool = True

    @property
    def is_digest_pinned(self) -> bool:
        return bool(self.digest)

    @property
    def is_unpinned(self) -> bool:
        return (not self.digest and self.tag == "latest") or bool(self.variable)

    def display(self) -> str:
        return self.raw


def normalize_repo(repo: str) -> str:
    for prefix in ("docker.io/", "index.docker.io/", "registry-1.docker.io/"):
        if repo.startswith(prefix):
            return repo[len(prefix) :]
    if repo.startswith("docker.n8n.io/"):
        return repo[len("docker.n8n.io/") :]
    return repo


def parse_image(raw: str) -> ImageRef:
    value = raw.strip().strip("'\"")
    digest = ""
    base = value
    if "@" in value:
        base, digest = value.split("@", 1)
        digest = f"sha256:{digest.split(':', 1)[1]}" if digest.startswith("sha256:") else digest
    variable = None
    default_tag = None
    match = IMAGE_VAR_RE.search(base)
    if match:
        variable = match.group(1)
        default_tag = match.group(2)
        base = IMAGE_VAR_RE.sub("", base)
        base = base.rstrip(":")
    last_slash = base.rfind("/")
    colon = base.rfind(":")
    explicit_tag = colon > last_slash
    if explicit_tag:
        repo, tag = base[:colon], base[colon + 1 :]
    else:
        repo, tag = base, "latest"
    first = repo.split("/", 1)[0]
    registry = first if ("." in first or ":" in first or first == "localhost") else "docker.io"
    if variable and not default_tag:
        tag = ""
    return ImageRef(
        raw=value,
        repo=repo,
        tag=tag,
        digest=digest,
        registry=registry,
        variable=variable,
        default_tag=default_tag,
        explicit_tag=explicit_tag and not variable,
    )


# ----------------------------------------------------------- compose model


@dataclass
class ServiceSpec:
    name: str
    image_raw: str = ""
    image: ImageRef | None = None
    image_line: int | None = None
    container_name: str = ""
    ports: list[dict[str, str]] = field(default_factory=list)
    env_files: list[str] = field(default_factory=list)
    has_build: bool = False

    def public(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "image": self.image_raw,
            "repo": self.image.repo if self.image else "",
            "tag": self.image.tag if self.image else "",
            "digest": self.image.digest if self.image else "",
            "variable": self.image.variable if self.image else None,
            "explicit_tag": self.image.explicit_tag if self.image else False,
            "container_name": self.container_name,
            "ports": self.ports,
            "has_build": self.has_build,
        }


@dataclass
class Stack:
    host: str
    name: str
    path: Path
    compose_file: str
    text: str
    services: list[ServiceSpec] = field(default_factory=list)
    project: str = ""
    parse_error: str = ""
    files: dict[str, str] = field(default_factory=dict)
    hashes: dict[str, str] = field(default_factory=dict)
    env: dict[str, str] = field(default_factory=dict)

    @property
    def service_names(self) -> list[str]:
        return [service.name for service in self.services]

    @property
    def images(self) -> list[ServiceSpec]:
        return [service for service in self.services if service.image is not None]

    def rel(self) -> str:
        return f"{self.host}/{self.name}"

    def public(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "name": self.name,
            "project": self.project,
            "compose_file": self.compose_file,
            "services": [service.public() for service in self.services],
            "parse_error": self.parse_error,
            "files": sorted(self.files),
            "hashes": self.hashes,
            "dir": str(self.path),
        }


def _strip_inline_comment(value: str) -> tuple[str, str]:
    """Split a YAML scalar from its trailing comment, respecting quotes."""
    quote = ""
    for index, char in enumerate(value):
        if char in "'\"":
            if not quote:
                quote = char
            elif quote == char:
                quote = ""
        elif char == "#" and not quote and (index == 0 or value[index - 1].isspace()):
            return value[:index].rstrip(), value[index:]
    return value.rstrip(), ""


def _scalar(value: str) -> str:
    value, _ = _strip_inline_comment(value.strip())
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        value = value[1:-1]
    return value.replace("''", "'").replace('\\"', '"')


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def parse_compose(text: str) -> tuple[list[ServiceSpec], str, str]:
    """Return (services, project_name, parse_error). Comments are ignored."""
    lines = text.splitlines()
    services: list[ServiceSpec] = []
    project = ""
    start = None
    for index, line in enumerate(lines):
        if re.match(r"^services:\s*(?:#.*)?$", line) and start is None:
            start = index
        if re.match(r"^name:\s*\S+", line):
            project = _scalar(line.split(":", 1)[1])
    if start is None:
        return [], project, "no top-level 'services:' block found"

    service_indent = None
    current: ServiceSpec | None = None
    current_key = ""
    for index in range(start + 1, len(lines)):
        line = lines[index]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = _indent(line)
        if indent == 0:
            break  # another top-level key ends the services block
        if service_indent is None:
            service_indent = indent
        if indent == service_indent:
            if not stripped.endswith(":"):
                continue
            name = stripped[:-1].strip().strip("'\"")
            if not name:
                continue
            current = ServiceSpec(name=name)
            services.append(current)
            current_key = ""
            continue
        if current is None or indent <= service_indent:
            continue
        # inside a service block
        if ":" in stripped and not stripped.startswith("-"):
            key, _, rest = stripped.partition(":")
            current_key = key.strip()
            value = rest.strip()
            if current_key == "image" and current.image_raw == "":
                current.image_raw = _scalar(value)
                current.image_line = index
            elif current_key == "container_name" and not current.container_name:
                current.container_name = _scalar(value)
            elif current_key == "build":
                current.has_build = True
            elif current_key == "env_file":
                if value and not value.startswith("["):
                    current.env_files.append(_scalar(value))
                elif value.startswith("["):
                    for part in value.strip("[]").split(","):
                        if part.strip():
                            current.env_files.append(_scalar(part.strip()))
            elif current_key == "image":
                pass
        elif current_key == "ports" and stripped.startswith("-"):
            raw = _scalar(stripped[1:].strip())
            current.ports.append(_parse_port(raw))
        elif current_key == "env_file" and stripped.startswith("-"):
            current.env_files.append(_scalar(stripped[1:].strip()))

    for service in services:
        if service.image_raw:
            try:
                service.image = parse_image(service.image_raw)
            except Exception:
                service.image = None
        if not service.image_raw and not service.has_build:
            service.image_raw = ""
    if not services:
        return [], project, "services block contains no services"
    return services, project, ""


def _parse_port(raw: str) -> dict[str, str]:
    value = raw.strip()
    protocol = "tcp"
    if "/" in value:
        value, protocol = value.rsplit("/", 1)
    host_ip = ""
    host_port = ""
    container_port = ""
    if value.startswith("["):  # [::1]:8080:80
        end = value.find("]")
        if end != -1 and value.find(":") > end:
            host_ip = value[1:end]
            rest = value[end + 2 :]
            parts = rest.split(":")
            if len(parts) == 2:
                host_port, container_port = parts
    else:
        parts = value.split(":")
        if len(parts) == 3:
            host_ip, host_port, container_port = parts
        elif len(parts) == 2:
            host_port, container_port = parts
        else:
            container_port = parts[0]
    return {"raw": raw, "host_ip": host_ip, "host_port": host_port, "container_port": container_port, "protocol": protocol}


def parse_env(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        else:
            value, _ = _strip_inline_comment(value)
        values[key] = value
    return values


def _junk(name: str) -> bool:
    return name.startswith("._") or name in JUNK


def find_compose_file(path: Path) -> str | None:
    for candidate in COMPOSE_NAMES:
        target = path / candidate
        if target.is_file() and not target.is_symlink():
            return candidate
    return None


def read_files(path: Path) -> dict[str, str]:
    files: dict[str, str] = {}
    if path.is_symlink() or not path.is_dir():
        return files
    root = path.resolve()
    for item in sorted(path.rglob("*")):
        if len(files) >= MAX_FILES:
            break
        # Reads are confined to the stack.  A symlink here could point at
        # ~/.ssh or another stack; following it would leak the target into the
        # editor, snapshots and the tar pushed to the host.
        if item.is_symlink() or not item.is_file() or _junk(item.name):
            continue
        try:
            if not str(item.resolve()).startswith(str(root) + os.sep):
                continue
        except OSError:
            continue
        relative = item.relative_to(path)
        if any(part == ".git" or part.startswith(".git") for part in relative.parts):
            continue
        try:
            if item.stat().st_size > MAX_FILE_BYTES:
                continue
            data = item.read_bytes()
        except OSError:
            continue
        if not util.is_probably_text(data):
            continue
        files[relative.as_posix()] = data.decode("utf-8")
    return files


def hashes_for(files: dict[str, str]) -> dict[str, str]:
    return {rel: util.sha256_text(text) for rel, text in files.items()}


def _tighten_env_permissions(directory: Path) -> None:
    """Keep existing .env files at 0600, including nested service config dirs."""
    candidates = set(directory.rglob(".env"))
    candidates.update(directory.rglob("*.env"))
    candidates.update(directory.rglob(".env.*"))
    root = directory.resolve()
    for candidate in candidates:
        try:
            if candidate.is_symlink() or not candidate.is_file():
                continue
            if not str(candidate.resolve()).startswith(str(root) + os.sep):
                continue
            if candidate.stat().st_mode & 0o077:
                candidate.chmod(0o600)
        except OSError:
            continue


def load_stack(settings: Settings, host_alias: str, path: Path) -> Stack | None:
    if path.is_symlink():
        return None
    _tighten_env_permissions(path)
    compose = find_compose_file(path)
    if not compose:
        return None
    text = util.read_text(path / compose)
    services, project, error = parse_compose(text)
    files = read_files(path)
    env_text = files.get(".env", "")
    merged_env = parse_env(env_text)
    for service in services:
        for env_file in service.env_files:
            rel = env_file.lstrip("./")
            if rel in files:
                merged_env.update(parse_env(files[rel]))
    stack = Stack(
        host=host_alias,
        name=path.name,
        path=path,
        compose_file=compose,
        text=text,
        services=services,
        project=project or path.name,
        parse_error=error,
        files=files,
        hashes=hashes_for(files),
        env=merged_env,
    )
    return stack


def discover(settings: Settings, hosts: Iterable[str] | None = None) -> list[Stack]:
    roots = settings.stacks_dir
    allowed = set(hosts) if hosts else None
    stacks: list[Stack] = []
    if not roots.is_dir():
        return stacks
    for host_dir in sorted(roots.iterdir()):
        if host_dir.is_symlink() or not host_dir.is_dir() or host_dir.name.startswith("."):
            continue
        if allowed is not None and host_dir.name not in allowed:
            continue
        for stack_dir in sorted(host_dir.iterdir()):
            if stack_dir.is_symlink() or not stack_dir.is_dir() or stack_dir.name.startswith("."):
                continue
            stack = load_stack(settings, host_dir.name, stack_dir)
            if stack:
                stacks.append(stack)
    return stacks


LAYOUT_MAX_WARNINGS = 20
LAYOUT_MAX_DEPTH = 3
LAYOUT_SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    "build",
    "dist",
    "target",
}


def layout_warnings(settings: Settings) -> list[dict[str, str]]:
    """Report compose files that sit outside the stacks/<host>/<stack>/ layout.

    A stack is valid only when the compose file is a direct child of the stack
    directory. Files nested inside a valid stack are assets and are never
    reported. The scan is deliberately bounded and never adopts a nested stack.
    """
    root = settings.stacks_dir
    warnings: list[dict[str, str]] = []
    if not root.is_dir():
        return warnings

    def add(path: Path, kind: str) -> None:
        try:
            rel = path.relative_to(settings.repo).as_posix()
        except ValueError:
            rel = str(path)
        warnings.append(
            {
                "path": rel,
                "kind": kind,
                "expected": "stacks/<host>/<stack>/<compose-file>",
            }
        )

    for host_dir in sorted(root.iterdir()):
        if host_dir.is_symlink() or not host_dir.is_dir() or host_dir.name.startswith("."):
            continue
        host_compose = find_compose_file(host_dir)
        if host_compose:
            add(host_dir / host_compose, "compose-in-host")
        for stack_dir in sorted(host_dir.iterdir()):
            if stack_dir.is_symlink() or not stack_dir.is_dir() or stack_dir.name.startswith("."):
                continue
            if find_compose_file(stack_dir):
                # Any compose nested below a valid stack is an asset, not a
                # second stack. Do not guess and do not warn.
                continue
            base_depth = len(stack_dir.parts)
            for current, dirs, files in os.walk(stack_dir, topdown=True):
                current_path = Path(current)
                depth = len(current_path.parts) - base_depth
                dirs[:] = sorted(
                    name
                    for name in dirs
                    if not name.startswith(".") and name not in LAYOUT_SKIP_DIRS
                )
                if depth >= LAYOUT_MAX_DEPTH:
                    dirs[:] = []
                found = next((name for name in COMPOSE_NAMES if name in files), None)
                if found and depth > 0:
                    add(current_path / found, "nested-stack")
                    dirs[:] = []
                    if len(warnings) >= LAYOUT_MAX_WARNINGS:
                        return warnings
    root_compose = find_compose_file(root)
    if root_compose:
        add(root / root_compose, "compose-in-stacks-root")
    return warnings[:LAYOUT_MAX_WARNINGS]



def get_stack(settings: Settings, host: str, name: str) -> Stack:
    _validate_host_dir(host)
    if not STACK_NAME_RE.match(name or ""):
        raise StackError("invalid stack name")
    path = settings.stacks_dir / host / name
    if path.is_symlink() or path.parent.name != host or not path.is_dir():
        raise StackError(f"stack not found: {host}/{name}")
    root = settings.stacks_dir.resolve()
    resolved = path.resolve()
    if not str(resolved).startswith(str(root) + os.sep):
        raise StackError(f"stack not found: {host}/{name}")
    stack = load_stack(settings, host, path)
    if stack is None:
        raise StackError(f"stack has no compose file: {host}/{name}")
    return stack


# ------------------------------------------------------------ diff / plan


@dataclass
class Change:
    path: str
    kind: str  # added | changed | remote-only
    local: str | None
    remote_text: str | None
    diff: list[str]
    destructive: bool

    def public(self, diff_limit: int = 160) -> dict[str, Any]:
        return {
            "path": self.path,
            "kind": self.kind,
            "destructive": self.destructive,
            "diff": self.diff[:diff_limit],
            "diff_truncated": len(self.diff) > diff_limit,
            "local_bytes": len(self.local.encode()) if self.local is not None else None,
            "remote_bytes": len(self.remote_text.encode()) if self.remote_text is not None else None,
        }


def _unified(remote_text: str | None, local_text: str | None) -> list[str]:
    import difflib

    if local_text is None:
        return [f"remote-only file: {len(remote_text or '')} bytes"]
    if remote_text is None:
        return [line for line in local_text.splitlines()] if len(local_text.splitlines()) <= 200 else local_text.splitlines()[:200]
    return list(
        difflib.unified_diff(
            remote_text.splitlines(),
            local_text.splitlines(),
            fromfile="target",
            tofile="repo",
            lineterm="",
            n=3,
        )
    )


def plan_changes(
    local_files: dict[str, str],
    remote_files: dict[str, str],
    *,
    remote_complete: bool = True,
) -> list[Change]:
    changes: list[Change] = []
    for rel in sorted(local_files):
        if _junk(Path(rel).name):
            continue
        local = local_files[rel]
        if rel not in remote_files:
            if remote_complete:
                changes.append(Change(rel, "added", local, None, _unified(None, local), False))
            else:
                # The host listing was incomplete (a large, binary or
                # unreadable file was skipped).  We cannot prove the path is
                # absent, so treat the write as destructive rather than
                # silently overwriting a file that was never reviewed.
                changes.append(
                    Change(
                        rel,
                        "changed",
                        local,
                        None,
                        ["host listing incomplete; the remote file could not be read for comparison"],
                        True,
                    )
                )
        elif remote_files[rel] != local:
            changes.append(Change(rel, "changed", local, remote_files[rel], _unified(remote_files[rel], local), True))
    for rel in sorted(set(remote_files) - set(local_files)):
        if _junk(Path(rel).name):
            continue
        # Extra files on the host (runtime state, old configs) are not lost by
        # a push. deploy.py gates on them; the console reports them as
        # informational "extra" files so harmless runtime state is not treated
        # as a destructive change.
        changes.append(Change(rel, "remote-only", None, remote_files[rel], _unified(remote_files[rel], None), False))
    return changes


def clean_remote_delete_paths(paths: Any, compose_file: str) -> list[str]:
    """Validate paths for removal from a host stack; never absolute or escape."""
    if not isinstance(paths, list) or not paths or len(paths) > 200:
        raise StackError("paths must be a non-empty list of at most 200 files")
    cleaned: list[str] = []
    for raw in paths:
        rel = PurePosixPath(str(raw or ""))
        if rel.is_absolute() or not rel.parts or ".." in rel.parts or "\x00" in str(rel) or "\n" in str(rel):
            raise StackError(f"invalid path: {raw!r}")
        value = str(rel)
        if value == compose_file:
            raise StackError("the compose file cannot be deleted from the host")
        if value not in cleaned:
            cleaned.append(value)
    return cleaned


def plan_hash(changes: list[Change]) -> str:
    payload = [
        {
            "path": change.path,
            "kind": change.kind,
            "local": util.sha256_text(change.local) if change.local is not None else "",
            "remote": util.sha256_text(change.remote_text) if change.remote_text is not None else "",
        }
        for change in changes
    ]
    return util.sha256_text(util.stable_json(payload))


def compute_plan(host: Host, stack: Stack, remote_root: str) -> dict[str, Any]:
    listing = remote.read_remote_listing(host, stack.name, remote_root)
    changes = plan_changes(stack.files, listing.files, remote_complete=listing.complete)
    return {
        "host": stack.host,
        "stack": stack.name,
        "changes": changes,
        "hash": plan_hash(changes),
        "destructive": any(change.destructive for change in changes),
        "remote_only": sum(1 for change in changes if change.kind == "remote-only"),
        "added": sum(1 for change in changes if change.kind == "added"),
        "remote_listing_complete": listing.complete,
        "remote_skipped": list(listing.skipped),
    }


# ------------------------------------------------------------------- tar ----


def build_tar(files: dict[str, str], base: Path | None = None) -> bytes:
    buffer = io.BytesIO()
    stamp = int(util.now_ts())
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for rel, text in sorted(files.items()):
            rel_posix = PurePosixPath(rel)
            if rel_posix.is_absolute() or ".." in rel_posix.parts:
                raise StackError(f"unsafe path in stack files: {rel}")
            data = text.encode("utf-8")
            info = tarfile.TarInfo(name=rel_posix.as_posix())
            info.size = len(data)
            info.mtime = stamp
            info.mode = 0o644
            if base is not None:
                disk = base / rel
                try:
                    info.mode = stat.S_IMODE(disk.stat().st_mode)
                except OSError:
                    pass
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _compose_command(stack_name: str, action: str, remote_root: str, extra: str = "") -> str:
    directory = f"{remote_root.rstrip('/')}/{stack_name}"
    base = f"cd {shlex.quote(directory)} && docker compose"
    if action == "up":
        return f"{base} up -d --remove-orphans"
    if action == "pull":
        return f"{base} pull && {base} up -d --remove-orphans"
    if action == "down":
        return f"{base} down"
    if action == "restart":
        return f"{base} restart"
    if action == "ps":
        return f"{base} ps --format 'table {{{{.Name}}}}\\t{{{{.Status}}}}\\t{{{{.Image}}}}'"
    if action == "logs":
        return f"{base} logs --no-color --tail {shlex.quote(extra or '200')}"
    if action == "config":
        return f"{base} config --quiet"
    raise StackError(f"unknown compose action: {action}")


def compose(host: Host, stack_name: str, action: str, remote_root: str, *, extra: str = "", timeout: float = 120):
    if host.local:
        raise StackError("local compose actions are not supported (no Docker on this Mac)")
    return remote.remote_shell(host, _compose_command(stack_name, action, remote_root, extra), timeout=timeout)


def validate_remote(host: Host, files: dict[str, str], remote_root: str, *, timeout: float = 60) -> dict[str, Any]:
    """Validate compose in a temp dir on the host without touching /opt/stacks."""
    if host.local:
        return {"ok": False, "error": "local stack validation is not supported", "services": []}
    payload = base64.b64encode(build_tar(files)).decode("ascii")
    command = (
        "set -u; d=$(mktemp -d) || exit 1; "
        "trap 'rm -rf \"$d\"' EXIT; "
        "base64 -d > \"$d/.payload.tar\" || exit 1; "
        "tar -C \"$d\" -xf \"$d/.payload.tar\" || exit 1; "
        "rm -f \"$d/.payload.tar\"; "
        "cd \"$d\" || exit 1; "
        "if ! docker compose config --quiet; then exit 3; fi; "
        "echo __HLC_OK__; docker compose config --services"
    )
    argv = remote.ssh_argv(host) + [command]
    result = util.run(argv, input_text=payload, timeout=timeout)
    if result.returncode == 0 and "__HLC_OK__" in result.stdout:
        services = [line.strip() for line in result.stdout.splitlines() if line.strip() and line.strip() != "__HLC_OK__"]
        return {"ok": True, "services": services, "output": result.stderr.strip()}
    return {
        "ok": False,
        "error": (result.stderr or result.stdout or "docker compose validation failed").strip()[-2000:],
        "services": [],
    }


def push_files(host: Host, stack_name: str, files: dict[str, str], remote_root: str, *, base: Path | None = None, timeout: float = 120) -> dict[str, Any]:
    """Stream a tar of the stack directory to /opt/stacks/<stack>.

    The payload is unpacked into a staging directory first and every live
    destination is probed for replaceability *before* the stack is touched, so
    a permission failure cannot leave a half-written stack behind.  Some
    images (SearXNG) chown their bind mounts on startup; when that leaves
    files the SSH user cannot replace, passwordless sudo hands the stack
    directory back, mirroring ``scripts/deploy.py``.

    ``written`` reports whether the live directory may have been modified;
    callers use it to decide whether a rollback needs to re-push.
    """
    if host.local:
        raise StackError("local pushes are not supported")
    root = remote_root.rstrip("/")
    dest = root + "/" + stack_name
    payload = base64.b64encode(build_tar(files, base=base)).decode("ascii")
    rels = base64.b64encode(("\n".join(sorted(files)) + "\n").encode("utf-8")).decode("ascii")
    command = (
        "set -u; "
        f"root={shlex.quote(root)}; dest={shlex.quote(dest)}; "
        "user=$(id -un) || exit 11; group=$(id -gn) || exit 11; "
        'mkdir -p "$root" || exit 12; '
        'mkdir -p "$dest" 2>/dev/null || true; '
        'tmp=$(mktemp) || exit 13; '
        'stage=$(mktemp -d) || { rm -f "$tmp"; exit 13; }; '
        'list=$(mktemp) || { rm -f "$tmp"; rm -rf "$stage"; exit 13; }; '
        'badlist=$(mktemp) || { rm -f "$tmp" "$list"; rm -rf "$stage"; exit 13; }; '
        'trap \'rm -f "$tmp" "$list" "$badlist"; rm -rf "$stage"\' EXIT; '
        'base64 -d > "$tmp" || exit 14; '
        'tar -C "$stage" -xf "$tmp" || exit 15; '
        f"printf %s {shlex.quote(rels)} | base64 -d > \"$list\" || exit 14; "
        'bad=0; '
        'while IFS= read -r rel; do '
        '  [ -n "$rel" ] || continue; '
        '  target="$dest/$rel"; '
        '  probe=$(dirname "$target"); '
        '  while [ ! -d "$probe" ] && [ "$probe" != "/" ]; do probe=$(dirname "$probe"); done; '
        '  if [ -e "$target" ] || [ -L "$target" ]; then '
        '    [ -w "$target" ] || { [ -w "$probe" ] && [ -x "$probe" ]; } || { bad=1; printf \'%s\\n\' "$rel" >> "$badlist"; }; '
        '  else '
        '    { [ -w "$probe" ] && [ -x "$probe" ]; } || { bad=1; printf \'%s\\n\' "$rel" >> "$badlist"; }; '
        '  fi; '
        'done < "$list"; '
        'if [ "$bad" = 1 ]; then '
        '  if ! sudo -n true 2>/dev/null; then '
        '    printf \'__HLC_UNWRITABLE__\\n\'; cat "$badlist"; exit 16; '
        '  fi; '
        '  sudo -n mkdir -p "$dest" && sudo -n chown -R "$user:$group" "$dest" || exit 17; '
        '  printf \'__HLC_CHOWNED__\'; '
        'fi; '
        'tar -C "$dest" -xf "$tmp" || { printf \'__HLC_WRITTEN__\'; exit 18; }; '
        'printf \'__HLC_PUSH_OK__\''
    )
    argv = remote.ssh_argv(host) + [command]
    result = util.run(argv, input_text=payload, timeout=timeout)
    out = result.stdout or ""
    ok = result.returncode == 0 and "__HLC_PUSH_OK__" in out
    repaired = "__HLC_CHOWNED__" in out
    written = ok or "__HLC_WRITTEN__" in out
    if ok:
        return {"ok": True, "files": len(files), "written": True, "repaired": repaired}
    if "__HLC_UNWRITABLE__" in out:
        blocked = [
            line.strip()
            for line in out.split("__HLC_UNWRITABLE__", 1)[1].splitlines()
            if line.strip() and not line.strip().startswith("__HLC_")
        ]
        shown = ", ".join(blocked[:6]) + (" …" if len(blocked) > 6 else "")
        error = (
            f"cannot replace {shown or 'stack files'} on {host.alias}: "
            f"{host.user} cannot write them and passwordless sudo is unavailable. "
            "A container may have chowned its bind mount; repair with: "
            f"ssh {host.target} 'sudo chown -R {host.user}:{host.user} {dest}'"
        )
        return {"ok": False, "error": error, "written": False, "repaired": repaired, "unwritable": blocked}
    cleaned = (
        out.replace("__HLC_PUSH_OK__", "")
        .replace("__HLC_WRITTEN__", "")
        .replace("__HLC_CHOWNED__", "")
        .strip()
    )
    error = (result.stderr or cleaned or "push failed").strip()[-2000:]
    hint = ""
    if "permission denied" in error.lower() or "read-only file system" in error.lower():
        hint = (
            f" — {host.user} needs write access to {dest} "
            "(or passwordless sudo so the console can repair ownership)"
        )
    return {"ok": False, "error": error + hint, "written": written, "repaired": repaired}


# ------------------------------------------------------------- local edit


def _validate_host_dir(host: str) -> str:
    if not re.match(r"^[a-z0-9][a-z0-9-]{0,31}$", host or ""):
        raise StackError("invalid host alias")
    return host


def write_file(
    settings: Settings,
    host: str,
    stack: str,
    rel: str,
    content: str,
    *,
    create_only: bool = False,
) -> dict[str, Any]:
    _validate_host_dir(host)
    if not STACK_NAME_RE.match(stack):
        raise StackError("invalid stack name")
    rel_path = validate_rel_path(rel)
    if len(content.encode()) > MAX_FILE_BYTES:
        raise StackError("file is too large (2 MB limit)")
    if _junk(rel_path.name):
        raise StackError("junk files are not editable")
    host_dir = settings.stacks_dir / host
    directory = host_dir / stack
    root = settings.stacks_dir.resolve()
    if host_dir.is_symlink() or directory.is_symlink() or not directory.is_dir():
        raise StackError(f"stack not found: {host}/{stack}")
    if not str(directory.resolve()).startswith(str(root) + os.sep):
        raise StackError(f"stack not found: {host}/{stack}")
    target = safe_stack_path(directory, rel_path.as_posix())
    if create_only and target.exists():
        raise StackError(f"file already exists: {rel_path.as_posix()}")
    if rel_path.as_posix() in COMPOSE_NAMES:
        _, _, error = parse_compose(content)
        if error:
            raise StackError(f"compose file is not valid: {error}")
    mode = 0o600 if util.is_secret_file(rel_path.name) else None
    util.atomic_write_text(target, content, mode=mode)
    return {"ok": True, "path": target.relative_to(settings.repo).as_posix()}


def create_stack(
    settings: Settings,
    host: str,
    name: str,
    compose_text: str,
    env_text: str = "",
    *,
    overwrite: bool = False,
) -> Stack:
    if not STACK_NAME_RE.match(name):
        raise StackError("stack name must be lowercase letters/numbers/dots/dashes (max 64)")
    _validate_host_dir(host)
    services, _, error = parse_compose(compose_text)
    if error:
        raise StackError(error)
    for service in services:
        if not service.image_raw and not service.has_build:
            raise StackError(f"service '{service.name}' has neither image: nor build:")
    host_dir = settings.stacks_dir / host
    directory = host_dir / name
    if host_dir.is_symlink() or directory.is_symlink():
        raise StackError(f"{host}/{name} is a symlink; refusing to write through it")
    if directory.exists() and not overwrite:
        raise StackError(f"{host}/{name} already exists")
    directory.mkdir(parents=True, exist_ok=True)
    root = settings.stacks_dir.resolve()
    if not str(directory.resolve()).startswith(str(root) + os.sep):
        raise StackError("stack path escapes the stacks directory")
    util.atomic_write_text(directory / "docker-compose.yml", compose_text.rstrip() + "\n")
    if env_text.strip():
        util.atomic_write_text(directory / ".env", env_text.rstrip() + "\n", mode=0o600)
    stack = load_stack(settings, host, directory)
    if stack is None:
        raise StackError("stack could not be reloaded")
    return stack


def delete_stack_files(settings: Settings, host: str, name: str) -> None:
    if not STACK_NAME_RE.match(name):
        raise StackError("invalid stack name")
    _validate_host_dir(host)
    directory = settings.stacks_dir / host / name
    if not directory.is_dir() or directory.is_symlink():
        raise StackError("stack not found")
    resolved = directory.resolve()
    if not str(resolved).startswith(str(settings.stacks_dir.resolve()) + "/"):
        raise StackError("path escapes the stacks directory")
    import shutil

    shutil.rmtree(directory)


# --------------------------------------------------------- state helpers ----


def match_containers(stack: Stack, containers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    matches = []
    for container in containers:
        project = str(container.get("project") or "")
        working_dir = str(container.get("working_dir") or "").rstrip("/")
        config_files = str(container.get("config_files") or "")
        if (
            project == stack.project
            or project == stack.name
            or working_dir.rsplit("/", 1)[-1] == stack.name
            or f"/stacks/{stack.name}/" in f"{config_files}/"
        ):
            matches.append(container)
    return matches


def sync_state(stack: Stack, remote_hashes: dict[str, str] | None) -> dict[str, Any]:
    if remote_hashes is None:
        return {"state": "unknown", "drift": [], "missing": [], "extra": []}
    if not remote_hashes:
        return {"state": "not-deployed", "drift": [], "missing": sorted(stack.hashes), "extra": []}
    drift = sorted(rel for rel, digest in stack.hashes.items() if rel in remote_hashes and remote_hashes[rel] != digest)
    missing = sorted(rel for rel in stack.hashes if rel not in remote_hashes)
    extra = sorted(rel for rel in remote_hashes if rel not in stack.hashes)
    if drift or missing:
        state = "drift"
    elif extra:
        state = "extra"
    else:
        state = "synced"
    return {"state": state, "drift": drift, "missing": missing, "extra": extra}


def stack_runtime(stack: Stack, containers: list[dict[str, Any]], remote_hashes: dict[str, str] | None) -> dict[str, Any]:
    matched = match_containers(stack, containers)
    by_service: dict[str, dict[str, Any]] = {}
    for container in matched:
        service = str(container.get("service") or container.get("name") or "")
        by_service[service] = container
    expected = stack.service_names
    running = [service for service in expected if service in by_service and by_service[service].get("state") == "running"]
    exceptions = [
        service
        for service in expected
        if service in by_service and by_service[service].get("exception")
    ]
    if not matched:
        status = "not-deployed"
    elif len(running) == len(expected) and not exceptions:
        status = "up"
    elif running:
        status = "degraded"
    else:
        status = "down"
    if exceptions and status == "up":
        status = "degraded"
    return {
        "status": status,
        "containers": matched,
        "running": len(running),
        "expected": len(expected),
        "exceptions": exceptions,
        "sync": sync_state(stack, remote_hashes),
    }


def _status_entries(settings: Settings, *, all_untracked: bool = False) -> list[dict[str, str]]:
    argv = [
        "git",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "protocol.ext.allow=never",
        "-C",
        str(settings.repo),
        "status",
        "--porcelain",
        "-z",
    ]
    if all_untracked:
        argv.append("--untracked-files=all")
    argv += ["--", "stacks", "scripts/hosts.json"]
    result = util.run(argv, timeout=15)
    entries: list[dict[str, str]] = []
    records = result.stdout.split("\0")
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if not record:
            continue
        code = record[:2].strip()
        path = record[3:].strip()
        if not path:
            continue
        # Porcelain v1 -z puts a rename/copy source in the following record.
        if code[:1] in ("R", "C") and index < len(records):
            index += 1
        if util.is_secret_file(path):
            continue
        entries.append({"code": code, "path": path})
    return entries


def git_status(settings: Settings) -> dict[str, Any]:
    entries = _status_entries(settings, all_untracked=True)
    branch = util.run(["git", "-C", str(settings.repo), "rev-parse", "--abbrev-ref", "HEAD"], timeout=10).stdout.strip()
    last = util.run(
        ["git", "-C", str(settings.repo), "log", "-1", "--format=%h %ad %s", "--date=format:%Y-%m-%d %H:%M"],
        timeout=10,
    ).stdout.strip()
    return {"dirty": bool(entries), "entries": entries, "branch": branch, "last_commit": last}


def workspace_commit_paths(settings: Settings) -> list[str]:
    """Every non-secret path the workspace commit may stage.

    ``git status --porcelain`` reports an untracked directory as one entry;
    staging it with ``git add -- stacks/`` would also stage the ``.env`` files
    inside it.  Expand to individual files and filter the secret family
    server-side, regardless of whether the workspace's .gitignore is complete.
    """
    return [entry["path"] for entry in _status_entries(settings, all_untracked=True)]
