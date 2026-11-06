# First-time setup

This is the shortest path from a fresh checkout to one Pi running one stack.
It covers what you do **outside the app** and the first few steps **inside it**.
Day-to-day settings, updates, Repo street and backups are in
[Using the console](operations.md).

## The flow at a glance

The split is deliberate: the console is the Mac-side control plane. It does not
flash, discover or provision a Pi.

| Step | Where | What happens |
|---|---|---|
| 1. Prepare the Mac | Outside | Install Python/command-line tools, clone the app, create a virtualenv if running from source. |
| 2. Prepare each Pi | Outside | Raspberry Pi OS, SSH, a stable address, Docker Engine + Compose v2, and a writable remote stack root. |
| 3. Choose a workspace | Outside or **Choose Workspace…** | Create or point at the folder that holds `scripts/hosts.json` and `stacks/`. |
| 4. Add the first host | Inside | Verify the fingerprint, install the SSH key, and save the host entry. |
| 5. Create a stack | Inside | Pick a template or paste a compose file; write it into the workspace. |
| 6. Deploy and verify | Inside | Review the diff, push over SSH, run `docker compose up -d`, watch health. |

The sections below expand those steps in order.

## Before you start

### Supported setups

| Piece | Supported | Not supported |
|---|---|---|
| Controller OS | macOS 13+ (native app); macOS with Python 3.10+ (source) | Windows, Linux, mobile |
| Controller CPU | Apple Silicon for the current `.app`; build on Intel for Intel | A universal `.app` |
| Controller tools | `git`, OpenSSH (`ssh`, `ssh-keygen`, `ssh-keyscan`), `curl` | A shell-free installer |
| Pi OS | Raspberry Pi OS Bookworm 64-bit, or Debian-like Linux with systemd and `/proc` | Arbitrary distros, rootless Docker, non-Linux hosts |
| Pi runtime | Docker Engine and Compose v2 (`docker compose`) installed already | App-driven Docker/OS installation |
| Pi access | SSH key auth; the SSH user can write the remote stack root | Password-only day-to-day use; automatic host discovery |
| Network | Same LAN, routed LAN, or Tailscale | NAT traversal or a public control plane |
| Distribution | Repository checkout, or a locally built `.app` (ad-hoc or self-signed) | Notarized download, App Store, auto-update |
| Operators | One person, one Mac | Teams, RBAC, a shared server |

The current native build is a thin `arm64` app. Build it on an Intel Mac if
that is the destination. The console is written for Raspberry Pi OS/Debian
conventions (`systemctl`, `/proc`, `python3`, `docker compose`, and `vcgencmd`
where available); it is not distro-universal. It is also not multi-user: the
server binds loopback and is designed for one operator.

### Access the app needs

| Access | Why |
|---|---|
| **Local Network** | Reach the Pis and Gitea over SSH/HTTPS. macOS may ask once. |
| **Keychain** | The native app stores the optional Gitea token there. Source runs keep it in `.hlc/config.json` (mode 600). |
| **SSH keys** | Read keys and `known_hosts`; write `~/.ssh/config` with a backup; optionally create `~/.ssh/homelab_console_ed25519`. |
| **Workspace files** | Write `stacks/`, `scripts/hosts.json`, and local state under `.hlc/`. |
| **Remote host** | An SSH user with Docker/Compose access and write access to the remote stack root. Passwordless `sudo` is optional, only for repairing a chowned stack directory. |
| **Gatekeeper** | The native app is ad-hoc signed and not notarized; right-click → **Open** the first time. |

It does **not** ask for administrator/root on the Mac, Full Disk Access,
Accessibility, Screen Recording, or a cloud login. If the workspace lives under
Desktop, Documents or Downloads, macOS may ask for folder access; a plain folder
such as `~/homelab` avoids that.

## Outside the app

### 1. Check the Mac

For a source checkout:

```sh
python3 --version     # must be 3.10 or newer
git --version
ssh -V
```

macOS may prompt to install the Command Line Tools the first time you run `git`;
`xcode-select --install` does it explicitly. If `python3` is older than 3.10,
install a current Python (python.org or Homebrew), then create the virtualenv
with that interpreter:

```sh
cd /path/to/homelab
python3.12 -m venv .venv
./hlc
```

`./hlc` prefers `.venv/bin/python`, then `python3`. The console uses only the
standard library, so normal use has no `pip install` step.

For the native app, Python is bundled; you only need the macOS tools above.
Building the app additionally needs Xcode or the Command Line Tools and
PyInstaller — see [macOS app bundle](macos-app.md).

### 2. Get the app

#### Option A — run from source

```sh
git clone https://github.com/awychoi/homelab-console.git
cd homelab-console
./hlc doctor
./hlc
```

`./hlc doctor` is read-only. It reports the repo, tools, `hosts.json`, SSH keys
and reachability, and whether port 8765 is free. A missing SSH key is expected
before onboarding; the wizard can create one. Host reachability checks fail
until you add a working host. With the example workspace it probes the
documentation address, which is expected to fail until you replace it.

#### Option B — use the native app

