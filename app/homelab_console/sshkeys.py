"""ed25519 key onboarding and SSH probing.

The console never stores a password.  For hosts that do not yet trust the
Mac's key, it offers a one-time password install through an askpass helper
(deleted immediately afterwards) *and* a copy-paste fallback that works on
any Pi where SSH password auth is disabled entirely.
"""

from __future__ import annotations

import hashlib
import os
import re
import shlex
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import util
from .hosts import Host

DEFAULT_COMMENT = "homelab-console"


class KeyError_(ValueError):
    pass


@dataclass
class KeyInfo:
    private: str
    public: str
    key_type: str
    fingerprint: str
    comment: str
    bits: str = ""

    def public_dict(self) -> dict[str, Any]:
        return {
            "private": self.private,
            "public": self.public,
            "type": self.key_type,
            "fingerprint": self.fingerprint,
            "comment": self.comment,
            "bits": self.bits,
        }


def ssh_dir() -> Path:
    return util.ssh_home()


def control_dir() -> Path:
    """A short, per-user directory for SSH multiplexing sockets.

    Unix socket paths are capped at 104 bytes on macOS, and the state dir can
    easily exceed that once it lives under ``Application Support``. Hashing the
    state dir into ``/tmp`` keeps the socket path short while still giving each
    workspace its own namespace.
    """
    override = os.environ.get("HLC_SSH_CONTROL_DIR")
    if override:
        return util.ensure_dir(Path(override).expanduser(), 0o700)
    try:
        from .config import get_settings

        state_dir = str(get_settings().state_dir)
    except Exception:
        state_dir = tempfile.gettempdir()
    digest = hashlib.sha256(state_dir.encode("utf-8")).hexdigest()[:8]
    return util.ensure_dir(Path("/tmp") / f"hlc-{os.getuid()}" / digest, 0o700)


def _fingerprint(pub_path: Path) -> tuple[str, str, str]:
    result = util.run(["ssh-keygen", "-lf", str(pub_path), "-E", "sha256"], timeout=10)
    if result.returncode != 0:
        return "", "", ""
    # "256 SHA256:abc user@host (ED25519)"
    parts = result.stdout.strip().split()
    if len(parts) >= 4:
        bits = parts[0]
        fingerprint = parts[1]
        # comment is everything between fingerprint and type parens
        comment = " ".join(parts[2:-1])
        key_type = parts[-1].strip("()")
        return fingerprint, comment, f"{key_type} {bits}"
    return "", "", ""


def key_info(private_path: Path) -> KeyInfo:
    private_path = private_path.expanduser()
    pub_path = private_path.with_name(private_path.name + ".pub")
    if not private_path.exists():
        raise KeyError_(f"private key not found: {private_path}")
    if not pub_path.exists():
        raise KeyError_(f"public key missing for {private_path}")
    pub = util.read_text(pub_path).strip()
    fingerprint, comment, typed = _fingerprint(pub_path)
    key_type = typed.split()[0] if typed else (pub.split()[0] if pub else "unknown")
    return KeyInfo(
        private=str(private_path),
        public=pub,
        key_type=key_type or "unknown",
        fingerprint=fingerprint,
        comment=comment,
        bits=typed,
    )


def list_keys() -> list[dict[str, Any]]:
    directory = ssh_dir()
    keys: list[dict[str, Any]] = []
    if not directory.exists():
        return keys
    for pub in sorted(directory.glob("*.pub")):
        private = pub.with_name(pub.name[:-4])
        if not private.exists():
            continue
        try:
            info = key_info(private)
        except KeyError_:
            continue
        if not info.key_type.lower().startswith(("ed25519", "rsa", "ecdsa")):
            continue
        keys.append(info.public_dict())
    ed = [key for key in keys if "ed25519" in key["type"].lower()]
    rest = [key for key in keys if "ed25519" not in key["type"].lower()]
    return ed + rest


def default_key() -> dict[str, Any] | None:
    preferred = ssh_dir() / "id_ed25519"
    keys = list_keys()
    for key in keys:
        if key["private"] == str(preferred):
            return key
    for key in keys:
        if "ed25519" in key["type"].lower():
            return key
    return keys[0] if keys else None


