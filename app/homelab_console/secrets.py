"""Guarded credential rotation for stack secrets.

A rotation is a *plan*, never a command: the workspace declares which secrets
are rotatable (``scripts/rotation.json``) and the console supplies the
mechanism. Nothing in a request can name a file, a path or an argv.

Two mechanisms are supported:

``env``
    Rewrite one ``KEY=value`` line in a ``.env``-family file. The value is a
    generated password.

``copyparty-accounts``
    Rewrite one ``[accounts]`` line in a copyparty accounts file. The value is
    an argon2 verifier produced by the pinned image itself, with the password
    passed on stdin so it never reaches argv, ``ps`` or a log line.

The plaintext is held by the server-side pending plan until it is applied (or
the plan expires); the request carries only the review hash. The target file is
checked again at write time, and a copyparty rotation only counts as successful
after the new login is verified against the restarted daemon.
"""

from __future__ import annotations

import json
import re
import secrets as _random
import shlex
from dataclasses import dataclass, field
from typing import Any

from . import remote, stacks, util

ROTATION_FILE = "scripts/rotation.json"
PASSWORD_LEN = 24
# No lookalikes and nothing that needs quoting in a .env line.
PASSWORD_ALPHABET = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"

KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")
# A copyparty verifier is "+" plus 24 base64url bytes (32 chars) for every
# supported algorithm.  Use \Z, not $: $ also matches before a trailing \n,
# which would let a verifier inject a blank line into accounts.conf.
HASH_RE = re.compile(r"^\+[A-Za-z0-9_-]{16,64}\Z")
ENV_LINE_RE = re.compile(r"^(?P<lead>[ \t]*)(?P<key>[A-Za-z_][A-Za-z0-9_.-]{0,63})(?P<eq>[ \t]*=[ \t]*)(?P<value>.*)$")
MECHANISMS = ("env", "copyparty-accounts")
# The only configurations the fixed argon2 hasher reproduces exactly.
ARGON2_CONFIGS = frozenset({"argon2", "argon2,3,256,4,19"})
UNSAFE_PASSWORD_CHARS = "\"'\\$#`"


class RotationError(Exception):
    pass


@dataclass(frozen=True)
class SecretSpec:
    host: str
    stack: str
    key: str
    label: str
    mechanism: str
    file: str
    service: str

    @property
    def id(self) -> str:
        return f"{self.host}/{self.stack}:{self.key}"


@dataclass(frozen=True, repr=False)
class Rotation:
    spec: SecretSpec
    old_content: str
    new_content: str
    value: str
    review: str
    generated: str | None = field(default=None, repr=False)
    # The plaintext, kept only in the server-side pending plan so the runner
    # can prove the new credential actually authenticates.  Never serialized.
    secret: str | None = field(default=None, repr=False)

    def __repr__(self) -> str:
        # File contents and the plaintext must never reach a log line.
        return f"Rotation(spec={self.spec!r}, review={self.review!r})"

    @property
    def base(self) -> str:
        return util.sha256_text(self.old_content)


# ------------------------------------------------------------- passwords ---

def generate_password() -> str:
    return "".join(_random.choice(PASSWORD_ALPHABET) for _ in range(PASSWORD_LEN))


def check_password(value: str) -> str:
    """Reject a password that would be stored or hashed differently later.

    Compose trims and interpolates .env values, copyparty reads stdin with
    .strip(), and the login probe puts the password in an HTTP header.  Values
    with edge whitespace, quotes, ``$``, ``#`` or non-ASCII survive none of
    those round-trips reliably.
    """
    text = str(value or "")
    if len(text) < 12:
        raise RotationError("use at least 12 characters")
    if len(text) > 128:
        raise RotationError("password is too long (128 character limit)")
    if any(ch in text for ch in "\r\n\x00"):
        raise RotationError("password cannot contain newlines or NUL")
    if text != text.strip():
        raise RotationError("password cannot start or end with whitespace")
    if any(ch in text for ch in UNSAFE_PASSWORD_CHARS):
        raise RotationError("password cannot contain quotes, backslashes, $, # or `")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
        raise RotationError("password cannot contain control characters")
    if not text.isascii():
        raise RotationError("password must be printable ASCII")
    return text