If someone handed you `Homelab Console.app`, drag it to `/Applications` and
open it. The current build is ad-hoc signed and **not notarized**, so macOS will
warn on first launch:

- Right-click the app → **Open** → **Open**, or
- System Settings → **Privacy & Security** → **Open Anyway**.

Do not disable Gatekeeper globally. Only open a copy you built yourself or got
from someone you trust. There is no auto-update; a new build is a new file.

The app starts its backend on a random loopback port and shows it in a native
window. It is the same console, not a different feature set. To build your own
copy instead of using someone else's, see
[Build and sign it yourself](building.md); the script supports ad-hoc and
self-signed identities and builds for the current architecture. The bundle
carries its own [third-party notices](../THIRD_PARTY_NOTICES.md) and licenses.

### 3. Pick a workspace

Hosts, stacks and local state live in a **workspace** folder, separate from the
app checkout. Create one and seed the example:

```sh
./hlc init --example "$HOME/homelab/workspace"
```

That writes `stacks/example/whoami/docker-compose.yml`, a `scripts/hosts.json`
with a documentation-only host (`192.0.2.10`), a `.gitignore` covering `.hlc/`
and `.env`-family secrets, and runs `git init`. Start against it with:

```sh
HLC_REPO="$HOME/homelab/workspace" ./hlc
```

For the native app, use **Homelab Console → Choose Workspace…**; it also asks
once on first launch. A Finder-launched app with no choice keeps an empty
workspace at `~/Library/Application Support/Homelab Console/workspace/` and
records your choice in `workspace.txt` beside it.

Workspace precedence for the backend is `--repo`, `HLC_REPO`,
`HLC_WORKSPACE`, the `workspace.txt` pointer, a checkout containing `stacks/`
and `scripts/`, then the empty application-support workspace.

This checkout has no live fleet. `examples/workspace/` is documentation
data only; replace the `example` host with your own. If you reuse an alias,
note that Stacks maps stack directories by alias.

If you keep the fleet in a separate private git repo, check it out at
`workspace/`; the app repo ignores that folder, so `.gitmodules` and the
workspace URL never enter the app tree.

### 4. Prepare each Pi

The app assumes the Pi is already a working Docker host. On the Pi:

1. **Flash Raspberry Pi OS Bookworm 64-bit** and boot it. In Raspberry Pi Imager,
   set the hostname and user, and enable SSH under the gear/customisation menu.
   Alternatively, run `sudo raspi-config` → **Interface Options** → **SSH**.
2. **Give it a stable address.** Reserve a DHCP lease, set a static address, or
   use its Tailscale IP/MagicDNS name. The app stores the address you type; a
   changed lease is a dead host in the console until you edit it.
