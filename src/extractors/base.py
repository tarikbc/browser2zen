"""
BrowserExtractor abstract base class and the ExportData payload it
produces.

Every supported source browser (Arc, Chrome, Edge, Brave, Firefox,
Safari) is represented by a concrete subclass that knows how to:

- locate the browser's profile directories on disk,
- detect whether the browser is installed and whether it is currently
  running,
- quit the browser gracefully,
- read the browser's bookmarks/spaces/folders into the unified
  :class:`ExportData` shape,
- expose the file-system paths the generic Chromium readers
  (``chromium_history_importer``, ``zen_favicon_importer``,
  ``chromium_cookies_importer``) need,
- unwrap the cookie master key (Keychain on macOS, DPAPI on Windows for
  Chromium browsers; unencrypted on Firefox; ``Cookies.binarycookies``
  on Safari).

The Zen-side writers (``zen_space_importer``, ``zen_sessions_importer``,
``zen_bookmark_importer``, ``zen_workspace_importer``) consume only
``ExportData`` — they neither know nor care which browser the data
originated from.
"""

from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------- paths


def xdg_config_home() -> Path:
    """Resolve ``$XDG_CONFIG_HOME``, defaulting to ``~/.config``.

    Both Firefox and Chromium read their Linux profile root from this
    variable, so XDG-aware builds (common on Arch) end up outside the
    classic ``~/.config`` when the user sets it. ``app/env_check.py``
    keeps its own copy on purpose: it is deliberately importable without
    ``src`` on ``sys.path``.
    """
    env = os.environ.get("XDG_CONFIG_HOME")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".config"


# --------------------------------------------------------------------- payload


@dataclass
class TabRecord:
    """A single bookmark / pinned tab / open tab. Used for both pinned and
    unpinned entries; the calling code distinguishes them by the list the
    record lives in (``ArcSpace.pinned_tabs`` vs ``open_tabs``)."""
    url: str
    title: str = ""
    folder_path: list[str] = field(default_factory=list)
    folder_id: str | None = None
    is_essential: bool = False              # only Arc has Essentials
    favicon_data: bytes | None = None     # only Safari embeds; otherwise loaded later

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "title": self.title,
            "folder_path": list(self.folder_path),
            "folder_id": self.folder_id,
            "is_essential": self.is_essential,
        }


@dataclass
class FolderRecord:
    folder_id: str
    title: str
    parent_id: str | None = None
    space_id: str = ""
    children_ids: list[str] = field(default_factory=list)
    index: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "folder_id": self.folder_id,
            "title": self.title,
            "parent_id": self.parent_id,
            "space_id": self.space_id,
            "children_ids": list(self.children_ids),
            "index": self.index,
        }


@dataclass
class SpaceRecord:
    """One workspace's worth of tabs and folders.

    Source browsers without the concept of a "space" (everyone except Arc)
    synthesise a single default space and put everything under it. Arc
    emits one SpaceRecord per Arc Space.
    """
    space_id: str
    space_name: str
    pinned_tabs: list[TabRecord] = field(default_factory=list)
    open_tabs: list[TabRecord] = field(default_factory=list)
    folders: list[FolderRecord] = field(default_factory=list)
    icon: str | None = None              # emoji, single-char string
    color: dict[str, float] | None = None  # {r,g,b} floats in 0..1
    # Bookmark-backup channel, kept separate from ``pinned_tabs`` so a
    # source's real tab-strip pinned tabs and its bookmarks no longer share
    # a list. ``None`` means "this source doesn't distinguish them" — the
    # legacy dict then falls these back to pinned_tabs/folders so
    # bookmark-only sources (Arc/Firefox/Safari) behave exactly as before.
    bookmarks: list[TabRecord] | None = None
    bookmark_folders: list[FolderRecord] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "space_id": self.space_id,
            "space_name": self.space_name,
            "pinned_tabs": [t.to_dict() for t in self.pinned_tabs],
            "open_tabs": [t.to_dict() for t in self.open_tabs],
            "folders": [f.to_dict() for f in self.folders],
            "icon": self.icon,
            "color": self.color,
        }


