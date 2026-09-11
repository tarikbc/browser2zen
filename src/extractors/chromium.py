"""
Shared base for Chromium-family browsers (Chrome, Edge, Brave).

What's common across the family:
- profiles live under a single "User Data" root,
- bookmarks are stored as a JSON tree in ``<profile>/Bookmarks``,
- history / favicons / cookies are SQLite (Chromium schema),
- the cookie master key is wrapped by either macOS Keychain (under a
  per-browser service name) or Windows DPAPI (per-browser ``Local State``).

What differs per subclass:
- the user-data dir locations,
- the Keychain service name and DPAPI ``Local State`` path,
- the process names used by ``is_running`` / ``quit``,
- the user-facing display name.

Subclasses set those as class attributes; everything else is generic.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
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
    xdg_config_home,
)

logger = logging.getLogger(__name__)


# Synthetic-id namespaces. Folder/space ids in Arc are stable UUIDs; we
# need our synthetic Chromium ids to be stable across runs too so the
# Zen-side importers (which dedupe by id) don't double-create things.
_NS_FOLDER = uuid.UUID("4e7b8d6a-9c33-4e2b-9fa3-1bd1d1ea2c41")
_NS_SPACE = uuid.UUID("0ad3a9d9-95c0-4de1-87c1-2c41a9a3b1aa")


def _root_has_profile(root: Path) -> bool:
    """A Chromium User Data root is live if it holds a profile dir with a
    ``Bookmarks`` file. An empty leftover install dir (e.g. an abandoned
    ``Brave-Browser`` next to a used ``Brave-Origin``) must not shadow the
    real one.

    This predicate has to stay in sync with ``is_installed`` — pick a root
    that ``is_installed`` then rejects and the browser reports "not found"
    anyway. In particular ``Local State`` alone is NOT enough: an install
    that was launched once and then abandoned has one, and would shadow
    the root the user actually uses.
    """
    try:
        return any(
            entry.is_dir() and (entry / "Bookmarks").is_file()
            for entry in root.iterdir()
        )
    except OSError:
        return False


class ChromiumExtractor(BrowserExtractor):
    """Subclass and set the four class attributes below."""

    # Identity ------------------------------------------------------------
    name: str = ""
    display_name: str = ""

    # Filesystem ----------------------------------------------------------
    # Candidate User-Data directories per platform, expressed as paths
    # *relative to Path.home()*. The base class prepends ``Path.home()``
    # at lookup time so tests can monkeypatch it.
    user_data_dirs_macos: tuple[str, ...] = ()
    user_data_dirs_windows: tuple[str, ...] = ()
    user_data_dirs_linux: tuple[str, ...] = ()

    # Cookie key ---------------------------------------------------------
    keychain_service: str = ""        # e.g. "Chrome Safe Storage"
    keychain_account: str = ""        # e.g. "Chrome"

    # Process detection / quit -------------------------------------------
    macos_app_name: str = ""          # for ``tell application "X" to quit``
    macos_process_paths: tuple[str, ...] = ()  # for pgrep -f
    windows_process_names: tuple[str, ...] = ()  # for taskkill /im

    # ---------- detection ----------

    def _linux_user_data_paths(self, home: Path) -> list[Path]:
        """Linux candidates, resolved against ``$XDG_CONFIG_HOME``.

        Chromium reads its User Data root from ``$XDG_CONFIG_HOME``
        (default ``~/.config``), so a ``.config/...`` candidate has to
        follow the variable when the user sets it. Snap and Flatpak
        candidates carry their own container prefix and stay relative to
        ``$HOME``. The XDG location comes first because that is the one
        Chromium itself would use.
        """
        xdg = xdg_config_home()
        out: list[Path] = []
        for rel in self.user_data_dirs_linux:
            if rel.startswith(".config/"):
                out.append(xdg / rel[len(".config/"):])
            out.append(home / rel)
        # Dedupe, preserving order. ``xdg`` equals ``~/.config`` whenever
        # the variable is unset, which is the common case.
        seen: set[Path] = set()
        return [p for p in out if not (p in seen or seen.add(p))]

    def _user_data_dir(self) -> Path | None:
        home = Path.home()
        if sys.platform == "darwin":
            paths = [home / rel for rel in self.user_data_dirs_macos]
        elif os.name == "nt":
            paths = [home / rel for rel in self.user_data_dirs_windows]
        else:
            paths = self._linux_user_data_paths(home)
        existing = [p for p in paths if p.is_dir()]
        # Prefer the first root that actually holds profile data, so a
        # leftover empty install dir can't shadow the one the user really
        # uses. Fall back to the first existing root, then to None.
        for root in existing:
            if _root_has_profile(root):
                return root
        return existing[0] if existing else None

    def is_installed(self) -> bool:
        root = self._user_data_dir()
        if root is None:
            return False
        # Need at least one profile dir with a Bookmarks file.
        return any((p / "Bookmarks").is_file() for p in root.iterdir() if p.is_dir())

    def profile_paths(self) -> list[Path]:
        root = self._user_data_dir()
        if root is None:
            return []
        out: list[Path] = []
        for entry in sorted(root.iterdir()):
            if not entry.is_dir():
                continue
            if (entry / "Bookmarks").is_file() or (entry / "History").is_file():
                out.append(entry)
        return out

    def is_running(self) -> bool:
        if sys.platform == "darwin":
            for path in self.macos_process_paths:
                try:
                    r = subprocess.run(
                        ["pgrep", "-f", path],
                        capture_output=True, text=True, timeout=2,
                    )
                    if r.returncode == 0 and r.stdout.strip():
                        return True
                except Exception:
                    continue
            return False
        if os.name == "nt":
            for image in self.windows_process_names:
                try:
                    r = subprocess.run(
                        ["tasklist", "/FI", f"IMAGENAME eq {image}"],
                        capture_output=True, text=True, timeout=2,
                    )
                    if image.lower() in r.stdout.lower():
                        return True
                except Exception:
                    continue
            return False
        return False

    def quit(self) -> dict:
        started = time.time()
        if sys.platform == "darwin" and self.macos_app_name:
            try:
                subprocess.run(
                    ["osascript", "-e",
                     f'tell application "{self.macos_app_name}" to quit'],
                    capture_output=True, timeout=3,
                )
            except Exception as exc:
                # A failed Apple event shouldn't leave the browser
                # running — fall through to the forced-kill escalation
                # below rather than bailing out here.
                logger.warning("osascript quit for %s failed: %s",
                               self.macos_app_name, exc)
        elif os.name == "nt":
            for image in self.windows_process_names:
                try:
                    subprocess.run(["taskkill", "/im", image],
                                   capture_output=True, timeout=3)
                except Exception:
                    continue
        else:
            return {"ok": False, "running": self.is_running(),
                    "elapsed": 0.0,
                    "error": "graceful quit only supports macOS and Windows"}

        deadline = started + 6.0
        while time.time() < deadline:
            if not self.is_running():
                return {"ok": True, "running": False,
                        "elapsed": time.time() - started}
            time.sleep(0.25)

        # Graceful quit timed out. Chromium browsers routinely ignore a
        # polite quit — utility/GPU child processes have no window to
        # receive WM_CLOSE on Windows, and "keep running in the
        # background" leaves a process alive on macOS. Escalate to a
        # forced kill, mirroring the Arc/Zen path in
        # ``app/browser_control.py`` so the user isn't stuck on
        # "browser is still running".
        if self._force_kill() and not self.is_running():
            return {"ok": True, "running": False, "forced": True,
                    "elapsed": time.time() - started}

        return {"ok": False, "running": True,
                "elapsed": time.time() - started,
                "error": "browser did not quit even after a forced kill"}

    def _force_kill(self) -> bool:
        """Best-effort hard kill of every browser process. Returns True if
        a kill command was issued (not whether the process actually died —
        the caller re-checks ``is_running``)."""
        issued = False
        if sys.platform == "darwin":
            for path in self.macos_process_paths:
                for sig in ("-TERM", "-KILL"):
                    try:
                        subprocess.run(["pkill", sig, "-f", path],
                                       capture_output=True, timeout=3)
                        issued = True
                    except Exception:
                        continue
                    time.sleep(0.5)
                    if not self.is_running():
                        return True
        elif os.name == "nt":
            for image in self.windows_process_names:
                try:
                    subprocess.run(["taskkill", "/f", "/im", image],
                                   capture_output=True, timeout=3)
                    issued = True
                except Exception:
                    continue
            time.sleep(0.5)
        return issued

    # ---------- chromium-style data paths ----------

    def history_db_paths(self) -> list[Path]:
        return [p / "History" for p in self.profile_paths()
                if (p / "History").is_file()]

    def favicon_db_paths(self) -> list[Path]:
        return [p / "Favicons" for p in self.profile_paths()
                if (p / "Favicons").is_file()]

    def cookie_db_paths(self) -> list[Path]:
        out: list[Path] = []
        for prof in self.profile_paths():
            # New Chromium nests cookies under Network/; older builds
            # keep them at the profile root.
            for cand in (prof / "Network" / "Cookies", prof / "Cookies"):
                if cand.is_file():
                    out.append(cand)
                    break
        return out

    def local_state_paths(self) -> list[Path]:
        root = self._user_data_dir()
        if root is None:
            return []
        return [root / "Local State"] if (root / "Local State").is_file() else []

    # ---------- cookie key ----------

    def cookie_master_key(self) -> bytes:
        if sys.platform == "darwin":
            from chromium_cookies_importer import (
                _derive_aes_key_macos,
                _read_keychain_password,
            )
            password = _read_keychain_password(
                service=self.keychain_service,
                account=self.keychain_account,
            )
            if password is None:
                raise BrowserExtractorError(
                    "keychain_denied",
                    f"macOS Keychain access for {self.keychain_service!r} was denied.",
                )
            return _derive_aes_key_macos(password)

        if os.name == "nt":
            from chromium_cookies_importer import (
                _DpapiError,
                _read_local_state_key_windows,
            )
            try:
                return _read_local_state_key_windows(self.local_state_paths())
            except _DpapiError as exc:
                raise BrowserExtractorError(exc.code, str(exc)) from exc

        raise BrowserExtractorError(
            "unsupported_platform",
            "Cookie decryption only supports macOS and Windows.",
        )

    # ---------- profile labels ----------

    def _profile_display_name(self, profile_dir: Path) -> str:
        """Pull the user-set name from Local State, falling back to the
        directory name."""
        try:
            ls = self._user_data_dir()
            if ls is None:
                return profile_dir.name
            data = json.loads((ls / "Local State").read_text(encoding="utf-8"))
            info = data.get("profile", {}).get("info_cache", {}).get(profile_dir.name, {})
            return info.get("name") or profile_dir.name
        except Exception:
            return profile_dir.name

    # ---------- bookmark JSON tree → records ----------

    def _parse_bookmarks(
        self,
        profile: Path,
        space_id: str,
    ) -> tuple[list[TabRecord], list[FolderRecord]]:
        bookmarks_file = profile / "Bookmarks"
        if not bookmarks_file.is_file():
            return [], []
        try:
            data = json.loads(bookmarks_file.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("failed to parse %s: %s", bookmarks_file, exc)
            return [], []

        tabs: list[TabRecord] = []
        folders: list[FolderRecord] = []
        # ``roots`` always contains bookmark_bar / other / synced. Walk
        # each as a top-level folder anchored to the synthetic space.
        roots = data.get("roots", {}) or {}
        # Hide the top-level wrapper folders (the user thinks of "Other
        # Bookmarks" as a place, not as a folder); their children become
        # children of the space root, except when there is meaningful
        # content under each (e.g. user-named bookmark_bar entries
        # alongside a populated 'other'). Keep the wrapper if it's
        # non-empty in more than one root.
        roots_kept = {
            key: roots[key] for key in ("bookmark_bar", "other", "synced")
            if key in roots and (roots[key].get("children") or [])
        }
        show_wrappers = len(roots_kept) > 1

        for root_key, root_node in roots_kept.items():
            wrapper_name = {
                "bookmark_bar": "Bookmarks Bar",
                "other": "Other Bookmarks",
                "synced": "Synced Bookmarks",
            }.get(root_key, root_key)

            if show_wrappers:
                wrapper_id = self._stable_folder_id(space_id, [wrapper_name])
                folders.append(FolderRecord(
                    folder_id=wrapper_id,
                    title=wrapper_name,
                    parent_id=None,
                    space_id=space_id,
                    children_ids=[],   # patched up after walk
                    index=len(folders),
                ))
                start_path = [wrapper_name]
                start_parent = wrapper_id
            else:
                start_path = []
                start_parent = None

            self._walk_node(
                node=root_node,
                space_id=space_id,
                folder_path=start_path,
                parent_id=start_parent,
                tabs_out=tabs,
                folders_out=folders,
            )

        # Backfill children_ids for the synthetic wrappers (and any other
        # folder that ended up with an empty list because we appended
        # children after the parent record was created).
        by_id = {f.folder_id: f for f in folders}
        for f in folders:
            if f.parent_id and f.parent_id in by_id:
                parent = by_id[f.parent_id]
                if f.folder_id not in parent.children_ids:
                    parent.children_ids.append(f.folder_id)

        return tabs, folders

    def _walk_node(
        self,
        node: dict,
        space_id: str,
        folder_path: list[str],
        parent_id: str | None,
        tabs_out: list[TabRecord],
        folders_out: list[FolderRecord],
    ) -> None:
        for child in (node.get("children") or []):
            ctype = child.get("type")
            cname = child.get("name") or ""
            if ctype == "url":
                url = child.get("url") or ""
                if not url or not url.lower().startswith(("http://", "https://", "ftp://")):
                    continue
                tabs_out.append(TabRecord(
                    url=url,
                    title=cname,
                    folder_path=list(folder_path),
                    folder_id=parent_id,
                    is_essential=False,
                ))
            elif ctype == "folder":
                child_path = folder_path + [cname]
                fid = self._stable_folder_id(space_id, child_path)
                folders_out.append(FolderRecord(
                    folder_id=fid,
                    title=cname,
                    parent_id=parent_id,
                    space_id=space_id,
                    children_ids=[],
                    index=len(folders_out),
                ))
                self._walk_node(
                    node=child,
                    space_id=space_id,
                    folder_path=child_path,
                    parent_id=fid,
                    tabs_out=tabs_out,
                    folders_out=folders_out,
                )

    @staticmethod
    def _stable_folder_id(space_id: str, folder_path: list[str]) -> str:
        return str(uuid.uuid5(_NS_FOLDER, space_id + "/" + "/".join(folder_path)))

    @classmethod
    def _stable_space_id(cls, profile_dir_name: str) -> str:
        return str(uuid.uuid5(_NS_SPACE, f"{cls.name}:{profile_dir_name}"))

    # ---------- extraction ----------

    def _read_session_tabs(
        self, profile: Path,
    ) -> tuple[list[TabRecord], list[TabRecord]]:
        """Recover the profile's live tab strip from Chromium's SNSS session
        store, split into (pinned, open). Best-effort: a missing or
        unreadable session yields ``([], [])`` so a profile with only
        bookmarks still migrates.
        """
        from ._snss import find_session_file, read_session_tabs

        session = find_session_file(profile)
        if session is None:
            return [], []
        try:
            tabs = read_session_tabs(session)
        except Exception as exc:
            logger.warning("could not read session store for %s: %s", profile, exc)
            return [], []

        pinned: list[TabRecord] = []
        opened: list[TabRecord] = []
        for t in tabs:
            rec = TabRecord(url=t.url, title=t.title, is_essential=False)
            (pinned if t.pinned else opened).append(rec)
        return pinned, opened

    def extract(self) -> ExportData:
        profiles = self.profile_paths()
        if not profiles:
            raise BrowserExtractorError(
                f"no_{self.name}_data",
                f"{self.display_name} has no data on this machine.",
            )

        spaces: list[SpaceRecord] = []
        for profile in profiles:
            space_id = self._stable_space_id(profile.name)
            # Bookmarks → the dedicated bookmark channel (Zen bookmarks).
            # Real tab-strip pinned/open tabs → the sidebar channels.
            # These are distinct concepts in every Chromium browser, so we
            # no longer conflate bookmarks into the pinned sidebar.
            bookmarks, bookmark_folders = self._parse_bookmarks(profile, space_id)
            pinned_tabs, open_tabs = self._read_session_tabs(profile)
            if not (bookmarks or bookmark_folders or pinned_tabs or open_tabs):
                continue
            spaces.append(SpaceRecord(
                space_id=space_id,
                space_name=self._profile_display_name(profile),
                pinned_tabs=pinned_tabs,
                open_tabs=open_tabs,
                folders=[],
                bookmarks=bookmarks,
                bookmark_folders=bookmark_folders,
            ))
        if not spaces:
            raise BrowserExtractorError(
                f"no_{self.name}_data",
                f"{self.display_name} has no tabs or bookmarks to migrate.",
            )
        return ExportData(source=self.name, spaces=spaces)
