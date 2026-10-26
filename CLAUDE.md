# CLAUDE.md

Guidance for Claude Code (and any other contributor reading this) when
working on browser2zen.

## What this is

A migration tool that moves your browser setup — workspaces, pinned
tabs, bookmarks, history, login state — from Arc / Dia / Chrome / Edge /
Brave / Firefox / Safari into [Zen Browser](https://zen-browser.app/).

The user-facing surface is a single-window PyWebView app. Power users
can drive the orchestrator programmatically.

## Run

```bash
pip install -r requirements.txt -r requirements-build.txt
python -m app           # GUI
python -m app --debug   # GUI with WebKit DevTools
```

## Architecture

The migration is a fixed pipeline of independent steps. Source-browser
adaptation happens once at the top via a `BrowserExtractor`; everything
downstream is source-agnostic.

```
BrowserExtractor.extract() → ExportData → to_legacy_dict()
                                                │
                                                ▼
              ┌─── ZenSpaceImporter      → containers.json
              ├─── ZenSessionsImporter   → zen-sessions.jsonlz4
              ├─── ZenBookmarkImporter   → places.sqlite (moz_bookmarks)
              ├─── FaviconImporter       → favicons.sqlite + inline data URIs
              ├─── HistoryImporter       → places.sqlite (moz_places, moz_historyvisits)
              ├─── CookiesImporter       → cookies.sqlite (incl. per-container dupes)
              └─── ZenPasswordImporter   → logins.json + key4.db (via Zen's own importer)
```

The password step is the one writer that does not hand-write a Zen file.
Zen encrypts logins with its NSS key, so `ZenPasswordImporter` briefly
launches Zen headless with Marionette and drives Zen's built-in
`ChromeProfileMigrator`, restricted to passwords, against the Chromium
source's `User Data`. No password plaintext passes through browser2zen,
and nothing reverse-engineers Zen's credential crypto. It runs only for
Chromium sources and refuses while Zen is open, since Zen must own the
login store during the write.

### Source extractors (`src/extractors/`)

| File | Source | Notes |
|---|---|---|
| `arc.py` | Arc | Wraps `arc_pinned_tab_extractor.py` (StorableSidebar.json parser, Spaces / Essentials / `childrenIds` ordering) |
| `chromium.py` | _shared base_ | Profile discovery, Bookmarks-JSON walk, SNSS session read (`_snss.py`), Chromium SQLite paths, cookie-key dispatch |
| `_snss.py` | _shared base_ | Parses Chromium's binary SessionService file (`<profile>/Sessions/Session_*`) to recover the real tab-strip **pinned + open tabs**. Bookmarks are NOT pinned tabs |
| `chrome.py`, `edge.py`, `brave.py` | Chrome / Edge / Brave | Subclass `ChromiumExtractor`; only paths + Keychain service / process names differ |
| `dia.py` | Dia | Subclasses `ChromiumExtractor`. Dia keeps Spaces and pinned tabs in an encrypted `tabs.db`, so they are read live over AppleScript (`profiles` of a window are Dia's Spaces) and snapshotted before `quit()`; falls back to SNSS when Dia is not running. The snapshot is dropped if Dia's session files change after the quit. Space ids derive from the space name (Dia's AppleScript exposes no stable id), so renaming a space creates a new Zen workspace on a re-run |
| `firefox.py` | Firefox | Reads `places.sqlite` `moz_bookmarks` directly; v1 is bookmarks-only (history+cookies need a Firefox→Firefox merger) |
| `safari.py` | Safari | Parses `Bookmarks.plist`; bookmarks-only; surfaces a clean error code if Full Disk Access is missing on Sequoia |

Every extractor lowers to the same `ExportData` payload, then
`to_legacy_dict()` produces the dict shape the Zen-side writers
consume. Only `extractors/arc.py` carries Arc-specific schema knowledge
(Essentials, etc.).

**Pinned tabs vs bookmarks are distinct channels.** `pinned_tabs` /
`open_tabs` are the Zen *sidebar* tabs (→ `ZenSessionsImporter`);
`bookmarks` / `bookmark_folders` are the bookmark backup (→
`ZenBookmarkImporter`). For Chromium the sidebar tabs come from the SNSS
session and bookmarks come from the `Bookmarks` JSON — they must not be
conflated, or you flood the sidebar with every bookmark. When an
extractor leaves `bookmarks`/`bookmark_folders` as `None`,
`to_legacy_dict()` falls them back to `pinned_tabs`/`folders`, so
bookmark-only sources (Arc/Firefox/Safari) are unaffected.
`ExportData.profile_bookmarks` / `profile_bookmark_folders` hold
bookmarks that belong to no space (Dia's Chromium profile);
`to_legacy_dict()` attaches them to the first space left after the
Preview screen's exclusions, so unticking a space never drops them.

### Zen-side writers (`src/zen_*.py`)

These are source-agnostic; they only know the legacy dict shape. The
heavy ones reverse-engineer Firefox internals:

- **`places::HashURL`** — 48-bit hash with prefix in upper 16 bits.
  Lives in `zen_favicon_importer.py:hash_page_url` and is reused by
  `chromium_history_importer.py` for `moz_places.url_hash`.
- **`zen-sessions.jsonlz4`** is the source of truth for Zen's sidebar
  tabs, NOT the `zen_pins` table. Modern Zen overwrites
  `sessionstore.jsonlz4` from `zen-sessions.jsonlz4` on every launch,
  so writing only the sessionstore is a no-op.
- **Pinned-tab favicons** are read from each tab's inline `image` data
  URI inside `zen-sessions.jsonlz4`, not from `favicons.sqlite`.
  Writing only the SQLite store leaves the sidebar blank.
- **`moz_cookies.expiry`** switched from seconds to milliseconds around
  Firefox 108. Storing seconds makes Firefox treat every cookie as
  expired in 1970 and purge them all on next startup.
- **Container cookies** need `^userContextId=N` duplicates per
  container so cookies are visible to per-space tabs.

### Backup/restore (`src/zen_backup.py`)

Independent of the migration pipeline. Two classes, ~330 lines total:

- `ZenBackupExporter(zen_profile, output_path, includes)` — snapshots
  SQLite + WAL/SHM siblings via the same temp-dir pattern
  `chromium_history_importer._snapshot` uses, copies the rest verbatim,
  writes a `manifest.json` with `format_version`, `browser2zen_version`,
  `source_profile_name`, and the included category list, then tar-gzips
  the whole staging dir to `<name>.zenbackup`.
- `ZenBackupImporter(archive_path, target_zen_profile, includes)` —
  validates the manifest version, takes a `.backup.<ts>` snapshot of
  each existing target file before overwriting, drops stale `-wal`/`-shm`
  siblings so SQLite re-reads cleanly, writes a `.browser2zen-restored`
  marker (sibling to `.browser2zen-migrated`).

The categories live in `CATEGORY_FILES` — a dict mapping `workspaces`,
`browsing`, `cookies`, `favicons`, `passwords`, `prefs`, `extensions`
to the file globs that belong to each. `DEFAULT_CATEGORIES` is the
on-by-default subset (the first four — the riskier ones default off).

Bridge methods follow the same worker-thread + `ProgressBus` pattern
as `start_migration`: `start_zen_export`, `start_zen_restore`,
`preview_zen_backup`, plus `choose_path` (a wrapper around
`webview.create_file_dialog` for save/open dialogs).

### Chromium readers (`src/chromium_*.py`, `src/zen_favicon_importer.py`)

- `chromium_history_importer.py` — Chromium `History` SQLite → Firefox
  `places.sqlite` (handles WebKit→Unix time, transition mapping).
- `chromium_cookies_importer.py` — Chromium `Cookies` SQLite → Firefox
  `cookies.sqlite`. macOS Keychain (PBKDF2 1003 / AES-128-CBC) on
  macOS, DPAPI + AES-256-GCM on Windows. Per-browser Keychain service
  name and Local State path come in via `__init__` kwargs.
- `zen_favicon_importer.py` — Chromium `Favicons` SQLite → Firefox
  `favicons.sqlite`, plus inline `image` data URI injection into
  `zen-sessions.jsonlz4`.

All three accept their per-source paths via `__init__` kwargs
(`history_dbs=`, `cookie_dbs=`, `favicon_dbs=`); the orchestrator
wires the chosen extractor's paths through.

### GUI app (`app/`)

- `app/orchestrator.py` — `MigrationOrchestrator`. Takes a
  `BrowserExtractor`, drives the pipeline, emits `ProgressEvent` dicts.
- `app/progress_bus.py` — `logging.Handler` that pushes records onto
  a queue the JS frontend polls.
- `app/env_check.py` — source + Zen detection, profile listing,
  running-process check.
- `app/browser_control.py` — graceful quit for Arc and Zen.
  Other browsers go through their `BrowserExtractor.quit()` method.
- `app/bridge.py` — JS bridge surface
  (`window.pywebview.api.<method>`). Source picker, backups, preview,
  start/cancel migration.
- `app/window.py` — PyWebView frameless-with-vibrancy window setup.
- `app/frontend/` — vanilla HTML+CSS+JS, no build step. Seven brand
  badges + Zen badge live under `assets/sources/`.

### Adding a new source

1. Create `src/extractors/<name>.py` subclassing `BrowserExtractor`
   (or `ChromiumExtractor` if it's Chromium-family).
2. Implement `extract()` to produce `ExportData` (one or more
   `SpaceRecord`s).
3. Add to the `EXTRACTORS` tuple in `src/extractors/__init__.py`.
4. Drop the brand SVG into `app/frontend/assets/sources/<name>.svg`
   and add the slug to `SOURCE_NAMES` in `app/frontend/app.js`.
5. Add a brand-tinted background rule in `app/frontend/styles.css`
   (`.brand-mark.<name>`).

That's it — the picker, orchestrator, and Zen writers all pick it up
without further changes.

## Build

```bash
bash build/make_app.sh   # dist/browser2zen.app
bash build/make_dmg.sh   # dist/browser2zen-<version>-arm64.dmg
pwsh build/make_exe.ps1  # dist/browser2zen/browser2zen.exe (Windows)
pwsh build/make_zip.ps1  # dist/browser2zen-<version>-win-x64.zip
```

CI builds happen automatically on every `v*` tag via
`.github/workflows/release.yml`.

## Database safety

- Source-browser data is read-only — every reader works against a
  `shutil.copy2`'d temp snapshot, never the live DB.
- Every Zen-side write is preceded by a timestamped `.backup.<ts>`
  copy in the same directory.
- The Backups screen in the GUI restores or deletes those.
- Zen has to be quit during migration so we don't fight its WAL lock.

## Conventions

- Python 3.7+, runtime deps are `lz4` and `cryptography`.
- No build step for the frontend — use `createElement` + `textContent`
  exclusively, never `innerHTML` with non-literal data.
- Source-agnostic identifiers in shared code (`source`, `export_data`,
  `db`); Arc-specific names only inside Arc-only files.
- `1000`-decade Chromium DPAPI errors surface as
  `chromium_local_state_missing` / `chromium_appbound_encryption` etc.
  through the orchestrator → Bridge → frontend dialog mapping.
