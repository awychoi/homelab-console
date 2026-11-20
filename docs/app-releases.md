# App releases: source → image → stack

**Status:** proposal. None of this is implemented. The current console checks
image tags and deploys stacks; it does not yet model the source repository,
build, version policy and migration state behind an image as one durable
contract. This document proposes that contract and a phased way to add it.

## Why this exists

The unjobs migration exposed a repeatable shape:

- a source repository with version tags and a CI-built image;
- one or more compose stacks pinning that image by tag;
- a data, secret and health contract that must survive a recreate;
- a legacy build-on-the-Pi shape that has to be retired;
- cleanup and credential revocation that are easy to forget;
- a decision about what "latest" means and when a pin moves.

Today the console knows the stack and the image tag. It may know a release
source or read an OCI label, but it does not treat the source repo, build,
version policy and migration state as one durable contract. It cannot tell
whether the tag is a real release, which stacks are the same app, or what a
safe migration looks like. Future apps would repeat the same runbook.

**Generalize the contract, not the runbook.** A small app manifest in the
workspace declares source, image, versioning, deploy contract and migration
scope. The console uses it for visibility, preflight, release planning,
verification and guided migration.

## Design principles

- **The workspace is desired state.** The app manifest lives in the private
  workspace at `apps/<slug>/app.json` and is committed like `stacks/`.
- **Build stays outside the console.** CI or a local script produces the image.
  The console manages version intent, provenance, pinning, deployment and
  verification. It does not become a CI system or registry.
- **Image-based stacks only.** No source checkout on a Pi, no build context on
  a Pi. The image is the deployable artifact.
- **Releases are immutable by convention.** Tags are never overwritten; a
  rebuild gets a new tag. Digest pinning is available where strict immutability
  matters. No force-push anywhere.
- **Read-only first, then gated writes.** Inventory and preflight never mutate.
  Release and migration plans require a reviewed plan hash; data backups and
  destructive cleanup require explicit acknowledgement.
- **Reuse the existing primitives.** `stacks.py` for compose parsing and
  pushes, `updates.py` for pin mutation and rollback, `runner.py` for runs,
  `store.py` for history, and the existing health gate.

## The manifest

A per-app folder keeps the contract, notes and future migration evidence
together beside the existing stacks:

```text
workspace/
├── apps/
│   └── unjobs/
│       ├── app.json
│       └── README.md      # optional operator notes
├── stacks/<host>/<service>/
├── scripts/hosts.json
└── .hlc/                  # local state; never committed
```

Minimal manifest for the current unjobs setup:

```json
{
  "schema": 1,
  "name": "unjobs",
  "title": "UN Vacancy Monitor",
  "source": {
    "provider": "github",
    "repo": "awychoi/vacancy_classifier",
    "tag_pattern": "v*",
    "default_branch": "main",
    "version_file": "unjobs/__init__.py"
  },
  "images": [
    {
      "repo": "ghcr.io/awychoi/vacancy_classifier",
      "role": "app",
      "platforms": ["linux/arm64"],
      "visibility": "private",
      "build": {
        "provider": "github-actions",
        "workflow": ".github/workflows/image.yml"
      }
    }
  ],
  "versioning": {
    "scheme": "semver",
    "pin": "tag",
    "tag_prefix": "v",
    "immutable_tags": true
  },
  "deploy": {
    "health": "/api/health",
    "ports": [8765],
    "data": ["/srv/unjobs/data"],
    "backup": {
      "kind": "sqlite",
      "path": "/srv/unjobs/data/unjobs.db",
      "max_age_hours": 24
    },
    "required_env": ["UNJOBS_API_TOKEN"],
    "forbidden_env_prefixes": [
      "UNJOBS_GIT_",
      "UNJOBS_AGENT_",
      "UNJOBS_ABOUT_ME_"
    ]
  },
  "migration": {
    "legacy_build_context": "/srv/unjobs/src",
    "legacy_image": "unjobs:local",
    "cleanup_paths": [
      "/srv/unjobs/src",
      "/srv/unjobs/venv",
      "/srv/unjobs/about_me",
      "/srv/unjobs/keys"
    ],
    "revoke": [
      "GitHub deploy key used by /srv/unjobs/src",
      "DeepSeek API key in the old Pi .env"
    ]
  }
}
```

