# App manifest

A project can link a built macOS application, and the console can track the
upstream repositories that application vendors. For that second part to work,
the console has to know **which versions are actually inside the bundle**. It
cannot read them out of the app's own version number: SevenZip is `0.1.0` and
ships 7-Zip `26.06`, so "is this app up to date" would be a guess.

The bundle answers for itself. An app that wants update flags ships one small
file:

```
YourApp.app/Contents/Resources/homelab-app.json
```

Apps without the file still get the launch button. Their dependencies show
*version unknown* — never a guessed "update available".

## Schema

```json
{
  "schema": 1,
  "app": { "name": "SevenZip", "version": "0.1.0" },
  "dependencies": [
    { "repo": "ip7z/7zip", "version": "26.06", "path": "7zz" }
  ]
}
```

| Field | Required | Meaning |
|---|---|---|
| `schema` | no | Reserved for future revisions. Currently `1`. |
| `app.name` | no | Display name. `CFBundleDisplayName` / `CFBundleName` win when present. |
| `app.version` | no | Fallback only; `CFBundleShortVersionString` is preferred. |
| `dependencies[].repo` | yes | Upstream repository as `owner/name`. |
| `dependencies[].version` | yes | Version this bundle actually carries. |
| `dependencies[].path` | no | Human hint for where it lives in the bundle (`7zz`, `Vendor/lib`). |

Rules:

- `repo` must match `owner/name`. It is used to query the GitHub releases API,
  so anything else is ignored rather than sent.
- `version` is compared numerically, so `26.06`, `v26.06` and `26.6` are equal.
  Keep it to the digits of the upstream tag.
- Duplicate `repo` entries keep the first one.
- A malformed or oversized file is ignored as a whole; the app is treated as
  having no manifest.
- At most twelve dependencies are read. If that is not enough, the list is
  probably describing something other than a bundled engine.

## Write it after the update succeeds

This is the rule that makes the flag trustworthy:

1. Build or download the new engine.
2. Verify it (the SevenZip repo runs its CLI contract and checks the binary's
   own version banner against the lock file).
3. **Only then** rewrite `homelab-app.json` with the new version.

If the update fails halfway, the old version stays in the file and the console
keeps showing the update — which is correct, because the app really is still
old. Writing the manifest first would clear the flag for an update that never
took effect.

## How the console reads it

- The file is read from the bundle the project points at, cached by the
  bundle's modification times, so an in-place self-update is picked up on the
  next scan without restarting anything.
- Latest upstream version comes from the GitHub releases API, falling back to
  the newest tag for projects that only publish tags. Results are cached for
  six hours (`release_ttl_s`); a failed lookup is retried after five minutes so
  a rate limit does not freeze the column for the day.
- States are deliberately simple:

| State | Meaning |
|---|---|
| `current` | Installed version is equal to or newer than the newest release. |
| `behind` | Installed version is older; the row links to the release. |
| `unknown` | No manifest, no version for that repository, or no successful lookup yet. |
| `error` | The lookup failed (offline, rate limit, repository missing). |

- With a GitHub token in **Settings → Repos**, checks are authenticated and the
  anonymous rate limit does not apply.
- The console never downloads, builds, launches-a-build or updates the app. It
  compares versions and opens the bundle; everything else belongs to the app.

## Minimal example

An app that vendors 7-Zip and writes the file after a verified rebuild:

```bash
cat > "$APP/Contents/Resources/homelab-app.json" <<JSON
{
  "schema": 1,
  "app": { "name": "SevenZip", "version": "$APP_VERSION" },
  "dependencies": [
    { "repo": "ip7z/7zip", "version": "$SEVENZIP_VERSION", "path": "7zz" }
  ]
}
JSON
```

`Contents/Resources` is covered by the code signature, so write the manifest
before the final signing step or re-sign the bundle afterwards. A modified
bundle with a stale signature can refuse to launch.
