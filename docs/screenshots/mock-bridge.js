// Mock implementation of the PyWebView JS bridge. Injected into the
// page via Playwright's addInitScript before navigation, so app.js sees
// it as an already-ready bridge as soon as we dispatch pywebviewready.
//
// The data shapes here mirror what app/bridge.py returns for a happy
// path: both browsers detected, no Zen running, one Zen profile, etc.
// Add a new shape here if you want a new screen in screens.spec.ts.

window.__SHOOT__ = true;

const ZEN_PROFILE = {
  path: "/Users/demo/Library/Application Support/zen/Profiles/o9i57a6u.Default (release)",
  name: "Default (release)",
  isRelease: true,
};

const SOURCES = [
  { name: "arc",     displayName: "Arc",     installed: true,  running: false },
  { name: "chrome",  displayName: "Chrome",  installed: true,  running: false },
  { name: "edge",    displayName: "Edge",    installed: false, running: false },
  { name: "brave",   displayName: "Brave",   installed: true,  running: false },
  { name: "firefox", displayName: "Firefox", installed: true,  running: false },
  { name: "safari",  displayName: "Safari",  installed: true,  running: false },
];

const CATEGORIES = [
  { id: "workspaces", label: "Workspaces, pinned tabs, folders", default: true,  caveat: "" },
  { id: "browsing",   label: "Browsing data (bookmarks + history)", default: true,  caveat: "" },
  { id: "cookies",    label: "Login state",                       default: true,  caveat: "" },
  { id: "favicons",   label: "Favicons",                          default: true,  caveat: "" },
  { id: "passwords",  label: "Saved passwords",                   default: false,
    caveat: "If a master password is set on the source profile, the same one is required on the target." },
  { id: "prefs",      label: "Preferences",                       default: false,
    caveat: "A few prefs reference absolute paths from the source machine." },
  { id: "extensions", label: "Extensions",                        default: false,
    caveat: "Extensions need to be compatible with the target machine's Zen version." },
  { id: "mods",       label: "Zen Mods (UI customisations)",      default: true,
    caveat: "If a mod targets a specific Zen version, the same Zen version is needed on the target." },
];

const ENV_REPORT = {
  sourceInstalled: true,
  sourceRunning: false,
  sourceProfiles: ["Default", "Work"],
  zenInstalled: true,
  zenRunning: false,
  zenProfiles: [ZEN_PROFILE],
  hasLz4: true,
};

window.pywebview = {
  api: {
    platform:                async () => "mac",
    version:                 async () => "1.2.2",
    list_sources:            async () => SOURCES,
    set_source:              async (name) => {
      const found = SOURCES.find((s) => s.name === name) || SOURCES[0];
      return { ok: true, ...found };
    },
    current_source:          async () => SOURCES[0],
    check_env:               async () => ENV_REPORT,
    is_zen_running:          async () => false,
    list_zen_profiles_json:  async () => [ZEN_PROFILE],
    list_backup_categories:  async () => CATEGORIES,
    preview_zen_backup:      async () => ({
      ok: true,
      manifest: {
        format_version: 1,
        browser2zen_version: "1.2.0",
        source_profile_name: "Default (release)",
        exported_at: "2026-05-07T13:43:21Z",
        included: ["workspaces", "browsing", "cookies", "favicons"],
      },
      archive_size: 17_300_000,
    }),
    quit_browser:            async () => ({ ok: true }),
    quit_app:                async () => null,
    open_url:                async () => true,
    open_path_in_finder:     async () => true,
    copy_to_clipboard:       async () => true,
    launch_zen:              async () => true,
    list_backups:            async () => [],
    drain_progress:          async () => ({ events: [], state: { status: "idle" }, steps: [], labels: {} }),
    get_step_metadata:       async () => ({ steps: [], labels: {} }),
    // Migration-side methods: tests don't drive these screens via the
    // happy path, but app.js may call them on screen entry.
    preview:                 async () => ({
      spaces: [
        { name: "Work",    icon: "💼", pinnedCount: 8, openCount: 2, folderCount: 1, essentialCount: 2, color: [94, 134, 255] },
        { name: "Reading", icon: "📚", pinnedCount: 5, openCount: 1, folderCount: 0, essentialCount: 1, color: [120, 90, 220] },
        { name: "Hobby",   icon: "🎧", pinnedCount: 4, openCount: 1, folderCount: 1, essentialCount: 2, color: [40, 170, 120] },
      ],
      pinnedTotal: 17, openTotal: 4, folderTotal: 2, bookmarkTotal: 19,
      faviconMatchEstimate: 15, historyRowsEstimate: 42000,
      cookiesEstimate: 12000, passwordsEstimate: 320,
    }),
    start_migration:         async () => ({ ok: true }),
    start_zen_export:        async () => ({ ok: true }),
    start_zen_restore:       async () => ({ ok: true }),
    choose_path:             async () => null,
    restore_backup:          async () => ({ ok: true }),
    delete_backup:           async () => ({ ok: true }),
  },
};
