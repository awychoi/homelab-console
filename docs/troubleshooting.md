# Troubleshooting

Start with the read-only checks, then use the symptom table. The console keeps
logs on disk; they usually say more than the UI banner.

## First checks

```sh
./hlc doctor                  # repo, tools, hosts.json, keys, SSH, port
./hlc status                  # one-shot fleet snapshot
./hlc check-releases          # upstream checks with errors
```

- Native app sidecar log: `~/Library/Logs/Homelab Console/sidecar.log`
- Workspace log: `<workspace>/.hlc/logs/console.log`
- Run logs: `<workspace>/.hlc/runs/*.log`
- The backend binds loopback and requires its per-process token; open the URL
  printed by `./hlc`, not the port by hand from another machine.

## Common symptoms

| Symptom | Check |
|---|---|
| `python3` is missing or older than 3.10 | Install a current Python and create `.venv`; `./hlc` prefers it. |
| Browser/API says `unexpected Host header` (421) | Open the console through `127.0.0.1:<port>` or `localhost:<port>`, not a DNS name. For a deliberate non-loopback bind, set `HLC_ALLOWED_HOSTS` to the exact `host:port` values clients use. |
| `port 8765 is not free` | `./hlc serve --port 8766`; the native app picks a free port itself. |
| `./hlc doctor` reports `MISS no SSH key` | Expected before onboarding; create a dedicated key in the wizard. |
| App shows no hosts after install | It is in its empty workspace. Use **Choose Workspace…** (or `HLC_REPO`) to point it at yours, or add a host from scratch. An old pointer at the checkout root resolves its `workspace/` folder automatically. |
| App says “backend error” or the window never loads | Read the sidecar log, then the workspace console log. |
| macOS refuses to open the app | Right-click → Open, or Privacy & Security → Open Anyway. The build is not notarized. |
| SSH fails in the wizard | `./hlc doctor`; check address, user, port, key and `~/.ssh/config`. SSH usernames are case-sensitive (`pi`, not `Pi`). Host-key changes fail closed. |
| `~/.ssh/config` was not written | Some sandboxes deny writes outside the project. The host entry is still saved; add the stanza manually or run outside the sandbox. |
| Host is online but deploy fails on permissions | The SSH user needs write access to the remote stack root; a container may have chowned a bind mount. Follow the printed `sudo chown` command, or grant passwordless sudo for the specific `mkdir`/`chown` repair. |
| Deploy says a plan hash is required | Reload the stack, review the plan again, and use **Push & up**. The hash is mandatory; a stale hash returns “diff changed”. |
| “Host key was not pinned” during onboarding | The verification step runs `ssh-keyscan` and writes `~/.ssh/known_hosts`; make sure the address/port is reachable and that `~/.ssh` is writable, then retry. |
| `docker compose` not found on the Pi | Install the Compose v2 plugin; the app calls `docker compose`, not `docker-compose`. |
| Compose validation fails | Usually a missing `.env` value or invalid YAML; the error is in the run log and deploy dialog. |
| Compose parse error in the console | The built-in parser is line-based and intentionally small. Keep the top-level `services:` block conventional; the console will not adopt unusual YAML shapes. |
| Stacks shows “Ignored nested stack layout” | Stack discovery is exactly `stacks/<host>/<stack>/<compose-file>`. Move the reported file up to that shape; files nested inside a valid stack are still pushed as assets. |
| Repos finds no projects | The configured folder is scanned recursively, but hidden and generated folders (`node_modules`, `.venv`, `build`, `dist`, `target`) and nested projects inside a project are skipped. Choose the project folder itself, then use **Manage projects…** to check what was discovered. |
| New file says it already exists | The path is unique per stack; pick another name or edit the existing file in the Files tab. |
| GitHub checks rate-limited | Add a GitHub token in Settings; Docker Hub needs none. |
| Update fails with `no matching manifest for linux/arm64/v8` | The upstream tag does not contain an image for that host's CPU yet (often a release still publishing its arm64 build). The compose files were already rolled back, so the running container is untouched. Rebuild/update the console: release checks index Docker Hub platforms per architecture and will not offer that tag to an arm64 host. If the official per-arch image exists (for example `arm64v8/caddy:2.11.9` while `library/caddy:2.11.9` is amd64-only), it is a valid temporary pin and the console tracks it as a normal Docker Hub source. Otherwise retry after upstream publishes the missing build. |
| Update rolls back even though every container says `running/healthy` | Verify also opens each published loopback TCP port. Look for `ports failing:` in the run log or `port probe failed:` in the error; a healthy container with a closed port fails by design. Check the compose `ports:` mapping and that the process listens on it. Builds before 2026-11-05 used a probe form that always reported failure for stacks with published ports; update to the fixed build. |
| An image says **unmanaged** or its check fails | Open **Settings → Update sources**, pick the detected image, and add a Docker Hub or GitHub source. `quay.io` and private registries are deliberately not guessed. |
| A stack should be excluded from auto-update | Open the stack → **Safety & backup**, set the level to **critical**, and save. |
| Gitea says “not checked” after a commit | The console rechecks the affected project after a commit or GitHub push. If it still says not checked, press **Check Gitea**. |
| Gitea project missing / create fails | Push-to-create must be enabled for that Gitea owner, or create an empty project with the same owner/name once. A GitHub push alone does not create it. |
| Gitea says token needed | Set it in Settings (source) or the app's ⌘, window (native app), or authenticate the git credential helper. |
| `Gitea URL must use https…` | Use an `https://` URL for a non-loopback Gitea, or set `HLC_GITEA_ALLOW_HTTP=1` only on a trusted network; the token travels as a header. |
| Gitea sync asks for a reviewed dry run | Run the Gitea dry run from the project row, review the refs, then sync. A changed ref set returns a stale-preview error instead of force-pushing. |
| Repos says “unsafe local Git configuration” | The project defines an executable local config: `url.*.insteadOf`, a shell/absolute credential helper, a filter, gpg/include or a TLS/proxy override. Remove that project-local config (or use a terminal) before letting the console act on it. A normal `pushurl`, `core.hooksPath` or `osxkeychain` helper is fine. |
| A `.env` file is missing from the commit dialog | That is intentional: `.env`, `.env.*` and `*.env` are filtered server-side, even if the workspace `.gitignore` is incomplete. |
| Gitea says “unrelated history” | The local project was rewritten while the mirror still holds the old history. Rename the Gitea project to keep the old backup, or delete it and let push-to-create make a fresh mirror. |

