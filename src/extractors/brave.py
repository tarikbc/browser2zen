"""Brave browser extractor."""

from __future__ import annotations

from .chromium import ChromiumExtractor


class BraveExtractor(ChromiumExtractor):
    name = "brave"
    display_name = "Brave"

    user_data_dirs_macos = (
        "Library/Application Support/BraveSoftware/Brave-Browser",
    )
    user_data_dirs_windows = (
        "AppData/Local/BraveSoftware/Brave-Browser/User Data",
    )
    user_data_dirs_linux = (
        ".config/BraveSoftware/Brave-Browser",
        # Brave Origin — the minimalist/debloated build shipped by the
        # official ``brave-origin-bin`` AUR package. It keeps its own
        # User Data dir under ``BraveSoftware/Brave-Origin[-Beta|-Nightly]``
        # instead of ``Brave-Browser``. Snap / Flatpak variants mirror the
        # ``Brave-Browser`` layout, so the same container paths apply.
        ".config/BraveSoftware/Brave-Origin",
        ".config/BraveSoftware/Brave-Origin-Beta",
        ".config/BraveSoftware/Brave-Origin-Nightly",
        # Snap (the `brave` snap keeps config under current/).
        "snap/brave/current/.config/BraveSoftware/Brave-Browser",
        # Flatpak (com.brave.Browser).
        ".var/app/com.brave.Browser/config/BraveSoftware/Brave-Browser",
    )

    keychain_service = "Brave Safe Storage"
    keychain_account = "Brave"

    macos_app_name = "Brave Browser"
    macos_process_paths = ("Brave Browser.app/Contents/MacOS/Brave Browser",)
    windows_process_names = ("brave.exe",)