# ---------------------------------------------------------------- plans ----

def _is_env_file(name: str) -> bool:
    base = str(name or "").rsplit("/", 1)[-1].lower()
    return base == ".env" or base.startswith(".env.") or base.endswith(".env")


def effective_env_value(text: str, key: str) -> str | None:
    """The value Compose would use for *key*: last assignment wins, quotes and
    an inline comment are resolved the way the dotenv loader does."""
    found: str | None = None
    for line in text.splitlines():
        body = line.strip()
        if not body or body.startswith("#") or "=" not in body:
            continue
        if body.startswith("export "):
            body = body[7:].lstrip()
        name, _, raw = body.partition("=")
        if name.strip() != key:
            continue
        value = raw.strip()
        if len(value) >= 2 and value[0] in "'\"" and value[-1] == value[0]:
            value = value[1:-1]
        else:
            for index, char in enumerate(value):
                if char == "#" and index and value[index - 1].isspace():
                    value = value[:index].rstrip()
                    break
            value = value.strip()
        found = value
    return found


def _coerce(entry: Any, host: str, stack: str) -> SecretSpec:
    if not isinstance(entry, dict):
        raise RotationError(f"{host}/{stack}: every rotation entry must be an object")
    key = str(entry.get("key") or "").strip()
    if not KEY_RE.match(key):
        raise RotationError(f"{host}/{stack}: invalid secret key {key!r}")
    mechanism = str(entry.get("mechanism") or "env").strip()
    if mechanism not in MECHANISMS:
        raise RotationError(f"{host}/{stack}: unknown mechanism {mechanism!r}")
    name = str(entry.get("file") or ".env").strip()
    # Only secret-named files may be rewritten; a rotation can never touch a
    # compose file or anything else the stack needs to parse.
    if not util.is_secret_file(name):
        raise RotationError(f"{host}/{stack}: {name!r} is not a secret file")
    if mechanism == "env" and not _is_env_file(name):
        raise RotationError(f"{host}/{stack}: env rotation needs an .env-family file, not {name!r}")
    if mechanism == "copyparty-accounts" and not name.lower().endswith(".conf"):
        raise RotationError(f"{host}/{stack}: copyparty-accounts needs a .conf file, not {name!r}")
    service = str(entry.get("service") or "").strip()
    if mechanism == "copyparty-accounts" and not service:
        raise RotationError(f"{host}/{stack}: {mechanism} needs a service")
    return SecretSpec(
        host=host,
        stack=stack,
        key=key,
        label=str(entry.get("label") or key).strip(),
        mechanism=mechanism,
        file=name,
        service=service,
    )


def load_specs(settings) -> list[SecretSpec]:
    """Read scripts/rotation.json. A missing file simply means no rotations."""
    path = settings.repo / ROTATION_FILE
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise RotationError(f"cannot read {ROTATION_FILE}: {exc}")
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise RotationError(f"{ROTATION_FILE} is not valid JSON: {exc}")
    if not isinstance(data, dict):
        raise RotationError(f"{ROTATION_FILE} must be an object keyed by host/stack")
    specs: list[SecretSpec] = []
    for target, entries in data.items():
        owner = str(target or "").strip()
        if owner.startswith("_"):
            continue  # _note and friends are documentation, not targets
        if owner.count("/") != 1:
            raise RotationError(f"{ROTATION_FILE}: {owner!r} must look like host/stack")
        host, stack = owner.split("/")
        if not isinstance(entries, list):
            raise RotationError(f"{ROTATION_FILE}: {owner} must map to a list")
        for entry in entries:
            specs.append(_coerce(entry, host, stack))
    return specs


def specs_for(specs: list[SecretSpec], host: str, stack: str) -> list[SecretSpec]:
    return [spec for spec in specs if spec.host == host and spec.stack == stack]


