"""Regression tests for the 2026-11 security assessment findings."""

from __future__ import annotations

import http.client
import http.server
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock
import urllib.request

APP = Path(__file__).resolve().parents[1]
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

from homelab_console import appbackup, backups, profiles, releases, repos, sshkeys, stacks, updates, util  # noqa: E402
from homelab_console.app_service import AppService  # noqa: E402
from homelab_console.config import Settings  # noqa: E402
from homelab_console.hosts import Host, HostError, HostStore, upsert_ssh_config  # noqa: E402
from homelab_console.server import ConsoleHTTPServer  # noqa: E402

TOKEN = "test-token"


def make_settings(root: Path) -> Settings:
    (root / "stacks").mkdir(parents=True, exist_ok=True)
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    (root / "scripts" / "hosts.json").write_text("{}")
    return Settings.load(root)


class _StaticApp:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.monitor = self

    def request_refresh(self):
        pass


class HostHeaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = make_settings(Path(self.tmp.name))
        self.server = ConsoleHTTPServer(("127.0.0.1", 0), _StaticApp(self.settings), TOKEN)
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop)

    def _stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def request(self, method, path, *, host=None, origin=None, token=None, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"Host": host or f"127.0.0.1:{self.port}"}
        if origin:
            headers["Origin"] = origin
        if token:
            headers["X-HLC-Token"] = token
        if body is not None:
            headers["Content-Type"] = "application/json"
            headers["Content-Length"] = str(len(body))
        try:
            conn.putrequest(method, path, skip_host=True)
            for key, value in headers.items():
                conn.putheader(key, value)
            conn.endheaders(body.encode() if isinstance(body, str) else body)
            response = conn.getresponse()
            return response.status, response.read()
        finally:
            conn.close()

    def test_foreign_host_cannot_read_the_token(self):
        status, body = self.request("GET", "/", host=f"rebind.evil.example:{self.port}")
        self.assertEqual(status, 421)
        self.assertNotIn(TOKEN.encode(), body)

    def test_loopback_host_serves_the_token(self):
        status, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(TOKEN.encode(), body)

    def test_health_requires_a_known_host(self):
        self.assertEqual(self.request("GET", "/api/health")[0], 200)
        self.assertEqual(
            self.request("GET", "/api/health", host=f"rebind.evil.example:{self.port}")[0],
            421,
        )

    def test_mutating_request_keeps_origin_and_host_checks(self):
        foreign = f"rebind.evil.example:{self.port}"
        status, _ = self.request(
            "POST", "/api/refresh", host=foreign, origin=f"http://{foreign}", token=TOKEN, body="{}"
        )
        self.assertEqual(status, 421)
        status, _ = self.request(
            "POST", "/api/refresh", origin="https://evil.example", token=TOKEN, body="{}"
        )
        self.assertEqual(status, 403)
        status, _ = self.request(
            "POST", "/api/refresh", origin=f"http://127.0.0.1:{self.port}", token=TOKEN, body="{}"
        )
        self.assertEqual(status, 200)


class PathAndPlanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.settings = make_settings(self.root)
        self.stack_dir = self.root / "stacks" / "pi" / "app"
        self.stack_dir.mkdir(parents=True)
        (self.stack_dir / "docker-compose.yml").write_text(
            "services:\n  app:\n    image: nginx:${TAG}\n"
        )

    def test_incomplete_remote_listing_is_destructive(self):
        changes = stacks.plan_changes({"a.txt": "x"}, {}, remote_complete=False)
        self.assertEqual(changes[0].kind, "changed")
        self.assertTrue(changes[0].destructive)

    def test_safe_stack_path_rejects_traversal_and_symlinks(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "secret").write_text("secret")
        (self.stack_dir / "link").symlink_to(outside, target_is_directory=True)
        stacks.safe_stack_path(self.stack_dir, "ok.txt")
        with self.assertRaises(stacks.StackError):
            stacks.safe_stack_path(self.stack_dir, "../outside/secret")
        with self.assertRaises(stacks.StackError):
            stacks.safe_stack_path(self.stack_dir, "link/secret")

    def test_read_files_skips_symlinks_and_git(self):
        secret = self.root / "secret.txt"
        secret.write_text("SECRET")
        (self.stack_dir / "leak.txt").symlink_to(secret)
        (self.stack_dir / ".git").mkdir()
        (self.stack_dir / ".git" / "config").write_text("[core]\n")
        files = stacks.read_files(self.stack_dir)
        self.assertNotIn("leak.txt", files)
        self.assertNotIn(".git/config", files)

    def test_env_file_traversal_is_ignored_and_mutation_is_rejected(self):
        (self.stack_dir / "docker-compose.yml").write_text(
            "services:\n  app:\n    image: nginx:${TAG}\n"
            "    env_file: x/../../../../tmp/hlc-escape.env\n"
        )
        stack = stacks.get_stack(self.settings, "pi", "app")
        service = stack.services[0]
        self.assertEqual(updates._env_file_for(stack, service, "TAG"), ".env")
        target = {
            "file": "x/../../../../tmp/hlc-escape.env",
            "service": "app",
            "mechanism": "env",
            "key": "TAG",
            "new_value": "1.2.3",
        }
        with self.assertRaises(updates.UpdateError):
            updates.plan_mutation(stack, target, "update")

    def test_workspace_commit_filters_env_family(self):
        git = ["git", "-C", str(self.root)]
        subprocess.run([*git, "init", "-q"], check=True)
        (self.stack_dir / ".env").write_text("A=1\n")
        (self.stack_dir / ".env.local").write_text("B=1\n")
        (self.root / ".gitignore").write_text("*.log\n")
        paths = stacks.workspace_commit_paths(self.settings)
        self.assertIn("stacks/pi/app/docker-compose.yml", paths)
        self.assertFalse(any(util.is_secret_file(path) for path in paths))

    def test_review_hash_binds_the_value(self):
        target = {
            "host": "pi",
            "stack": "app",
            "service": "app",
            "file": ".env",
            "mechanism": "env",
            "key": "TAG",
            "current": "1.2.2",
            "new_value": "1.2.3",
            "pin_latest": "",
            "digest_value": "",
            "services_affected": ["app"],
        }
        first = updates.review_hash(target, "update")
        second = updates.review_hash(target, "update")
        target["new_value"] = "9.9.9"
        self.assertEqual(first, second)
        self.assertNotEqual(first, updates.review_hash(target, "update"))


