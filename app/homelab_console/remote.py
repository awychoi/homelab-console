"""Remote telemetry and file access over SSH.

One SSH round-trip per host collects everything the dashboard needs: uptime,
temperature, throttle flags, memory, disk, Docker state, listening ports,
Tailscale identity, RAID health, failed units and sha256 hashes of the pushed
stack files (which is how drift is detected without copying files).

The collector is a Python program shipped on stdin (``python3 -``), so the
only host dependency is python3, present on Raspberry Pi OS / Debian.
"""

from __future__ import annotations

import base64
import json
import shlex
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import util
from .hosts import Host
from .sshkeys import _friendly_ssh_error, ssh_argv

COLLECT_TIMEOUT = 55


def _ssh(host: Host, remote_command: str, *, input_text: str | None = None, timeout: float = 45, key: Path | None = None):
    argv = ssh_argv(host, key=key) + [remote_command]
    return util.run(argv, input_text=input_text, timeout=timeout)


# --------------------------------------------------------------- collector

REMOTE_COLLECTOR = r'''
import hashlib, json, os, re, subprocess, sys, time

ROOT = sys.argv[1] if len(sys.argv) > 1 else "/opt/stacks"

def run(args, timeout=12):
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                           errors="replace", stdin=subprocess.DEVNULL)
        return (p.stdout or "").strip()
    except Exception:
        return ""

def read(path, limit=4000):
    try:
        with open(path, "r", errors="replace") as handle:
            return handle.read(limit).strip()
    except Exception:
        return ""

def float_read(path):
    try:
        with open(path) as handle:
            return float(handle.read().strip())
    except Exception:
        return None

def int_read(path):
    try:
        with open(path) as handle:
            return int(handle.read().strip())
    except Exception:
        return None

def os_release():
    data = {}
    for line in read("/etc/os-release", 4000).splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            data[key] = value.strip().strip('"')
    return data

def model():
    value = read("/proc/device-tree/model", 200).replace("\x00", "").strip()
    if value:
        return value
    cpuinfo = read("/proc/cpuinfo", 8000)
    for line in cpuinfo.splitlines():
        if line.lower().startswith("model") or line.lower().startswith("hardware"):
            parts = line.split(":", 1)
            if len(parts) == 2 and parts[1].strip():
                return parts[1].strip()
    return ""

def temperature():
    value = run(["vcgencmd", "measure_temp"], timeout=5)
    match = re.search(r"([0-9]+(?:\.[0-9]+)?)", value)
    if match:
        return float(match.group(1))
    raw = int_read("/sys/class/thermal/thermal_zone0/temp")
    if raw is not None:
        return round(raw / 1000.0, 1)
    return None

THROTTLE_BITS = {
    0: ("now", "under-voltage"),
    1: ("now", "arm-frequency-capped"),
    2: ("now", "throttled"),
    3: ("now", "soft-temp-limit"),
    16: ("since-boot", "under-voltage"),
    17: ("since-boot", "arm-frequency-capped"),
    18: ("since-boot", "throttled"),
    19: ("since-boot", "soft-temp-limit"),
}

def throttled():
    text = run(["vcgencmd", "get_throttled"], timeout=5)
    match = re.search(r"0x([0-9a-fA-F]+)", text)
    if not match:
        return {"supported": False, "raw": "", "now": [], "since_boot": [], "ok": True}
    value = int(match.group(1), 16)
    now, since = [], []
    for bit, (when, label) in THROTTLE_BITS.items():
        if value & (1 << bit):
            (now if when == "now" else since).append(label)
    return {"supported": True, "raw": hex(value), "value": value, "now": now,
            "since_boot": since, "ok": not now}

def memory():
    info = {}
    for line in read("/proc/meminfo", 8000).splitlines():
        key, _, rest = line.partition(":")
        parts = rest.split()
        if parts:
            try:
                info[key] = int(parts[0]) * 1024
            except ValueError:
                pass
    total = info.get("MemTotal", 0)
    available = info.get("MemAvailable", info.get("MemFree", 0))
    swap_total = info.get("SwapTotal", 0)
    swap_free = info.get("SwapFree", 0)
    return {
        "total": total,
        "available": available,
        "used": max(0, total - available),
        "used_pct": round(100 * (total - available) / total, 1) if total else None,
        "swap_total": swap_total,
        "swap_used": max(0, swap_total - swap_free),
    }

def disks():
    text = run(["df", "-P", "-h", "-T", "-x", "tmpfs", "-x", "devtmpfs", "-x", "squashfs",
                "-x", "overlay", "-x", "efivarfs"], timeout=10)
    rows = []
    for line in text.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 7:
            continue
        filesystem, fstype = fields[0], fields[1]
        size, used, avail, pct = fields[2:6]
        mountpoint = " ".join(fields[6:])
        rows.append({
            "filesystem": filesystem, "filesystem_type": fstype,
            "size": size, "used": used, "available": avail,
            "percent": pct.rstrip("%"), "mountpoint": mountpoint,
        })
    return rows

def raid():
    text = read("/proc/mdstat", 8000)
    devices = {}
    current = None
    for line in text.splitlines():
        match = re.match(r"^(md\d+)\s*:\s*(.*)$", line)
        if match:
            current = match.group(1)
            devices[current] = {"name": current, "status": match.group(2), "flags": None, "degraded": None}
            continue
        if current and "[" in line and "]" in line:
            flags = re.findall(r"\[([U_]+)\]", line)
            if flags:
                devices[current]["flags"] = flags[-1]
                devices[current]["degraded"] = "_" in flags[-1]
    return list(devices.values())

def listeners():
    text = run(["ss", "-H", "-tln"], timeout=8)
    rows = []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) >= 4:
            rows.append(fields[3])
    return sorted(set(rows))[:100]

def failed_units():
    text = run(["systemctl", "--failed", "--no-legend", "--plain"], timeout=8)
    rows = []
    for line in text.splitlines():
        fields = line.split()
        if fields and fields[0].endswith(".service"):
            rows.append(fields[0])
    return rows[:20]

def tailscale():
    text = run(["tailscale", "status", "--json"], timeout=10)
    if not text:
        return {"installed": False}
    try:
        payload = json.loads(text)
    except Exception:
        return {"installed": True, "error": "could not parse tailscale status"}
    self_info = payload.get("Self") or {}
    ips = self_info.get("TailscaleIPs") or []
    v4 = [ip for ip in ips if ":" not in ip]
    peers = payload.get("Peer") or {}
    online_peers = sum(1 for peer in peers.values() if peer.get("Online"))
    return {
        "installed": True,
        "hostname": self_info.get("HostName") or "",
        "ipv4": v4[0] if v4 else "",
        "ips": ips,
        "backend": payload.get("BackendState") or "",
        "version": payload.get("Version") or "",
        "online": bool(self_info.get("Online")),
        "peers_total": len(peers),
        "peers_online": online_peers,
    }

def image_metadata(ids):
    if not ids:
        return {}
    raw = run(["docker", "image", "inspect", *ids], timeout=20)
    try:
        payload = json.loads(raw)
    except Exception:
        return {}
    out = {}
    for item in payload:
        labels = item.get("Config", {}).get("Labels") or {}
        out[item.get("Id", "")] = {
            "repo_digests": item.get("RepoDigests") or [],
            "repo_tags": item.get("RepoTags") or [],
            "labels": {k: v for k, v in labels.items() if k.startswith("org.opencontainers.image.")},
        }
    return out

def containers():
    ids = run(["docker", "ps", "-aq"], timeout=10).split()
    if not ids:
        return [], {}
    raw = run(["docker", "inspect", *ids], timeout=25)
    try:
        payload = json.loads(raw)
    except Exception:
        return [], {}
    image_ids = sorted({item.get("Image", "") for item in payload if item.get("Image")})
    images = image_metadata(image_ids)
    rows = []
    for item in payload:
        config = item.get("Config") or {}
        state = item.get("State") or {}
        labels = config.get("Labels") or {}
        health = (state.get("Health") or {})
        ports = []
        for container_port, bindings in ((item.get("NetworkSettings") or {}).get("Ports") or {}).items():
            for binding in bindings or []:
                ports.append({
                    "container": container_port,
                    "host_ip": binding.get("HostIp") or "",
                    "host_port": binding.get("HostPort") or "",
                })
        mounts = []
        for mount in item.get("Mounts") or []:
            mounts.append({
                "type": mount.get("Type") or "",
                "source": mount.get("Source") or "",
                "destination": mount.get("Destination") or "",
            })
        rows.append({
            "name": (item.get("Name") or "").lstrip("/"),
            "image": config.get("Image") or "",
            "image_id": item.get("Image") or "",
            "state": state.get("Status") or "unknown",
            "health": health.get("Status") or "",
            "started_at": state.get("StartedAt") or "",
            "finished_at": state.get("FinishedAt") or "",
            "restart_count": int(item.get("RestartCount") or 0),
            "oom_killed": bool(state.get("OOMKilled")),
            "project": labels.get("com.docker.compose.project") or "",
            "service": labels.get("com.docker.compose.service") or "",
            "working_dir": labels.get("com.docker.compose.project.working_dir") or "",
            "config_files": labels.get("com.docker.compose.project.config_files") or "",
            "ports": ports,
            "mounts": mounts,
        })
    return rows, images

def stack_files():
    rows = {}
    if not os.path.isdir(ROOT):
        return rows
    count = 0
    try:
        names = sorted(os.listdir(ROOT))
    except Exception:
        return rows
    for name in names:
        path = os.path.join(ROOT, name)
        if not os.path.isdir(path):
            continue
        files = {}
        for base, dirs, filenames in os.walk(path):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for filename in filenames:
                if filename.startswith("._") or filename in (".DS_Store", ".gitkeep"):
                    continue
                full = os.path.join(base, filename)
                rel = os.path.relpath(full, path)
                try:
                    if os.path.getsize(full) > 4 * 1024 * 1024:
                        continue
                    digest = hashlib.sha256()
                    with open(full, "rb") as handle:
                        for chunk in iter(lambda: handle.read(65536), b""):
                            digest.update(chunk)
                    files[rel] = digest.hexdigest()
                    count += 1
                    if count > 800:
                        break
                except Exception:
                    continue
            if count > 800:
                break
        rows[name] = files
    return rows

def uptime_seconds():
    try:
        with open("/proc/uptime") as handle:
            return float(handle.read().split()[0])
    except Exception:
        return None

def time_state():
    ntp = run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"], timeout=6)
    boot = uptime_seconds()
    return {
        "ntp_synced": ntp == "yes" if ntp else None,
        "boot_epoch": (time.time() - boot) if boot else None,
        "reboot_required": os.path.exists("/var/run/reboot-required"),
    }

release = os_release()
containers_rows, images = containers()
try:
    load = [float(value) for value in read("/proc/loadavg", 200).split()[:3]]
except Exception:
    load = []

print(json.dumps({
    "hostname": run(["hostname"], timeout=4),
    "fqdn": run(["hostname", "-f"], timeout=4),
    "os": release.get("PRETTY_NAME") or release.get("NAME") or "",
    "os_id": release.get("ID") or "",
    "kernel": run(["uname", "-r"], timeout=4),
    "arch": run(["uname", "-m"], timeout=4),
    "model": model(),
    "cpu_count": os.cpu_count(),
    "uptime_s": round(uptime_seconds() or 0, 1),
    "load": load,
    "memory": memory(),
    "temperature_c": temperature(),
    "throttled": throttled(),
    "disks": disks(),
    "raid": raid(),
    "docker_version": run(["docker", "version", "--format", "{{.Server.Version}}"], timeout=10),
    "compose_version": run(["docker", "compose", "version", "--short"], timeout=10),
    "containers": containers_rows,
    "images": images,
    "stack_files": stack_files(),
    "listeners": listeners(),
    "failed_units": failed_units(),
    "tailscale": tailscale(),
    "time": time_state(),
    "collected_epoch": time.time(),
}))
'''


