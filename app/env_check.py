"""
Environment detection for the Detect screen.

We deliberately do NOT import ``src.arc_profile_discovery`` because that file
contains a syntax error (mixed tabs/spaces) in the current upstream tree.
Instead we glob the well-known Arc and Zen paths directly. The logic mirrors
the working subset that the CLI tool already relies on via
``arc_pinned_tab_extractor`` (which reads ``StorableSidebar.json`` directly).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class ZenProfile:
    name: str
    path: Path
    is_release: bool          # cheap heuristic: name contains "release"
    has_zen_sessions: bool    # zen-sessions.jsonlz4 exists (modern format)
    install: str = ""         # "", "XDG" or "Flatpak" — qualifies the picker


@dataclass(frozen=True)
class EnvReport:
    # Source-browser fields describe whichever source is currently
    # selected (Arc by default). They were called ``arc_*`` historically.
    source_installed: bool
    source_data_path: Path | None
    source_profiles: list[str]            # subdirectory names with migrate-able data
    source_running: bool
    # Arc-only — not populated for other sources.
    arc_storable_sidebar: Path | None
    zen_installed: bool
    zen_profiles: list[ZenProfile]
    zen_running: bool
    has_lz4: bool
    has_cryptography: bool
    previous_migration_detected: bool
    errors: list[str] = field(default_factory=list)


# ---------- Arc ----------


def _arc_user_data_dirs() -> list[Path]:
    """Return every plausible Arc User Data root for the current OS.

    Windows users may have either the UWP package or the standalone-installer
    layout, occasionally both. macOS only uses one canonical location.
    """
    home = Path.home()
    if sys.platform == "darwin":
        return [home / "Library/Application Support/Arc/User Data"]
    if os.name == "nt":
        return [
            home / "AppData/Local/Packages/TheBrowserCompany.Arc_ttt1ap7aakyb4"
                 / "LocalCache/Local/Arc/User Data",
            home / "AppData/Local/Arc/User Data",
        ]
    return [home / ".config/Arc/User Data"]


def _arc_user_data_dir() -> Path | None:
    """First existing Arc User Data dir, or the canonical-but-missing one."""
    candidates = _arc_user_data_dirs()
    for c in candidates:
        if c.is_dir():
            return c
    return candidates[0] if candidates else None


def _arc_storable_sidebar() -> Path | None:
    """Path to Arc's StorableSidebar.json: present whenever Arc has data."""
    home = Path.home()
    candidates = [
        home / "Library/Application Support/Arc/StorableSidebar.json",
        home / "AppData/Local/Packages/TheBrowserCompany.Arc_ttt1ap7aakyb4"
             / "LocalCache/Local/Arc/StorableSidebar.json",
        home / "AppData/Local/Arc/StorableSidebar.json",
    ]
    for c in candidates:
        if c.is_file():
            return c
    return None


def _arc_profiles(user_data: Path | None) -> list[str]:
    """List Arc profile directory names. Newer Chromium builds keep the
    History SQLite at ``<profile>/History``; some store it under
    ``<profile>/Network/`` after the cookie/network split. Either is a
    sufficient sign of a real profile.
    """
    if user_data is None or not user_data.is_dir():
        return []
    out: list[str] = []
    for entry in sorted(user_data.iterdir()):
        if not entry.is_dir():
            continue
        if (entry / "History").is_file() or (entry / "Network" / "Cookies").is_file():
            out.append(entry.name)
    return out


# ---------- Zen ----------


def _xdg_config_home() -> Path:
    """Resolve ``$XDG_CONFIG_HOME``, defaulting to ``~/.config``.

    XDG-aware builds (common on Arch) keep browser data under
    ``$XDG_CONFIG_HOME`` instead of the classic dotdir; honour the
    variable when the user sets it.
    """
    env = os.environ.get("XDG_CONFIG_HOME")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".config"


