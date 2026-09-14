"""Single source of truth for the browser2zen app version.

Imported by:
- ``app.bridge.Bridge.version()`` so the frontend can render it.
- ``build/browser2zen.spec`` so the macOS Info.plist and Windows file
  metadata reflect the same value.

Update this file when cutting a new tag; the matching ``v<version>``
git tag triggers the GitHub Actions release workflow.
"""

VERSION = "1.2.7"