def _normalize(host: Host, payload: dict[str, Any], latency_ms: int) -> dict[str, Any]:
    now = util.now_ts()
    containers = []
    images = payload.get("images") or {}
    for item in payload.get("containers") or []:
        started_ts = util.parse_docker_time(item.get("started_at"))
        image = str(item.get("image") or "")
        meta = images.get(str(item.get("image_id") or ""), {})
        digests = meta.get("repo_digests") or []
        digest = ""
        for value in digests:
            if "@sha256:" in value:
                digest = value.split("@", 1)[1]
                break
        if not digest and isinstance(item.get("image_id"), str) and item["image_id"].startswith("sha256:"):
            digest = item["image_id"]
        uptime_s = max(0, now - int(started_ts)) if started_ts and item.get("state") == "running" else None
        stopped_s = max(0, now - int(started_ts)) if started_ts and item.get("state") != "running" else None
        containers.append(
            {
                **item,
                "image_repo": image.split("@")[0].rsplit(":", 1)[0] if "@" in image else _repo_of(image),
                "image_tag": _tag_of(image),
                "digest": digest,
                "repo_digests": digests,
                "image_labels": meta.get("labels") or {},
                "uptime_s": uptime_s,
                "stopped_s": stopped_s,
                "healthy": item.get("health") in ("healthy", "") and item.get("state") == "running",
                "exception": item.get("state") != "running"
                or item.get("health") in ("unhealthy", "starting")
                or bool(item.get("oom_killed")),
            }
        )
    stacks = payload.get("stack_files") or {}
    data = {
        **payload,
        "containers": containers,
        "stack_files": stacks,
        "collected_epoch": payload.get("collected_epoch") or now,
        "hostname": payload.get("hostname") or host.alias,
    }
    return {
        "alias": host.alias,
        "online": True,
        "latency_ms": latency_ms,
        "collected_at": now,
        "error": "",
        "data": data,
    }


