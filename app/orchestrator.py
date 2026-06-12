"""
MigrationOrchestrator — the single migration entry point.

The GUI wraps this via :class:`app.bridge.Bridge`; any headless caller
can import it directly. The orchestrator drives a fixed pipeline of
independent importers from ``src/``, layered with structured progress
events through :class:`ProgressBus`, plus cross-cutting concerns
(previous-migration detection, per-space preview counts).

Source-browser dispatch happens via a :class:`BrowserExtractor` instance
passed at construction; everything past ``extract`` is source-agnostic.
"""

from __future__ import annotations

import json
import logging
import sys
import time
import traceback
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# Make ``src/`` importable when running from the repo root or as a packaged app.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

# Existing importer modules (unchanged).
from chromium_cookies_importer import CookiesImporter, _discover_user_containers  # noqa: E402
from chromium_history_importer import HistoryImporter  # noqa: E402

# Source-browser adapters.
from extractors import ArcExtractor, BrowserExtractor  # noqa: E402
from zen_bookmark_importer import ZenBookmarkImporter  # noqa: E402
from zen_favicon_importer import FaviconImporter, _iter_pinned_urls  # noqa: E402
from zen_sessions_importer import ZenSessionsImporter  # noqa: E402
from zen_space_importer import ZenProfile as _SrcZenProfile
from zen_space_importer import ZenSpaceImporter  # noqa: E402

from .env_check import EnvReport, ZenProfile, check_environment
from .progress_bus import ProgressBus, ProgressEvent

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------ models

@dataclass(frozen=True)
class SpaceSummary:
    name: str
    icon: str | None
    pinned_count: int
    open_count: int
    folder_count: int
    essential_count: int = 0
    # Arc midTone colour as integer RGB (0-255) so the frontend can tint the
    # space card background to match the Arc workspace. ``None`` when the
    # space has no theme set.
    color: tuple[int, int, int] | None = None


@dataclass(frozen=True)
class PreviewReport:
    spaces: list[SpaceSummary]
    pinned_total: int
    open_total: int
    folder_total: int
    bookmark_total: int            # pinned + folder tabs (what gets bookmarked)
    favicon_match_estimate: int    # how many URLs Arc has cached icons for
    history_rows_estimate: int
    cookies_estimate: int


@dataclass
class MigrationOptions:
    zen_profile_path: Path
    space_filter: str | None = None    # substring filter for power-user CLI
    excluded_spaces: list[str] = field(default_factory=list)  # exact-name set for the Preview-screen checkboxes
    folders_collapsed: bool = True
    include_workspaces: bool = True
    include_pinned_tabs: bool = True
    include_bookmarks: bool = True
    include_favicons: bool = True
    include_open_tabs: bool = True
    include_history: bool = False
    include_cookies: bool = False


# ----------- step ordering used in the GUI's progress list (left to right) ---

GUI_STEPS = (
    "extract",
    "containers",
    "sessions",
    "bookmarks",
    "favicons",
    "history",
    "cookies",
    "finalize",
)

# ``extract`` is templated by the source-browser display name; everything
# else is source-agnostic.
STEP_LABELS = {
    "extract":    "Reading {source} data",
    "containers": "Creating containers",
    "sessions":   "Importing spaces, pinned tabs, open tabs and folders",
    "bookmarks":  "Backing up as bookmarks",
    "favicons":   "Importing favicons",
    "history":    "Importing browsing history",
    "cookies":    "Importing cookies",
    "finalize":   "Finalizing",
}


def _step_label(step: str, source_display: str) -> str:
    return STEP_LABELS.get(step, step).format(source=source_display)