def generate_key(path: Path | None = None, comment: str = DEFAULT_COMMENT) -> KeyInfo:
    directory = ssh_dir()
    util.ensure_dir(directory, 0o700)
    target = (path or (directory / "id_ed25519")).expanduser()
    if target.exists():
        return key_info(target)
    result = util.run(
        ["ssh-keygen", "-t", "ed25519", "-a", "100", "-N", "", "-C", comment, "-f", str(target)],
        timeout=20,
    )
    if result.returncode != 0:
        raise KeyError_(result.stderr.strip() or "ssh-keygen failed")
    target.chmod(0o600)
    pub = target.with_name(target.name + ".pub")
    if pub.exists():
        pub.chmod(0o644)
    return key_info(target)


def ensure_key(preferred: str | None = None) -> KeyInfo:
    """Return the requested key, generating an ed25519 identity when needed."""
    if preferred:
        path = Path(preferred).expanduser()
        if path.exists():
            return key_info(path)
        raise KeyError_(f"key not found: {path}")
    existing = default_key()
    if existing:
        return key_info(Path(existing["private"]))
    return generate_key()


def key_for_host(host: Host, override: str | None = None) -> Path | None:
    if override:
        return Path(override).expanduser()
    if host.key:
        return Path(host.key).expanduser()
    return None


# ------------------------------------------------------------- ssh plumbing


def ssh_options(
    *,
    port: int = 22,
    key: Path | None = None,
    batch: bool = True,
    connect_timeout: int = 6,
    control: bool = True,
) -> list[str]:
    opts = [
        "-o", f"ConnectTimeout={connect_timeout}",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "ServerAliveInterval=10",
        "-o", "ServerAliveCountMax=2",
    ]
    if batch:
        opts += ["-o", "BatchMode=yes"]
    if port and port != 22:
        opts += ["-p", str(port)]
    if key is not None:
        opts += ["-i", str(key), "-o", "IdentitiesOnly=yes"]
    if control:
        opts += [
            "-o", "ControlMaster=auto",
            "-o", f"ControlPath={control_dir() / 'hlc-%C'}",
            "-o", "ControlPersist=120",
        ]
    return opts


def ssh_argv(host: Host, *, key: Path | None = None, batch: bool = True, control: bool = True) -> list[str]:
    if host.local:
        raise KeyError_("local host has no ssh transport")
    return [
        "ssh",
        *ssh_options(port=host.port, key=key, batch=batch, control=control),
        f"{host.user}@{host.address}",
    ]


def terminal_command(host: Host, path: str, *, key: Path | None = None) -> str:
    """Build a plain interactive ssh command that lands in *path*."""
    inner = f"cd {shlex.quote(path)} && exec $SHELL -l"
    if host.local:
        return inner
    args = ["ssh", "-t"]
    if host.port and host.port != 22:
        args += ["-p", str(host.port)]
    if key is not None and key.exists():
        args += ["-i", str(key)]
    args += [host.target, inner]
    return shlex.join(args)


def test_connection(host: Host, key: Path | None = None, timeout: float = 15) -> dict[str, Any]:
    """Authenticated, side-effect-free probe."""
    if host.local:
        return {"ok": True, "local": True, "latency_ms": 0, "hostname": "localhost", "error": ""}
    argv = ssh_argv(host, key=key)
    import time

    started = time.monotonic()
    result = util.run(argv + ["printf 'HLC_OK\\n'; hostname; uname -m"], timeout=timeout)
    latency = int((time.monotonic() - started) * 1000)
    if result.returncode == 0 and "HLC_OK" in result.stdout:
        lines = [line for line in result.stdout.splitlines() if line != "HLC_OK"]
        return {
            "ok": True,
            "latency_ms": latency,
            "hostname": lines[0] if lines else host.alias,
            "arch": lines[1] if len(lines) > 1 else "",
            "error": "",
        }
    stderr = (result.stderr or "").strip()
    return {
        "ok": False,
        "latency_ms": latency,
        "error": _friendly_ssh_error(stderr),
        "raw": stderr[-600:],
        "auth_failed": "permission denied" in stderr.lower(),
    }


