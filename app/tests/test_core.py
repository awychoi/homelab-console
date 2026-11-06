"""Unit tests for the console core. No network, no SSH, no Docker.

Run from the repo root:
    .venv/bin/python -m unittest discover -s app/tests -v
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

from homelab_console import backups, profiles, releases, sshkeys, stacks, updates, util  # noqa: E402
from homelab_console.app_service import WARMUP_SOFT_DEADLINE_S, AppService  # noqa: E402
from homelab_console.config import Settings, ensure_workspace, reset_settings  # noqa: E402
from homelab_console.hosts import Host, HostError, HostStore, upsert_ssh_config, validate_user  # noqa: E402
from homelab_console.runner import Run, TaskManager  # noqa: E402

COMPOSE = """name: demo

# leading comment
services:
  web:
    image: "nginx:1.29.0"   # pinned on purpose
    container_name: demo-web
    ports:
      - "127.0.0.1:8080:80"
      - "8443:443/udp"
    environment:
      FOO: bar

  worker:
    image: repo/app:${TAG:-latest}
    build: .
  buildonly:
    build: .
"""


def make_repo(root: Path) -> Settings:
    (root / "stacks" / "pi3" / "demo").mkdir(parents=True)
    (root / "scripts").mkdir(parents=True)
    (root / "scripts" / "hosts.json").write_text(
        json.dumps(
            {
                "location_": "scripts/hosts.json",
                "pi3": {"user": "pi", "host": "10.0.0.3", "notes": "test"},
            },
            indent=4,
        )
    )
    (root / "stacks" / "pi3" / "demo" / "docker-compose.yml").write_text(COMPOSE)
    (root / "stacks" / "pi3" / "demo" / ".env").write_text("TAG=v1\nDB_PASSWORD='secret#not-comment'\n")
    settings = Settings.load(root)
    settings.values["gitea_url"] = "https://gitea.invalid"
    settings.values["gitea_owner"] = "example"
    return settings


class ComposeTests(unittest.TestCase):
    def test_parse_services_images_ports(self):
        services, project, error = stacks.parse_compose(COMPOSE)
        self.assertEqual(error, "")
        self.assertEqual(project, "demo")
        self.assertEqual([service.name for service in services], ["web", "worker", "buildonly"])
        web = services[0]
        self.assertEqual(web.image_raw, "nginx:1.29.0")
        self.assertEqual(web.image.repo, "nginx")
        self.assertEqual(web.image.tag, "1.29.0")
        self.assertEqual(web.container_name, "demo-web")
        self.assertEqual(web.ports[0]["host_ip"], "127.0.0.1")
        self.assertEqual(web.ports[0]["host_port"], "8080")
        self.assertEqual(web.ports[1]["protocol"], "udp")
        worker = services[1]
        self.assertTrue(worker.has_build)
        self.assertEqual(worker.image.variable, "TAG")
        self.assertEqual(worker.image.default_tag, "latest")
        self.assertFalse(worker.image.explicit_tag)
        self.assertTrue(services[2].has_build)

    def test_parse_compose_rejects_missing_services(self):
        _, _, error = stacks.parse_compose("version: '3'\n")
        self.assertIn("services", error)

    def test_image_reference_forms(self):
        caddy = stacks.parse_image("caddy:2.11.7")
        self.assertEqual((caddy.repo, caddy.tag, caddy.explicit_tag), ("caddy", "2.11.7", True))
        n8n = stacks.parse_image("docker.n8n.io/n8nio/n8n")
        self.assertEqual((n8n.tag, n8n.explicit_tag), ("latest", False))
        digest = stacks.parse_image("nginx@sha256:" + "a" * 64)
        self.assertEqual(digest.repo, "nginx")
        self.assertFalse(digest.explicit_tag)
        self.assertTrue(digest.digest.startswith("sha256:"))

    def test_env_parsing_preserves_quotes_and_hash(self):
        env = stacks.parse_env("TAG=v1\nDB_PASSWORD='secret#not-comment'\n# ignored\nA=1 # tail\n")
        self.assertEqual(env["TAG"], "v1")
        self.assertEqual(env["DB_PASSWORD"], "secret#not-comment")
        self.assertEqual(env["A"], "1")

    def test_plan_changes_and_remote_only(self):
        local = {"docker-compose.yml": "a", "new.txt": "n"}
        remote = {"docker-compose.yml": "b", "old.txt": "o"}
        changes = stacks.plan_changes(local, remote)
        by_path = {change.path: change for change in changes}
        self.assertEqual(by_path["docker-compose.yml"].kind, "changed")
        self.assertTrue(by_path["docker-compose.yml"].destructive)
        self.assertEqual(by_path["new.txt"].kind, "added")
        self.assertFalse(by_path["new.txt"].destructive)
        self.assertEqual(by_path["old.txt"].kind, "remote-only")
        self.assertFalse(by_path["old.txt"].destructive)
        first = stacks.plan_hash(changes)
        second = stacks.plan_hash(stacks.plan_changes(local, remote))
        self.assertEqual(first, second)
        self.assertNotEqual(first, stacks.plan_hash(stacks.plan_changes({"docker-compose.yml": "c"}, remote)))

    def test_sync_state_distinguishes_extra_from_drift(self):
        services, _, _ = stacks.parse_compose(COMPOSE)
        stack = stacks.Stack(host="pi3", name="demo", path=Path("/tmp/demo"), compose_file="docker-compose.yml",
                             text=COMPOSE, services=services, project="demo",
                             files={"a": "1"}, hashes={"a": util.sha256_text("1")})
        synced = stacks.sync_state(stack, {"a": util.sha256_text("1")})
        self.assertEqual(synced["state"], "synced")
        extra = stacks.sync_state(stack, {"a": util.sha256_text("1"), "runtime.db": "x"})
        self.assertEqual(extra["state"], "extra")
        drift = stacks.sync_state(stack, {"a": util.sha256_text("2")})
        self.assertEqual(drift["state"], "drift")
        missing = stacks.sync_state(stack, {})
        self.assertEqual(missing["state"], "not-deployed")

    def test_replace_image_preserves_comment_and_quotes(self):
        new = stacks_updates_replace(COMPOSE, "web", "nginx:1.30.0")
        self.assertIn('image: "nginx:1.30.0"   # pinned on purpose', new)
        self.assertIn("image: repo/app:${TAG:-latest}", new)
        with self.assertRaises(updates.UpdateError):
            updates.replace_image(COMPOSE, "web", "bad ref\nrm -rf /")
        with self.assertRaises(updates.UpdateError):
            updates.replace_image(COMPOSE, "missing", "nginx:1")

    def test_replace_env_preserves_quotes_and_comment(self):
        text = "A=1\nDB_PASSWORD='old' # keep\n"
        new = updates.replace_env_value(text, "DB_PASSWORD", "new")
        self.assertIn("DB_PASSWORD='new' # keep", new)
        appended = updates.replace_env_value(text, "NEW_KEY", "x")
        self.assertIn("NEW_KEY=x", appended)
        with self.assertRaises(updates.UpdateError):
            updates.replace_env_value(text, "A", "multi\nline")


def stacks_updates_replace(text: str, service: str, ref: str) -> str:
    return updates.replace_image(text, service, ref)


class ReleaseTests(unittest.TestCase):
    def test_version_compare(self):
        self.assertEqual(releases.compare_versions("2.43.4-69d73b9", "2.44.3-bfbeea9"), 1)
        self.assertEqual(releases.compare_versions("v3.3.3", "v3.3.3"), 0)
        self.assertEqual(releases.compare_versions("2026.10.17-1c10abe2d", "2026.10.27-cb74e7f81"), 1)
        self.assertEqual(releases.compare_versions("1.26.1-0", "1.26.2-0"), 1)
        self.assertEqual(releases.compare_versions("bogus", "1.0.0"), None)
        self.assertEqual(releases.status_for("", "1.0.0"), "unpinned")
        self.assertEqual(releases.status_for("latest", "1.0.0"), "unpinned")
        self.assertEqual(releases.status_for("1.0.0", "1.1.0"), "update-available")
        self.assertEqual(releases.status_for("1.0.0", "1.0.0"), "current")
        self.assertEqual(releases.status_for("2.0.0", "1.0.0"), "ahead-of-stable")

    def test_pick_latest_avoids_variants_and_prereleases(self):
        tags = [
            {"name": "nightly"},
            {"name": "2.45.4-pc"},
            {"name": "2.45.4-e0c02a6"},
            {"name": "2.45.4"},
            {"name": "2.45.4-arm64"},
            {"name": "2.44.7"},
            {"name": "v2.46.0-rc.1"},
        ]
        self.assertEqual(releases.pick_latest_tag(tags, ""), "2.45.4")
        alpine = [{"name": "2.11.7-alpine"}, {"name": "2.11.7"}, {"name": "2.11.6-alpine"}]
        self.assertEqual(releases.pick_latest_tag(alpine, "2.11.6-alpine"), "2.11.7-alpine")
        self.assertEqual(releases.pick_latest_tag(alpine, "2.11.6"), "2.11.7")

    def test_arch_normalization_and_platform_index(self):
        self.assertEqual(releases.normalize_arch("aarch64"), "arm64")
        self.assertEqual(releases.normalize_arch("x86_64"), "amd64")
        self.assertEqual(releases.normalize_arch("armv7l"), "armv7")
        self.assertEqual(releases.normalize_arch(""), "")
        tags = [
            {
                "name": "2.11.9",
                "images": [{"os": "linux", "architecture": "amd64"}],
                "last_updated": "2026-11-05T20:53:25Z",
            },
            {
                "name": "2.11.9-alpine",
                "images": [{"os": "linux", "architecture": "amd64"}],
                "last_updated": "2026-11-05T20:53:27Z",
            },
            {
                "name": "2.11.8",
                "images": [
                    {"os": "linux", "architecture": "amd64"},
                    {"os": "linux", "architecture": "arm64", "variant": "v8"},
                    {"os": "linux", "architecture": "arm", "variant": "v7"},
                ],
                "last_updated": "2026-11-05T08:52:45Z",
            },
            {
                "name": "2.11.8-alpine",
                "images": [
                    {"os": "linux", "architecture": "amd64"},
                    {"os": "linux", "architecture": "arm64", "variant": "v8"},
                ],
                "last_updated": "2026-11-05T08:52:47Z",
            },
            {"name": "2.11.9-windowsservercore", "images": [{"os": "windows", "architecture": "amd64"}]},
        ]
        latest, published = releases.platform_tag_index(tags)
        self.assertEqual(latest["plain:amd64"], "2.11.9")
        self.assertEqual(latest["plain:arm64"], "2.11.8")
        self.assertEqual(latest["plain:armv7"], "2.11.8")
        self.assertEqual(latest["alpine:amd64"], "2.11.9-alpine")
        self.assertEqual(latest["alpine:arm64"], "2.11.8-alpine")
        self.assertEqual(published["plain:amd64"], releases.parse_timestamp("2026-11-05T20:53:25Z"))

        entry = releases.ReleaseEntry(
            key="caddy", kind="dockerhub", label="Caddy", latest="2.11.9",
            extra={
                "latest_by_platform": latest,
                "latest_published_by_platform": published,
            },
        )
        self.assertEqual(releases.tag_latest_for_arch(entry, "2.11.8", "arm64")[0], "2.11.8")
        self.assertEqual(releases.tag_latest_for_arch(entry, "2.11.8-alpine", "arm64")[0], "2.11.8-alpine")
        self.assertEqual(releases.tag_latest_for_arch(entry, "2.11.8", "amd64")[0], "2.11.9")
        self.assertFalse(releases.tag_latest_for_arch(entry, "2.11.8", "")[2])

    def test_calendar_tags(self):
        tags = [
            {"name": "latest"},
            {"name": "2026.10.27-cb74e7f81"},
            {"name": "2026.10.17-1c10abe2d"},
            {"name": "2026.9.30-aaaaaaa"},
        ]
        self.assertEqual(releases.pick_latest_tag(tags, "2026.10.17-1c10abe2d", calendar=True), "2026.10.27-cb74e7f81")

    def test_timestamp_parsing(self):
        self.assertEqual(releases.parse_timestamp("2026-10-27T09:41:00Z"), 1793094060)
        self.assertEqual(releases.parse_timestamp(""), 0)
        self.assertEqual(releases.parse_timestamp("not-a-date"), 0)

    def test_catalog_managed_and_alias(self):
        # Redis is only compatibility-managed inside an Immich stack; the bare
        # image must remain updatable for everyone else.
        self.assertEqual(releases.lookup("docker.io/redis").kind, "dockerhub")
        self.assertEqual(releases.lookup("ghcr.io/immich-app/immich-server").group, "immich")
        n8n = releases.lookup("docker.n8n.io/n8nio/n8n")
        self.assertEqual(n8n.key, "n8nio/n8n")
        self.assertEqual(n8n.hub_repo, "n8nio/n8n")
        self.assertEqual(releases.lookup("caddy").origin, "catalog")
        self.assertEqual(releases.lookup("gitea/gitea").origin, "auto")

    def test_redis_group_scope_and_override_priority(self):
        self.assertIsNone(releases.group_managed_source("redis", set()))
        self.assertEqual(releases.group_managed_source("redis", {"immich"}).kind, "managed")
        key, source = releases.validate_source_override(
            "redis", {"kind": "dockerhub", "hub_repo": "library/redis"}
        )
        self.assertIsNone(releases.group_managed_source("redis", {"immich"}, {key: source}))

    def test_lscr_and_foreign_registries_are_not_guessed_as_docker_hub(self):
        lscr = releases.lookup("lscr.io/linuxserver/jellyfin")
        self.assertEqual(lscr.kind, "dockerhub")
        self.assertEqual(lscr.hub_repo, "linuxserver/jellyfin")
        self.assertEqual(lscr.key, "linuxserver/jellyfin")
        self.assertEqual(releases.lookup("quay.io/org/app").kind, "unknown")
        self.assertEqual(releases.lookup("registry.example:5000/team/app").kind, "unknown")

    def test_architecture_namespace_stays_on_docker_hub(self):
        # Official per-arch images carry an OCI source label pointing at the
        # upstream GitHub project.  Their Docker Hub tag list is the authority.
        labels = {"org.opencontainers.image.source": "https://github.com/caddyserver/caddy-docker"}
        arm = releases.lookup("arm64v8/caddy", labels=labels)
        self.assertEqual(arm.kind, "dockerhub")
        self.assertEqual(arm.hub_repo, "arm64v8/caddy")
        self.assertEqual(arm.label, "caddy")
        self.assertEqual(releases.lookup("amd64/nginx").hub_repo, "amd64/nginx")

    def test_custom_release_source_overrides_catalog_and_auto_detection(self):
        key, source = releases.validate_source_override(
            "ghcr.io/paperless-ngx/paperless-ngx",
            {"kind": "github", "github_repo": "paperless-ngx/paperless-ngx", "label": "Paperless"},
        )
        self.assertEqual(key, "ghcr.io/paperless-ngx/paperless-ngx")
        overrides = {key: source}
        resolved = releases.lookup("ghcr.io/paperless-ngx/paperless-ngx", overrides=overrides)
        self.assertEqual(resolved.kind, "github")
        self.assertEqual(resolved.github_repo, "paperless-ngx/paperless-ngx")
        self.assertEqual(resolved.origin, "custom")

    def test_source_validation_and_library_alias(self):
        with self.assertRaises(ValueError):
            releases.validate_source_override("ghcr.io/owner/app", {"kind": "nonsense"})
        with self.assertRaises(ValueError):
            releases.validate_source_override("ghcr.io/owner/app", {"kind": "dockerhub"})
        key, source = releases.validate_source_override(
            "redis", {"kind": "dockerhub", "hub_repo": "library/redis"}
        )
        self.assertEqual(
            releases.lookup("docker.io/library/redis", overrides={key: source}).origin,
            "custom",
        )
        self.assertEqual(releases.lookup("ghcr.io/owner/app").suggested_github_repo, "owner/app")


class TerminalAndDeletePathTests(unittest.TestCase):
    def test_terminal_command_is_interactive_and_lands_in_path(self):
        host = Host(alias="pi", user="pi", address="192.0.2.10", port=2222, key="/tmp/key")
        command = sshkeys.terminal_command(host, "/opt/stacks/my app", key=Path("/tmp/key"))
        self.assertTrue(command.startswith("ssh -t "))
        self.assertIn("pi@192.0.2.10", command)
        self.assertIn("-p 2222", command)
        self.assertNotIn("BatchMode", command)
        self.assertNotIn("ControlPath", command)
        self.assertIn("/opt/stacks/my app", command)
        self.assertIn("$SHELL -l", command)

    def test_local_terminal_command_does_not_ssh(self):
        command = sshkeys.terminal_command(Host(alias="mac", local=True), "/tmp/demo")
        self.assertTrue(command.startswith("cd "))
        self.assertNotIn("ssh ", command)

    def test_remote_delete_paths_are_relative_and_deduplicated(self):
        cleaned = stacks.clean_remote_delete_paths(
            ["config/a", "./config/a", "data/b"], "docker-compose.yml"
        )
        self.assertEqual(cleaned, ["config/a", "data/b"])

    def test_remote_delete_paths_reject_escapes_and_compose(self):
        for paths in (
            ["../escape"],
            ["/etc/passwd"],
            ["docker-compose.yml"],
            [""],
            [],
            "config/a",
        ):
            with self.assertRaises(stacks.StackError, msg=repr(paths)):
                stacks.clean_remote_delete_paths(paths, "docker-compose.yml")


class HostsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.settings = make_repo(self.root)
        self.old_ssh = os.environ.get("HLC_SSH_DIR")
        os.environ["HLC_SSH_DIR"] = str(self.root / "ssh")

    def tearDown(self):
        if self.old_ssh is None:
            os.environ.pop("HLC_SSH_DIR", None)
        else:
            os.environ["HLC_SSH_DIR"] = self.old_ssh
        self.tmp.cleanup()

    def test_add_remove_round_trip_keeps_metadata(self):
        store = HostStore(self.settings)
        host = store.add("pi4", "10.0.0.4", "pi", 2222, tags="dns, primary", notes="new")
        self.assertEqual(host.tags, ["dns", "primary"])
        raw = json.loads(self.settings.hosts_file.read_text())
        self.assertEqual(raw["location_"], "scripts/hosts.json")
        self.assertEqual(raw["pi4"]["port"], 2222)
        store.remove("pi4")
        raw = json.loads(self.settings.hosts_file.read_text())
        self.assertNotIn("pi4", raw)
        self.assertIn("location_", raw)
        reloaded = HostStore(self.settings)
        self.assertEqual(reloaded.aliases(), ["pi3"])

    def test_validation(self):
        store = HostStore(self.settings)
        with self.assertRaises(HostError):
            store.add("Bad Alias", "10.0.0.4")
        # Real accounts, not the Raspberry Pi default: uppercase and dotted
        # usernames are valid on Linux and macOS.
        for user in ("first.last", "First.Last", "JohnDoe", "deploy-bot", "_svc"):
            self.assertEqual(validate_user(user), user)
        for user in ("bad user", "-bad", "user@host"):
            with self.assertRaises(HostError):
                validate_user(user)
        with self.assertRaises(HostError):
            store.add("pi4", "-oProxyCommand=evil")
        with self.assertRaises(HostError):
            store.add("pi4", "10.0.0.4", port=99999)
        with self.assertRaises(HostError):
            store.add("pi3", "10.0.0.5")

    def test_ssh_config_updates_stanza_in_place(self):
        ssh_dir = Path(os.environ["HLC_SSH_DIR"])
        ssh_dir.mkdir(parents=True, exist_ok=True)
        config = ssh_dir / "config"
        config.write_text(
            "Host other\n  HostName 1.2.3.4\n\nHost pi3\n  HostName 9.9.9.9\n  User pi\n  ForwardAgent yes\n"
        )
        host = Host(alias="pi3", user="pi", address="10.0.0.3", key=str(ssh_dir / "id_ed25519"))
        result = upsert_ssh_config(host)
        self.assertTrue(result["changed"])
        text = config.read_text()
        self.assertIn("Host pi3", text)
        self.assertIn("HostName 10.0.0.3", text)
        self.assertIn("ForwardAgent yes", text)
        self.assertIn(f"IdentityFile {ssh_dir / 'id_ed25519'}", text)
        self.assertIn("Host other", text)
        self.assertTrue(Path(result["backup"]).exists())
        # second call is a no-op
        self.assertFalse(upsert_ssh_config(host)["changed"])


class UpdateTargetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.settings = make_repo(self.root)
        self.service = releases.ReleaseService(self.settings)

    def tearDown(self):
        self.tmp.cleanup()

    def _entry(self, key: str, latest: str, variants=None, published_at=0, published_variants=None,
               platforms=None, published_platforms=None):
        return releases.ReleaseEntry(
            key=key, kind="github", label=key, latest=latest, published_at=published_at,
            checked_at=util.now_ts(), source_name="test",
            extra={
                "latest_by_variant": variants or {},
                "latest_published_by_variant": published_variants or {},
                "latest_by_platform": platforms or {},
                "latest_published_by_platform": published_platforms or {},
            },
        )

    def test_env_pin_merges_immich_services(self):
        stack_dir = self.root / "stacks" / "pi3" / "immich"
        stack_dir.mkdir()
        (stack_dir / "docker-compose.yml").write_text(
            """name: immich
