"""HTTP-layer regression tests (local socket only)."""

from __future__ import annotations

import http.client
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

APP = Path(__file__).resolve().parents[1]
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

from homelab_console.app_service import AppService  # noqa: E402
from homelab_console.config import Settings  # noqa: E402
from homelab_console.server import ConsoleHTTPServer  # noqa: E402

TOKEN = "test-token"


class _StubMonitor:
    def __init__(self):
        self.refreshes = 0

    def request_refresh(self):
        self.refreshes += 1


class _StubRepos:
    def __init__(self):
        self.refreshed = 0
        self.opened: list[str] = []
        self.queued: list[list[str]] = []
        self.selection: dict[str, list[str]] = {}
        self.nested_selection: dict[str, list[str]] = {}
        self.state_data = {
            "root": "/tmp/repos",
            "root_error": "",
            "repos": [{"id": "homelab"}],
            "summary": {"total": 1},
            "generated_at": 1,
            "scanning": False,
            "gitea": {"state": "never"},
            "queue": None,
        }

    def state(self):
        return dict(self.state_data)

    def discovered(self):
        return {
            "root_configured": True,
            "roots": ["/tmp/repos"],
            "deep_roots": [],
            "repos": [
                {"root": "/tmp/repos", "rel": "homelab", "name": "homelab", "selected": True},
                {"root": "/tmp/repos", "rel": "skip-me", "name": "skip-me", "selected": False},
            ],
            "errors": [],
            "warnings": [],
            "exclusions": self.selection,
            "included": self.nested_selection,
        }

    def set_repo_exclusions(self, exclusions, included=None):
        self.selection = exclusions
        if included is not None:
            self.nested_selection = included
        return {
            "roots": ["/tmp/repos"],
            "exclusions": exclusions,
            "included": self.nested_selection,
            "discovered": [],
            "warnings": [],
            "errors": [],
        }

    def request_refresh(self):
        self.refreshed += 1

    def refresh_after_commit(self, path):
        self.refreshed += 1
        return True

    def check_gitea_async(self):
        return True

    def check_connectivity_async(self):
        return True

    def get_queue(self, queue_id):
        return {"id": queue_id, "items": []} if queue_id == "q1" else None

    def queue_push_both(self, ids):
        self.queued.append(ids)
        return {"ok": True, "run_id": "run1", "queue": {"id": "q1", "items": [], "status": "queued"}}

    def open_in_vscode(self, repo_id):
        self.opened.append(repo_id)
        return {"ok": True, "path": "/tmp/repos/homelab"}


class _StubApp:
    def __init__(self):
        self.monitor = _StubMonitor()
        self.repos = _StubRepos()


class KeepAliveTests(unittest.TestCase):
    def setUp(self):
        self.app = _StubApp()
        self.server = ConsoleHTTPServer(("127.0.0.1", 0), self.app, TOKEN)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def test_repeated_posts_drain_their_bodies(self):
        """A body must never leak into the next request on the socket.

        Regression: POST /api/refresh carries "{}"; if that body is not read,
        the next request line becomes "{}GET ..." and BaseHTTPRequestHandler
        answers 501 Unsupported method.
        """
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
        headers = {"X-HLC-Token": TOKEN, "Content-Type": "application/json"}
        try:
            for _ in range(3):
                conn.request("POST", "/api/refresh", body="{}", headers=headers)
                response = conn.getresponse()
                self.assertEqual(response.status, 200)
                self.assertIn(b'"ok"', response.read())

                conn.request("GET", "/api/health")
                health = conn.getresponse()
                self.assertEqual(health.status, 200)
                self.assertIn(b'"ok": true', health.read())
        finally:
            conn.close()
        self.assertEqual(self.app.monitor.refreshes, 3)


