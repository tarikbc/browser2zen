<p align="left">
  <img src="docs/branding/icon-256.png" alt="browser2zen icon" width="120" height="120">
</p>

# browser2zen

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](./LICENSE)
[![Platform](https://img.shields.io/badge/platform-macOS%20arm64%20%7C%20Windows%20x64-lightgrey?logo=apple)](https://github.com/tarikbc/browser2zen/releases/latest)
[![Python](https://img.shields.io/badge/python-3.7%2B-yellow?logo=python&logoColor=white)](https://www.python.org/)
[![PyWebView](https://img.shields.io/badge/GUI-PyWebView-5e86ff)](https://pywebview.flowrl.com/)
[![Latest release](https://img.shields.io/github/v/release/tarikbc/browser2zen?display_name=tag&label=release)](./releases/latest)

Move any browser to [Zen Browser](https://zen-browser.app/) in under a
minute. **Arc, Chrome, Edge, Brave, Firefox, and Safari** are all
supported as sources. Workspaces, pinned tabs, bookmarks, browsing
history, and login state come across.

A polished single-window app handles the whole migration for you. No
terminal, no Python, no install steps to follow.

> Originally based on [`arc2zen`](https://github.com/rafcabezas/arc2zen)
> by Rafael Cabezas (MIT). The Arc-specific extractor and the Zen-side
> writers are inherited from that project; the multi-source architecture
> and the Chrome/Edge/Brave/Firefox/Safari adapters are new here.

---

## Screenshots

<table>
  <tr>
    <td><img src="docs/screenshots/welcome.png" alt="Welcome screen showing all six supported source browsers next to the Zen logo"></td>
    <td><img src="docs/screenshots/source-picker.png" alt="Source picker with Arc, Chrome, Edge, Brave, Firefox, and Safari cards, dimmed when not installed"></td>
  </tr>
  <tr>
    <td colspan="2" align="center"><img src="docs/screenshots/detect.png" alt="Detect screen confirming the chosen source browser and Zen profile are ready" width="80%"></td>
  </tr>
</table>

---

## Install

### macOS (Apple Silicon) — Homebrew (recommended)

```bash
brew tap tarikbc/tap
brew trust tarikbc/tap
brew install --cask browser2zen
```

Launch from Spotlight or `/Applications` afterwards. On first run macOS
will show one "Open Anyway" prompt; see the [Privacy & Security
flow](#macos-apple-silicon--manual-dmg) below for the exact path.

### macOS (Apple Silicon) — manual DMG

1. Download `browser2zen-x.y.z-arm64.dmg` from the
   [latest release](./releases/latest).
2. Double-click the DMG to mount it, then double-click `browser2zen.app`.
3. macOS will show a dialog ("damaged" or "cannot verify"). Click
   **Done**. Do **not** click "Move to Trash".
4. Open  → **System Settings** → **Privacy & Security**.
5. Scroll the right pane until you see *"browser2zen was blocked to
   protect your Mac."* Click **Open Anyway**.
6. Enter your Mac password if prompted.
7. macOS shows one final confirmation. Click **Open Anyway**.

The app launches and walks you through detection, preview, and
migration. Steps 3 to 7 only happen the first time. After that you can
double-click `browser2zen.app` normally. When you're done, drag the DMG to
the Trash; browser2zen runs from inside the DMG and doesn't install itself
anywhere.

### Windows (x64)

1. Download `browser2zen-x.y.z-win-x64.zip`.
2. **Before extracting**, right-click the .zip in your Downloads
   folder and choose **Properties**. Tick the **Unblock** checkbox at
   the bottom and click OK. (This strips the Mark-of-the-Web tag from
   every file inside in one shot. If you skip this step, every .dll
   inside the bundle trips SmartScreen separately.)
3. Double-click the .zip and drag the `browser2zen` folder anywhere.
4. Open the `browser2zen` folder and double-click `browser2zen.exe`.
5. Windows will show *"Windows protected your PC"*. Click **More
   info**, then **Run anyway**.

The app launches. When you're done, drag the `browser2zen` folder to the
Recycle Bin; nothing else needs to be uninstalled.

> **Why all the steps?** Apple and Microsoft each charge developers
> for the certificates that remove these prompts. browser2zen is free
> open-source software and those fees aren't worth passing on. The
> macOS bundle is ad-hoc codesigned, so you can verify its contents
> have not been tampered with at any time:
>
> ```
> codesign --verify --deep --strict /Volumes/browser2zen/browser2zen.app
> ```
>
> Both platforms are reproducible from source: see [`build/`](./build).

### Linux (x86_64)

1. Download `browser2zen-x.y.z-linux-x86_64.tar.gz`.
2. Install the GTK 3 + WebKit2GTK runtime if you don't already have it:
   - Debian / Ubuntu: `sudo apt install python3-gi gir1.2-webkit2-4.1`
   - Fedora: `sudo dnf install python3-gobject webkit2gtk4.1`
   - Arch: `sudo pacman -S python-gobject webkit2gtk-4.1`
3. Extract: `tar -xzf browser2zen-*-linux-x86_64.tar.gz`.
4. Run: `./browser2zen/browser2zen`.

The Linux bundle is a PyInstaller `--onedir` build with no installer.
Drop the folder anywhere; delete it when you're done.

### Don't have Zen yet?

If Zen Browser isn't installed yet, the detection screen offers a
**Download Zen** button. Install Zen, launch it once so it creates
your profile, then click **Recheck** in browser2zen and continue.

---

## What gets migrated

| | |
| --- | --- |
| **Spaces** | Each source-browser space becomes a Zen workspace, with its emoji icon and colour theme. |
| **Pinned tabs** | All pinned tabs land on the matching workspace, in their original order. |
| **Folders** | The full nested folder hierarchy is preserved, collapsed by default to keep your sidebar clean. |
| **Essential tabs** | Arc's top-toolbar Essentials (Arc only) become pinned tabs on the right space. |
| **Open tabs** | Optional. Live tabs become real Zen tabs. |
| **Bookmarks** | Pinned tabs are also mirrored to Firefox bookmarks as a backup. |
| **Favicons** | The source's cached icons are inlined so tabs show their icons immediately, with no waiting for refetch. |
| **History** | Optional. Browsing history with original timestamps is copied over. |
| **Login state** | Optional. Chromium-format cookies are decrypted (via macOS Keychain or Windows DPAPI, depending on platform) and re-encrypted into Zen so you stay logged in to Gmail, Twitter, and the rest. |

Every step writes a timestamped backup beside your Zen profile before
it changes anything, and source-browser data is read-only. The Backups screen
inside the app lets you restore or delete those backups any time.

---

## Backup and restore

browser2zen also doubles as a portable Zen profile mover. From the
welcome screen, click **Backup or restore Zen** and pick a direction:

- **Export** bundles your current Zen profile into a single
  `.zenbackup` file (gzipped tarball with a `manifest.json`). Pick which
  categories to include — workspaces, browsing data, login state,
  favicons, and Zen Mods are on by default; saved passwords, preferences
  and extensions are opt-in because they're more fragile across machines.
- **Restore** opens an existing `.zenbackup` and writes its contents
  into a Zen profile on this machine. Each target file gets a
  timestamped backup before it's overwritten.

Use it to migrate a Zen profile to a new laptop, fork a profile for
testing, or hand a friend your setup as a single file.

```
~/Downloads/zen-backup-2026-05-07.zenbackup    # 17–125 MB depending on what's included
```

Zen must be quit on both ends — the export reads SQLite databases that
WAL-lock under live writes, and the restore writes into files Zen
expects to own. The GUI surfaces a "Quit Zen" button when it detects
Zen is running.

---

## Run from source

For Linux, Intel Mac, contributors, or anyone who'd rather skip the DMG:

```bash
git clone https://github.com/tarikbc/browser2zen.git
cd browser2zen
pip install -r requirements.txt -r requirements-build.txt
python -m app                # GUI (works on macOS and Windows)
python -m app --debug        # GUI with WebKit DevTools open
```

Individual importers can be exercised on their own (each is idempotent
and produces a timestamped backup before writing):

```bash
python3 src/chromium_history_importer.py --zen-profile "Default (release)"
python3 src/chromium_cookies_importer.py --zen-profile "Default (release)"
python3 src/zen_favicon_importer.py     --zen-profile "Default (release)"
```

The orchestrator's pipeline is what the GUI runs end-to-end; see
`app/orchestrator.py` if you want to drive a migration programmatically.

---

## How it works

The migration runs as a fixed pipeline of independent importers, each
of which reads source-browser data through a snapshot copy of the
underlying SQLite/plist file (so the source browser is never touched)
and writes to its corresponding Zen file with a backup taken first.

| Step | Reads | Writes (Zen) |
| --- | --- | --- |
| Spaces & pinned tabs | source bookmarks (`StorableSidebar.json` / Chromium `Bookmarks` / `places.sqlite` / `Bookmarks.plist`) | `zen-sessions.jsonlz4`, `containers.json` |
| Bookmarks | same as above | `places.sqlite` |
| Favicons | Chromium `Favicons` SQLite (Arc/Chrome/Edge/Brave only) | `favicons.sqlite`, plus inline `image` data URIs in `zen-sessions.jsonlz4` |
| History | Chromium `History` SQLite (Arc/Chrome/Edge/Brave only) | `places.sqlite` |
| Cookies | Chromium `Cookies` SQLite (AES-128-CBC on macOS, AES-256-GCM on Windows) | `cookies.sqlite` (incl. per-space containers) |

Firefox and Safari are bookmarks-only in v1 — their history and cookies
need a Firefox→Firefox places merger and a `Cookies.binarycookies`
parser respectively, neither of which ship in this release.

A few things that took some reverse-engineering and might be useful if
you're hacking on this:

- **Firefox `places::HashURL`** is the 48-bit hash function used in
  `moz_places.url_hash`, `moz_pages_w_icons.page_url_hash`, and
  `moz_icons.fixed_icon_url_hash`. Implementation in
  `src/zen_favicon_importer.py`.
- **Modern Zen renders pinned-tab favicons from each tab's inline
  `image` data URI in `zen-sessions.jsonlz4`**, not from
  `favicons.sqlite`. Writing only the SQLite store leaves the sidebar
  blank.
- **`moz_cookies.expiry` switched from seconds to milliseconds around
  Firefox 108.** Storing seconds makes Firefox treat every cookie as
  expired in 1970 and purge them all on next startup.
- **Cookies in container tabs are isolated.** Cookies imported with
  empty `originAttributes` are invisible to per-space containers, so
  each cookie is also written under `^userContextId=N` for every
  container the migration creates.

The GUI in `app/` wraps the same importer classes from `src/` without
modifying them. Architecture details are in [CLAUDE.md](./CLAUDE.md).

---

## Troubleshooting

- **"Zen profile not found"**: launch Zen once so it creates the
  profile, then click Recheck.
- **"No browser data found"**: make sure your source browser has been opened at least
  once and has at least one pinned tab.
- **Cookies didn't carry over**: close Zen completely before running
  (Firefox holds an exclusive lock on `cookies.sqlite` while open) and
  approve the Keychain prompt that appears on first run.
- **Something looks wrong after migration**: open the **Backups**
  screen in the app and restore the most recent backup of the relevant
  file. Or close Zen and copy any of the `*.backup.<timestamp>` files
  in your Zen profile directory back over the live file.

For anything else, [open an issue](./issues).

---

## Contributing

Pull requests welcome. The codebase is plain Python 3.7+ with a single
runtime dependency (`lz4`) plus `cryptography` for the cookies path.
The GUI uses PyWebView (Python) + vanilla HTML/CSS/JS with no build
step.

```bash
git clone https://github.com/tarikbc/browser2zen.git
cd browser2zen
pip install -r requirements.txt -r requirements-build.txt
python -m app --debug   # GUI with WebKit DevTools
```

To produce a `.dmg` locally:

```bash
bash build/make_app.sh   # produces dist/browser2zen.app
bash build/make_dmg.sh   # produces dist/browser2zen-<version>-arm64.dmg
```

CI builds happen automatically on every `v*` git tag via
[`.github/workflows/release.yml`](./.github/workflows/release.yml).

---

## License

MIT, see [LICENSE](./LICENSE).

Always back up your data before running migrations. Use at your own
risk.

## Acknowledgements

- Arc Browser team for inspiring the original arc2zen project.
- Zen Browser team for the privacy-focused alternative.
- The open source community for inspiration and tools.
