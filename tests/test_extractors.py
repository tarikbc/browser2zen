"""End-to-end tests for each source-browser extractor.

Each test:
1. Materialises the fixture profile under a fake $HOME.
2. Verifies ``is_installed()`` returns True against that fixture.
3. Verifies ``extract()`` produces the expected ExportData shape.
4. Verifies the legacy_dict round-trip preserves URLs and titles.
"""

from __future__ import annotations

import pytest

# ----- Arc ----------------------------------------------------------------

def test_arc_extractor_detects_fixture(arc_home):
    from extractors import ArcExtractor

    ext = ArcExtractor()
    assert ext.is_installed() is True


def test_arc_extractor_extract_shape(arc_home):
    from extractors import ArcExtractor

    data = ArcExtractor().extract()
    assert data.source == "arc"
    assert len(data.spaces) == 1
    space = data.spaces[0]
    assert space.space_name == "Test Space"
    urls = sorted(t.url for t in space.pinned_tabs)
    assert urls == ["https://example.com/", "https://mozilla.org/"]


def test_arc_extractor_legacy_dict(arc_home):
    from extractors import ArcExtractor

    legacy = ArcExtractor().extract().to_legacy_dict()
    assert legacy["source"] == "arc"
    assert legacy["total_spaces"] == 1
    space = legacy["spaces"][0]
    assert {"space_id", "space_name", "pinned_tabs", "open_tabs", "folders",
            "icon", "color"} <= set(space.keys())
    assert any(t["url"] == "https://example.com/" for t in space["pinned_tabs"])


# ----- Chrome -------------------------------------------------------------

def test_chrome_extractor_detects_fixture(chrome_home):
    from extractors import ChromeExtractor

    assert ChromeExtractor().is_installed() is True


def test_chrome_extractor_extract_shape(chrome_home):
    """Chromium pinned/open tabs come from the SNSS session store; the
    Bookmarks tree feeds the separate ``bookmarks`` channel (so it lands
    in Zen's bookmarks, not as a flood of sidebar pinned tabs)."""
    from extractors import ChromeExtractor

    data = ChromeExtractor().extract()
    assert data.source == "chrome"
    assert len(data.spaces) >= 1
    space = data.spaces[0]

    # Real tab-strip state from the session fixture.
    assert [t.url for t in space.pinned_tabs] == ["https://pinned.example/"]
    assert [t.url for t in space.open_tabs] == ["https://open.example/"]

    # Bookmarks ride the dedicated channel, not pinned_tabs.
    bm_urls = sorted(t.url for t in (space.bookmarks or []))
    assert "https://example.com/" in bm_urls
    assert "https://mozilla.org/" in bm_urls
    folder_titles = {f.title for f in (space.bookmark_folders or [])}
    assert "Test Folder" in folder_titles
    # Bookmarks must NOT leak into the sidebar pinned tabs.
    assert "https://example.com/" not in {t.url for t in space.pinned_tabs}


def test_chrome_history_db_paths(chrome_home):
    from extractors import ChromeExtractor

    paths = ChromeExtractor().history_db_paths()
    assert any(p.name == "History" for p in paths)


def test_chrome_cookie_db_paths(chrome_home):
    from extractors import ChromeExtractor

    paths = ChromeExtractor().cookie_db_paths()
    assert any(p.name == "Cookies" for p in paths)


