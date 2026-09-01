"""
JavaScript bridge for the GUI.

PyWebView passes the ``Bridge`` instance as ``js_api`` and every public method
becomes callable from JS as ``window.pywebview.api.<method>``. All methods
return JSON-serialisable values (or ``None``).

Migration runs on a worker thread so the JS side can poll
``drain_progress`` while the importers do their work. The orchestrator's
``ProgressBus`` is the queue between them.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
import traceback
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Optional

from .browser_control import launch_zen, open_in_finder, quit_browser
from .env_check import env_report_to_dict
from .orchestrator import (
    GUI_STEPS,
    STEP_LABELS,
    MigrationOptions,
    MigrationOrchestrator,
    preview_to_dict,
)

logger = logging.getLogger(__name__)


def _safe(payload: Any) -> Any:
    """Best-effort JSON sanitiser for return values."""
    if isinstance(payload, Path):
        return str(payload)
    if is_dataclass(payload) and not isinstance(payload, type):
        return _safe(asdict(payload))
    if isinstance(payload, dict):
        return {str(k): _safe(v) for k, v in payload.items()}
    if isinstance(payload, (list, tuple)):
        return [_safe(v) for v in payload]
    return payload


class Bridge:
    def __init__(self) -> None:
        self.orchestrator = MigrationOrchestrator()
        self._worker: threading.Thread | None = None
        self._final_state: dict = {"status": "idle"}  # 'idle' | 'running' | 'done' | 'error'
        self._lock = threading.Lock()

    # ---------- source-browser picker --------------------------------

    def list_sources(self) -> list:
        """Return the catalogue of supported source browsers, with each
        marked installed/running so the picker can dim entries the user
        can't migrate from."""
        # Local import keeps the source registry out of the bridge's
        # cold-start path; we don't need it until the user opens the
        # source picker.
        from extractors import EXTRACTORS  # type: ignore[import-not-found]
        out: list[dict] = []
        for cls in EXTRACTORS:
            inst = cls()
            try:
                installed = inst.is_installed()
            except Exception:
                installed = False
            try:
                running = inst.is_running() if installed else False
            except Exception:
                running = False
            out.append({
                "name": cls.name,
                "displayName": cls.display_name,
                "installed": installed,
                "running": running,
            })
        return out

    def set_source(self, name: str) -> dict:
        """Switch the orchestrator's current source browser. Returns the
        same shape as one entry of :meth:`list_sources` so the frontend
        can confirm the switch landed."""
        from extractors import by_name  # type: ignore[import-not-found]
        try:
            cls = by_name(name)
        except KeyError:
            return {"ok": False, "error": f"unknown source: {name!r}"}
        inst = cls()
        self.orchestrator = MigrationOrchestrator(source=inst)
        # Reset run state — the new source means a fresh detect/preview.
        with self._lock:
            self._final_state = {"status": "idle"}
        return {
            "ok": True,
            "name": cls.name,
            "displayName": cls.display_name,
            "installed": inst.is_installed(),
            "running": inst.is_running(),
        }

    def current_source(self) -> dict:
        src = self.orchestrator.source
        return {"name": src.name, "displayName": src.display_name}

    # ----------------------------- window helpers -----------------------------

    def quit_app(self) -> None:
        """Close the window. Deferred so the current JS-Python call can return
        before WKWebView is torn down (otherwise the JS promise never resolves
        and the window appears to crash/hang)."""
        win = getattr(self, "_window", None)
        if win is None:
            return
        threading.Timer(0.05, self._destroy_window_safely).start()

    def _destroy_window_safely(self) -> None:
        try:
            self._window.destroy()
        except Exception:
            pass

    # ---- backup management ------------------------------------------

    def list_backups(self, profile_path: str | None = None) -> list:
        """List all *.backup.<unix_ts> files in a Zen profile, newest first."""
        from datetime import datetime
        profile = Path(profile_path) if profile_path else self._guess_zen_profile()
        if profile is None or not profile.is_dir():
            return []
        items: list[dict] = []
        for f in profile.glob("*.backup.*"):
            try:
                ts = int(f.name.rsplit(".backup.", 1)[1])
            except (ValueError, IndexError):
                continue
            original = f.name.split(".backup.")[0]
            try:
                size = f.stat().st_size
            except OSError:
                continue
            items.append({
                "path": str(f),
                "name": f.name,
                "original": original,
                "ts": ts,
                "size": size,
                "iso": datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S"),
            })
        items.sort(key=lambda x: x["ts"], reverse=True)
        return items

    def restore_backup(self, backup_path: str) -> dict:
        src = Path(backup_path)
        if not src.is_file():
            return {"ok": False, "error": "backup not found"}
        try:
            original = src.name.split(".backup.")[0]
        except Exception:
            return {"ok": False, "error": "could not derive original filename"}
        target = src.parent / original
        try:
            import shutil as _shutil
            # Snapshot the current target before overwriting (so the user can
            # roll forward again if they restore the wrong backup).
            if target.is_file():
                ts = int(time.time())
                _shutil.copy2(target, target.with_name(f"{original}.backup.{ts}"))
            _shutil.copy2(src, target)
            # Force WAL/SHM stale files to be discarded so SQLite re-reads cleanly.
            for suffix in ("-wal", "-shm"):
                stale = target.with_name(target.name + suffix)
                if stale.is_file():
                    try:
                        stale.unlink()
                    except Exception:
                        pass
            return {"ok": True, "restored": str(target)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def delete_backup(self, backup_path: str) -> dict:
        src = Path(backup_path)
        if not src.is_file():
            return {"ok": False, "error": "backup not found"}
        try:
            src.unlink()
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def _guess_zen_profile(self) -> Path | None:
        from .env_check import list_zen_profiles
        profs = list_zen_profiles()
        return profs[0].path if profs else None

    def open_path_in_finder(self, path: str) -> bool:
        return open_in_finder(path)

    def open_url(self, url: str) -> bool:
        """Open an http(s) URL in the user's default browser, cross-platform."""
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            return False
        try:
            import webbrowser
            return webbrowser.open(url, new=2)
        except Exception:
            return False

    def platform(self) -> str:
        """Return ``"mac"``, ``"win"``, or ``"linux"`` for the JS side to gate UI."""
        if sys.platform == "darwin":
            return "mac"
        if os.name == "nt":
            return "win"
        return "linux"

    def version(self) -> str:
        """Return the canonical app version string (e.g. ``"1.1.0"``)."""
        try:
            from .__version__ import VERSION
            return VERSION
        except Exception:
            return "0.0.0"

    def copy_to_clipboard(self, text: str) -> bool:
        if not isinstance(text, str):
            return False
        try:
            import subprocess
            if sys.platform == "darwin":
                p = subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE)
                p.communicate(input=text.encode("utf-8"), timeout=2)
                return p.returncode == 0
            if os.name == "nt":
                # clip.exe ships with every Windows install. It expects
                # UTF-16 LE on stdin; pass without a BOM.
                p = subprocess.Popen(["clip.exe"], stdin=subprocess.PIPE)
                p.communicate(input=text.encode("utf-16-le"), timeout=2)
                return p.returncode == 0
        except Exception:
            return False
        return False

    # ---------- environment / preview / migration --------------

    def check_env(self) -> dict:
        try:
            report = self.orchestrator.check_environment()
            return _safe(env_report_to_dict(report))
        except Exception as exc:
            logger.exception("check_env failed")
            return {"error": str(exc), "trace": traceback.format_exc()}

    def quit_browser(self, name: str) -> dict:
        # ``arc`` and ``zen`` keep going through the legacy graceful-quit
        # helper (it ships AppleScript / taskkill maps for both). Anything
        # else is delegated to the source extractor's ``quit()``, which
        # knows the right process name for that browser.
        if name == "zen":
            return _safe(quit_browser("zen"))
        if name == "arc":
            return _safe(quit_browser("arc"))
        # Source-browser path: the JS sends the source name directly
        # (``chrome``, ``edge``, ``brave``, ``firefox``, ``safari``).
        try:
            from extractors import by_name  # type: ignore[import-not-found]
            inst = by_name(name)()
            return _safe(inst.quit())
        except KeyError:
            return {"ok": False, "error": f"unknown browser: {name!r}"}
        except Exception as exc:
            logger.exception("quit_browser %s failed", name)
            return {"ok": False, "error": str(exc)}

    def is_zen_running(self) -> bool:
        # Used by the backup/restore screens to gate the action. Cheaper
        # than ``check_env`` since it skips source-browser detection.
        from env_check import is_zen_running as _is_zen_running
        try:
            return bool(_is_zen_running())
        except Exception:
            logger.exception("is_zen_running failed")
            return False

    def quit_source(self) -> dict:
        """Quit whatever source browser is currently selected. Lets the
        frontend skip the name dispatch entirely."""
        try:
            return _safe(self.orchestrator.source.quit())
        except Exception as exc:
            logger.exception("quit_source failed")
            return {"ok": False, "error": str(exc)}

    def launch_zen(self) -> bool:
        return launch_zen()

    def preview(self, opts_json: str) -> dict:
        try:
            opts = self._parse_options(opts_json)
            report = self.orchestrator.preview(opts)
            return _safe(preview_to_dict(report))
        except Exception as exc:
            logger.exception("preview failed")
            return {"error": str(exc), "trace": traceback.format_exc()}

    def start_migration(self, opts_json: str) -> dict:
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                return {"ok": False, "error": "migration already running"}

        try:
            opts = self._parse_options(opts_json)
        except Exception as exc:
            return {"ok": False, "error": f"bad options: {exc}"}

        with self._lock:
            self._final_state = {"status": "running"}

        def _run() -> None:
            try:
                # Drain the iterator; events flow into the queue via the bus.
                for _ in self.orchestrator.migrate(opts):
                    pass
                with self._lock:
                    self._final_state = {
                        "status": "done",
                        "backups": [str(p) for p in self.orchestrator.find_backups(opts.zen_profile_path)],
                        "zenProfilePath": str(opts.zen_profile_path),
                    }
            except Exception as exc:
                logger.exception("migration worker crashed")
                with self._lock:
                    self._final_state = {
                        "status": "error",
                        "error": str(exc),
                        "trace": traceback.format_exc(),
                    }

        self._worker = threading.Thread(target=_run, daemon=True, name="browser2zen-migrate")
        self._worker.start()
        return {"ok": True}

    def drain_progress(self) -> dict:
        events = self.orchestrator.bus.drain()
        with self._lock:
            state = dict(self._final_state)
        return {"events": _safe(events), "state": state, "steps": list(GUI_STEPS), "labels": STEP_LABELS}

    # ---------- backup + restore (.zenbackup) ----------
    #
    # The migration flow lives in MigrationOrchestrator. Backup is its own
    # thing: a single tar.gz on disk, no Zen-side writers downstream. We
    # reuse the same worker-thread + ProgressBus pattern so the existing
    # progress screen Just Works. Each "step" emits a tiny dict via the
    # bus; the frontend renders progress identically to migration.

    def list_zen_profiles_json(self) -> list:
        """Helper: profiles formatted for the backup screens (lighter
        shape than env_check.list_zen_profiles)."""
        from .env_check import list_zen_profiles
        return [
            {"name": p.name, "path": str(p.path),
             "isRelease": p.is_release, "hasZenSessions": p.has_zen_sessions,
             "install": p.install}
            for p in list_zen_profiles()
        ]

    def list_backup_categories(self) -> list:
        """Catalogue of (category, label, default_on, caveat) for the
        export and restore screens."""
        return [
            {"id": "workspaces", "label": "Workspaces, pinned tabs, folders",
             "default": True, "caveat": ""},
            {"id": "browsing", "label": "Browsing data (bookmarks + history)",
             "default": True, "caveat": ""},
            {"id": "cookies", "label": "Login state",
             "default": True, "caveat": ""},
            {"id": "favicons", "label": "Favicons",
             "default": True, "caveat": ""},
            {"id": "passwords", "label": "Saved passwords",
             "default": False,
             "caveat": "If a master password is set on the source profile, "
                       "the same password is required on the target machine. "
                       "The encryption key travels with key4.db."},
            {"id": "prefs", "label": "Preferences",
             "default": False,
             "caveat": "A few prefs reference absolute paths from the source "
                       "machine. Most settings transfer cleanly, but some "
                       "UI-state knobs may reset."},
            {"id": "extensions", "label": "Extensions",
             "default": False,
             "caveat": "Extensions need to be compatible with the target "
                       "machine's Zen version. Mismatches can leave "
                       "extensions disabled. Adds the most archive size."},
            {"id": "mods", "label": "Zen Mods (UI customisations)",
             "default": True,
             "caveat": "If a mod targets a specific Zen version, the same "
                       "Zen version is needed on the target for it to "
                       "render correctly."},
        ]

    def choose_path(self, kind: str, default_name: str = "") -> str | None:
        """Native file dialog. ``kind`` is 'save' or 'open'."""
        try:
            import webview as _webview
        except Exception as exc:
            logger.warning("pywebview missing for choose_path: %s", exc)
            return None
        if not _webview.windows:
            return None
        win = _webview.windows[0]

        if kind == "save":
            result = win.create_file_dialog(
                _webview.SAVE_DIALOG,
                save_filename=default_name or "zen-backup.zenbackup",
                file_types=("Zen backup (*.zenbackup)",),
            )
        else:
            result = win.create_file_dialog(
                _webview.OPEN_DIALOG,
                file_types=("Zen backup (*.zenbackup)",),
            )
        # PyWebView returns a tuple of paths on macOS, a single path string
        # on some other platforms, and None on cancel.
        if not result:
            return None
        if isinstance(result, (list, tuple)):
            return str(result[0]) if result else None
        return str(result)

    def preview_zen_backup(self, archive_path: str) -> dict:
        """Read the manifest of a .zenbackup file without unpacking."""
        from pathlib import Path as _Path

        from zen_backup import ZenBackupImporter
        importer = ZenBackupImporter(_Path(archive_path), target_zen_profile=_Path("/"))
        return _safe(importer.preview())

    def start_zen_export(
        self,
        profile_path: str,
        output_path: str,
        includes_json: str,
    ) -> dict:
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                return {"ok": False, "error": "another job is running"}
        try:
            includes = json.loads(includes_json) if isinstance(includes_json, str) else list(includes_json)
            if not isinstance(includes, list):
                raise ValueError("includes must be a JSON array")
        except Exception as exc:
            return {"ok": False, "error": f"bad includes: {exc}"}

        with self._lock:
            self._final_state = {"status": "running", "kind": "export"}

        def _run() -> None:
            from pathlib import Path as _Path

            from zen_backup import ZenBackupExporter
            try:
                self._emit_step("snapshot", "Reading the Zen profile")
                exporter = ZenBackupExporter(
                    _Path(profile_path), _Path(output_path), includes=includes,
                )
                summary = exporter.export()
                if summary.get("ok"):
                    self._emit_step_done("snapshot", summary={"file_count": summary["file_count"]})
                    self._emit_step("bundle", "Writing the archive",
                                    summary={"bytes_out": summary["bytes_out"]})
                    self._emit_step_done("bundle", summary={"bytes_out": summary["bytes_out"]})
                    self._emit_step("finalize", "Done")
                    self._emit_step_done("finalize")
                    with self._lock:
                        self._final_state = {
                            "status": "done",
                            "kind": "export",
                            "archivePath": str(output_path),
                            "bytesOut": summary["bytes_out"],
                            "fileCount": summary["file_count"],
                        }
                else:
                    err = "; ".join(summary.get("errors", []) or ["unknown export failure"])
                    self._emit_step_error("snapshot", err)
                    with self._lock:
                        self._final_state = {"status": "error", "kind": "export", "error": err}
            except Exception as exc:
                logger.exception("zen export worker crashed")
                with self._lock:
                    self._final_state = {
                        "status": "error", "kind": "export",
                        "error": str(exc),
                        "trace": traceback.format_exc(),
                    }

        self._worker = threading.Thread(target=_run, daemon=True, name="browser2zen-export")
        self._worker.start()
        return {"ok": True}

    def start_zen_restore(
        self,
        archive_path: str,
        target_profile_path: str,
        includes_json: str,
    ) -> dict:
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                return {"ok": False, "error": "another job is running"}
        try:
            includes = json.loads(includes_json) if isinstance(includes_json, str) else list(includes_json)
            # ``None`` means "everything in the archive" — JS sends null.
            if includes is not None and not isinstance(includes, list):
                raise ValueError("includes must be a JSON array or null")
        except Exception as exc:
            return {"ok": False, "error": f"bad includes: {exc}"}

        with self._lock:
            self._final_state = {"status": "running", "kind": "restore"}

        def _run() -> None:
            from pathlib import Path as _Path

            from zen_backup import ZenBackupImporter
            try:
                self._emit_step("preflight", "Validating the archive")
                importer = ZenBackupImporter(
                    _Path(archive_path),
                    _Path(target_profile_path),
                    includes=includes,
                )
                preview = importer.preview()
                if not preview.get("ok"):
                    err = "; ".join(preview.get("errors", []) or ["bad archive"])
                    self._emit_step_error("preflight", err)
                    with self._lock:
                        self._final_state = {"status": "error", "kind": "restore", "error": err}
                    return
                self._emit_step_done("preflight",
                                     summary={"manifest": preview.get("manifest")})

                self._emit_step("restore", "Restoring files")
                summary = importer.import_archive()
                if summary.get("ok"):
                    self._emit_step_done("restore", summary={
                        "restored": len(summary.get("restored_files", [])),
                        "skipped": len(summary.get("skipped", [])),
                    })
                    self._emit_step("finalize", "Done")
                    self._emit_step_done("finalize")
                    with self._lock:
                        self._final_state = {
                            "status": "done", "kind": "restore",
                            "targetProfilePath": str(target_profile_path),
                            "restoredCount": len(summary.get("restored_files", [])),
                        }
                else:
                    err = "; ".join(summary.get("errors", []) or ["unknown restore failure"])
                    self._emit_step_error("restore", err)
                    with self._lock:
                        self._final_state = {"status": "error", "kind": "restore", "error": err}
            except Exception as exc:
                logger.exception("zen restore worker crashed")
                with self._lock:
                    self._final_state = {
                        "status": "error", "kind": "restore",
                        "error": str(exc),
                        "trace": traceback.format_exc(),
                    }

        self._worker = threading.Thread(target=_run, daemon=True, name="browser2zen-restore")
        self._worker.start()
        return {"ok": True}

    # Tiny helpers that emit step events through the orchestrator's bus
    # without owning a full progress pipeline. The frontend's existing
    # progress renderer reads these the same way it reads migration steps.
    def _emit_step(self, step: str, message: str, summary: dict | None = None) -> None:
        ev: dict = {"kind": "step_start", "step": step, "message": message}
        if summary is not None:
            ev["summary"] = summary
        self.orchestrator.bus.push(ev)

    def _emit_step_done(self, step: str, summary: dict | None = None) -> None:
        ev: dict = {"kind": "step_done", "step": step}
        if summary is not None:
            ev["summary"] = summary
        self.orchestrator.bus.push(ev)

    def _emit_step_error(self, step: str, detail: str) -> None:
        self.orchestrator.bus.push({"kind": "step_error", "step": step, "detail": detail})

    def get_step_metadata(self) -> dict:
        return {"steps": list(GUI_STEPS), "labels": STEP_LABELS}

    # ----------------------------- helpers -----------------------------------

    def set_window(self, window: Any) -> None:
        self._window = window

    @staticmethod
    def _parse_options(opts_json: str) -> MigrationOptions:
        data = json.loads(opts_json) if isinstance(opts_json, str) else dict(opts_json)
        zen_profile = Path(data["zenProfilePath"]).expanduser()
        excluded_raw = data.get("excludedSpaces") or []
        excluded_spaces = [str(name) for name in excluded_raw if isinstance(name, str)]
        return MigrationOptions(
            zen_profile_path=zen_profile,
            space_filter=data.get("spaceFilter") or None,
            excluded_spaces=excluded_spaces,
            folders_collapsed=bool(data.get("foldersCollapsed", True)),
            include_workspaces=bool(data.get("includeWorkspaces", True)),
            include_pinned_tabs=bool(data.get("includePinnedTabs", True)),
            include_bookmarks=bool(data.get("includeBookmarks", True)),
            include_favicons=bool(data.get("includeFavicons", True)),
            include_open_tabs=bool(data.get("includeOpenTabs", False)),
            include_history=bool(data.get("includeHistory", False)),
            include_cookies=bool(data.get("includeCookies", False)),
        )