services:
  immich-server:
    image: ghcr.io/immich-app/immich-server:${IMMICH_VERSION:-release}
  immich-machine-learning:
    image: ghcr.io/immich-app/immich-machine-learning:${IMMICH_VERSION:-release}
  redis:
    image: docker.io/redis:6.2-alpine
  database:
    image: ghcr.io/immich-app/postgres:${POSTGRES_VERSION:-16}
"""
        )
        (stack_dir / ".env").write_text("IMMICH_VERSION=v3.3.3\nPOSTGRES_VERSION=14-x\n")
        stack = stacks.get_stack(self.settings, "pi3", "immich")
        released = util.now_ts() - 3600
        self.service.entries["ghcr.io/immich-app/immich-server"] = self._entry(
            "ghcr.io/immich-app/immich-server", "v3.3.5", published_at=released
        )
        self.service.entries["ghcr.io/immich-app/immich-machine-learning"] = self._entry(
            "ghcr.io/immich-app/immich-machine-learning", "v3.3.5", published_at=released
        )
        runtime = {
            "containers": [
                {"service": "immich-server", "image_tag": "v3.3.3", "repo_digests": [], "state": "running"},
                {"service": "immich-machine-learning", "image_tag": "v3.3.3", "repo_digests": [], "state": "running"},
            ]
        }
        targets = updates.build_targets(stack, runtime, self.service)
        server = next(item for item in targets if item["service"] == "immich-server")
        self.assertEqual(server["status"], "update-available")
        self.assertEqual(server["mechanism"], "env")
        self.assertEqual(server["file"], ".env")
        self.assertEqual(server["new_value"], "v3.3.5")
        self.assertEqual(server["latest_published"], released)
        self.assertGreaterEqual(server["latest_age_s"], 3600)
        self.assertEqual(server["services_affected"], ["immich-server", "immich-machine-learning"])
        self.assertEqual(server["safety"]["level"], "critical")
        self.assertTrue(server["safety"]["database_backup"])
        self.assertEqual([item["service"] for item in targets].count("immich-server"), 1)
        database = next(item for item in targets if item["service"] == "database")
        self.assertEqual(database["status"], "managed")
        self.assertEqual(database["actions"], [])
        redis = next(item for item in targets if item["service"] == "redis")
        self.assertEqual(redis["status"], "managed")

    def test_redis_is_checked_outside_immich(self):
        stack_dir = self.root / "stacks" / "pi3" / "vault"
        stack_dir.mkdir()
        (stack_dir / "docker-compose.yml").write_text(
            "services:\n  vaultwarden:\n    image: vaultwarden/server:1.32.7\n  cache:\n    image: redis:7.4.1\n"
        )
        stack = stacks.get_stack(self.settings, "pi3", "vault")
        self.service.entries["redis"] = self._entry("redis", "7.4.9")
        runtime = {
            "containers": [
                {"service": "vaultwarden", "image_tag": "1.32.7", "repo_digests": [], "state": "running"},
                {"service": "cache", "image_tag": "7.4.1", "repo_digests": [], "state": "running"},
            ]
        }
        targets = {item["service"]: item for item in updates.build_targets(stack, runtime, self.service)}
        self.assertEqual(targets["cache"]["status"], "update-available")
        self.assertIn("update", targets["cache"]["actions"])
        self.assertEqual(targets["cache"]["source"]["kind"], "dockerhub")

    def test_workspace_profile_controls_target_safety(self):
        stack_dir = self.root / "stacks" / "pi3" / "paperless"
        stack_dir.mkdir()
        (stack_dir / "docker-compose.yml").write_text(
            "services:\n  app:\n    image: ghcr.io/paperless-ngx/paperless-ngx:2.10.0\n"
        )
        self.settings.values["stack_profiles"] = {
            "pi3/paperless": {
                "safety": {"level": "critical", "backup": "Dump the database first."},
                "database": {"label": "Paperless Postgres", "container": "db", "upload_env": "DATA_DIR"},
            }
        }
        stack = stacks.get_stack(self.settings, "pi3", "paperless")
        target = updates.build_targets(stack, {"containers": []}, self.service)[0]
        self.assertEqual(target["safety"]["level"], "critical")
        self.assertTrue(target["safety"]["database_backup"])
        self.assertEqual(target["safety"]["backup"], "Dump the database first.")

    def test_variant_release_date_is_exposed(self):
        stack_dir = self.root / "stacks" / "pi3" / "caddy"
        stack_dir.mkdir()
        (stack_dir / "docker-compose.yml").write_text(
            "services:\n  web:\n    image: caddy:2.11.6-alpine\n"
        )
        stack = stacks.get_stack(self.settings, "pi3", "caddy")
        released = util.now_ts() - 7200
        self.service.entries["caddy"] = self._entry(
            "caddy", "2.11.7", variants={"alpine": "2.11.7-alpine"},
            published_at=1, published_variants={"alpine": released},
        )
        runtime = {"containers": [{"service": "web", "image_tag": "2.11.6-alpine", "repo_digests": [], "state": "running"}]}
        target = updates.build_targets(stack, runtime, self.service)[0]
        self.assertEqual(target["latest"], "2.11.7-alpine")
        self.assertEqual(target["latest_published"], released)

    def test_arch_incompatible_latest_is_not_proposed(self):
        stack_dir = self.root / "stacks" / "pi3" / "caddy"
        stack_dir.mkdir()
        (stack_dir / "docker-compose.yml").write_text(
            "services:\n  web:\n    image: caddy:2.11.8\n"
        )
        stack = stacks.get_stack(self.settings, "pi3", "caddy")
        self.service.entries["caddy"] = self._entry(
            "caddy", "2.11.9",
            platforms={"plain:amd64": "2.11.9", "plain:arm64": "2.11.8"},
            published_platforms={"plain:amd64": 200, "plain:arm64": 100},
        )
        runtime = {"containers": [{"service": "web", "image_tag": "2.11.8", "repo_digests": [], "state": "running"}]}

        arm = updates.build_targets(stack, runtime, self.service, host_arch="aarch64")[0]
        self.assertEqual(arm["platform"], "arm64")
        self.assertEqual(arm["latest"], "2.11.8")
        self.assertEqual(arm["upstream_latest"], "2.11.9")
        self.assertEqual(arm["status"], "current")
        self.assertTrue(arm["platform_limited"])
        self.assertEqual(arm["actions"], [])
        self.assertEqual(arm["latest_published"], 100)
        self.assertIn("2.11.9 has no linux/arm64 image", " ".join(arm["notes"]))

        amd = updates.build_targets(stack, runtime, self.service, host_arch="x86_64")[0]
        self.assertEqual(amd["platform"], "amd64")
        self.assertEqual(amd["latest"], "2.11.9")
        self.assertEqual(amd["status"], "update-available")
        self.assertEqual(amd["actions"], ["update"])
        self.assertFalse(amd["platform_limited"])

    def test_arch_unavailable_and_newer_compatible_tag(self):
        stack_dir = self.root / "stacks" / "pi3" / "caddy"
        stack_dir.mkdir()
        (stack_dir / "docker-compose.yml").write_text(
            "services:\n  web:\n    image: caddy:2.11.7\n"
        )
        stack = stacks.get_stack(self.settings, "pi3", "caddy")
        runtime = {"containers": [{"service": "web", "image_tag": "2.11.7", "repo_digests": [], "state": "running"}]}
        self.service.entries["caddy"] = self._entry(
            "caddy", "2.11.9",
            platforms={"plain:amd64": "2.11.9", "plain:arm64": "2.11.8"},
        )
        target = updates.build_targets(stack, runtime, self.service, host_arch="aarch64")[0]
        self.assertEqual(target["latest"], "2.11.8")
        self.assertEqual(target["status"], "update-available")
        self.assertTrue(target["platform_limited"])
        self.assertEqual(target["actions"], ["update"])
        self.assertEqual(target["new_value"], "caddy:2.11.8")

        # A newer tag exists but no tag has an arm64 image at all.
        self.service.entries["caddy"] = self._entry(
            "caddy", "2.11.9", platforms={"plain:amd64": "2.11.9"}
        )
        blocked = updates.build_targets(stack, runtime, self.service, host_arch="aarch64")[0]
        self.assertEqual(blocked["latest"], "")
        self.assertEqual(blocked["upstream_latest"], "2.11.9")
        self.assertEqual(blocked["status"], "arch-unavailable")
        self.assertEqual(blocked["actions"], [])
        self.assertTrue(blocked["platform_limited"])

    def test_arch_specific_image_tracks_its_docker_hub_tags(self):
        stack_dir = self.root / "stacks" / "pi3" / "caddy"
        stack_dir.mkdir()
        (stack_dir / "docker-compose.yml").write_text(
            "services:\n  web:\n    image: arm64v8/caddy:2.11.9\n"
        )
        stack = stacks.get_stack(self.settings, "pi3", "caddy")
        self.service.entries["arm64v8/caddy"] = self._entry(
            "arm64v8/caddy", "2.11.9", platforms={"plain:arm64": "2.11.9"}
        )
        runtime = {"containers": [{"service": "web", "image_tag": "2.11.9", "repo_digests": [], "state": "running"}]}
        arm = updates.build_targets(stack, runtime, self.service, host_arch="aarch64")[0]
        self.assertEqual(arm["source"]["key"], "arm64v8/caddy")
        self.assertEqual(arm["source"]["kind"], "dockerhub")
        self.assertEqual(arm["status"], "current")
        self.assertFalse(arm["platform_limited"])

        amd = updates.build_targets(stack, runtime, self.service, host_arch="x86_64")[0]
        self.assertEqual(amd["status"], "arch-unavailable")

    def test_unpinned_image_offers_pin(self):
        stack_dir = self.root / "stacks" / "pi3" / "app"
        stack_dir.mkdir()
        (stack_dir / "docker-compose.yml").write_text(
            "services:\n  app:\n    image: n8nio/n8n\n"
        )
        stack = stacks.get_stack(self.settings, "pi3", "app")
        self.service.entries["n8nio/n8n"] = self._entry("n8nio/n8n", "2.45.4")
        runtime = {"containers": [{"service": "app", "image_tag": "latest", "repo_digests": [], "state": "running"}]}
        target = updates.build_targets(stack, runtime, self.service)[0]
        self.assertEqual(target["status"], "unpinned")
        self.assertIn("pin-latest", target["actions"])
        self.assertEqual(target["new_value"], "n8nio/n8n:2.45.4")


class BackupPlanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.settings = make_repo(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_only_known_stacks_have_a_builtin_plan(self):
        self.assertEqual(backups.plan_for("immich")["container"], "immich_postgres")
        self.assertIsNone(backups.plan_for("n8n"))

    def test_workspace_profile_can_add_and_disable_database_plans(self):
        self.settings.values["stack_profiles"] = {
            "pi3/paperless": {
                "safety": {"level": "critical", "backup": "Dump the database first."},
                "database": {
                    "label": "Paperless Postgres",
                    "container": "paperless-db",
                    "user_key": "POSTGRES_USER",
                    "upload_env": "DATA_DIR",
                },
            },
            "pi3/immich": {"safety": {"backup": "Custom note"}, "database": None},
        }
        plan = backups.plan_for("paperless", self.settings, "pi3")
        self.assertEqual(plan["container"], "paperless-db")
        self.assertEqual(plan["scheduled_subdir"], "backups")
        safety = updates.safety_for("paperless", self.settings, "pi3")
        self.assertEqual(safety["level"], "critical")
        self.assertTrue(safety["database_backup"])
        self.assertIsNone(backups.plan_for("immich", self.settings, "pi3"))
        immich_safety = updates.safety_for("immich", self.settings, "pi3")
        self.assertEqual(immich_safety["level"], "critical")
        self.assertEqual(immich_safety["backup"], "Custom note")
        self.assertFalse(immich_safety["database_backup"])

    def test_custom_database_plan_drives_the_dump_command(self):
        stack_dir = self.root / "stacks" / "pi3" / "paperless"
        stack_dir.mkdir()
        (stack_dir / "docker-compose.yml").write_text("services:\n  app:\n    image: postgres:16.4\n")
        (stack_dir / ".env").write_text("POSTGRES_USER=puser\nDATA_DIR=/srv/paperless\n")
        self.settings.values["stack_profiles"] = {
            "pi3/paperless": {
                "database": {
                    "label": "Paperless DB",
                    "container": "custom-db",
                    "user_key": "POSTGRES_USER",
                    "upload_env": "DATA_DIR",
                    "console_subdir": "console-dumps",
                    "console_prefix": "paperless-",
                }
            }
        }
        stack = stacks.get_stack(self.settings, "pi3", "paperless")
        host = Host(alias="pi3", user="pi", address="10.0.0.3", key="/tmp/key")
        completed = unittest.mock.MagicMock(
            returncode=0, stdout="100|1700000000|/srv/paperless/console-dumps/paperless-1.sql.gz\n", stderr=""
        )
        with unittest.mock.patch("homelab_console.backups.util.run", return_value=completed) as run:
            result = backups.dump_database(host, stack, settings=self.settings)
        argv = run.call_args.args[0]
        command = argv[-1]
        self.assertIn("docker exec custom-db pg_dumpall", command)
        self.assertIn("-U puser", command)
        self.assertIn("/srv/paperless/console-dumps", command)
        self.assertEqual(result["backup"]["bytes"], 100)

    def test_profile_validation_rejects_unknown_fields_and_bad_paths(self):
        with self.assertRaises(ValueError):
            profiles.validate_stack_profile({"safety": {"level": "very-high"}})
        with self.assertRaises(ValueError):
            profiles.validate_stack_profile({"safety": {}, "unknown": True})
        with self.assertRaises(ValueError):
            profiles.validate_stack_profile({"database": {"scheduled_subdir": "../escape"}})
        with self.assertRaises(ValueError):
            profiles.validate_stack_profile({"database": {"user_key": "BAD KEY"}})

    def test_scheduled_listing_parser(self):
        output = "\n".join([
            "1793080872.5641409180|341496870|immich-db-backup-20261027T020000-v3.3.3-pg14.19.sql.gz",
            "1792994472.9504661570|341484802|immich-db-backup-20261026T020000-v3.3.3-pg14.19.sql.gz",
            "garbage",
        ])
        rows = backups.parse_scheduled_listing(output)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["name"], "immich-db-backup-20261027T020000-v3.3.3-pg14.19.sql.gz")
        self.assertEqual(rows[0]["bytes"], 341496870)
        self.assertEqual(rows[0]["version"], "v3.3.3")
        self.assertEqual(rows[0]["created"], 1793080872)


class BulkQueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.settings = make_repo(self.root)
        self.hosts = HostStore(self.settings)
        self.store = unittest.mock.MagicMock()
        self.monitor = type("Monitor", (), {
            "state": lambda _self: {
                "hosts": [{"alias": "pi3"}],
                "updates": [
                    {"host": "pi3", "stack": "caddy", "service": "caddy", "current_display": "2.11.6", "latest": "2.11.7", "actions": ["update"], "safety": {"level": "medium"}},
                    {"host": "pi3", "stack": "immich", "service": "immich-server", "current_display": "v3.3.3", "latest": "v3.3.5", "actions": ["update"], "safety": {"level": "critical"}},
                    {"host": "pi3", "stack": "demo", "service": "web", "current_display": "1.0.0", "latest": "1.1.0", "actions": ["update"], "safety": {"level": "low"}},
                ],
            },
            "request_refresh": lambda _self: None,
        })()
        self.manager = TaskManager(self.settings, self.store, self.hosts, self.monitor)

    def tearDown(self):
        self.tmp.cleanup()

    def _wait(self, batch_id):
        deadline = time.time() + 3
        while time.time() < deadline:
            batch = self.manager.get_batch(batch_id)
            if batch and batch.status not in ("queued", "running"):
                return batch
            time.sleep(0.01)
        self.fail("bulk queue did not finish")

    def test_bulk_is_sequential_and_excludes_critical(self):
        order = []
        active = 0
        max_active = 0
        guard = threading.Lock()

        def fake_update(host, stack, service, action, **_kwargs):
            nonlocal active, max_active
            order.append((host, stack, service))
            with guard:
                active += 1
                max_active = max(max_active, active)
            run = Run(id=f"run-{len(order)}", kind="update", host=host, svc=stack, title="test")
            def finish():
                nonlocal active
                time.sleep(0.05)
                with guard:
                    active -= 1
                run.finish("ok")
            threading.Thread(target=finish, daemon=True).start()
            return run

        self.manager.update = fake_update
        batch = self.manager.start_bulk("fleet")
        finished = self._wait(batch.id)
        self.assertEqual(finished.status, "ok")
        self.assertEqual([item["status"] for item in finished.public()["items"]], ["ok", "ok"])
        self.assertEqual([item["stack"] for item in finished.public()["items"]], ["caddy", "demo"])
        self.assertEqual(len(finished.public()["excluded"]), 1)
        self.assertEqual(order, [("pi3", "caddy", "caddy"), ("pi3", "demo", "web")])
        self.assertEqual(max_active, 1)

    def test_bulk_stops_after_first_failure(self):
        calls = []

        def fake_update(host, stack, service, action, **_kwargs):
            calls.append(stack)
            run = Run(id="run-fail", kind="update", host=host, svc=stack, title="test")
            if stack == "caddy":
                run.finish("failed", error="health check failed")
            else:
                run.finish("ok")
            return run

        self.manager.update = fake_update
        batch = self.manager.start_bulk("host", "pi3")
        finished = self._wait(batch.id)
        self.assertEqual(finished.status, "failed")
        statuses = [item["status"] for item in finished.public()["items"]]
        self.assertEqual(statuses, ["failed", "skipped"])
        self.assertEqual(calls, ["caddy"])


class FileSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.settings = make_repo(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_write_file_rejects_traversal_and_keeps_dotenv(self):
        stacks.write_file(self.settings, "pi3", "demo", ".env", "A=1\n")
        with self.assertRaises(stacks.StackError):
            stacks.write_file(self.settings, "pi3", "demo", "../evil.txt", "x")
        with self.assertRaises(stacks.StackError):
            stacks.write_file(self.settings, "pi3", "demo", "/tmp/evil", "x")
        with self.assertRaises(stacks.StackError):
            stacks.write_file(self.settings, "pi3", "demo", "nested/../evil", "x")
        with self.assertRaises(stacks.StackError):
            stacks.write_file(self.settings, "pi3", "demo", "docker-compose.yml", "not: [valid")

    def test_write_file_creates_nested_parents_and_protects_env(self):
        result = stacks.write_file(
            self.settings, "pi3", "demo", "config/prometheus/prometheus.yml", "scrape_configs: []\n"
        )
        self.assertTrue(result["ok"])
        target = self.root / "stacks" / "pi3" / "demo" / "config" / "prometheus" / "prometheus.yml"
        self.assertEqual(target.read_text(), "scrape_configs: []\n")
        self.assertEqual(target.stat().st_mode & 0o777, 0o644)

        stacks.write_file(self.settings, "pi3", "demo", "config/.env.local", "SECRET=1\n")
        env = self.root / "stacks" / "pi3" / "demo" / "config" / ".env.local"
        self.assertEqual(env.stat().st_mode & 0o777, 0o600)

        stacks.write_file(self.settings, "pi3", "demo", "config/prod.env", "SECRET=2\n")
        suffix_env = self.root / "stacks" / "pi3" / "demo" / "config" / "prod.env"
        self.assertEqual(suffix_env.stat().st_mode & 0o777, 0o600)

        with self.assertRaises(stacks.StackError):
            stacks.write_file(self.settings, "pi3", "demo", "config/.git/config", "x")
        with self.assertRaises(stacks.StackError):
            stacks.write_file(self.settings, "pi3", "demo", ".gitignore", "x")

    def test_create_only_refuses_to_overwrite(self):
        stacks.write_file(self.settings, "pi3", "demo", "config/app.yml", "one\n", create_only=True)
        with self.assertRaises(stacks.StackError):
            stacks.write_file(self.settings, "pi3", "demo", "config/app.yml", "two\n", create_only=True)
        self.assertEqual((self.root / "stacks" / "pi3" / "demo" / "config" / "app.yml").read_text(), "one\n")

    def test_layout_warnings_find_nested_stacks_but_not_stack_assets(self):
        valid = self.root / "stacks" / "pi3" / "demo"
        (valid / "config").mkdir(exist_ok=True)
        (valid / "config" / "docker-compose.yml").write_text("services:\n  asset:\n    image: nginx:1.29.0\n")
        nested = self.root / "stacks" / "pi3" / "group" / "app"
        nested.mkdir(parents=True)
        (nested / "docker-compose.yml").write_text("services:\n  app:\n    image: nginx:1.29.0\n")
        (self.root / "stacks" / "pi3" / "compose.yml").write_text("services:\n  flat:\n    image: nginx:1.29.0\n")
        (self.root / "stacks" / "compose.yml").write_text("services:\n  root:\n    image: nginx:1.29.0\n")

        warnings = stacks.layout_warnings(self.settings)
        paths = {warning["path"] for warning in warnings}
        self.assertIn("stacks/pi3/group/app/docker-compose.yml", paths)
        self.assertIn("stacks/pi3/compose.yml", paths)
        self.assertIn("stacks/compose.yml", paths)
        self.assertFalse(any("stacks/pi3/demo/config" in path for path in paths))

    def test_create_stack_validates(self):
        text = "services:\n  x:\n    image: nginx:1.29.0\n"
        stack = stacks.create_stack(self.settings, "pi3", "newone", text, "A=1\n")
        self.assertTrue((self.root / "stacks" / "pi3" / "newone" / "docker-compose.yml").exists())
        self.assertEqual(stack.name, "newone")
        with self.assertRaises(stacks.StackError):
            stacks.create_stack(self.settings, "pi3", "Bad Name", text)
        with self.assertRaises(stacks.StackError):
            stacks.create_stack(self.settings, "pi3", "noservice", "services: {}\n")

    def test_snapshot_and_restore(self):
        stack = stacks.get_stack(self.settings, "pi3", "demo")
        target = {"service": "web", "file": "docker-compose.yml", "new_value": "nginx:1.30.0",
                  "safety": {"level": "medium"}, "actions": ["update"]}
        snap = updates.create_snapshot(self.settings, stack, target, "update")
        self.assertEqual(snap["manifest"]["target"]["value"], "nginx:1.30.0")
        compose = stack.path / "docker-compose.yml"
        composed = compose.read_text()
        compose.write_text(composed.replace("nginx:1.29.0", "nginx:1.30.0"))
        updates.restore_snapshot_files(self.settings, snap["id"], stack)
        self.assertIn("nginx:1.29.0", compose.read_text())
        rows = updates.list_snapshots(self.settings, "pi3", "demo")
        self.assertEqual(rows[0]["id"], snap["id"])
        self.assertEqual(rows[0]["target"]["value"], "nginx:1.30.0")


class PushTests(unittest.TestCase):
    """No SSH: the remote side is simulated at the util.run boundary."""

    def setUp(self):
        self.host = Host(alias="pi3", user="pi", address="10.0.0.3")
        self.files = {
            ".env": "A=1\n",
            "core-config/settings.yml": "x: 1\n",
            "docker-compose.yml": "services: {}\n",
        }

    def _push(self, *, returncode: int, stdout: str, stderr: str = ""):
        fake = subprocess.CompletedProcess(["ssh"], returncode, stdout, stderr)
        with unittest.mock.patch.object(stacks.util, "run", return_value=fake) as runner, \
                unittest.mock.patch.object(stacks.remote, "ssh_argv", return_value=["ssh"]):
            result = stacks.push_files(self.host, "demo", self.files, "/opt/stacks")
        return result, runner.call_args[0][0][-1]

    def test_success_marks_host_written(self):
        result, command = self._push(returncode=0, stdout="__HLC_PUSH_OK__")
        self.assertEqual(result, {"ok": True, "files": 3, "written": True, "repaired": False})
        # Staging + writability preflight + a sudo repair path must be present.
        self.assertIn("stage=$(mktemp -d)", command)
        self.assertIn("sudo -n chown -R", command)
        self.assertIn("__HLC_UNWRITABLE__", command)

    def test_preflight_covers_every_file_including_the_last(self):
        _result, command = self._push(returncode=0, stdout="__HLC_PUSH_OK__")
        match = re.search(r"printf %s ([A-Za-z0-9+/=]+) \| base64 -d", command)
        self.assertIsNotNone(match)
        listed = base64.b64decode(match.group(1)).decode().splitlines()
        self.assertEqual(listed, sorted(self.files))

    def test_unwritable_files_fail_before_any_write(self):
        result, _command = self._push(
            returncode=16,
            stdout="__HLC_UNWRITABLE__\ncore-config/settings.yml\n",
        )
        self.assertFalse(result["ok"])
        self.assertFalse(result["written"])
        self.assertIn("core-config/settings.yml", result["error"])
        self.assertIn("sudo chown -R pi:pi /opt/stacks/demo", result["error"])

    def test_partial_write_is_reported(self):
        result, _command = self._push(returncode=18, stdout="__HLC_WRITTEN__")
        self.assertFalse(result["ok"])
        self.assertTrue(result["written"])

    def test_ownership_repair_is_reported(self):
        result, _command = self._push(returncode=0, stdout="__HLC_CHOWNED____HLC_PUSH_OK__")
        self.assertTrue(result["ok"])
        self.assertTrue(result["repaired"])

    def test_update_push_failure_carries_written_flag(self):
        stack = unittest.mock.Mock()
        stack.files = {"docker-compose.yml": "services: {}\n"}
        stack.name = "demo"
        stack.path = Path("/tmp/demo")
        with unittest.mock.patch.object(
            stacks, "push_files", return_value={"ok": False, "error": "nope", "written": True}
        ):
            with self.assertRaises(updates.UpdateError) as caught:
                updates._push(self.host, stack, "/opt/stacks", lambda _line: None)
        self.assertTrue(caught.exception.details["written"])


class ConfigTests(unittest.TestCase):
    def test_repo_exclusions_round_trip_and_sanitize(self):
        import tempfile
        from homelab_console.config import Settings

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "code"
            repo.mkdir()
            settings = Settings.load(root)
            settings.update(
                {
                    "repo_exclusions": {
                        str(repo): ["alpha", "group/beta", "../escape", "/absolute", "alpha"],
                        "relative-root": ["ignored"],
                    }
                }
            )
            reloaded = Settings.load(root)
            self.assertEqual(reloaded.get("repo_exclusions"), {str(repo.resolve()): ["alpha", "group/beta"]})

    def test_theme_migration_and_validation(self):
        import tempfile
        from homelab_console.config import Settings

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".hlc").mkdir()
            (root / ".hlc" / "config.json").write_text('{"theme": "carbon"}')
            self.assertEqual(Settings.load(root).get("theme"), "dark")
            (root / ".hlc" / "config.json").write_text('{"theme": "light"}')
            self.assertEqual(Settings.load(root).get("theme"), "light")
            (root / ".hlc" / "config.json").write_text('{"theme": "neon"}')
            self.assertEqual(Settings.load(root).get("theme"), "dark")


class UtilTests(unittest.TestCase):
    def test_duration_and_docker_time(self):
        self.assertEqual(util.human_duration(0), "0s")
        self.assertEqual(util.human_duration(3660), "1h 1m")
        self.assertEqual(util.human_duration(90061), "1d 1h")
        ts = util.parse_docker_time("2026-09-13T04:29:53.113841037Z")
        self.assertIsNotNone(ts)
        self.assertIsNone(util.parse_docker_time("0001-01-01T00:00:00Z"))

    def test_remote_root_traversal_is_not_accepted_by_settings(self):
        # server-level validation is covered by the API; settings.load only
        # persists known keys.
        tmp = tempfile.TemporaryDirectory()
        try:
            settings = Settings.load(Path(tmp.name))
            self.assertEqual(settings.remote_root, "/opt/stacks")
        finally:
            tmp.cleanup()


class WarmupTests(unittest.TestCase):
    """The boot gate: what the splash sees, and when it lets go."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = make_repo(Path(self.tmp.name))
        self.app = AppService(self.settings)
        self.addCleanup(self.app.store.close)

    def test_unconfigured_gitea_is_not_queued(self):
        self.settings.update({"gitea_url": "not-a-url"})
        self.assertFalse(self.app.repos.gitea_configured())
        self.app._boot_started = time.time()
        payload = self.app.warmup()
        self.assertEqual([task["id"] for task in payload["tasks"]], ["fleet", "repos"])
        self.assertEqual(payload["state"], "warming")

    def test_soft_deadline_releases_a_stuck_probe(self):
        self.app._boot_started = time.time() - (WARMUP_SOFT_DEADLINE_S + 1)
        self.assertEqual(self.app.warmup()["state"], "ready")

    def test_unconfigured_repo_root_skips_the_gitea_boot_task(self):
        self.app._boot_started = time.time()
        self.app.monitor.snapshot = {"generated_at": time.time(), "summary": {"hosts_online": 1}}
        self.app.repos._snapshot["generated_at"] = time.time()
        payload = self.app.warmup()
        self.assertEqual([task["id"] for task in payload["tasks"]], ["fleet", "repos"])

    def test_warmup_reports_ready_once_every_task_is_terminal(self):
        self.settings.update({"gitea_check_on_start": False})
        self.app._boot_started = time.time()
        self.app.monitor.snapshot = {"generated_at": time.time(), "summary": {"hosts_online": 1}}
        self.app.repos._snapshot["generated_at"] = time.time()
        payload = self.app.warmup()
        self.assertEqual(payload["state"], "ready")
        self.assertEqual({task["state"] for task in payload["tasks"]}, {"done"})

    def test_startup_gitea_check_skips_when_disabled(self):
        self.settings.update({"gitea_check_on_start": False})
        with unittest.mock.patch.object(self.app.repos, "check_gitea_async") as check:
            self.app._boot_gitea()
        check.assert_not_called()

    def test_startup_gitea_check_runs_without_a_stored_token(self):
        # The credential helper is a supported auth path, so a missing token
        # must not suppress the startup check.
        self.settings.update({"gitea_check_on_start": True, "gitea_token": ""})
        self.app.repos._snapshot["root_configured"] = True
        self.assertEqual(self.settings.get("gitea_token"), "")
        self.app.repos._snapshot["generated_at"] = time.time()
        with (
            unittest.mock.patch.object(self.app.repos, "check_gitea_async", return_value=True) as check,
            unittest.mock.patch.object(self.app, "_wait_for_gitea", return_value={"errors": 0}),
        ):
            self.app._boot_gitea()
        check.assert_called_once_with()

    def test_startup_gitea_check_retries_once_after_errors(self):
        # A cold launch can fail every probe ("no route to host" in 14 ms) and
        # then work a minute later; one delayed retry clears the transient.
        self.app.repos._snapshot["root_configured"] = True
        self.app.repos._snapshot["generated_at"] = time.time()
        with (
            unittest.mock.patch.object(self.app.repos, "check_gitea_async", return_value=True) as check,
            unittest.mock.patch.object(self.app, "_wait_for_gitea", return_value={"errors": 3}),
            unittest.mock.patch.object(self.app._stop, "wait", return_value=False),
        ):
            self.app._boot_gitea()
        self.assertEqual(check.call_count, 2)