def test_chromium_quit_escalates_to_force_kill(monkeypatch):
    """When a graceful quit is ignored, quit() must escalate to a forced
    kill rather than just timing out (regression: Brave wouldn't quit)."""
    import extractors.chromium as chromium
    from extractors import BraveExtractor

    ext = BraveExtractor()
    state = {"running": True, "cmds": []}

    def fake_run(cmd, **kwargs):
        state["cmds"].append(cmd)
        # Only a forced kill actually stops it: pkill -KILL (macOS) or
        # taskkill /f (Windows). A polite quit is ignored.
        if cmd[0] == "pkill" and "-KILL" in cmd:
            state["running"] = False
        if cmd[0] == "taskkill" and "/f" in cmd:
            state["running"] = False

        class _R:
            returncode = 0
            stdout = ""
            stderr = ""

        return _R()

    # quit() escalation is macOS/Windows-only by design (see CLAUDE.md);
    # on any other host it bails early. Pin the platform so the test
    # exercises the macOS force-kill path regardless of the CI runner's OS
    # (the Linux runner would otherwise hit the unsupported-platform branch).
    monkeypatch.setattr(chromium.sys, "platform", "darwin")

    # Fake clock so the 6s graceful deadline elapses without real waiting.
    clock = {"t": 1000.0}
    monkeypatch.setattr(chromium.time, "time", lambda: clock["t"])
    monkeypatch.setattr(chromium.time, "sleep",
                        lambda s: clock.__setitem__("t", clock["t"] + s))
    monkeypatch.setattr(chromium.subprocess, "run", fake_run)
    monkeypatch.setattr(ext, "is_running", lambda: state["running"])

    result = ext.quit()

    assert result["ok"] is True
    assert result.get("forced") is True
    assert state["running"] is False
    # A forced kill command must have been issued.
    forced = [c for c in state["cmds"]
              if ("-KILL" in c) or (c[0] == "taskkill" and "/f" in c)]
    assert forced, "expected a forced-kill command to be issued"


# ----- Firefox ------------------------------------------------------------

def test_firefox_extractor_detects_fixture(firefox_home):
    from extractors import FirefoxExtractor

    assert FirefoxExtractor().is_installed() is True


def test_firefox_extractor_extract_shape(firefox_home):
    from extractors import FirefoxExtractor

    data = FirefoxExtractor().extract()
    assert data.source == "firefox"
    assert len(data.spaces) == 1
    space = data.spaces[0]
    urls = sorted(t.url for t in space.pinned_tabs)
    assert urls == ["https://example.com/", "https://mozilla.org/"]


def test_firefox_extractor_skips_disabled_root(firefox_home):
    """The fixture has bookmarks under toolbar (id=3); other / mobile /
    menu are empty. Extractor should only emit folders for non-empty
    roots."""
    from extractors import FirefoxExtractor

    data = FirefoxExtractor().extract()
    folder_titles = {f.title for f in data.spaces[0].folders}
    # The toolbar wrapper folder should be present.
    assert "Bookmarks Toolbar" in folder_titles
    # Empty roots should not produce wrappers.
    assert "Bookmarks Menu" not in folder_titles


@pytest.mark.parametrize(
    "root_rel",
    [
        ".config/mozilla/firefox",                              # Arch XDG-config layout
        "snap/firefox/common/.mozilla/firefox",                 # Ubuntu Snap
        ".var/app/org.mozilla.firefox/.mozilla/firefox",        # Flatpak
    ],
)
def test_firefox_extractor_detects_linux_packaged_install(
    tmp_path, monkeypatch, root_rel
):
    """Snap/Flatpak/XDG-config Firefox keeps its profiles outside
    ``~/.mozilla``; detection must find those too (regression for the
    Reddit report that Firefox wasn't detected, and the Arch report where
    profiles live under ``~/.config/mozilla/firefox``)."""
    import shutil
    import sys
    from pathlib import Path

    from extractors import FirefoxExtractor

    fixtures = Path(__file__).resolve().parent / "fixtures" / "firefox"
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(sys, "platform", "linux")

    # Re-anchor the Firefox fixture tree under the packaged root instead
    # of the macOS ``Library/Application Support/Firefox`` location.
    for src in fixtures.rglob("*"):
        if src.is_file():
            dest = home / root_rel / src.relative_to(fixtures)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)

    ext = FirefoxExtractor()
    assert ext.is_installed() is True
    data = ext.extract()
    assert data.source == "firefox"
    assert len(data.spaces) == 1


