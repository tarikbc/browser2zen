"""Microsoft Edge extractor."""

from __future__ import annotations

from .chromium import ChromiumExtractor


class EdgeExtractor(ChromiumExtractor):
    name = "edge"
    display_name = "Microsoft Edge"

    user_data_dirs_macos = (
        "Library/Application Support/Microsoft Edge",
    )
    user_data_dirs_windows = (
        "AppData/Local/Microsoft/Edge/User Data",
    )
    user_data_dirs_linux = (
        ".config/microsoft-edge",
        # Flatpak (com.microsoft.Edge).
        ".var/app/com.microsoft.Edge/config/microsoft-edge",
    )

    keychain_service = "Microsoft Edge Safe Storage"
    keychain_account = "Microsoft Edge"

    macos_app_name = "Microsoft Edge"
    macos_process_paths = ("Microsoft Edge.app/Contents/MacOS/Microsoft Edge",)
    windows_process_names = ("msedge.exe",)
