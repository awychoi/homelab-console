"""Background runs: deploy, lifecycle, update, rollback.

Runs are streamed to the browser over SSE and persisted under ``.hlc/runs``.
A run is deliberately *not* the place where gates are decided: the HTTP layer
refuses destructive deploys without a fresh diff hash, and this layer checks
again before writing anything.
"""

from __future__ import annotations

import json
import logging
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

from . import stacks, updates, util
from .config import Settings
from .hosts import HostStore
from .monitor import FleetMonitor
from .store import Store

log = logging.getLogger("hlc.runs")


class RunError(RuntimeError):
    pass


@dataclass
class Run:
    id: str
    kind: str
    host: str
    svc: str
    title: str
    status: str = "running"
    started: int = field(default_factory=util.now_ts)
    finished: int = 0
    error: str = ""
    result: dict[str, Any] = field(default_factory=dict)
    log_path: Path | None = None
    lines: list[dict[str, Any]] = field(default_factory=list)
    cond: threading.Condition = field(default_factory=threading.Condition)
    runner: Callable[["Run"], dict[str, Any]] | None = None

    def emit(self, text: str, level: str = "info") -> None:
        text = str(text)
        for chunk in text.splitlines() or [""]:
            entry = {"i": len(self.lines), "ts": util.now_ts(), "text": chunk, "level": level}
            with self.cond:
                self.lines.append(entry)
                if len(self.lines) > 5000:
                    del self.lines[:1000]
                    for index, item in enumerate(self.lines):
                        item["i"] = index
                self.cond.notify_all()
            if self.log_path:
                try:
                    with self.log_path.open("a", encoding="utf-8") as handle:
                        handle.write(f"{util.iso(entry['ts'])} {chunk}\n")
                except OSError:
                    pass

    def finish(self, status: str, error: str = "", result: dict[str, Any] | None = None) -> None:
        with self.cond:
            self.status = status
            self.finished = util.now_ts()
            self.error = error
            self.result = result or {}
            self.cond.notify_all()

    def public(self, since: int = 0) -> dict[str, Any]:
        with self.cond:
            lines = self.lines[since:]
            next_index = self.lines[-1]["i"] + 1 if self.lines else 0
            return {
                "id": self.id,
                "kind": self.kind,
                "host": self.host,
                "svc": self.svc,
                "title": self.title,
                "status": self.status,
                "started": self.started,
                "started_iso": util.iso(self.started),
                "finished": self.finished,
                "finished_iso": util.iso(self.finished) if self.finished else "",
                "error": self.error,
                "result": self.result,
                "lines": lines,
                "next": next_index,
                "next_index": next_index,
            }

    def stream(self, since: int = 0) -> Iterator[dict[str, Any]]:
        index = max(0, since)
        while True:
            with self.cond:
                if index >= len(self.lines) and self.status == "running":
                    self.cond.wait(timeout=15)
                lines = self.lines[index:]
                index = len(self.lines)
                status = self.status
                payload = {
                    "status": status,
                    "error": self.error,
                    "result": self.result,
                    "next": index,
                    "lines": lines,
                }
            if lines:
                yield {"event": "lines", "data": payload}
            elif status != "running":
                yield {"event": "done", "data": payload}
                return
            else:
                yield {"event": "ping", "data": {"status": status, "next": index}}
            if status != "running":
                yield {"event": "done", "data": payload}
                return


@dataclass
class BatchItem:
    host: str
    stack: str
    service: str
    current: str
    latest: str
    status: str = "queued"
    run_id: str = ""
    error: str = ""

    def public(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "stack": self.stack,
            "service": self.service,
            "current": self.current,
            "latest": self.latest,
            "status": self.status,
            "run_id": self.run_id,
            "error": self.error,
        }