3. **Install Docker Engine and the Compose plugin.** Follow Docker's
   [Debian instructions](https://docs.docker.com/engine/install/debian/), or use
   the convenience script and reconnect:

   ```sh
   curl -fsSL https://get.docker.com | sudo sh
   sudo usermod -aG docker "$USER"
   sudo mkdir -p /opt/stacks
   sudo chown -R "$USER" /opt/stacks
   ```

   Reconnect the SSH session (or reboot) so the `docker` group applies.
4. **Verify the contract** from the Mac:

   ```sh
   ssh PI_USER@PI_ADDRESS 'python3 -V && docker compose version && test -w /opt/stacks && echo ready'
   ```

   You want `ready` and a Compose v2 version. A permission error on
   `docker compose` means the group change has not taken effect yet.

`python3` is present on Raspberry Pi OS and is the only collector dependency on
the host. `sudo` is not needed for a normal first deploy if the remote stack root
is owned by the SSH user. It is only used to repair a stack directory after a
container has chowned a bind mount. That repair runs `sudo -n mkdir -p` and then
`sudo -n chown -R`, so passwordless sudo would need to cover both commands. Most
people can leave sudo alone: the app stops and prints the exact repair command.

SSH password authentication is only needed for the wizard's one-time key
install. If password auth is disabled, use the wizard's manual command instead.

Optional integrations:

- **GitHub push:** make sure `git push` already works from Terminal with your
  credential helper. The console does not store a GitHub token for pushes.
- **Gitea backup:** have the base URL, owner and a token with
  **repository: Read and Write** ready.
- **VS Code:** install the `code` shell command (VS Code → Command Palette →
  “Shell Command: Install 'code' command in PATH”).

### 5. Start the console

```sh
./hlc                    # source: http://127.0.0.1:8765, opens a browser
./hlc serve --port 8766  # if 8765 is busy
```

For the native app, open it from `/Applications` or
`open "dist/Homelab Console.app"`. The app window is the console; there is no
browser tab to manage.

First launch creates `.hlc/` in the workspace. That is local state only —
SQLite telemetry, run logs, release cache and compose snapshots — and is
gitignored. The repo remains the source of truth.

## Inside the app

### 1. Wait for the first paint

The console shows a short warm-up screen while it probes the fleet, scans local
repositories and (if configured) checks Gitea. The gate releases when those
finish or after 12 seconds, so an unreachable host cannot hold the UI.

On the native app, macOS may ask for **Local Network** access; allow it. If you
later use the Gitea token window, macOS may ask for Keychain access; choose
**Always Allow** for your own build.

If the workspace is empty, **Fleet** says “No hosts configured yet”. That is the
expected first-run state, not a backend failure. With the example workspace,
Fleet shows a single `example` host as unreachable. Remove it in **Hosts** and
add your own; removing a host removes the inventory entry only and does not
touch the Pi or the stack files.

### 2. Add the first host

Open **Hosts → Add host**. The wizard has four gates:

1. **Address.** Choose a lowercase alias (for example `pi5`), the Pi's address
   or hostname, the SSH user, and the port. The SSH user field defaults to `pi`;
   Raspberry Pi Imager may have created a different user, so use the one you
   actually log in with. The alias becomes the stack directory name
   (`stacks/pi5/...`). Role and notes are optional. Leave “Also add/update
   `~/.ssh/config`” checked unless you manage that file yourself.
2. **Fingerprint.** The app runs `ssh-keyscan` and shows the host-key
   fingerprints. Verify the ed25519 fingerprint against the Pi before trusting
   it. On the Pi:

   ```sh
   ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub
   ```

   Compare the `SHA256:...` value, then tick the verification box. The console
   pins the scanned keys in `~/.ssh/known_hosts` (with a backup) before the
   first login. If the address is already there, the wizard says so; it still
   expects the fingerprint to match the machine you intend to reach.
3. **Key and trust.** Pick an existing ed25519 key or click **Create dedicated
   key** (it becomes `~/.ssh/homelab_console_ed25519`). Then click **Add host &
   test**. If key authentication does not work yet, enter the Pi password once
   under “Pi password” and click **Install key with password**. The password is
   used once for `authorized_keys`, never stored or logged. If password auth is
   disabled, open the **Manual fallback** disclosure and run the printed command
   on the Pi.
4. **Test.** The wizard runs a side-effect-free SSH probe. When it reports
   **Connected**, click **Finish**. If you choose **Finish later**, the host entry
   is saved but stays unreachable until trust is installed.

The host is written to `scripts/hosts.json` in the workspace, preserving unknown
fields and other tooling's metadata. `~/.ssh/config` is updated in place with a
backup, so `ssh pi5` keeps working in Terminal.

The **Hosts** page also lists local SSH identities, lets you copy a public key,
and has a **Test SSH** button on every host card. Use it after any network or
key change; `./hlc doctor` does the same checks from the terminal.

### 3. Create and deploy the first stack

1. Open **Stacks → New stack**.
2. Pick the host, the **Whoami (diagnostic)** template, and a name such as
   `whoami`.
3. Click **Create stack**. This writes `stacks/<host>/whoami/docker-compose.yml`
   in the workspace; it does not touch the Pi yet.
4. Open the stack and click **Deploy**, or use the row's **Deploy** button.
5. Review the plan. On a new stack it is an add-only push; on an existing stack
   the dialog shows every changed file and requires the overwrite checkbox for a
   destructive plan. The plan hash is what gets applied, so a file changed
   between review and apply is rejected with a “diff changed” message.
6. Click **Push & up** and watch the run log. The sequence is: validate compose
   in a staging directory on the Pi, push the tar, `docker compose up -d`, then
   health checks.

Need a config file (a `Caddyfile`, `prometheus.yml`, `mosquitto.conf`, …)?
Open the stack's **Files** tab and use **New file**; enter a relative path such
as `config/app.yml` and paste the contents. Parent folders are created
automatically, and `.env`, `.env.*` and `*.env` files are saved mode 600. The
file is in the repo only until you deploy.

Stacks must be exactly `stacks/<host>/<stack>/<compose-file>`. If you put a
compose file one directory deeper (for example
`stacks/pi5/group/app/docker-compose.yml`), the app does not adopt it;
**Stacks** and `./hlc doctor` show the ignored path so you can move it.

The template publishes Whoami on the Pi's loopback (`127.0.0.1:9180`). To see it
from the Mac, open a tunnel and curl:

```sh
ssh -N -L 9180:127.0.0.1:9180 PI_USER@PI_ADDRESS
# in another terminal
curl -s http://127.0.0.1:9180
```

Once the run completes, the stack row should show **up** and **synced**, and the
host card should show the Pi online with its temperature and container count.

## What “done” looks like

- **Hosts** shows your Pi with a green status and a working **Test SSH**.
- **Fleet** shows the host online, with telemetry and no unreachable banner.
- **Stacks** shows the stack as **up** and **synced**.
- **Activity** contains the deploy run and its full log.
- Restarting the console preserves the host and stack: they live in
  `scripts/hosts.json` and `stacks/`, while history lives in `.hlc/`.

## Next steps

- [Using the console](operations.md) — settings, update sources, safety
  profiles, updates, Repo street and workspace backups.
- [Troubleshooting](troubleshooting.md) — common failures and current limits.
- [Homelab Console](console.md) — architecture, API and detailed behavior.