def _zen_profiles_roots() -> list[tuple[str, Path]]:
    """Every plausible Zen profiles root, each with a short install label.

    The label disambiguates the picker when the same profile name exists
    under more than one root (a native Zen and a Flatpak Zen side by
    side). It is empty for the classic location, which is the common
    case and needs no qualifier.
    """
    home = Path.home()
    if sys.platform == "darwin":
        return [("", home / "Library/Application Support/zen/Profiles")]
    if os.name == "nt":
        return [("", home / "AppData/Roaming/zen/Profiles")]
    return [
        ("", home / ".zen"),
        # XDG-aware builds (common on Arch) put the root under
        # $XDG_CONFIG_HOME instead of the classic dotdir.
        ("XDG", _xdg_config_home() / "zen"),
        # Flatpak (app.zen_browser.zen) keeps its data under the sandbox
        # home, not the host ~/.zen or ~/.config/zen.
        ("Flatpak", home / ".var/app/app.zen_browser.zen/.zen"),
    ]


def list_zen_profiles() -> list[ZenProfile]:
    """Every live Zen profile across every candidate root.

    We scan all roots rather than picking one, for two reasons: a stale
    ``~/.zen`` left by an uninstalled Zen must not shadow a live profile
    elsewhere, and a machine can genuinely run a native Zen and a Flatpak
    Zen at once. Both then show up in the profile picker instead of the
    tool silently choosing for the user.
    """
    result: list[ZenProfile] = []
    seen: set[Path] = set()
    for install, root in _zen_profiles_roots():
        try:
            entries = sorted(root.iterdir())
        except OSError:
            continue
        for entry in entries:
            if not entry.is_dir():
                continue
            # Heuristic: a real profile has at least places.sqlite
            if not (entry / "places.sqlite").is_file():
                continue
            resolved = entry.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            name = entry.name.split(".", 1)[1] if "." in entry.name else entry.name
            result.append(
                ZenProfile(
                    name=name,
                    path=entry,
                    is_release="release" in entry.name.lower(),
                    has_zen_sessions=(entry / "zen-sessions.jsonlz4").is_file(),
                    install=install,
                )
            )
    # Prefer release-labelled profiles first. The sort is stable, so
    # within a tie the root order above wins (classic before XDG before
    # Flatpak), keeping single-install machines on their old profile.
    result.sort(key=lambda p: (not p.is_release, p.name.lower()))
    return result


# ---------- browsers running ----------


_ARC_PROCESS_PATHS = ("/Applications/Arc.app",)
_ZEN_PROCESS_PATHS = (
    "/Applications/Zen.app/Contents/MacOS/zen",
    "/Applications/Zen Browser.app/Contents/MacOS/zen",
    "/Applications/zen.app/Contents/MacOS/zen",
)


def _pgrep_any(paths: tuple[str, ...]) -> bool:
    for p in paths:
        try:
            r = subprocess.run(["pgrep", "-f", p], capture_output=True, text=True, timeout=2)
        except Exception:
            continue
        if r.returncode == 0 and r.stdout.strip():
            return True
    return False


def _tasklist_running(image_names: tuple[str, ...]) -> bool:
    """Windows: faster running-process check via ``tasklist`` (~50 ms cold)
    rather than ``Get-Process`` via PowerShell (~300 ms cold).
    """
    for name in image_names:
        try:
            r = subprocess.run(
                ["tasklist", "/fi", f"imagename eq {name}", "/fo", "csv", "/nh"],
                capture_output=True, text=True, timeout=4,
            )
        except Exception:
            continue
        # ``tasklist`` prints a notice line ("INFO: No tasks ...") on stdout
        # when nothing matches; success means at least one CSV row.
        if r.returncode == 0 and r.stdout and name.lower() in r.stdout.lower():
            return True
    return False


def _powershell_running(name: str) -> bool:
    """Slower PowerShell-based fallback, kept for parity."""
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f'(Get-Process -Name "{name}" -ErrorAction SilentlyContinue).Count'],
            capture_output=True, text=True, timeout=4,
        )
    except Exception:
        return False
    return r.returncode == 0 and (r.stdout or "0").strip() != "0"


