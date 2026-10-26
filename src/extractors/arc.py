"""
Arc browser extractor.

Wraps the existing :class:`ArcPinnedTabExtractor` (which parses Arc's
unique ``StorableSidebar.json`` format) and re-shapes its output into
the unified :class:`ExportData`. The wrapped extractor stays unchanged
so the substantial parsing logic that already understands Arc Spaces,
Essential tabs, the ``childrenIds`` ordering trick, etc., keeps working
as-is.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Optional

from arc_pinned_tab_extractor import ArcPinnedTabExtractor

from .base import (
    BrowserExtractor,
    BrowserExtractorError,
    ExportData,
    FolderRecord,
    SpaceRecord,
    TabRecord,
)

logger = logging.getLogger(__name__)


# Paths are kept as strings *relative to Path.home()* so tests can
# monkeypatch Path.home() without the values being baked in at import.
_ARC_USER_DATA_RELS = (
    "Library/Application Support/Arc/User Data",
    "AppData/Local/Packages/TheBrowserCompany.Arc_ttt1ap7aakyb4/LocalCache/Local/Arc/User Data",
    "AppData/Local/Arc/User Data",
)
_ARC_STORABLE_SIDEBAR_RELS = (
    "Library/Application Support/Arc/StorableSidebar.json",
    "AppData/Local/Packages/TheBrowserCompany.Arc_ttt1ap7aakyb4/LocalCache/Local/Arc/StorableSidebar.json",
    "AppData/Local/Arc/StorableSidebar.json",
)


def _first_existing(paths) -> Path | None:
    for p in paths:
        if p.exists():
            return p
    return None


def _arc_user_data_paths() -> tuple[Path, ...]:
    home = Path.home()
    return tuple(home / rel for rel in _ARC_USER_DATA_RELS)


def _arc_storable_sidebar_paths() -> tuple[Path, ...]:
    home = Path.home()
    return tuple(home / rel for rel in _ARC_STORABLE_SIDEBAR_RELS)


class ArcExtractor(BrowserExtractor):
    name = "arc"
    display_name = "Arc"

    # ---- detection ------------------------------------------------------

    def is_installed(self) -> bool:
        return _first_existing(_arc_storable_sidebar_paths()) is not None

    def profile_paths(self) -> list[Path]:
        root = _first_existing(_arc_user_data_paths())
        if root is None:
            return []
        return [
            entry for entry in sorted(root.iterdir())
            if entry.is_dir()
            and ((entry / "History").is_file()
                 or (entry / "Network" / "Cookies").is_file())
        ]

    def is_running(self) -> bool:
        # Reuse the existing detection helper from app.env_check; importing
        # lazily avoids a circular dependency when this module is loaded
        # without a GUI.
        try:
            from app.env_check import is_arc_running  # type: ignore[import-not-found]
            return is_arc_running()
        except Exception:
            return False

    def quit(self) -> dict:
        try:
            from app.browser_control import quit_browser  # type: ignore[import-not-found]
            return quit_browser("arc")
        except Exception as exc:
            return {"ok": False, "running": True, "elapsed": 0.0, "error": str(exc)}

    # ---- extraction -----------------------------------------------------

    def extract(self) -> ExportData:
        ext = ArcPinnedTabExtractor()
        spaces = ext.extract_pinned_tabs()
        if not spaces:
            raise BrowserExtractorError(
                "no_arc_data",
                "Arc has no pinned-tab data on this machine.",
            )

        out_spaces: list[SpaceRecord] = []
        for s in spaces:
            tabs = [self._tab_to_record(t) for t in (s.pinned_tabs or [])]
            opens = [self._tab_to_record(t) for t in (s.open_tabs or [])]
            folders = [self._folder_to_record(f, s.space_id) for f in (s.folders or [])]
            out_spaces.append(SpaceRecord(
                space_id=s.space_id,
                space_name=s.space_name,
                pinned_tabs=tabs,
                open_tabs=opens,
                folders=folders,
                icon=s.icon,
                color=s.color,
            ))
        return ExportData(source=self.name, spaces=out_spaces)

    @staticmethod
    def _tab_to_record(arc_tab) -> TabRecord:
        return TabRecord(
            url=getattr(arc_tab, "url", "") or "",
            title=getattr(arc_tab, "title", "") or "",
            folder_path=list(getattr(arc_tab, "folder_path", []) or []),
            folder_id=getattr(arc_tab, "folder_id", None),
            is_essential=bool(getattr(arc_tab, "is_essential", False)),
        )

    @staticmethod
    def _folder_to_record(arc_folder, space_id: str) -> FolderRecord:
        return FolderRecord(
            folder_id=getattr(arc_folder, "folder_id", "") or "",
            title=getattr(arc_folder, "title", "") or "",
            parent_id=getattr(arc_folder, "parent_id", None),
            space_id=getattr(arc_folder, "space_id", space_id) or space_id,
            children_ids=list(getattr(arc_folder, "children_ids", []) or []),
            index=int(getattr(arc_folder, "index", 0) or 0),
        )

    # ---- chromium-style data paths --------------------------------------

    def _user_data_dir(self) -> Path | None:
        return _first_existing(_arc_user_data_paths())

    def history_db_paths(self) -> list[Path]:
        root = self._user_data_dir()
        return sorted(p for p in (root.glob("*/History") if root else [])
                      if p.is_file())

    def favicon_db_paths(self) -> list[Path]:
        root = self._user_data_dir()
        return sorted(p for p in (root.glob("*/Favicons") if root else [])
                      if p.is_file())

    def cookie_db_paths(self) -> list[Path]:
        root = self._user_data_dir()
        if root is None:
            return []
        out: list[Path] = []
        for profile in sorted(root.iterdir()):
            if not profile.is_dir():
                continue
            for candidate in (profile / "Network" / "Cookies", profile / "Cookies"):
                if candidate.is_file():
                    out.append(candidate)
                    break
        return out

    def login_data_paths(self) -> list[Path]:
        root = self._user_data_dir()
        if root is None:
            return []
        return [profile / "Login Data" for profile in sorted(root.iterdir())
                if profile.is_dir() and (profile / "Login Data").is_file()]

    def user_data_root(self) -> Path | None:
        return self._user_data_dir()

    def local_state_paths(self) -> list[Path]:
        # Used on Windows for DPAPI key unwrap.
        home = Path.home()
        candidates = (
            home / "AppData/Local/Packages/TheBrowserCompany.Arc_ttt1ap7aakyb4"
                 / "LocalCache/Local/Arc/User Data/Local State",
            home / "AppData/Local/Arc/User Data/Local State",
        )
        return [p for p in candidates if p.is_file()]

    # ---- cookie key -----------------------------------------------------

    def cookie_master_key(self) -> bytes:
        # Reuse the existing chromium_cookies_importer helpers. They already
        # know how to talk to the Keychain on macOS and DPAPI on Windows;
        # they just hard-code the service/account name to "Arc Safe Storage"
        # / "Arc". When the refactor introduces ChromiumCookiesImporter
        # this method moves into chromium.py and the service name comes
        # from the subclass.
        if sys.platform == "darwin":
            from chromium_cookies_importer import _derive_aes_key_macos, _read_keychain_password
            password = _read_keychain_password()
            if password is None:
                raise BrowserExtractorError(
                    "keychain_denied",
                    "macOS Keychain access for 'Arc Safe Storage' was denied.",
                )
            return _derive_aes_key_macos(password)

        if os.name == "nt":
            from chromium_cookies_importer import _DpapiError, _read_local_state_key_windows
            try:
                return _read_local_state_key_windows()
            except _DpapiError as exc:
                raise BrowserExtractorError(exc.code, str(exc)) from exc

        raise BrowserExtractorError(
            "unsupported_platform",
            "Cookie decryption only supports macOS and Windows.",
        )
