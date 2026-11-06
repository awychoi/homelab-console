"""Local HTTP API + static SPA host for the Homelab Console.

Binds loopback only.  Every API call needs the per-process token that is
injected into index.html; mutating calls also reject cross-origin browser
requests.  This is a local operational tool, not a multi-user service.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import logging
import mimetypes
import os
import re
import shlex
import shutil
import socket
import sys
import time
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Any

from . import __version__, appbackup, backups, hosts as hosts_mod, profiles, releases, repos, sshkeys, stacks, updates, util
from .app_service import AppService
from .config import APP_NAME, VERSION
from .hosts import HostError
from .runner import RunError
from .sshkeys import KeyError_
from .templates import TEMPLATES

log = logging.getLogger("hlc.http")

MAX_BODY = 2 * 1024 * 1024
# In a checkout this is app/web; inside the macOS bundle it is <bundle>/web.
WEB_DIR = util.bundle_root() / "web"

MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".json": "application/json; charset=utf-8",
    ".woff2": "font/woff2",
    ".txt": "text/plain; charset=utf-8",
}

STACK_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
HOST_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")


class ApiError(Exception):
    def __init__(self, status: int, message: str, **extra: Any):
        super().__init__(message)
        self.status = status
        self.extra = extra


class ConsoleHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], app: AppService, token: str):
        self.app = app
        self.token = token
        self.bind_host = str(address[0])
        self.started_at = util.now_ts()
        super().__init__(address, ConsoleHandler)
        self.allowed_hosts = self._allowed_hosts()

    def _allowed_hosts(self) -> set[str]:
        """Host headers that may receive the SPA/token.

        Binding loopback is not enough: a DNS-rebinding page reaches 127.0.0.1
        with its own Host.  Only names that actually mean this console are
        allowed.  HLC_ALLOWED_HOSTS is an explicit opt-in for a reverse proxy
        or a deliberate non-loopback bind; values may include the port.
        """
        port = self.server_port
        names = {"127.0.0.1", "localhost", "::1"}
        if self.bind_host and self.bind_host not in ("0.0.0.0", "::"):
            names.add(self.bind_host)
        for item in (os.environ.get("HLC_ALLOWED_HOSTS") or "").split(","):
            value = item.strip().lower()
            if value:
                names.add(value)
        allowed: set[str] = set()
        for name in names:
            name = name.lower()
            allowed.add(name)
            allowed.add(f"{name}:{port}")
            if ":" in name and not name.startswith("["):
                allowed.add(f"[{name}]:{port}")
                allowed.add(f"[{name}]")
        return allowed

    def stream_token(self, run_id: str, ttl: int = 300) -> str:
        """A short-lived token that only opens one run's SSE stream."""
        expires = int(time.time()) + max(1, ttl)
        message = f"stream:{run_id}:{expires}".encode("utf-8")
        signature = hmac.new(self.token.encode("utf-8"), message, hashlib.sha256).hexdigest()
        return f"{expires}.{signature}"

    def valid_stream_token(self, run_id: str, token: str) -> bool:
        try:
            expires_text, signature = (token or "").split(".", 1)
            expires = int(expires_text)
        except (ValueError, AttributeError):
            return False
        if expires < int(time.time()):
            return False
        message = f"stream:{run_id}:{expires}".encode("utf-8")
        expected = hmac.new(self.token.encode("utf-8"), message, hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature, expected)

    def server_bind(self) -> None:
        # Allow fast restarts while old TIME_WAIT sockets drain.
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        super().server_bind()