def test_firefox_extractor_honours_xdg_config_home(tmp_path, monkeypatch):
    """When the user sets ``XDG_CONFIG_HOME`` explicitly, Firefox's
    profiles root must follow it (the profiles root is
    ``$XDG_CONFIG_HOME/mozilla/firefox`` on XDG-aware builds)."""
    import shutil
    import sys
    from pathlib import Path

    from extractors import FirefoxExtractor

    fixtures = Path(__file__).resolve().parent / "fixtures" / "firefox"
    home = tmp_path / "home"
    home.mkdir()
    xdg_config = tmp_path / "xdg"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_config))
    monkeypatch.setattr(sys, "platform", "linux")

    root_rel = "mozilla/firefox"
    for src in fixtures.rglob("*"):
        if src.is_file():
            dest = xdg_config / root_rel / src.relative_to(fixtures)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)

    ext = FirefoxExtractor()
    assert ext.is_installed() is True
    assert len(ext.profile_paths()) == 1


def test_firefox_extractor_detects_windows_store_msix(tmp_path, monkeypatch):
    """Microsoft Store (MSIX) Firefox sandboxes its profile under the
    package container; detection must look there too (regression for the
    Win11 report where both browser2zen and Zen missed a Store Firefox)."""
    import shutil
    import sys
    from pathlib import Path

    from extractors import FirefoxExtractor

    fixtures = Path(__file__).resolve().parent / "fixtures" / "firefox"
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(sys, "platform", "win32")

    # MSIX container path with a publisher-id suffix we don't hardcode.
    root_rel = (
        "AppData/Local/Packages/Mozilla.Firefox_n80bbvh6b1yt2"
        "/LocalCache/Roaming/Mozilla/Firefox"
    )
    for src in fixtures.rglob("*"):
        if src.is_file():
            dest = home / root_rel / src.relative_to(fixtures)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)

    ext = FirefoxExtractor()
    assert ext.is_installed() is True
    data = ext.extract()
    assert data.source == "firefox"
    assert len(data.spaces) == 1


# ----- Safari -------------------------------------------------------------

def test_safari_extractor_detects_fixture(safari_home):
    from extractors import SafariExtractor

    assert SafariExtractor().is_installed() is True


def test_safari_extractor_extract_shape(safari_home):
    from extractors import SafariExtractor

    data = SafariExtractor().extract()
    assert data.source == "safari"
    assert len(data.spaces) == 1
    space = data.spaces[0]
    urls = sorted(t.url for t in space.pinned_tabs)
    assert urls == ["https://example.com/", "https://mozilla.org/"]
    folder_titles = {f.title for f in space.folders}
    assert "Bookmarks Bar" in folder_titles
    assert "Bookmarks Menu" in folder_titles
    # Empty Reading List is filtered out by _root_has_content.
    assert "Reading List" not in folder_titles


# ----- Edge / Brave (no fixtures, just registry sanity) -------------------

def test_edge_extractor_registered():
    from extractors import EdgeExtractor, by_name
    assert by_name("edge") is EdgeExtractor


def test_brave_extractor_registered():
    from extractors import BraveExtractor, by_name
    assert by_name("brave") is BraveExtractor


def test_brave_extractor_detects_brave_origin(tmp_path, monkeypatch):
    """Brave Origin (official ``brave-origin-bin`` AUR package) keeps its
    User Data under ``~/.config/BraveSoftware/Brave-Origin`` instead of
    ``Brave-Browser``. Detection must find it, even when a leftover empty
    ``Brave-Browser`` directory is also present (regression for the Arch
    report where Brave showed as "not found")."""
    import json
    import sys
    from pathlib import Path

    from extractors import BraveExtractor

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(sys, "platform", "linux")

    origin = home / ".config/BraveSoftware/Brave-Origin"
    (origin / "Default").mkdir(parents=True)
    (origin / "Local State").write_text('{"profile": {}}', encoding="utf-8")
    (origin / "Default" / "Bookmarks").write_text(json.dumps({
        "roots": {
            "bookmark_bar": {
                "children": [
                    {"type": "url", "name": "Example", "url": "https://example.com/"}
                ]
            },
            "other": {"children": []},
            "synced": {"children": []},
        },
    }), encoding="utf-8")

    # A leftover, never-launched Brave-Browser install must NOT shadow it.
    (home / ".config/BraveSoftware/Brave-Browser/NativeMessagingHosts").mkdir(parents=True)

    ext = BraveExtractor()
    assert ext.is_installed() is True
    roots = [p.name for p in ext.profile_paths()]
    assert roots == ["Default"]
    data = ext.extract()
    assert data.source == "brave"
    urls = sorted(t.url for t in (data.spaces[0].bookmarks or []))
    assert urls == ["https://example.com/"]