_ARC_WINDOWS_IMAGES = ("Arc.exe",)
_ZEN_WINDOWS_IMAGES = ("zen.exe", "zen-bin.exe")


def is_arc_running() -> bool:
    if sys.platform == "darwin" or sys.platform == "linux":
        return _pgrep_any(_ARC_PROCESS_PATHS)
    if os.name == "nt":
        return _tasklist_running(_ARC_WINDOWS_IMAGES)
    return False


def is_zen_running() -> bool:
    if sys.platform == "darwin" or sys.platform == "linux":
        return _pgrep_any(_ZEN_PROCESS_PATHS)
    if os.name == "nt":
        return _tasklist_running(_ZEN_WINDOWS_IMAGES)
    return False


# ---------- previous migration marker ----------


def _previous_migration_detected(zen_profile_path: Path | None) -> bool:
    if zen_profile_path is None:
        return False
    # Honour the legacy ``.arc2zen-migrated`` marker too, so an early
    # adopter who first ran arc2zen v1.1.2 doesn't get re-migrated.
    return (
        (zen_profile_path / ".browser2zen-migrated").is_file()
        or (zen_profile_path / ".arc2zen-migrated").is_file()
    )


# ---------- module checks ----------


def _has_module(name: str) -> bool:
    try:
        __import__(name)
        return True
    except Exception:
        return False


# ---------- public ----------


def check_environment(source=None) -> EnvReport:
    """Detect source-browser + Zen state.

    ``source`` is an optional :class:`BrowserExtractor` instance. When
    omitted we fall back to the Arc-specific path lookup so the existing
    CLI / Arc-only frontend keep working. When provided, all of the
    ``source_*`` fields on the returned report describe the chosen
    source — the frontend doesn't have to care which browser is selected.
    """
    errors: list[str] = []

    if source is not None:
        try:
            installed = source.is_installed()
            profile_dirs = source.profile_paths()
            profile_names = [p.name for p in profile_dirs]
            data_path = profile_dirs[0].parent if profile_dirs else None
            running = source.is_running()
        except Exception as exc:
            errors.append(f"{type(source).__name__} check failed: {exc}")
            installed = False
            profile_names = []
            data_path = None
            running = False
        sidebar = None  # only meaningful for Arc
    else:
        data_path = _arc_user_data_dir()
        sidebar = _arc_storable_sidebar()
        installed = sidebar is not None
        profile_names = _arc_profiles(data_path) if installed else []
        running = is_arc_running()

    zen_profiles = list_zen_profiles()
    zen_installed = bool(zen_profiles)

    return EnvReport(
        source_installed=installed,
        source_data_path=data_path if installed else None,
        source_profiles=profile_names,
        source_running=running,
        arc_storable_sidebar=sidebar,
        zen_installed=zen_installed,
        zen_profiles=zen_profiles,
        zen_running=is_zen_running(),
        has_lz4=_has_module("lz4"),
        has_cryptography=_has_module("cryptography"),
        previous_migration_detected=_previous_migration_detected(
            zen_profiles[0].path if zen_profiles else None
        ),
        errors=errors,
    )


def env_report_to_dict(report: EnvReport) -> dict:
    """JSON-serializable shape for the JS bridge."""
    return {
        "sourceInstalled": report.source_installed,
        "sourceDataPath": str(report.source_data_path) if report.source_data_path else None,
        "sourceProfiles": list(report.source_profiles),
        "sourceRunning": report.source_running,
        "zenInstalled": report.zen_installed,
        "zenProfiles": [
            {
                "name": p.name,
                "path": str(p.path),
                "isRelease": p.is_release,
                "hasZenSessions": p.has_zen_sessions,
                "install": p.install,
            }
            for p in report.zen_profiles
        ],
        "zenRunning": report.zen_running,
        "hasLz4": report.has_lz4,
        "hasCryptography": report.has_cryptography,
        "previousMigrationDetected": report.previous_migration_detected,
        "errors": list(report.errors),
    }