def _friendly_ssh_error(stderr: str) -> str:
    low = stderr.lower()
    if "permission denied" in low:
        return "SSH authentication failed — check the username (SSH names are case-sensitive) and that the Mac's public key is installed on the host."
    if "no route to host" in low:
        return "Host unreachable (no route). Check power, LAN address or DHCP lease."
    if "connection refused" in low:
        return "SSH refused the connection. Is sshd running on this address/port?"
    if "connection timed out" in low or "timed out" in low:
        return "SSH connection timed out. Check the address and network."
    if "host key verification failed" in low:
        return "Host key verification failed. The address may now belong to a different machine."
    if "could not resolve hostname" in low:
        return "Could not resolve the hostname."
    if "no matching host key type" in low:
        return "The host offers no compatible SSH host key."
    return stderr.strip()[:400] or "SSH probe failed."


# ----------------------------------------------------------- host key trust


def host_key_fingerprints(address: str, port: int = 22) -> list[dict[str, str]]:
    """Fetch (unauthenticated) host key fingerprints for explicit TOFU consent."""
    scan = util.run(
        ["ssh-keyscan", "-T", "6", "-p", str(port), "-t", "ed25519,ecdsa,rsa", address],
        timeout=15,
    )
    if scan.returncode != 0 or not scan.stdout.strip():
        return []
    keys: list[dict[str, str]] = []
    for line in scan.stdout.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        result = util.run(["ssh-keygen", "-lf", "-", "-E", "sha256"], input_text=line + "\n", timeout=10)
        if result.returncode != 0:
            continue
        parts = result.stdout.strip().split()
        if len(parts) >= 4:
            keys.append(
                {
                    "bits": parts[0],
                    "fingerprint": parts[1],
                    "type": parts[-1].strip("()"),
                    "algorithm": parts[-1].strip("()").lower(),
                }
            )
    # De-duplicate by fingerprint, prefer ed25519.
    seen: set[str] = set()
    unique: list[dict[str, str]] = []
    for key in keys:
        if key["fingerprint"] in seen:
            continue
        seen.add(key["fingerprint"])
        unique.append(key)
    unique.sort(key=lambda item: 0 if "ed25519" in item["type"].lower() else 1)
    return unique


def is_known_host(address: str, port: int = 22) -> bool:
    lookup = f"[{address}]:{port}" if port != 22 else address
    result = util.run(["ssh-keygen", "-F", lookup], timeout=10)
    return result.returncode == 0 and bool(result.stdout.strip())


def trust_host_keys(address: str, port: int = 22, *, workdir: Path | None = None) -> dict[str, Any]:
    """Append the host key(s) the user just verified to known_hosts.

    ``ssh-keyscan`` itself is unauthenticated; this only records the keys after
    the onboarding UI has asked the operator to compare the fingerprints.  It
    turns the first connection from silent TOFU into an explicit pin, and keeps
    a backup because ~/.ssh/known_hosts is security-sensitive.
    """
    scan = util.run(
        ["ssh-keyscan", "-T", "6", "-p", str(port), "-t", "ed25519,ecdsa,rsa", address],
        timeout=15,
    )
    lines = [line.strip() for line in scan.stdout.splitlines() if line.strip() and not line.startswith("#")]
    if not lines:
        raise KeyError_("could not fetch a host key; verify the address and SSH port first")
    path = ssh_dir() / "known_hosts"
    existing = util.read_text(path)
    known = set(existing.splitlines())
    added = [line for line in lines if line not in known]
    if added:
        backup = None
        if path.exists():
            backup = path.with_name(f"known_hosts.hlc-backup-{util.now_ts()}")
            shutil.copy2(path, backup)
            backup.chmod(0o600)
        util.atomic_write_text(path, "\n".join([*existing.splitlines(), *added]) + "\n", mode=0o600)
    else:
        backup = None
    return {"ok": True, "added": len(added), "path": str(path), "backup": str(backup) if backup else None}


# --------------------------------------------------------------- key install