def test_brave_origin_survives_a_launched_then_abandoned_brave_browser(
    tmp_path, monkeypatch
):
    """A ``Brave-Browser`` dir that was launched once and then abandoned
    carries a ``Local State`` and a ``Default/History``, but no bookmarks.
    It must not shadow a live ``Brave-Origin``: selecting it made
    ``is_installed`` return False, i.e. the very "not found" symptom the
    Brave-Origin support exists to fix."""
    import json
    import sys
    from pathlib import Path

    from extractors import BraveExtractor

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(sys, "platform", "linux")

    # Abandoned install: launched (so Local State + History exist), but
    # the user never bookmarked anything.
    old = home / ".config/BraveSoftware/Brave-Browser"
    (old / "Default").mkdir(parents=True)
    (old / "Local State").write_text('{"profile": {}}', encoding="utf-8")
    (old / "Default" / "History").write_bytes(b"\x00" * 64)

    # The install actually in use.
    origin = home / ".config/BraveSoftware/Brave-Origin"
    (origin / "Default").mkdir(parents=True)
    (origin / "Local State").write_text('{"profile": {}}', encoding="utf-8")
    (origin / "Default" / "Bookmarks").write_text(json.dumps({
        "roots": {
            "bookmark_bar": {"children": [
                {"type": "url", "name": "Example", "url": "https://example.com/"}
            ]},
            "other": {"children": []},
            "synced": {"children": []},
        },
    }), encoding="utf-8")

    ext = BraveExtractor()
    assert ext._user_data_dir() == origin
    assert ext.is_installed() is True
    urls = sorted(t.url for t in (ext.extract().spaces[0].bookmarks or []))
    assert urls == ["https://example.com/"]


def test_chromium_user_data_dir_honours_xdg_config_home(tmp_path, monkeypatch):
    """Chromium reads its User Data root from ``$XDG_CONFIG_HOME``, so a
    ``.config/...`` candidate must follow the variable when the user sets
    it. Firefox and Zen already honoured it; the Chromium family did
    not."""
    import json
    import sys
    from pathlib import Path

    from extractors import BraveExtractor

    home = tmp_path / "home"
    home.mkdir()
    xdg = tmp_path / "xdg"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    monkeypatch.setattr(sys, "platform", "linux")

    root = xdg / "BraveSoftware/Brave-Browser"
    (root / "Default").mkdir(parents=True)
    (root / "Local State").write_text('{"profile": {}}', encoding="utf-8")
    (root / "Default" / "Bookmarks").write_text(json.dumps({
        "roots": {
            "bookmark_bar": {"children": [
                {"type": "url", "name": "Example", "url": "https://example.com/"}
            ]},
            "other": {"children": []},
            "synced": {"children": []},
        },
    }), encoding="utf-8")

    ext = BraveExtractor()
    assert ext._user_data_dir() == root
    assert ext.is_installed() is True


def test_chromium_container_paths_ignore_xdg_config_home(tmp_path, monkeypatch):
    """Snap and Flatpak candidates carry their own container prefix, so
    they stay relative to ``$HOME`` even when ``XDG_CONFIG_HOME`` is
    set."""
    import sys
    from pathlib import Path

    from extractors import BraveExtractor

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(sys, "platform", "linux")

    paths = BraveExtractor()._linux_user_data_paths(home)
    flatpak = home / ".var/app/com.brave.Browser/config/BraveSoftware/Brave-Browser"
    snap = home / "snap/brave/current/.config/BraveSoftware/Brave-Browser"
    assert flatpak in paths
    assert snap in paths
    # The XDG root is preferred over ~/.config for the plain install.
    assert paths[0] == tmp_path / "xdg/BraveSoftware/Brave-Browser"


def test_unknown_source_raises():
    from extractors import by_name
    with pytest.raises(KeyError):
        by_name("netscape")
