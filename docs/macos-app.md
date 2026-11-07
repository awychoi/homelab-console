# Homelab Console.app

A native macOS shell around the console. **Swift** owns the window, the
lifecycle and the menus; the existing Python backend runs as a child process
and still does all the work.

This replaced the earlier browser-launcher bundle once a browser tab stopped
being enough: the shell can keep the backend alive, reload it, quit it, and
reopen the window from the Dock.

For a fresh install — including **Choose Workspace…** and the supported macOS
versions — see
[First-time setup](getting-started.md).

## Architecture

```text
Homelab Console.app
├── Contents/MacOS/Homelab Console        Swift shell (AppKit + WKWebView)
└── Contents/Resources/backend/           PyInstaller Python backend
    ├── HomelabConsole
    └── _internal/                        Python runtime + app/web
```

On launch the shell:

1. asks the kernel for a free loopback port;
2. starts `backend/HomelabConsole serve --host 127.0.0.1 --port N --no-browser`
   with `HLC_PARENT_PID` set;
3. reads the Gitea token from the Keychain, if present, into the child's
   environment (`HLC_GITEA_TOKEN`, `HLC_APP=1`);
4. polls `http://127.0.0.1:N/api/health` until it answers (30 s budget);
5. loads `http://127.0.0.1:N/` in a `WKWebView`;
6. on quit, terminates the child. If the app dies without warning (crash,
   Force Quit), the backend notices it was reparented and exits within a
   second, so no server leaks a random port.

External links open in the system browser; the console stays in the window.
`Cmd-R` reloads, `Cmd-Shift-O` opens the current page in the browser.

## Security boundary

The webview only ever hosts the console origin. Any other navigation, including
a meta refresh, is cancelled and opened externally with an allow-listed scheme
(`http`, `https`, `mailto`, `homelab-console`); the native message handlers are
therefore not reachable from a remote page. `hlcPick` and `hlcTerminal` accept
messages only from the main frame of the console origin. `hlcTerminal` further
requires an `ssh ` command under 8 KB with no newline, carriage return or tab,
writes it to a mode-0700 self-deleting `.command` file, and opens Terminal.
The sidecar log is mode 0600. A non-ad-hoc build is signed with the hardened
runtime when an identity is supplied.

## Build

Requirements: Xcode or the command line tools (`swiftc`), Python 3.10+, plus
PyInstaller as a build-time dependency. The console itself stays stdlib-only.

```sh
./.venv/bin/python -m pip install "pyinstaller>=6.16"
desktop/build_app.sh
open "dist/Homelab Console.app"
```

Output: `dist/Homelab Console.app`. `dist/` and `build/` are gitignored. The
script keeps PyInstaller's cache under `build/` and the Swift module cache
there too, so it never needs to write to Application Support. The bundle
includes `Contents/Resources/LICENSE`, `THIRD_PARTY_NOTICES.md`,
`BUILD_INFO.txt` and `licenses/`; keep those files with any copy you
redistribute.

For the user-facing path, see [Build and sign it yourself](building.md).

## Signing

Default is ad-hoc (`codesign --sign -`). That runs locally but has no stable
identity: macOS ties Local Network and Keychain permissions to the signature,
so every rebuild can look like a new app.

The bundle identifier is a build input: set `HLC_BUNDLE_ID` when redistributing
(default `dev.homelab-console.app`). The Keychain service is derived from the
bundle id, so your build never reads the author's item. To keep a token created
by an older build, pass that build's id:

```sh
HLC_BUNDLE_ID=com.example.homelab-console desktop/build_app.sh
```

**Self-signed is the sweet spot for a personal build.** It costs nothing and
gives the bundle a stable identity, so permissions survive rebuilds.

1. Keychain Access → **Certificate Assistant** → **Create a Certificate…**
2. Name `Homelab Console Dev`, Identity Type **Self Signed Root**,
   Certificate Type **Code Signing**, then Create.
3. Find the identity:

   ```sh
   security find-identity -v -p codesigning
   ```

4. Build with it:

   ```sh
   HOMELAB_SIGN_IDENTITY="Homelab Console Dev" desktop/build_app.sh
   ```

A stable identity also keeps the Keychain item readable across rebuilds; with
ad-hoc signing a rebuild can make macOS treat the app as a different client and
prompt for access again.

