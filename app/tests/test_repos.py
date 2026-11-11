"""Repos workspace tests. Local temp repos and bare remotes only; no network."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

APP = Path(__file__).resolve().parents[1]
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

from homelab_console import repos  # noqa: E402
from homelab_console.config import Settings  # noqa: E402


def run(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, text=True, capture_output=True, check=True)


def make_repo(path: Path, text: str = "one") -> None:
    path.mkdir(parents=True)
    run("git", "init", "-q", cwd=path)
    run("git", "config", "user.email", "t@t", cwd=path)
    run("git", "config", "user.name", "t", cwd=path)
    (path / "f.txt").write_text(text + "\n")
    run("git", "add", "f.txt", cwd=path)
    run("git", "commit", "-qm", "initial", cwd=path)


def configure_gitea(settings: Settings, url: str = "https://gitea.invalid", owner: str = "example") -> Settings:
    """Give a test settings object a neutral, explicit Gitea target."""
    settings.values["gitea_url"] = url
    settings.values["gitea_owner"] = owner
    return settings


def repo_settings(
    base: Path,
    roots: list[Path],
    deep: list[Path] | None = None,
) -> Settings:
    settings = Settings.load(base / "console")
    settings.values["repo_roots"] = [str(root) for root in roots]
    settings.values["repo_root"] = str(roots[0]) if roots else ""
    settings.values["repo_deep_roots"] = [str(root) for root in (deep or [])]
    settings.values["repo_exclusions"] = {}
    settings.values["repo_nested_selected"] = {}
    return settings


def refs(path: Path) -> dict[str, str]:
    output = run(
        "git", "for-each-ref", "--format=%(refname) %(objectname)",
        "refs/heads", "refs/tags", cwd=path,
    ).stdout
    return dict(line.split(" ", 1) for line in output.splitlines() if line)


class StoreStub:
    def __init__(self):
        self.events: list[tuple] = []

    def add_event(self, *args, **kwargs):
        self.events.append((args, kwargs))


class DiscoveryTests(unittest.TestCase):
    def test_recursive_discovery_skips_hidden_and_generated_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve() / "repos"
            root.mkdir()
            for path in (
                root / "direct",
                root / "container" / "nested",
                root / "container" / "deep" / "too-deep",
                root / "node_modules" / "ignored",
                root / ".hidden" / "repo",
            ):
                make_repo(path)
            found = {path.relative_to(root).as_posix() for path in repos.discover_repos(root)}
        self.assertEqual(found, {"direct", "container/nested", "container/deep/too-deep"})

    def test_a_repo_root_is_treated_as_the_repo_itself(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve() / "single"
            make_repo(root)
            make_repo(root / "nested")
            found = repos.discover_repos(root)
            self.assertEqual(found, [root.resolve()])
            identity = repos.repo_identity(root.resolve(), root.resolve())
            self.assertEqual(identity["rel"], "single")
            self.assertEqual(identity["id"], "single")

    def test_discovery_stops_at_a_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve() / "repos"
            root.mkdir()
            make_repo(root / "parent")
            make_repo(root / "parent" / "child")
            found = {path.relative_to(root).as_posix() for path in repos.discover_repos(root)}
        self.assertEqual(found, {"parent"})

    def test_missing_root_is_a_user_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(repos.RepoError):
                repos.discover_repos(Path(tmp) / "nope")


class DeepScanTests(unittest.TestCase):
    def test_deep_scan_lists_nested_checkouts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve() / "repos"
            root.mkdir()
            make_repo(root / "parent")
            make_repo(root / "parent" / "child")
            shallow = {path.relative_to(root).as_posix() for path in repos.discover_repos(root)}
            deep = {
                path.relative_to(root).as_posix()
                for path in repos.discover_repos(root, deep=True)
            }
        self.assertEqual(shallow, {"parent"})
        self.assertEqual(deep, {"parent", "parent/child"})

    def test_scan_caps_are_reported_in_stats(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve() / "repos"
            root.mkdir()
            (root / "one" / "two").mkdir(parents=True)
            dir_stats: dict = {}
            with mock.patch.object(repos, "MAX_DISCOVERY_DIRS", 1):
                repos.discover_repos(root, deep=True, stats=dir_stats)
            self.assertTrue(dir_stats["truncated_dirs"])
            self.assertEqual(dir_stats["visited"], 2)

            make_repo(root / "alpha")
            make_repo(root / "beta")
            repo_stats: dict = {}
            with mock.patch.object(repos, "MAX_DISCOVERED_REPOS", 1):
                found = repos.discover_repos(root, stats=repo_stats)
            self.assertEqual(len(found), 1)
            self.assertTrue(repo_stats["truncated_repos"])

    def test_deep_discovery_marks_nested_and_keeps_it_unselected(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            root = base / "code"
            make_repo(root / "parent")
            make_repo(root / "parent" / "child")
            settings = repo_settings(base, [root], [root])
            roots, entries, errors, warnings = repos._discovery_entries(settings)
        self.assertEqual(roots, [root])
        self.assertEqual(errors, [])
        self.assertEqual(warnings, [])
        by_rel = {entry["rel"]: entry for entry in entries}
        self.assertEqual(set(by_rel), {"parent", "parent/child"})
        self.assertTrue(by_rel["parent"]["selected"])
        self.assertFalse(by_rel["parent"]["nested"])
        self.assertTrue(by_rel["parent/child"]["nested"])
        self.assertFalse(by_rel["parent/child"]["selected"])

    def test_nested_checkout_can_be_opted_in_and_resolved(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            root = base / "code"
            make_repo(root / "parent")
            make_repo(root / "parent" / "child")
            settings = repo_settings(base, [root], [root])
            fleet = repos.RepoFleet(settings, StoreStub())
            first = fleet.refresh()
            self.assertEqual({row["rel"] for row in first["repos"]}, {"parent"})
            self.assertEqual(first["summary"]["nested"], 0)

            result = fleet.set_repo_exclusions({}, {str(root): ["parent/child"]})
            self.assertEqual(result["included"], {str(root): ["parent/child"]})
            state = fleet.state()
            self.assertEqual(
                {row["rel"] for row in state["repos"]},
                {"parent", "parent/child"},
            )
            self.assertEqual(state["summary"]["nested"], 1)
            child = next(row for row in state["repos"] if row["rel"] == "parent/child")
            self.assertTrue(child["nested"])
            path, identity = fleet.resolve("parent/child")
            self.assertEqual(path, (root / "parent" / "child").resolve())
            self.assertEqual(identity["id"], "parent/child")

    def test_explicit_nested_root_wins_over_the_parent_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            root = base / "code"
            parent = root / "parent"
            child = parent / "child"
            make_repo(parent)
            make_repo(child)
            settings = repo_settings(base, [root, child], [root])
            _roots, entries, _errors, _warnings = repos._discovery_entries(settings)
        by_rel = {entry["rel"]: entry for entry in entries}
        self.assertIn("child", by_rel)
        self.assertEqual(by_rel["child"]["root"], str(child))
        self.assertTrue(by_rel["child"]["root_repo"])
        self.assertFalse(by_rel["child"]["nested"])
        self.assertTrue(by_rel["child"]["selected"])

    def test_deep_root_must_also_be_a_configured_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            root = base / "code"
            other = base / "other"
            root.mkdir()
            other.mkdir()
            settings = repo_settings(base, [root], [other])
            self.assertEqual(repos.repo_deep_roots(settings), set())
            self.assertEqual(repos.validate_deep_roots([str(root)], [root]), [str(root)])
            with self.assertRaises(repos.RepoError):
                repos.validate_deep_roots([str(other)], [root])
            with self.assertRaises(repos.RepoError):
                repos.validate_deep_roots([str(root / "missing")], [root])

    def test_scan_cap_becomes_a_discovery_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            root = base / "code"
            (root / "one" / "two").mkdir(parents=True)
            settings = repo_settings(base, [root])
            with mock.patch.object(repos, "MAX_DISCOVERY_DIRS", 1):
                _roots, _entries, errors, warnings = repos._discovery_entries(settings)
        self.assertEqual(errors, [])
        self.assertTrue(any("scan stopped after" in warning for warning in warnings))


class UnconfiguredRootTests(unittest.TestCase):
    def test_fleet_is_idle_until_a_root_is_chosen(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings.load(Path(tmp).resolve() / "console")
            self.assertFalse(settings.get("repo_root"))
            fleet = repos.RepoFleet(settings, StoreStub())
            snapshot = fleet.refresh()
            self.assertFalse(snapshot["root_configured"])
            self.assertEqual(snapshot["root"], "")
            self.assertEqual(snapshot["repos"], [])
            with self.assertRaises(repos.RepoError) as caught:
                fleet.resolve("anything")
            self.assertIn("not configured", str(caught.exception))


class GiteaDefaultsTests(unittest.TestCase):
    def test_empty_settings_mean_unconfigured_not_the_author(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings.load(Path(tmp))
            self.assertEqual(settings.get("gitea_url"), "")
            self.assertEqual(settings.get("gitea_owner"), "")
            with self.assertRaises(repos.RepoError) as caught:
                repos.gitea_config(settings)
            self.assertIn("not configured", str(caught.exception))
            fleet = repos.RepoFleet(settings, StoreStub())
            self.assertFalse(fleet.state()["gitea_configured"])
            self.assertEqual(fleet.state()["gitea"]["state"], "unconfigured")


class MultiRootTests(unittest.TestCase):
    def test_scans_every_root_and_keeps_duplicate_names_apart(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            first, second = base / "code", base / "archive"
            make_repo(first / "alpha")
            make_repo(first / "shared")
            make_repo(second / "beta")
            make_repo(second / "shared")
            settings = Settings.load(base / "console")
            settings.values["repo_roots"] = [str(first), str(second)]
            settings.values["repo_root"] = str(first)
            fleet = repos.RepoFleet(settings, StoreStub())
            state = fleet.refresh()
            self.assertEqual(state["roots"], [str(first), str(second)])
            self.assertEqual(state["summary"]["total"], 4)
            ids = {row["id"] for row in state["repos"]}
            self.assertIn("alpha", ids)
            self.assertIn("beta", ids)
            self.assertIn(str(first / "shared"), ids)
            self.assertIn(str(second / "shared"), ids)
            # Both duplicate-named repos resolve individually.
            for repo_id in (str(first / "shared"), str(second / "shared")):
                path, identity = fleet.resolve(repo_id)
                self.assertEqual(identity["id"], repo_id)
                self.assertEqual(str(path), repo_id)

    def test_legacy_repo_root_migrates_to_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            root = base / "code"
            root.mkdir()
            settings = Settings.load(base / "console")
            settings.values["repo_root"] = str(root)
            settings.values["repo_roots"] = []
            settings.save()
            reloaded = Settings.load(base / "console")
            self.assertEqual(reloaded.get("repo_roots"), [str(root)])
            self.assertEqual(reloaded.get("repo_root"), str(root))

    def test_deep_roots_and_nested_selection_survive_reload(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            root = base / "code"
            root.mkdir()
            settings = Settings.load(base / "console")
            settings.values["repo_roots"] = [str(root)]
            settings.values["repo_deep_roots"] = [str(root)]
            settings.values["repo_nested_selected"] = {str(root): ["group/child"]}
            settings.save()
            reloaded = Settings.load(base / "console")
            self.assertEqual(reloaded.get("repo_deep_roots"), [str(root)])
            self.assertEqual(
                reloaded.get("repo_nested_selected"),
                {str(root): ["group/child"]},
            )


class RepoSelectionTests(unittest.TestCase):
    def test_exclusions_hide_repos_but_keep_them_discovered(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            root = base / "code"
            make_repo(root / "alpha")
            make_repo(root / "beta")
            make_repo(root / "group" / "gamma")
            settings = Settings.load(base / "console")
            settings.values["repo_roots"] = [str(root)]
            settings.values["repo_root"] = str(root)
            settings.values["repo_exclusions"] = {str(root): ["beta"]}
            fleet = repos.RepoFleet(settings, StoreStub())
            state = fleet.refresh()
            self.assertEqual({row["rel"] for row in state["repos"]}, {"alpha", "group/gamma"})
            self.assertEqual(state["summary"]["total"], 2)
            self.assertEqual(state["summary"]["discovered"], 3)
            self.assertEqual(state["summary"]["excluded"], 1)
            beta = next(entry for entry in state["discovered"] if entry["rel"] == "beta")
            self.assertFalse(beta["selected"])

    def test_selection_round_trip_ignores_stale_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            root = base / "code"
            make_repo(root / "alpha")
            make_repo(root / "beta")
            settings = Settings.load(base / "console")
            settings.values["repo_roots"] = [str(root)]
            fleet = repos.RepoFleet(settings, StoreStub())
            result = fleet.set_repo_exclusions({str(root): ["alpha", "does-not-exist"]})
            self.assertEqual(result["exclusions"], {str(root): ["alpha"]})
            self.assertEqual(settings.get("repo_exclusions"), {str(root): ["alpha"]})
            self.assertEqual({row["rel"] for row in fleet.state()["repos"]}, {"beta"})
            discovered = fleet.discovered()
            alpha = next(entry for entry in discovered["repos"] if entry["rel"] == "alpha")
            self.assertFalse(alpha["selected"])

    def test_a_single_repo_root_can_be_selected_and_deselected(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            root = base / "single"
            make_repo(root)
            settings = Settings.load(base / "console")
            settings.values["repo_roots"] = [str(root)]
            fleet = repos.RepoFleet(settings, StoreStub())
            state = fleet.refresh()
            self.assertEqual(len(state["repos"]), 1)
            self.assertEqual(state["repos"][0]["rel"], "single")
            self.assertTrue(state["discovered"][0]["root_repo"])
            fleet.set_repo_exclusions({str(root): ["single"]})
            self.assertEqual(fleet.state()["repos"], [])
            self.assertFalse(fleet.discovered()["repos"][0]["selected"])


class UnconfiguredGiteaTests(unittest.TestCase):
    def test_repo_rows_skip_instead_of_erroring(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve() / "repos"
            root.mkdir()
            make_repo(root / "demo")
            settings = Settings.load(Path(tmp).resolve() / "console")
            settings.values["repo_root"] = str(root)
            fleet = repos.RepoFleet(settings, StoreStub())
            fleet.refresh()
            state = fleet.state()
            self.assertEqual(state["gitea_config_error"], "")
            self.assertFalse(state["gitea_configured"])
            self.assertEqual(state["repos"][0]["gitea"]["status"], "skip")
            self.assertIn("not configured", state["repos"][0]["gitea"]["detail"])


class UrlTests(unittest.TestCase):
    def test_github_variants(self):
        for value in (
            "https://github.com/example/demo.git",
            "git@github.com:example/demo.git",
            "ssh://git@github.com/example/demo.git",
        ):
            self.assertEqual(repos.github_repo_from_url(value), ("example", "demo"))
        self.assertIsNone(repos.github_repo_from_url("https://gitlab.com/example/demo.git"))
        self.assertIsNone(repos.github_repo_from_url(""))
        self.assertEqual(repos.github_repo_from_url("https://github.com/other/demo"), ("other", "demo"))


class StatusTests(unittest.TestCase):
    def test_dirty_ahead_and_no_upstream_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            local = base / "local"
            origin = base / "origin.git"
            make_repo(local)
            run("git", "init", "-q", "--bare", str(origin))
            run("git", "remote", "add", "origin", str(origin), cwd=local)
            run("git", "push", "-q", "-u", "origin", "HEAD", cwd=local)
            cfg = repos.GiteaConfig(root=base, url="https://gitea.invalid", owner="example")
            row = repos.repo_row(local, base, cfg)
            self.assertFalse(row["dirty"])
            self.assertEqual(row["ahead"], 0)
            self.assertFalse(row["needs"]["push"])

            (local / "f.txt").write_text("two\n")
            (local / "new.txt").write_text("new\n")
            row = repos.repo_row(local, base, cfg)
            self.assertTrue(row["dirty"])
            self.assertEqual(row["dirty_count"], 2)
            self.assertEqual({entry["path"] for entry in row["entries"]}, {"f.txt", "new.txt"})

            run("git", "add", "f.txt", cwd=local)
            run("git", "commit", "-qm", "second", cwd=local)
            row = repos.repo_row(local, base, cfg)
            self.assertEqual(row["ahead"], 1)
            self.assertTrue(row["needs"]["push"])

            # Unset tracking: origin/<branch> must still drive ahead/behind.
            run("git", "branch", "--unset-upstream", cwd=local)
            row = repos.repo_row(local, base, cfg)
            self.assertEqual(row["upstream"], f"origin/{row['branch']}")
            self.assertFalse(row["tracking"])
            self.assertEqual(row["ahead"], 1)

    def test_new_branch_and_detached_head(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            local = base / "local"
            origin = base / "origin.git"
            make_repo(local)
            run("git", "init", "-q", "--bare", str(origin))
            run("git", "remote", "add", "origin", str(origin), cwd=local)
            cfg = repos.GiteaConfig(root=base, url="https://gitea.invalid", owner="example")
            row = repos.repo_row(local, base, cfg)
            self.assertTrue(row["new_branch"])
            self.assertTrue(row["needs"]["push"])
            self.assertIsNone(row["ahead"])

            run("git", "checkout", "-q", "--detach", cwd=local)
            row = repos.repo_row(local, base, cfg)
            self.assertTrue(row["detached"])
            self.assertFalse(row["needs"]["push"])


class CommitTests(unittest.TestCase):
    def test_selective_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve() / "repos"
            root.mkdir()
            local = root / "demo"
            make_repo(local)
            (local / "keep.txt").write_text("keep\n")
            (local / "other.txt").write_text("other\n")
            run("git", "add", "keep.txt", "other.txt", cwd=local)
            run("git", "commit", "-qm", "files", cwd=local)
            (local / "keep.txt").write_text("changed\n")
            (local / "other.txt").write_text("changed\n")
            settings = configure_gitea(Settings.load(Path(tmp).resolve() / "console"))
            settings.values["repo_root"] = str(root)
            store = StoreStub()
            fleet = repos.RepoFleet(settings, store)
            with mock.patch.object(fleet, "recheck_gitea_async", return_value=True) as recheck:
                result = fleet.commit("demo", ["keep.txt"], "demo: keep only")
            self.assertTrue(result["ok"])
            recheck.assert_called_once_with("demo")
            names = run("git", "show", "--name-only", "--format=", "HEAD", cwd=local).stdout.split()
            self.assertEqual(names, ["keep.txt"])
            self.assertTrue((local / "other.txt").read_text() == "changed\n")
            self.assertEqual(len(store.events), 1)

    def test_refresh_after_console_commit_maps_the_checkout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve() / "repos"
            root.mkdir()
            local = root / "demo"
            make_repo(local)
            run("git", "remote", "add", "origin", "https://github.com/example/demo.git", cwd=local)
            settings = configure_gitea(Settings.load(Path(tmp).resolve() / "console"))
            settings.values["repo_root"] = str(root)
            fleet = repos.RepoFleet(settings, StoreStub())
            fleet.refresh()
            with mock.patch.object(fleet, "recheck_gitea_async", return_value=True) as recheck:
                self.assertTrue(fleet.refresh_after_commit(local))
            recheck.assert_called_once_with("demo")

    def test_rejects_unsafe_or_stale_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve() / "repos"
            root.mkdir()
            local = root / "demo"
            make_repo(local)
            (local / "f.txt").write_text("changed\n")
            settings = configure_gitea(Settings.load(Path(tmp).resolve() / "console"))
            settings.values["repo_root"] = str(root)
            fleet = repos.RepoFleet(settings, StoreStub())
            for bad in (["../outside"], ["/etc/passwd"], ["missing.txt"]):
                with self.assertRaises(repos.RepoError):
                    fleet.commit("demo", bad, "bad")


class PushTests(unittest.TestCase):
    def test_push_refreshes_gitea_and_connectivity(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            root = base / "repos"
            root.mkdir()
            local = root / "demo"
            make_repo(local)
            origin = base / "origin.git"
            run("git", "init", "-q", "--bare", str(origin))
            run("git", "remote", "add", "origin", str(origin), cwd=local)
            run("git", "push", "-q", "-u", "origin", "HEAD", cwd=local)
            (local / "f.txt").write_text("two\n")
            run("git", "add", "f.txt", cwd=local)
            run("git", "commit", "-qm", "second", cwd=local)
            settings = configure_gitea(Settings.load(base / "console"))
            settings.values["repo_root"] = str(root)
            fleet = repos.RepoFleet(settings, StoreStub())
            fleet.refresh()
            with mock.patch.object(fleet, "recheck_gitea_async", return_value=True) as recheck, \
                 mock.patch.object(fleet, "check_connectivity_async", return_value=True) as connectivity:
                result = fleet.push_github("demo")
            self.assertTrue(result["ok"])
            recheck.assert_called_once_with("demo")
            connectivity.assert_called_once()


class GiteaSyncTests(unittest.TestCase):
    def _fixture(self, base: Path):
        local = base / "local"
        make_repo(local)
        run("git", "remote", "add", "origin", "https://github.com/example/widget.git", cwd=local)
        target = base / "gitea" / "example" / "widget.git"
        target.parent.mkdir(parents=True)
        run("git", "init", "-q", "--bare", str(target))
        cfg = repos.GiteaConfig(
            root=base,
            url=(base / "gitea").as_uri(),
            owner="example",
            timeout=30,
        )
        return local, target, cfg

    def test_recheck_updates_one_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            local, _target, _cfg = self._fixture(base)
            settings = Settings.load(base / "console")
            settings.values.update(
                {
                    "repo_root": str(base),
                    "gitea_url": "https://gitea.invalid",
                    "gitea_owner": "example",
                }
            )
            fleet = repos.RepoFleet(settings, StoreStub())
            fleet.refresh()
            self.assertEqual(fleet._row("local")["gitea"]["status"], "unknown")
            checked = {
                "status": "synced",
                "detail": "all refs match Gitea",
                "target": "https://gitea.invalid/example/widget.git",
                "checked_at": 123,
            }
            with mock.patch.object(repos, "check_gitea", return_value=checked) as probe:
                result = fleet.check_gitea_repo("local")
            probe.assert_called_once()
            self.assertEqual(result["status"], "synced")
            self.assertEqual(fleet._row("local")["gitea"]["status"], "synced")

    def test_auto_recheck_respects_the_automatic_checks_setting(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            self._fixture(base)
            settings = Settings.load(base / "console")
            settings.values.update(
                {
                    "repo_root": str(base),
                    "gitea_url": "https://gitea.invalid",
                    "gitea_owner": "example",
                    "gitea_check_on_start": False,
                }
            )
            fleet = repos.RepoFleet(settings, StoreStub())
            fleet.refresh()
            self.assertFalse(fleet.recheck_gitea_async("local"))

    def test_create_update_and_up_to_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            local, target, cfg = self._fixture(Path(tmp).resolve())
            result = repos.sync_repo(local, cfg, "", dry_run=True)
            self.assertEqual(result["status"], "would-sync")
            self.assertEqual(refs(target), {})

            result = repos.sync_repo(local, cfg, "")
            self.assertTrue(result["ok"])
            self.assertEqual(result["status"], "synced")
            self.assertEqual(refs(local), refs(target))

            result = repos.sync_repo(local, cfg, "")
            self.assertEqual(result["status"], "up-to-date")

            (local / "f.txt").write_text("two\n")
            run("git", "add", "f.txt", cwd=local)
            run("git", "commit", "-qm", "second", cwd=local)
            run("git", "tag", "v1", cwd=local)
            result = repos.sync_repo(local, cfg, "")
            self.assertEqual(result["status"], "synced")
            self.assertEqual(refs(local), refs(target))

    def test_force_and_prune_after_amend(self):
        with tempfile.TemporaryDirectory() as tmp:
            local, target, cfg = self._fixture(Path(tmp).resolve())
            (local / "f.txt").write_text("second\n")
            run("git", "add", "f.txt", cwd=local)
            run("git", "commit", "-qm", "second", cwd=local)
            repos.sync_repo(local, cfg, "")
            tip = run("git", "rev-parse", "HEAD", cwd=target).stdout.strip()
            run("git", "branch", "extra", tip, cwd=target)
            run("git", "tag", "extra-tag", tip, cwd=target)
            (local / "f.txt").write_text("rewritten\n")
            run("git", "add", "f.txt", cwd=local)
            run("git", "commit", "-q", "--amend", "-m", "rewritten", cwd=local)
            result = repos.sync_repo(local, cfg, "")
            self.assertTrue(result["ok"])
            self.assertEqual(result["status"], "synced")
            self.assertEqual(refs(local), refs(target))
            self.assertIn("pruned", result["detail"])

    def test_prune_off_leaves_extra_refs(self):
        with tempfile.TemporaryDirectory() as tmp:
            local, target, cfg = self._fixture(Path(tmp).resolve())
            repos.sync_repo(local, cfg, "")
            tip = run("git", "rev-parse", "HEAD", cwd=target).stdout.strip()
            run("git", "branch", "extra", tip, cwd=target)
            run("git", "tag", "extra-tag", tip, cwd=target)
            no_prune = repos.GiteaConfig(
                root=cfg.root,
                url=cfg.url,
                owner=cfg.owner,
                timeout=cfg.timeout,
                prune=False,
                force=cfg.force,
            )

            checked = repos.check_gitea(local, no_prune, "")
            self.assertEqual(checked["status"], "synced")
            self.assertIn("extra ref(s) left on Gitea", checked["detail"])
            self.assertEqual(checked["counts"]["prune"], 0)

            preview = repos.sync_repo(local, no_prune, "", dry_run=True)
            self.assertEqual(preview["status"], "up-to-date")
            self.assertIn("extra left on Gitea", preview["detail"])
            self.assertEqual(preview["counts"]["prune"], 0)

            result = repos.sync_repo(local, no_prune, "")
            self.assertEqual(result["status"], "up-to-date")
            self.assertIn("refs/heads/extra", refs(target))
            self.assertNotIn("refs/heads/extra", refs(local))

    def test_unrelated_history_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            local, target, cfg = self._fixture(base)
            other = base / "other"
            make_repo(other, "other")
            import shutil
            shutil.rmtree(target)
            run("git", "clone", "-q", "--bare", str(other), str(target))
            result = repos.sync_repo(local, cfg, "")
            self.assertTrue(result["ok"])
            self.assertEqual(result["status"], "skip")
            self.assertIn("unrelated history", result["detail"])

    def test_lfs_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            local, _target, cfg = self._fixture(Path(tmp).resolve())
            (local / ".gitattributes").write_text("*.bin filter=lfs diff=lfs merge=lfs -text\n")
            result = repos.sync_repo(local, cfg, "")
            self.assertTrue(result["ok"])
            self.assertEqual(result["status"], "skip")
            self.assertIn("LFS", result["detail"])

    def test_check_compares_refs_without_push(self):
        with tempfile.TemporaryDirectory() as tmp:
            local, target, cfg = self._fixture(Path(tmp).resolve())
            checked = repos.check_gitea(local, cfg, "")
            self.assertEqual(checked["status"], "behind")
            self.assertEqual(refs(target), {})
            repos.sync_repo(local, cfg, "")
            checked = repos.check_gitea(local, cfg, "")
            self.assertEqual(checked["status"], "synced")


class TokenTests(unittest.TestCase):
    def test_env_header_does_not_expose_token(self):
        cfg = repos.GiteaConfig(root=Path("/tmp"), url="https://gitea.home.arpa", owner="example")
        env = repos._gitea_env(cfg, "abcdefghijklmnopqrstuvwxyz0123456789")
        header = env["GIT_CONFIG_VALUE_0"]
        self.assertTrue(header.startswith("Authorization: Basic "))
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz0123456789", header)
        self.assertFalse(any("abcdefghijklmnopqrstuvwxyz0123456789" == value for value in env.values()))

    def test_public_settings_mask_tokens(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings.load(Path(tmp))
            settings.values["github_token"] = "ghp_test"
            settings.values["gitea_token"] = "gitea_test_token_1234567890"
            public = settings.public()
            self.assertNotIn("github_token", public)
            self.assertNotIn("gitea_token", public)
            self.assertTrue(public["github_token_set"])
            self.assertTrue(public["gitea_token_set"])


class QueueTests(unittest.TestCase):
    class FakeRun:
        id = "run-test"

        def emit(self, *_args, **_kwargs):
            pass

    class FakeTasks:
        def __init__(self):
            self.work = None

        def start(self, kind, fn, **kwargs):
            self.work = fn
            return QueueTests.FakeRun()

    def test_queue_continues_after_github_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve() / "repos"
            root.mkdir()
            local = root / "demo"
            make_repo(local)
            (local / "f.txt").write_text("two\n")
            run("git", "add", "f.txt", cwd=local)
            run("git", "commit", "-qm", "second", cwd=local)
            run("git", "remote", "add", "origin", "https://github.com/example/demo.git", cwd=local)
            settings = configure_gitea(Settings.load(Path(tmp).resolve() / "console"))
            settings.values["repo_root"] = str(root)
            fleet = repos.RepoFleet(settings, StoreStub(), QueueTests.FakeTasks())
            fleet.refresh()
            with mock.patch.object(fleet, "push_github", return_value={"ok": False, "status": "error", "detail": "github down"}) as push_mock, \
                 mock.patch.object(fleet, "sync_gitea", return_value={"ok": True, "status": "synced", "detail": "backed up"}) as sync_mock:
                result = fleet.queue_push_both(["demo"])
                with self.assertRaises(repos.RepoQueueError) as caught:
                    fleet.tasks.work(QueueTests.FakeRun())
            push_mock.assert_called_once_with("demo", post_action=False)
            sync_mock.assert_called_once_with("demo", dry_run=False, post_action=False)
            queue = result["queue"]
            self.assertEqual(queue["total"], 1)
            self.assertEqual(caught.exception.run_result["queue"]["items"][0]["gitea"], "ok")
            self.assertEqual(caught.exception.run_result["queue"]["items"][0]["github"], "failed")


if __name__ == "__main__":
    unittest.main()


class OpenTests(unittest.TestCase):
    def test_open_uses_vscode_new_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve() / "repos"
            root.mkdir()
            local = root / "demo"
            make_repo(local)
            settings = configure_gitea(Settings.load(Path(tmp).resolve() / "console"))
            settings.values["repo_root"] = str(root)
            fleet = repos.RepoFleet(settings, StoreStub())
            with mock.patch.object(repos, "_find_vscode", return_value="/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code"), \
                 mock.patch.object(repos.subprocess, "Popen") as popen:
                result = fleet.open_in_vscode("demo")
            self.assertTrue(result["ok"])
            command = popen.call_args.args[0]
            self.assertEqual(command, ["/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code", "-n", str(local)])
            with self.assertRaises(repos.RepoError):
                fleet.open_in_vscode("not-a-repo")


class GiteaStateTests(unittest.TestCase):
    def test_dry_run_does_not_replace_cached_status_and_created_is_synced(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve() / "repos"
            root.mkdir()
            local = root / "demo"
            make_repo(local)
            run("git", "remote", "add", "origin", "https://github.com/example/demo.git", cwd=local)
            settings = configure_gitea(Settings.load(Path(tmp).resolve() / "console"))
            settings.values["repo_root"] = str(root)
            fleet = repos.RepoFleet(settings, StoreStub())
            fleet.refresh()
            before = fleet._row("demo")["gitea"]["status"]
            with mock.patch.object(repos, "sync_repo", return_value={"ok": True, "status": "would-sync", "detail": "preview"}):
                result = fleet.sync_gitea("demo", dry_run=True)
            self.assertEqual(result["status"], "would-sync")
            self.assertEqual(fleet._row("demo")["gitea"]["status"], before)
            fleet._set_gitea("demo", {"status": "created", "detail": "created", "target": "x"})
            self.assertEqual(fleet._row("demo")["gitea"]["status"], "synced")


class ConfigErrorTests(unittest.TestCase):
    def test_bad_gitea_url_does_not_hide_local_repos(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve() / "repos"
            root.mkdir()
            make_repo(root / "demo")
            settings = configure_gitea(Settings.load(Path(tmp).resolve() / "console"))
            settings.values["repo_root"] = str(root)
            settings.values["gitea_url"] = "ftp://gitea.invalid"
            fleet = repos.RepoFleet(settings, StoreStub())
            state = fleet.refresh()
            self.assertTrue(state["gitea_config_error"])
            self.assertEqual(state["root_error"], "")
            self.assertEqual([row["id"] for row in state["repos"]], ["demo"])
            self.assertEqual(state["repos"][0]["gitea"]["status"], "error")


class GitignoreTests(unittest.TestCase):
    def test_tracked_ignored_file_is_hidden_from_status_and_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve() / "repos"
            root.mkdir()
            local = root / "demo"
            make_repo(local)
            (local / "tracked.log").write_text("one\n")
            run("git", "add", "tracked.log", cwd=local)
            run("git", "commit", "-qm", "track log", cwd=local)
            (local / ".gitignore").write_text("*.log\n")
            run("git", "add", ".gitignore", cwd=local)
            run("git", "commit", "-qm", "ignore logs", cwd=local)
            (local / "tracked.log").write_text("two\n")
            (local / "f.txt").write_text("changed\n")
            settings = configure_gitea(Settings.load(Path(tmp).resolve() / "console"))
            settings.values["repo_root"] = str(root)
            fleet = repos.RepoFleet(settings, StoreStub())
            row = fleet._row("demo") or repos.repo_row(local, root, repos.gitea_config(settings))
            self.assertEqual(row["dirty_count"], 1)
            self.assertEqual(row["ignored_count"], 1)
            self.assertEqual([entry["path"] for entry in row["entries"] if entry["ignored"]], ["tracked.log"])
            with self.assertRaises(repos.RepoError):
                fleet.commit("demo", ["tracked.log"], "should not commit ignored")
            result = fleet.commit("demo", ["f.txt"], "commit the visible file")
            self.assertTrue(result["ok"])
            names = run("git", "show", "--name-only", "--format=", "HEAD", cwd=local).stdout.split()
            self.assertEqual(names, ["f.txt"])


class ConnectivityTests(unittest.TestCase):
    def test_github_up_and_down(self):
        up = {"reachable": True, "status": 200, "latency_ms": 42, "body": b"", "error": ""}
        with mock.patch.object(repos, "_probe_http", return_value=up):
            result = repos.probe_github()
        self.assertEqual(result["state"], "up")
        self.assertIn("HTTP 200", result["detail"])
        down = {"reachable": False, "status": 0, "latency_ms": 0, "body": b"", "error": "URLError: offline"}
        with mock.patch.object(repos, "_probe_http", return_value=down):
            result = repos.probe_github()
        self.assertEqual(result["state"], "down")
        self.assertIn("offline", result["detail"])

    def test_gitea_up_auth_and_version(self):
        cfg = repos.GiteaConfig(root=Path("/tmp"), url="https://gitea.home.arpa", owner="example")
        ok = {"reachable": True, "status": 200, "latency_ms": 12, "body": b'{"version":"1.24.3"}', "error": ""}
        with mock.patch.object(repos, "_probe_http", return_value=ok) as probe:
            result = repos.probe_gitea(cfg, "token-value")
        self.assertEqual(result["state"], "up")
        self.assertEqual(result["version"], "1.24.3")
        self.assertEqual(probe.call_args.kwargs["headers"], {"Authorization": "token token-value"})
        auth = {"reachable": True, "status": 401, "latency_ms": 5, "body": b"", "error": "HTTP 401"}
        with mock.patch.object(repos, "_probe_http", return_value=auth):
            result = repos.probe_gitea(cfg, "")
        self.assertEqual(result["state"], "auth")


class CommitMetricsTests(unittest.TestCase):
    def test_commit_activity_feeds_the_repo_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            local = base / "local"
            make_repo(local)
            cfg = repos.GiteaConfig(root=base, url="https://gitea.invalid", owner="example")
            row = repos.repo_row(local, base, cfg)
            self.assertEqual(len(row["commit_activity"]["days"]), 14)
            self.assertGreaterEqual(row["commit_activity"]["last_30d"], 1)
            summary = repos._summarise([row])
            self.assertGreaterEqual(summary["commits_30d"], 1)
            self.assertEqual(summary["active_repos"], 1)
            self.assertEqual(summary["quiet_repos"], 0)
            self.assertEqual(len(summary["activity_days"]), 14)

    def test_merge_conflicts_are_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            local = base / "conflict"
            make_repo(local)
            branch = run("git", "rev-parse", "--abbrev-ref", "HEAD", cwd=local).stdout.strip()
            run("git", "checkout", "-qb", "feature", cwd=local)
            (local / "f.txt").write_text("feature\n")
            run("git", "commit", "-qam", "feature", cwd=local)
            run("git", "checkout", "-q", branch, cwd=local)
            (local / "f.txt").write_text("base\n")
            run("git", "commit", "-qam", "base", cwd=local)
            subprocess.run(
                ["git", "merge", "feature"],
                cwd=local, text=True, capture_output=True, check=False,
            )
            cfg = repos.GiteaConfig(root=base, url="https://gitea.invalid", owner="example")
            row = repos.repo_row(local, base, cfg)
            self.assertEqual(row["conflicts"], 1)
            self.assertTrue(row["needs"]["commit"])

    def test_last_touch_and_fetch_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            local = base / "local"
            make_repo(local)
            env = {
                **os.environ,
                "GIT_AUTHOR_DATE": "2020-01-01T00:00:00",
                "GIT_COMMITTER_DATE": "2020-01-01T00:00:00",
            }
            (local / "f.txt").write_text("two\n")
            subprocess.run(["git", "add", "f.txt"], cwd=local, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-qm", "old"], cwd=local, check=True, capture_output=True, env=env)
            (local / "f.txt").write_text("dirty\n")
            (local / ".git" / "FETCH_HEAD").write_text("unused\n", encoding="utf-8")
            cfg = repos.GiteaConfig(root=base, url="https://gitea.invalid", owner="example")
            row = repos.repo_row(local, base, cfg)
            self.assertGreater(row["last_touch_at"], row["last_commit"]["at"])
            self.assertGreater(row["last_fetch_at"], 0)

    def test_activity_cache_reuses_a_tip_until_it_moves(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            local = base / "local"
            make_repo(local)
            head = run("git", "rev-parse", "HEAD", cwd=local).stdout.strip()
            first = repos._commit_activity(local, head)
            self.assertEqual(first["last_30d"], 1)
            (local / "f.txt").write_text("two\n")
            run("git", "add", "f.txt", cwd=local)
            run("git", "commit", "-qm", "second", cwd=local)
            new_head = run("git", "rev-parse", "HEAD", cwd=local).stdout.strip()
            second = repos._commit_activity(local, new_head)
            self.assertEqual(second["last_30d"], 2)


class BehindsTests(unittest.TestCase):
    def test_behind_origin_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            local = base / "local"
            origin = base / "origin.git"
            make_repo(local)
            run("git", "init", "-q", "--bare", str(origin))
            run("git", "remote", "add", "origin", str(origin), cwd=local)
            run("git", "push", "-q", "-u", "origin", "HEAD", cwd=local)
            other = base / "other"
            run("git", "clone", "-q", str(origin), str(other))
            run("git", "config", "user.email", "t@t", cwd=other)
            run("git", "config", "user.name", "t", cwd=other)
            (other / "f.txt").write_text("two\n")
            run("git", "add", "f.txt", cwd=other)
            run("git", "commit", "-qm", "second", cwd=other)
            run("git", "push", "-q", "origin", "HEAD", cwd=other)
            run("git", "fetch", "-q", "origin", cwd=local)
            cfg = repos.GiteaConfig(root=base, url="https://gitea.invalid", owner="example")
            row = repos.repo_row(local, base, cfg)
            self.assertEqual(row["behind"], 1)
            self.assertEqual(row["ahead"], 0)
            summary = repos._summarise([row])
            self.assertEqual(summary["behind_repos"], 1)
            self.assertEqual(summary["behind_commits"], 1)
            self.assertGreaterEqual(summary["attention"], 1)
