"""Local SQLite store: history, run records, activity and update policies.

Kept beside the repo in the gitignored ``.hlc/`` dir.  Nothing here is
authoritative for desired state — the git checkout is.  This is telemetry and
audit trail only, so losing it costs charts, not containers.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable

from . import util
from .config import Settings

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS host_samples (
    host TEXT NOT NULL,
    ts INTEGER NOT NULL,
    reachable INTEGER NOT NULL DEFAULT 0,
    uptime_s INTEGER,
    temp_c REAL,
    throttled INTEGER NOT NULL DEFAULT 0,
    load1 REAL,
    mem_pct REAL,
    disk_pct REAL,
    containers_up INTEGER NOT NULL DEFAULT 0,
    containers_total INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (host, ts)
);

CREATE TABLE IF NOT EXISTS stack_samples (
    host TEXT NOT NULL,
    stack TEXT NOT NULL,
    ts INTEGER NOT NULL,
    status TEXT NOT NULL,
    running INTEGER NOT NULL DEFAULT 0,
    expected INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (host, stack, ts)
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    host TEXT,
    svc TEXT,
    message TEXT NOT NULL,
    run_id TEXT,
    details TEXT
);

CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    host TEXT,
    svc TEXT,
    status TEXT NOT NULL,
    started INTEGER NOT NULL,
    finished INTEGER,
    log_path TEXT,
    summary TEXT
);

CREATE TABLE IF NOT EXISTS policies (
    host TEXT NOT NULL,
    svc TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'off',
    updated INTEGER NOT NULL,
    last_applied_version TEXT,
    last_applied_ts INTEGER,
    last_attempt_ts INTEGER,
    last_error TEXT,
    PRIMARY KEY (host, svc)
);

CREATE INDEX IF NOT EXISTS idx_host_samples_ts ON host_samples(ts);
CREATE INDEX IF NOT EXISTS idx_stack_samples_ts ON stack_samples(ts);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
"""


