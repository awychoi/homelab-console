"""Workspace backup/restore tests. Temp directories only; no network."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

from homelab_console import appbackup, profiles, releases  # noqa: E402
from homelab_console.config import Settings  # noqa: E402


def make_workspace(root: Path) -> Settings:
    (root / "stacks" / "pi-a" / "whoami").mkdir(parents=True)
    (root / "scripts").mkdir(parents=True)
    (root / "scripts" / "hosts.json").write_text(
        json.dumps({"location_": "scripts/hosts.json", "pi-a": {"user": "pi", "host": "192.0.2.10"}}, indent=4)
    )
    (root / "stacks" / "pi-a" / "whoami" / "docker-compose.yml").write_text("services: {}\n")
    (root / "stacks" / "pi-a" / "whoami" / ".env").write_text("TAG=v1\n")
    settings = Settings.load(root)
    settings.update({"repo_roots": [str(root / "code")], "theme": "light"})
    return settings


class BackupTests(unittest.TestCase):
    def test_archive_has_fleet_stacks_and_settings_but_no_tokens(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            settings = make_workspace(base / "ws")
            settings.update({"gitea_token": "gitea_secret_token_1234567890", "github_token": "ghp_secret"})
            (base / "target").mkdir()
            result = appbackup.create_backup(settings, base / "target" / "fleet.zip")
            self.assertTrue(result["ok"])
            self.assertTrue((base / "target" / "fleet.zip").is_file())
            with zipfile.ZipFile(base / "target" / "fleet.zip") as archive:
                names = set(archive.namelist())
                self.assertIn("manifest.json", names)
                self.assertIn("hosts.json", names)
                self.assertIn("settings.json", names)
                self.assertIn("stacks/pi-a/whoami/docker-compose.yml", names)
                self.assertIn("stacks/pi-a/whoami/.env", names)
                settings_payload = json.loads(archive.read("settings.json"))
                self.assertNotIn("gitea_token", settings_payload)
                self.assertNotIn("github_token", settings_payload)
                self.assertEqual(settings_payload["theme"], "light")
                self.assertEqual(settings_payload["repo_roots"], [str(base / "ws" / "code")])
                manifest = json.loads(archive.read("manifest.json"))
                self.assertEqual(manifest["app"], "Homelab Console")

    def test_restore_replaces_fleet_and_keeps_a_pre_restore_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            source = make_workspace(base / "source")
            appbackup.create_backup(source, base / "fleet.zip")

            target = make_workspace(base / "target")
            (target.stacks_dir / "pi-a" / "whoami" / "docker-compose.yml").write_text("services:\n  old: {}\n")
            target.update({"repo_roots": [], "theme": "dark"})
            result = appbackup.restore_backup(target, base / "fleet.zip")
            self.assertTrue(result["ok"])
            self.assertEqual(
                (target.stacks_dir / "pi-a" / "whoami" / "docker-compose.yml").read_text(),
                "services: {}\n",
            )
            hosts = json.loads(target.hosts_file.read_text())
            self.assertIn("pi-a", hosts)
            self.assertEqual(target.get("theme"), "light")
            self.assertEqual(target.get("repo_roots"), [str(base / "source" / "code")])
            self.assertTrue(Path(result["pre_restore_backup"]).is_file())

    def test_service_metadata_round_trips(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            source = make_workspace(base / "source")
            key, release_source = releases.validate_source_override(
                "ghcr.io/example/app", {"kind": "github", "github_repo": "example/app", "label": "Example"}
            )
            profile = profiles.validate_stack_profile(
                {
                    "safety": {"level": "critical", "backup": "Dump the DB."},
                    "database": {"container": "db", "upload_env": "DATA_DIR"},
                }
            )
            source.update(
                {
                    "release_sources": {key: release_source},
                    "stack_profiles": {"pi-a/whoami": profile},
                    "repo_exclusions": {str(source.repo / "code"): ["skip-me"]},
                }
            )
            appbackup.create_backup(source, base / "fleet.zip")
            with zipfile.ZipFile(base / "fleet.zip") as archive:
                payload = json.loads(archive.read("settings.json"))
                self.assertIn(key, payload["release_sources"])
                self.assertEqual(payload["stack_profiles"]["pi-a/whoami"]["safety"]["level"], "critical")
                self.assertEqual(payload["repo_exclusions"][str(source.repo / "code")], ["skip-me"])

            target = make_workspace(base / "target")
            appbackup.restore_backup(target, base / "fleet.zip")
            self.assertEqual(target.get("release_sources")[key]["github_repo"], "example/app")
            self.assertEqual(target.get("stack_profiles")["pi-a/whoami"]["database"]["container"], "db")
            self.assertEqual(target.get("repo_exclusions")[str(source.repo / "code")], ["skip-me"])

    def test_restore_rejects_path_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            settings = make_workspace(base / "ws")
            evil = base / "evil.zip"
            with zipfile.ZipFile(evil, "w") as archive:
                archive.writestr("manifest.json", json.dumps({"app": "Homelab Console"}))
                archive.writestr("hosts.json", "{}")
                archive.writestr("stacks/pi-a/whoami/docker-compose.yml", "services: {}\n")
                archive.writestr("stacks/../../escape.txt", "nope")
            with self.assertRaises(appbackup.BackupError):
                appbackup.restore_backup(settings, evil)
            self.assertFalse((base / "escape.txt").exists())

    def test_destination_must_be_a_zip(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = make_workspace(Path(tmp).resolve() / "ws")
            with self.assertRaises(appbackup.BackupError):
                appbackup.create_backup(settings, Path(tmp).resolve() / "fleet.tar")


if __name__ == "__main__":
    unittest.main()
