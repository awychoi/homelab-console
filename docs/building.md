# Build and sign it yourself

Homelab Console is distributed as **source**, not as a notarized download. Build
the `.app` on the Mac that will run it, then sign it with an identity you
control. A locally built app is not quarantined by macOS, and a stable signature
keeps Local Network and Keychain permissions across rebuilds.

This page is the short path. The [macOS app bundle](macos-app.md)
reference has signing internals, workspace resolution and log locations.

## Requirements

- macOS 13 or newer.
- Xcode or the Command Line Tools (`xcode-select --install`).
- Python 3.10 or newer, plus a virtualenv.
- PyInstaller 6.16 or newer — a build-time dependency only; the console itself
  uses the standard library.

## Build

```sh
git clone https://github.com/awychoi/homelab-console.git
cd homelab-console
python3 --version   # 3.10+; use python3.12 if your system python3 is older
python3 -m venv .venv
./.venv/bin/python -m pip install "pyinstaller>=6.16"

# Ad-hoc signature: works on this Mac, no stable identity.
desktop/build_app.sh

open "dist/Homelab Console.app"
```

The build produces `dist/Homelab Console.app`. `dist/` and `build/` are
gitignored; the app bundle is the thing you keep or move to `/Applications`.

`desktop/build_app.sh` builds the Swift shell for the current architecture
(`arm64` on Apple Silicon, `x86_64` on Intel) and targets macOS 13.0. Build on
the architecture you intend to run; there is no universal binary.

### Give it your own bundle id

The default bundle id is `dev.homelab-console.app`. Set `HLC_BUNDLE_ID` so your
build has its own Keychain item and does not collide with someone else's:

```sh
HLC_BUNDLE_ID=com.example.homelab-console desktop/build_app.sh
```

The Keychain service is derived from the bundle id. If you already stored a
Gitea token under an older build's id, pass that id to keep it.

## Sign with your own identity

The default is an ad-hoc signature (`codesign --sign -`). It runs locally, but
macOS treats every rebuild as a new app, so it can ask for Local Network and
Keychain permission again. A self-signed **Code Signing** certificate is the
sweet spot for a personal build: no cost, stable identity, permissions survive
rebuilds.

1. Open **Keychain Access → Certificate Assistant → Create a Certificate…**
2. Name it `Homelab Console Dev`, choose **Self Signed Root** and
   **Code Signing**, then create it.
3. Confirm the identity exists:

   ```sh
   security find-identity -v -p codesigning
   ```

4. Build with it:

   ```sh
   HOMELAB_SIGN_IDENTITY="Homelab Console Dev" \
   HLC_BUNDLE_ID=com.example.homelab-console \
   desktop/build_app.sh
   ```

The script signs the finished bundle. To verify:

```sh
codesign --verify --deep --strict "dist/Homelab Console.app"
codesign -dv "dist/Homelab Console.app" 2>&1 | head -5
```

A Developer ID and `notarytool` are a separate decision, only needed if you send
a build to someone who will not build it themselves. Ad-hoc and self-signed
builds are not notarized.

## What ships in the bundle

```text
Homelab Console.app/Contents/Resources/
├── LICENSE
├── NOTICE
├── THIRD_PARTY_NOTICES.md
├── BUILD_INFO.txt
└── licenses/
```

The bundle embeds a Python runtime and native libraries: Python, OpenSSL,
liblzma, mpdecimal, Zstandard, SQLite and the PyInstaller bootloader. Their
versions, licenses and full texts are in
[THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md) and
[`licenses/`](../licenses/). The build script copies them into the bundle and
writes `BUILD_INFO.txt` with the Python, OpenSSL, SQLite and PyInstaller
versions actually used. Keep those files with any copy you redistribute.

## Sharing a build

Because the app is ad-hoc or self-signed, the recipient's Mac will warn on first
launch if you transfer the `.app` to them. They can right-click → **Open** once
(or use **System Settings → Privacy & Security → Open Anyway**). It is safer for
each person to build and sign their own copy.

The app has **no auto-update**: a new build is a new file. Closing the window
hides the app and keeps the backend running for an instant reopen; **Quit
(⌘Q)** stops it. There is no login item or menu-bar extra. Logs are at
`~/Library/Logs/Homelab Console/sidecar.log` and
`<workspace>/.hlc/logs/console.log`.

## Troubleshooting

- **`swiftc` not found** — `xcode-select --install`, or install Xcode.
- **PyInstaller is not installed** — run the `pip install` step with the same
  interpreter the script uses (`./.venv/bin/python`), or set
  `HLC_PYTHON=/path/to/python`.
- **`AppIcon.icns` is missing** — run `node desktop/make_icon.cjs` (the file is
  normally tracked already).
- **Signing failed** — confirm the identity name with
  `security find-identity -v -p codesigning`; use `-` for ad-hoc.
- **Wrong architecture** — rebuild on the target architecture. The script uses
  `uname -m`; it does not cross-build.
- **OpenSSL/Python version differs from the reference build** — rebuild and read
  `BUILD_INFO.txt`; the license list is still the same family. If you add a
  bundled library, add its license and a row to `THIRD_PARTY_NOTICES.md`.
