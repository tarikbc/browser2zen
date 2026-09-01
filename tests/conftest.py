"""Shared test fixtures.

The fixture tree under ``tests/fixtures/`` mirrors the directory layout
each source browser uses on a real macOS machine, anchored under a
``HOME`` we redirect via ``Path.home()``. Every test gets a fresh copy
of the source + Zen fixtures in a tempdir so writes don't bleed between
runs.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"

# Make ``src/`` and the repo root importable from tests, mirroring how
# the orchestrator wires things up at runtime.
for path in (REPO_ROOT, REPO_ROOT / "src"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))


@pytest.fixture(autouse=True)
def _clear_xdg_config_home(monkeypatch):
    """Unset ``XDG_CONFIG_HOME`` for every test.

    The Linux path lookups fall back to ``Path.home()/".config"`` only
    when the variable is absent, and the tests redirect ``Path.home()``
    into a tempdir. GitHub's Ubuntu runners export the variable, so
    leaving it in place made the XDG tests pass on a developer's Mac and
    fail in CI. Tests that exercise the variable set it themselves with
    ``monkeypatch.setenv``, which still works on top of this.
    """
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)


# --- per-source fixture trees ---------------------------------------------
# Each "tree" maps source-relative paths to home-relative destinations,
# matching what the corresponding extractor's path lookups expect on real
# macOS.

_SOURCE_TREES = {
    "arc": {
        "arc/StorableSidebar.json":
            "Library/Application Support/Arc/StorableSidebar.json",
        # ArcPinnedTabExtractor only reads the StorableSidebar; the User
        # Data dir is for the Chromium readers, which Arc's tests skip.
    },
    "chrome": {
        "chrome/User Data/Default/Bookmarks":
            "Library/Application Support/Google/Chrome/Default/Bookmarks",
        "chrome/User Data/Default/History":
            "Library/Application Support/Google/Chrome/Default/History",
        "chrome/User Data/Default/Favicons":
            "Library/Application Support/Google/Chrome/Default/Favicons",
        "chrome/User Data/Default/Cookies":
            "Library/Application Support/Google/Chrome/Default/Cookies",
        "chrome/User Data/Default/Sessions/Session_13400000000000000":
            "Library/Application Support/Google/Chrome/Default/Sessions/Session_13400000000000000",
        "chrome/User Data/Local State":
            "Library/Application Support/Google/Chrome/Local State",
    },
    "firefox": {
        "firefox/profiles.ini":
            "Library/Application Support/Firefox/profiles.ini",
        "firefox/Profiles/test.default-release/places.sqlite":
            "Library/Application Support/Firefox/Profiles/test.default-release/places.sqlite",
        "firefox/Profiles/test.default-release/cookies.sqlite":
            "Library/Application Support/Firefox/Profiles/test.default-release/cookies.sqlite",
    },
    "safari": {
        "safari/Library/Safari/Bookmarks.plist":
            "Library/Safari/Bookmarks.plist",
        "safari/Library/Safari/History.db":
            "Library/Safari/History.db",
        "safari/Library/Containers/com.apple.Safari/Data/Library/Cookies/Cookies.binarycookies":
            "Library/Containers/com.apple.Safari/Data/Library/Cookies/Cookies.binarycookies",
    },
}

ZEN_FIXTURE = "zen/Profiles/test.default (release)"


def _materialise(home: Path, source: str) -> None:
    """Copy a fixture tree into ``home`` so extractors find it via Path.home()."""
    if source not in _SOURCE_TREES:
        raise KeyError(f"unknown fixture source: {source}")
    for src_rel, dest_rel in _SOURCE_TREES[source].items():
        src = FIXTURES / src_rel
        dest = home / dest_rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)


def _materialise_zen(home: Path) -> Path:
    """Copy the Zen fixture under ~/Library/Application Support/zen/Profiles/."""
    src = FIXTURES / ZEN_FIXTURE
    dest = home / "Library/Application Support/zen/Profiles/test.default (release)"
    dest.mkdir(parents=True, exist_ok=True)
    for f in src.iterdir():
        shutil.copy2(f, dest / f.name)
    return dest


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """Redirect Path.home() to a tmpdir + force ``sys.platform == 'darwin'``
    for the duration of one test.

    The platform pin is what makes the suite portable across OSes:
    every extractor's path lookup and every is_installed() check
    branches on ``sys.platform``, and our fixture trees only mirror the
    macOS layout. Without this pin, CI on Linux would look at
    ``~/.config/google-chrome`` and find nothing.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(sys, "platform", "darwin")
    return home


@pytest.fixture
def arc_home(fake_home):
    _materialise(fake_home, "arc")
    return fake_home


@pytest.fixture
def chrome_home(fake_home):
    _materialise(fake_home, "chrome")
    return fake_home


@pytest.fixture
def firefox_home(fake_home):
    _materialise(fake_home, "firefox")
    return fake_home


@pytest.fixture
def safari_home(fake_home):
    _materialise(fake_home, "safari")
    return fake_home


@pytest.fixture
def zen_profile(fake_home):
    """Materialise the Zen target fixture and return its profile path."""
    return _materialise_zen(fake_home)


@pytest.fixture
def all_homes(fake_home):
    """A home containing every source-browser fixture plus an empty Zen
    profile. Used for orchestrator-level tests that exercise the full
    pipeline."""
    for source in _SOURCE_TREES:
        _materialise(fake_home, source)
    _materialise_zen(fake_home)
    return fake_home