class Store:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.path = settings.db_file
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -------------------------------------------------------------- samples
    def record_snapshot(self, hosts: list[dict[str, Any]], stacks: list[dict[str, Any]], ts: int | None = None) -> None:
        ts = ts or util.now_ts()
        with self._lock:
            for host in hosts:
                data = host.get("data") or {}
                disks = data.get("disks") or []
                root_pct = None
                for disk in disks:
                    if disk.get("mountpoint") == "/":
                        try:
                            root_pct = float(str(disk.get("percent", "")).replace("%", ""))
                        except ValueError:
                            root_pct = None
                        break
                memory = data.get("memory") or {}
                load = data.get("load") or []
                containers = data.get("containers") or []
                total = len(containers)
                up = sum(1 for item in containers if item.get("state") == "running")
                throttled = 0
                if (data.get("throttled") or {}).get("now"):
                    throttled = 1
                self._conn.execute(
                    """INSERT OR REPLACE INTO host_samples
                       (host, ts, reachable, uptime_s, temp_c, throttled, load1, mem_pct,
                        disk_pct, containers_up, containers_total)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        host.get("alias"),
                        ts,
                        1 if host.get("online") else 0,
                        int(data.get("uptime_s") or 0),
                        data.get("temperature_c"),
                        throttled,
                        float(load[0]) if load else None,
                        memory.get("used_pct"),
                        root_pct,
                        up,
                        total,
                    ),
                )
            for stack in stacks:
                runtime = stack.get("runtime") or {}
                self._conn.execute(
                    """INSERT OR REPLACE INTO stack_samples
                       (host, stack, ts, status, running, expected)
                       VALUES (?,?,?,?,?,?)""",
                    (
                        stack.get("host"),
                        stack.get("name"),
                        ts,
                        runtime.get("status") or "unknown",
                        int(runtime.get("running") or 0),
                        int(runtime.get("expected") or 0),
                    ),
                )
            self._conn.commit()

    def prune(self) -> None:
        days = int(self.settings.get("history_days", 30) or 30)
        cutoff = util.now_ts() - days * 86400
        with self._lock:
            self._conn.execute("DELETE FROM host_samples WHERE ts < ?", (cutoff,))
            self._conn.execute("DELETE FROM stack_samples WHERE ts < ?", (cutoff,))
            self._conn.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
            self._conn.commit()

    # -------------------------------------------------------------- history
    def host_history(self, host: str, hours: int = 24, max_points: int = 180) -> dict[str, Any]:
        since = util.now_ts() - hours * 3600
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts, reachable, uptime_s, temp_c, throttled, load1, mem_pct, disk_pct, containers_up, containers_total "
                "FROM host_samples WHERE host = ? AND ts >= ? ORDER BY ts",
                (host, since),
            ).fetchall()
        points = [dict(row) for row in rows]
        if len(points) > max_points:
            step = max(1, len(points) // max_points)
            points = points[::step]
        reachable = sum(1 for point in points if point["reachable"])
        return {
            "host": host,
            "hours": hours,
            "points": points,
            "samples": len(rows),
            "uptime_pct": round(100 * reachable / len(points), 2) if points else None,
            "last_ts": points[-1]["ts"] if points else None,
        }

    def stack_history(self, host: str, stack: str, hours: int = 24, max_points: int = 180) -> dict[str, Any]:
        since = util.now_ts() - hours * 3600
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts, status, running, expected FROM stack_samples "
                "WHERE host = ? AND stack = ? AND ts >= ? ORDER BY ts",
                (host, stack, since),
            ).fetchall()
        points = [dict(row) for row in rows]
        if len(points) > max_points:
            step = max(1, len(points) // max_points)
            points = points[::step]
        up = sum(1 for point in points if point["status"] == "up")
        degraded = sum(1 for point in points if point["status"] == "degraded")
        return {
            "host": host,
            "stack": stack,
            "hours": hours,
            "points": points,
            "samples": len(rows),
            "uptime_pct": round(100 * up / len(points), 2) if points else None,
            "degraded_pct": round(100 * degraded / len(points), 2) if points else None,
        }

    def fleet_uptime(self, hours: int = 24) -> dict[str, Any]:
        since = util.now_ts() - hours * 3600
        with self._lock:
            host_rows = self._conn.execute(
                "SELECT host, COUNT(*) AS samples, SUM(reachable) AS up, MAX(ts) AS last_ts "
                "FROM host_samples WHERE ts >= ? GROUP BY host",
                (since,),
            ).fetchall()
            stack_rows = self._conn.execute(
                "SELECT host, stack, COUNT(*) AS samples, "
                "SUM(CASE WHEN status = 'up' THEN 1 ELSE 0 END) AS up, "
                "SUM(CASE WHEN status = 'degraded' THEN 1 ELSE 0 END) AS degraded, MAX(ts) AS last_ts "
                "FROM stack_samples WHERE ts >= ? GROUP BY host, stack",
                (since,),
            ).fetchall()
        return {
            "hours": hours,
            "hosts": [
                {
                    "host": row["host"],
                    "samples": row["samples"],
                    "uptime_pct": round(100 * (row["up"] or 0) / row["samples"], 2) if row["samples"] else None,
                    "last_ts": row["last_ts"],
                }
                for row in host_rows
            ],
            "stacks": [
                {
                    "host": row["host"],
                    "stack": row["stack"],
                    "samples": row["samples"],
                    "uptime_pct": round(100 * (row["up"] or 0) / row["samples"], 2) if row["samples"] else None,
                    "degraded_pct": round(100 * (row["degraded"] or 0) / row["samples"], 2) if row["samples"] else None,
                    "last_ts": row["last_ts"],
                }
                for row in stack_rows
            ],
        }

    # --------------------------------------------------------------- events
    def add_event(
        self,
        kind: str,
        status: str,
        message: str,
        *,
        host: str = "",
        svc: str = "",
        run_id: str = "",
        details: dict[str, Any] | None = None,
    ) -> int:
        with self._lock:
            cursor = self._conn.execute(
                "INSERT INTO events (ts, kind, status, host, svc, message, run_id, details) VALUES (?,?,?,?,?,?,?,?)",
                (
                    util.now_ts(),
                    kind,
                    status,
                    host,
                    svc,
                    message[:2000],
                    run_id,
                    json.dumps(details, default=str)[:8000] if details else None,
                ),
            )
            self._conn.commit()
            return int(cursor.lastrowid)

    def events(self, limit: int = 100, host: str = "", svc: str = "", kind: str = "") -> list[dict[str, Any]]:
        clauses, params = [], []
        if host:
            clauses.append("host = ?")
            params.append(host)
        if svc:
            clauses.append("svc = ?")
            params.append(svc)
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT id, ts, kind, status, host, svc, message, run_id, details FROM events {where} "
                "ORDER BY id DESC LIMIT ?",
                (*params, limit),
            ).fetchall()
        results = []
        for row in rows:
            item = dict(row)
            item["iso"] = util.iso(item["ts"])
            if item.get("details"):
                try:
                    item["details"] = json.loads(item["details"])
                except ValueError:
                    pass
            results.append(item)
        return results

    # ----------------------------------------------------------------- runs
    def start_run(self, run_id: str, kind: str, host: str, svc: str, log_path: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO runs (id, kind, host, svc, status, started, log_path) VALUES (?,?,?,?,?,?,?)",
                (run_id, kind, host, svc, "running", util.now_ts(), log_path),
            )
            self._conn.commit()

    def finish_run(self, run_id: str, status: str, summary: str = "") -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE runs SET status = ?, finished = ?, summary = ? WHERE id = ?",
                (status, util.now_ts(), summary[:2000], run_id),
            )
            self._conn.commit()

    def mark_interrupted_runs(self) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE runs SET status = 'interrupted', finished = ? WHERE status = 'running'",
                (util.now_ts(),),
            )
            self._conn.commit()

    def runs(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, kind, host, svc, status, started, finished, summary FROM runs ORDER BY started DESC LIMIT ?",
                (limit,),
            ).fetchall()
        results = []
        for row in rows:
            item = dict(row)
            item["started_iso"] = util.iso(item["started"])
            item["finished_iso"] = util.iso(item["finished"]) if item["finished"] else ""
            results.append(item)
        return results

    # ------------------------------------------------------------- policies
    def policies(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT host, svc, mode, updated, last_applied_version, last_applied_ts, last_attempt_ts, last_error "
                "FROM policies ORDER BY host, svc"
            ).fetchall()
        return [dict(row) for row in rows]

    def policy(self, host: str, svc: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT host, svc, mode, updated, last_applied_version, last_applied_ts, last_attempt_ts, last_error "
                "FROM policies WHERE host = ? AND svc = ?",
                (host, svc),
            ).fetchone()
        return dict(row) if row else None

    def set_policy(self, host: str, svc: str, mode: str) -> dict[str, Any]:
        if mode not in ("off", "notify", "auto"):
            raise ValueError("mode must be off, notify or auto")
        with self._lock:
            self._conn.execute(
                """INSERT INTO policies (host, svc, mode, updated) VALUES (?,?,?,?)
                   ON CONFLICT(host, svc) DO UPDATE SET mode = excluded.mode, updated = excluded.updated""",
                (host, svc, mode, util.now_ts()),
            )
            self._conn.commit()
        return self.policy(host, svc) or {}

    def mark_policy_attempt(self, host: str, svc: str, version: str, *, error: str = "", applied: bool = False) -> None:
        now = util.now_ts()
        with self._lock:
            self._conn.execute(
                """UPDATE policies SET last_attempt_ts = ?, last_error = ?,
                   last_applied_version = CASE WHEN ? THEN ? ELSE last_applied_version END,
                   last_applied_ts = CASE WHEN ? THEN ? ELSE last_applied_ts END
                   WHERE host = ? AND svc = ?""",
                (now, error[:500], 1 if applied else 0, version, 1 if applied else 0, now, host, svc),
            )
            self._conn.commit()

    def stats(self) -> dict[str, Any]:
        with self._lock:
            host_count = self._conn.execute("SELECT COUNT(*) FROM host_samples").fetchone()[0]
            stack_count = self._conn.execute("SELECT COUNT(*) FROM stack_samples").fetchone()[0]
            event_count = self._conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            run_count = self._conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        try:
            size = self.path.stat().st_size
        except OSError:
            size = 0
        return {
            "host_samples": host_count,
            "stack_samples": stack_count,
            "events": event_count,
            "runs": run_count,
            "db_bytes": size,
        }

    def clear_history(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM host_samples")
            self._conn.execute("DELETE FROM stack_samples")
            self._conn.commit()
