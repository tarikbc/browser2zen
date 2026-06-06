"""
Firefox extractor.

Firefox stores everything in SQLite under each profile dir:

- ``places.sqlite``  — bookmarks AND history (``moz_bookmarks`` /
  ``moz_places`` / ``moz_historyvisits`` tables),
- ``cookies.sqlite`` — cookies (``moz_cookies``),
- ``favicons.sqlite``— favicons.

Because this is the same schema Zen uses, a Firefox→Zen migration is
fundamentally a SQLite-to-SQLite merge — there is no Chromium-style
transform step. v1 of browser2zen ships **bookmarks only** for the
Firefox path; history and cookies need a dedicated Firefox→Firefox
places/cookies merger that is outside the scope of the existing
Chromium-format importers (which expect Chromium schema).

Profile detection follows the standard ``profiles.ini`` convention
documented at https://support.mozilla.org/kb/profiles-where-firefox-stores-user-data.
"""

from __future__ import annotations

import configparser
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Optional

from .base import (
    BrowserExtractor,
    BrowserExtractorError,
    ExportData,
    FolderRecord,
    SpaceRecord,
    TabRecord,
)

logger = logging.getLogger(__name__)


_NS_FOLDER = uuid.UUID("4e7b8d6a-9c33-4e2b-9fa3-1bd1d1ea2c41")
_NS_SPACE = uuid.UUID("0ad3a9d9-95c0-4de1-87c1-2c41a9a3b1aa")

# Firefox bookmark roots. Children of id=1 (the places container).
# We pull from menu, toolbar, unfiled, mobile — and skip ``tags``,
# which is a virtual hierarchy of tag→bookmark assignments.
_ROOT_IDS = {
    2: "Bookmarks Menu",
    3: "Bookmarks Toolbar",
    5: "Other Bookmarks",
    6: "Mobile Bookmarks",
}


def _firefox_profiles_roots() -> list[Path]:
    """Every plausible Firefox profiles root for the current OS.

    On Linux the profile location depends on how Firefox was packaged:
    a classic distro/apt install uses ``~/.mozilla/firefox``, but the
    Snap build (the default on Ubuntu 22.04+) lives under
    ``~/snap/firefox/common/.mozilla/firefox`` and the Flatpak build
    under ``~/.var/app/org.mozilla.firefox/.mozilla/firefox``. We probe
    all three so a Snap/Flatpak user isn't told Firefox is "not
    installed".
    """
    home = Path.home()
    if sys.platform == "darwin":
        return [home / "Library/Application Support/Firefox"]
    if os.name == "nt":
        return [home / "AppData/Roaming/Mozilla/Firefox"]
    return [
        home / ".mozilla/firefox",
        home / "snap/firefox/common/.mozilla/firefox",
        home / ".var/app/org.mozilla.firefox/.mozilla/firefox",
    ]


def _firefox_profiles_root() -> Path | None:
    """First candidate root that has a ``profiles.ini``, else the first
    candidate that merely exists, else the canonical-but-missing one."""
    candidates = _firefox_profiles_roots()
    for root in candidates:
        if (root / "profiles.ini").is_file():
            return root
    for root in candidates:
        if root.is_dir():
            return root
    return candidates[0] if candidates else None