class RepoApiTests(unittest.TestCase):
    def setUp(self):
        self.app = _StubApp()
        self.server = ConsoleHTTPServer(("127.0.0.1", 0), self.app, TOKEN)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def request(self, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
        headers = {"X-HLC-Token": TOKEN, "Content-Type": "application/json"}
        try:
            conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
            response = conn.getresponse()
            return response.status, json.loads(response.read() or b"{}")
        finally:
            conn.close()

    def test_requires_token(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
        try:
            conn.request("GET", "/api/repos")
            response = conn.getresponse()
            self.assertEqual(response.status, 401)
            response.read()
        finally:
            conn.close()

    def test_get_state_and_encoded_repo_id(self):
        status, payload = self.request("GET", "/api/repos")
        self.assertEqual(status, 200)
        self.assertEqual(payload["repos"][0]["id"], "homelab")

        status, payload = self.request("POST", "/api/repos/dsh-extensions%2Fdsh-agents-md/open")
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(self.app.repos.opened, ["dsh-extensions/dsh-agents-md"])

    def test_queue_actions(self):
        status, payload = self.request("POST", "/api/repos/connectivity/check")
        self.assertEqual(status, 202)
        self.assertTrue(payload["started"])

        status, payload = self.request("POST", "/api/repos/push-both", {"repos": ["homelab"]})
        self.assertEqual(status, 202)
        self.assertEqual(payload["run_id"], "run1")
        self.assertEqual(self.app.repos.queued, [["homelab"]])

        status, payload = self.request("GET", "/api/repos/queue/q1")
        self.assertEqual(status, 200)
        self.assertEqual(payload["queue"]["id"], "q1")

    def test_discovered_and_selection_round_trip(self):
        status, payload = self.request("GET", "/api/repos/discovered")
        self.assertEqual(status, 200)
        self.assertEqual(payload["roots"], ["/tmp/repos"])
        self.assertEqual(len(payload["repos"]), 2)

        status, payload = self.request(
            "POST",
            "/api/repos/selection",
            {"exclusions": {"/tmp/repos": ["skip-me"]}, "included": {"/tmp/repos": ["nested/one"]}},
        )
        self.assertEqual(status, 200)
        self.assertEqual(self.app.repos.selection, {"/tmp/repos": ["skip-me"]})
        self.assertEqual(self.app.repos.nested_selection, {"/tmp/repos": ["nested/one"]})
        self.assertEqual(payload["exclusions"], {"/tmp/repos": ["skip-me"]})
        self.assertEqual(payload["included"], {"/tmp/repos": ["nested/one"]})


class ServiceMetadataApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        (root / "scripts").mkdir(parents=True)
        (root / "scripts" / "hosts.json").write_text(
            json.dumps({"location_": "scripts/hosts.json", "pi-a": {"user": "pi", "host": "192.0.2.10"}})
        )
        stack = root / "stacks" / "pi-a" / "app"
        stack.mkdir(parents=True)
        (stack / "docker-compose.yml").write_text(
            "services:\n  app:\n    image: ghcr.io/paperless-ngx/paperless-ngx:2.10.0\n"
        )
        self.settings = Settings.load(root)
        self.app = AppService(self.settings)
        self.addCleanup(self.app.store.close)
        self.app.monitor.check_releases_now = mock.MagicMock(return_value=True)
        self.server = ConsoleHTTPServer(("127.0.0.1", 0), self.app, TOKEN)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def request(self, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
        headers = {"X-HLC-Token": TOKEN, "Content-Type": "application/json"}
        try:
            conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
            response = conn.getresponse()
            return response.status, json.loads(response.read() or b"{}")
        finally:
            conn.close()

    def test_release_source_round_trip(self):
        status, payload = self.request(
            "POST",
            "/api/release-sources",
            {"repo": "ghcr.io/paperless-ngx/paperless-ngx", "source": {"kind": "github", "github_repo": "paperless-ngx/paperless-ngx"}},
        )
        self.assertEqual(status, 200)
        key = "ghcr.io/paperless-ngx/paperless-ngx"
        self.assertEqual(payload["release_sources"][key]["kind"], "github")
        self.assertEqual(self.app.settings.get("release_sources")[key]["github_repo"], "paperless-ngx/paperless-ngx")
        status, payload = self.request("GET", "/api/release-sources")
        self.assertEqual(status, 200)
        self.assertIn(key, payload["release_sources"])
        status, _ = self.request("POST", "/api/release-sources", {"repo": key, "delete": True})
        self.assertEqual(status, 200)
        self.assertNotIn(key, self.app.settings.get("release_sources"))

    def test_stack_profile_round_trip_and_validation(self):
        status, payload = self.request(
            "POST",
            "/api/stacks/pi-a/app/profile",
            {"profile": {"safety": {"level": "critical", "backup": "Dump first."}, "database": None}},
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["profile"]["custom"])
        self.assertEqual(payload["profile"]["safety"]["level"], "critical")
        status, payload = self.request("GET", "/api/stacks/pi-a/app/profile")
        self.assertEqual(status, 200)
        self.assertFalse(payload["profile"]["safety"]["database_backup"])
        status, payload = self.request(
            "POST", "/api/stacks/pi-a/app/profile", {"profile": {"safety": {"level": "nope"}}}
        )
        self.assertEqual(status, 400)
        status, payload = self.request("POST", "/api/stacks/pi-a/app/profile", {"reset": True})
        self.assertEqual(status, 200)
        self.assertFalse(payload["profile"]["custom"])

    def test_create_file_route_writes_nested_text_and_refuses_overwrite(self):
        status, payload = self.request(
            "PUT",
            "/api/stacks/pi-a/app/file",
            {"path": "config/sub/settings.yml", "content": "enabled: true\n", "create": True},
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["path"], "stacks/pi-a/app/config/sub/settings.yml")
        target = self.settings.stacks_dir / "pi-a" / "app" / "config" / "sub" / "settings.yml"
        self.assertEqual(target.read_text(), "enabled: true\n")

        status, _payload = self.request(
            "PUT",
            "/api/stacks/pi-a/app/file",
            {"path": "config/sub/settings.yml", "content": "enabled: false\n", "create": True},
        )
        self.assertEqual(status, 400)
        self.assertEqual(target.read_text(), "enabled: true\n")

        status, _payload = self.request(
            "PUT",
            "/api/stacks/pi-a/app/file",
            {"path": "config/.env.local", "content": "SECRET=1\n", "create": True},
        )
        self.assertEqual(status, 200)
        env = self.settings.stacks_dir / "pi-a" / "app" / "config" / ".env.local"
        self.assertEqual(env.stat().st_mode & 0o777, 0o600)

    def test_stack_detail_uses_profile_immediately(self):
        self.request(
            "POST",
            "/api/stacks/pi-a/app/profile",
            {"profile": {"safety": {"level": "critical", "backup": "Dump first."}}},
        )
        status, payload = self.request("GET", "/api/stacks/pi-a/app")
        self.assertEqual(status, 200)
        self.assertEqual(payload["profile"]["safety"]["level"], "critical")
        self.assertEqual(payload["targets"][0]["safety"]["level"], "critical")


if __name__ == "__main__":
    unittest.main()
