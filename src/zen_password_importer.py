#!/usr/bin/env python3
"""
Chromium → Zen saved-password importer.

Zen stores passwords encrypted with its own NSS key (``key4.db``), so the
only robust way to add logins is to let Zen encrypt them. This importer
briefly launches Zen headless with Marionette and runs Zen's built-in
``ChromeProfileMigrator`` pointed at the source browser's ``User Data``
directory, restricted to passwords. Zen reads the source ``Login Data``,
decrypts each entry through the OS keystore (macOS Keychain /
Windows DPAPI), and writes it back encrypted with the target profile's
key. No password plaintext ever passes through this process, and nothing
here reverse-engineers Zen's credential crypto.

The step is skipped cleanly for non-Chromium sources (``user_data_root``
is ``None``) and refuses to run while Zen is open, since Zen must own the
login store during the write.

Backups of ``logins.json``, ``logins-backup.json`` and ``key4.db`` are
written beside the profile before anything changes, unless ``dry_run``.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


# Resource-type bit for passwords in Zen's MigrationUtils. Stable across
# Firefox/Zen releases; asserted in-browser below so a change surfaces
# loudly rather than silently importing the wrong data.
_PASSWORDS_RESOURCE_TYPE = 0x10

# Launch env that lets Marionette reach Zen's privileged chrome context.
_SYSTEM_ACCESS_ENV = "MOZ_REMOTE_ALLOW_SYSTEM_ACCESS"

_LAUNCH_TIMEOUT_S = 90.0
_QUIT_TIMEOUT_S = 30.0


# The chrome-context script Zen runs. It receives the plan as JSON and
# returns only counts, never a username or password. ``ChromeProfileMigrator``
# does the decrypt/encrypt internally.
_IMPORT_JS = r"""
const done = arguments[arguments.length - 1];
const plan = JSON.parse(arguments[0]);
(async () => {
  const { ChromeProfileMigrator } = ChromeUtils.importESModule(
    "resource:///modules/ChromeProfileMigrator.sys.mjs");
  const { MigrationUtils } = ChromeUtils.importESModule(
    "resource:///modules/MigrationUtils.sys.mjs");

  const PASSWORDS = MigrationUtils.resourceTypes.PASSWORDS;
  if (PASSWORDS !== plan.passwordsType) {
    return { fatal: "passwords resource type changed: " + PASSWORDS };
  }

  const m = new ChromeProfileMigrator();
  m._chromeUserDataPath = plan.userData;
  m._keychainServiceName = plan.keychainService;
  m._keychainAccountName = plan.keychainAccount;

  const before = (await Services.logins.getAllLogins()).length;
  const sourceProfiles = await m.getSourceProfiles();
  const byId = new Map(sourceProfiles.map(p => [p.id, p]));
  const report = { before, perProfile: {}, missing: [] };

  for (const id of plan.profileIds) {
    const profile = byId.get(id);
    if (!profile) { report.missing.push(id); continue; }
    const countBefore = (await Services.logins.getAllLogins()).length;
    try {
      await m.migrate(PASSWORDS, false, profile);
    } catch (e) {
      report.perProfile[id] = { error: String(e) };
      continue;
    }
    const countAfter = (await Services.logins.getAllLogins()).length;
    report.perProfile[id] = { added: countAfter - countBefore };
  }

  report.after = (await Services.logins.getAllLogins()).length;
  report.imported = report.after - before;
  return report;
})().then(done, e => done({ fatal: String(e), stack: e && e.stack }));
"""


def summarize_report(report: dict) -> dict:
    """Fold Zen's in-browser report into the result the UI renders.

    Every requested profile lands in exactly one bucket. ``profiles`` maps
    a migrated profile to the logins it added, ``failed`` maps a profile
    whose migration threw to Zen's error, and ``missing`` lists profiles
    Zen's migrator did not find. Partial outcomes carry a ``warning`` and a
    run where no profile migrated carries an ``error``, so a partial import
    never reads as a clean success.
    """
    per = report.get("perProfile") or {}
    profiles = {
        str(pid): int(v.get("added", 0))
        for pid, v in per.items() if "error" not in v
    }
    failed = {str(pid): str(v["error"]) for pid, v in per.items() if "error" in v}
    missing = [str(pid) for pid in report.get("missing") or []]
    result: dict = {
        "imported": int(report.get("imported", 0)),
        "profiles": profiles,
        "failed": failed,
        "missing": missing,
    }
    if not profiles:
        result["error"] = "no_profiles_imported"
    elif failed or missing:
        result["warning"] = "partial_import"
    elif result["imported"] == 0:
        # Every profile ran but nothing landed. Most often the Keychain or
        # DPAPI prompt was declined, or Zen already had every login.
        result["warning"] = "no_passwords_imported"
    return result


class ZenPasswordImporter:
    """Import Chromium-format saved passwords into a Zen profile."""

    def __init__(
        self,
        zen_profile: Path,
        zen_binary: Path,
        source_user_data: Path,
        profile_ids: list[str],
        *,
        keychain_service: str = "Arc Safe Storage",
        keychain_account: str = "Arc",
        dry_run: bool = False,
    ):
        self.zen_profile = Path(zen_profile)
        self.zen_binary = Path(zen_binary)
        self.source_user_data = Path(source_user_data)
        self.profile_ids = list(profile_ids)
        self.keychain_service = keychain_service
        self.keychain_account = keychain_account
        self.dry_run = dry_run

    # ---- guards ----

    def _zen_is_running_on_profile(self) -> bool:
        lock = self.zen_profile / ".parentlock"
        if not lock.exists():
            return False
        if os.name == "nt":
            # Windows holds the file open exclusively while Zen runs, so a
            # rename to itself fails only then.
            try:
                lock.rename(lock)
                return False
            except OSError:
                return True
        import fcntl

        try:
            fd = os.open(lock, os.O_RDWR)
        except OSError:
            return False
        try:
            fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.lockf(fd, fcntl.LOCK_UN)
            return False
        except OSError:
            return True
        finally:
            os.close(fd)

    # ---- backups ----

    def _backup(self) -> Path | None:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        dest = self.zen_profile / f"browser2zen-passwords-backup-{stamp}"
        made = False
        for name in ("logins.json", "logins-backup.json", "key4.db"):
            src = self.zen_profile / name
            if src.is_file():
                dest.mkdir(exist_ok=True)
                shutil.copy2(src, dest / name)
                made = True
        return dest if made else None

    def _count_existing_logins(self) -> int:
        store = self.zen_profile / "logins.json"
        if not store.is_file():
            return 0
        try:
            return len(json.loads(store.read_text(encoding="utf-8")).get("logins", []))
        except Exception:
            return 0

    # ---- marionette ----

    @staticmethod
    def _free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    def _run_in_zen(self, plan: dict, port: int) -> dict:
        deadline = time.time() + _LAUNCH_TIMEOUT_S
        sock: socket.socket | None = None
        while sock is None:
            try:
                sock = socket.create_connection(("127.0.0.1", port))
            except OSError:
                if time.time() > deadline:
                    raise TimeoutError("Zen Marionette did not accept a connection")
                time.sleep(0.5)
        try:
            sock.settimeout(_LAUNCH_TIMEOUT_S)
            self._recv(sock)  # server "hello"
            self._cmd(sock, "WebDriver:NewSession",
                      {"capabilities": {"alwaysMatch": {"timeouts": {"script": 300000}}}})
            self._cmd(sock, "Marionette:SetContext", {"value": "chrome"})
            res = self._cmd(sock, "WebDriver:ExecuteAsyncScript",
                            {"script": _IMPORT_JS, "args": [json.dumps(plan)]})
            report = res.get("value", res) if isinstance(res, dict) else res
            try:
                self._cmd(sock, "Marionette:Quit", {"flags": ["eAttemptQuit"]})
            except Exception:
                pass
            return report
        finally:
            sock.close()

    _msg_id = 0

    def _cmd(self, sock: socket.socket, name: str, params: dict) -> dict:
        self._msg_id += 1
        body = json.dumps([0, self._msg_id, name, params]).encode()
        sock.sendall(str(len(body)).encode() + b":" + body)
        _, _, err, res = self._recv(sock)
        if err:
            raise RuntimeError(f"{name}: {err}")
        return res

    @staticmethod
    def _recv(sock: socket.socket) -> list:
        buf = b""
        while b":" not in buf:
            chunk = sock.recv(1)
            if not chunk:
                raise ConnectionError("Marionette closed the connection")
            buf += chunk
        length_str, rest = buf.split(b":", 1)
        length = int(length_str)
        while len(rest) < length:
            chunk = sock.recv(length - len(rest))
            if not chunk:
                raise ConnectionError("Marionette closed the connection")
            rest += chunk
        return json.loads(rest)

    # ---- temp prefs ----

    def _apply_temp_prefs(self, port: int) -> str | None:
        """Write Marionette prefs into ``user.js``; return prior contents."""
        user_js = self.zen_profile / "user.js"
        prior = user_js.read_text(encoding="utf-8") if user_js.is_file() else None
        lines = (prior or "")
        lines += (
            '\nuser_pref("remote.prefs.recommended", false);'
            f'\nuser_pref("marionette.port", {port});\n'
        )
        user_js.write_text(lines, encoding="utf-8")
        return prior

    def _restore_prefs(self, prior: str | None) -> None:
        user_js = self.zen_profile / "user.js"
        if prior is None:
            user_js.unlink(missing_ok=True)
        else:
            user_js.write_text(prior, encoding="utf-8")
        # Zen copies user.js values into prefs.js on launch, so strip the two
        # we injected or they persist after the run.
        prefs = self.zen_profile / "prefs.js"
        if prefs.is_file():
            kept = [
                ln for ln in prefs.read_text(encoding="utf-8").splitlines(keepends=True)
                if '"marionette.port"' not in ln
                and '"remote.prefs.recommended"' not in ln
            ]
            prefs.write_text("".join(kept), encoding="utf-8")

    # ---- orchestration ----

    def import_passwords(self) -> dict:
        result: dict = {"imported": 0, "profiles": {}, "failed": {}, "missing": []}

        # Zen's Chromium importer decrypts through the macOS Keychain or
        # Windows DPAPI; there is no supported keystore path elsewhere.
        if sys.platform not in ("darwin", "win32"):
            result["error"] = "passwords_unsupported_platform"
            return result
        if not self.profile_ids or not self.source_user_data.is_dir():
            result["error"] = "no_source_passwords"
            return result
        if not self.zen_binary.is_file():
            result["error"] = "zen_binary_missing"
            return result
        if not (self.zen_profile / "key4.db").is_file():
            # Fresh profile that Zen has never launched has no login key yet.
            result["error"] = "zen_login_store_missing"
            return result
        if self._zen_is_running_on_profile():
            result["error"] = "zen_running"
            return result

        existing = self._count_existing_logins()
        if self.dry_run:
            result["would_import_from"] = list(self.profile_ids)
            result["existing_logins"] = existing
            return result

        result["backup"] = str(self._backup() or "")

        plan = {
            "userData": str(self.source_user_data),
            "profileIds": self.profile_ids,
            "keychainService": self.keychain_service,
            "keychainAccount": self.keychain_account,
            "passwordsType": _PASSWORDS_RESOURCE_TYPE,
        }
        port = self._free_port()
        prior_prefs = self._apply_temp_prefs(port)
        proc: subprocess.Popen | None = None
        try:
            env = dict(os.environ, **{_SYSTEM_ACCESS_ENV: "1"})
            proc = subprocess.Popen(
                [str(self.zen_binary), "--marionette", "--headless",
                 "-no-remote", "-profile", str(self.zen_profile)],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            report = self._run_in_zen(plan, port)
            self._wait_for_exit(proc)
        except Exception as exc:
            logger.error("Password import failed: %s", exc)
            result["error"] = "password_import_failed"
            if proc is not None:
                self._terminate(proc)
            return result
        finally:
            self._restore_prefs(prior_prefs)

        if isinstance(report, dict) and report.get("fatal"):
            logger.error("Zen password import error: %s", report.get("fatal"))
            result["error"] = "password_import_failed"
            return result

        if not isinstance(report, dict):
            result["error"] = "password_import_failed"
            return result
        return {**summarize_report(report), "backup": result["backup"]}

    def _wait_for_exit(self, proc: subprocess.Popen) -> None:
        try:
            proc.wait(timeout=_QUIT_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            self._terminate(proc)

    @staticmethod
    def _terminate(proc: subprocess.Popen) -> None:
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
