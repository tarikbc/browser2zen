"""Google Chrome extractor."""

from __future__ import annotations

from .chromium import ChromiumExtractor


class ChromeExtractor(ChromiumExtractor):
    name = "chrome"
    display_name = "Chrome"

    user_data_dirs_macos = (
        "Library/Application Support/Google/Chrome",
    )
    user_data_dirs_windows = (
        "AppData/Local/Google/Chrome/User Data",
    )
    user_data_dirs_linux = (
        ".config/google-chrome",
        # Flatpak (com.google.Chrome).
        ".var/app/com.google.Chrome/config/google-chrome",
    )

    keychain_service = "Chrome Safe Storage"
    keychain_account = "Chrome"

    macos_app_name = "Google Chrome"
    macos_process_paths = ("Google Chrome.app/Contents/MacOS/Google Chrome",)
    windows_process_names = ("chrome.exe",)
