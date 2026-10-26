import { test, expect } from "@playwright/test";
import path from "path";
import { fileURLToPath } from "url";

const __filename = fileURLToPath(import.meta.url);
const __dirname  = path.dirname(__filename);

const REPO_ROOT  = path.resolve(__dirname, "..", "..");
const INDEX_HTML = "file://" + path.join(REPO_ROOT, "app", "frontend", "index.html");
const MOCK_FILE  = path.join(__dirname, "mock-bridge.js");

// Boots the app exactly the way PyWebView would: mock bridge in place,
// then dispatch pywebviewready to kick the JS state machine into life.
// We rely on real UI clicks afterwards so Chromium flushes layout/paint
// the same way it would for a user — driving navigation through
// page.evaluate alone races the repaint and produces stale screenshots.
async function bootHarness(page) {
  page.on("pageerror", (err) => console.log(`[pageerror] ${err.message}`));
  await page.addInitScript({ path: MOCK_FILE });
  // Inject a CSS override that kills transitions/animations *before*
  // any markup loads, so the screen swaps don't race the fade-in.
  // Without this we hit intermittent "element is not visible" failures
  // on the second-clicked button while opacity is still 0.
  await page.addInitScript(() => {
    const style = document.createElement("style");
    style.textContent = "*, *::before, *::after { transition: none !important; animation: none !important; }";
    if (document.head) document.head.appendChild(style);
    else document.addEventListener("DOMContentLoaded", () => document.head.appendChild(style));
  });
  await page.goto(INDEX_HTML);
  await page.evaluate(() => window.dispatchEvent(new Event("pywebviewready")));
  await waitForActiveScreen(page, "welcome");
  await page.waitForTimeout(150);
}

// Wait for a screen to be both flagged active AND rendered with non-zero
// geometry. Without the geometry check we sometimes click the next
// button before Chromium has run layout for the freshly-activated screen.
async function waitForActiveScreen(page, name: string) {
  await page.waitForFunction((n) => {
    if (document.body.dataset.screen !== n) return false;
    const screen = document.getElementById(`screen-${n}`);
    if (!screen) return false;
    const r = screen.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  }, name, { timeout: 10_000 });
}

async function shoot(page, name: string) {
  // Two RAFs + a small idle window before capture to make sure layout
  // has fully settled (icons that load via SVG inject, etc.).
  await page.evaluate(() => new Promise((r) =>
    requestAnimationFrame(() => requestAnimationFrame(r))));
  await page.waitForTimeout(150);
  await page.screenshot({ path: path.join(__dirname, `${name}.png`) });
}

test("welcome", async ({ page }) => {
  await bootHarness(page);
  await shoot(page, "welcome");
});

test("source-picker", async ({ page }) => {
  await bootHarness(page);
  await page.locator("#welcome-go").click();
  await waitForActiveScreen(page, "source");
  await shoot(page, "source-picker");
});

test("detect", async ({ page }) => {
  await bootHarness(page);
  await page.locator("#welcome-go").click();
  await waitForActiveScreen(page, "source");
  // Pick Arc explicitly so the detect card matches what users see most.
  await page.locator('.source-card[data-name="arc"]').click();
  await page.locator("#source-next").click();
  await waitForActiveScreen(page, "detect");
  await shoot(page, "detect");
});

test("preview", async ({ page }) => {
  await bootHarness(page);
  await page.locator("#welcome-go").click();
  await waitForActiveScreen(page, "source");
  await page.locator('.source-card[data-name="arc"]').click();
  await page.locator("#source-next").click();
  await waitForActiveScreen(page, "detect");
  await page.locator("#detect-next").click();
  await waitForActiveScreen(page, "preview");
  await page.locator("#toggles .toggle").last().waitFor({ state: "visible" });
  await shoot(page, "preview");
});

test("backup-mode", async ({ page }) => {
  await bootHarness(page);
  await page.locator("#welcome-backup").click();
  await waitForActiveScreen(page, "backup-mode");
  await shoot(page, "backup-mode");
});

test("backup-export", async ({ page }) => {
  await bootHarness(page);
  await page.locator("#welcome-backup").click();
  await waitForActiveScreen(page, "backup-mode");
  await page.locator("#backup-mode-export").waitFor({ state: "visible" });
  await page.locator("#backup-mode-export").click();
  await waitForActiveScreen(page, "backup-export");
  await shoot(page, "backup-export");
});

test("backup-restore", async ({ page }) => {
  await bootHarness(page);
  await page.locator("#welcome-backup").click();
  await waitForActiveScreen(page, "backup-mode");
  await page.locator("#backup-mode-restore").waitFor({ state: "visible" });
  await page.locator("#backup-mode-restore").click();
  await waitForActiveScreen(page, "backup-restore");
  // Seed a chosen archive + manifest preview so the screen looks like
  // someone's mid-restore, not blank.
  await page.evaluate(() => {
    const bs = window.__shoot.backupState;
    bs.restoreArchivePath = "~/Downloads/zen-backup-2026-05-07.zenbackup";
    bs.restoreManifest = {
      format_version: 1,
      browser2zen_version: "1.2.0",
      source_profile_name: "Default (release)",
      exported_at: "2026-05-07T13:43:21Z",
      included: ["workspaces", "browsing", "cookies", "favicons"],
    };
    const pathLabel = document.getElementById("backup-restore-path");
    pathLabel.textContent = bs.restoreArchivePath;
    pathLabel.classList.remove("muted");
    document.getElementById("backup-restore-manifest-row").style.display = "";
    const manifestNode = document.getElementById("backup-restore-manifest");
    while (manifestNode.firstChild) manifestNode.removeChild(manifestNode.firstChild);
    const made = (cls, text) => {
      const d = document.createElement("div");
      d.className = cls;
      d.textContent = text;
      return d;
    };
    manifestNode.appendChild(made("backup-manifest-name", "From: Default (release)"));
    manifestNode.appendChild(made("muted", "Exported 2026-05-07 · " +
      bs.restoreManifest.included.join(", ")));
    manifestNode.appendChild(made("muted", "Format v1 · browser2zen 1.2.0"));
  });
  await shoot(page, "backup-restore");
});

test("backup-done", async ({ page }) => {
  // The done screen only renders after a backup finishes. Hijack the
  // welcome-screen "Backup" button so a real user click runs finishOk
  // — clicks force layout/paint scheduling that page.evaluate alone
  // doesn't, and we get a clean done-screen capture.
  await bootHarness(page);
  await page.evaluate(() => {
    const btn = document.getElementById("welcome-backup");
    const fresh = btn.cloneNode(true);
    btn.replaceWith(fresh);
    fresh.addEventListener("click", () => {
      window.__shoot.backupState.exportOutputPath =
        "~/Downloads/zen-backup-2026-05-07.zenbackup";
      window.__shoot.finishOk({
        status: "done",
        kind: "export",
        archivePath: "~/Downloads/zen-backup-2026-05-07.zenbackup",
        bytesOut: 130_000_000,
        fileCount: 32,
      });
    });
  });
  await page.locator("#welcome-backup").click();
  await waitForActiveScreen(page, "done");
  await shoot(page, "backup-done");
});