@dataclass
class Batch:
    id: str
    scope: str
    host: str
    items: list[BatchItem]
    excluded: list[dict[str, Any]] = field(default_factory=list)
    status: str = "queued"
    created: int = field(default_factory=util.now_ts)
    started: int = 0
    finished: int = 0
    error: str = ""

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "scope": self.scope,
            "host": self.host,
            "status": self.status,
            "created": self.created,
            "created_iso": util.iso(self.created),
            "started": self.started,
            "finished": self.finished,
            "finished_iso": util.iso(self.finished) if self.finished else "",
            "error": self.error,
            "excluded": self.excluded,
            "items": [item.public() for item in self.items],
            "completed": sum(1 for item in self.items if item.status in ("ok", "failed", "skipped")),
            "total": len(self.items),
        }


class TaskManager:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        host_store: HostStore,
        monitor: FleetMonitor,
    ):
        self.settings = settings
        self.store = store
        self.host_store = host_store
        self.monitor = monitor
        self.runs: dict[str, Run] = {}
        self.batches: dict[str, Batch] = {}
        self._lock = threading.RLock()

    # ---------------------------------------------------------------- api
    def get(self, run_id: str) -> Run | None:
        with self._lock:
            return self.runs.get(run_id)

    def list(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            runs = sorted(self.runs.values(), key=lambda run: run.started, reverse=True)[:limit]
            return [run.public(since=10**9) for run in runs]

    def active_for(self, host: str = "", svc: str = "") -> Run | None:
        with self._lock:
            for run in self.runs.values():
                if run.status != "running":
                    continue
                if host and run.host and run.host != host:
                    continue
                if svc and run.svc and run.svc != svc:
                    continue
                if run.kind == "deploy":
                    if host and run.host != host:
                        continue
                    if svc and run.svc != svc:
                        continue
                return run
        return None

    def active_count(self) -> int:
        with self._lock:
            return sum(1 for run in self.runs.values() if run.status == "running")

    def get_batch(self, batch_id: str) -> Batch | None:
        with self._lock:
            return self.batches.get(batch_id)

    def batch_active(self) -> bool:
        with self._lock:
            return any(batch.status in ("queued", "running") for batch in self.batches.values())

    def start(self, kind: str, fn: Callable[[Run], dict[str, Any]], *, host: str = "", svc: str = "", title: str = "") -> Run:
        run_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}"
        log_path = self.settings.runs_dir / f"{run_id}.log"
        util.ensure_dir(log_path.parent, 0o700)
        log_path.touch(exist_ok=True)
        try:
            log_path.chmod(0o600)
        except OSError:
            pass
        run = Run(id=run_id, kind=kind, host=host, svc=svc, title=title or kind, log_path=log_path)
        with self._lock:
            self.runs[run_id] = run
        self.store.start_run(run_id, kind, host, svc, str(log_path))
        thread = threading.Thread(target=self._execute, args=(run, fn), name=f"hlc-run-{run_id}", daemon=True)
        thread.start()
        return run

    def _execute(self, run: Run, fn: Callable[[Run], dict[str, Any]]) -> None:
        try:
            result = fn(run)
            run.finish("ok", result=result or {})
            self.store.finish_run(run.id, "ok", json.dumps(result or {})[:1800])
        except updates.UpdateError as exc:
            run.emit(f"✗ {exc}", "error")
            if getattr(exc, "rolled_back", False):
                run.emit("   compose files were rolled back", "warning")
            run.finish("failed", error=str(exc), result=getattr(exc, "details", {}) or {})
            self.store.finish_run(run.id, "failed", str(exc))
        except Exception as exc:
            # Repo queue failures are expected partial results, not bugs; keep
            # the queue payload available to the UI while still failing the run.
            result_payload = getattr(exc, "run_result", None)
            if result_payload is None:
                log.exception("run %s failed", run.id)
                run.emit(f"✗ unexpected failure: {exc}", "error")
            else:
                run.emit(f"✗ {exc}", "error")
            run.finish("failed", error=str(exc), result=result_payload or {})
            self.store.finish_run(run.id, "failed", str(exc))
        finally:
            self.monitor.request_refresh()

    # ------------------------------------------------------------- deploy
    def deploy(
        self,
        host_alias: str,
        stack_name: str,
        action: str,
        plan_hash: str,
        approve: bool,
        *,
        dry_run: bool = False,
    ) -> Run:
        if action not in ("up", "pull", "sync"):
            raise RunError("deploy action must be up, pull or sync")
        if not str(plan_hash or "").strip():
            raise RunError("a reviewed plan hash is required before deploying")
        active = self.active_for(host_alias, stack_name)
        if active:
            raise RunError(f"a run is already active for {host_alias}/{stack_name}: {active.id}")
        host = self.host_store.get(host_alias)
        if host.local:
            raise RunError("local deploys are not supported by the console")
        stack = stacks.get_stack(self.settings, host_alias, stack_name)

        def work(run: Run) -> dict[str, Any]:
            run.emit(f">> deploy {host_alias}/{stack_name} · {action}")
            plan = stacks.compute_plan(host, stack, self.settings.remote_root)
            if plan_hash and plan["hash"] != plan_hash:
                raise stacks.StackError("the diff changed since it was reviewed — review again")
            changes = plan["changes"]
            if changes:
                for change in changes:
                    run.emit(f"   {change.kind}: {change.path}")
            else:
                run.emit("   repo is already in sync with the host")
            if plan["destructive"] and not approve:
                raise stacks.StackError("destructive changes require explicit approval")
            if not changes and action != "down":
                run.emit("   nothing to push")
            run.emit(">> validating compose on the host (staging dir)")
            validation = stacks.validate_remote(host, stack.files, self.settings.remote_root)
            if not validation.get("ok"):
                raise stacks.StackError(f"compose validation failed: {validation.get('error')}")
            run.emit(f"   valid services: {', '.join(validation.get('services') or [])}")
            if dry_run:
                run.emit("   dry-run: stopping before any write")
                return {"dry_run": True, "changes": [change.path for change in changes]}

            if action in ("up", "pull", "sync"):
                run.emit(">> pushing files")
                push = stacks.push_files(host, stack_name, stack.files, self.settings.remote_root, base=stack.path)
                if push.get("repaired"):
                    run.emit("   repaired remote file ownership (a container had chowned the stack directory)")
                if not push.get("ok"):
                    raise stacks.StackError(f"push failed: {push.get('error')}")
                for rel in sorted(stack.files):
                    run.emit(f"   → {rel}")
            if action == "sync":
                run.emit("✓ files pushed (no compose action)")
                self.store.add_event("deploy", "success", f"{host_alias}/{stack_name}: files synced", host=host_alias, svc=stack_name)
                return {"pushed": len(stack.files), "action": action}

            if action == "pull":
                run.emit(">> docker compose pull")
                pulled = stacks.compose(host, stack_name, "pull", self.settings.remote_root, timeout=600)
                _emit_result(run, pulled)
                if pulled.returncode != 0:
                    raise stacks.StackError(f"docker compose pull failed (rc={pulled.returncode})")
            else:
                run.emit(">> docker compose up -d --remove-orphans")
                upped = stacks.compose(host, stack_name, "up", self.settings.remote_root, timeout=300)
                _emit_result(run, upped)
                if upped.returncode != 0:
                    raise stacks.StackError(f"docker compose up failed (rc={upped.returncode})")

            run.emit(">> waiting for health")
            health = updates.wait_healthy(
                host, stack, self.settings.remote_root, run.emit,
                timeout=int(self.settings.get("health_timeout_s", 120)),
            )
            if not health.get("ok"):
                self.store.add_event(
                    "deploy", "warning",
                    f"{host_alias}/{stack_name}: deployed but health is not green ({updates.describe_health(health)})",
                    host=host_alias, svc=stack_name,
                )
                raise stacks.StackError(
                    f"deploy completed but health is not green: {updates.describe_health(health)}"
                )
            run.emit("✓ deploy complete")
            self.store.add_event(
                "deploy", "success", f"{host_alias}/{stack_name}: {action} deployed", host=host_alias, svc=stack_name
            )
            return {"pushed": len(stack.files), "action": action, "health": health}

        return self.start("deploy", work, host=host_alias, svc=stack_name, title=f"deploy {host_alias}/{stack_name}")

    # ----------------------------------------------------------- lifecycle
    def lifecycle(self, host_alias: str, stack_name: str, action: str) -> Run:
        if action not in ("restart", "down", "up"):
            raise RunError("lifecycle action must be restart, down or up")
        active = self.active_for(host_alias, stack_name)
        if active:
            raise RunError(f"a run is already active for {host_alias}/{stack_name}: {active.id}")
        host = self.host_store.get(host_alias)
        stack = stacks.get_stack(self.settings, host_alias, stack_name)

        def work(run: Run) -> dict[str, Any]:
            run.emit(f">> {action} {host_alias}/{stack_name} (no file push)")
            run.emit(f"   $ docker compose {action}")
            result = stacks.compose(host, stack_name, action, self.settings.remote_root, timeout=180)
            _emit_result(run, result)
            if result.returncode != 0:
                raise stacks.StackError(f"docker compose {action} failed (rc={result.returncode})")
            if action in ("restart", "up"):
                health = updates.wait_healthy(
                    host, stack, self.settings.remote_root, run.emit,
                    timeout=int(self.settings.get("health_timeout_s", 120)),
                )
                if not health.get("ok"):
                    raise stacks.StackError(f"containers not healthy after {action}: {updates.describe_health(health)}")
            run.emit(f"✓ {action} complete")
            self.store.add_event(
                "lifecycle", "success", f"{host_alias}/{stack_name}: {action}", host=host_alias, svc=stack_name
            )
            return {"action": action}

        return self.start("lifecycle", work, host=host_alias, svc=stack_name, title=f"{action} {host_alias}/{stack_name}")

    # -------------------------------------------------------------- update
    def update(
        self,
        host_alias: str,
        stack_name: str,
        service: str,
        action: str,
        *,
        acknowledge: bool = False,
        automatic: bool = False,
        review_hash: str = "",
        _bulk: bool = False,
    ) -> Run:
        if not _bulk and self.batch_active():
            raise RunError("a bulk update queue is active; wait for it to finish")
        active = self.active_for(host_alias, stack_name)
        if active:
            raise RunError(f"a run is already active for {host_alias}/{stack_name}: {active.id}")
        host = self.host_store.get(host_alias)
        stack = stacks.get_stack(self.settings, host_alias, stack_name)

        def work(run: Run) -> dict[str, Any]:
            state = self.monitor.state()
            host_state = next((item for item in state.get("hosts", []) if item["alias"] == host_alias), None)
            data = (host_state or {}).get("data") or {}
            remote_hashes = (data.get("stack_files") or {}).get(stack_name)
            runtime = stacks.stack_runtime(stack, data.get("containers") or [], remote_hashes)
            targets = updates.build_targets(
                stack, runtime, self.monitor.releases, host_arch=str(data.get("arch") or "")
            )
            target = next((item for item in targets if item["service"] == service), None)
            if target is None:
                raise updates.UpdateError(f"no update target found for {stack_name}/{service}")
            if review_hash and updates.review_hash(target, action) != review_hash:
                raise updates.UpdateError(
                    "the update target changed since it was reviewed — refresh the updates view"
                )
            if automatic:
                if target.get("safety", {}).get("level") == "critical":
                    raise updates.UpdateError("critical stacks are never auto-updated")
                if runtime.get("status") != "up":
                    raise updates.UpdateError(f"stack is not healthy ({runtime.get('status')}); auto-update skipped")
                run.emit("   automatic policy triggered this update")
            result = updates.perform_update(
                self.settings, self.store, host, stack, target,
                action=action, log=run.emit, acknowledge=acknowledge,
            )
            policy = self.store.policy(host_alias, stack_name)
            if policy:
                self.store.mark_policy_attempt(
                    host_alias, stack_name, str(target.get("latest") or ""), applied=True
                )
            return result

        run = self.start(
            "update", work, host=host_alias, svc=stack_name,
            title=f"update {host_alias}/{stack_name}",
        )
        if self.store.policy(host_alias, stack_name):
            self.store.mark_policy_attempt(host_alias, stack_name, "", applied=False)
        return run

    # --------------------------------------------------------- bulk updates
    def start_bulk(self, scope: str, host_alias: str = "") -> Batch:
        """Queue eligible updates sequentially, stopping at the first failure."""
        if scope not in ("fleet", "host"):
            raise RunError("bulk scope must be fleet or host")
        if self.batch_active():
            raise RunError("a bulk update queue is already active")
        if self.active_count():
            raise RunError("a run is already active; wait for it to finish before queueing")
        state = self.monitor.state()
        if state.get("warming_up"):
            raise RunError("fleet state is still warming up")
        if scope == "host":
            if not host_alias:
                raise RunError("host is required for a host-scoped queue")
            target_host = self.host_store.get(host_alias)
            if target_host.local:
                raise RunError("local stacks cannot be updated this way")

        host_rows = {
            str(host.get("alias")): host
            for host in (state.get("hosts") or [])
        }
        host_order = {
            str(host.get("alias")): index
            for index, host in enumerate(state.get("hosts") or [])
        }
        eligible: list[dict[str, Any]] = []
        excluded: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str]] = set()
        for target in state.get("updates") or []:
            if "update" not in (target.get("actions") or []):
                continue
            if scope == "host" and target.get("host") != host_alias:
                continue
            key = (str(target.get("host")), str(target.get("stack")), str(target.get("service")))
            if key in seen:
                continue
            seen.add(key)
            host_name = str(target.get("host"))
            if (target.get("safety") or {}).get("level") == "critical":
                excluded.append({
                    "host": host_name,
                    "stack": target.get("stack"),
                    "service": target.get("service"),
                    "latest": target.get("latest"),
                    "reason": "critical stack — apply individually after a database dump",
                })
                continue
            if host_rows.get(host_name, {}).get("online") is False:
                excluded.append({
                    "host": host_name,
                    "stack": target.get("stack"),
                    "service": target.get("service"),
                    "latest": target.get("latest"),
                    "reason": "host is offline",
                })
                continue
            try:
                host = self.host_store.get(host_name)
            except Exception:
                continue
            if host.local:
                continue
            eligible.append(target)
        if not eligible:
            if excluded:
                reasons = sorted({str(item.get("reason") or "not eligible") for item in excluded})
                raise RunError(f"no eligible updates — {'; '.join(reasons)}")
            raise RunError("no updates are available in this scope")

        eligible.sort(key=lambda item: (
            host_order.get(str(item.get("host")), 99),
            str(item.get("stack")),
            str(item.get("service")),
        ))
        items = [
            BatchItem(
                host=str(target.get("host")),
                stack=str(target.get("stack")),
                service=str(target.get("service")),
                current=str(target.get("current_display") or target.get("current") or ""),
                latest=str(target.get("latest") or ""),
            )
            for target in eligible
        ]
        batch = Batch(
            id=f"bulk-{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}",
            scope=scope,
            host=host_alias,
            items=items,
            excluded=excluded,
        )
        with self._lock:
            self.batches[batch.id] = batch
        self.store.add_event(
            "bulk-update", "info",
            f"queued {len(items)} update(s) · {scope}" + (f" · {host_alias}" if scope == "host" else ""),
            host=host_alias if scope == "host" else "",
        )
        thread = threading.Thread(
            target=self._run_batch, args=(batch,), name=f"hlc-{batch.id}", daemon=True
        )
        thread.start()
        return batch

    def _run_batch(self, batch: Batch) -> None:
        with self._lock:
            batch.status = "running"
            batch.started = util.now_ts()
        for item in batch.items:
            if batch.error:
                item.status = "skipped"
                continue
            with self._lock:
                item.status = "running"
            try:
                run = self.update(
                    item.host, item.stack, item.service, "update", _bulk=True
                )
            except Exception as exc:
                item.status = "failed"
                item.error = str(exc)
                batch.status = "failed"
                batch.error = f"{item.host}/{item.stack}: {exc}"
                break
            item.run_id = run.id
            deadline = time.time() + 3600
            with run.cond:
                while run.status == "running" and time.time() < deadline:
                    run.cond.wait(timeout=5)
            if run.status != "ok":
                item.status = "failed"
                item.error = run.error or "update did not finish successfully"
                batch.status = "failed"
                batch.error = f"{item.host}/{item.stack}: {item.error}"
                break
            item.status = "ok"
        for item in batch.items:
            if item.status == "queued":
                item.status = "skipped"
        with self._lock:
            batch.status = "ok" if all(item.status == "ok" for item in batch.items) else "failed"
            batch.finished = util.now_ts()
        if batch.status == "ok":
            self.store.add_event(
                "bulk-update", "success",
                f"{len(batch.items)} update(s) completed sequentially",
                host=batch.host if batch.scope == "host" else "",
            )
        else:
            self.store.add_event(
                "bulk-update", "error", batch.error or "bulk update failed",
                host=batch.host if batch.scope == "host" else "",
            )
        self.monitor.request_refresh()

    def rollback(self, host_alias: str, stack_name: str, snapshot_id: str) -> Run:
        active = self.active_for(host_alias, stack_name)
        if active:
            raise RunError(f"a run is already active for {host_alias}/{stack_name}: {active.id}")
        host = self.host_store.get(host_alias)
        stack = stacks.get_stack(self.settings, host_alias, stack_name)

        def work(run: Run) -> dict[str, Any]:
            return updates.rollback_to_snapshot(
                self.settings, self.store, host, stack, snapshot_id, run.emit
            )

        return self.start("rollback", work, host=host_alias, svc=stack_name, title=f"rollback {host_alias}/{stack_name}")

    # -------------------------------------------------------- auto updates
    def auto_sweep(self) -> str | None:
        """Start at most one auto-update; called by the app service loop."""
        if not self.settings.get("auto_update_enabled", False):
            return None
        if self.batch_active() or self.active_count():
            return None
        state = self.monitor.state()
        if state.get("warming_up"):
            return None
        for policy in self.store.policies():
            if policy.get("mode") != "auto" or policy.get("host") == "":
                continue
            host_alias, stack_name = policy["host"], policy["svc"]
            host = next((item for item in state.get("hosts", []) if item["alias"] == host_alias), None)
            if not host or not host.get("online"):
                continue
            stack_state = next(
                (item for item in state.get("stacks", []) if item["host"] == host_alias and item["name"] == stack_name),
                None,
            )
            if not stack_state or stack_state["runtime"].get("status") != "up":
                continue
            if stack_state["runtime"].get("sync", {}).get("state") == "drift":
                continue
            target = next((item for item in stack_state.get("updates") or [] if item.get("actions")), None)
            if target is None:
                continue
            if target["safety"]["level"] == "critical":
                continue
            if policy.get("last_attempt_ts") and util.now_ts() - policy["last_attempt_ts"] < 6 * 3600:
                continue
            if policy.get("last_applied_version") == target.get("latest"):
                continue
            try:
                run = self.update(
                    host_alias, stack_name, target["service"],
                    "update", acknowledge=False, automatic=True,
                )
            except Exception as exc:
                log.warning("auto-update failed to start for %s/%s: %s", host_alias, stack_name, exc)
                continue
            self.store.mark_policy_attempt(host_alias, stack_name, str(target.get("latest") or ""), applied=False)
            self.store.add_event(
                "update", "info",
                f"auto-update started: {host_alias}/{stack_name} → {target.get('latest')}",
                host=host_alias, svc=stack_name, run_id=run.id,
            )
            return run.id
        return None


def _emit_result(run: Run, result) -> None:
    output = ((result.stdout or "") + (result.stderr or "")).strip()
    seen: set[str] = set()
    for line in output.splitlines():
        text = line.strip()
        if text and text not in seen:
            seen.add(text)
            run.emit("   " + text[:600])