def describe(specs: list[SecretSpec], stack: stacks.Stack) -> list[dict[str, Any]]:
    """What the stack page shows: declared secrets and whether they resolve."""
    rows: list[dict[str, Any]] = []
    for spec in specs_for(specs, stack.host, stack.name):
        present = spec.file in stack.files
        service_ok = not spec.service or spec.service in stack.service_names
        rows.append(
            {
                "key": spec.key,
                "label": spec.label,
                "mechanism": spec.mechanism,
                "file": spec.file,
                "service": spec.service,
                "ready": bool(present and service_ok),
                "reason": ""
                if present and service_ok
                else (f"{spec.file} is missing from the stack" if not present else f"service {spec.service} is not in the compose file"),
            }
        )
    return rows


# ------------------------------------------------------------- rewriters ---

def rewrite_env(text: str, key: str, value: str) -> str:
    """Replace the value of *key*, preserving indentation and any quote style.

    A repeated key is rejected: Compose resolves duplicates last-wins, so
    rewriting only the first occurrence would leave the old credential live.
    """
    lines = text.splitlines(keepends=True)
    matches: list[tuple[int, str, str, re.Match[str]]] = []
    for index, line in enumerate(lines):
        body = line.rstrip("\r\n")
        match = ENV_LINE_RE.match(body)
        if match and match.group("key") == key:
            matches.append((index, line, body, match))
    if not matches:
        raise RotationError(f"{key} is not set; add it to the file before rotating it")
    if len(matches) > 1:
        raise RotationError(
            f"{key} appears {len(matches)} times; remove the duplicate lines before rotating it"
        )
    index, line, body, match = matches[0]
    suffix = line[len(body):]
    current = match.group("value").strip()
    quote = ""
    if current[:1] in ("'", '"'):
        if current.find(current[0], 1) == -1:
            raise RotationError(f"{key} has a multi-line quoted value; rotate it by hand")
        quote = current[0]
    new_value = f"{quote}{value}{quote}" if quote else value
    lines[index] = f"{match.group('lead')}{key}{match.group('eq')}{new_value}{suffix}"
    return "".join(lines)


def rewrite_accounts(text: str, account: str, verifier: str) -> str:
    """Replace the verifier for *account* inside the [accounts] section."""
    lines = text.splitlines(keepends=True)
    inside = False
    matches: list[tuple[int, str, re.Match[str]]] = []
    for index, line in enumerate(lines):
        body = line.rstrip("\r\n")
        stripped = body.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            inside = stripped.lower() == "[accounts]"
            continue
        if not inside:
            continue
        match = re.match(r"^(\s*)([^\s:#]+)(\s*:\s*)(\S.*)$", body)
        if match and match.group(2) == account:
            matches.append((index, line, match))
    if not matches:
        raise RotationError(f"{account} is not in the [accounts] section of this file")
    if len(matches) > 1:
        raise RotationError(
            f"{account} appears {len(matches)} times in [accounts]; remove the duplicate lines before rotating it"
        )
    index, line, match = matches[0]
    body = line.rstrip("\r\n")
    suffix = line[len(body):]
    lines[index] = f"{match.group(1)}{account}{match.group(3)}{verifier}{suffix}"
    return "".join(lines)


# ---------------------------------------------------------------- hasher ---

def hash_account(host: Any, stack_name: str, service: str, password: str, remote_root: str) -> str:
    """Hash *password* inside the pinned copyparty image; the password is stdin."""
    directory = f"{remote_root.rstrip('/')}/{stack_name}"
    command = (
        f"cd {shlex.quote(directory)} && "
        f"docker compose exec -T {shlex.quote(service)} "
        "python3 -m copyparty --ah-alg argon2 --ah-gen -"
    )
    result = remote._ssh(host, command, input_text=password + "\n", timeout=90)
    text = (result.stdout or "") if getattr(result, "stdout", None) is not None else ""
    for line in reversed(text.splitlines()):
        candidate = line.strip()
        if HASH_RE.match(candidate):
            return candidate
    detail = ((getattr(result, "stderr", "") or "") or text).strip().splitlines()
    raise RotationError(
        "the hasher did not return a verifier"
        + (f": {detail[-1][:160]}" if detail else "")
    )