class FirefoxExtractor(BrowserExtractor):
    name = "firefox"
    display_name = "Firefox"

    # ---------- detection ----------

    def is_installed(self) -> bool:
        root = _firefox_profiles_root()
        return bool(root and (root / "profiles.ini").is_file())

    def profile_paths(self) -> list[Path]:
        root = _firefox_profiles_root()
        if root is None or not root.is_dir():
            return []
        # Prefer profiles.ini when present so we honour the user's
        # Default profile selection. Fall back to a directory glob if
        # the file is missing or unparseable.
        ini = root / "profiles.ini"
        if ini.is_file():
            try:
                cp = configparser.ConfigParser()
                cp.read(ini, encoding="utf-8")
                paths: list[Path] = []
                for section in cp.sections():
                    if not section.startswith("Profile"):
                        continue
                    rel_or_abs = cp[section].get("Path", "")
                    is_relative = cp[section].get("IsRelative", "1") == "1"
                    p = (root / rel_or_abs) if is_relative else Path(rel_or_abs)
                    if (p / "places.sqlite").is_file():
                        paths.append(p)
                if paths:
                    return paths
            except Exception as exc:
                logger.warning("failed to parse %s: %s", ini, exc)
        # Fallback: any ``Profiles/<id>.<name>/`` dir with places.sqlite.
        out: list[Path] = []
        profiles_dir = root / "Profiles"
        if profiles_dir.is_dir():
            for entry in sorted(profiles_dir.iterdir()):
                if entry.is_dir() and (entry / "places.sqlite").is_file():
                    out.append(entry)
        return out

    def is_running(self) -> bool:
        if sys.platform == "darwin" or sys.platform == "linux":
            try:
                r = subprocess.run(
                    ["pgrep", "-f", "Firefox.app/Contents/MacOS/firefox"]
                    if sys.platform == "darwin"
                    else ["pgrep", "-x", "firefox"],
                    capture_output=True, text=True, timeout=2,
                )
                return r.returncode == 0 and bool(r.stdout.strip())
            except Exception:
                return False
        if os.name == "nt":
            try:
                r = subprocess.run(
                    ["tasklist", "/FI", "IMAGENAME eq firefox.exe"],
                    capture_output=True, text=True, timeout=2,
                )
                return "firefox.exe" in r.stdout.lower()
            except Exception:
                return False
        return False

    def quit(self) -> dict:
        started = time.time()
        if sys.platform == "darwin":
            try:
                subprocess.run(
                    ["osascript", "-e", 'tell application "Firefox" to quit'],
                    capture_output=True, timeout=3,
                )
            except Exception as exc:
                return {"ok": False, "running": True,
                        "elapsed": time.time() - started, "error": str(exc)}
        elif os.name == "nt":
            try:
                subprocess.run(["taskkill", "/im", "firefox.exe"],
                               capture_output=True, timeout=3)
            except Exception:
                pass
        else:
            return {"ok": False, "running": self.is_running(), "elapsed": 0.0,
                    "error": "graceful quit only supports macOS and Windows"}
        deadline = started + 6.0
        while time.time() < deadline:
            if not self.is_running():
                return {"ok": True, "running": False,
                        "elapsed": time.time() - started}
            time.sleep(0.25)
        return {"ok": False, "running": True,
                "elapsed": time.time() - started,
                "error": "browser did not quit within timeout"}

    # ---------- extraction ----------

    def extract(self) -> ExportData:
        profiles = self.profile_paths()
        if not profiles:
            # Distinguish "Firefox isn't here at all" from "Firefox is
            # installed but the profile was never opened, so there's no
            # places.sqlite yet" — the latter is a common point of
            # confusion when the Detect screen says Firefox is present.
            root = _firefox_profiles_root()
            if root is not None and (root / "profiles.ini").is_file():
                raise BrowserExtractorError(
                    "no_firefox_profile_data",
                    "Firefox is installed but its profile has no data yet "
                    "(places.sqlite is missing). Launch Firefox once so it "
                    "initialises the profile, then try again.",
                )
            raise BrowserExtractorError(
                "no_firefox_profiles",
                "Firefox has no profile directory on this machine.",
            )
        spaces: list[SpaceRecord] = []
        for profile in profiles:
            tabs, folders = self._read_bookmarks(profile)
            if not tabs and not folders:
                continue
            space_id = str(uuid.uuid5(_NS_SPACE, f"{self.name}:{profile.name}"))
            spaces.append(SpaceRecord(
                space_id=space_id,
                space_name=self._profile_label(profile),
                pinned_tabs=tabs,
                open_tabs=[],
                folders=folders,
            ))
        if not spaces:
            raise BrowserExtractorError(
                "no_firefox_bookmarks",
                "Firefox has no bookmarks to migrate.",
            )
        return ExportData(source=self.name, spaces=spaces)

    @staticmethod
    def _profile_label(profile: Path) -> str:
        # Firefox profile dirs are ``<id>.<name>``; the user thinks in
        # terms of <name>.
        name = profile.name
        if "." in name:
            name = name.split(".", 1)[1]
        return name or profile.name

    def _read_bookmarks(self, profile: Path) -> tuple[list[TabRecord], list[FolderRecord]]:
        places_db = profile / "places.sqlite"
        if not places_db.is_file():
            return [], []
        # Snapshot to avoid holding a read lock if Firefox happens to
        # touch the file mid-walk. Same trick the Chromium readers use.
        with tempfile.TemporaryDirectory(prefix="browser2zen_ff_") as td:
            snap = Path(td) / "places.sqlite"
            shutil.copy2(places_db, snap)
            for suffix in ("-wal", "-shm"):
                sib = places_db.with_name(places_db.name + suffix)
                if sib.exists():
                    shutil.copy2(sib, snap.with_name(snap.name + suffix))
            return self._walk_places_db(snap, profile.name)

    def _walk_places_db(self, db: Path, profile_dir_name: str) -> tuple[list[TabRecord], list[FolderRecord]]:
        space_id = str(uuid.uuid5(_NS_SPACE, f"{self.name}:{profile_dir_name}"))
        conn = sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True)
        try:
            conn.row_factory = sqlite3.Row
            # Pre-build parent → [children] for fast tree walk. Skip
            # separators (type=3) and the tags root subtree.
            children: dict[int, list[sqlite3.Row]] = {}
            for row in conn.execute(
                """SELECT b.id, b.parent, b.type, b.title, b.position, p.url
                   FROM moz_bookmarks b LEFT JOIN moz_places p ON b.fk = p.id
                   WHERE b.type IN (1, 2)
                   ORDER BY b.parent, b.position"""
            ):
                children.setdefault(row["parent"], []).append(row)

            tabs: list[TabRecord] = []
            folders: list[FolderRecord] = []
            # Each top-level root (menu/toolbar/unfiled/mobile) becomes
            # a wrapper folder so the user can tell them apart in Zen.
            for root_id, root_label in _ROOT_IDS.items():
                if root_id not in children:
                    continue
                # Skip the wrapper if it contains no descendants at all.
                if not _root_has_content(root_id, children):
                    continue
                wrapper_id = self._stable_folder_id(space_id, [root_label])
                folders.append(FolderRecord(
                    folder_id=wrapper_id,
                    title=root_label,
                    parent_id=None,
                    space_id=space_id,
                    children_ids=[],
                    index=len(folders),
                ))
                self._walk_children(
                    parent_db_id=root_id,
                    children=children,
                    folder_path=[root_label],
                    parent_record_id=wrapper_id,
                    space_id=space_id,
                    tabs_out=tabs,
                    folders_out=folders,
                )
            # Backfill children_ids on parent folders.
            by_id = {f.folder_id: f for f in folders}
            for f in folders:
                if f.parent_id and f.parent_id in by_id:
                    parent = by_id[f.parent_id]
                    if f.folder_id not in parent.children_ids:
                        parent.children_ids.append(f.folder_id)
            return tabs, folders
        finally:
            conn.close()

    def _walk_children(
        self,
        parent_db_id: int,
        children: dict[int, list[sqlite3.Row]],
        folder_path: list[str],
        parent_record_id: str | None,
        space_id: str,
        tabs_out: list[TabRecord],
        folders_out: list[FolderRecord],
    ) -> None:
        for row in children.get(parent_db_id, []):
            kind = row["type"]
            title = row["title"] or ""
            if kind == 1:
                url = row["url"] or ""
                if not url or not url.lower().startswith(("http://", "https://", "ftp://")):
                    continue
                tabs_out.append(TabRecord(
                    url=url,
                    title=title,
                    folder_path=list(folder_path),
                    folder_id=parent_record_id,
                    is_essential=False,
                ))
            elif kind == 2:
                child_path = folder_path + [title or "Untitled"]
                fid = self._stable_folder_id(space_id, child_path)
                folders_out.append(FolderRecord(
                    folder_id=fid,
                    title=title or "Untitled",
                    parent_id=parent_record_id,
                    space_id=space_id,
                    children_ids=[],
                    index=len(folders_out),
                ))
                self._walk_children(
                    parent_db_id=row["id"],
                    children=children,
                    folder_path=child_path,
                    parent_record_id=fid,
                    space_id=space_id,
                    tabs_out=tabs_out,
                    folders_out=folders_out,
                )

    @staticmethod
    def _stable_folder_id(space_id: str, folder_path: list[str]) -> str:
        return str(uuid.uuid5(_NS_FOLDER, space_id + "/" + "/".join(folder_path)))

    # ---------- history / cookies ----------
    #
    # Firefox shares Zen's schema, so the orchestrator dispatches by
    # source.name to the Firefox-flavoured importers in
    # ``src/firefox_history_importer.py`` and
    # ``src/firefox_cookies_importer.py``. We expose the source paths
    # here and skip the Chromium-style cookie_master_key entirely.

    def history_db_paths(self) -> list[Path]:
        return [p / "places.sqlite" for p in self.profile_paths()
                if (p / "places.sqlite").is_file()]

    def cookie_db_paths(self) -> list[Path]:
        return [p / "cookies.sqlite" for p in self.profile_paths()
                if (p / "cookies.sqlite").is_file()]

    def cookie_master_key(self) -> bytes:
        # Firefox cookies are unencrypted at rest; the importer dispatches
        # on source.name and never calls this. Kept as a defensive raise.
        raise BrowserExtractorError(
            "firefox_cookies_use_direct_path",
            "Firefox cookies should be imported via FirefoxCookiesImporter, "
            "not via the Chromium key-unwrap path.",
        )


def _root_has_content(root_id: int, children: dict) -> bool:
    """Cheap recursive check: at least one URL bookmark anywhere under root."""
    stack = [root_id]
    while stack:
        cur = stack.pop()
        for row in children.get(cur, []):
            if row["type"] == 1:
                return True
            stack.append(row["id"])
    return False
