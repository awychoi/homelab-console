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

COPYPARTY_CONF = "[global]\n  ah-alg: argon2\n"


class FakeSettings:
    def __init__(self, repo: Path, remote_root: str = "/opt/stacks"):
        self.repo = repo
        self.remote_root = remote_root
        self.backups_dir = repo / "backups"
        self.values = {}

    def get(self, key, default=None):
        return self.values.get(key, default)


class FakeStack:
    def __init__(self, files, host="nas", name="copyparty", services=("copyparty",)):
        self.host = host
        self.name = name
        self.files = dict(files)
        self.hashes = {rel: util.sha256_text(text) for rel, text in files.items()}
        self.service_names = list(services)
        self.services = []
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

    def test_duplicate_env_key_is_rejected(self):
        with self.assertRaises(secrets.RotationError):
            secrets.rewrite_env("A=one\nB=keep\nA=two\n", "A", "ROTATED")

    def test_multiline_quoted_env_value_is_rejected(self):
        with self.assertRaises(secrets.RotationError):
            secrets.rewrite_env('A="unterminated\n', "A", "ROTATED")

    def test_quoted_env_value_with_a_comment_keeps_the_quote(self):
        self.assertEqual(
            secrets.rewrite_env('A="old" # note\n', "A", "new-value-123"),
            'A="new-value-123"\n',
        )

    def test_duplicate_account_is_rejected(self):
        text = "[accounts]\n  awychoi: +OLD\n  awychoi: +OLDER\n"
        with self.assertRaises(secrets.RotationError):
            secrets.rewrite_accounts(text, "awychoi", "+" + "A" * 32)

    def test_effective_env_value_matches_compose(self):
        self.assertEqual(secrets.effective_env_value(ENV, "DB_PASSWORD"), "keep#me")
        self.assertEqual(secrets.effective_env_value("A=1\nA=2\n", "A"), "2")
        self.assertEqual(secrets.effective_env_value("A='quoted'\n", "A"), "quoted")
        self.assertEqual(secrets.effective_env_value("A=value # note\n", "A"), "value")
        self.assertIsNone(secrets.effective_env_value(ENV, "MISSING"))


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

    def test_supplied_passwords_cannot_change_shape(self):
        for unsafe in (
            "  paddedpassword  ",
            "            ",
            'quote"password',
            "dollar$password",
            "hash#password",
            "back\\slashpassword",
            "unicode-pässword",
        ):
            with self.assertRaises(secrets.RotationError, msg=unsafe):
                secrets.check_password(unsafe)

    def test_verifier_shape_rejects_trailing_newline_and_overlong(self):
        spec = secrets.SecretSpec("h", "s", "u", "u", "copyparty-accounts", "accounts.conf", "c")
        with self.assertRaises(secrets.RotationError):
            secrets.apply_value(spec, ACCOUNTS, "+" + "A" * 32 + "\n")
        with self.assertRaises(secrets.RotationError):
            secrets.apply_value(spec, ACCOUNTS, "+" + "A" * 80)


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
            {"h/s": [{"key": "K", "file": "id_rsa"}]},
            {"h/s": [{"key": "K", "mechanism": "copyparty-accounts"}]},
            {"h/s": [{"key": "K", "mechanism": "copyparty-accounts", "file": ".env", "service": "c"}]},
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
            self.assertEqual(rotation.secret, rotation.value)
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
            stack = FakeStack({"accounts.conf": ACCOUNTS, "copyparty.conf": COPYPARTY_CONF}, host="nas", name="copyparty")
            verifier = "+" + "B" * 32
            with mock.patch.object(secrets, "hash_account", return_value=verifier) as hasher:
                rotation = secrets.plan(settings, "host-object", stack, "awychoi")
            hasher.assert_called_once()
            self.assertEqual(hasher.call_args.args[1], "copyparty")
            self.assertEqual(rotation.value, verifier)
            self.assertEqual(rotation.secret, hasher.call_args.args[3])
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

    def test_copyparty_plan_refuses_an_unsupported_ah_alg(self):
        cases = (
            "[global]\n  ah-alg: scrypt\n",
            "[global]\n  ah-alg: argon2,2,64,2,19\n",
            "[global]\n  ah-alg: none\n",
            "[global]\n  ah-alg: argon2\n  ah-salt: fixed\n",
            "[global]\n  ah-alg: argon2\n  % /cfg/extra.conf\n",
            "[global]\n  p: 3923\n",
        )
        for text in cases:
            with tempfile.TemporaryDirectory() as tmp:
                settings = self._settings(Path(tmp), {"nas/copyparty": [COPYPARTY_SPEC]})
                stack = FakeStack({"accounts.conf": ACCOUNTS, "copyparty.conf": text}, host="nas", name="copyparty")
                with mock.patch.object(secrets, "hash_account", return_value="+" + "A" * 32):
                    with self.assertRaises(secrets.RotationError, msg=text):
                        secrets.plan(settings, object(), stack, "awychoi")

    def test_copyparty_plan_refuses_compose_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._settings(Path(tmp), {"nas/copyparty": [COPYPARTY_SPEC]})
            stack = FakeStack(
                {
                    "accounts.conf": ACCOUNTS,
                    "copyparty.conf": COPYPARTY_CONF,
                    "docker-compose.yml": "services:\n  copyparty:\n    command: --usernames\n",
                },
                host="nas",
                name="copyparty",
            )
            with mock.patch.object(secrets, "hash_account", return_value="+" + "A" * 32):
                with self.assertRaises(secrets.RotationError):
                    secrets.plan(settings, object(), stack, "awychoi")

    def test_copyparty_plan_requires_the_service(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._settings(Path(tmp), {"nas/copyparty": [COPYPARTY_SPEC]})
            stack = FakeStack(
                {"accounts.conf": ACCOUNTS, "copyparty.conf": COPYPARTY_CONF},
                host="nas", name="copyparty", services=("other",),
            )
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
        with self.assertRaises(secrets.RotationError):
            secrets.apply_value(env_spec, ENV, "short")
        self.assertIn("K=", secrets.apply_value(env_spec, "K=old\n", "long-enough-value"))
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
    """Snapshot, verify and roll back; a lockout must not be reported as success."""

    def _rotation(self, new=ACCOUNTS.replace("5sCHBKTXle2HJL0cBJaVcWWjYf-zuOlw", "+" + "D" * 32)):
        spec = secrets.SecretSpec("nas", "copyparty", "awychoi", "Library account", "copyparty-accounts", "accounts.conf", "copyparty")
        return secrets.Rotation(
            spec=spec, old_content=ACCOUNTS, new_content=new, value="+" + "D" * 32,
            review="r", generated=None, secret="new-password-123",
        )

    def test_success_snapshots_writes_one_file_and_verifies(self):
        run = FakeRun()
        store = FakeStore()
        settings = FakeSettings(Path("/tmp"))
        stack = FakeStack({"accounts.conf": ACCOUNTS})
        order = []
        def snapshot(*args, **kwargs):
            order.append("snapshot")
            return {"id": "snap-1"}
        def write(*args, **kwargs):
            order.append(("write", args[4], kwargs.get("base_hash")))
        with mock.patch.object(runner.updates, "create_snapshot", side_effect=snapshot) as snap, \
             mock.patch.object(runner.stacks, "write_file", side_effect=write) as wrote, \
             mock.patch.object(runner.stacks, "get_stack", return_value=stack), \
             mock.patch.object(runner.stacks, "push_files", return_value={"ok": True}) as push, \
             mock.patch.object(runner.stacks, "compose", return_value=SimpleNamespace(returncode=0, stdout="", stderr="")), \
             mock.patch.object(runner.updates, "wait_healthy", return_value={"ok": True}), \
             mock.patch.object(runner, "verify_account") as verify:
            runner.rotate_secret(settings, store, run, object(), "nas", "copyparty", self._rotation())
        self.assertEqual(order[0], "snapshot")
        self.assertEqual(order[1][0], "write")
        self.assertEqual(order[1][1], self._rotation().new_content)
        self.assertEqual(order[1][2], self._rotation().base)
        snap.assert_called_once()
        self.assertEqual(snap.call_args.args[3], "rotate")
        wrote.assert_called_once()
        push.assert_called_once()
        self.assertEqual(push.call_args.args[2], {"accounts.conf": self._rotation().new_content})
        verify.assert_called_once()
        self.assertEqual(verify.call_args.args[3], "new-password-123")
        self.assertEqual(store.events[-1][0][1], "success")

    def test_health_failure_restores_the_old_credential(self):
        run = FakeRun()
        store = FakeStore()
        settings = FakeSettings(Path("/tmp"))
        stack = FakeStack({"accounts.conf": ACCOUNTS})
        written = []
        def write(*a, **k):
            written.append(a[4])
            stack.files[a[3]] = a[4]
        with mock.patch.object(runner.updates, "create_snapshot", return_value={"id": "snap-1"}), \
             mock.patch.object(runner.stacks, "write_file", side_effect=write), \
             mock.patch.object(runner.stacks, "get_stack", return_value=stack), \
             mock.patch.object(runner.stacks, "push_files", return_value={"ok": True}), \
             mock.patch.object(runner.stacks, "compose", return_value=SimpleNamespace(returncode=0, stdout="", stderr="")), \
             mock.patch.object(runner, "verify_account"), \
             mock.patch.object(runner.updates, "wait_healthy", side_effect=[{"ok": False, "containers": [], "detail": "restarting"}, {"ok": True}]):
            with self.assertRaises(Exception):
                runner.rotate_secret(settings, store, run, object(), "nas", "copyparty", self._rotation())
        self.assertEqual(len(written), 2)
        self.assertIn("+" + "D" * 32, written[0])
        self.assertEqual(written[1], ACCOUNTS)  # original restored verbatim
        self.assertEqual(store.events[-1][0][1], "warning")
        self.assertTrue(any("restoring the previous credential" in line for line in run.lines))

    def test_login_probe_failure_restores_the_old_credential(self):
        run = FakeRun()
        store = FakeStore()
        settings = FakeSettings(Path("/tmp"))
        stack = FakeStack({"accounts.conf": ACCOUNTS})
        written = []
        def write(*a, **k):
            written.append(a[4])
            stack.files[a[3]] = a[4]
        with mock.patch.object(runner.updates, "create_snapshot", return_value={"id": "snap-1"}), \
             mock.patch.object(runner.stacks, "write_file", side_effect=write), \
             mock.patch.object(runner.stacks, "get_stack", return_value=stack), \
             mock.patch.object(runner.stacks, "push_files", return_value={"ok": True}), \
             mock.patch.object(runner.stacks, "compose", return_value=SimpleNamespace(returncode=0, stdout="", stderr="")), \
             mock.patch.object(runner, "verify_account", side_effect=secrets.RotationError("401")), \
             mock.patch.object(runner.updates, "wait_healthy", return_value={"ok": True}):
            with self.assertRaises(Exception):
                runner.rotate_secret(settings, store, run, object(), "nas", "copyparty", self._rotation())
        self.assertEqual(len(written), 2)
        self.assertEqual(written[1], ACCOUNTS)
        self.assertEqual(store.events[-1][0][1], "warning")
        self.assertTrue(any("verifying the new login" in line for line in run.lines))

    def test_write_failure_does_not_clobber_the_file(self):
        run = FakeRun()
        store = FakeStore()
        settings = FakeSettings(Path("/tmp"))
        stack = FakeStack({"accounts.conf": ACCOUNTS})
        with mock.patch.object(runner.updates, "create_snapshot", return_value={"id": "snap-1"}), \
             mock.patch.object(runner.stacks, "write_file", side_effect=runner.stacks.StackError("changed")) as write, \
             mock.patch.object(runner.stacks, "get_stack", return_value=stack), \
             mock.patch.object(runner.stacks, "push_files", return_value={"ok": True}) as push, \
             mock.patch.object(runner.stacks, "compose", return_value=SimpleNamespace(returncode=0, stdout="", stderr="")), \
             mock.patch.object(runner.updates, "wait_healthy", return_value={"ok": True}):
            with self.assertRaises(Exception):
                runner.rotate_secret(settings, store, run, object(), "nas", "copyparty", self._rotation())
        write.assert_called_once()
        push.assert_not_called()
        self.assertTrue(any("nothing to restore" in line for line in run.lines))

    def test_failed_restore_is_reported_as_an_error(self):
        run = FakeRun()
        store = FakeStore()
        settings = FakeSettings(Path("/tmp"))
        stack = FakeStack({"accounts.conf": ACCOUNTS})
        def write(*a, **k):
            stack.files[a[3]] = a[4]
        with mock.patch.object(runner.updates, "create_snapshot", return_value={"id": "snap-1"}), \
             mock.patch.object(runner.stacks, "write_file", side_effect=write), \
             mock.patch.object(runner.stacks, "get_stack", return_value=stack), \
             mock.patch.object(runner.stacks, "push_files", return_value={"ok": False, "error": "no space"}), \
             mock.patch.object(runner.stacks, "compose", return_value=SimpleNamespace(returncode=0, stdout="", stderr="")), \
             mock.patch.object(runner.updates, "wait_healthy", return_value={"ok": True}):
            with self.assertRaises(Exception):
                runner.rotate_secret(settings, store, run, object(), "nas", "copyparty", self._rotation())
        self.assertEqual(store.events[-1][0][1], "error")
        self.assertIn("snap-1", store.events[-1][0][2])


class PlanCacheTests(unittest.TestCase):
    """The reviewed value is held by the server, not handed back by the client."""

    def test_plan_is_single_use_and_expires(self):
        import threading
        import time

        from homelab_console.server import ConsoleHandler

        fake = SimpleNamespace(server=SimpleNamespace(pending_plans={}, pending_lock=threading.Lock()))
        spec = secrets.SecretSpec("h", "s", "k", "k", "env", ".env", "")
        rotation = secrets.Rotation(
            spec=spec, old_content="K=old\n", new_content="K=new-value-123\n",
            value="new-value-123", review="abc", generated="new-value-123",
            secret="new-value-123",
        )
        ConsoleHandler._remember_secret_plan(fake, rotation)
        self.assertIs(ConsoleHandler._take_secret_plan(fake, "abc"), rotation)
        self.assertIsNone(ConsoleHandler._take_secret_plan(fake, "abc"))
        ConsoleHandler._remember_secret_plan(fake, rotation)
        fake.server.pending_plans["abc"] = (time.time() - 1, rotation)
        self.assertIsNone(ConsoleHandler._take_secret_plan(fake, "abc"))

    def test_rotation_repr_hides_file_contents_and_plaintext(self):
        spec = secrets.SecretSpec("h", "s", "k", "k", "env", ".env", "")
        rotation = secrets.Rotation(
            spec=spec, old_content="K=old-secret\n", new_content="K=new-value-123\n",
            value="new-value-123", review="abc", generated="new-value-123",
            secret="new-value-123",
        )
        text = repr(rotation)
        self.assertNotIn("old-secret", text)
        self.assertNotIn("new-value-123", text)
        self.assertIn("review='abc'", text)


if __name__ == "__main__":
    unittest.main()
