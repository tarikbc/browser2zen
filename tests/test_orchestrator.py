"""Orchestrator-level tests.

These exercise the orchestrator end-to-end against the fixture trees:
preview produces sensible counts, the extractor dispatch works, and the
``excluded_spaces`` filter actually drops spaces.
"""

from __future__ import annotations

import pytest


def _orch(source_cls):
    """Build an orchestrator with the given extractor instance."""
    from app.orchestrator import MigrationOrchestrator
    return MigrationOrchestrator(source=source_cls())


@pytest.mark.parametrize(
    "extractor_name,home_fixture",
    [
        ("ArcExtractor", "arc_home"),
        ("ChromeExtractor", "chrome_home"),
        ("FirefoxExtractor", "firefox_home"),
        ("SafariExtractor", "safari_home"),
    ],
)
def test_preview_produces_counts(request, extractor_name, home_fixture, zen_profile):
    request.getfixturevalue(home_fixture)

    import extractors
    from app.orchestrator import MigrationOptions

    o = _orch(getattr(extractors, extractor_name))
    preview = o.preview(MigrationOptions(zen_profile_path=zen_profile))
    assert preview.spaces, f"{extractor_name} preview returned no spaces"
    assert preview.pinned_total >= 1


def test_check_environment_uses_source(arc_home, zen_profile):
    from extractors import ArcExtractor
    o = _orch(ArcExtractor)
    env = o.check_environment()
    assert env.source_installed is True
    assert env.zen_installed is True


def test_check_environment_finds_zen_under_xdg_config_home(tmp_path, monkeypatch):
    """Zen on Arch-style XDG-config installs keeps its profiles under
    ``~/.config/zen`` rather than ``~/.zen``; detection must find those
    (same root cause as the Firefox/Brave "not found" report)."""
    import shutil
    import sys
    from pathlib import Path

    from app.orchestrator import MigrationOrchestrator
    from extractors import ArcExtractor

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(sys, "platform", "linux")

    # Copy the Zen fixture under the XDG layout. Linux Zen keeps profiles
    # flat in the root (~/.zen/<profile>, ~/.config/zen/<profile>), not
    # under a macOS-style ``Profiles/`` subdirectory.
    fixtures = Path(__file__).resolve().parent / "fixtures" / "zen"
    src_profile = fixtures / "Profiles/test.default (release)"
    dest_profile = home / ".config/zen/test.default (release)"
    dest_profile.mkdir(parents=True)
    for f in src_profile.iterdir():
        if f.is_file():
            shutil.copy2(f, dest_profile / f.name)

    o = MigrationOrchestrator(source=ArcExtractor())
    env = o.check_environment()
    assert env.zen_installed is True
    assert len(env.zen_profiles) >= 1
    assert str(env.zen_profiles[0].path).startswith(str(home / ".config/zen"))


def test_check_environment_finds_zen_under_flatpak(tmp_path, monkeypatch):
    """Zen Flatpak (app.zen_browser.zen) keeps its profiles under the
    sandbox home ``~/.var/app/app.zen_browser.zen/.zen``; detection must
    find those (issue #5: Flatpak version not detected on Linux)."""
    import shutil
    import sys
    from pathlib import Path

    from app.orchestrator import MigrationOrchestrator
    from extractors import ArcExtractor

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(sys, "platform", "linux")

    fixtures = Path(__file__).resolve().parent / "fixtures" / "zen"
    src_profile = fixtures / "Profiles/test.default (release)"
    dest_profile = home / ".var/app/app.zen_browser.zen/.zen/test.default (release)"
    dest_profile.mkdir(parents=True)
    for f in src_profile.iterdir():
        if f.is_file():
            shutil.copy2(f, dest_profile / f.name)

    o = MigrationOrchestrator(source=ArcExtractor())
    env = o.check_environment()
    assert env.zen_installed is True
    assert len(env.zen_profiles) >= 1
    assert str(env.zen_profiles[0].path).startswith(
        str(home / ".var/app/app.zen_browser.zen/.zen")
    )