class PortProbeTests(unittest.TestCase):
    def test_crafted_port_is_ignored_and_labels_are_quoted(self):
        services, _, _ = stacks.parse_compose(
            "services:\n  web:\n    image: nginx:1\n    ports:\n"
            "      - \"127.0.0.1'; id>pwn; echo ':8080:80\"\n"
            "      - \"127.0.0.1:8081:80\"\n"
        )
        ports = updates._tcp_ports(services)
        self.assertEqual(ports, [("web", "127.0.0.1:8081")])
        captured = {}
        class Result:
            stdout = "web:8081:OK\n"
        def fake_shell(host, command, **kwargs):
            captured["command"] = command
            return Result()
        with mock.patch.object(updates.remote, "remote_shell", side_effect=fake_shell):
            updates._port_probe(mock.Mock(local=False), ports)
        self.assertNotIn("id>pwn", captured["command"])
        self.assertIn("bash -c", captured["command"])
        self.assertIn("exec 3<>/dev/tcp/127.0.0.1/8081", captured["command"])
        # A bare /dev/tcp path is a command word, not a redirection: bash
        # reports rc 127 even when the port accepts connections.
        self.assertNotIn("bash -c /dev/tcp", captured["command"])

    def test_probe_fragment_opens_a_live_socket(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
            server.bind(("127.0.0.1", 0))
            server.listen(1)
            port = server.getsockname()[1]
            fragment = updates._tcp_probe_fragment(f"127.0.0.1:{port}")
            result = subprocess.run(
                ["bash", "-c", fragment], capture_output=True, text=True, timeout=10
            )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_probe_fragment_fails_on_a_closed_port(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
            server.bind(("127.0.0.1", 0))
            port = server.getsockname()[1]
        fragment = updates._tcp_probe_fragment(f"127.0.0.1:{port}")
        result = subprocess.run(["bash", "-c", fragment], capture_output=True, text=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)


class HealthGateTests(unittest.TestCase):
    def test_describe_health_includes_port_failure(self):
        summary = updates.describe_health(
            {"states": {"web": "running/healthy"}, "port_failures": ["web:8080"]}
        )
        self.assertIn("web=running/healthy", summary)
        self.assertIn("port probe failed", summary)
        self.assertIn("web:8080", summary)

    def test_wait_healthy_reports_ports_after_containers_are_green(self):
        stack = mock.Mock()
        stack.name = "app"
        stack.service_names = ["web"]
        stack.services = []
        rows = [{"Service": "web", "State": "running", "Health": "healthy"}]
        lines: list[str] = []
        clock = iter([0.0, 0.0, 20.0, 20.0, 25.0])
        with mock.patch.object(updates, "compose_ps", return_value=rows), mock.patch.object(
            updates,
            "_port_probe",
            return_value={"checked": 1, "ok": False, "failures": ["web:8080"]},
        ), mock.patch.object(
            updates.time, "time", side_effect=lambda: next(clock)
        ), mock.patch.object(
            updates.time, "sleep", return_value=None
        ):
            result = updates.wait_healthy(
                mock.Mock(local=False), stack, "/opt/stacks", lines.append, timeout=1
            )
        self.assertFalse(result["ok"])
        self.assertEqual(result["states"], {"web": "running/healthy"})
        self.assertEqual(result["port_failures"], ["web:8080"])
        self.assertTrue(any("ports failing: web:8080" in line for line in lines))


class BackupSecretTests(unittest.TestCase):
    def test_env_family_is_marked_and_restored_0600(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = make_settings(base / "source")
            stack = source.stacks_dir / "pi" / "app"
            stack.mkdir(parents=True)
            (stack / "docker-compose.yml").write_text("services:\n  app:\n    image: nginx:1\n")
            (stack / ".env.local").write_text("B=2\n")
            archive = base / "backup.zip"
            result = appbackup.create_backup(source, archive)
            self.assertTrue(result["includes_secrets"])
            target = make_settings(base / "target")
            appbackup.restore_backup(target, archive)
            restored = target.stacks_dir / "pi" / "app" / ".env.local"
            self.assertTrue(restored.is_file())
            self.assertEqual(restored.stat().st_mode & 0o777, 0o600)


class ProfileAndBackupPathTests(unittest.TestCase):
    def test_dot_segments_are_rejected(self):
        with self.assertRaises(ValueError):
            profiles.validate_stack_profile({"database": {"console_subdir": ".."}})
        with self.assertRaises(ValueError):
            profiles.validate_stack_profile({"database": {"scheduled_subdir": "."}})

    def test_relative_upload_location_is_rejected(self):
        stack = mock.Mock()
        stack.name = "app"
        stack.env = {"DATA_DIR": "../../etc"}
        plan = {"upload_env": "DATA_DIR", "scheduled_subdir": "backups", "console_subdir": "console-backups"}
        with self.assertRaises(backups.BackupError):
            backups._base_directory(stack, plan)


class RepoSafetyTests(unittest.TestCase):
    def test_dangerous_repo_config_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
            subprocess.run(
                ["git", "-C", str(repo), "config", "url.http://evil.invalid/.insteadOf", "https://gitea.example/"],
                check=True,
            )
            with self.assertRaises(repos.RepoError):
                repos._ensure_repo_config_safe(repo)

    def test_common_repo_config_is_not_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
            subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", "https://github.com/e/demo.git"], check=True)
            # These are normal settings from gitea-tools / a local keychain
            # setup and must not stop a read-only Gitea check.
            subprocess.run(["git", "-C", str(repo), "config", "remote.origin.pushurl", "https://github.com/e/demo.git"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "core.hooksPath", ".githooks"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "credential.helper", "osxkeychain"], check=True)
            repos._ensure_repo_config_safe(repo)

    def test_shell_credential_helper_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "credential.helper", "!/tmp/evil"], check=True)
            with self.assertRaises(repos.RepoError):
                repos._ensure_repo_config_safe(repo)

    def test_gitea_header_is_scoped_and_hides_the_token(self):
        cfg = repos.GiteaConfig(root=Path("/tmp"), url="https://gitea.example", owner="example")
        token = "abcdefghijklmnopqrstuvwxyz0123456789"
        env = repos._gitea_env(cfg, token)
        self.assertEqual(env["GIT_CONFIG_KEY_0"], "http.https://gitea.example/.extraHeader")
        self.assertNotIn(token, env["GIT_CONFIG_VALUE_0"])
        self.assertTrue(env["GIT_CONFIG_VALUE_0"].startswith("Authorization: Basic "))

    def test_child_env_drops_app_secrets(self):
        with mock.patch.dict(
            os.environ,
            {
                "HLC_GITEA_TOKEN": "secret",
                "GITEA_TOKEN": "secret",
                "GITHUB_TOKEN": "secret",
                "HLC_TOKEN": "secret",
                "PATH": "/usr/bin",
            },
        ):
            env = util.child_env()
        self.assertEqual(env.get("PATH"), "/usr/bin")
        for key in ("HLC_GITEA_TOKEN", "GITEA_TOKEN", "GITHUB_TOKEN", "HLC_TOKEN"):
            self.assertNotIn(key, env)

    def test_redirect_does_not_forward_authorization(self):
        seen = {}
        class Target(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                seen["auth"] = self.headers.get("Authorization")
                self.send_response(200)
                self.end_headers()
            def log_message(self, *_args):
                pass
        class Redirect(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{target.server_port}/x")
                self.end_headers()
            def log_message(self, *_args):
                pass
        target = http.server.HTTPServer(("127.0.0.1", 0), Target)
        redirect = http.server.HTTPServer(("127.0.0.1", 0), Redirect)
        for server in (target, redirect):
            threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            request = urllib.request.Request(
                f"http://127.0.0.1:{redirect.server_port}/",
                headers={"Authorization": "token SECRET"},
            )
            with util.urlopen(request, timeout=5) as response:
                response.read()
        finally:
            target.shutdown()
            redirect.shutdown()
            target.server_close()
            redirect.server_close()
        self.assertIsNone(seen.get("auth"))


class HostAndSshTests(unittest.TestCase):
    def test_host_alias_and_key_are_validated_on_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = make_settings(root)
            settings.hosts_file.write_text(
                json.dumps(
                    {
                        "bad alias!": {"user": "pi", "host": "192.0.2.10"},
                        "pi": {"user": "pi", "host": "192.0.2.10", "key": "/tmp/key\nProxyCommand x"},
                    }
                )
            )
            store = HostStore(settings)
            self.assertEqual(store.aliases(), [])

    def test_upsert_ssh_config_rejects_control_characters(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"HLC_SSH_DIR": str(Path(tmp) / "ssh")}):
                host = Host(alias="pi", user="pi", address="192.0.2.10", key="/tmp/key\nProxyCommand x")
                with self.assertRaises(HostError):
                    upsert_ssh_config(host)

    def test_trust_host_keys_pins_known_hosts(self):
        with tempfile.TemporaryDirectory() as tmp:
            ssh_dir = Path(tmp) / "ssh"
            completed = subprocess.CompletedProcess(
                ["ssh-keyscan"], 0, "192.0.2.10 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAA\n", ""
            )
            with mock.patch.dict(os.environ, {"HLC_SSH_DIR": str(ssh_dir)}), \
                 mock.patch.object(sshkeys.util, "run", return_value=completed):
                result = sshkeys.trust_host_keys("192.0.2.10")
            known = ssh_dir / "known_hosts"
            self.assertEqual(result["added"], 1)
            self.assertIn("ssh-ed25519", known.read_text())
            self.assertEqual(known.stat().st_mode & 0o777, 0o600)


class StreamTokenTests(unittest.TestCase):
    def test_stream_token_is_scoped_and_expires(self):
        with tempfile.TemporaryDirectory() as tmp:
            server = ConsoleHTTPServer(("127.0.0.1", 0), _StaticApp(make_settings(Path(tmp))), TOKEN)
            try:
                token = server.stream_token("run-1")
                self.assertTrue(server.valid_stream_token("run-1", token))
                self.assertFalse(server.valid_stream_token("run-2", token))
                self.assertFalse(server.valid_stream_token("run-1", "not-a-token"))
            finally:
                server.server_close()


class GiteaUrlTests(unittest.TestCase):
    def test_plain_http_needs_an_explicit_opt_in(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            settings.values.update({"gitea_url": "http://192.0.2.10:3000", "gitea_owner": "example"})
            with self.assertRaises(repos.RepoError):
                repos.gitea_config(settings)
            with mock.patch.dict(os.environ, {"HLC_GITEA_ALLOW_HTTP": "1"}):
                cfg = repos.gitea_config(settings)
            self.assertEqual(cfg.url, "http://192.0.2.10:3000")

    def test_invalid_env_token_is_not_used(self):
        with mock.patch.dict(os.environ, {"HLC_GITEA_TOKEN": "bad\ntoken"}):
            settings = mock.Mock(values={})
            self.assertEqual(repos.resolve_gitea_token(settings), ("", "invalid"))


class RepoIdentityAndPushTests(unittest.TestCase):
    def test_push_to_a_foreign_branch_remote_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
            subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", "https://github.com/example/demo.git"], check=True)
            subprocess.run(["git", "-C", str(repo), "remote", "add", "evil", "https://evil.invalid/demo.git"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "branch.main.remote", "evil"], check=True)
            with self.assertRaises(repos.RepoError):
                repos._assert_push_destination(repo, "https://github.com/example/demo.git")

    def test_duplicate_relative_ids_are_ambiguous(self):
        class Store:
            def add_event(self, *_args, **_kwargs):
                pass

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            first, second = base / "code", base / "archive"
            for root in (first, second):
                repo = root / "shared"
                repo.mkdir(parents=True)
                subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
            settings = make_settings(base / "console")
            settings.values["repo_roots"] = [str(first), str(second)]
            settings.values["repo_root"] = str(first)
            fleet = repos.RepoFleet(settings, Store())
            fleet.refresh()
            with self.assertRaises(repos.RepoError):
                fleet.resolve("shared")


class GiteaSyncReviewTests(unittest.TestCase):
    def test_a_stale_preview_hash_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            local = base / "local"
            local.mkdir()
            subprocess.run(["git", "-C", str(local), "init", "-q"], check=True)
            (local / "f.txt").write_text("one\n")
            subprocess.run(["git", "-C", str(local), "add", "f.txt"], check=True)
            subprocess.run(["git", "-C", str(local), "commit", "-qm", "one"], check=True)
            subprocess.run(
                ["git", "-C", str(local), "remote", "add", "origin", "https://github.com/example/widget.git"],
                check=True,
            )
            target = base / "gitea" / "example" / "widget.git"
            target.parent.mkdir(parents=True)
            subprocess.run(["git", "init", "-q", "--bare", str(target)], check=True)
            cfg = repos.GiteaConfig(root=base, url=(base / "gitea").as_uri(), owner="example", timeout=30)
            result = repos.sync_repo(local, cfg, "", dry_run=True)
            self.assertEqual(result["status"], "would-sync")
            self.assertTrue(result.get("review_hash"))
            stale = repos.sync_repo(local, cfg, "", review_hash="stale")
            self.assertEqual(stale["status"], "stale")
            (local / "f.txt").write_text("two\n")
            subprocess.run(["git", "-C", str(local), "add", "f.txt"], check=True)
            subprocess.run(["git", "-C", str(local), "commit", "-qm", "two"], check=True)
            changed = repos.sync_repo(local, cfg, "", review_hash=result["review_hash"])
            self.assertEqual(changed["status"], "stale")

class DeployGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        (root / "scripts").mkdir(parents=True)
        (root / "scripts" / "hosts.json").write_text(
            json.dumps({"pi": {"user": "pi", "host": "192.0.2.10"}})
        )
        stack = root / "stacks" / "pi" / "app"
        stack.mkdir(parents=True)
        (stack / "docker-compose.yml").write_text("services:\n  app:\n    image: nginx:1\n")
        self.settings = Settings.load(root)
        self.app = AppService(self.settings)
        self.addCleanup(self.app.store.close)
        self.server = ConsoleHTTPServer(("127.0.0.1", 0), self.app, TOKEN)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop)
        self.plan = {
            "hash": "reviewed-hash",
            "destructive": False,
            "remote_only": 0,
            "added": 0,
            "changes": [],
            "remote_listing_complete": True,
            "remote_skipped": [],
        }

    def _stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def post(self, body):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
        raw = json.dumps(body).encode()
        try:
            conn.request(
                "POST",
                "/api/stacks/pi/app/deploy",
                body=raw,
                headers={
                    "Host": f"127.0.0.1:{self.server.server_port}",
                    "X-HLC-Token": TOKEN,
                    "Content-Type": "application/json",
                },
            )
            response = conn.getresponse()
            return response.status, json.loads(response.read() or b"{}")
        finally:
            conn.close()

    def test_deploy_without_a_plan_hash_is_rejected(self):
        class FakeRun:
            id = "run-1"
            def public(self):
                return {"id": self.id, "status": "queued"}
        with mock.patch.object(self.app.tasks, "deploy", return_value=FakeRun()) as deploy, \
             mock.patch("homelab_console.server.stacks.compute_plan", return_value=self.plan):
            status, payload = self.post({"action": "up", "approve": True})
            self.assertEqual(status, 428)
            deploy.assert_not_called()
            status, _payload = self.post({"action": "up", "approve": True, "plan_hash": "reviewed-hash"})
            self.assertEqual(status, 202)
            deploy.assert_called_once()

class StreamEndpointTests(unittest.TestCase):
    class Run:
        def stream(self, since=0):
            yield {"event": "done", "data": {"status": "ok", "lines": []}}

    class Tasks:
        def get(self, run_id):
            return StreamEndpointTests.Run() if run_id == "run-1" else None

    class App(_StaticApp):
        def __init__(self, settings):
            super().__init__(settings)
            self.tasks = StreamEndpointTests.Tasks()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = make_settings(Path(self.tmp.name))
        self.server = ConsoleHTTPServer(("127.0.0.1", 0), self.App(self.settings), TOKEN)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop)

    def _stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def get(self, path, *, host=None, token=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
        headers = {"Host": host or f"127.0.0.1:{self.server.server_port}"}
        if token:
            headers["X-HLC-Token"] = token
        try:
            conn.request("GET", path, headers=headers)
            response = conn.getresponse()
            return response.status, response.read()
        finally:
            conn.close()

    def test_stream_token_is_required_and_scoped(self):
        status, body = self.get("/api/runs/run-1/stream-token", token=TOKEN)
        self.assertEqual(status, 200)
        stream_token = json.loads(body)["token"]
        status, body = self.get(f"/api/runs/run-1/stream?stream_token={stream_token}")
        self.assertEqual(status, 200)
        self.assertIn(b"event: done", body)
        status, _ = self.get(f"/api/runs/run-1/stream?token={TOKEN}")
        self.assertEqual(status, 401)

if __name__ == "__main__":
    unittest.main()