def _repo_of(image: str) -> str:
    slash = image.rfind("/")
    colon = image.rfind(":")
    return image[:colon] if colon > slash else image


def _tag_of(image: str) -> str:
    if "@" in image:
        return image.split("@", 1)[0].rsplit(":", 1)[1] if ":" in image.split("@")[0] else "latest"
    slash = image.rfind("/")
    colon = image.rfind(":")
    return image[colon + 1 :] if colon > slash else "latest"


def collect(host: Host, *, remote_root: str = "/opt/stacks", key: Path | None = None) -> dict[str, Any]:
    """Return a normalized telemetry payload, or an offline record with error."""
    if host.local:
        return _collect_local(host)
    started = time.monotonic()
    command = f"python3 - {shlex.quote(remote_root)}"
    result = _ssh(host, command, input_text=REMOTE_COLLECTOR, timeout=COLLECT_TIMEOUT, key=key)
    latency = int((time.monotonic() - started) * 1000)
    if result.returncode != 0:
        error = _friendly_ssh_error(result.stderr or result.stdout or "")
        return {
            "alias": host.alias,
            "online": False,
            "latency_ms": latency,
            "collected_at": util.now_ts(),
            "error": error,
            "raw": (result.stderr or "")[-800:],
            "data": None,
        }
    try:
        payload = json.loads(result.stdout)
    except ValueError:
        return {
            "alias": host.alias,
            "online": False,
            "latency_ms": latency,
            "collected_at": util.now_ts(),
            "error": "collector returned invalid JSON (is python3 present on the host?)",
            "raw": result.stdout[-400:],
            "data": None,
        }
    return _normalize(host, payload, latency)


