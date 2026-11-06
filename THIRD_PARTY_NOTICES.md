# Third-party notices

Homelab Console itself is MIT-licensed; see [LICENSE](LICENSE).

The macOS app bundle embeds a Python runtime and several native libraries so
that recipients do not need Python installed. This file lists those components,
and the full license texts are in [`licenses/`](licenses/). When the app is
built, the same files are copied into the bundle:

```text
Homelab Console.app/Contents/Resources/
├── LICENSE                      # the app's MIT license
├── THIRD_PARTY_NOTICES.md       # this file
├── BUILD_INFO.txt               # versions captured by build_app.sh
└── licenses/                    # full texts for every component below
```

## Bundled components

| Component | Version | License | Where it is used |
|---|---|---|---|
| [Python](https://www.python.org/) | 3.14.8 | PSF License 2.0 | Embedded runtime (`backend/_internal/Python.framework`) |
| [OpenSSL](https://www.openssl.org/) | 4.0.3 | Apache License 2.0 | `libssl.4.dylib`, `libcrypto.4.dylib` (TLS and hashing) |
| [XZ Utils](https://tukaani.org/xz/) (liblzma) | 5.8.4 | 0BSD | `liblzma.5.dylib` (lzma/xz support) |
| [mpdecimal](https://www.bytereef.org/mpdecimal/) | 4.0.1 | BSD 2-Clause | `libmpdec.4.dylib` (`decimal`) |
| [Zstandard](https://facebook.github.io/zstd/) | 1.5.7 | BSD 3-Clause | `libzstd.1.dylib` (`compression.zstd`) |
| [SQLite](https://www.sqlite.org/) | 3.53.4 | Public domain | `libsqlite3.dylib` (`sqlite3`) |
| [PyInstaller](https://pyinstaller.org/) | 6.22.3 | GPL-2.0-or-later with the bootloader exception | Backend executable bootloader (build-time dependency) |

The versions above are the ones used for the reference build of this checkout.
If you build the app yourself, `BUILD_INFO.txt` inside the bundle records the
Python, OpenSSL, SQLite and PyInstaller versions actually used for your build.
If you rebuild with a different Python or OpenSSL minor version, replace the
corresponding text in `licenses/` with that version's license before you
redistribute the bundle.

## Full texts

| Component | Text |
|---|---|
| Python | [`licenses/Python-3.14.8.txt`](licenses/Python-3.14.8.txt) |
| OpenSSL | [`licenses/OpenSSL-4.0.3.txt`](licenses/OpenSSL-4.0.3.txt) |
| XZ Utils / liblzma | [`licenses/XZ-Utils-5.8.4-0BSD.txt`](licenses/XZ-Utils-5.8.4-0BSD.txt) |
| mpdecimal | [`licenses/mpdecimal-4.0.1.txt`](licenses/mpdecimal-4.0.1.txt) |
| Zstandard | [`licenses/Zstandard-1.5.7.txt`](licenses/Zstandard-1.5.7.txt) |
| SQLite | [`licenses/SQLite-3.53.4-public-domain.txt`](licenses/SQLite-3.53.4-public-domain.txt) |
| PyInstaller | [`licenses/PyInstaller-6.22.3.txt`](licenses/PyInstaller-6.22.3.txt) |

## Notes

- **OpenSSL** is used under the Apache License 2.0. The OpenSSL Project's
  copyright and attribution notice is preserved in the license text.
- **Python** is distributed under the PSF License 2.0, including the historical
  notices reproduced in its license text.
- **PyInstaller** is used only at build time. Its bootloader is embedded in the
  backend executable; the bootloader exception permits that use and does not
  make the console subject to the GPL.
- Apple frameworks (AppKit, WebKit, Foundation and `libSystem`) are provided by
  macOS and are linked, not redistributed, so they are not listed here.
- If you add another bundled library, add its license to `licenses/` and a row
  to this file. `desktop/build_app.sh` copies the directory into the bundle, and
  CI checks that the notices are present.
