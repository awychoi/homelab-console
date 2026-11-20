"""Credential rotation: planning, rewriting and the lockout rollback."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from homelab_console import runner, secrets, util

ACCOUNTS = """# account hashes live here
[accounts]
  awychoi: +5sCHBKTXle2HJL0cBJaVcWWjYf-zuOlw
"""

ENV = "TAG=v1\nSEARXNG_SECRET_KEY=old\nDB_PASSWORD='keep#me'\n"


class FakeSettings:
    def __init__(self, repo: Path, remote_root: str = "/opt/stacks"):
        self.repo = repo
        self.remote_root = remote_root
        self.values = {}

    def get(self, key, default=None):
        return self.values.get(key, default)


class FakeStack:
    def __init__(self, files, host="nas", name="copyparty", services=("copyparty",)):
        self.host = host
        self.name = name
        self.files = dict(files)
        self.service_names = list(services)
        self.path = Path("/tmp") / name


class FakeStore:
    def __init__(self):
        self.events = []

    def add_event(self, *args, **kwargs):
        self.events.append((args, kwargs))


class FakeRun:
    def __init__(self):
        self.lines = []

    def emit(self, line):
        self.lines.append(line)


def write_rotation(repo: Path, payload) -> None:
    target = repo / "scripts" / "rotation.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload), encoding="utf-8")


COPYPARTY_SPEC = {
    "key": "awychoi",
    "label": "Library account",
    "mechanism": "copyparty-accounts",
    "file": "accounts.conf",
    "service": "copyparty",
}


class RewriteTests(unittest.TestCase):
    def test_env_rewrite_preserves_shape(self):
        out = secrets.rewrite_env(ENV, "SEARXNG_SECRET_KEY", "NEWVALUE123")
        self.assertIn("SEARXNG_SECRET_KEY=NEWVALUE123\n", out)
        self.assertIn("DB_PASSWORD='keep#me'", out)
        self.assertIn("TAG=v1", out)
        # An existing quote style is kept.
        quoted = secrets.rewrite_env(ENV, "DB_PASSWORD", "newpass")
        self.assertIn("DB_PASSWORD='newpass'", quoted)

    def test_env_rewrite_requires_the_key(self):
        with self.assertRaises(secrets.RotationError):
            secrets.rewrite_env(ENV, "MISSING_KEY", "x")

    def test_accounts_rewrite_only_touches_the_account_line(self):
        out = secrets.rewrite_accounts(ACCOUNTS, "awychoi", "+" + "A" * 32)
        self.assertIn("awychoi: " + "+" + "A" * 32, out)
        self.assertNotIn("5sCHBKTXle2HJL0cBJaVcWWjYf-zuOlw", out)

    def test_accounts_rewrite_requires_the_account(self):
        with self.assertRaises(secrets.RotationError):
            secrets.rewrite_accounts(ACCOUNTS, "someone-else", "+" + "A" * 32)


class PasswordTests(unittest.TestCase):
    def test_generated_passwords_are_long_and_unambiguous(self):
        for _ in range(20):
            value = secrets.generate_password()
            self.assertEqual(len(value), secrets.PASSWORD_LEN)
            self.assertTrue(set(value) <= set(secrets.PASSWORD_ALPHABET))

    def test_supplied_passwords_are_bounded(self):
        with self.assertRaises(secrets.RotationError):
            secrets.check_password("short")
        with self.assertRaises(secrets.RotationError):
            secrets.check_password("long enough\nwith newline")
        self.assertEqual(secrets.check_password("a-perfectly-fine-passphrase"), "a-perfectly-fine-passphrase")


class SpecTests(unittest.TestCase):
    def test_missing_file_means_no_rotations(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(secrets.load_specs(FakeSettings(Path(tmp))), [])

    def test_notes_are_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            write_rotation(repo, {"_note": "docs", "nas/searxng": [{"key": "SEARXNG_SECRET_KEY"}]})
            specs = secrets.load_specs(FakeSettings(repo))
            self.assertEqual([(s.host, s.stack, s.key, s.mechanism, s.file) for s in specs], [
                ("nas", "searxng", "SEARXNG_SECRET_KEY", "env", ".env"),
            ])

    def test_bad_entries_are_rejected(self):
        cases = [
            {"noslash": [{"key": "K"}]},
            {"h/s": [{"key": "bad key"}]},
            {"h/s": [{"key": "K", "mechanism": "shell"}]},
            {"h/s": [{"key": "K", "file": "docker-compose.yml"}]},
            {"h/s": [{"key": "K", "mechanism": "copyparty-accounts"}]},
        ]
        for payload in cases:
            with tempfile.TemporaryDirectory() as tmp:
                repo = Path(tmp)
                write_rotation(repo, payload)
                with self.assertRaises(secrets.RotationError, msg=payload):
                    secrets.load_specs(FakeSettings(repo))


class PlanTests(unittest.TestCase):
    def _settings(self, repo: Path, payload) -> FakeSettings:
        write_rotation(repo, payload)
        return FakeSettings(repo)

    def test_env_plan_is_stable_and_never_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._settings(Path(tmp), {"nas/searxng": [{"key": "SEARXNG_SECRET_KEY"}]})
            stack = FakeStack({".env": ENV}, host="nas", name="searxng", services=())
            rotation = secrets.plan(settings, object(), stack, "SEARXNG_SECRET_KEY")
            self.assertRegex(rotation.value, "^[A-Za-z0-9]{24}$")
            self.assertEqual(rotation.generated, rotation.value)
            self.assertIn(f"SEARXNG_SECRET_KEY={rotation.value}", rotation.new_content)
            self.assertIn("DB_PASSWORD='keep#me'", rotation.new_content)
            self.assertEqual(rotation.review, secrets.reapply_review(rotation.spec, ENV, rotation.value))
            self.assertEqual(rotation.old_content, ENV)

    def test_env_plan_rejects_a_short_supplied_password(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._settings(Path(tmp), {"nas/searxng": [{"key": "SEARXNG_SECRET_KEY"}]})
            stack = FakeStack({".env": ENV}, host="nas", name="searxng", services=())
            with self.assertRaises(secrets.RotationError):
                secrets.plan(settings, object(), stack, "SEARXNG_SECRET_KEY", "short")

    def test_copyparty_plan_hashes_then_rewrites(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._settings(Path(tmp), {"nas/copyparty": [COPYPARTY_SPEC]})
            stack = FakeStack({"accounts.conf": ACCOUNTS}, host="nas", name="copyparty")
            verifier = "+" + "B" * 32
            with mock.patch.object(secrets, "hash_account", return_value=verifier) as hasher:
                rotation = secrets.plan(settings, "host-object", stack, "awychoi")
            hasher.assert_called_once()
            self.assertEqual(hasher.call_args.args[1], "copyparty")
            self.assertEqual(rotation.value, verifier)
            self.assertIn(f"awychoi: {verifier}", rotation.new_content)
            self.assertEqual(rotation.generated, hasher.call_args.args[3])

    def test_copyparty_plan_refuses_a_usernames_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._settings(Path(tmp), {"nas/copyparty": [COPYPARTY_SPEC]})
            stack = FakeStack(
                {
                    "accounts.conf": ACCOUNTS,
                    "copyparty.conf": "[global]\n  usernames\n  ah-alg: argon2\n",
                },
                host="nas",
                name="copyparty",
            )
            with mock.patch.object(secrets, "hash_account", return_value="+x"):
                with self.assertRaises(secrets.RotationError):
                    secrets.plan(settings, object(), stack, "awychoi")

    def test_copyparty_plan_requires_the_service(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._settings(Path(tmp), {"nas/copyparty": [COPYPARTY_SPEC]})
            stack = FakeStack({"accounts.conf": ACCOUNTS}, host="nas", name="copyparty", services=("other",))
            with mock.patch.object(secrets, "hash_account", return_value="+" + "C" * 32):
                with self.assertRaises(secrets.RotationError):
                    secrets.plan(settings, object(), stack, "awychoi")

    def test_review_changes_when_the_file_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._settings(Path(tmp), {"nas/searxng": [{"key": "SEARXNG_SECRET_KEY"}]})
            stack = FakeStack({".env": ENV}, host="nas", name="searxng", services=())
            rotation = secrets.plan(settings, object(), stack, "SEARXNG_SECRET_KEY")
            self.assertNotEqual(
                rotation.review,
                secrets.reapply_review(rotation.spec, ENV + "EDITED=1\n", rotation.value),
            )

    def test_apply_value_rejects_unsafe_values(self):
        env_spec = secrets.SecretSpec("h", "s", "K", "K", "env", ".env", "")
        with self.assertRaises(secrets.RotationError):
            secrets.apply_value(env_spec, ENV, "a\nb")
        acct_spec = secrets.SecretSpec("h", "s", "u", "u", "copyparty-accounts", "accounts.conf", "c")
        with self.assertRaises(secrets.RotationError):
            secrets.apply_value(acct_spec, ACCOUNTS, "not-a-verifier")

    def test_describe_reports_readiness(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._settings(Path(tmp), {"nas/copyparty": [COPYPARTY_SPEC], "nas/searxng": [{"key": "SEARXNG_SECRET_KEY"}]})
            specs = secrets.load_specs(settings)
            ready = secrets.describe(specs, FakeStack({"accounts.conf": ACCOUNTS}, host="nas", name="copyparty"))
            self.assertTrue(ready[0]["ready"])
            missing = secrets.describe(specs, FakeStack({}, host="nas", name="searxng", services=()))
            self.assertFalse(missing[0]["ready"])
            self.assertIn("missing", missing[0]["reason"])


class RotateRunTests(unittest.TestCase):
    """The lockout guarantee: a failed rotation restores the old credential."""

    def _rotation(self, new=ACCOUNTS.replace("5sCHBKTXle2HJL0cBJaVcWWjYf-zuOlw", "+" + "D" * 32)):
        spec = secrets.SecretSpec("nas", "copyparty", "awychoi", "Library account", "copyparty-accounts", "accounts.conf", "copyparty")
        return secrets.Rotation(spec=spec, old_content=ACCOUNTS, new_content=new, value="+" + "D" * 32, review="r", generated=None)

    def test_success_pushes_once_and_restarts(self):
        run = FakeRun()
        store = FakeStore()
        settings = FakeSettings(Path("/tmp"))
        stack = FakeStack({"accounts.conf": ACCOUNTS})
        with mock.patch.object(runner.stacks, "write_file") as write, \
             mock.patch.object(runner.stacks, "get_stack", return_value=stack), \
             mock.patch.object(runner.stacks, "push_files", return_value={"ok": True}) as push, \
             mock.patch.object(runner.stacks, "compose", return_value=SimpleNamespace(returncode=0, stdout="", stderr="")), \
             mock.patch.object(runner.updates, "wait_healthy", return_value={"ok": True}):
            runner.rotate_secret(settings, store, run, object(), "nas", "copyparty", self._rotation())
        write.assert_called_once()
        self.assertEqual(push.call_count, 1)
        self.assertEqual(store.events[-1][0][1], "success")

    def test_health_failure_restores_the_old_credential(self):
        run = FakeRun()
        store = FakeStore()
        settings = FakeSettings(Path("/tmp"))
        stack = FakeStack({"accounts.conf": ACCOUNTS})
        written = []
        with mock.patch.object(runner.stacks, "write_file", side_effect=lambda *a: written.append(a[4])), \
             mock.patch.object(runner.stacks, "get_stack", return_value=stack), \
             mock.patch.object(runner.stacks, "push_files", return_value={"ok": True}), \
             mock.patch.object(runner.stacks, "compose", return_value=SimpleNamespace(returncode=0, stdout="", stderr="")), \
             mock.patch.object(runner.updates, "wait_healthy", side_effect=[{"ok": False, "containers": [], "detail": "restarting"}, {"ok": True}]):
            with self.assertRaises(Exception):
                runner.rotate_secret(settings, store, run, object(), "nas", "copyparty", self._rotation())
        self.assertEqual(len(written), 2)
        self.assertIn("+" + "D" * 32, written[0])
        self.assertEqual(written[1], ACCOUNTS)  # original restored verbatim
        self.assertEqual(store.events[-1][0][1], "warning")
        self.assertTrue(any("restoring the previous credential" in line for line in run.lines))

    def test_failed_restore_is_reported_as_an_error(self):
        run = FakeRun()
        store = FakeStore()
        settings = FakeSettings(Path("/tmp"))
        stack = FakeStack({"accounts.conf": ACCOUNTS})
        with mock.patch.object(runner.stacks, "write_file"), \
             mock.patch.object(runner.stacks, "get_stack", return_value=stack), \
             mock.patch.object(runner.stacks, "push_files", return_value={"ok": False, "error": "no space"}), \
             mock.patch.object(runner.stacks, "compose", return_value=SimpleNamespace(returncode=0, stdout="", stderr="")), \
             mock.patch.object(runner.updates, "wait_healthy", return_value={"ok": True}):
            with self.assertRaises(Exception):
                runner.rotate_secret(settings, store, run, object(), "nas", "copyparty", self._rotation())
        self.assertEqual(store.events[-1][0][1], "error")


if __name__ == "__main__":
    unittest.main()
