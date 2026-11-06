"""Command line entry point for the Homelab Console."""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import signal
import sys
import time
import webbrowser
from pathlib import Path

from . import releases, sshkeys, stacks, updates, util
from .app_service import AppService
from .config import APP_NAME, VERSION, Settings, TokenRegistry, ensure_workspace, reset_settings
from .hosts import HostStore
from .server import make_server
from .store import Store

log = logging.getLogger("hlc.cli")


def _setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-5s %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )


def _setup_file_logging(path: Path, verbose: bool = False) -> None:
    """A windowed .app has no stdout, so failures need somewhere to land."""
    util.ensure_dir(path.parent)
    handler = logging.FileHandler(path, encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-5s %(name)s  %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    root.addHandler(handler)


def _bind_server(args, app: AppService, token: str):
    """Bind the requested port, falling back to an ephemeral one for the app."""
    attempts = [args.port]
    if util.is_frozen() and args.port:
        attempts.append(0)
    last: OSError | None = None
    for port in attempts:
        try:
            return make_server(args.host, port, app, token)
        except OSError as exc:
            last = exc
            if port == args.port:
                log.warning("cannot bind %s:%s (%s)", args.host, port, exc)
    raise last if last else OSError("cannot bind console server")


def _load_settings(args) -> Settings:
    """Load settings once and make them the process-wide default.

    AppService takes this object, but sshkeys (control sockets) and a few
    helpers call config.get_settings(). Without this reset, --repo and the
    packaged app could disagree with each other about the workspace.
    """
    settings = Settings.load(Path(args.repo) if args.repo else None)
    reset_settings(settings)
    return settings


def cmd_serve(args) -> int:
    settings = _load_settings(args)
    if util.is_frozen():
        _setup_file_logging(settings.state_dir / "logs" / "console.log", verbose=args.verbose)
    util.watch_parent()
    tokens = TokenRegistry()
    if args.token:
        os.environ["HLC_TOKEN"] = args.token
        tokens = TokenRegistry()
    app = AppService(settings)
    app.start()
    try:
        server = _bind_server(args, app, tokens.token)
    except OSError as exc:
        app.stop()
        print(f"error: cannot bind {args.host}:{args.port} — {exc}", file=sys.stderr)
        print("hint: another process may be using the port; pass --port 8766", file=sys.stderr)
        return 2

    url = f"http://{args.host}:{server.server_port}/"
    print(f"{APP_NAME} {VERSION}")
    if util.is_frozen():
        # Windowed apps have no stdout; the URL belongs in the log.
        log.info("console listening on %s (repo=%s)", url, settings.repo)
    print(f"  repo:  {settings.repo}")
    print(f"  state: {settings.state_dir}")
    print(f"  fleet: {', '.join(app.hosts.aliases()) or '(none configured)'}")
    print(f"  url:   {url}")
    print("  stop:  Ctrl-C")
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print("  warning: bound beyond loopback; anyone who can reach this port can deploy.")
    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    def _stop(_signum=None, _frame=None):
        raise KeyboardInterrupt

    try:
        signal.signal(signal.SIGTERM, _stop)
    except (ValueError, OSError):
        pass
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\nshutting down…")
    finally:
        server.server_close()
        app.stop()
    return 0


def _seed_example(source: Path, target: Path) -> None:
    """Copy the example workspace without clobbering anything the user edited."""
    # A developer who ran the demo leaves .hlc/ (and __pycache__) beside the
    # example. That is local state, possibly with tokens, and must never be
    # seeded into a new workspace.
    skip_parts = {".git", ".hlc", "__pycache__"}
    skip_names = {".DS_Store"}
    for item in sorted(source.rglob("*")):
        relative = item.relative_to(source)
        if any(part in skip_parts for part in relative.parts) or item.name in skip_names:
            continue
        if item.suffix in {".pyc", ".pyo"}:
            continue
        dest = target / relative
        if item.is_dir():
            util.ensure_dir(dest)
        elif not dest.exists():
            util.ensure_dir(dest.parent)
            dest.write_bytes(item.read_bytes())


def cmd_init(args) -> int:
    target = Path(args.path or "workspace").expanduser().resolve()
    non_empty = target.exists() and any(target.iterdir())
    if non_empty and not (target / "stacks").is_dir() and not args.force:
        print(
            f"error: {target} is not empty and does not look like a workspace "
            "(no stacks/ directory); pass --force to initialise it anyway",
            file=sys.stderr,
        )
        return 2
    source = util.REPO_ROOT / "examples" / "workspace"
    if args.example and source.is_dir():
        # Seed before ensure_workspace so the example hosts.json wins over the
        # minimal one-file stub.
        _seed_example(source, target)
    ensure_workspace(target)
    if args.example and not source.is_dir():
        print("note: examples/workspace is not bundled here; created an empty workspace")
    if shutil.which("git") and not (target / ".git").exists():
        result = util.run(["git", "init", "-q", str(target)], timeout=15)
        if result.returncode != 0:
            print(f"note: git init failed: {result.stderr.strip()}", file=sys.stderr)
    print(f"{APP_NAME}: workspace ready at {target}")
    print(f"  next:  HLC_REPO={target} ./hlc")
    print("  or:    Homelab Console → Choose Workspace…")
    return 0


def cmd_status(args) -> int:
    settings = _load_settings(args)
    app = AppService(settings)
    settings.values["check_releases_on_start"] = False
    print(f"{APP_NAME} {VERSION} — {settings.repo}")
    try:
        snapshot = app.monitor.collect()
    finally:
        app.stop()
    print(f"fleet collected in {snapshot.get('duration_ms')} ms\n")
    for host in snapshot["hosts"]:
        summary = host["summary"]
        temp = f"{summary['temp_c']:.1f}°C" if summary.get("temp_c") is not None else "n/a"
        print(
            f"{host['alias']:<6} {host['status']:<8} {summary['hostname']:<10} "
            f"uptime {summary['uptime']:<10} temp {temp:<7} "
            f"containers {summary['containers_running']}/{summary['containers_total']}"
        )
        if host.get("error"):
            print(f"       ! {host['error']}")
        if summary.get("throttled", {}).get("now"):
            print(f"       ! throttled: {', '.join(summary['throttled']['now'])}")
    print()
    for stack in snapshot["stacks"]:
        runtime = stack["runtime"]
        flags = []
        if stack["updates"]:
            flags.append(f"update: {', '.join(item['service'] for item in stack['updates'])}")
        if stack["unpinned"]:
            flags.append(f"unpinned: {', '.join(item['service'] for item in stack['unpinned'])}")
        if runtime.get("sync", {}).get("state") == "drift":
            flags.append("drift")
        suffix = ("  ← " + "; ".join(flags)) if flags else ""
        label = f"{stack['host']}/{stack['name']}"
        print(
            f"{label:<20} {runtime.get('status', '?'):<12} "
            f"sync={runtime.get('sync', {}).get('state', '?'):<12}{suffix}"
        )
    summary = snapshot["summary"]
    print(
        f"\nhosts {summary['hosts_online']}/{summary['hosts_total']} online · "
        f"containers {summary['containers_running']}/{summary['containers_total']} running · "
        f"{summary['updates_available']} update(s), {summary['unpinned']} unpinned"
    )
    if summary.get("remote_only"):
        print(f"unmanaged dirs on hosts: {', '.join(summary['remote_only'])}")
    return 0


def cmd_check_releases(args) -> int:
    settings = _load_settings(args)
    service = releases.ReleaseService(settings)
    all_stacks = stacks.discover(settings)
    requests = []
    seen = set()
    for stack in all_stacks:
        for svc in stack.services:
            if svc.image and svc.image.repo not in seen:
                seen.add(svc.image.repo)
                requests.append({"repo": svc.image.repo, "current": svc.image.tag if svc.image.explicit_tag else ""})
    print(f"checking {len(requests)} source(s)…")
    summary = service.refresh(requests, force=not args.cached, progress=lambda message: print(f"  {message}"))
    print()
    for stack in all_stacks:
        targets = updates.build_targets(stack, None, service)
        for target in targets:
            if target["status"] in ("current", "managed", "unmanaged"):
                continue
            print(
                f"{target['host']:<5}/{target['stack']:<10} {target['service']:<24} "
                f"{target['status']:<18} {target['current_display']:<24} → {target['latest'] or '?'}"
            )
            if target.get("error"):
                print(f"      ! {target['error']}")
    errors = [entry.error for entry in service.entries.values() if entry.error]
    if errors:
        print("\nerrors:")
        for error in errors:
            print(f"  ! {error}")
    return 0


def cmd_doctor(args) -> int:
    settings = _load_settings(args)
    problems = 0
    print(f"{APP_NAME} doctor")
    print(f"  repo:  {settings.repo}")
    print(f"  state: {settings.state_dir}")
    for binary in ("git", "ssh", "ssh-keygen", "ssh-keyscan", "curl"):
        path = shutil.which(binary)
        print(f"  {'ok ' if path else 'MISS'} {binary:<12} {path or ''}")
        problems += 0 if path else 1
    print(f"  {'ok ' if settings.stacks_dir.is_dir() else 'MISS'} stacks directory {settings.stacks_dir}")
    layout_warnings = stacks.layout_warnings(settings)
    for warning in layout_warnings:
        print(f"  WARN nested stack? {warning['path']} (expected {warning['expected']})")
    if layout_warnings:
        problems += 1
    try:
        host_store = HostStore(settings)
        print(f"  ok  hosts.json ({len(host_store.all())} hosts: {', '.join(host_store.aliases())})")
    except Exception as exc:
        print(f"  FAIL hosts.json: {exc}")
        return 1
    key = sshkeys.default_key()
    if key:
        print(f"  ok  default key {key['private']} ({key['type']}, {key['fingerprint']})")
    else:
        print("  MISS no SSH key found — onboarding will offer to create one")
        problems += 1
    print()
    for host in host_store.remote():
        key_path = Path(host.key).expanduser() if host.key else None
        result = sshkeys.test_connection(host, key=key_path)
        if result.get("ok"):
            print(f"  ok  {host.alias:<5} {host.target}:{host.port}  {result.get('hostname')} ({result.get('latency_ms')} ms)")
        else:
            print(f"  FAIL {host.alias:<5} {host.target}:{host.port}  {result.get('error')}")
            problems += 1
    # Port availability
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((args.host, args.port))
            print(f"\n  ok  port {args.host}:{args.port} is free")
        except OSError as exc:
            print(f"\n  WARN port {args.host}:{args.port} is not free: {exc}")
            problems += 1
    print("\nready" if problems == 0 else f"\n{problems} issue(s) found")
    return 0 if problems == 0 else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hlc", description=f"{APP_NAME} — local homelab control plane")
    parser.add_argument("--repo", help="homelab repo root (default: auto-detect)")
    parser.add_argument("--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command")

    init = sub.add_parser("init", help="create a workspace (stacks/, scripts/hosts.json, .gitignore)")
    init.add_argument("path", nargs="?", default="workspace", help="workspace directory (default: ./workspace)")
    init.add_argument("--example", action="store_true", help="seed the example host and whoami stack")
    init.add_argument("--force", action="store_true", help="initialise a non-empty directory")
    init.set_defaults(func=cmd_init)

    serve = sub.add_parser("serve", help="start the web console (default)")
    serve.add_argument("--host", default=os.environ.get("HLC_HOST", "127.0.0.1"))
    serve.add_argument("--port", type=int, default=int(os.environ.get("HLC_PORT", "8765")))
    serve.add_argument("--no-browser", action="store_true")
    serve.add_argument("--token", help="override the random session token (testing)")
    serve.set_defaults(func=cmd_serve)

    status = sub.add_parser("status", help="one-shot fleet snapshot in the terminal")
    status.set_defaults(func=cmd_status)

    check = sub.add_parser("check-releases", help="refresh upstream releases and print targets")
    check.add_argument("--cached", action="store_true", help="honour the cache TTL")
    check.set_defaults(func=cmd_check_releases)

    doctor = sub.add_parser("doctor", help="verify repo, tools, keys and host reachability")
    doctor.add_argument("--host", default="127.0.0.1")
    doctor.add_argument("--port", type=int, default=int(os.environ.get("HLC_PORT", "8765")))
    doctor.set_defaults(func=cmd_doctor)

    parser.set_defaults(
        func=cmd_serve,
        host=os.environ.get("HLC_HOST", "127.0.0.1"),
        port=int(os.environ.get("HLC_PORT", "8765")),
        no_browser=False,
        token=None,
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    # A windowed .app has no stderr; serve adds a file handler once the
    # workspace (and therefore the state dir) is known.
    if not util.is_frozen():
        _setup_logging(args.verbose)
    if not hasattr(args, "func"):
        args = parser.parse_args([*(argv or sys.argv[1:]), "serve"])
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        log.exception("fatal: %s", exc)
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