Field notes:

| Field | Meaning |
|---|---|
| `source` | Where releases are defined. `tag_pattern` is authoritative; GitHub releases are optional. `version_file` is used only when a local checkout is available. |
| `images` | One entry per artifact. Binding to stacks is by `images[].repo`; compose files need no console-specific metadata. Multi-image apps list every image and may share a release group. |
| `build` | Descriptive, not executable. `github-actions` links a workflow; `external` points at a script or runbook. The console never runs arbitrary build commands. |
| `versioning` | `scheme` is `semver`, `calver`, `sha` or `custom`; `pin` is `tag` or `digest`; `immutable_tags` states the policy the preflight enforces. |
| `deploy` | The contract a recreate must preserve: health path, ports, data paths, backup source and age, required environment *names*, and forbidden legacy prefixes. Values are never stored or displayed. |
| `migration` | Exact legacy paths and credentials. Cleanup paths are explicit, absolute and validated; no globs or shell. Revocation is a human checklist because the console cannot revoke provider credentials. |

The existing per-image `release_sources` config remains the fallback for images
without an app manifest. When an app exists, its `source` wins.

## What the console does with it

### 1. Inventory and provenance (read-only)

A new **Apps** view lists each app with its current version, latest source
release, image digest, bound stacks, hosts and attention state. The detail page
shows the chain:

```text
source tag ──▶ CI run ──▶ image digest ──▶ compose pin ──▶ deployed host
```

Preflight checks, all read-only:

| Check | What it proves |
|---|---|
| Source repo reachable with the available token | private tags can be read |
| Tag matches `tag_pattern` and exists | the release is real |
| Image tag exists in the registry | the pull can succeed |
| Image supports the target host's architecture | no `no matching manifest` at deploy time |
| OCI labels `source`, `version`, `revision` match | the artifact came from that source release |
| Registry auth exists on every bound host | the pull is authenticated |
| Stack is image-based (`build:` absent) | no source on the Pi |
| Compose pin matches the app image and a source tag | no drift between source and deployment |
| Required env names are present; forbidden prefixes are absent | the deploy contract holds |
| Data path exists and is writable by the container UID | state survives the recreate |
| Backup exists and is fresh enough | rollback is safe |
| No legacy build context, local image or env prefix | migration is complete |

The checks read key **names** and metadata only. They never read or display
secret values. `hlc app check <slug>` exposes the same result outside the UI.

Attention items follow from the checks: unpinned `latest`, tag/digest drift,
source/image version mismatch, private source without a token, legacy stack,
cleanup pending, stale backup, and `pull_policy: always` on a mutable tag. A
digest pin is reported as a state, not a fault.

### 2. Release planning and apply (gated writes)

A new source tag — or a manually chosen version — produces one **app release
plan** across every bound stack, not one update per stack:

1. Resolve the source tag and image digest; confirm the image exists for every
   target architecture.
2. Verify OCI labels against the source tag and revision.
3. Compute the pin mutation in every stack (compose image line or shared env
   pin) and the resulting combined diff.
4. Pull in each stack's safety profile: backup requirement, critical gate,
   DNS warning and post-update note.
5. Present one review hash covering every target and value.

Apply is serial by host, reusing the existing update machinery: snapshot, push,
pull only the affected services, recreate, wait for Docker health and loopback
ports, and verify the reported version where the app exposes one. A failed host
is rolled back from its snapshot; the release stops. The default partial-failure
policy is **stop and leave previously updated hosts on the new version, clearly
flagged**; an optional stricter policy rolls every host back to the previous
release. The release record stores per-host status so either choice is visible.

Rollback is the inverse: re-pin every target to the previous release's values,
redeploy, verify, and record a new release event. Data is never touched.

This is mostly orchestration over `updates.py`: the per-target mutation,
snapshot, push, pull, health and rollback already exist. The new parts are the
app grouping, the combined plan/review hash, the release record and the
partial-failure policy.

### 3. Migration and onboarding (gated writes)

For an existing build-on-the-Pi stack, the console detects:

- `build:` in the compose file, or an image that looks local
  (`unjobs:local`);
- a source checkout path referenced by the build context;
- legacy env prefixes and a remote `.env` that still carries them;
- a running image whose tag is not the app's release tag.

The guided migration is the unjobs runbook made explicit:

1. Create or select the app manifest.
2. Preflight source, image, architecture, registry auth, data path and backup.
3. Show the environment contract diff: keys that will be added, keys that will
   disappear (the old git/agent/about-me keys), and required keys that are
   missing. Values are never shown.
4. Require a data backup acknowledgement; offer a measured SQLite backup action
   where the manifest declares one.
5. Replace the compose file with the image-based version; show the combined
   diff and review hash.
6. Deploy and verify image tag, app version, health and loopback port.
7. Present a post-migration checklist: remove the exact legacy paths, remove
   the old local image, revoke the deploy key and any provider key that was in
   the old `.env`, and confirm the forbidden keys are gone. Path cleanup is an
   explicit, validated list; key revocation is a human acknowledgement.

The same machinery drives onboarding of a new app: pick a local checkout or
enter the source repo, detect the Dockerfile/workflow/image, suggest a manifest,
bind the stacks that already use the image, and run the same preflight.

### 4. Release creation (later, opt-in)

The console deliberately does not create tags today. A later phase can add a
guarded `Create release` action:

- clean working tree, current branch, no existing local or remote tag;
- annotated tag only, never force; push the branch and tag;
- optional GitHub Actions `workflow_dispatch`, with a link to the run;
- a read-only GitHub token by default; write scopes only when this is enabled.

This is optional. The default workflow can stay: tag and push in git, CI builds,
the console detects the image and prepares the release. The console should not
force-push or rewrite history in any phase.

## Non-goals

- Building images, running CI, or hosting a registry inside the console.
- Copying source to a Pi or using a Pi as a build machine.
- Automatic data-volume backups or silent destructive cleanup.
- Branch management, force-push, rebase or tag rewriting.
- A general Git-host or registry UI.
- Replacing the existing per-image update page for images without an app.

## UI, API and storage

Suggested surface:

- **Apps** rail item (Repo street group), list and detail. The name should not
  be confused with the existing macOS app-bundle feature inside a Repos project;
  label that one **Desktop app** if needed.
- Detail tabs: **Overview**, **Releases**, **Stacks**, **Setup**, **Migration**.
- Stack page: an app chip (`unjobs · v1.2.2 · source`) linking to the app, plus
  the normal deploy controls.
- Updates page: group image targets by app when an app manifest matches; keep
  the flat list for everything else.
- CLI: `hlc app list`, `hlc app check <slug>`, `hlc app plan <slug>`,
  `hlc app migrate <slug>`.

