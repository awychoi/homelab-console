"""Entry point for the packaged console backend (PyInstaller).

The Swift shell runs this as a child process with ``serve --port …``; the older
browser-launcher bundle passed only flags. Accept both: if no subcommand is
present, default to ``serve``.
"""

from __future__ import annotations

import sys

from homelab_console.cli import main

SUBCOMMANDS = {"serve", "status", "check-releases", "doctor", "init"}


def launch() -> int:
    # Global flags (--repo, --verbose) must precede the subcommand. Everything
    # else on the command line belongs to the subcommand.
    args = list(sys.argv[1:])
    global_args: list[str] = []
    for flag in ("--repo", "--verbose"):
        if flag in args:
            index = args.index(flag)
            take = 2 if flag == "--repo" else 1
            global_args.extend(args[index : index + take])
            del args[index : index + take]
    if args and args[0] in SUBCOMMANDS:
        return main([*global_args, *args])
    return main([*global_args, "serve", *args])


if __name__ == "__main__":
    raise SystemExit(launch())