def _conf_lines(text: str):
    for line in text.splitlines():
        body = line.split("#", 1)[0].strip()
        if body:
            yield body


def _copyparty_config_guard(stack: stacks.Stack) -> None:
    """Refuse a copyparty config the fixed argon2 hasher cannot reproduce.

    The hasher runs without the stack config, so it always uses argon2 with
    the default parameters, no explicit salt and password-only input.  A
    config that differs would produce a verifier the daemon rejects.
    """
    seen_argon2 = False
    for name, text in stack.files.items():
        if not name.endswith(".conf"):
            continue
        section = ""
        for body in _conf_lines(text):
            if body.startswith("%"):
                raise RotationError(
                    f"{name} imports other config files; the console cannot verify their "
                    "ah-alg/ah-salt/usernames settings — rotate this account by hand"
                )
            if body.startswith("[") and body.endswith("]"):
                section = body.lower()
                continue
            if section == "[accounts]":
                continue  # account entries, not global options
            key, sep, value = body.partition(":")
            key = key.strip().lower()
            value = value.replace(" ", "").strip().lower()
            if key == "usernames":
                raise RotationError(
                    "this stack hashes usernames together with the password (usernames is set); "
                    "rotate it by hand so the verifier is computed the same way"
                )
            if key == "ah-salt":
                raise RotationError(
                    "this stack sets ah-salt; the console hasher would use a different salt — "
                    "rotate this account by hand"
                )
            if key == "ah-alg":
                if not sep or value not in ARGON2_CONFIGS:
                    raise RotationError(
                        f"this stack hashes passwords with ah-alg {value or '(unset)'}; the console "
                        "writes default argon2 verifiers — set 'ah-alg: argon2' or rotate by hand"
                    )
                seen_argon2 = True
    if not seen_argon2:
        raise RotationError(
            "this stack does not declare 'ah-alg: argon2' in a local .conf; the console cannot "
            "prove the verifier will match — rotate this account by hand"
        )
    for name, text in stack.files.items():
        if name.rsplit("/", 1)[-1] not in stacks.COMPOSE_NAMES:
            continue
        for body in _conf_lines(text):
            for flag in ("--usernames", "--ah-salt", "--ah-alg"):
                if flag in body:
                    raise RotationError(
                        f"{name} sets {flag}; the console reads the mechanism from .conf only — "
                        "move it there or rotate by hand"
                    )


def check_target(spec: SecretSpec, stack: stacks.Stack) -> None:
    """Common plan/apply checks; apply re-runs this so a changed config is caught."""
    if spec.file not in stack.files:
        raise RotationError(f"{spec.file} is not in this stack")
    if spec.mechanism != "copyparty-accounts":
        return
    _copyparty_config_guard(stack)
    if spec.service not in stack.service_names:
        raise RotationError(f"service {spec.service} is not in the compose file")


def verify_account(
    host: Any,
    stack_name: str,
    service: str,
    password: str | None,
    remote_root: str,
    port: int = 3923,
) -> None:
    """Prove the new password authenticates against the running copyparty.

    A container that comes up healthy is not proof the credential works: a
    verifier computed for the wrong config still serves the login page.  The
    password reaches the container on stdin, never argv.
    """
    if not password:
        raise RotationError("no plaintext password is available to verify the account")
    if not isinstance(port, int) or not 0 < port < 65536:
        port = 3923
    script = (
        "import sys, urllib.error, urllib.request\n"
        "pw = sys.stdin.readline().rstrip('\\n')\n"
        f"url = 'http://127.0.0.1:{port}/'\n"
        "req = urllib.request.Request(url)\n"
        "req.add_header('PW', pw)\n"
        "class NoRedirect(urllib.request.HTTPRedirectHandler):\n"
        "    def redirect_request(self, *a, **k):\n"
        "        return None\n"
        "opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect)\n"
        "try:\n"
        "    with opener.open(req, timeout=10) as r:\n"
        "        sys.exit(0 if 200 <= r.status < 300 else 3)\n"
        "except urllib.error.HTTPError as exc:\n"
        "    sys.exit(0 if 200 <= exc.code < 300 else 4)\n"
        "except Exception:\n"
        "    sys.exit(5)\n"
    )
    directory = f"{remote_root.rstrip('/')}/{stack_name}"
    command = (
        f"cd {shlex.quote(directory)} && "
        f"docker compose exec -T {shlex.quote(service)} "
        f"python3 -c {shlex.quote(script)}"
    )
    result = remote._ssh(host, command, input_text=password + "\n", timeout=45)
    if result.returncode != 0:
        detail = (
            (getattr(result, "stderr", "") or "")
            or (getattr(result, "stdout", "") or "")
        ).strip().splitlines()
        raise RotationError(
            "the new credential did not authenticate"
            + (f": {detail[-1][:160]}" if detail else "")
        )