def _collect_local(host: Host) -> dict[str, Any]:
    """Minimal telemetry for the controller (monitoring only; no deploy root)."""
    import os
    import platform
    import shutil
    import sys

    boot = time.time() - _mac_uptime()
    disk = shutil.disk_usage(str(Path.home()))
    data = {
        "hostname": platform.node() or "mac",
        "os": (
            f"macOS {platform.mac_ver()[0]}"
            if sys.platform == "darwin"
            else f"{platform.system()} {platform.release()}"
        ),
        "arch": platform.machine(),
        "model": "Mac (controller)",
        "kernel": platform.release(),
        "uptime_s": round(_mac_uptime(), 1),
        "load": list(os.getloadavg()),
        "memory": {"total": 0, "available": 0, "used": 0, "used_pct": None},
        "temperature_c": None,
        "throttled": {"supported": False, "raw": "", "now": [], "since_boot": [], "ok": True},
        "disks": [
            {
                "filesystem": str(Path.home()),
                "mountpoint": "/",
                "size": _human_bytes(disk.total),
                "used": _human_bytes(disk.used),
                "available": _human_bytes(disk.free),
                "percent": str(round(100 * disk.used / disk.total)) if disk.total else "0",
            }
        ],
        "raid": [],
        "docker_version": "",
        "compose_version": "",
        "containers": [],
        "images": {},
        "stack_files": {},
        "listeners": [],
        "failed_units": [],
        "tailscale": {},
        "time": {"ntp_synced": None, "boot_epoch": boot, "reboot_required": False},
        "collected_epoch": time.time(),
    }
    return {
        "alias": host.alias,
        "online": True,
        "latency_ms": 0,
        "collected_at": util.now_ts(),
        "error": "",
        "data": data,
    }


def _mac_uptime() -> float:
    import sys

    if sys.platform == "darwin":
        result = util.run(["sysctl", "-n", "kern.boottime"], timeout=5)
        # { sec = 1690000000, usec = 0 } ...
        import re

        match = re.search(r"sec\s*=\s*(\d+)", result.stdout)
        if match:
            return max(0.0, time.time() - int(match.group(1)))
    try:
        with open("/proc/uptime") as handle:
            return float(handle.read().split()[0])
    except OSError:
        return 0.0