class ConsoleHandler(BaseHTTPRequestHandler):
    server: ConsoleHTTPServer
    server_version = f"HomelabConsole/{VERSION}"
    protocol_version = "HTTP/1.1"

    # ------------------------------------------------------------- routing
    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PUT(self) -> None:
        self._dispatch("PUT")

    def do_PATCH(self) -> None:
        self._dispatch("PATCH")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def log_message(self, fmt: str, *args: Any) -> None:
        message = fmt % args
        if self.server.token:
            message = message.replace(self.server.token, "<redacted>")
        message = re.sub(r"(stream_token=)[^&\s]+", r"\1<redacted>", message)
        log.debug("%s - %s", self.address_string(), message)

    def _dispatch(self, method: str) -> None:
        try:
            # A handler instance serves every request on a keep-alive socket.
            # Reset the cached body so request N+1 never reuses request N's
            # payload (that produced the "{}GET" 501 after repeated POSTs).
            self._raw_body_cache = None
            self._check_host()
            # HTTP/1.1 keep-alive: always consume the request body, even when
            # the endpoint does not need it. Otherwise a body like "{}" from a
            # no-payload POST becomes the prefix of the next request line.
            if method not in ("GET", "HEAD", "OPTIONS"):
                self._read_raw_body()
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            query = urllib.parse.parse_qs(parsed.query)
            if path.startswith("/api/"):
                self._api(method, path, query)
            elif method == "GET":
                self._static(path)
            else:
                raise ApiError(404, "not found")
        except ApiError as exc:
            self._send_json(exc.status, {"error": str(exc), **exc.extra})
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except HostError as exc:
            self._send_json(400, {"error": str(exc)})
        except (stacks.StackError, updates.UpdateError, repos.RepoError) as exc:
            self._send_json(400, {"error": str(exc)})
        except KeyError_ as exc:
            self._send_json(400, {"error": str(exc)})
        except Exception as exc:
            safe_path = self.path.replace(self.server.token, "<redacted>") if self.server.token else self.path
            safe_path = re.sub(r"(stream_token=)[^&\s]+", r"\1<redacted>", safe_path)
            log.exception("unhandled request error: %s %s", method, safe_path)
            self._send_json(500, {"error": "internal error"})

    # --------------------------------------------------------------- auth
    def _check_host(self) -> None:
        host = (self.headers.get("Host") or "").strip().lower()
        if not host or host not in self.server.allowed_hosts:
            self.close_connection = True
            raise ApiError(421, "unexpected Host header")

    def _token(self) -> str:
        return self.headers.get("X-HLC-Token") or ""

    def _stream_run_id(self) -> str:
        path = urllib.parse.urlparse(self.path).path
        parts = [urllib.parse.unquote(part) for part in path.strip("/").split("/")]
        if len(parts) >= 4 and parts[1] == "runs" and parts[3] == "stream":
            return parts[2]
        return ""

    def _require_token(self) -> None:
        if not self.server.app:  # pragma: no cover - defensive
            raise ApiError(503, "service not ready")
        if not self.server.token:
            return
        header = self._token()
        if header and hmac.compare_digest(header, self.server.token):
            return
        run_id = self._stream_run_id()
        if run_id:
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            candidate = (query.get("stream_token") or [""])[0]
            if candidate and self.server.valid_stream_token(run_id, candidate):
                return
        raise ApiError(401, "missing or invalid session token")

    def _check_origin(self) -> None:
        if self.command in ("GET", "HEAD", "OPTIONS"):
            return
        origin = self.headers.get("Origin")
        if not origin:
            return  # non-browser client (curl, tests)
        expected_host = self.headers.get("Host") or ""
        allowed = {f"http://{expected_host}", f"https://{expected_host}"}
        if origin.rstrip("/") not in allowed:
            raise ApiError(403, "cross-origin request rejected")

    def _read_raw_body(self) -> bytes:
        """Read the body exactly once; cached for the rest of the request."""
        cached = getattr(self, "_raw_body_cache", None)
        if cached is not None:
            return cached
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self.close_connection = True
            raise ApiError(400, "invalid content length")
        if length > MAX_BODY:
            self.close_connection = True
            raise ApiError(413, "request body too large")
        raw = self.rfile.read(length) if length > 0 else b""
        self._raw_body_cache = raw
        return raw

    def _body(self) -> dict[str, Any]:
        raw = self._read_raw_body()
        if not raw:
            return {}
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ApiError(400, "body must be JSON")
        if not isinstance(payload, dict):
            raise ApiError(400, "body must be a JSON object")
        return payload

    # ------------------------------------------------------------- helpers
    @property
    def app(self) -> AppService:
        return self.server.app

    def _send_json(self, status: int, payload: Any) -> None:
        data = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _require_stack(self, host: str, name: str) -> tuple[Any, Any]:
        if not HOST_RE.match(host or ""):
            raise ApiError(400, "invalid host alias")
        if not STACK_NAME_RE.match(name or ""):
            raise ApiError(400, "invalid stack name")
        host_obj = self.app.hosts.get(host)
        stack_obj = stacks.get_stack(self.app.settings, host, name)
        return host_obj, stack_obj

    def _state(self) -> dict[str, Any]:
        state = self.app.monitor.state()
        state["git"] = stacks.git_status(self.app.settings)
        state["policies"] = self.app.store.policies()
        state["runs"] = self.app.tasks.list(10)
        state["activity"] = self.app.store.events(30)
        state["release_check"] = self.app.monitor.release_status()
        state["auto_update_enabled"] = bool(self.app.settings.get("auto_update_enabled", False))
        state["repos"] = self.app.repos.state()
        state["warmup"] = self.app.warmup()
        return state

    def _stack_row(self, host: str, name: str) -> dict[str, Any] | None:
        state = self.app.monitor.state()
        return next(
            (row for row in state.get("stacks", []) if row["host"] == host and row["name"] == name),
            None,
        )

    def _target(self, host: str, name: str, service: str) -> dict[str, Any] | None:
        # Rebuild from the current workspace/profile instead of trusting the
        # last cached state row: a profile or release-source save must apply to
        # the very next update click, not after the next poll.
        stack_obj = stacks.get_stack(self.app.settings, host, name)
        state = self.app.monitor.state()
        host_state = next((item for item in state.get("hosts", []) if item["alias"] == host), None)
        data = (host_state or {}).get("data") or {}
        runtime = stacks.stack_runtime(
            stack_obj,
            data.get("containers") or [],
            (data.get("stack_files") or {}).get(name),
        )
        rows = updates.build_targets(
            stack_obj, runtime, self.app.monitor.releases, host_arch=str(data.get("arch") or "")
        )
        return next((item for item in rows if item["service"] == service), None)

    # --------------------------------------------------------------- API
    def _api(self, method: str, path: str, query: dict[str, list[str]]) -> None:
        if path == "/api/health":
            if method != "GET":
                raise ApiError(405, "method not allowed")
            self._send_json(
                200,
                {"ok": True, "name": APP_NAME, "version": VERSION, "uptime_s": util.now_ts() - self.server.started_at},
            )
            return
        self._require_token()
        self._check_origin()
        parts = [urllib.parse.unquote(part) for part in path.strip("/").split("/")]
        # parts[0] == "api"
        rest = parts[1:]
        if not rest:
            raise ApiError(404, "not found")

        if method == "GET" and rest == ["bootstrap"]:
            return self._bootstrap()
        if method == "GET" and rest == ["state"]:
            return self._send_json(200, self._state())
        if method == "POST" and rest == ["refresh"]:
            self.app.monitor.request_refresh()
            return self._send_json(200, {"ok": True})
        if rest and rest[0] == "runs" and len(rest) >= 2:
            return self._runs(method, rest, query)
        if rest and rest[0] == "hosts":
            return self._hosts(method, rest[1:])
        if rest and rest[0] == "ssh":
            return self._ssh(method, rest[1:])
        if rest and rest[0] == "stacks":
            return self._stacks(method, rest[1:], query)
        if rest and rest[0] == "updates":
            return self._updates(method, rest[1:])
        if rest and rest[0] == "policies":
            return self._policies(method)
        if rest and rest[0] == "activity":
            return self._activity(method, query)
        if rest and rest[0] == "history":
            return self._history(method, rest[1:], query)
        if rest and rest[0] == "git":
            return self._git(method, rest[1:])
        if rest and rest[0] == "repos":
            return self._repos(method, rest[1:])
        if rest and rest[0] == "backup":
            return self._backup(method, rest[1:])
        if rest and rest[0] == "release-sources":
            return self._release_sources(method)
        if rest and rest[0] == "settings":
            return self._settings(method)
        if rest and rest[0] == "system":
            if method != "GET":
                raise ApiError(405, "method not allowed")
            return self._system()
        if rest and rest[0] == "templates" and method == "GET":
            return self._send_json(200, {"templates": TEMPLATES})
        if rest and rest[0] == "maintenance":
            return self._maintenance(method, rest[1:])
        raise ApiError(404, "unknown endpoint")

    # ---------------------------------------------------------- bootstrap
    def _bootstrap(self) -> None:
        settings = self.app.settings
        host_rows = []
        for host in self.app.hosts.all():
            key_info = None
            if host.key:
                try:
                    key_info = sshkeys.key_info(Path(host.key).expanduser()).public_dict()
                except Exception:
                    key_info = {"error": f"cannot read key: {host.key}"}
            host_rows.append(
                {
                    **host.public(),
                    "ssh_config": hosts_mod.ssh_config_entry(host.alias),
                    "key_info": key_info,
                }
            )
        git = stacks.git_status(settings)
        payload = {
            "app": {
                "name": APP_NAME,
                "version": VERSION,
                "package_version": __version__,
                "repo": str(settings.repo),
                "state_dir": str(settings.state_dir),
                "remote_root": settings.remote_root,
                "started": self.server.started_at,
                "port": self.server.server_port,
                "python": sys.version.split()[0],
                "platform": sys.platform,
            },
            "settings": settings.public(),
            "hosts": host_rows,
            "ssh": {
                "dir": str(sshkeys.ssh_dir()),
                "keys": sshkeys.list_keys(),
                "default_key": sshkeys.default_key(),
            },
            "git": git,
            "state": self._state(),
            "activity": self.app.store.events(20),
            "runs": self.app.tasks.list(20),
            "policies": self.app.store.policies(),
            "stats": self.app.store.stats(),
            "templates": TEMPLATES,
            "limits": {
                "max_body": MAX_BODY,
                "history_days": settings.get("history_days"),
            },
        }
        self._send_json(200, payload)

    # --------------------------------------------------------------- runs
    def _runs(self, method: str, rest: list[str], query: dict[str, list[str]]) -> None:
        run_id = rest[1]
        if method == "GET" and len(rest) == 3 and rest[2] == "stream-token":
            if self.app.tasks.get(run_id) is None:
                row = next((item for item in self.app.store.runs(200) if item["id"] == run_id), None)
                if row is None:
                    raise ApiError(404, "run not found")
            return self._send_json(
                200,
                {"token": self.server.stream_token(run_id), "expires_in": 300, "run_id": run_id},
            )
        if method == "GET" and len(rest) == 2:
            run = self.app.tasks.get(run_id)
            if run is None:
                row = next((item for item in self.app.store.runs(200) if item["id"] == run_id), None)
                if row is None:
                    raise ApiError(404, "run not found")
                log_path = self.app.settings.runs_dir / f"{run_id}.log"
                text = util.read_text(log_path)
                return self._send_json(
                    200,
                    {
                        **row,
                        "lines": [
                            {"i": index, "ts": row["started"], "text": line, "level": "info"}
                            for index, line in enumerate(text.splitlines())
                        ],
                    },
                )
            since = _int(query.get("since", ["0"])[0], 0)
            return self._send_json(200, run.public(since=since))
        if method == "GET" and len(rest) == 3 and rest[2] == "stream":
            return self._stream_run(run_id, query)
        raise ApiError(404, "unknown run endpoint")

    def _stream_run(self, run_id: str, query: dict[str, list[str]]) -> None:
        run = self.app.tasks.get(run_id)
        if run is None:
            raise ApiError(404, "run not found")
        since = _int(query.get("since", ["0"])[0], 0)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            self.wfile.write(b"retry: 2000\n\n")
            self.wfile.flush()
            for event in run.stream(since):
                payload = json.dumps(event["data"], default=str)
                chunk = f"event: {event['event']}\ndata: {payload}\n\n".encode("utf-8")
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            self.close_connection = True

    # -------------------------------------------------------------- hosts
    def _hosts(self, method: str, rest: list[str]) -> None:
        if not rest:
            if method == "POST":
                return self._add_host(self._body())
            if method != "GET":
                raise ApiError(405, "method not allowed")
            state = self.app.monitor.state()
            by_alias = {row["alias"]: row for row in state.get("hosts", [])}
            rows = []
            for host in self.app.hosts.all():
                rows.append(
                    {
                        **host.public(),
                        "ssh_config": hosts_mod.ssh_config_entry(host.alias),
                        "fleet": by_alias.get(host.alias, {}),
                    }
                )
            return self._send_json(200, {"hosts": rows})

        alias = rest[0]
        if len(rest) == 1 and method == "POST":
            body = self._body()
            return self._add_host(body)
        if len(rest) == 1 and method == "PATCH":
            body = self._body()
            return self._update_host(alias, body)
        if len(rest) == 1 and method == "DELETE":
            return self._delete_host(alias)
        if len(rest) == 2 and rest[1] == "test" and method == "POST":
            host = self.app.hosts.get(alias)
            key = Path(host.key).expanduser() if host.key else None
            if key and not key.exists():
                key = None
            result = sshkeys.test_connection(host, key=key)
            return self._send_json(200, {"result": result, "ssh_config": hosts_mod.ssh_config_entry(alias)})
        if len(rest) == 2 and rest[1] == "terminal" and method == "GET":
            host = self.app.hosts.get(alias)
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            raw = str((query.get("path") or [""])[0]).strip()
            if not raw.startswith("/") or "\x00" in raw or "\n" in raw or ".." in PurePosixPath(raw).parts or len(raw) > 500:
                raise ApiError(400, "path must be a plain absolute path")
            command = sshkeys.terminal_command(host, raw, key=host.key_path)
            return self._send_json(200, {"host": alias, "path": raw, "command": command})
        raise ApiError(404, "unknown host endpoint")

    def _add_host(self, body: dict[str, Any]) -> None:
        alias = str(body.get("alias") or "").strip().lower()
        address = str(body.get("address") or body.get("host") or "").strip()
        user = str(body.get("user") or "pi").strip()
        port = body.get("port", 22)
        key = str(body.get("key") or "").strip()
        warnings: list[str] = []
        if key:
            key_path = Path(key).expanduser()
            if not key_path.exists():
                raise ApiError(400, f"key file not found: {key}")
            try:
                sshkeys.key_info(key_path)
            except KeyError_ as exc:
                raise ApiError(400, str(exc))
        host = self.app.hosts.add(
            alias,
            address,
            user,
            port,
            key=key,
            role=str(body.get("role") or ""),
            tags=body.get("tags") or [],
            notes=str(body.get("notes") or ""),
        )
        ssh_result = None
        if body.get("sync_ssh_config", True) and not host.local:
            try:
                ssh_result = hosts_mod.upsert_ssh_config(host)
            except Exception as exc:
                warnings.append(f"could not update ~/.ssh/config: {exc}")
        self.app.reload_hosts()
        self.app.monitor.request_refresh()
        key_path = Path(host.key).expanduser() if host.key else None
        if key_path and not key_path.exists():
            key_path = None
        connection = sshkeys.test_connection(host, key=key_path)
        self._send_json(
            201,
            {
                "host": self.app.hosts.get(alias).public(),
                "ssh_config": ssh_result,
                "connection": connection,
                "warnings": warnings,
                "ssh": {"keys": sshkeys.list_keys(), "default_key": sshkeys.default_key()},
            },
        )

    def _update_host(self, alias: str, body: dict[str, Any]) -> None:
        host = self.app.hosts.get(alias)
        changes: dict[str, Any] = {}
        for field in ("address", "user", "port", "key", "role", "notes", "tags"):
            if field in body:
                changes[field] = body[field]
        if "key" in changes and changes["key"]:
            key_path = Path(str(changes["key"])).expanduser()
            if not key_path.exists():
                raise ApiError(400, f"key file not found: {key_path}")
        updated = self.app.hosts.update(alias, **changes)
        ssh_result = None
        if body.get("sync_ssh_config", True) and not updated.local:
            try:
                ssh_result = hosts_mod.upsert_ssh_config(updated)
            except Exception as exc:
                ssh_result = {"changed": False, "error": str(exc)}
        self.app.reload_hosts()
        self.app.monitor.request_refresh()
        self._send_json(200, {"host": self.app.hosts.get(alias).public(), "ssh_config": ssh_result})

    def _delete_host(self, alias: str) -> None:
        host = self.app.hosts.get(alias)
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        force = query.get("force", ["0"])[0] in ("1", "true", "yes")
        directory = self.app.settings.stacks_dir / host.alias
        stack_names = sorted(item.name for item in directory.iterdir()) if directory.is_dir() else []
        if stack_names and not force:
            raise ApiError(
                409,
                f"{alias} still owns {len(stack_names)} stack(s) in the repo; pass force=1 to remove the host entry only",
                stacks=stack_names,
            )
        self.app.hosts.remove(alias)
        self.app.reload_hosts()
        self.app.monitor.request_refresh()
        self._send_json(
            200,
            {
                "removed": alias,
                "note": "stack files were left in place; ~/.ssh/config was not edited",
                "stacks_left": stack_names,
            },
        )

    # ---------------------------------------------------------------- ssh
    def _ssh(self, method: str, rest: list[str]) -> None:
        if method == "GET" and not rest:
            return self._send_json(
                200,
                {"dir": str(sshkeys.ssh_dir()), "keys": sshkeys.list_keys(), "default_key": sshkeys.default_key()},
            )
        if method == "POST" and rest == ["generate"]:
            body = self._body()
            name = str(body.get("name") or "homelab_console_ed25519").strip()
            if not re.match(r"^[A-Za-z0-9._-]{1,64}$", name):
                raise ApiError(400, "invalid key name")
            target = sshkeys.ssh_dir() / name
            if target.exists():
                raise ApiError(409, f"key already exists: {target}")
            info = sshkeys.generate_key(target, comment="homelab-console")
            return self._send_json(201, {"key": info.public_dict()})
        if method == "POST" and rest == ["fingerprint"]:
            body = self._body()
            address = hosts_mod.validate_address(str(body.get("address") or ""))
            port = hosts_mod.validate_port(body.get("port", 22))
            fingerprints = sshkeys.host_key_fingerprints(address, port)
            return self._send_json(
                200,
                {
                    "address": address,
                    "port": port,
                    "fingerprints": fingerprints,
                    "known": sshkeys.is_known_host(address, port),
                },
            )
        if method == "POST" and rest == ["trust"]:
            body = self._body()
            address = hosts_mod.validate_address(str(body.get("address") or ""))
            port = hosts_mod.validate_port(body.get("port", 22))
            try:
                result = sshkeys.trust_host_keys(address, port)
            except KeyError_ as exc:
                raise ApiError(400, str(exc))
            return self._send_json(200, {"result": result})
        if method == "POST" and rest == ["install"]:
            body = self._body()
            alias = str(body.get("alias") or "")
            password = str(body.get("password") or "")
            host = self.app.hosts.get(alias)
            key_choice = str(body.get("key") or host.key or "").strip()
            if not key_choice:
                default = sshkeys.default_key()
                if not default:
                    raise ApiError(400, "no SSH key selected — generate one first")
                key_choice = default["private"]
            key_path = Path(key_choice).expanduser()
            if not key_path.exists():
                raise ApiError(400, f"key file not found: {key_path}")
            if password and len(password) > 512:
                raise ApiError(400, "password too long")
            workdir = self.app.settings.state_dir / "tmp"
            util.ensure_dir(workdir, 0o700)
            result = sshkeys.install_key(host, key_path, password, workdir=workdir)
            if result.get("ok"):
                if not host.key or Path(host.key).expanduser() != key_path:
                    self.app.hosts.update(alias, key=str(key_path))
                    try:
                        hosts_mod.upsert_ssh_config(self.app.hosts.get(alias))
                    except Exception as exc:
                        result.setdefault("warnings", []).append(f"ssh config update failed: {exc}")
                    self.app.reload_hosts()
                self.app.monitor.request_refresh()
            result.pop("raw", None)
            return self._send_json(200 if result.get("ok") else 400, {"result": result})
        raise ApiError(404, "unknown ssh endpoint")

    # ------------------------------------------------------------- stacks
    def _stacks(self, method: str, rest: list[str], query: dict[str, list[str]]) -> None:
        if method == "POST" and not rest:
            return self._create_stack(self._body())
        if method == "GET" and rest == ["templates"]:
            return self._send_json(200, {"templates": TEMPLATES})
        if len(rest) < 2:
            raise ApiError(404, "stack host/name required")
        host, name = rest[0], rest[1]
        tail = rest[2:]
        if method == "GET" and not tail:
            return self._stack_detail(host, name)
        if method == "GET" and tail == ["plan"]:
            return self._stack_plan(host, name)
        if method == "GET" and tail == ["snapshots"]:
            return self._send_json(200, {"snapshots": updates.list_snapshots(self.app.settings, host, name)})
        if tail == ["profile"]:
            return self._stack_profile(method, host, name)
        if tail == ["database-backups"]:
            host_obj, stack = self._require_stack(host, name)
            if method == "GET":
                return self._send_json(200, backups.summary(host_obj, stack, self.app.settings))
            if method == "POST":
                if host_obj.local:
                    raise ApiError(400, "database backups are only supported for remote hosts")
                try:
                    result = backups.dump_database(host_obj, stack, settings=self.app.settings)
                except backups.BackupError as exc:
                    self.app.store.add_event(
                        "database-backup", "error", str(exc), host=host, svc=name
                    )
                    raise ApiError(502, str(exc))
                backup = result.get("backup") or {}
                self.app.store.add_event(
                    "database-backup",
                    "ok",
                    f"{backup.get('name') or 'database dump'} · {backup.get('bytes', 0)} bytes",
                    host=host,
                    svc=name,
                )
                result.update(backups.summary(host_obj, stack, self.app.settings))
                return self._send_json(200, result)
            raise ApiError(405, "method not allowed")
        if method == "POST" and tail == ["remote-files", "delete"]:
            return self._delete_remote_files(host, name, self._body())
        if method == "GET" and tail == ["logs"]:
            return self._stack_logs(host, name, query)
        if method == "PUT" and tail == ["file"]:
            return self._write_stack_file(host, name, self._body())
        if method == "POST" and tail == ["deploy"]:
            return self._deploy(host, name, self._body())
        if method == "POST" and tail == ["lifecycle"]:
            return self._lifecycle(host, name, self._body())
        if method == "POST" and tail == ["rollback"]:
            return self._rollback(host, name, self._body())
        raise ApiError(404, "unknown stack endpoint")

    def _stack_detail(self, host: str, name: str) -> None:
        host_obj, stack = self._require_stack(host, name)
        state = self.app.monitor.state()
        host_state = next((item for item in state.get("hosts", []) if item["alias"] == host), None)
        data = (host_state or {}).get("data") or {}
        row = self._stack_row(host, name)
        if row is None:
            runtime = stacks.stack_runtime(stack, [], None)
        else:
            runtime = row.get("runtime") or stacks.stack_runtime(stack, [], None)
        # Safety profiles and release sources are workspace settings; rebuild
        # the targets so this detail page and the update action agree.
        targets = updates.build_targets(
            stack, runtime, self.app.monitor.releases, host_arch=str(data.get("arch") or "")
        )
        row = {
            "host": host,
            "name": name,
            "runtime": runtime,
            "targets": targets,
            "updates": [item for item in targets if item["status"] == "update-available"],
            "unpinned": [item for item in targets if item["status"] == "unpinned"],
        }
        files = []
        for rel, text in sorted(stack.files.items()):
            files.append(
                {
                    "path": rel,
                    "content": text,
                    "bytes": len(text.encode()),
                    "hash": stack.hashes.get(rel, ""),
                    "secret": rel == ".env" or rel.endswith(".env"),
                }
            )
        remote_files = sorted(
            ((host_state or {}).get("data") or {}).get("stack_files", {}).get(name, {}).keys()
        )
        git = util.run(
            ["git", "-C", str(self.app.settings.repo), "status", "--porcelain", "--", str(stack.path)],
            timeout=10,
        )
        self._send_json(
            200,
            {
                "host": host_obj.public(),
                "stack": stack.public(),
                "files": files,
                "runtime": row.get("runtime"),
                "targets": row.get("targets", []),
                "updates": row.get("updates", []),
                "profile": profiles.profile_view(self.app.settings, host, name),
                "unpinned": row.get("unpinned", []),
                "remote_files": remote_files,
                "remote_path": f"{self.app.settings.remote_root.rstrip('/')}/{name}",
                "snapshots": updates.list_snapshots(self.app.settings, host, name),
                "git": {"dirty": bool(git.stdout.strip()), "porcelain": git.stdout.strip()},
            },
        )

    def _stack_plan(self, host: str, name: str) -> None:
        host_obj, stack = self._require_stack(host, name)
        if host_obj.local:
            raise ApiError(400, "local stacks are not deployable through the console")
        try:
            plan = stacks.compute_plan(host_obj, stack, self.app.settings.remote_root)
        except Exception as exc:
            raise ApiError(502, f"could not read remote stack: {exc}")
        self._send_json(
            200,
            {
                "host": host,
                "stack": name,
                "hash": plan["hash"],
                "destructive": plan["destructive"],
                "remote_only": plan["remote_only"],
                "remote_listing_complete": plan.get("remote_listing_complete", True),
                "remote_skipped": plan.get("remote_skipped", []),
                "changes": [change.public() for change in plan["changes"]],
                "checked_at": util.now_ts(),
            },
        )

    def _delete_remote_files(self, host: str, name: str, body: dict[str, Any]) -> None:
        host_obj, stack = self._require_stack(host, name)
        try:
            cleaned = stacks.clean_remote_delete_paths(body.get("paths"), stack.compose_file)
        except stacks.StackError as exc:
            raise ApiError(400, str(exc))
        if host_obj.local:
            result = util.run(["rm", "-f", "--", *cleaned], cwd=stack.path, timeout=30)
        else:
            base = f"{self.app.settings.remote_root.rstrip('/')}/{name}"
            command = "cd " + shlex.quote(base) + " && rm -f -- " + " ".join(shlex.quote(path) for path in cleaned)
            result = util.run(sshkeys.ssh_argv(host_obj) + [command], timeout=30)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "could not delete host files").strip()[-800:]
            self.app.store.add_event("stack", "error", f"remote file delete failed for {host}/{name}: {detail}", host=host, svc=name)
            raise ApiError(502, detail)
        self.app.monitor.request_refresh()
        self.app.store.add_event(
            "stack", "success", f"deleted {len(cleaned)} host-only file(s) from {host}/{name}", host=host, svc=name
        )
        self._send_json(200, {"ok": True, "deleted": cleaned})

    def _stack_logs(self, host: str, name: str, query: dict[str, list[str]]) -> None:
        host_obj, stack = self._require_stack(host, name)
        if host_obj.local:
            raise ApiError(400, "no local container logs")
        tail = _int(query.get("tail", ["300"])[0], 300)
        tail = max(10, min(2000, tail))
        result = stacks.compose(host_obj, name, "logs", self.app.settings.remote_root, extra=str(tail), timeout=45)
        text = ((result.stdout or "") + (result.stderr or ""))[-500_000:]
        self._send_json(200, {"host": host, "stack": name, "tail": tail, "text": text, "rc": result.returncode})

    def _write_stack_file(self, host: str, name: str, body: dict[str, Any]) -> None:
        _, stack = self._require_stack(host, name)
        rel = str(body.get("path") or "")
        content = body.get("content")
        if not isinstance(content, str):
            raise ApiError(400, "content must be a string")
        base_hash = body.get("base_hash")
        if base_hash:
            current = stack.files.get(rel)
            if current is not None and util.sha256_text(current) != base_hash:
                raise ApiError(409, "file changed on disk since it was loaded — reload before saving")
        result = stacks.write_file(
            self.app.settings,
            host,
            name,
            rel,
            content,
            create_only=bool(body.get("create")),
        )
        self.app.monitor.request_refresh()
        self._send_json(200, result)

    def _deploy(self, host: str, name: str, body: dict[str, Any]) -> None:
        host_obj, stack = self._require_stack(host, name)
        if host_obj.local:
            raise ApiError(400, "local stacks are not deployable through the console")
        action = str(body.get("action") or "up")
        if action not in ("up", "pull", "sync"):
            raise ApiError(400, "action must be up, pull or sync")
        try:
            plan = stacks.compute_plan(host_obj, stack, self.app.settings.remote_root)
        except Exception as exc:
            raise ApiError(502, f"could not read remote stack: {exc}")
        approve = bool(body.get("approve"))
        plan_hash = str(body.get("plan_hash") or "").strip()
        if not plan_hash:
            raise ApiError(428, "review the plan and send its plan_hash before deploying")
        if plan_hash != plan["hash"]:
            raise ApiError(
                409,
                "the diff changed since it was reviewed",
                plan=_plan_payload(plan),
            )
        if plan["destructive"] and not approve:
            raise ApiError(
                428,
                "destructive changes require explicit approval",
                plan=_plan_payload(plan),
                requires_approval=True,
            )
        run = self.app.tasks.deploy(
            host,
            name,
            action,
            plan_hash=plan_hash,
            approve=approve,
            dry_run=bool(body.get("dry_run")),
        )
        self._send_json(
            202,
            {
                "run_id": run.id,
                "run": run.public(),
                "plan": _plan_payload(plan),
            },
        )

    def _lifecycle(self, host: str, name: str, body: dict[str, Any]) -> None:
        host_obj, _ = self._require_stack(host, name)
        if host_obj.local:
            raise ApiError(400, "no local lifecycle actions")
        action = str(body.get("action") or "")
        run = self.app.tasks.lifecycle(host, name, action)
        self._send_json(202, {"run_id": run.id, "run": run.public()})

    def _rollback(self, host: str, name: str, body: dict[str, Any]) -> None:
        host_obj, _ = self._require_stack(host, name)
        if host_obj.local:
            raise ApiError(400, "no local rollback")
        snapshot_id = str(body.get("snapshot_id") or "")
        if not updates.SNAPSHOT_RE.match(snapshot_id):
            raise ApiError(400, "invalid snapshot id")
        known = updates.list_snapshots(self.app.settings, host, name)
        if not any(item["id"] == snapshot_id for item in known):
            raise ApiError(404, "snapshot not found for this stack")
        run = self.app.tasks.rollback(host, name, snapshot_id)
        self._send_json(202, {"run_id": run.id, "run": run.public()})

    def _create_stack(self, body: dict[str, Any]) -> None:
        host = str(body.get("host") or "")
        name = str(body.get("name") or "").strip().lower()
        compose = str(body.get("compose") or body.get("compose_text") or "")
        env = str(body.get("env") or body.get("env_text") or "")
        files = body.get("files") or {}
        overwrite = bool(body.get("overwrite"))
        if not HOST_RE.match(host) or host not in self.app.hosts.aliases():
            raise ApiError(400, "unknown host")
        if not STACK_NAME_RE.match(name):
            raise ApiError(400, "stack name must be lowercase letters/numbers/dots/dashes")
        stack = stacks.create_stack(self.app.settings, host, name, compose, env, overwrite=overwrite)
        if isinstance(files, dict):
            for rel, content in files.items():
                if not isinstance(rel, str) or not isinstance(content, str):
                    continue
                if rel in (stack.compose_file, ".env"):
                    continue
                stacks.write_file(self.app.settings, host, name, rel, content)
        stack = stacks.get_stack(self.app.settings, host, name)
        self.app.monitor.request_refresh()
        self._send_json(
            201,
            {
                "stack": stack.public(),
                "message": "stack files created; review the compose and deploy when ready",
            },
        )

    # ------------------------------------------------------------- updates
    def _updates(self, method: str, rest: list[str]) -> None:
        if method == "GET" and not rest:
            state = self.app.monitor.state()
            return self._send_json(
                200,
                {
                    "targets": state.get("updates", []),
                    "policies": self.app.store.policies(),
                    "release_check": self.app.monitor.release_status(),
                    "auto_update_enabled": bool(self.app.settings.get("auto_update_enabled", False)),
                    "release_log": self.app.monitor.release_log[-50:],
                    "summary": updates.targets_summary(state.get("updates", [])),
                },
            )
        if method == "POST" and rest == ["check"]:
            started = self.app.monitor.check_releases_now(force=True)
            return self._send_json(202, {"started": started, "release_check": self.app.monitor.release_status()})
        if method == "POST" and rest == ["bulk"]:
            body = self._body()
            scope = str(body.get("scope") or "fleet")
            host = str(body.get("host") or "")
            if scope not in ("fleet", "host"):
                raise ApiError(400, "scope must be fleet or host")
            if scope == "host":
                if not HOST_RE.match(host):
                    raise ApiError(400, "invalid host alias")
                self.app.hosts.get(host)
            try:
                batch = self.app.tasks.start_bulk(scope, host)
            except RunError as exc:
                raise ApiError(409, str(exc))
            return self._send_json(202, {"batch": batch.public()})
        if method == "GET" and len(rest) == 2 and rest[0] == "bulk":
            batch = self.app.tasks.get_batch(rest[1])
            if batch is None:
                raise ApiError(404, "bulk update queue not found")
            return self._send_json(200, {"batch": batch.public()})
        if method == "POST" and rest == ["apply"]:
            body = self._body()
            host = str(body.get("host") or "")
            stack_name = str(body.get("stack") or "")
            service = str(body.get("service") or "")
            action = str(body.get("action") or "update")
            acknowledge = bool(body.get("acknowledge"))
            review = str(body.get("review_hash") or "").strip()
            if not HOST_RE.match(host):
                raise ApiError(400, "invalid host")
            if not STACK_NAME_RE.match(stack_name):
                raise ApiError(400, "invalid stack")
            host_obj = self.app.hosts.get(host)
            if host_obj.local:
                raise ApiError(400, "local stacks cannot be updated this way")
            stacks.get_stack(self.app.settings, host, stack_name)
            target = self._target(host, stack_name, service)
            if target is None:
                raise ApiError(404, "update target not found (refresh the dashboard)")
            if action not in (target.get("actions") or []):
                raise ApiError(409, "that action is no longer available — refresh the updates view", target=target)
            if not review:
                raise ApiError(428, "review the update target and send its review_hash")
            if review != updates.review_hash(target, action):
                raise ApiError(409, "the update target changed since it was reviewed — refresh the updates view", target=target)
            safety = target.get("safety") or {}
            if safety.get("level") == "critical" and not acknowledge:
                raise ApiError(
                    428,
                    "this is a critical stack — confirm the backup acknowledgement",
                    requires_acknowledge=True,
                    safety=safety,
                    target=target,
                )
            try:
                run = self.app.tasks.update(host, stack_name, service, action, acknowledge=acknowledge, review_hash=review)
            except RunError as exc:
                raise ApiError(409, str(exc))
            return self._send_json(202, {"run_id": run.id, "run": run.public(), "target": target})
        raise ApiError(404, "unknown updates endpoint")

    def _policies(self, method: str) -> None:
        if method == "GET":
            return self._send_json(200, {"policies": self.app.store.policies()})
        if method == "POST":
            body = self._body()
            host = str(body.get("host") or "")
            stack_name = str(body.get("stack") or "")
            mode = str(body.get("mode") or "off")
            if mode not in ("off", "notify", "auto"):
                raise ApiError(400, "mode must be off, notify or auto")
            if host not in self.app.hosts.aliases():
                raise ApiError(400, "unknown host")
            stacks.get_stack(self.app.settings, host, stack_name)
            policy = self.app.store.set_policy(host, stack_name, mode)
            self.app.store.add_event(
                "policy", "info", f"{host}/{stack_name}: update policy = {mode}", host=host, svc=stack_name
            )
            return self._send_json(200, {"policy": policy})
        raise ApiError(405, "method not allowed")

    # ------------------------------------------------------------ activity
    def _activity(self, method: str, query: dict[str, list[str]]) -> None:
        if method != "GET":
            raise ApiError(405, "method not allowed")
        limit = max(1, min(500, _int(query.get("limit", ["100"])[0], 100)))
        host = (query.get("host") or [""])[0]
        svc = (query.get("svc") or [""])[0]
        self._send_json(
            200,
            {
                "events": self.app.store.events(limit, host=host, svc=svc),
                "runs": self.app.store.runs(limit),
                "active": [run.public(since=10**9) for run in self.app.tasks.runs.values() if run.status == "running"],
            },
        )

    def _history(self, method: str, rest: list[str], query: dict[str, list[str]]) -> None:
        if method != "GET":
            raise ApiError(405, "method not allowed")
        hours = max(1, min(24 * 60, _int(query.get("hours", ["24"])[0], 24)))
        if len(rest) == 2 and rest[0] == "host":
            return self._send_json(200, self.app.store.host_history(rest[1], hours))
        if len(rest) == 3 and rest[0] == "stack":
            return self._send_json(200, self.app.store.stack_history(rest[1], rest[2], hours))
        raise ApiError(404, "history endpoint not found")

    # -------------------------------------------------------------- repos
    def _repos(self, method: str, rest: list[str]) -> None:
        if method == "GET":
            if not rest:
                return self._send_json(200, self.app.repos.state())
            if rest[0] == "queue" and len(rest) == 1:
                return self._send_json(200, {"queue": self.app.repos.state().get("queue")})
            if rest[0] == "queue" and len(rest) == 2:
                queue = self.app.repos.get_queue(rest[1])
                if queue is None:
                    raise ApiError(404, "queue not found")
                return self._send_json(200, {"queue": queue})
            if rest == ["discovered"]:
                return self._send_json(200, self.app.repos.discovered())
            raise ApiError(404, "unknown repos endpoint")
        if method != "POST":
            raise ApiError(405, "method not allowed")
        if rest == ["refresh"]:
            self.app.repos.request_refresh()
            return self._send_json(200, {"ok": True, **self.app.repos.state()})
        if rest == ["selection"]:
            body = self._body()
            raw = body.get("exclusions", body)
            if not isinstance(raw, dict):
                raise ApiError(400, "exclusions must be an object keyed by repo root")
            result = self.app.repos.set_repo_exclusions(raw)
            return self._send_json(200, {"ok": True, **result})
        if rest == ["gitea", "check"]:
            if not self.app.repos.gitea_configured():
                raise ApiError(400, "Gitea is not configured — set a URL and owner in Settings")
            started = self.app.repos.check_gitea_async()
            return self._send_json(202, {"ok": True, "started": started, "gitea": self.app.repos.state().get("gitea")})
        if rest == ["connectivity", "check"]:
            started = self.app.repos.check_connectivity_async()
            return self._send_json(202, {"ok": True, "started": started, "connectivity": self.app.repos.state().get("connectivity")})
        if rest == ["push-both"]:
            body = self._body()
            ids = body.get("repos") or body.get("ids") or []
            if not isinstance(ids, list):
                raise ApiError(400, "repos must be a list of repo ids")
            result = self.app.repos.queue_push_both([str(item) for item in ids])
            return self._send_json(202, result)
        if not rest:
            raise ApiError(404, "unknown repos endpoint")
        repo_id = rest[0]
        action = rest[1] if len(rest) > 1 else ""
        if action == "open" and len(rest) == 2:
            return self._send_json(200, self.app.repos.open_in_vscode(repo_id))
        if action == "commit" and len(rest) == 2:
            body = self._body()
            result = self.app.repos.commit(repo_id, body.get("files"), body.get("message"))
            return self._send_json(200, result)
        if action == "push" and len(rest) == 2:
            result = self.app.repos.push_github(repo_id)
            if not result.get("ok"):
                raise ApiError(409, result.get("detail") or "push failed", result=result)
            return self._send_json(200, result)
        if action == "gitea" and len(rest) == 3 and rest[2] in ("dry-run", "sync"):
            body = self._body() if rest[2] == "sync" else {}
            review = str(body.get("review_hash") or "").strip()
            if rest[2] == "sync" and not review:
                raise ApiError(428, "run the Gitea dry run and send its review_hash before syncing")
            result = self.app.repos.sync_gitea(
                repo_id, dry_run=rest[2] == "dry-run", review_hash=review
            )
            if not result.get("ok"):
                raise ApiError(409, result.get("detail") or "Gitea sync failed", result=result)
            return self._send_json(200, result)
        raise ApiError(404, "unknown repos endpoint")

    # -------------------------------------------------------------- backup
    def _backup(self, method: str, rest: list[str]) -> None:
        if method != "POST":
            raise ApiError(405, "method not allowed")
        body = self._body()
        if not rest:
            raw = str(body.get("path") or "").strip()
            try:
                result = appbackup.create_backup(self.app.settings, raw or None)
            except (OSError, ValueError, appbackup.BackupError) as exc:
                raise ApiError(400, str(exc))
            self.app.store.add_event("backup", "success", f"backup created: {result['path']}")
            return self._send_json(200, result)
        if rest == ["restore"]:
            raw = str(body.get("path") or "").strip()
            if not raw:
                raise ApiError(400, "path is required")
            try:
                result = appbackup.restore_backup(self.app.settings, raw)
            except (OSError, ValueError, appbackup.BackupError) as exc:
                raise ApiError(400, str(exc))
            # Settings were updated in place; rebuild inventory and rescan.
            self.app.reload_hosts()
            self.app.repos.config_changed()
            self.app.repos.request_refresh()
            self.app.monitor.request_refresh()
            self.app.store.add_event("backup", "success", f"workspace restored from {raw}")
            return self._send_json(200, result)
        raise ApiError(404, "unknown backup endpoint")

    # ---------------------------------------------------------------- git
    def _git(self, method: str, rest: list[str]) -> None:
        if method == "GET" and not rest:
            status = stacks.git_status(self.app.settings)
            log = util.run(
                ["git", "-C", str(self.app.settings.repo), "log", "-5", "--format=%h|%ad|%s", "--date=format:%Y-%m-%d %H:%M"],
                timeout=10,
            )
            remotes = util.run(["git", "-C", str(self.app.settings.repo), "remote", "-v"], timeout=10)
            return self._send_json(
                200,
                {
                    "status": status,
                    "log": [line for line in log.stdout.splitlines() if line],
                    "remotes": [line for line in remotes.stdout.splitlines() if line],
                },
            )
        if method == "POST" and rest == ["commit"]:
            body = self._body()
            return self._commit(body)
        raise ApiError(404, "unknown git endpoint")

    def _commit(self, body: dict[str, Any]) -> None:
        message = str(body.get("message") or "").strip()
        if not message or len(message) > 200 or message.startswith("-"):
            raise ApiError(400, "commit message must be 1-200 characters and not start with '-'")
        repo = str(self.app.settings.repo)
        paths = stacks.workspace_commit_paths(self.app.settings)
        if not paths:
            raise ApiError(400, "nothing to commit under stacks/ or scripts/hosts.json")
        add = util.run(
            ["git", "-c", "core.fsmonitor=false", "-C", repo, "add", "--", *paths],
            timeout=20,
        )
        if add.returncode != 0:
            raise ApiError(500, f"git add failed: {add.stderr.strip()}")
        commit = util.run(
            [
                "git",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.hooksPath=/dev/null",
                "-C",
                repo,
                "commit",
                "--no-verify",
                "-m",
                message,
                "--",
                *paths,
            ],
            timeout=30,
        )
        if commit.returncode != 0:
            raise ApiError(
                409,
                f"git commit failed: {(commit.stderr or commit.stdout).strip()[:500]}",
            )
        self.app.store.add_event("git", "success", f"committed {len(paths)} file(s): {message}")
        try:
            self.app.repos.refresh_after_commit(Path(repo))
        except Exception:
            log.exception("post-commit repo refresh failed")
        self._send_json(
            200,
            {
                "ok": True,
                "commit": commit.stdout.strip().splitlines()[0] if commit.stdout.strip() else "",
                "paths": paths,
                "status": stacks.git_status(self.app.settings),
            },
        )

    # ----------------------------------------------------- release sources
    def _release_sources(self, method: str) -> None:
        current = dict(self.app.settings.get("release_sources") or {})
        if method == "GET":
            return self._send_json(200, {"release_sources": current})
        if method != "POST":
            raise ApiError(405, "method not allowed")
        body = self._body()
        repo = str(body.get("repo") or "").strip()
        values = dict(current)
        if body.get("delete"):
            if not repo:
                raise ApiError(400, "repo is required")
            key = releases.normalize_key(repo)
            candidates = {key}
            if key.startswith("library/"):
                candidates.add(key[len("library/"):])
            else:
                candidates.add(f"library/{key}")
            removed = [candidate for candidate in candidates if candidate in values]
            if not removed:
                raise ApiError(404, "release source not found")
            for candidate in removed:
                values.pop(candidate, None)
            self.app.store.add_event("release-source", "info", f"removed custom source for {key}")
        else:
            try:
                key, source = releases.validate_source_override(repo, body.get("source"))
            except ValueError as exc:
                raise ApiError(400, str(exc))
            values[key] = source
            self.app.store.add_event(
                "release-source",
                "info",
                f"{key}: {source['kind']} source configured",
            )
        self.app.settings.update({"release_sources": values})
        self.app.monitor.releases.reload_overrides()
        self.app.monitor.check_releases_now(force=True)
        self.app.monitor.request_refresh()
        return self._send_json(
            200,
            {
                "ok": True,
                "release_sources": values,
                "settings": self.app.settings.public(),
            },
        )

    # -------------------------------------------------------- stack profiles
    def _stack_profile(self, method: str, host: str, name: str) -> None:
        self._require_stack(host, name)
        if method == "GET":
            return self._send_json(200, {"profile": profiles.profile_view(self.app.settings, host, name)})
        if method != "POST":
            raise ApiError(405, "method not allowed")
        body = self._body()
        profile_map = dict(self.app.settings.get("stack_profiles") or {})
        key = profiles.profile_key(host, name)
        if body.get("reset"):
            profile_map.pop(key, None)
            action = "reset to built-in"
        else:
            raw = body.get("profile") if "profile" in body else body
            try:
                profile = profiles.validate_stack_profile(raw)
            except ValueError as exc:
                raise ApiError(400, str(exc))
            profile_map[key] = profile
            action = "saved"
        self.app.settings.update({"stack_profiles": profile_map})
        self.app.store.add_event("stack-profile", "info", f"{key}: safety profile {action}", host=host, svc=name)
        self.app.monitor.request_refresh()
        return self._send_json(
            200,
            {
                "ok": True,
                "profile": profiles.profile_view(self.app.settings, host, name),
                "settings": self.app.settings.public(),
            },
        )

    # ----------------------------------------------------------- settings
    def _settings(self, method: str) -> None:
        if method == "GET":
            return self._send_json(
                200,
                {
                    "settings": self.app.settings.public(),
                    "stats": self.app.store.stats(),
                    "app": {
                        "name": APP_NAME,
                        "version": VERSION,
                        "repo": str(self.app.settings.repo),
                        "state_dir": str(self.app.settings.state_dir),
                        "remote_root": self.app.settings.remote_root,
                    },
                },
            )
        if method == "POST":
            body = self._body()
            values: dict[str, Any] = {}
            ints = {
                "poll_interval_s": (10, 600),
                "release_ttl_s": (900, 7 * 86400),
                "history_days": (1, 365),
                "health_timeout_s": (30, 600),
                "gitea_timeout_s": (15, 900),
                "repo_poll_interval_s": (5, 120),
            }
            for key, (low, high) in ints.items():
                if key in body:
                    try:
                        value = int(body[key])
                    except (TypeError, ValueError):
                        raise ApiError(400, f"{key} must be an integer")
                    if not low <= value <= high:
                        raise ApiError(400, f"{key} must be between {low} and {high}")
                    values[key] = value
            if "remote_root" in body:
                value = str(body["remote_root"]).strip()
                if (
                    not value.startswith("/")
                    or value == "/"
                    or "//" in value
                    or not re.match(r"^/[A-Za-z0-9._/-]+$", value)
                    or ".." in value
                    or len(value) > 200
                ):
                    raise ApiError(400, "remote_root must be a concrete absolute path without spaces or '..'")
                values["remote_root"] = value
            for key in (
                "auto_update_enabled",
                "check_releases_on_start",
                "gitea_prune",
                "gitea_force",
                "gitea_check_on_start",
            ):
                if key in body:
                    values[key] = bool(body[key])
            if "repo_roots" in body:
                raw_roots = body["repo_roots"]
                if not isinstance(raw_roots, list) or len(raw_roots) > 20:
                    raise ApiError(400, "repo_roots must be a list of at most 20 folders")
                roots: list[str] = []
                for item in raw_roots:
                    raw = str(item or "").strip()
                    if not raw:
                        continue
                    path = Path(raw).expanduser()
                    if not path.is_absolute() or not path.is_dir() or len(str(path)) > 400:
                        raise ApiError(400, "each repo root must be an existing absolute directory")
                    resolved = str(path.resolve())
                    if resolved not in roots:
                        roots.append(resolved)
                values["repo_roots"] = roots
                values["repo_root"] = roots[0] if roots else ""
            elif "repo_root" in body:
                raw = str(body["repo_root"] or "").strip()
                if not raw:
                    values["repo_root"] = ""
                    values["repo_roots"] = []
                else:
                    path = Path(raw).expanduser()
                    if not path.is_absolute() or not path.is_dir() or len(str(path)) > 400:
                        raise ApiError(400, "repo_root must be an existing absolute directory")
                    resolved = str(path.resolve())
                    values["repo_root"] = resolved
                    values["repo_roots"] = [resolved]
            if "repo_roots" in values:
                # Removing a root also removes its per-root exclusions.
                allowed_roots = set(values["repo_roots"])
                exclusions = self.app.settings.get("repo_exclusions") or {}
                values["repo_exclusions"] = {
                    root: ids for root, ids in exclusions.items() if root in allowed_roots
                }
            if "gitea_url" in body:
                raw = str(body["gitea_url"] or "").strip().rstrip("/")
                if raw:
                    parsed = urllib.parse.urlparse(raw)
                    if (
                        parsed.scheme not in ("http", "https")
                        or not parsed.hostname
                        or parsed.username
                        or parsed.password
                        or len(raw) > 400
                        or " " in raw
                    ):
                        raise ApiError(400, "gitea_url must be an http(s) URL without credentials or spaces")
                    if (
                        parsed.scheme == "http"
                        and parsed.hostname not in ("127.0.0.1", "localhost", "::1")
                        and os.environ.get("HLC_GITEA_ALLOW_HTTP") != "1"
                    ):
                        raise ApiError(
                            400,
                            "gitea_url must use https for a non-loopback host; "
                            "the token is sent as a header",
                        )
                # Empty is meaningful: it disables the Gitea backup instead of
                # silently falling back to a built-in example host.
                values["gitea_url"] = raw
            if "gitea_owner" in body:
                owner = str(body["gitea_owner"] or "").strip()
                if owner and not repos.OWNER_RE.match(owner):
                    raise ApiError(400, "gitea_owner must be a valid owner name")
                values["gitea_owner"] = owner
            if "gitea_token" in body and body["gitea_token"]:
                token = str(body["gitea_token"]).strip()
                if not repos.TOKEN_RE.match(token):
                    raise ApiError(400, "gitea_token does not look like a Gitea access token")
                values["gitea_token"] = token
            if body.get("clear_gitea_token"):
                values["gitea_token"] = ""
            if "theme" in body:
                theme = str(body["theme"] or "").strip()
                if theme not in ("dark", "light", "system"):
                    raise ApiError(400, "theme must be dark, light or system")
                values["theme"] = theme
            if "github_token" in body and body["github_token"]:
                token = str(body["github_token"]).strip()
                if len(token) > 300 or any(char in token for char in ("\r", "\n", "\x00")):
                    raise ApiError(400, "token too long or contains control characters")
                values["github_token"] = token
            if body.get("clear_github_token"):
                values["github_token"] = ""
            try:
                self.app.settings.update(values)
            except KeyError as exc:
                raise ApiError(400, str(exc))
            if values.get("poll_interval_s"):
                self.app.monitor.request_refresh()
            if values.get("auto_update_enabled"):
                self.app.store.add_event("settings", "info", "automatic updates enabled")
            repo_keys = {
                "repo_root",
                "repo_roots",
                "gitea_url",
                "gitea_owner",
                "gitea_timeout_s",
                "gitea_prune",
                "gitea_force",
                "gitea_token",
                "gitea_check_on_start",
            }
            if repo_keys & values.keys():
                self.app.repos.config_changed()
                if values.get("gitea_check_on_start") and self.app.repos.gitea_configured():
                    self.app.repos.check_gitea_async()
            elif ("repo_roots" in values or "repo_root" in values
                  or "repo_exclusions" in values or "repo_poll_interval_s" in values):
                self.app.repos.request_refresh()
            return self._send_json(200, {"settings": self.app.settings.public()})
        raise ApiError(405, "method not allowed")

    # ------------------------------------------------------------- system
    def _system(self) -> None:
        settings = self.app.settings
        usage = shutil.disk_usage(str(settings.repo))
        self._send_json(
            200,
            {
                "app": {
                    "name": APP_NAME,
                    "version": VERSION,
                    "python": sys.version.split()[0],
                    "platform": sys.platform,
                    "repo": str(settings.repo),
                    "state_dir": str(settings.state_dir),
                    "remote_root": settings.remote_root,
                    "port": self.server.server_port,
                    "pid": os.getpid(),
                },
                "storage": {
                    "repo_free_bytes": usage.free,
                    "repo_total_bytes": usage.total,
                    "state_bytes": _dir_size(settings.state_dir),
                    "snapshots": len(list(settings.backups_dir.glob("*"))) if settings.backups_dir.exists() else 0,
                    "runs": len(list(settings.runs_dir.glob("*.log"))) if settings.runs_dir.exists() else 0,
                    "db": self.app.store.stats(),
                },
                "ssh": {
                    "dir": str(sshkeys.ssh_dir()),
                    "keys": sshkeys.list_keys(),
                    "default_key": sshkeys.default_key(),
                },
                "hosts": len(self.app.hosts.all()),
                "settings": settings.public(),
            },
        )

    def _maintenance(self, method: str, rest: list[str]) -> None:
        if method != "POST":
            raise ApiError(405, "method not allowed")
        if rest == ["clear-history"]:
            self.app.store.clear_history()
            self.app.store.add_event("maintenance", "info", "history cleared from Settings")
            return self._send_json(200, {"ok": True})
        raise ApiError(404, "unknown maintenance action")

    # -------------------------------------------------------------- static
    def _static(self, path: str) -> None:
        if path in ("/", ""):
            rel = "index.html"
        else:
            rel = path.lstrip("/")
        if ".." in Path(rel).parts or rel.startswith("/"):
            raise ApiError(404, "not found")
        target = (WEB_DIR / rel).resolve()
        if not str(target).startswith(str(WEB_DIR.resolve()) + os.sep) or not target.is_file():
            raise ApiError(404, "not found")
        suffix = target.suffix.lower()
        mime = MIME.get(suffix, mimetypes.guess_type(str(target))[0] or "application/octet-stream")
        data = target.read_bytes()
        if rel == "index.html":
            text = data.decode("utf-8")
            text = text.replace("__HLC_TOKEN__", html.escape(self.server.token, quote=True))
            text = text.replace("__HLC_VERSION__", html.escape(VERSION, quote=True))
            theme = str(self.server.app.settings.get("theme", "system"))
            if theme not in ("dark", "light", "system"):
                theme = "system"
            text = text.replace("__HLC_THEME_MODE__", html.escape(theme, quote=True))
            data = text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store" if suffix == ".html" else "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        if suffix == ".html":
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; "
                "img-src 'self' data:; connect-src 'self'; font-src 'self'; "
                "base-uri 'none'; form-action 'none'; object-src 'none'; frame-ancestors 'none'",
            )
        self.end_headers()
        self.wfile.write(data)


# ---------------------------------------------------------------- helpers


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _plan_payload(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "hash": plan["hash"],
        "destructive": plan["destructive"],
        "remote_only": plan["remote_only"],
        "added": plan["added"],
        "remote_listing_complete": plan.get("remote_listing_complete", True),
        "remote_skipped": plan.get("remote_skipped", []),
        "changes": [change.public() for change in plan["changes"]],
    }


def _dir_size(path: Path) -> int:
    total = 0
    if not path.exists():
        return 0
    for item in path.rglob("*"):
        if item.is_file():
            try:
                total += item.stat().st_size
            except OSError:
                pass
    return total


def make_server(host: str, port: int, app: AppService, token: str) -> ConsoleHTTPServer:
    return ConsoleHTTPServer((host, port), app, token)