class WorkspaceTests(unittest.TestCase):
    def test_cli_init_seeds_the_example(self):
        from homelab_console import cli

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "ws"
            self.assertEqual(cli.main(["init", str(target), "--example"]), 0)
            self.assertTrue((target / "stacks" / "example" / "whoami" / "docker-compose.yml").is_file())
            self.assertTrue((target / "scripts" / "hosts.json").is_file())
            self.assertIn("example", (target / "scripts" / "hosts.json").read_text())
            self.assertIn(".hlc/", (target / ".gitignore").read_text())
            # idempotent: keeps a host the user added
            hosts = target / "scripts" / "hosts.json"
            hosts.write_text('{"location_": "scripts/hosts.json", "mine": {"host": "192.0.2.99"}}')
            self.assertEqual(cli.main(["init", str(target)]), 0)
            self.assertIn("mine", hosts.read_text())

    def test_cli_init_refuses_a_foreign_directory(self):
        from homelab_console import cli

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "not-a-workspace"
            target.mkdir()
            (target / "important.txt").write_text("keep me")
            self.assertEqual(cli.main(["init", str(target)]), 2)
            self.assertFalse((target / "stacks").exists())

    def test_cli_init_example_skips_local_state(self):
        from homelab_console import cli

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "examples" / "workspace"
            (source / "stacks" / "example" / "whoami").mkdir(parents=True)
            (source / "stacks" / "example" / "whoami" / "docker-compose.yml").write_text(
                "services:\n  whoami:\n    image: traefik/whoami:v1.11.0\n"
            )
            (source / ".hlc").mkdir()
            (source / ".hlc" / "config.json").write_text('{"gitea_token": "must-not-be-seeded"}\n')
            target = root / "ws"
            with unittest.mock.patch.object(util, "REPO_ROOT", root):
                self.assertEqual(cli.main(["init", "--example", str(target)]), 0)
            self.assertTrue((target / "stacks" / "example" / "whoami" / "docker-compose.yml").is_file())
            self.assertFalse((target / ".hlc").exists())

    """Path resolution for the packaged app: no checkout required."""

    def test_ensure_workspace_seeds_the_minimum_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = ensure_workspace(Path(tmp) / "ws")
            self.assertTrue((path / "stacks").is_dir())
            self.assertTrue((path / "scripts" / "hosts.json").is_file())
            ignore = (path / ".gitignore").read_text()
            self.assertIn(".hlc/", ignore)
            self.assertIn("**/.env", ignore)
            self.assertIn("**/.env.*", ignore)
            self.assertIn("**/*.env", ignore)
            self.assertEqual(
                json.loads((path / "scripts" / "hosts.json").read_text()),
                {"location_": "scripts/hosts.json"},
            )
            # idempotent: a second run must not clobber an edited hosts file
            (path / "scripts" / "hosts.json").write_text('{"pi3": {"host": "10.0.0.3"}}')
            ensure_workspace(path)
            self.assertIn("pi3", (path / "scripts" / "hosts.json").read_text())

    def test_workspace_env_wins_when_there_is_no_checkout(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "ws"
            env = {"HLC_WORKSPACE": str(target)}
            with unittest.mock.patch.dict(os.environ, env, clear=False):
                os.environ.pop("HLC_REPO", None)
                settings = Settings.load()
                self.assertEqual(settings.repo, target.resolve())
                self.assertTrue(settings.stacks_dir.is_dir())

    def test_pointer_at_a_checkout_root_resolves_its_nested_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            checkout = root / "homelab"
            (checkout / "app").mkdir(parents=True)
            workspace = checkout / "workspace"
            for name in ("stacks", "scripts"):
                (workspace / name).mkdir(parents=True)
            (workspace / "scripts" / "hosts.json").write_text("{}")
            # Simulate a packaged app: its REPO_ROOT is not the checkout.
            bundle = root / "bundle"
            bundle.mkdir()
            env = {"HLC_WORKSPACE": str(checkout)}
            with unittest.mock.patch.dict(os.environ, env, clear=False), \
                 unittest.mock.patch.object(util, "REPO_ROOT", bundle):
                os.environ.pop("HLC_REPO", None)
                settings = Settings.load()
            self.assertEqual(settings.repo, workspace)

    def test_legacy_pointer_at_the_checkout_redirects_to_the_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            for name in ("workspace", ""):
                base = root / name if name else root
                (base / "stacks").mkdir(parents=True, exist_ok=True)
                (base / "scripts").mkdir(parents=True, exist_ok=True)
                (base / "scripts" / "hosts.json").write_text("{}")
            env = {"HLC_WORKSPACE": str(root)}
            with unittest.mock.patch.dict(os.environ, env, clear=False), \
                 unittest.mock.patch.object(util, "REPO_ROOT", root):
                os.environ.pop("HLC_REPO", None)
                settings = Settings.load()
            self.assertEqual(settings.repo, root / "workspace")

    def test_stale_workspace_pointer_does_not_litter_a_foreign_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            foreign = root / "checkout"
            foreign.mkdir()
            (foreign / "important.txt").write_text("keep me")
            data = root / "data"
            env = {"HLC_WORKSPACE": str(foreign), "HLC_DATA_DIR": str(data)}
            with unittest.mock.patch.dict(os.environ, env, clear=False):
                os.environ.pop("HLC_REPO", None)
                settings = Settings.load()
            self.assertFalse((foreign / "stacks").exists())
            self.assertTrue((data / "workspace" / "stacks").is_dir())
            self.assertNotEqual(settings.repo, foreign)

    def test_control_dir_is_short_enough_for_a_unix_socket(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings.load(Path(tmp))
            # Longest plausible ControlPath: the dir plus "hlc-" and a 64-char
            # OpenSSH %C hash. macOS caps sun_path at 104 bytes.
            with unittest.mock.patch("homelab_console.config.get_settings", return_value=settings):
                socket_path = sshkeys.control_dir() / ("hlc-" + "a" * 64)
            self.assertLess(len(str(socket_path)), 104)

    def test_cli_repo_flag_becomes_the_process_default(self):
        # sshkeys and other helpers call config.get_settings(); a --repo run
        # must not leave them pointing at a different workspace.
        from homelab_console import cli, config

        self.addCleanup(lambda: reset_settings(Settings.load()))
        with tempfile.TemporaryDirectory() as tmp:
            make_repo(Path(tmp))
            args = type("Args", (), {"repo": tmp})()
            loaded = cli._load_settings(args)
            self.assertEqual(config.get_settings().repo, loaded.repo)


if __name__ == "__main__":
    unittest.main()