Self-signed is **not notarized**. On this Mac it runs normally. On another Mac,
right-click → **Open** (or add the certificate to that Mac's trust settings).
Distributing to strangers needs a Developer ID and `notarytool`; that is a
separate decision.

## Workspace

A Finder-launched app has no shell environment and no checkout, so the backend
resolves its workspace in this order:

1. `--repo <path>` (passed to the backend)
2. `HLC_REPO`
3. `HLC_WORKSPACE`
4. `~/Library/Application Support/Homelab Console/workspace.txt` (one line)
5. a checkout containing `stacks/` and `scripts/` (dev runs)
6. `~/Library/Application Support/Homelab Console/workspace` (created empty)

**A freshly installed app shows no Pis.** That is expected: it starts in its
own empty workspace rather than guessing where your data is. Use
**Homelab Console → Choose Workspace…** (also offered once on first launch) and
pick an empty folder or an existing workspace with `stacks/` and `scripts/`.
The shell writes the pointer for you at
`~/Library/Application Support/Homelab Console/workspace.txt` and restarts the
backend. The menu item also works after the first run.

## Logs

| File | Contents |
|---|---|
| `~/Library/Logs/Homelab Console/sidecar.log` | backend stdout/stderr, including why it failed to start |
| `<workspace>/.hlc/logs/console.log` | the console's own log (frozen runs log here) |

Both are plain text. If the window shows "backend error", start with the first
one.

## Settings and window lifecycle

The app menu has two entries that live entirely in the shell:

- **Settings… (⌘,)** — the Gitea token. The console's own Gitea banner links
  here through `homelab-console://settings`, so a missing token is one click
  from the fix. Saving closes the window and brings the console forward while
  it restarts. It is stored in the macOS Keychain
  (service = the app bundle id, default `dev.homelab-console.app`, account
  `gitea-token`), never in
  `.hlc/config.json`. Saving restarts the backend so the new value takes
  effect immediately: the shell reads the item at launch and passes it as
  `HLC_GITEA_TOKEN` (with `HLC_APP=1`).
- **Choose Workspace…** — an `NSOpenPanel` for the folder that holds `stacks/`,
  `scripts/hosts.json` and `.hlc/`. Picking a new folder writes the pointer and
  restarts the backend against it. An empty folder is seeded with the minimum
  layout and a `.gitignore`.
- The web UI also asks the shell for folder/file dialogs (`hlcPick` message
  handler) so **Choose folders…** can select several project folders and backup
  save/restore can use real paths instead of typed ones.

The app is **foreground-only**. Closing the console window quits the app,
terminates the backend child process, and unregisters any login item left by an
older build. There is no menu-bar mode, launch agent, or daemon; opening the app
is the only way it starts.

While the app is running, the console's own Settings page hides the token
field and says the token is app-managed, so there is only one place to set it.
With no token stored, ref checks fall back to the git credential helper; a
credential-helper item created from Terminal is not always readable by a
GUI-launched app, which is why the Keychain field exists.

The token is read on a background queue after the window is up. A Keychain
permission prompt (common right after an ad-hoc rebuild, because the signature
changed) therefore appears over a visible window instead of a blank app; choose
**Always Allow**. A self-signed identity avoids the repeat prompts.

Headless helpers (also used for verification):

```sh
BIN="dist/Homelab Console.app/Contents/MacOS/Homelab Console"
"$BIN" --login-item-status
"$BIN" --login-item-register
"$BIN" --login-item-unregister
"$BIN" --gitea-token-status
"$BIN" --clear-gitea-token
```

`--set-gitea-token-file <path>` and `--set-gitea-token-stdin` exist for
automation; both are careful to keep the value out of `ps` argv when read from
stdin. `launchctl setenv HLC_SET_GITEA_TOKEN_FILE …` / `HLC_CLEAR_GITEA_TOKEN=1`
do the same when LaunchServices will not forward argv.

## Icon

The bundle icon is generated from the same artwork the browser tab uses:

```sh
node desktop/make_icon.cjs     # app/web/favicon.svg -> desktop/macos/Resources/AppIcon.icns
desktop/build_app.sh
```

`make_icon.cjs` renders the tile at every size macOS asks for, packs the `.iconset`
with `iconutil`, and writes `AppIcon.icns`; `build_app.sh` copies it into
`Contents/Resources` and refuses to build if it is missing. `Info.plist` names it
with `CFBundleIconFile`, which is also what the standard About panel shows.

## Not included

- Notarization and Developer ID distribution.
- Auto-update and menu-bar mode. (The app icon is wired in; see **Icon** above.)
- Linux or Windows. This shell is deliberately Mac-only.
