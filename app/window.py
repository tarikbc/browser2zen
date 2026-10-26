"""
PyWebView window setup.

Single frameless window, 760×580 by default and resizable up from there,
vibrancy on macOS for the modern translucent chrome. Frontend assets are
served by pywebview's built-in HTTP server so we avoid WKWebView's
local-file restrictions.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import webview

from .bridge import Bridge

logger = logging.getLogger(__name__)


def _frontend_dir() -> Path:
    """Find the bundled frontend assets, both in dev and PyInstaller modes."""
    here = Path(__file__).resolve().parent / "frontend"
    if here.is_dir():
        return here
    # PyInstaller --onedir: assets are next to the binary in Contents/Resources.
    if hasattr(sys, "_MEIPASS"):
        bundled = Path(sys._MEIPASS) / "app" / "frontend"
        if bundled.is_dir():
            return bundled
    raise RuntimeError(f"Frontend directory not found (tried {here})")


def launch(debug: bool = False) -> None:
    bridge = Bridge()

    frontend = _frontend_dir()
    # Use a file:// URL (simpler and avoids the bottle HTTP server thread).
    # We don't need cross-origin fetches; everything goes through the JS bridge.
    entry = (frontend / "index.html").as_uri()

    is_mac = sys.platform == "darwin"

    window = webview.create_window(
        title="browser2zen",
        url=entry,
        js_api=bridge,
        width=760,
        height=580,
        # Keep the designed layout as the floor, but let the window grow so
        # tall screens (the preview with many spaces) are not clipped.
        min_size=(760, 580),
        resizable=True,
        # macOS: frameless window with vibrancy and our custom titlebar /
        # traffic lights. Windows: keep the OS-native title bar so we get
        # snap zones, Aero shake, multi-monitor DPI handling, and the
        # platform-correct close/minimize/maximize buttons for free.
        frameless=is_mac,
        # easy_drag=True makes the *entire* window a drag region. We want
        # only our titlebar to drag (CSS sets -webkit-app-region on it),
        # so this stays off.
        easy_drag=False,
        background_color="#0E0F12",
        vibrancy=is_mac,
    )
    bridge.set_window(window)

    # Inject the canonical version straight into the DOM as soon as the page
    # finishes loading. Doing this here (rather than relying on a JS-bridge
    # call from app.js) sidesteps any race between ``pywebviewready`` firing
    # and ``window.pywebview.api`` being populated.
    from .__version__ import VERSION

    def _on_loaded() -> None:
        try:
            window.evaluate_js(
                "(() => {"
                f"  const v = {VERSION!r};"
                "   document.body.dataset.appVersion = v;"
                "   const node = document.getElementById('ver');"
                "   if (node) node.textContent = 'browser2zen · v' + v;"
                "})();"
            )
        except Exception as exc:
            logger.warning(f"version injection failed: {exc}")

    window.events.loaded += _on_loaded

    webview.start(
        debug=debug,
        private_mode=False,
    )