## Current limits

These are deliberate product boundaries, not bugs to work around.

- **No Pi provisioning.** The app does not flash a Pi, install Docker, prepare
  the remote stack root, or discover hosts. Prepare each Pi first.
- **macOS only.** The native shell is AppKit, and the docs and tests assume
  macOS. The current `.app` is architecture-specific.
- **No notarized installer or auto-update.** The app is ad-hoc signed. There is
  no App Store, hosted download, or background update.
- **Loopback only.** Remote use is an SSH tunnel. There is no users-and-roles
  web service, shared server or RBAC.
- **No automatic data backups.** Updates snapshot repo files and roll compose
  back on failure. Databases are a deliberate action; a stack profile can enable
  PostgreSQL `pg_dumpall`, but other engines need your own routine.
- **No hand edits on the Pi.** The repo is desired state; every write goes
  through the diff and plan gate.
- **Tag-based release detection.** A registry re-push of the same tag with a new
  digest is not detected. Digest-pinned stacks are reported as digest-pinned.
- **Text stack files.** Files larger than 2 MB or non-UTF-8 are not pushed by the
  console. `.env` values are visible in the local file editor by design.
- **GitHub/Gitea, not a general Git host manager.** Repos pushes the
  current branch, mirrors to one configured Gitea owner, and does not create
  GitHub repos, rebase, pull or manage pull requests.

## Reporting a problem

Include the app version, controller macOS version, whether you run from source
or the `.app`, the host alias (not its address if you prefer), the failing action,
and the last relevant lines from the run log. For security issues, follow
[SECURITY.md](../SECURITY.md) instead of opening a public issue.

## Related

- [First-time setup](getting-started.md)
- [Using the console](operations.md)
- [Homelab Console](console.md)
