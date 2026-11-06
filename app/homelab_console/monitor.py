"""Fleet state assembly.

The monitor is the only component that talks to every host on a schedule.  It
combines live SSH telemetry with repo stacks and cached release data, writes
history samples, emits health-transition events, and exposes one immutable
snapshot for the HTTP layer.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from . import releases, remote, stacks, updates, util
from .config import Settings
from .hosts import Host, HostStore
from .store import Store

log = logging.getLogger("hlc.monitor")

TEMP_WARN = 75.0
TEMP_BAD = 82.0
DISK_WARN = 85.0
DISK_BAD = 92.0
MEM_WARN = 90.0


class FleetMonitor:
    def __init__(
        self,
        settings: Settings,
        host_store: HostStore,
        store: Store,
        release_service: releases.ReleaseService,
    ):
        self.settings = settings
        self.host_store = host_store
        self.store = store
        self.releases = release_service
        self.snapshot: dict[str, Any] = {
            "generated_at": 0,
            "generated_iso": "",
            "warming_up": True,
            "hosts": [],
            "stacks": [],
            "remote_stacks": [],
            "summary": {},
        }
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._release_thread: threading.Thread | None = None
        self._last_sample = 0.0
        self._last_release_check = 0.0
        self._release_checking = False
        self._last_release_error = ""
        self._notified_updates: set[str] = set()
        self._last_host_status: dict[str, str] = {}
        self._last_stack_status: dict[str, str] = {}
        self._collections = 0
        self.on_releases_updated: Callable[[dict[str, Any]], None] | None = None
        self.release_log: list[str] = []

    # ------------------------------------------------------------- lifecycle
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="hlc-monitor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=15)

    def request_refresh(self) -> None:
        self._wake.set()

    @property
    def collecting(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _loop(self) -> None:
        interval = max(10, int(self.settings.get("poll_interval_s", 30)))
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                self.collect()
            except Exception:  # never let one bad collection kill the loop
                log.exception("collection failed")
            elapsed = time.monotonic() - started
            wait_for = max(2.0, interval - elapsed)
            self._wake.wait(timeout=wait_for)
            self._wake.clear()

    # ------------------------------------------------------------- snapshot
    def state(self) -> dict[str, Any]:
        with self._lock:
            snapshot = self.snapshot
        payload = dict(snapshot)
        payload["age_s"] = int(time.time() - snapshot.get("generated_at", 0)) if snapshot.get("generated_at") else None
        payload["poll_interval_s"] = self.settings.get("poll_interval_s")
        payload["release_check"] = self.release_status()
        return payload

    def release_status(self) -> dict[str, Any]:
        return {
            **self.releases.status(),
            "checking": self._release_checking,
            "last_error": self._last_release_error,
            "auto_check_age_s": int(time.time() - self._last_release_check) if self._last_release_check else None,
        }

    # ------------------------------------------------------------ collection
    def collect(self) -> dict[str, Any]:
        started = time.monotonic()
        hosts = self.host_store.all()
        host_snaps: list[dict[str, Any]] = []
        if hosts:
            from concurrent.futures import as_completed

            with ThreadPoolExecutor(max_workers=min(6, len(hosts))) as pool:
                futures = {
                    pool.submit(remote.collect, host, remote_root=self.settings.remote_root): host for host in hosts
                }
                for future in as_completed(futures):
                    host = futures[future]
                    try:
                        host_snaps.append(future.result())
                    except Exception as exc:
                        host_snaps.append(
                            {
                                "alias": host.alias,
                                "online": False,
                                "latency_ms": 0,
                                "collected_at": util.now_ts(),
                                "error": str(exc),
                                "data": None,
                            }
                        )
        by_alias = {snap["alias"]: snap for snap in host_snaps}

        all_stacks = stacks.discover(self.settings, [host.alias for host in hosts])
        layout_warnings = stacks.layout_warnings(self.settings)
        stack_rows: list[dict[str, Any]] = []
        remote_rows: list[dict[str, Any]] = []
        update_rows: list[dict[str, Any]] = []
        exceptions: list[dict[str, Any]] = []

        for stack in all_stacks:
            host = next((item for item in hosts if item.alias == stack.host), None)
            snap = by_alias.get(stack.host)
            online = bool(snap and snap.get("online"))
            data = (snap or {}).get("data") or {}
            remote_hashes = (data.get("stack_files") or {}).get(stack.name) if online else None
            if online:
                runtime = stacks.stack_runtime(stack, data.get("containers") or [], remote_hashes)
            else:
                runtime = {
                    "status": "unknown",
                    "containers": [],
                    "running": 0,
                    "expected": len(stack.service_names),
                    "exceptions": [],
                    "sync": {"state": "unknown", "drift": [], "missing": [], "extra": []},
                }
            targets = updates.build_targets(stack, runtime, self.releases, host_arch=str(data.get("arch") or ""))
            update_rows.extend(targets)
            generated = {
                "host": stack.host,
                "name": stack.name,
                "project": stack.project,
                "dir": str(stack.path.relative_to(self.settings.repo)),
                "services": [service.public() for service in stack.services],
                "parse_error": stack.parse_error,
                "runtime": runtime,
                "targets": targets,
                "updates": [item for item in targets if item["status"] == "update-available"],
                "unpinned": [item for item in targets if item["status"] == "unpinned"],
                "git_dirty": self._stack_dirty(stack),
                "host_online": online,
                "host_error": (snap or {}).get("error", ""),
            }
            stack_rows.append(generated)
            for container in runtime.get("containers") or []:
                if container.get("exception"):
                    exceptions.append(
                        {
                            "host": stack.host,
                            "stack": stack.name,
                            "container": container.get("name"),
                            "service": container.get("service"),
                            "state": container.get("state"),
                            "health": container.get("health"),
                            "oom_killed": container.get("oom_killed"),
                            "uptime_s": container.get("uptime_s"),
                            "stopped_s": container.get("stopped_s"),
                            "restart_count": container.get("restart_count"),
                        }
                    )
        matched_containers = {
            container.get("name")
            for row in stack_rows
            for container in (row["runtime"].get("containers") or [])
        }
        for host in hosts:
            snap = by_alias.get(host.alias)
            if not snap or not snap.get("online"):
                continue
            for container in (snap.get("data") or {}).get("containers") or []:
                if container.get("exception") and container.get("name") not in matched_containers:
                    exceptions.append(
                        {
                            "host": host.alias,
                            "stack": "unmanaged",
                            "container": container.get("name"),
                            "service": container.get("service"),
                            "state": container.get("state"),
                            "health": container.get("health"),
                            "oom_killed": container.get("oom_killed"),
                            "uptime_s": container.get("uptime_s"),
                            "stopped_s": container.get("stopped_s"),
                            "restart_count": container.get("restart_count"),
                        }
                    )

        # Remote dirs that the repo does not know about (or whose version differs)
        repo_by_host: dict[str, set[str]] = {}
        for stack in all_stacks:
            repo_by_host.setdefault(stack.host, set()).add(stack.name)
        for alias, snap in by_alias.items():
            data = snap.get("data") or {}
            for remote_name, files in (data.get("stack_files") or {}).items():
                if remote_name in repo_by_host.get(alias, set()):
                    continue
                remote_rows.append(
                    {
                        "host": alias,
                        "name": remote_name,
                        "files": sorted(files),
                        "empty": not files,
                        "online": True,
                    }
                )

        host_rows = [self._host_row(host, by_alias.get(host.alias), stack_rows) for host in hosts]
        history = self.store.fleet_uptime(24)
        self._merge_history(host_rows, stack_rows, history)
        self._annotate_hosts(host_rows, stack_rows)

        summary = self._summary(host_rows, stack_rows, update_rows, exceptions, remote_rows)
        generated_at = util.now_ts()
        snapshot = {
            "generated_at": generated_at,
            "generated_iso": util.iso(generated_at),
            "duration_ms": int((time.monotonic() - started) * 1000),
            "warming_up": False,
            "repo": str(self.settings.repo),
            "hosts": host_rows,
            "stacks": stack_rows,
            "remote_stacks": remote_rows,
            "updates": update_rows,
            "exceptions": exceptions,
            "layout_warnings": layout_warnings,
            "summary": summary,
        }

        should_sample = (time.time() - self._last_sample) >= 55
        if should_sample:
            self.store.record_snapshot(host_rows, stack_rows, generated_at)
            self._last_sample = time.time()
            if self._collections % 60 == 0 and self._collections:
                self.store.prune()
        self._collections += 1
        self._record_transitions(host_rows, stack_rows, update_rows)
        with self._lock:
            self.snapshot = snapshot
        self._maybe_check_releases()
        log.debug("collection finished in %sms", snapshot["duration_ms"])
        return snapshot

    def _maybe_check_releases(self) -> None:
        if self._release_checking:
            return
        ttl = int(self.settings.get("release_ttl_s", 6 * 3600))
        now = time.time()
        if self._last_release_check and (now - self._last_release_check) < ttl:
            return
        if not self._last_release_check and not self.settings.get("check_releases_on_start", True):
            return
        self._last_release_check = now
        self._release_checking = True
        self._release_thread = threading.Thread(target=self._release_work, name="hlc-releases", daemon=True)
        self._release_thread.start()

    def _release_work(self) -> None:
        self.release_log = []
        try:
            requests = self._release_requests()
            self.release_log.append(f"checking {len(requests)} image source(s)")
            summary = self.releases.refresh(
                requests,
                progress=lambda message: self.release_log.append(message),
            )
            self._last_release_error = "; ".join(summary.get("errors") or [])[:300]
            self.release_log.extend(f"error: {error}" for error in summary.get("errors") or [])
            if summary.get("errors"):
                ttl = int(self.settings.get("release_ttl_s", 6 * 3600))
                self._last_release_check = time.time() - ttl + 600
            if self.on_releases_updated:
                self.on_releases_updated(self.releases.status())
            self.request_refresh()
        except Exception as exc:
            log.exception("release refresh failed")
            self._last_release_error = str(exc)
            self.release_log.append(f"release refresh failed: {exc}")
        finally:
            self._release_checking = False
            self._last_release_check = time.time()

    def _release_requests(self) -> list[dict[str, Any]]:
        requests = {}
        snapshot = self.state()
        labels_by_image = {}
        for host in snapshot.get("hosts", []):
            for container in (host.get("data") or {}).get("containers") or []:
                labels_by_image[container.get("image") or ""] = container.get("image_labels") or {}
        for stack in stacks.discover(self.settings, [host.alias for host in self.host_store.all()]):
            groups = self.releases.stack_groups(stack, labels_by_image)
            for service in stack.services:
                if not service.image:
                    continue
                requests[service.image.repo] = {
                    "repo": service.image.repo,
                    "current": service.image.tag if service.image.explicit_tag else "",
                    "labels": labels_by_image.get(service.image_raw) or {},
                    "groups": sorted(groups),
                }
        return list(requests.values())

    def check_releases_now(self, force: bool = True) -> bool:
        if self._release_checking:
            return False
        self._last_release_check = 0 if force else self._last_release_check
        self._release_checking = True
        self._release_thread = threading.Thread(target=self._release_work, name="hlc-releases", daemon=True)
        self._release_thread.start()
        return True

    # ---------------------------------------------------------------- rows
    def _host_row(self, host: Host, snap: dict[str, Any] | None, stacks_rows: list[dict[str, Any]]) -> dict[str, Any]:
        online = bool(snap and snap.get("online"))
        data = (snap or {}).get("data") or {}
        containers = data.get("containers") or []
        running = sum(1 for item in containers if item.get("state") == "running")
        total = len(containers)
        temp = data.get("temperature_c")
        disks = data.get("disks") or []
        root_disk = next((disk for disk in disks if disk.get("mountpoint") == "/"), None)
        disk_pct = None
        if root_disk:
            try:
                disk_pct = float(str(root_disk.get("percent", "")).replace("%", ""))
            except ValueError:
                disk_pct = None
        memory = data.get("memory") or {}
        throttled = data.get("throttled") or {}
        host_stacks = [row for row in stacks_rows if row["host"] == host.alias]
        update_count = sum(len(row["updates"]) for row in host_stacks)
        exception_count = sum(1 for item in containers if item.get("exception"))
        problems = []
        if not online:
            problems.append("unreachable")
        else:
            if throttled.get("now"):
                problems.append("throttled: " + ", ".join(throttled["now"]))
            if temp is not None and temp >= TEMP_BAD:
                problems.append(f"hot {temp:.0f}°C")
            elif temp is not None and temp >= TEMP_WARN:
                problems.append(f"warm {temp:.0f}°C")
            if disk_pct is not None and disk_pct >= DISK_BAD:
                problems.append(f"disk {disk_pct:.0f}%")
            elif disk_pct is not None and disk_pct >= DISK_WARN:
                problems.append(f"disk {disk_pct:.0f}%")
            if memory.get("used_pct") is not None and memory["used_pct"] >= MEM_WARN:
                problems.append(f"memory {memory['used_pct']:.0f}%")
            for disk in disks:
                if disk.get("mountpoint") == "/":
                    continue
                try:
                    percent = float(str(disk.get("percent", "")).replace("%", ""))
                except ValueError:
                    continue
                if percent >= DISK_BAD:
                    problems.append(f"{disk.get('mountpoint')} {percent:.0f}%")
                elif percent >= DISK_WARN:
                    problems.append(f"{disk.get('mountpoint')} {percent:.0f}%")
            if exception_count:
                problems.append(f"{exception_count} container exception(s)")
            if data.get("failed_units"):
                problems.append(f"{len(data['failed_units'])} failed unit(s)")
            if (data.get("time") or {}).get("reboot_required"):
                problems.append("reboot required")
        status = "offline" if not online else ("degraded" if problems else "online")
        return {
            "alias": host.alias,
            "user": host.user,
            "address": host.address,
            "port": host.port,
            "local": host.local,
            "role": host.role,
            "notes": host.notes,
            "tags": host.tags,
            "online": online,
            "status": status,
            "latency_ms": (snap or {}).get("latency_ms", 0),
            "error": (snap or {}).get("error", ""),
            "collected_at": (snap or {}).get("collected_at", 0),
            "problems": problems,
            "data": data,
            "summary": {
                "hostname": data.get("hostname") or host.alias,
                "model": data.get("model") or "",
                "os": data.get("os") or "",
                "kernel": data.get("kernel") or "",
                "arch": data.get("arch") or "",
                "uptime_s": data.get("uptime_s") or 0,
                "uptime": util.human_duration(data.get("uptime_s")),
                "temp_c": temp,
                "throttled": throttled,
                "load": data.get("load") or [],
                "mem_pct": memory.get("used_pct"),
                "mem_used": memory.get("used"),
                "mem_total": memory.get("total"),
                "disk_pct": disk_pct,
                "containers_running": running,
                "containers_total": total,
                "containers_bad": sum(1 for item in containers if item.get("exception")),
                "updates": update_count,
                "docker_version": data.get("docker_version") or "",
                "compose_version": data.get("compose_version") or "",
                "tailscale": data.get("tailscale") or {},
                "reboot_required": bool((data.get("time") or {}).get("reboot_required")),
                "ntp_synced": (data.get("time") or {}).get("ntp_synced"),
                "failed_units": data.get("failed_units") or [],
                "raid": data.get("raid") or [],
            },
            "stacks": [
                {
                    "name": row["name"],
                    "status": row["runtime"].get("status"),
                    "sync": row["runtime"].get("sync", {}).get("state"),
                    "updates": len(row["updates"]),
                }
                for row in host_stacks
            ],
        }

    def _merge_history(self, host_rows: list[dict[str, Any]], stack_rows: list[dict[str, Any]], history: dict[str, Any]) -> None:
        host_history = {row["host"]: row for row in history.get("hosts", [])}
        stack_history = {(row["host"], row["stack"]): row for row in history.get("stacks", [])}
        for host in host_rows:
            item = host_history.get(host["alias"])
            host["history"] = {
                "uptime_pct": item["uptime_pct"] if item else None,
                "last_ts": item["last_ts"] if item else None,
            }
            host["history_points"] = self.store.host_history(host["alias"], 24).get("points", [])[-48:]
        for stack in stack_rows:
            item = stack_history.get((stack["host"], stack["name"]))
            stack["runtime"]["uptime_pct"] = item["uptime_pct"] if item else None
            stack["runtime"]["degraded_pct"] = item["degraded_pct"] if item else None
            stack["runtime"]["history_points"] = self.store.stack_history(stack["host"], stack["name"], 24).get("points", [])[-48:]

    def _annotate_hosts(self, host_rows: list[dict[str, Any]], stack_rows: list[dict[str, Any]]) -> None:
        for host in host_rows:
            host["update_count"] = sum(len(row["updates"]) for row in stack_rows if row["host"] == host["alias"])

    def _stack_dirty(self, stack: stacks.Stack) -> bool:
        # Cheap: only check modification times against the last commit is not
        # available per-file; git status is computed lazily by the API detail
        # view. Fleet rows stay false to avoid a git call per stack per poll.
        return False

    def _summary(
        self,
        hosts: list[dict[str, Any]],
        stack_rows: list[dict[str, Any]],
        update_rows: list[dict[str, Any]],
        exceptions: list[dict[str, Any]],
        remote_rows: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return {
            "hosts_total": len(hosts),
            "hosts_online": sum(1 for host in hosts if host["online"]),
            "hosts_offline": [host["alias"] for host in hosts if not host["online"]],
            "hosts_degraded": [host["alias"] for host in hosts if host["status"] == "degraded"],
            "containers_running": sum(host["summary"]["containers_running"] for host in hosts),
            "containers_total": sum(host["summary"]["containers_total"] for host in hosts),
            "stacks_total": len(stack_rows),
            "stacks_up": sum(1 for row in stack_rows if row["runtime"].get("status") == "up"),
            "stacks_degraded": [
                f"{row['host']}/{row['name']}" for row in stack_rows if row["runtime"].get("status") == "degraded"
            ],
            "stacks_down": [
                f"{row['host']}/{row['name']}" for row in stack_rows if row["runtime"].get("status") == "down"
            ],
            "stacks_not_deployed": [
                f"{row['host']}/{row['name']}"
                for row in stack_rows
                if row["runtime"].get("status") == "not-deployed"
            ],
            "drift": [
                f"{row['host']}/{row['name']}"
                for row in stack_rows
                if row["runtime"].get("sync", {}).get("state") == "drift"
            ],
            "remote_extra": [
                f"{row['host']}/{row['name']}"
                for row in stack_rows
                if row["runtime"].get("sync", {}).get("state") == "extra"
            ],
            "updates_available": sum(1 for row in update_rows if row["status"] == "update-available"),
            "unpinned": sum(1 for row in update_rows if row["status"] == "unpinned"),
            "exceptions": len(exceptions),
            "remote_only": [
                f"{row['host']}/{row['name']}" for row in remote_rows if not row["empty"]
            ],
            "remote_empty_dirs": [f"{row['host']}/{row['name']}" for row in remote_rows if row["empty"]],
            "releases": self.releases.status(),
        }

    def _record_transitions(
        self,
        hosts: list[dict[str, Any]],
        stack_rows: list[dict[str, Any]],
        update_rows: list[dict[str, Any]],
    ) -> None:
        first = self._collections <= 1
        for host in hosts:
            status = host["status"]
            previous = self._last_host_status.get(host["alias"])
            if previous and previous != status:
                if status == "offline":
                    self.store.add_event("health", "error", f"{host['alias']} went offline: {host['error']}", host=host["alias"])
                elif previous == "offline":
                    self.store.add_event("health", "success", f"{host['alias']} is back online", host=host["alias"])
                elif status == "degraded":
                    self.store.add_event(
                        "health", "warning", f"{host['alias']} degraded: {', '.join(host['problems'])}", host=host["alias"]
                    )
                else:
                    self.store.add_event("health", "success", f"{host['alias']} healthy again", host=host["alias"])
            if not first:
                self._last_host_status[host["alias"]] = status
            else:
                self._last_host_status.setdefault(host["alias"], status)

        for stack in stack_rows:
            key = f"{stack['host']}/{stack['name']}"
            status = stack["runtime"].get("status", "unknown")
            previous = self._last_stack_status.get(key)
            if previous and previous != status and status in ("down", "degraded"):
                self.store.add_event(
                    "health",
                    "warning" if status == "degraded" else "error",
                    f"{key} is {status}",
                    host=stack["host"],
                    svc=stack["name"],
                )
            if not first:
                self._last_stack_status[key] = status
            else:
                self._last_stack_status.setdefault(key, status)

        for row in update_rows:
            if row["status"] != "update-available":
                continue
            key = f"{row['host']}/{row['stack']}/{row['service']}@{row['latest']}"
            if key in self._notified_updates:
                continue
            self._notified_updates.add(key)
            if not first:
                self.store.add_event(
                    "release",
                    "info",
                    f"{row['host']}/{row['stack']} {row['service']}: {row['current_display']} → {row['latest']}",
                    host=row["host"],
                    svc=row["stack"],
                )
