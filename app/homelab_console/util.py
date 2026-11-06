"""Small, dependency-free helpers shared by the console.

Everything here is deliberately boring: this app runs on a Mac against a
git repo full of operational state, so predictability beats cleverness.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Iterable, Sequence

HERE = Path(__file__).resolve().parent
APP_ROOT = HERE.parent
REPO_ROOT = APP_ROOT.parent

# "Homelab Console" is also the .app bundle name; keep the on-disk name stable
# so an unpackaged and a packaged run share one workspace.
APP_DATA_NAME = "Homelab Console"


def is_frozen() -> bool:
    """True inside a PyInstaller bundle."""
    return bool(getattr(sys, "frozen", False))


def bundle_root() -> Path:
    """Directory that holds bundled data (``web/``, templates).

    Under PyInstaller this is the extracted bundle (``sys._MEIPASS``); from a
    checkout it is ``app/``, which already contains ``web/``.
    """
    if is_frozen():
        base = getattr(sys, "_MEIPASS", None)
        if base:
            return Path(base).resolve()
        return Path(sys.executable).resolve().parent
    return APP_ROOT


def user_data_dir() -> Path:
    """Per-user location for a workspace when there is no checkout to use."""
    override = os.environ.get("HLC_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_DATA_NAME
    base = os.environ.get("XDG_DATA_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".local" / "share"
    return root / "homelab-console"


def watch_parent() -> None:
    """Exit if the process that spawned us goes away.

    The macOS app owns this backend. A crash or Force Quit skips
    ``applicationWillTerminate``, which would otherwise leave a server holding
    a random port and a stale fleet snapshot forever. ``getppid`` becomes 1
    (launchd) when the parent dies, so polling it is enough.
    """
    raw = os.environ.get("HLC_PARENT_PID", "").strip()
    if not raw:
        return
    try:
        parent = int(raw)
    except ValueError:
        return

    def loop() -> None:
        while True:
            if os.getppid() != parent:
                os._exit(0)
            time.sleep(1.0)

    threading.Thread(target=loop, name="hlc-parent-watch", daemon=True).start()


# ---------------------------------------------------------------- time ----


def now_ts() -> int:
    return int(time.time())


def iso(ts: float | int | None = None) -> str:
    """Local ISO-8601 timestamp with seconds resolution."""
    moment = _dt.datetime.fromtimestamp(ts if ts is not None else time.time())
    return moment.astimezone().isoformat(timespec="seconds")


def human_duration(seconds: float | int | None) -> str:
    if seconds is None:
        return "unknown"
    seconds = max(0, int(seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m"
    return f"{seconds}s"


def parse_docker_time(value: str | None) -> float | None:
    """Docker inspect timestamps ("0001-01-01T00:00:00Z" means never)."""
    if not value or value.startswith("0001-"):
        return None
    text = value.strip().replace("Z", "+00:00")
    # Docker emits up to nanosecond precision; Python accepts microseconds.
    match = re.match(
        r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?([+-]\d{2}:\d{2})$",
        text,
    )
    if match:
        base, frac, offset = match.groups()
        text = f"{base}.{frac[:6].ljust(6, '0')}{offset}" if frac else f"{base}{offset}"
    try:
        return _dt.datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


# ----------------------------------------------------------- processes ----

# Environment variables that must never be inherited by a child process.  The
# macOS app passes the Keychain Gitea token as HLC_GITEA_TOKEN; a Git hook, a
# filter, an editor or a VS Code extension that we spawn must not receive it.
# Callers that genuinely need an HLC_* value (the SSH askpass helper, tests)
# pass it explicitly through ``env=``.
SENSITIVE_ENV_KEYS = {
    "HLC_TOKEN",
    "HLC_GITEA_TOKEN",
    "HLC_GITHUB_TOKEN",
    "GITEA_TOKEN",
    "GITHUB_TOKEN",
    "GH_TOKEN",
}


def child_env() -> dict[str, str]:
    """A copy of the environment with app secrets removed.

    PATH, HOME, SSH_AUTH_SOCK and the platform's normal variables survive.
    Any HLC_* value is dropped unless the caller re-adds it explicitly; that
    includes the parent-pid watch and app-mode markers, which children do not
    need.
    """
    return {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("HLC_") and key not in SENSITIVE_ENV_KEYS
    }


def run(
    cmd: Sequence[str],
    *,
    input_text: str | None = None,
    timeout: float = 30,
    env: dict[str, str] | None = None,
    cwd: str | Path | None = None,
) -> subprocess.CompletedProcess:
    """Run a command with text pipes, never a TTY, never a shell.

    Children receive ``child_env()`` plus only the explicit overrides.  This is
    deliberately narrower than the server's own environment so a Git hook or
    editor cannot read the Gitea token for free.
    """
    merged_env = {**child_env(), **(env or {})}
    try:
        return subprocess.run(
            list(cmd),
            input=input_text,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            env=merged_env,
            cwd=str(cwd) if cwd else None,
            stdin=subprocess.DEVNULL if input_text is None else None,
        )
    except FileNotFoundError as exc:
        return subprocess.CompletedProcess(cmd, 127, "", str(exc))
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode(errors="replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode(errors="replace")
        return subprocess.CompletedProcess(cmd, 124, stdout, (stderr + f"\ntimed out after {timeout}s").strip())


def which(binary: str) -> str | None:
    return shutil.which(binary)


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follow redirects without leaking credentials to another origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is None:
            return None
        old = urllib.parse.urlparse(req.full_url)
        new_parsed = urllib.parse.urlparse(newurl)
        if old.scheme == "https" and new_parsed.scheme != "https":
            raise urllib.error.HTTPError(
                newurl, 403, "refusing an HTTPS-to-HTTP redirect", headers, fp
            )
        if (old.hostname, old.port) != (new_parsed.hostname, new_parsed.port):
            for name in ("Authorization", "Proxy-Authorization", "Cookie"):
                new.remove_header(name)
        return new


_SAFE_OPENER = urllib.request.build_opener(_SafeRedirectHandler())


def urlopen(request: urllib.request.Request, timeout: float = 20):
    """Open an HTTP request with credential-safe redirect handling."""
    return _SAFE_OPENER.open(request, timeout=timeout)


def shell_join(cmd: Sequence[str]) -> str:
    return " ".join(shlex.quote(part) for part in cmd)


# --------------------------------------------------------------- files ----


def ssh_home() -> Path:
    """~/.ssh, overridable with HLC_SSH_DIR for tests and managed setups."""
    override = os.environ.get("HLC_SSH_DIR")
    path = Path(override).expanduser() if override else Path.home() / ".ssh"
    ensure_dir(path, 0o700)
    return path


def ensure_dir(path: Path, mode: int = 0o700) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(mode)
    except OSError:
        pass
    return path


def atomic_write_text(path: Path, text: str, mode: int | None = None) -> None:
    ensure_dir(path.parent)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        if mode is not None:
            tmp.chmod(mode)
        elif path.exists():
            tmp.chmod(path.stat().st_mode & 0o777)
        else:
            tmp.chmod(0o644)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def atomic_write_bytes(path: Path, data: bytes, mode: int | None = None) -> None:
    ensure_dir(path.parent)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        if mode is not None:
            tmp.chmod(mode)
        elif path.exists():
            tmp.chmod(path.stat().st_mode & 0o777)
        else:
            tmp.chmod(0o644)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def read_text(path: Path, default: str = "") -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return default


def is_probably_text(data: bytes) -> bool:
    if b"\x00" in data[:4096]:
        return False
    try:
        data[:4096].decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("_") or "unnamed"


def is_secret_file(name: str) -> bool:
    """True for the workspace's .env-family secret names."""
    base = os.path.basename(str(name or ""))
    return base == ".env" or base.startswith(".env.") or base.endswith(".env")


# ---------------------------------------------------------------- json ----


def json_load(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def json_dump(path: Path, payload: Any, *, mode: int | None = None) -> None:
    atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=False) + "\n", mode=mode)


def stable_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


# --------------------------------------------------------------- misc -----


class _Missing:
    pass


MISSING = _Missing()


def dig(payload: Any, *keys: str, default: Any = None) -> Any:
    current = payload
    for key in keys:
        if not isinstance(current, dict) or key not in current:
            return default
        current = current[key]
    return current


def tail_lines(text: str, count: int) -> list[str]:
    lines = text.splitlines()
    return lines[-count:] if count > 0 else lines


def clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def chunks(items: Sequence[Any], size: int) -> Iterable[Sequence[Any]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]
