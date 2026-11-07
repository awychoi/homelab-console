# Security policy

Homelab Console is a local, single-operator control plane: a loopback web app
that reaches your own Pis over SSH. There is no cloud service, telemetry,
multi-user layer, or remote-access story.

## Security model

- **Local API:** binds `127.0.0.1`, rejects a non-loopback `Host` (blocking DNS
  rebinding), and requires a random per-process `X-HLC-Token`. SSE uses a
  short-lived run-scoped token, not the session token in a URL. Mutating
  requests reject a foreign `Origin`.
- **Gated writes:** deploys require a reviewed plan hash; updates require a
  review hash bound to the target/value; Gitea force/prune sync requires the
  hash from its dry run. A changed plan or target is rejected.
- **Browser:** strict CSP, no inline scripts or external assets, escaped
  host-derived fields, `X-Frame-Options: DENY`. The native webview loads only
  the console origin; its Terminal bridge accepts main-frame commands from that
  origin only, and external URLs open in the browser with allow-listed schemes.
- **Commands:** fixed API; SSH/git are argv vectors; remote Docker paths are
  quoted; no arbitrary command endpoint. Repos rejects executable
  repo-local Git config and disables hooks/fsmonitor; the Gitea header is
  scoped to its base; redirects strip credentials; non-loopback Gitea requires
  HTTPS (`HLC_GITEA_ALLOW_HTTP=1` is an explicit override).
- **Trust:** onboarding requires fingerprint verification and pins
  `known_hosts`; SSH passwords are used once and never stored. `.env`-family
  files and tokens are filtered from commits; config/snapshots/backups are
  mode 600; child processes get a sanitized environment.

## Not protected

Another process/account on the same Mac; a compromised or misconfigured host;
repositories you deliberately trust; Docker images or compose files you choose;
binding beyond loopback (use `HLC_ALLOWED_HOSTS`; there is no TLS); and anyone
with your unlocked Mac account.

## Secrets and backups

The native app keeps the Gitea token in the Keychain; a source run stores it in
`.hlc/config.json` (600, never returned by the API). Precedence is
`HLC_GITEA_TOKEN` → `GITEA_TOKEN` → stored setting, and
`HLC_GITHUB_TOKEN` → `GITHUB_TOKEN` → `GH_TOKEN` → stored setting. SSH keys live
in `~/.ssh`; per-stack secrets live in `.env`-family files under `stacks/`.

Backups include every stack file, including `.env` secrets, but exclude tokens
and `.hlc/` state. They are written 600 and restore writes a safety backup
first. Treat an archive like the `.env` files: keep it off shared storage.

## Scope

**In scope:** local API, token/Host/Origin handling, SSH/git construction, the
plan/review gates, secret redaction, backup paths, UI escaping/CSP, and the
macOS shell's navigation, process and Keychain handling. **Out of scope:**
misconfigured hosts, images you deploy, Gitea/GitHub themselves, repositories
you trust, and the unlocked Mac account.

## Reporting

Report vulnerabilities through **Security → Report a vulnerability** (GitHub
private advisory), not a public issue. Include the version, the affected file
or endpoint, and a minimal reproduction.