def install_command(public_key: str) -> str:
    quoted = shlex.quote(public_key.strip())
    return (
        'umask 077; mkdir -p "$HOME/.ssh"; touch "$HOME/.ssh/authorized_keys"; '
        'chmod 700 "$HOME/.ssh"; chmod 600 "$HOME/.ssh/authorized_keys"; '
        f"grep -qxF {quoted} \"$HOME/.ssh/authorized_keys\" || "
        f"printf '%s\\n' {quoted} >> \"$HOME/.ssh/authorized_keys\"; printf 'HLC_KEY_INSTALLED\\n'"
    )


def manual_instructions(host: Host, public_key: str) -> str:
    return (
        f"# On the Mac, copy the key to {host.alias}:\n"
        f"ssh-copy-id -i {shlex.quote(str(ssh_dir() / 'id_ed25519.pub'))} "
        f"{'-p ' + str(host.port) + ' ' if host.port != 22 else ''}{host.user}@{host.address}\n\n"
        f"# Or paste this into a shell *on the Pi*:\n"
        f"mkdir -p ~/.ssh && chmod 700 ~/.ssh\n"
        f"printf '%s\\n' {shlex.quote(public_key.strip())} >> ~/.ssh/authorized_keys\n"
        f"chmod 600 ~/.ssh/authorized_keys\n"
    )


def install_key(
    host: Host,
    key_path: Path,
    password: str,
    *,
    workdir: Path,
    timeout: float = 30,
) -> dict[str, Any]:
    """Append the public key to the remote authorized_keys using one password.

    A temp askpass helper (mode 0700) reads a temp password file (mode 0600).
    Both are removed before this function returns, success or failure.
    """
    if host.local:
        return {"ok": True, "message": "local host needs no SSH key"}
    key_path = key_path.expanduser()
    info = key_info(key_path)
    # If key auth already works, there is nothing to do.
    probe = test_connection(host, key=key_path)
    if probe.get("ok"):
        return {"ok": True, "already": True, "message": "key authentication already works"}

    if not password:
        return {
            "ok": False,
            "manual": True,
            "message": "No password supplied. Install the key yourself, then retry.",
            "instructions": manual_instructions(host, info.public),
        }

    util.ensure_dir(workdir, 0o700)
    fd, passfile_name = tempfile.mkstemp(prefix=".hcpass-", dir=str(workdir))
    helper_path: Path | None = None
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(password)
        os.chmod(passfile_name, 0o600)
        helper = tempfile.NamedTemporaryFile(
            mode="w", prefix=".hcaskpass-", suffix=".sh", dir=str(workdir), delete=False
        )
        helper.write("#!/bin/sh\ncat \"$HLC_SSH_PASSFILE\"\n")
        helper.close()
        helper_path = Path(helper.name)
        helper_path.chmod(0o700)

        env = {
            "SSH_ASKPASS": str(helper_path),
            "SSH_ASKPASS_REQUIRE": "force",
            "DISPLAY": os.environ.get("DISPLAY", ":0"),
            "HLC_SSH_PASSFILE": passfile_name,
        }
        argv = [
            "ssh",
            *ssh_options(port=host.port, key=None, batch=False, control=False, connect_timeout=10),
            "-o", "PreferredAuthentications=password,keyboard-interactive",
            "-o", "PubkeyAuthentication=no",
            "-o", "NumberOfPasswordPrompts=1",
            f"{host.user}@{host.address}",
            install_command(info.public),
        ]
        result = util.run(argv, timeout=timeout, env=env)
        output = (result.stdout or "") + (result.stderr or "")
        if result.returncode == 0 and "HLC_KEY_INSTALLED" in result.stdout:
            verify = test_connection(host, key=key_path)
            if verify.get("ok"):
                return {"ok": True, "installed": True, "message": "public key installed and verified"}
            return {
                "ok": False,
                "installed": True,
                "manual": True,
                "message": "key was written, but key authentication still fails — check permissions on ~/.ssh",
                "instructions": manual_instructions(host, info.public),
            }
        return {
            "ok": False,
            "manual": True,
            "message": _friendly_ssh_error(output),
            "instructions": manual_instructions(host, info.public),
            "raw": output[-800:],
        }
    finally:
        Path(passfile_name).unlink(missing_ok=True)
        if helper_path is not None:
            helper_path.unlink(missing_ok=True)