Proposed API:

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/apps` | app list with current/latest state |
| GET | `/api/apps/<slug>` | detail: source, images, stacks, preflight, releases |
| POST | `/api/apps` | create or import a manifest |
| PUT | `/api/apps/<slug>` | validated manifest update |
| POST | `/api/apps/<slug>/check` | refresh source, image and preflight checks |
| POST | `/api/apps/<slug>/release/plan` | combined release plan + review hash |
| POST | `/api/apps/<slug>/release/apply` | gated apply (`review_hash`, acknowledgements) |
| POST | `/api/apps/<slug>/release/rollback` | restore a previous release |
| POST | `/api/apps/<slug>/migrate/plan` | detect and plan a legacy conversion |
| POST | `/api/apps/<slug>/migrate/apply` | gated migration |
| GET | `/api/apps/<slug>/releases` | release history with per-host results |

Storage split:

- `apps/<slug>/app.json` and optional notes: desired state, committed.
- `.hlc/console.db`: `app_releases` and `app_release_targets` tables for
  history, per-host status, run ids and previous pins. Existing `runs` and
  `events` provide logs and the audit trail.
- `.hlc/snapshots/`: unchanged per-stack snapshots used by rollback.
- Workspace backup: include `apps/` alongside `stacks/`, `hosts.json` and
  non-secret settings.

## Adding the next app

The repeatable onboarding path should be short:

1. **Make the artifact releasable.** The repo tags a version, CI builds an
   image for the target architectures, and the image carries the standard OCI
   source/version/revision labels. A local build is acceptable if it adds the
   same labels and records the artifact somewhere the Pi can pull.
2. **Add `apps/<slug>/app.json`.** Declare the source repo and tag pattern, the
   image repo, the versioning policy, and the deploy contract (health, data,
   backup, required/forbidden env names).
3. **Point a stack at the image.** If the stack already uses the image, the
   console binds it automatically. If it is still build-based, run the migration
   plan instead of hand-editing the compose file.
4. **Run `hlc app check <slug>`.** Resolve the preflight: source token, tag,
   architecture, registry auth, env contract, data path, backup, legacy
   leftovers.
5. **Release.** Pick the source tag, review the combined plan, apply it serially,
   and keep the release record as the rollback target.

That is the whole feature from the operator's side. The console owns the
contract and the evidence; CI or the local build script owns the artifact.

## Phases

**Phase 0 — manifest and read-only inventory.** Parse and validate
`apps/<slug>/app.json`; match images to stacks; show the app list/detail,
version chain and preflight; `hlc app check`; attention items; tests. Done when
unjobs is legible without any deploy write.

**Phase 1 — app release plan/apply/rollback.** Group targets across stacks,
resolve digest and labels, produce a combined plan hash, apply serially through
the existing update machinery, record a release, and roll back. Done when a
`v1.2.3` release can move pi3's unjobs with one reviewed plan and a recorded
per-host result.

**Phase 2 — migration and onboarding.** Legacy detection, environment contract
diff, backup gate, compose replacement, verification, cleanup checklist and key
revocation acknowledgement; plus the add-app wizard. Done when the current
unjobs legacy stack can be converted entirely from the console.

**Phase 3 — optional release creation.** Guarded tag/push, optional CI
dispatch, and a link from tag to build to image. Done when the console can
start a release without ever force-pushing.

**Phase 4 — optional local build lane.** An external script builds and pushes
(or loads) an image and writes a release record; the console verifies its
labels and treats it like any other artifact. The console still does not run
Docker.

## Unjobs as the first user

The first manifest is the JSON above. Expected initial findings:

- source repo private, so a read-only GitHub token is required for release
  checks;
- image is private, and pi3 already has `ghcr.io` auth;
- the image is arm64 and the host is aarch64;
- the live stack is still legacy (`build.context: /srv/unjobs/src`,
  `unjobs:local`), so migration is pending;
- the old remote `.env` still carries legacy git/agent keys, so the
  environment diff and revocation checklist are not empty;
- the current `pull_policy: always` on a pinned tag is a warning, not an error.

After migration, a `v1.2.3` tag in the app repo becomes an app release: CI
builds it, the console detects the tag, resolves the digest, plans the pin for
`stacks/pi3/unjobs`, deploys, and records the result. No force-push, no source
on the Pi, no manual two-repo version bump.

## Open decisions

- **Naming.** "Apps" is the clearest word but collides with macOS app bundles in
  Repos. Alternatives: "Releases", "Products", "Ship".
- **Binding.** Matching by image repo avoids new stack metadata and handles
  multiple hosts naturally. An explicit stack link is more robust for apps with
  several images or local builds.
- **Manifest format.** JSON keeps the console stdlib-only and matches
  `hosts.json`; YAML is friendlier but adds a dependency to a deliberately
  dependency-free app.
- **Partial-failure policy.** Stop-and-report versus roll-back-all. The release
  record should make either explicit.
- **Release creation.** Whether the console should ever create tags or dispatch
  CI, and with which token scopes.
- **Backup automation.** A SQLite `.backup` is safe online and could be a
  measured action like the existing PostgreSQL dump. It is still a write and
  needs its own gate.

## Recommendation

Build Phase 0 first. It is read-only, small, and immediately useful for unjobs
and every future app; it also produces the schema that later phases depend on.
Then add Phase 1, reusing the update machinery. Treat the migration wizard as a
workflow layer on top of those primitives, not as the first thing to build.
Keep CI as the build lane; a local build lane can remain an external script
that records provenance.
