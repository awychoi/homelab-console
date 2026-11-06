"""Composition root: shared singletons plus the auto-update sweeper."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from .config import Settings, get_settings
from .hosts import HostStore
from .monitor import FleetMonitor
from .releases import ReleaseService
from .repos import RepoFleet
from .runner import TaskManager
from .store import Store

log = logging.getLogger("hlc.app")

# A slow SSH probe must never hold the boot screen hostage. Past this the
# browser draws the dashboard with whatever has arrived and lets the rest
# stream in, exactly like a normal refresh.
WARMUP_SOFT_DEADLINE_S = 12.0


class AppService:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.hosts = HostStore(self.settings)
        self.store = Store(self.settings)
        self.store.mark_interrupted_runs()
        self.releases = ReleaseService(self.settings)
        self.monitor = FleetMonitor(self.settings, self.hosts, self.store, self.releases)
        self.tasks = TaskManager(self.settings, self.store, self.hosts, self.monitor)
        self.repos = RepoFleet(self.settings, self.store, self.tasks)
        self._stop = threading.Event()
        self._auto_thread: threading.Thread | None = None
        self._boot_gitea_thread: threading.Thread | None = None
        self._boot_started = 0.0
        self._started = False
        self._last_auto_sweep = 0.0

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._boot_started = time.time()
        self.monitor.start()
        self.repos.start()
        self._boot_gitea_thread = threading.Thread(target=self._boot_gitea, name="hlc-boot-gitea", daemon=True)
        self._boot_gitea_thread.start()
        self._auto_thread = threading.Thread(target=self._auto_loop, name="hlc-auto", daemon=True)
        self._auto_thread.start()
        log.info("console service started (repo=%s, state=%s)", self.settings.repo, self.settings.state_dir)

    def stop(self) -> None:
        self._stop.set()
        self.monitor.stop()
        self.repos.stop()
        if self._boot_gitea_thread:
            self._boot_gitea_thread.join(timeout=5)
        if self._auto_thread:
            self._auto_thread.join(timeout=5)
        self.store.close()

    def _auto_loop(self) -> None:
        while not self._stop.wait(45):
            try:
                run_id = self.tasks.auto_sweep()
                if run_id:
                    log.info("started auto-update run %s", run_id)
            except Exception:
                log.exception("auto-update sweep failed")
            if self.settings.get("auto_update_enabled", False):
                self._last_auto_sweep = time.time()

    # --------------------------------------------------------------- warmup
    def _boot_gitea(self) -> None:
        """One Gitea ref check per process start, queued behind the first scan.

        Deliberately not a loop. A ref check is one network round trip per
        repository, and the backup mirror only drifts when a push happens, so
        startup plus the explicit **Check Gitea** action covers it.

        The first check can land in the window where a freshly launched app has
        no working DNS or route to the tailnet (packaged runs showed 1-14 ms
        "no route to host" failures and then succeeded minutes later). One
        delayed retry turns that transient into a warning instead of a stale
        "unreachable" that only a click can clear.
        """
        if not self.settings.get("gitea_check_on_start", True):
            return
        if not self.repos.gitea_configured():
            log.info("startup Gitea check skipped: not configured")
            return
        if not self.repos.state().get("root_configured"):
            log.info("startup Gitea check skipped: repo root not configured")
            return
        deadline = time.monotonic() + 30
        while not self._stop.is_set() and time.monotonic() < deadline:
            if self.repos.state().get("generated_at"):
                break
            self._stop.wait(0.2)
        if self._stop.is_set():
            return
        try:
            if not self.repos.check_gitea_async():
                return
            log.info("startup Gitea check queued")
            result = self._wait_for_gitea(timeout=120)
            if result is None or not result.get("errors"):
                return
            log.warning("startup Gitea check had %s error(s); retrying once in 30s", result["errors"])
            if self._stop.wait(30):
                return
            self.repos.check_gitea_async()
        except Exception:
            log.exception("startup Gitea check failed")

    def _wait_for_gitea(self, *, timeout: float) -> dict[str, Any] | None:
        """Block until the async ref check leaves "checking" (or time out)."""
        deadline = time.monotonic() + timeout
        while not self._stop.is_set() and time.monotonic() < deadline:
            meta = self.repos.state().get("gitea") or {}
            if meta.get("state") not in ("checking", "never"):
                return meta
            self._stop.wait(0.5)
        return None

    def warmup(self) -> dict[str, Any]:
        """First-paint readiness for the browser boot screen.

        The console is a long-lived server, so "startup" means process start:
        the fleet probe and the local repo scan begin immediately, and one
        Gitea check queues behind the first scan.
        """
        now = time.time()
        started = self._boot_started or now
        elapsed = max(0.0, now - started)

        fleet = self.monitor.state()
        hosts_total = len(self.hosts.all())
        fleet_done = bool(fleet.get("generated_at"))
        fleet_online = int((fleet.get("summary") or {}).get("hosts_online") or 0) if fleet_done else 0
        tasks: list[dict[str, Any]] = [
            {
                "id": "fleet",
                "label": "Probing the fleet",
                "state": "done" if fleet_done else "running",
                "detail": (
                    f"{fleet_online}/{hosts_total} hosts online"
                    if fleet_done
                    else f"SSH telemetry for {hosts_total} host(s)"
                ),
            }
        ]

        repos = self.repos.state()
        rows = repos.get("repos") or []
        repos_done = bool(repos.get("generated_at"))
        tasks.append(
            {
                "id": "repos",
                "label": "Scanning repositories",
                "state": "done" if repos_done else "running",
                "detail": f"{len(rows)} repositories" if repos_done else "local git status",
            }
        )

        if (
            repos.get("root_configured")
            and self.repos.gitea_configured()
            and self.settings.get("gitea_check_on_start", True)
        ):
            meta = repos.get("gitea") or {}
            phase = meta.get("state") or "never"
            if phase in ("done", "partial", "error"):
                tasks.append(
                    {
                        "id": "gitea",
                        "label": "Checking the Gitea backup",
                        "state": "warn" if phase in ("partial", "error") else "done",
                        "detail": str(meta.get("message") or f"{meta.get('checked', 0)} repositories compared"),
                    }
                )
            else:
                tasks.append(
                    {
                        "id": "gitea",
                        "label": "Checking the Gitea backup",
                        "state": "running" if phase == "checking" else "pending",
                        "detail": (
                            f"reading refs from {self._gitea_host()}"
                            if phase == "checking"
                            else "queued behind the local scan"
                        ),
                    }
                )

        terminal = all(task["state"] in ("done", "warn") for task in tasks)
        ready = terminal or elapsed >= WARMUP_SOFT_DEADLINE_S
        return {
            "state": "ready" if ready else "warming",
            "started_at": started,
            "elapsed_s": round(elapsed, 1),
            "deadline_s": WARMUP_SOFT_DEADLINE_S,
            "tasks": tasks,
        }

    def _gitea_host(self) -> str:
        host = str(self.settings.get("gitea_url") or "")
        for prefix in ("https://", "http://"):
            if host.startswith(prefix):
                host = host[len(prefix) :]
        return host.rstrip("/") or "Gitea"

    def reload_hosts(self) -> None:
        self.hosts = HostStore(self.settings)
        self.monitor.host_store = self.hosts
        self.tasks.host_store = self.hosts
        self.monitor.request_refresh()