# User-facing messages mapped from the structured error codes the
# Chromium cookie importer surfaces. Kept at module scope so the
# orchestrator's `_run` method stays focused on flow. ``{source}`` is
# filled with the active source-browser display name at emit time, so an
# Edge/Chrome/Brave user never sees a message that says "Arc".
_COOKIE_ERROR_MESSAGES = {
    # macOS
    "keychain_denied":
        "macOS Keychain access was denied; cookies skipped.",
    # Windows DPAPI / Chromium-family
    "chromium_local_state_missing":
        "{source} has not been launched on this Windows account yet; cookies skipped.",
    "chromium_no_encrypted_key":
        "{source} has no cookie encryption key on this account; cookies skipped.",
    "chromium_appbound_encryption":
        "{source} cookies use newer (v20) app-bound encryption; cookies skipped. "
        "Sign in fresh on imported sites.",
    "chromium_unknown_key_prefix":
        "{source} Local State key has an unrecognised prefix; cookies skipped.",
    "chromium_unexpected_key_length":
        "{source} DPAPI key has unexpected length; cookies skipped.",
    "dpapi_wrong_user":
        "Cookies were encrypted on a different Windows account and can't be "
        "migrated. Sign in fresh on imported sites.",
    "dpapi_failed":
        "Windows DPAPI rejected the cookie key; cookies skipped.",
    # Firefox
    "firefox_master_password_set":
        "Firefox profile has a master password set. Cookies stay encrypted in NSS "
        "and can't be migrated. Sign in fresh on imported sites.",
    # Safari
    "safari_needs_full_disk_access":
        "macOS hides Safari's cookie store behind Full Disk Access. "
        "Open System Settings, Privacy & Security, Full Disk Access, enable "
        "browser2zen, then click Recheck.",
    # Both
    "unsupported_platform":
        "Cookie import only supports macOS and Windows.",
    "cookies_db_missing":
        "Zen cookies.sqlite was not found.",
}


# ------------------------------------------------------------------ orchestrator