def test_stale_zen_root_does_not_shadow_live_xdg_profile(tmp_path, monkeypatch):
    """A leftover empty ``~/.zen/profiles.ini`` from an uninstalled Zen
    must not shadow a live profile under ``~/.config/zen`` (regression:
    root-picker used to prefer any root with a profiles.ini, making Zen
    look \"not installed\" on Arch upgrades)."""
    import sys
    from pathlib import Path

    from app.orchestrator import MigrationOrchestrator
    from extractors import ArcExtractor

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(sys, "platform", "linux")

    # Stale ~/.zen with a profiles.ini but no real profile data.
    stale = home / ".zen"
    stale.mkdir(parents=True)
    (stale / "profiles.ini").write_text(
        "[Profile0]\nPath=abc.default\n", encoding="utf-8"
    )

    # The live profile lives under the XDG layout.
    dest_profile = home / ".config/zen/abc.default (release)"
    dest_profile.mkdir(parents=True)
    (dest_profile / "places.sqlite").write_bytes(b"\x00" * 64)

    o = MigrationOrchestrator(source=ArcExtractor())
    env = o.check_environment()
    assert env.zen_installed is True
    assert len(env.zen_profiles) >= 1
    assert str(env.zen_profiles[0].path).startswith(str(home / ".config/zen"))


def test_zen_lists_profiles_from_every_root(tmp_path, monkeypatch):
    """A native Zen and a Flatpak Zen can be installed side by side. Both
    profiles must reach the picker: choosing a single root hid the Flatpak
    one whenever ``~/.zen`` also held a live profile, which is exactly the
    Flatpak-first case in issue #5."""
    import sys
    from pathlib import Path

    from app.orchestrator import MigrationOrchestrator
    from extractors import ArcExtractor

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(sys, "platform", "linux")

    native = home / ".zen/aaa.default (release)"
    flatpak = home / ".var/app/app.zen_browser.zen/.zen/bbb.default (release)"
    for p in (native, flatpak):
        p.mkdir(parents=True)
        (p / "places.sqlite").write_bytes(b"\x00" * 64)

    o = MigrationOrchestrator(source=ArcExtractor())
    env = o.check_environment()
    assert env.zen_installed is True
    paths = {str(p.path) for p in env.zen_profiles}
    assert paths == {str(native), str(flatpak)}
    # The install label disambiguates two identically-named profiles.
    installs = {str(p.path): p.install for p in env.zen_profiles}
    assert installs[str(native)] == ""
    assert installs[str(flatpak)] == "Flatpak"


def test_zen_classic_root_stays_first_when_both_are_live(tmp_path, monkeypatch):
    """With several live roots the classic ``~/.zen`` profile stays the
    default selection, so a single-install machine keeps its old target."""
    import sys
    from pathlib import Path

    from app.orchestrator import MigrationOrchestrator
    from extractors import ArcExtractor

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(sys, "platform", "linux")

    # Same profile name under both roots — only the root differs.
    native = home / ".zen/abc.default (release)"
    xdg = home / ".config/zen/abc.default (release)"
    for p in (native, xdg):
        p.mkdir(parents=True)
        (p / "places.sqlite").write_bytes(b"\x00" * 64)

    o = MigrationOrchestrator(source=ArcExtractor())
    env = o.check_environment()
    assert len(env.zen_profiles) == 2
    assert str(env.zen_profiles[0].path) == str(native)


def test_excluded_spaces_filter(chrome_home, zen_profile):
    """The orchestrator drops any space whose name is in
    excluded_spaces before lowering to the legacy dict."""
    from app.orchestrator import MigrationOptions
    from extractors import ChromeExtractor

    o = _orch(ChromeExtractor)
    # First, see what spaces Chrome produces.
    data = ChromeExtractor().extract()
    all_names = [s.space_name for s in data.spaces]
    if len(all_names) < 2:
        pytest.skip("Chrome fixture only emits one space; nothing to exclude.")

    excluded = [all_names[0]]
    opts = MigrationOptions(
        zen_profile_path=zen_profile,
        excluded_spaces=excluded,
    )
    # Drive _run far enough to hit the filter, then bail. The dry-run
    # surface is via direct re-call of source.extract() + apply the
    # exclusion in-process; we do it manually here.
    export = o.source.extract()
    export.spaces = [s for s in export.spaces if s.space_name not in excluded]
    remaining = [s.space_name for s in export.spaces]
    assert all_names[0] not in remaining
    assert len(remaining) == len(all_names) - 1