# ---------------------------------------------------------------- review ---

def review_hash(
    host: str,
    stack: str,
    key: str,
    file: str,
    mechanism: str,
    value: str,
    base: str,
    service: str = "",
) -> str:
    payload = {
        "host": host,
        "stack": stack,
        "key": key,
        "file": file,
        "mechanism": mechanism,
        "service": service,
        "value": value,
        "base": base,
    }
    return util.sha256_text(util.stable_json(payload))


def find(settings, host: str, stack: str, key: str) -> SecretSpec:
    for spec in load_specs(settings):
        if spec.host == host and spec.stack == stack and spec.key == key:
            return spec
    raise RotationError(f"{key} is not declared as rotatable for {host}/{stack}")


def plan(settings, host_obj: Any, stack: stacks.Stack, key: str, password: str | None = None) -> Rotation:
    """Derive the value that would be written; never touches the repository."""
    spec = find(settings, stack.host, stack.name, key)
    check_target(spec, stack)
    old = stack.files[spec.file]
    generated: str | None = None
    if password is None:
        secret = generate_password()
        generated = secret
    else:
        secret = check_password(password)

    if spec.mechanism == "env":
        value = secret
        new = rewrite_env(old, spec.key, value)
        if effective_env_value(new, spec.key) != value:
            raise RotationError(
                f"{spec.key} would not read back as the generated value; "
                "quote or whitespace handling in .env is ambiguous"
            )
    else:
        value = hash_account(host_obj, stack.name, spec.service, secret, settings.remote_root)
        new = rewrite_accounts(old, spec.key, value)

    review = review_hash(
        stack.host, stack.name, spec.key, spec.file, spec.mechanism,
        value, util.sha256_text(old), service=spec.service,
    )
    return Rotation(
        spec=spec, old_content=old, new_content=new, value=value,
        review=review, generated=generated, secret=secret,
    )


def reapply_review(spec: SecretSpec, current: str, value: str) -> str:
    """Re-derive the review hash against the file as it is *now*."""
    return review_hash(
        spec.host, spec.stack, spec.key, spec.file, spec.mechanism,
        value, util.sha256_text(current), service=spec.service,
    )


def apply_value(spec: SecretSpec, current: str, value: str) -> str:
    """Rebuild the new file content from a value that passed the review gate."""
    if spec.mechanism == "env":
        text = check_password(str(value or ""))
        new = rewrite_env(current, spec.key, text)
        if effective_env_value(new, spec.key) != text:
            raise RotationError("refusing a .env value that would not read back verbatim")
        return new
    if not HASH_RE.match(str(value or "")):
        raise RotationError("refusing a value that is not a copyparty verifier")
    return rewrite_accounts(current, spec.key, value)



def changed_lines(old: str, new: str) -> dict[str, Any]:
    """The single line a rotation rewrites, for the confirmation dialog."""
    for index, (before, after) in enumerate(zip(old.splitlines(), new.splitlines()), start=1):
        if before != after:
            return {"line": index, "before": before.strip(), "after": after.strip()}
    return {}