@dataclass
class ExportData:
    """Unified intermediate representation handed to the Zen-side writers."""
    source: str                  # "arc", "chrome", "edge", "brave", "firefox", "safari"
    spaces: list[SpaceRecord] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "spaces": [s.to_dict() for s in self.spaces],
        }

    def to_legacy_dict(self) -> dict[str, Any]:
        """Emit the dict shape the Zen-side writers were originally
        designed against (the legacy ``arc_pinned_tabs_export.json``).

        The writers (``ZenSpaceImporter``, ``ZenSessionsImporter``,
        ``ZenBookmarkImporter``) iterate this shape directly. Keeping
        them stable means we don't have to refactor them when adding
        new sources — every extractor lowers to this single shape.
        """
        spaces_out: list[dict[str, Any]] = []
        for s in self.spaces:
            spaces_out.append({
                "space_id": s.space_id,
                "space_name": s.space_name,
                "icon": s.icon,
                "color": s.color,
                "total_pinned_tabs": len(s.pinned_tabs),
                "total_open_tabs": len(s.open_tabs),
                "total_folders": len(s.folders),
                "pinned_tabs": [
                    {
                        "url": t.url,
                        "title": t.title,
                        "space_id": s.space_id,
                        "space_name": s.space_name,
                        "folder_path": list(t.folder_path),
                        "tab_id": "",
                        "parent_id": t.folder_id or "",
                        "index": idx,
                        "is_essential": t.is_essential,
                    }
                    for idx, t in enumerate(s.pinned_tabs)
                ],
                "open_tabs": [
                    {
                        "url": t.url,
                        "title": t.title,
                        "space_id": s.space_id,
                        "space_name": s.space_name,
                        "tab_id": "",
                        "index": idx,
                    }
                    for idx, t in enumerate(s.open_tabs)
                ],
                "folders": [
                    {
                        "folder_id": f.folder_id,
                        "title": f.title,
                        "parent_id": f.parent_id or "",
                        "space_id": f.space_id or s.space_id,
                        "children_ids": list(f.children_ids),
                        "index": f.index,
                    }
                    for f in s.folders
                ],
                "bookmarks": [
                    {
                        "url": t.url,
                        "title": t.title,
                        "space_id": s.space_id,
                        "space_name": s.space_name,
                        "folder_path": list(t.folder_path),
                        "tab_id": "",
                        "parent_id": t.folder_id or "",
                        "index": idx,
                        "is_essential": t.is_essential,
                    }
                    for idx, t in enumerate(
                        s.bookmarks if s.bookmarks is not None else s.pinned_tabs
                    )
                ],
                "bookmark_folders": [
                    {
                        "folder_id": f.folder_id,
                        "title": f.title,
                        "parent_id": f.parent_id or "",
                        "space_id": f.space_id or s.space_id,
                        "children_ids": list(f.children_ids),
                        "index": f.index,
                    }
                    for f in (s.bookmark_folders
                              if s.bookmark_folders is not None else s.folders)
                ],
            })
        return {
            "source": self.source,
            "total_spaces": len(self.spaces),
            "spaces": spaces_out,
        }


# --------------------------------------------------------------------- error


class BrowserExtractorError(RuntimeError):
    """Raised by an extractor when the source browser cannot be read.

    ``code`` is a stable string the GUI maps to a friendly message;
    ``message`` is the human-readable explanation.
    """

    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


# --------------------------------------------------------------------- ABC


class BrowserExtractor(ABC):
    """Abstract base for every source-browser adapter."""

    name: str = ""               # short, lowercase identifier ("arc", "chrome", ...)
    display_name: str = ""       # user-facing label ("Arc", "Chrome", ...)

    # ---- environment detection ----

    @abstractmethod
    def is_installed(self) -> bool:
        """Return True if the browser is installed (data on disk exists)."""

    @abstractmethod
    def profile_paths(self) -> list[Path]:
        """All per-user profile directories that contain migrate-able data."""

    def is_running(self) -> bool:
        """Default: false. Concrete subclasses should detect process state."""
        return False

    def quit(self) -> dict:
        """Best-effort graceful quit. Returns the same shape as
        ``app/browser_control.py:quit_browser`` — ``{"ok": bool, ...}``.
        Default: no-op success."""
        return {"ok": True, "running": False, "elapsed": 0.0}

    # ---- data extraction ----

    @abstractmethod
    def extract(self) -> ExportData:
        """Read the browser's bookmarks/spaces/folders and emit ExportData."""

    # ---- chromium-style data paths used by generic readers ----

    def history_db_paths(self) -> list[Path]:
        """SQLite ``History`` files (Chromium-format). Empty for Firefox/Safari
        (those use a different reader)."""
        return []

    def favicon_db_paths(self) -> list[Path]:
        """SQLite ``Favicons`` files (Chromium-format)."""
        return []

    def cookie_db_paths(self) -> list[Path]:
        """Cookie SQLite files. Schema differs per browser; the cookie
        importer dispatches based on extractor type."""
        return []

    def local_state_paths(self) -> list[Path]:
        """Chromium ``Local State`` JSON files (used for DPAPI key unwrap
        on Windows). Empty for Firefox/Safari and unused on macOS."""
        return []

    # ---- cookie decryption ----

    def cookie_master_key(self) -> bytes:
        """Return the symmetric key used to decrypt the cookie ``encrypted_value``
        column. Chromium subclasses pull this from the OS keystore.
        Firefox returns an empty key (cookies are usually unencrypted).
        Safari raises (handled separately by the binary cookies parser).

        Raises :class:`BrowserExtractorError` on failure with a code the
        GUI surfaces."""
        raise BrowserExtractorError(
            "cookies_unsupported",
            f"{self.display_name} cookie decryption is not implemented.",
        )
