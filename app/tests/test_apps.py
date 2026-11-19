"""App bundle, manifest and dependency-check tests. Local files only, no network."""

from __future__ import annotations

import json
import os
import plistlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

APP = Path(__file__).resolve().parents[1]
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

from homelab_console import apps  # noqa: E402


def make_bundle(
    path: Path,
    *,
    name: str = "SevenZip",
    identifier: str = "com.local.SevenZipGUI",
    version: str = "0.1.0",
    dependencies: list[dict] | None = None,
    manifest: bool = True,
) -> Path:
    resources = path / "Contents" / "Resources"
    resources.mkdir(parents=True)
    info = {"CFBundleName": name, "CFBundleIdentifier": identifier}
    if version:
        info["CFBundleShortVersionString"] = version
    (path / "Contents" / "Info.plist").write_bytes(plistlib.dumps(info))
    if manifest:
        payload: dict = {"schema": 1, "app": {"name": name, "version": version}}
        if dependencies is not None:
            payload["dependencies"] = dependencies
        (resources / apps.MANIFEST_NAME).write_text(json.dumps(payload))
    return path


class BundleTests(unittest.TestCase):
    def test_reads_plist_and_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = make_bundle(
                Path(tmp) / "SevenZip.app",
                dependencies=[{"repo": "ip7z/7zip", "version": "26.06", "path": "7zz"}],
            )
            bundle = apps.read_bundle(path)
            self.assertIsNotNone(bundle)
            assert bundle is not None
            self.assertEqual(bundle.name, "SevenZip")
            self.assertEqual(bundle.identifier, "com.local.SevenZipGUI")
            self.assertEqual(bundle.version, "0.1.0")
            self.assertTrue(bundle.manifest)
            self.assertEqual(bundle.installed("ip7z/7zip"), "26.06")
            # Case-insensitive: manifests and settings may disagree on case.
            self.assertEqual(bundle.installed("IP7Z/7Zip"), "26.06")
            self.assertEqual(bundle.installed("other/repo"), "")

    def test_missing_manifest_is_reported_not_guessed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = make_bundle(Path(tmp) / "Plain.app", manifest=False)
            bundle = apps.read_bundle(path)
            assert bundle is not None
            self.assertFalse(bundle.manifest)
            self.assertEqual(bundle.dependencies, {})
            self.assertEqual(bundle.version, "0.1.0")

    def test_malformed_manifest_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = make_bundle(Path(tmp) / "Odd.app", manifest=False)
            (path / "Contents" / "Resources" / apps.MANIFEST_NAME).write_text("{not json")
            bundle = apps.read_bundle(path)
            assert bundle is not None
            self.assertFalse(bundle.manifest)
            self.assertEqual(bundle.dependencies, {})

    def test_non_app_paths_are_not_bundles(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "SevenZip"
            path.mkdir()
            self.assertFalse(apps.is_bundle(path))
            self.assertIsNone(apps.read_bundle(path))

    def test_bundle_stamp_tracks_manifest_rewrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = make_bundle(Path(tmp) / "SevenZip.app", manifest=False)
            before = apps.bundle_stamp(path)
            manifest = path / "Contents" / "Resources" / apps.MANIFEST_NAME
            manifest.write_text(json.dumps({"dependencies": []}))
            os.utime(manifest, (before + 60, before + 60))
            self.assertGreater(apps.bundle_stamp(path), before)


class ScanTests(unittest.TestCase):
    def test_ranks_shallow_project_named_bundle_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "sevenzip-gui"
            make_bundle(repo / "dist" / "SevenZip.app", identifier="com.local.SevenZipGUI")
            make_bundle(repo / "Vendor" / "sample" / "Helper.app", name="Helper", identifier="x.helper")
            make_bundle(repo / "Other.app", name="Other", identifier="x.other")
            found = apps.scan_apps(repo)
            self.assertEqual(found[0].name, "SevenZip")
            self.assertEqual(len(found), 3)

    def test_does_not_descend_into_a_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            outer = make_bundle(repo / "Outer.app", name="Outer", identifier="x.outer")
            make_bundle(
                outer / "Contents" / "Frameworks" / "Inner.app",
                name="Inner",
                identifier="x.inner",
            )
            found = apps.scan_apps(repo)
            self.assertEqual([bundle.name for bundle in found], ["Outer"])

    def test_skips_hidden_and_dependency_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            make_bundle(repo / ".build" / "Hidden.app", name="Hidden")
            make_bundle(repo / "node_modules" / "Dep.app", name="Dep")
            make_bundle(repo / "dist" / "Real.app", name="Real")
            found = apps.scan_apps(repo)
            self.assertEqual([bundle.name for bundle in found], ["Real"])

    def test_missing_directory_is_empty_not_an_error(self):
        self.assertEqual(apps.scan_apps(Path("/nonexistent/project/for/test")), [])


class VersionTests(unittest.TestCase):
    def test_ordering_ignores_prefix_and_padding(self):
        self.assertEqual(apps.compare_versions("26.06", "26.07"), -1)
        self.assertEqual(apps.compare_versions("26.07", "26.06"), 1)
        self.assertEqual(apps.compare_versions("v26.06", "26.6"), 0)
        self.assertEqual(apps.compare_versions("26.10", "26.9"), 1)

    def test_unparsable_versions_compare_equal(self):
        self.assertEqual(apps.compare_versions("nightly", "26.07"), 0)
        self.assertEqual(apps.compare_versions("", "26.07"), 0)

    def test_status_never_calls_unknown_behind(self):
        release = apps.Release(slug="ip7z/7zip", tag="26.07")
        self.assertEqual(apps.dependency_status("26.06", release), "behind")
        self.assertEqual(apps.dependency_status("26.07", release), "current")
        self.assertEqual(apps.dependency_status("", release), "unknown")
        self.assertEqual(apps.dependency_status("26.06", None), "unknown")
        self.assertEqual(
            apps.dependency_status("26.06", apps.Release(slug="ip7z/7zip", error="offline")),
            "error",
        )


class NormalizeTests(unittest.TestCase):
    def test_app_path_must_be_absolute_and_end_in_app(self):
        self.assertEqual(apps.normalize_app_path("/tmp/Thing.app"), "/tmp/Thing.app")
        for bad in ("", "Thing.app", "/tmp/Thing", "/tmp/Thing.app/Contents"):
            with self.assertRaises(apps.AppError):
                apps.normalize_app_path(bad)

    def test_repo_apps_drops_invalid_rows(self):
        cleaned = apps.normalize_repo_apps(
            {
                "/tmp/repo": "/tmp/repo/dist/Thing.app",
                "relative": "/tmp/Thing.app",
                "/tmp/other": "/tmp/not-an-app",
                123: 456,
            }
        )
        self.assertEqual(cleaned, {"/tmp/repo": "/tmp/repo/dist/Thing.app"})

    def test_dependencies_accept_strings_and_dicts(self):
        cleaned = apps.normalize_dependencies(
            [{"repo": "ip7z/7zip"}, "owner/name", "not a slug", {"repo": "IP7Z/7zip"}]
        )
        self.assertEqual(cleaned, [{"repo": "ip7z/7zip"}, {"repo": "owner/name"}])

    def test_strict_validation_rejects_bad_entries(self):
        with self.assertRaises(apps.AppError):
            apps.validate_dependencies(["not a slug"])
        with self.assertRaises(apps.AppError):
            apps.validate_dependencies("ip7z/7zip")
        self.assertEqual(apps.validate_dependencies(None), [])

    def test_repo_dependencies_are_keyed_by_absolute_path(self):
        cleaned = apps.normalize_repo_dependencies(
            {"/tmp/repo": [{"repo": "ip7z/7zip"}], "relative": [{"repo": "a/b"}]}
        )
        self.assertEqual(cleaned, {"/tmp/repo": [{"repo": "ip7z/7zip"}]})


class FetchReleaseTests(unittest.TestCase):
    def test_release_payload_is_used(self):
        payload = {
            "tag_name": "26.07",
            "name": "7-Zip 26.07",
            "html_url": "https://github.com/ip7z/7zip/releases/tag/26.07",
            "published_at": "2026-12-01T00:00:00Z",
        }
        with mock.patch.object(apps, "http_json", return_value=(200, payload, {})):
            release = apps.fetch_release("ip7z/7zip")
        self.assertTrue(release.ok)
        self.assertEqual(release.tag, "26.07")
        self.assertEqual(release.source, "release")
        self.assertTrue(release.url.endswith("/26.07"))

    def test_missing_releases_falls_back_to_tags(self):
        tags = [{"name": "v1.2.3", "commit": {"sha": "abc"}}]
        responses = [(404, {"message": "Not Found"}, {}), (200, tags, {})]
        with mock.patch.object(apps, "http_json", side_effect=responses):
            release = apps.fetch_release("owner/project")
        self.assertTrue(release.ok)
        self.assertEqual(release.tag, "v1.2.3")
        self.assertEqual(release.source, "tag")

    def test_rate_limit_is_explained(self):
        with mock.patch.object(
            apps,
            "http_json",
            return_value=(403, {"message": "rate limit"}, {"x-ratelimit-remaining": "0"}),
        ):
            release = apps.fetch_release("ip7z/7zip")
        self.assertIn("rate limit", release.error)
        self.assertIn("token", release.error)

    def test_network_failure_becomes_data(self):
        with mock.patch.object(apps, "http_json", side_effect=OSError("offline")):
            release = apps.fetch_release("ip7z/7zip")
        self.assertIn("could not reach GitHub", release.error)

    def test_invalid_slug_never_reaches_the_network(self):
        with mock.patch.object(apps, "http_json") as http:
            release = apps.fetch_release("../../etc/passwd")
        http.assert_not_called()
        self.assertTrue(release.error)


class ReleaseCacheTests(unittest.TestCase):
    def test_fetches_once_then_serves_from_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cache" / "app-releases.json"
            cache = apps.ReleaseCache(path, ttl=3600)
            fetched = apps.Release(
                slug="ip7z/7zip", tag="26.07", fetched_at=apps.util.now_ts()
            )
            with mock.patch.object(apps, "fetch_release", return_value=fetched) as fetch:
                cache.ensure(["ip7z/7zip"], force=True)
                cache.ensure(["ip7z/7zip"])
            self.assertEqual(fetch.call_count, 1)
            # A fresh process reads the same answer from disk.
            again = apps.ReleaseCache(path, ttl=3600)
            self.assertEqual(again.get("ip7z/7zip").tag, "26.07")
            self.assertTrue(
                again.is_fresh("ip7z/7zip", now=again.get("ip7z/7zip").fetched_at + 10)
            )

    def test_stale_rows_are_refetched_and_errors_retry_early(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = apps.ReleaseCache(Path(tmp) / "r.json", ttl=3600)
            stale = apps.Release(slug="a/b", tag="1.0", fetched_at=1)
            with cache._lock:  # seed the exact row under test
                cache._load()["a/b"] = stale
            self.assertFalse(cache.is_fresh("a/b", now=10_000))
            broken = apps.Release(
                slug="a/b", error="offline", fetched_at=10_000
            )
            with cache._lock:
                cache._load()["a/b"] = broken
            # An error is retried after five minutes, not after the full TTL.
            self.assertFalse(cache.is_fresh("a/b", now=10_000 + 301))
            self.assertTrue(cache.is_fresh("a/b", now=10_000 + 10))

    def test_empty_slug_list_does_no_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = apps.ReleaseCache(Path(tmp) / "r.json")
            with mock.patch.object(apps, "fetch_release") as fetch:
                self.assertEqual(cache.ensure([]), {})
            fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