class MigrationOrchestrator:
    def __init__(self, source: BrowserExtractor | None = None) -> None:
        self.bus = ProgressBus()
        # Default source = Arc so existing callers (CLI/Arc-only frontend)
        # don't need to change.
        self.source: BrowserExtractor = source or ArcExtractor()

    # ---- env / preview --------------------------------------------------

    def check_environment(self) -> EnvReport:
        return check_environment(self.source)

    def preview(self, opts: MigrationOptions) -> PreviewReport:
        # Read-only: no bus, no temp files left behind.
        data = self.source.extract()
        spaces = data.spaces

        if opts.space_filter:
            needle = opts.space_filter.lower()
            spaces = [s for s in spaces if needle in s.space_name.lower()]

        space_summaries: list[SpaceSummary] = []
        pinned_total = open_total = folder_total = bookmark_total = 0
        all_urls: list[str] = []

        for s in spaces:
            pinned_count = len(s.pinned_tabs or [])
            open_count = len(s.open_tabs or [])
            essential = sum(1 for t in (s.pinned_tabs or []) if t.is_essential)
            folder_count = len(s.folders or [])
            color_rgb: tuple[int, int, int] | None = None
            if s.color and all(k in s.color for k in ("r", "g", "b")):
                color_rgb = (
                    int(round(s.color["r"] * 255)),
                    int(round(s.color["g"] * 255)),
                    int(round(s.color["b"] * 255)),
                )
            space_summaries.append(SpaceSummary(
                name=s.space_name,
                icon=s.icon,
                pinned_count=pinned_count,
                open_count=open_count,
                folder_count=folder_count,
                essential_count=essential,
                color=color_rgb,
            ))
            # Bookmarks ride their own channel; when a source doesn't set it
            # they fall back to pinned_tabs (matches to_legacy_dict).
            bookmarks = s.bookmarks if s.bookmarks is not None else (s.pinned_tabs or [])
            pinned_total += pinned_count
            open_total += open_count
            folder_total += folder_count
            bookmark_total += len(bookmarks)
            for t in (s.pinned_tabs or []):
                if t.url:
                    all_urls.append(t.url)
            for t in (s.open_tabs or []):
                if t.url:
                    all_urls.append(t.url)

        # Cheap estimates for the heavy steps. Real counts emerge during the
        # run; the Preview screen just uses these for UX.
        favicon_match_estimate = self._estimate_favicons(all_urls)
        history_rows_estimate = self._estimate_history_rows()
        cookies_estimate = self._estimate_cookies()

        return PreviewReport(
            spaces=space_summaries,
            pinned_total=pinned_total,
            open_total=open_total,
            folder_total=folder_total,
            bookmark_total=bookmark_total,
            favicon_match_estimate=favicon_match_estimate,
            history_rows_estimate=history_rows_estimate,
            cookies_estimate=cookies_estimate,
        )

    def _estimate_favicons(self, urls: list[str]) -> int:
        # Cheapest accurate estimate: count source URLs that have a cached icon.
        try:
            import sqlite3
            unique = set()
            for db in self.source.favicon_db_paths():
                try:
                    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2.0)
                    cur = conn.execute("SELECT DISTINCT page_url FROM icon_mapping")
                    for (page_url,) in cur:
                        unique.add(page_url)
                    conn.close()
                except Exception:
                    continue
            return len(unique & set(urls))
        except Exception:
            return 0

    def _estimate_history_rows(self) -> int:
        try:
            import sqlite3
            total = 0
            for db in self.source.history_db_paths():
                try:
                    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2.0)
                    n = conn.execute(
                        "SELECT COUNT(*) FROM urls WHERE url LIKE 'http%' OR url LIKE 'ftp%'"
                    ).fetchone()[0]
                    conn.close()
                    total += int(n or 0)
                except Exception:
                    continue
            return total
        except Exception:
            return 0

    def _estimate_cookies(self) -> int:
        try:
            import sqlite3
            total = 0
            for db in self.source.cookie_db_paths():
                try:
                    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2.0)
                    n = conn.execute("SELECT COUNT(*) FROM cookies").fetchone()[0]
                    conn.close()
                    total += int(n or 0)
                except Exception:
                    continue
            return total
        except Exception:
            return 0

    # ---- migration ------------------------------------------------------

    def migrate(self, opts: MigrationOptions) -> Iterator[ProgressEvent]:
        """Run the full migration. Yields events through the bus.

        Designed to be called on a worker thread; the JS bridge polls
        ``self.bus.drain()`` from its own thread.
        """
        self.bus.install()
        try:
            yield from self._run(opts)
        finally:
            self.bus.uninstall()

    def _emit(self, event: ProgressEvent) -> None:
        self.bus.push(event)

    def _start_step(self, step: str) -> None:
        self.bus.set_step(step)
        self._emit({"kind": "step_start", "step": step,
                    "message": _step_label(step, self.source.display_name)})

    def _done_step(self, step: str, summary: dict | None = None,
                   message: str | None = None) -> None:
        ev: ProgressEvent = {"kind": "step_done", "step": step,
                             "message": message or
                                        f"{_step_label(step, self.source.display_name)} done"}
        if summary is not None:
            ev["summary"] = summary
        self._emit(ev)

    def _error_step(self, step: str, exc: BaseException) -> None:
        self._emit({
            "kind": "step_error",
            "step": step,
            "message": f"{_step_label(step, self.source.display_name)} failed",
            "detail": "".join(traceback.format_exception_only(type(exc), exc)).strip(),
        })

    def _run(self, opts: MigrationOptions) -> Iterator[ProgressEvent]:
        zen_profile = opts.zen_profile_path

        # 1: extract -----------------------------------------------------
        self._start_step("extract")
        try:
            export = self.source.extract()
        except Exception as exc:
            self._error_step("extract", exc)
            yield from self._drain_yield()
            return
        if opts.space_filter:
            needle = opts.space_filter.lower()
            export.spaces = [s for s in export.spaces if needle in s.space_name.lower()]
            if not export.spaces:
                self._error_step("extract", RuntimeError(
                    f"No {self.source.display_name} space matches '{opts.space_filter}'."
                ))
                yield from self._drain_yield()
                return

        # Drop spaces the user unchecked on the Preview screen. The check is
        # exact-name and case-sensitive because that's what the Preview
        # checkboxes send; the substring ``space_filter`` above is the
        # power-user CLI knob and runs first.
        if opts.excluded_spaces:
            excluded = set(opts.excluded_spaces)
            export.spaces = [s for s in export.spaces if s.space_name not in excluded]
            if not export.spaces:
                self._error_step("extract", RuntimeError(
                    "All spaces were excluded. Pick at least one space to migrate."
                ))
                yield from self._drain_yield()
                return

        # Materialise the dict shape the Zen-side writers consume. Every
        # extractor lowers to this single shape; the writers stay
        # source-agnostic.
        export_data = export.to_legacy_dict()

        space_count = len(export_data.get("spaces", []))
        pinned_count = sum(len(sp.get("pinned_tabs") or []) for sp in export_data.get("spaces", []))
        self._done_step("extract", summary={
            "spaces": space_count,
            "pinned": pinned_count,
        }, message=f"Read {space_count} {self.source.display_name} spaces")
        yield from self._drain_yield()

        # 2: containers --------------------------------------------------
        container_mappings: dict = {}
        if opts.include_workspaces:
            self._start_step("containers")
            try:
                src_zen = _SrcZenProfile(name=zen_profile.name, path=zen_profile)
                space_importer = ZenSpaceImporter(src_zen)
                container_mappings = space_importer.import_spaces_as_containers(
                    export_data, dry_run=False
                ) or {}
                self._done_step("containers", summary={"created_or_reused": len(container_mappings)})
            except Exception as exc:
                self._error_step("containers", exc)
            yield from self._drain_yield()

        # 3: sessions / pinned tabs / folders ----------------------------
        if opts.include_pinned_tabs or opts.include_workspaces:
            self._start_step("sessions")
            try:
                # ``include_open_tabs`` controls whether Arc's currently-open
                # (non-pinned) tabs come along too. They go into the same
                # zen-sessions.jsonlz4 the pinned tabs do — modern Zen reads
                # both from there, regardless of pinned/unpinned status.
                # Filter them out of the data fed to the importer when the
                # toggle is off so we don't have to plumb a flag through.
                payload = export_data
                if not opts.include_open_tabs:
                    payload = dict(export_data)
                    payload["spaces"] = [
                        {**sp, "open_tabs": []}
                        for sp in export_data.get("spaces", [])
                    ]

                sess = ZenSessionsImporter(zen_profile, folders_collapsed=opts.folders_collapsed)
                ok = sess.import_data(payload, container_mappings, dry_run=False)
                pinned_total = sum(len(sp.get("pinned_tabs") or [])
                                   for sp in payload.get("spaces", []))
                open_total = sum(len(sp.get("open_tabs") or [])
                                 for sp in payload.get("spaces", []))
                self._done_step("sessions", summary={
                    "ok": bool(ok), "pinned": pinned_total, "open": open_total,
                })
            except Exception as exc:
                self._error_step("sessions", exc)
            yield from self._drain_yield()

        # 4: bookmarks ---------------------------------------------------
        if opts.include_bookmarks:
            self._start_step("bookmarks")
            try:
                bm = ZenBookmarkImporter(zen_profile)
                ok = bm.import_bookmarks(export_data, dry_run=False)
                self._done_step("bookmarks", summary={"ok": bool(ok)})
            except Exception as exc:
                self._error_step("bookmarks", exc)
            yield from self._drain_yield()

        # 5: favicons (DB + inline session image) -----------------------
        if opts.include_favicons:
            self._start_step("favicons")
            try:
                fav = FaviconImporter(
                    zen_profile, dry_run=False,
                    favicon_dbs=self.source.favicon_db_paths(),
                )
                urls = list(dict.fromkeys(_iter_pinned_urls(export_data)))
                db_summary = fav.import_favicons(urls)
                session_summary = fav.inject_session_images(urls)
                self._done_step("favicons", summary={
                    "db": db_summary, "session": session_summary,
                })
            except Exception as exc:
                self._error_step("favicons", exc)
            yield from self._drain_yield()

        # Open tabs are now part of the "sessions" step above (they go
        # into zen-sessions.jsonlz4 alongside pinned tabs, which is where
        # modern Zen actually reads them from). The legacy
        # ZenSessionstoreManager that used to write to sessionstore.jsonlz4
        # was a no-op because Zen's #restoreWindowData() overwrites the
        # sessionstore from zen-sessions on every launch.

        # 7: history -----------------------------------------------------
        if opts.include_history:
            self._start_step("history")
            try:
                if self.source.name == "firefox":
                    from firefox_history_importer import FirefoxHistoryImporter
                    h = FirefoxHistoryImporter(
                        zen_profile, dry_run=False,
                        history_dbs=self.source.history_db_paths(),
                    )
                elif self.source.name == "safari":
                    from safari_history_importer import SafariHistoryImporter
                    h = SafariHistoryImporter(
                        zen_profile, dry_run=False,
                        history_dbs=self.source.history_db_paths(),
                    )
                else:
                    h = HistoryImporter(
                        zen_profile, dry_run=False,
                        history_dbs=self.source.history_db_paths(),
                    )
                summary = h.import_history()
                self._done_step("history", summary=summary)
            except Exception as exc:
                self._error_step("history", exc)
            yield from self._drain_yield()

        # 8: cookies -----------------------------------------------------
        if opts.include_cookies:
            self._start_step("cookies")
            try:
                container_ids = _discover_user_containers(zen_profile)
                if self.source.name == "firefox":
                    from firefox_cookies_importer import FirefoxCookiesImporter
                    c = FirefoxCookiesImporter(
                        zen_profile, dry_run=False,
                        container_ids=container_ids,
                        cookie_dbs=self.source.cookie_db_paths(),
                    )
                elif self.source.name == "safari":
                    from safari_cookies_importer import SafariCookiesImporter
                    c = SafariCookiesImporter(
                        zen_profile, dry_run=False,
                        container_ids=container_ids,
                        cookie_dbs=self.source.cookie_db_paths(),
                    )
                else:
                    c = CookiesImporter(
                        zen_profile, dry_run=False,
                        container_ids=container_ids,
                        cookie_dbs=self.source.cookie_db_paths(),
                        keychain_service=getattr(self.source, "keychain_service", "Arc Safe Storage"),
                        keychain_account=getattr(self.source, "keychain_account", "Arc"),
                        local_state_paths=self.source.local_state_paths(),
                    )
                summary = c.import_cookies()
                if summary.get("error"):
                    err = summary["error"]
                    msg = _COOKIE_ERROR_MESSAGES.get(
                        err, f"Cookie import failed: {err}"
                    ).format(source=self.source.display_name)
                    self._error_step("cookies", RuntimeError(msg))
                else:
                    self._done_step("cookies", summary=summary)
            except Exception as exc:
                self._error_step("cookies", exc)
            yield from self._drain_yield()

        # 9: finalize ----------------------------------------------------
        self._start_step("finalize")
        try:
            (zen_profile / ".browser2zen-migrated").write_text(
                json.dumps({"ts": time.time(), "version": 1}), encoding="utf-8"
            )
        except Exception:
            pass
        self._done_step("finalize", message="Migration complete")
        yield from self._drain_yield()

    def _drain_yield(self) -> Iterator[ProgressEvent]:
        yield from self.bus.drain()

    # ---- backups + utility for the Done screen --------------------------

    @staticmethod
    def find_backups(zen_profile: Path) -> list[Path]:
        """Backup files we (and the existing importers) leave behind."""
        if not zen_profile.is_dir():
            return []
        out: list[Path] = []
        out.extend(sorted(zen_profile.glob("*.backup.*")))
        return out


# ----- JSON helpers for the bridge ----------------------------------------


def preview_to_dict(report: PreviewReport) -> dict:
    return {
        "spaces": [
            {
                "name": s.name,
                "icon": s.icon,
                "pinnedCount": s.pinned_count,
                "openCount": s.open_count,
                "folderCount": s.folder_count,
                "essentialCount": s.essential_count,
                "color": list(s.color) if s.color else None,
            }
            for s in report.spaces
        ],
        "pinnedTotal": report.pinned_total,
        "openTotal": report.open_total,
        "folderTotal": report.folder_total,
        "bookmarkTotal": report.bookmark_total,
        "faviconMatchEstimate": report.favicon_match_estimate,
        "historyRowsEstimate": report.history_rows_estimate,
        "cookiesEstimate": report.cookies_estimate,
    }