def _human_bytes(value: int) -> str:
    for unit in ("B", "K", "M", "G", "T"):
        if value < 1024 or unit == "T":
            return f"{value:.0f}{unit}" if unit == "B" else f"{value:.1f}{unit}"
        value /= 1024
    return f"{value:.1f}T"


# ------------------------------------------------------------ remote files

REMOTE_FILES = r'''
import json, os, sys
root = sys.argv[1]
files = {}
skipped = []
for base, dirs, names in os.walk(root):
    dirs[:] = [d for d in dirs if d != ".git"]
    for name in names:
        if name.startswith("._") or name.startswith(".git") or name in (".DS_Store", ".gitkeep"):
            continue
        path = os.path.join(base, name)
        rel = os.path.relpath(path, root)
        try:
            if os.path.islink(path) or not os.path.isfile(path):
                skipped.append(rel)
                continue
            if os.path.getsize(path) > 1024 * 1024:
                skipped.append(rel)
                continue
            with open(path, "rb") as handle:
                data = handle.read()
            if b"\x00" in data[:4096]:
                skipped.append(rel)
                continue
            text = data.decode("utf-8")
        except Exception:
            skipped.append(rel)
            continue
        files[rel] = text
print(json.dumps({"files": files, "complete": not skipped, "skipped": sorted(skipped)[:80]}))
'''


@dataclass
class RemoteListing:
    files: dict[str, str] = field(default_factory=dict)
    complete: bool = True
    skipped: list[str] = field(default_factory=list)


def read_remote_listing(host: Host, stack: str, remote_root: str = "/opt/stacks") -> RemoteListing:
    """Read a stack directory, reporting whether every file was readable.

    The caller uses ``complete`` to decide whether a local path absent from the
    listing is really new or merely unreadable on the host.  Marking the plan
    destructive in the latter case prevents a silent overwrite.
    """
    if host.local:
        root = Path(remote_root) / stack
        files: dict[str, str] = {}
        skipped: list[str] = []
        if not root.is_dir() or root.is_symlink():
            return RemoteListing(files, True, [])
        for path in sorted(root.rglob("*")):
            if path.name.startswith("._") or path.name.startswith(".git") or path.name in (".DS_Store", ".gitkeep"):
                continue
            rel = path.relative_to(root).as_posix()
            if any(part == ".git" for part in path.relative_to(root).parts):
                continue
            if path.is_symlink() or not path.is_file():
                skipped.append(rel)
                continue
            try:
                if path.stat().st_size > 1024 * 1024:
                    skipped.append(rel)
                    continue
                data = path.read_bytes()
            except OSError:
                skipped.append(rel)
                continue
            if not util.is_probably_text(data):
                skipped.append(rel)
                continue
            files[rel] = data.decode("utf-8")
        return RemoteListing(files, not skipped, skipped[:80])

    root = f"{remote_root.rstrip('/')}/{stack}"
    result = _ssh(host, f"python3 - {shlex.quote(root)}", input_text=REMOTE_FILES, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(_friendly_ssh_error(result.stderr))
    try:
        raw = json.loads(result.stdout or "{}")
    except ValueError as exc:
        raise RuntimeError("could not read remote files") from exc
    if not isinstance(raw, dict):
        raise RuntimeError("could not read remote files")
    # The current collector returns {files, complete, skipped}; an older
    # response was a bare {path: base64} map.  Treat the legacy shape as
    # incomplete so the plan stays conservative.
    if "files" in raw and isinstance(raw.get("files"), dict):
        encoded = raw["files"]
        complete = bool(raw.get("complete", False))
        skipped = [str(item) for item in (raw.get("skipped") or [])][:80]
        legacy = False
    else:
        encoded = raw
        complete = False
        skipped = []
        legacy = True
    files = {}
    for rel, value in encoded.items():
        if not isinstance(rel, str) or not isinstance(value, str):
            continue
        try:
            text = base64.b64decode(value).decode("utf-8") if legacy else value
        except Exception:
            continue
        files[rel] = text
    return RemoteListing(files, complete, skipped)


def read_remote_files(host: Host, stack: str, remote_root: str = "/opt/stacks") -> dict[str, str]:
    """Backward-compatible view for callers that only need the text files."""
    return read_remote_listing(host, stack, remote_root).files

def remote_shell(host: Host, command: str, *, timeout: float = 60, key: Path | None = None):
    return _ssh(host, command, timeout=timeout, key=key)
