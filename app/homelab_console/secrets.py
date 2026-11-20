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

The plaintext only exists for the duration of :func:`plan`. What crosses the
review gate is the value that will actually be written, bound to the current
file content, so an edit in between is rejected rather than silently merged.
"""

from __future__ import annotations

import json
import re
import secrets as _random
import shlex
from dataclasses import dataclass
from typing import Any

from . import remote, stacks, util

ROTATION_FILE = "scripts/rotation.json"
PASSWORD_LEN = 24
# No lookalikes and nothing that needs quoting in a .env line.
PASSWORD_ALPHABET = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"

KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")
HASH_RE = re.compile(r"^\+[A-Za-z0-9_-]{16,}$")
ENV_LINE_RE = re.compile(r"^(?P<lead>[ \t]*)(?P<key>[A-Za-z_][A-Za-z0-9_.-]{0,63})(?P<eq>[ \t]*=[ \t]*)(?P<value>.*)$")
MECHANISMS = ("env", "copyparty-accounts")


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


@dataclass(frozen=True)
class Rotation:
    spec: SecretSpec
    old_content: str
    new_content: str
    value: str
    review: str
    generated: str | None

    @property
    def base(self) -> str:
        return util.sha256_text(self.old_content)


# ------------------------------------------------------------- passwords ---

def generate_password() -> str:
    return "".join(_random.choice(PASSWORD_ALPHABET) for _ in range(PASSWORD_LEN))


def check_password(value: str) -> str:
    """Reject an operator-supplied password that cannot be written safely."""
    text = str(value or "")
    if len(text) < 12:
        raise RotationError("use at least 12 characters")
    if len(text) > 128:
        raise RotationError("password is too long (128 character limit)")
    if any(ch in text for ch in "\r\n\x00"):
        raise RotationError("password cannot contain newlines or NUL")
    return text


# ---------------------------------------------------------------- plans ----

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
    """Replace the value of *key*, preserving indentation and any quote style."""
    lines = text.splitlines(keepends=True)
    for index, line in enumerate(lines):
        body = line.rstrip("\r\n")
        match = ENV_LINE_RE.match(body)
        if not match or match.group("key") != key:
            continue
        suffix = line[len(body):]
        current = match.group("value").strip()
        quote = ""
        if len(current) >= 2 and current[0] in "'\"" and current[-1] == current[0]:
            quote = current[0]
        new_value = f"{quote}{value}{quote}" if quote else value
        lines[index] = f"{match.group('lead')}{key}{match.group('eq')}{new_value}{suffix}"
        return "".join(lines)
    raise RotationError(f"{key} is not set; add it to the file before rotating it")


def rewrite_accounts(text: str, account: str, verifier: str) -> str:
    """Replace the verifier for *account* inside the [accounts] section."""
    lines = text.splitlines(keepends=True)
    inside = False
    for index, line in enumerate(lines):
        body = line.rstrip("\r\n")
        stripped = body.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            inside = stripped.lower() == "[accounts]"
            continue
        if not inside:
            continue
        match = re.match(r"^(\s*)([^\s:#]+)(\s*:\s*)(\S.*)$", body)
        if not match or match.group(2) != account:
            continue
        suffix = line[len(body):]
        lines[index] = f"{match.group(1)}{account}{match.group(3)}{verifier}{suffix}"
        return "".join(lines)
    raise RotationError(f"{account} is not in the [accounts] section of this file")


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


def _copyparty_config_guard(stack: stacks.Stack) -> None:
    """Rotation assumes password-only hashing; `usernames` would change it."""
    for name, text in stack.files.items():
        if not name.endswith(".conf"):
            continue
        for line in text.splitlines():
            body = line.split("#", 1)[0].strip()
            if body.split(":", 1)[0].strip() == "usernames":
                raise RotationError(
                    "this stack hashes usernames together with the password (usernames is set); "
                    "rotate it by hand so the verifier is computed the same way"
                )


# ---------------------------------------------------------------- review ---

def review_hash(host: str, stack: str, key: str, file: str, mechanism: str, value: str, base: str) -> str:
    payload = {
        "host": host,
        "stack": stack,
        "key": key,
        "file": file,
        "mechanism": mechanism,
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
    old = stack.files.get(spec.file)
    if old is None:
        raise RotationError(f"{spec.file} is not in this stack")
    generated: str | None = None
    if password is None:
        secret = generate_password()
        generated = secret
    else:
        secret = check_password(password)

    if spec.mechanism == "env":
        value = secret
        new = rewrite_env(old, spec.key, value)
    else:
        _copyparty_config_guard(stack)
        if spec.service not in stack.service_names:
            raise RotationError(f"service {spec.service} is not in the compose file")
        value = hash_account(host_obj, stack.name, spec.service, secret, settings.remote_root)
        new = rewrite_accounts(old, spec.key, value)

    review = review_hash(stack.host, stack.name, spec.key, spec.file, spec.mechanism, value, util.sha256_text(old))
    return Rotation(spec=spec, old_content=old, new_content=new, value=value, review=review, generated=generated)


def reapply_review(spec: SecretSpec, current: str, value: str) -> str:
    """Re-derive the review hash against the file as it is *now*."""
    return review_hash(spec.host, spec.stack, spec.key, spec.file, spec.mechanism, value, util.sha256_text(current))


def apply_value(spec: SecretSpec, current: str, value: str) -> str:
    """Rebuild the new file content from a value that passed the review gate."""
    if spec.mechanism == "env":
        text = str(value or "")
        if not text or len(text) > 128 or any(ch in text for ch in "\r\n\x00"):
            raise RotationError("refusing an unsafe secret value")
        return rewrite_env(current, spec.key, text)
    if not HASH_RE.match(str(value or "")):
        raise RotationError("refusing a value that is not a copyparty verifier")
    return rewrite_accounts(current, spec.key, value)



def changed_lines(old: str, new: str) -> dict[str, Any]:
    """The single line a rotation rewrites, for the confirmation dialog."""
    for index, (before, after) in enumerate(zip(old.splitlines(), new.splitlines()), start=1):
        if before != after:
            return {"line": index, "before": before.strip(), "after": after.strip()}
    return {}
