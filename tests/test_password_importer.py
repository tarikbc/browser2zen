"""Tests for the saved-password migration path.

These never launch Zen. They cover the extractor data source, the
preview estimate, the importer's guards and backups, and the
non-Chromium skip. The actual in-browser import is exercised manually
against a throwaway profile, since it needs a real Zen binary.
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest


def _make_login_data(path: Path, *, usable: int, blacklisted: int = 0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE logins (origin_url TEXT, username_value TEXT, "
        "password_value BLOB, blacklisted_by_user INTEGER)"
    )
    rows = [("https://a.test", "u", b"x", 0)] * usable
    rows += [("https://b.test", "", b"", 1)] * blacklisted
    conn.executemany("INSERT INTO logins VALUES (?,?,?,?)", rows)
    conn.commit()
    conn.close()


# ---- extractor data source ----

def test_chromium_login_data_and_root(chrome_home):
    from extractors import ChromeExtractor

    ext = ChromeExtractor()
    root = ext.user_data_root()
    assert root is not None
    profile = ext.profile_paths()[0]
    _make_login_data(profile / "Login Data", usable=3)

    login_dbs = ext.login_data_paths()
    assert login_dbs == [profile / "Login Data"]
    assert login_dbs[0].parent.name == profile.name


def test_arc_login_data_and_root(arc_home):
    from extractors import ArcExtractor

    user_data = arc_home / "Library/Application Support/Arc/User Data"
    _make_login_data(user_data / "Default" / "Login Data", usable=4)
    _make_login_data(user_data / "Profile 1" / "Login Data", usable=1)

    ext = ArcExtractor()
    assert ext.user_data_root() == user_data
    assert {p.parent.name for p in ext.login_data_paths()} == {"Default", "Profile 1"}


def test_non_chromium_has_no_login_store(firefox_home):
    from extractors import FirefoxExtractor, SafariExtractor

    for cls in (FirefoxExtractor, SafariExtractor):
        assert cls().user_data_root() is None
        assert cls().login_data_paths() == []


def test_preview_estimate_counts_usable_logins(chrome_home, zen_profile):
    from app.orchestrator import MigrationOptions, MigrationOrchestrator
    from extractors import ChromeExtractor

    profile = ChromeExtractor().profile_paths()[0]
    _make_login_data(profile / "Login Data", usable=5, blacklisted=2)

    o = MigrationOrchestrator(source=ChromeExtractor())
    preview = o.preview(MigrationOptions(zen_profile_path=zen_profile))
    assert preview.passwords_estimate == 5


# ---- importer guards ----

@pytest.fixture
def macos(monkeypatch):
    """Pin the platform the importer supports.

    The importer refuses anything but macOS and Windows, so guard tests
    that are about a later check must say which platform they run on.
    Without this, Linux CI stops at the platform guard first.
    """
    monkeypatch.setattr(sys, "platform", "darwin")


def _importer(zen_profile: Path, source_user_data: Path, **kw):
    from zen_password_importer import ZenPasswordImporter

    defaults = dict(
        zen_profile=zen_profile,
        zen_binary=Path(sys.executable),
        source_user_data=source_user_data,
        profile_ids=["Default"],
    )
    defaults.update(kw)
    return ZenPasswordImporter(**defaults)


def test_dry_run_reports_plan_without_launching(tmp_path, macos):
    zen = tmp_path / "zen"
    zen.mkdir()
    (zen / "key4.db").write_bytes(b"x")
    src = tmp_path / "User Data"
    (src / "Default").mkdir(parents=True)

    res = _importer(zen, src, dry_run=True).import_passwords()
    assert res["would_import_from"] == ["Default"]
    assert "imported" in res and res["imported"] == 0


def test_guard_no_source_passwords(tmp_path, macos):
    zen = tmp_path / "zen"
    zen.mkdir()
    (zen / "key4.db").write_bytes(b"x")
    src = tmp_path / "User Data"
    src.mkdir()
    res = _importer(zen, src, profile_ids=[]).import_passwords()
    assert res["error"] == "no_source_passwords"


def test_guard_zen_binary_missing(tmp_path, macos):
    zen = tmp_path / "zen"
    zen.mkdir()
    (zen / "key4.db").write_bytes(b"x")
    src = tmp_path / "User Data"
    (src / "Default").mkdir(parents=True)
    res = _importer(zen, src, zen_binary=tmp_path / "nope").import_passwords()
    assert res["error"] == "zen_binary_missing"


def test_guard_login_store_missing(tmp_path, macos):
    zen = tmp_path / "zen"
    zen.mkdir()
    src = tmp_path / "User Data"
    (src / "Default").mkdir(parents=True)
    res = _importer(zen, src).import_passwords()
    assert res["error"] == "zen_login_store_missing"


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX fcntl lock")
def test_guard_zen_running_detects_lock(tmp_path, macos):
    zen = tmp_path / "zen"
    zen.mkdir()
    (zen / "key4.db").write_bytes(b"x")
    lock = zen / ".parentlock"
    lock.write_bytes(b"")
    ready = tmp_path / "ready"
    holder = subprocess.Popen([
        sys.executable, "-c",
        textwrap.dedent(f"""
            import fcntl, os, time
            fd = os.open({str(lock)!r}, os.O_RDWR)
            fcntl.lockf(fd, fcntl.LOCK_EX)
            open({str(ready)!r}, 'w').close()
            time.sleep(30)
        """),
    ])
    try:
        for _ in range(100):
            if ready.exists():
                break
            time.sleep(0.05)
        src = tmp_path / "User Data"
        (src / "Default").mkdir(parents=True)
        res = _importer(zen, src).import_passwords()
        assert res["error"] == "zen_running"
    finally:
        holder.terminate()
        holder.wait(timeout=5)


@pytest.mark.parametrize("platform", ["linux", "freebsd14"])
def test_guard_unsupported_platform_runs_first(tmp_path, monkeypatch, platform):
    """Off macOS/Windows there is no keystore Zen can decrypt with, so the
    importer stops before touching the profile, even in dry-run mode."""
    monkeypatch.setattr(sys, "platform", platform)
    zen = tmp_path / "zen"
    zen.mkdir()
    (zen / "key4.db").write_bytes(b"x")
    src = tmp_path / "User Data"
    (src / "Default").mkdir(parents=True)

    res = _importer(zen, src, dry_run=True).import_passwords()
    assert res["error"] == "passwords_unsupported_platform"
    assert "would_import_from" not in res
    assert sorted(p.name for p in zen.iterdir()) == ["key4.db"]


def test_guard_windows_is_supported(tmp_path, monkeypatch):
    """Windows passes the platform guard and reaches the next check."""
    monkeypatch.setattr(sys, "platform", "win32")
    zen = tmp_path / "zen"
    zen.mkdir()
    src = tmp_path / "User Data"
    (src / "Default").mkdir(parents=True)
    res = _importer(zen, src).import_passwords()
    assert res["error"] == "zen_login_store_missing"


# ---- per-profile outcomes ----

def test_summarize_all_profiles_imported():
    from zen_password_importer import summarize_report

    res = summarize_report({
        "imported": 7,
        "perProfile": {"Default": {"added": 5}, "Profile 1": {"added": 2}},
        "missing": [],
    })
    assert res["profiles"] == {"Default": 5, "Profile 1": 2}
    assert res["failed"] == {} and res["missing"] == []
    assert "warning" not in res and "error" not in res


def test_summarize_mixed_success_is_partial():
    from zen_password_importer import summarize_report

    res = summarize_report({
        "imported": 5,
        "perProfile": {"Default": {"added": 5}, "Profile 1": {"error": "NS_ERROR_FAILURE"}},
        "missing": [],
    })
    assert res["profiles"] == {"Default": 5}
    assert res["failed"] == {"Profile 1": "NS_ERROR_FAILURE"}
    assert res["warning"] == "partial_import"
    assert "error" not in res


def test_summarize_missing_profile_is_partial():
    from zen_password_importer import summarize_report

    res = summarize_report({
        "imported": 3,
        "perProfile": {"Default": {"added": 3}},
        "missing": ["Profile 2"],
    })
    assert res["missing"] == ["Profile 2"]
    assert res["warning"] == "partial_import"


def test_summarize_no_profile_migrated_is_error():
    from zen_password_importer import summarize_report

    res = summarize_report({
        "imported": 0,
        "perProfile": {"Default": {"error": "boom"}},
        "missing": ["Profile 1"],
    })
    assert res["error"] == "no_profiles_imported"
    assert res["failed"] == {"Default": "boom"}
    assert res["missing"] == ["Profile 1"]


def test_summarize_zero_imports_warns():
    from zen_password_importer import summarize_report

    res = summarize_report({
        "imported": 0,
        "perProfile": {"Default": {"added": 0}},
        "missing": [],
    })
    assert res["warning"] == "no_passwords_imported"
    assert "error" not in res


def _password_step_events(monkeypatch, zen_profile, summary):
    from app.orchestrator import MigrationOrchestrator
    from extractors import ChromeExtractor

    o = MigrationOrchestrator(source=ChromeExtractor())
    monkeypatch.setattr(o, "_import_passwords", lambda _zen: summary)
    o._run_passwords_step(zen_profile)
    return list(o.bus.drain())


def test_step_surfaces_mixed_success(chrome_home, zen_profile, monkeypatch):
    events = _password_step_events(monkeypatch, zen_profile, {
        "imported": 5, "profiles": {"Default": 5},
        "failed": {"Profile 1": "NS_ERROR_FAILURE"}, "missing": ["Profile 2"],
        "warning": "partial_import",
    })
    done = next(e for e in events if e["kind"] == "step_done")
    notice = done["summary"]["notice"]
    assert "5 passwords from 1 of 3 profiles" in notice
    assert "Profile 1 failed (NS_ERROR_FAILURE)" in notice
    assert "did not find Profile 2" in notice
    assert done["summary"]["warning"] == "partial_import"
    assert any(e["kind"] == "warn" and e["message"] == notice for e in events)


def test_step_surfaces_missing_profiles(chrome_home, zen_profile, monkeypatch):
    events = _password_step_events(monkeypatch, zen_profile, {
        "imported": 2, "profiles": {"Default": 2}, "failed": {},
        "missing": ["Profile 3", "Profile 4"], "warning": "partial_import",
    })
    done = next(e for e in events if e["kind"] == "step_done")
    assert "did not find Profile 3, Profile 4" in done["summary"]["notice"]


def test_step_surfaces_zero_imports(chrome_home, zen_profile, monkeypatch):
    events = _password_step_events(monkeypatch, zen_profile, {
        "imported": 0, "profiles": {"Default": 0}, "failed": {}, "missing": [],
        "warning": "no_passwords_imported",
    })
    done = next(e for e in events if e["kind"] == "step_done")
    assert "No passwords were imported" in done["summary"]["notice"]
    assert any(e["kind"] == "warn" for e in events)


def test_step_errors_when_no_profile_migrated(chrome_home, zen_profile, monkeypatch):
    events = _password_step_events(monkeypatch, zen_profile, {
        "imported": 0, "profiles": {}, "failed": {"Default": "boom"},
        "missing": [], "error": "no_profiles_imported",
    })
    assert not any(e["kind"] == "step_done" for e in events)
    err = next(e for e in events if e["kind"] == "step_error")
    assert "any Chrome profile" in err["detail"]
    assert "Default failed (boom)" in err["detail"]


def test_step_clean_import_has_no_notice(chrome_home, zen_profile, monkeypatch):
    events = _password_step_events(monkeypatch, zen_profile, {
        "imported": 4, "profiles": {"Default": 4}, "failed": {}, "missing": [],
    })
    done = next(e for e in events if e["kind"] == "step_done")
    assert "notice" not in done["summary"]
    assert not any(e["kind"] == "warn" for e in events)


# ---- temp prefs + backup ----

def test_temp_prefs_roundtrip_without_existing_user_js(tmp_path):
    zen = tmp_path / "zen"
    zen.mkdir()
    (zen / "prefs.js").write_text('user_pref("x", 1);\n', encoding="utf-8")
    imp = _importer(zen, tmp_path)

    prior = imp._apply_temp_prefs(2828)
    assert prior is None
    body = (zen / "user.js").read_text()
    assert '"marionette.port", 2828' in body

    # Simulate Zen persisting the injected prefs into prefs.js.
    (zen / "prefs.js").write_text(
        'user_pref("x", 1);\n'
        'user_pref("marionette.port", 2828);\n'
        'user_pref("remote.prefs.recommended", false);\n',
        encoding="utf-8",
    )
    imp._restore_prefs(prior)
    assert not (zen / "user.js").exists()
    kept = (zen / "prefs.js").read_text()
    assert "marionette.port" not in kept
    assert "remote.prefs.recommended" not in kept
    assert 'user_pref("x", 1);' in kept


def test_temp_prefs_preserves_existing_user_js(tmp_path):
    zen = tmp_path / "zen"
    zen.mkdir()
    (zen / "user.js").write_text('user_pref("keep.me", true);\n', encoding="utf-8")
    imp = _importer(zen, tmp_path)

    prior = imp._apply_temp_prefs(3000)
    assert prior == 'user_pref("keep.me", true);\n'
    imp._restore_prefs(prior)
    assert (zen / "user.js").read_text() == 'user_pref("keep.me", true);\n'


def test_backup_copies_login_store(tmp_path):
    zen = tmp_path / "zen"
    zen.mkdir()
    (zen / "logins.json").write_text("{}", encoding="utf-8")
    (zen / "key4.db").write_bytes(b"k")
    imp = _importer(zen, tmp_path)

    dest = imp._backup()
    assert dest is not None and dest.is_dir()
    assert (dest / "logins.json").is_file()
    assert (dest / "key4.db").is_file()


# ---- zen binary discovery ----

def test_zen_binary_path_finds_macos_app(fake_home, monkeypatch, tmp_path):
    import app.env_check as env_check

    fake = tmp_path / "Zen.app/Contents/MacOS/zen"
    fake.parent.mkdir(parents=True)
    fake.write_text("#!/bin/sh\n")
    monkeypatch.setattr(env_check, "_ZEN_PROCESS_PATHS", (str(fake),))
    assert env_check.zen_binary_path() == fake


def test_zen_binary_path_none_when_absent(fake_home, monkeypatch):
    import app.env_check as env_check

    monkeypatch.setattr(env_check, "_ZEN_PROCESS_PATHS", ("/nope/zen",))
    assert env_check.zen_binary_path() is None


# ---- orchestrator skip for non-Chromium ----

def test_orchestrator_skips_non_chromium_passwords(firefox_home, zen_profile):
    from app.orchestrator import MigrationOrchestrator
    from extractors import FirefoxExtractor

    o = MigrationOrchestrator(source=FirefoxExtractor())
    res = o._import_passwords(zen_profile)
    assert res["error"] == "passwords_source_unsupported"


def test_orchestrator_chromium_reaches_importer(chrome_home, zen_profile, monkeypatch):
    """A Chromium source must resolve the whole wiring (env_check import,
    importer construction) and stop at a guard, never crash. Guards against
    the bare-module import regression."""
    import app.env_check as env_check
    from app.orchestrator import MigrationOrchestrator
    from extractors import ChromeExtractor

    profile = ChromeExtractor().profile_paths()[0]
    _make_login_data(profile / "Login Data", usable=2)
    # Pin a resolvable binary and an empty login store so nothing launches.
    monkeypatch.setattr(env_check, "zen_binary_path", lambda: Path(sys.executable))
    (zen_profile / "key4.db").unlink(missing_ok=True)

    o = MigrationOrchestrator(source=ChromeExtractor())
    res = o._import_passwords(zen_profile)
    assert res["error"] == "zen_login_store_missing"
