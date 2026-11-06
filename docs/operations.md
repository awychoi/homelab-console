# Using the console

This page covers the day-to-day surface after the first deploy: settings,
update sources, stack safety profiles, updates, Repo street and workspace
backups. The first-run walkthrough is [First-time setup](getting-started.md);
the implementation reference is [Homelab Console](console.md).

## Where things live

| Path | What it is |
|---|---|
| `stacks/<host>/<stack>/` | The deployable copy: compose file, `.env`, configs. Pushed over SSH from here. |
| `scripts/hosts.json` | Fleet inventory. The console preserves fields added by other tools. |
| `.hlc/` | Gitignored local state: SQLite history, run logs, release cache, snapshots, workspace backups. |
| `.hlc/config.json` | Local settings, mode 600. May contain GitHub/Gitea tokens. |
| `.hlc/snapshots/` | Pre-update copies of repo stack files, mode 600. |
| `.hlc/runs/*.log` | Full log for every deploy/update/rollback run. |

The workspace is the desired state. The Pi is a replaceable runtime; the console
does not edit files on it by hand. Deploys start with a reviewed diff and a
mandatory plan hash; update and Gitea sync actions carry a review hash bound to
what the dialog showed.

## Settings

Open **Settings** in the left rail. Most defaults are usable for a first Pi.
Use the sticky **Jump to** bar at the top of the page to reach Appearance,
Collection, Sources, Console, Repo fleet, Backup, Safety or Maintenance
directly; the active section follows you as you scroll.

### Collection and updates

- **Telemetry poll interval** — how often the SSH fleet probe runs.
- **Release check cache** — how long detected releases are reused.
- **History retention** — how many days of host/stack samples stay in SQLite.
- **Health verification timeout** — how long an update waits for containers and
  loopback ports after a recreate.
- **Remote stack root** — where stacks are pushed on every host. Defaults to
  `/opt/stacks`. Changing it does not move existing stacks.
- **Check releases on start** — refresh the upstream cache at launch.
- **Enable automatic updates** — master switch for per-stack `auto` policies.
  Critical stacks are always excluded. Leave this off until you have watched a
  few manual updates.

### Update checks and sources

Docker Hub images are compared against the actual tag list. `lscr.io/linuxserver/*`
resolves to the Docker Hub `linuxserver/*` mirror. GHCR images use a known
GitHub source, an OCI `org.opencontainers.image.source` label when the container
exposes one, or a source you add.

Images on `quay.io` or a private registry are marked **unmanaged** rather than
guessed. If an image is unmanaged or its check fails, add a source.

#### What an update source does

An update source is **metadata for the update checker**. It answers “where does
this image publish releases?” It does not edit the compose file, change an image
pin, pull an image, or deploy anything.

1. Open **Settings → Update sources**.
2. Find the image under **Detected images** and choose its action
   (**Add source**, **Add GitHub source**, or **Edit/Change source**), or use
   **Add update source** for an image that is not in the current stacks. The
   dialog opens with the current source prefilled.
3. Choose a source type:
   - **Docker Hub tags** — compare the image’s Docker Hub tag list. Use
     `owner/name`, or `library/name` for an official image.
   - **GitHub releases** — use GitHub releases/tags from `owner/repository`,
     with GitHub’s `v` tag convention.
   - **Compatibility-managed** — report the image, but never propose an update.
     Useful for a dependency that must follow its parent product’s matrix.
   - **No checks** — leave the image unmanaged and hide it from release checks.
4. Save. A custom source wins over the built-in catalog and auto-detection.

The card action opens the source dialog. Nothing is stored until you press
**Save source**. Sources are stored per image in `.hlc/config.json`; **Remove** deletes the override and the
image falls back to the catalog or automatic detection. The image in the stack
keeps its exact pinned reference either way.

A GitHub personal access token is optional and only raises the anonymous GitHub
API limit; Docker Hub checks need no token. Tokens are never returned by the API.

### Repo fleet

Repo street is optional and not needed to deploy to a Pi.

- Add one or more **Repo folders**. Each is scanned **recursively** for git
  checkouts, including the folder itself. Hidden folders and generated folders
  such as `node_modules`, `.venv`, `build`, `dist` and `target` are skipped; the
  scan stops at a repo and does not list its nested repos.
- Use **Manage repos…** on the Repositories page or in Settings to deselect
  checkouts. A deselected repo stays on disk but is hidden from Repo street;
  new repos under the root are selected by default. The selection is stored in
  `.hlc/config.json` and included in Backup & restore.
- **Gitea URL** and **Gitea owner** are both empty by default. Filling both in
  enables the backup mirror and the **Check Gitea** actions. Leaving either
  empty disables them. Use `https://` for a non-loopback Gitea: the token is
  sent as a header. Plain `http://` is accepted only for loopback, or with
  `HLC_GITEA_ALLOW_HTTP=1` for a network you trust.
- Gitea is a backup, not a second push remote. It compares refs at startup,
  after a commit or GitHub push for the affected repo, and on demand. Use
  **Check Gitea** then sync, or **Queue both**.
- A missing Gitea repo relies on Gitea's push-to-create. The GitHub owner must
  match `gitea_owner`.
- In the native app, the Gitea token is owned by the app: use
  **Homelab Console → Settings… (⌘,)** and it goes to the macOS Keychain. The web
  page hides the field and links there.

### Appearance

Dark, Light or System. The choice is stored server-side and mirrored in
`localStorage` for a flash-free first paint.

## Stack safety and backup profiles

Every stack page has a **Safety & backup** card. It drives the update dialog,
the critical-stack gate and auto-update exclusion.

- **Update level** — `low`, `medium`, `high` or `critical`. Critical stacks are
  manual-only; bulk and auto-update skip them.
- **Data to check** — paths shown in the update dialog before an update.
- **Backup / pre-update note** — the instruction shown in the dialog. It is
  guidance only and runs nothing.
- **DNS-adjacent** — adds a short resolution warning to the dialog.
- **PostgreSQL dump** — optionally names a container, a user env key, a base
  directory env key, and the scheduled/console backup folders. The console then
  offers **Dump fresh copy on Pi** in the update dialog.

Profiles are stored per `host/stack` in `.hlc/config.json`. The built-in
defaults (including the Immich plan) are fallbacks; **Reset to built-in** removes
the workspace override. A profile never accepts an arbitrary shell command:
database dumps always use a validated `pg_dumpall` path and are PostgreSQL only.
Data volumes are never copied automatically.

## Deploys, drift and host files

- A deploy uploads every file in the stack's repo folder, including `.env` and
  configs.
- The **Files** tab has **New file**: enter a relative path such as
  `config/prometheus/prometheus.yml`, paste the contents, and create it. Parent
  folders are created automatically; `.env`, `.env.*` and `*.env` are written
  mode 600. The file stays in the repo until you deploy.
- The **Diff** tab fetches host file hashes, shows additions/changes and any
  host-only files, and returns the plan hash used by the deploy.
- A changed plan is rejected rather than applied blind. Overwriting host files
  requires the review gate.
- Host-only files are listed separately and are never uploaded or deleted by a
  push. Remove stale runtime files deliberately from the stack's **Files** tab.
- **Folders** lists the host stack directory and bind mounts, with a **Terminal**
  action (the browser UI copies the SSH command instead).
- If the repo hash and host hash diverge, the stack is marked **drift**. The fix
  is a repo edit plus deploy, not an SSH edit.
- Stacks live exactly at `stacks/<host>/<stack>/<compose-file>`. A compose file
  one directory deeper is not adopted; **Stacks** and `./hlc doctor` list
  those paths so you can move them. Files inside a valid stack (for example
  `config/docker-compose.yml`) are ordinary assets.

## Updates and rollback

The update flow is deliberately conservative:

```text
snapshot repo files
  -> rewrite the image pin (compose line or .env variable)
  -> validate compose in a staging dir on the Pi
  -> push
  -> pull only the affected services
  -> docker compose up -d
  -> wait for Docker health and loopback TCP checks
  -> on failure: restore the snapshot, push, up again
```

- **Pins & releases** on the stack page shows the repo pin, running image and
  latest candidate. Docker Hub candidates are matched to the host's CPU
  architecture, so an amd64-only tag is not offered to an arm64 Pi; the page
  notes that the newer tag has no build for the host yet.
- **Updates** shows every actionable target with source, release age, safety and
  policy. **Update all…** queues a serial fleet/host batch; the queue stops at
  the first failure and skips critical stacks.
- A per-stack policy can be `off`, `notify` or `auto`. `auto` is inert until
  **Enable automatic updates** is on in Settings. The sweeper updates at most one
  stack at a time, only when healthy and in sync, and applies a six-hour cooldown
  after a failure.
- **Green means containers *and* ports.** Verify waits for every expected
  container to be running/healthy, then opens one loopback TCP connection to
  each published port. A closed port fails the update even when Docker health
  is green; the run log names the failing `service:port`.
- Immich's `IMMICH_VERSION` pair is collapsed into one env-pin update. Immich's
  Postgres/Redis are compatibility-managed only inside an Immich stack.
- Compose-only rollback is automatic. Data rollback is not: use the profile's
  backup note or dump.

The detailed release rules are in
[Homelab Console → Updates](console.md#updates-that-verify-themselves).

## Repo street

Repo street is the local git half of the console, separate from the Pi fleet.
It scans each configured folder recursively for git checkouts and lets you
deselect the ones you do not want to manage.

- **Repositories** lists branch/upstream, dirty files, commits ahead/behind
  origin, Gitea backup state, last commit and a GitHub link.
- **Commit** opens a checklist of changed and untracked files. Ignored and
  build-noise files start unchecked; nothing commits until you click.
- **Push** sends the current branch to GitHub, using your existing git
  credential helper. It never force-pushes and does not manage pull requests.
- **Check Gitea** compares local heads/tags with the configured mirror and shows
  a sync preview. **Queue both** pushes GitHub (when ahead), then mirrors to
  Gitea. A GitHub failure does not stop the Gitea backup.
- **Open in VS Code** uses `code -n`, with a fallback to the macOS application.

## Backup and restore

**Settings → Backup & restore** writes one zip containing `hosts.json`, every
stack file (including `.env` secrets), and all non-secret settings — including
repo folders, repo selections, update sources and safety profiles. Tokens and
`.hlc/` state are excluded.

The archive is mode 600 because it contains stack secrets. Treat it like the
`.env` files: keep it off shared storage and delete old copies.

Restore validates the archive, writes a `pre-restore-*.zip` into
`.hlc/backups/`, then replaces hosts, stacks and settings. Telemetry, run logs
and snapshots stay. Use **Choose file…** for a native picker, or type the
archive path.

## Next steps

- [Troubleshooting](troubleshooting.md) — common failures and current limits.
- [Homelab Console](console.md) — architecture, API and detailed behavior.
