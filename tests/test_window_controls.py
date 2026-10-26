"""Window-control bridge methods.

These back the macOS traffic-light buttons. They must no-op without a
window and delegate correctly once one is attached, and the maximize
toggle must alternate between maximize and restore.
"""

from __future__ import annotations


class _FakeWindow:
    def __init__(self):
        self.calls: list[str] = []
        self.sizes: list[tuple[int, int]] = []

    def minimize(self):
        self.calls.append("minimize")

    def resize(self, w, h):
        self.sizes.append((w, h))


def _bridge():
    from app.bridge import Bridge
    return Bridge()


def test_controls_noop_without_window():
    b = _bridge()
    # No window attached: these must not raise.
    b.minimize_app()
    b.toggle_maximize_app()


def test_minimize_delegates():
    b = _bridge()
    win = _FakeWindow()
    b.set_window(win)
    b.minimize_app()
    assert win.calls == ["minimize"]


def test_maximize_toggles():
    b = _bridge()
    win = _FakeWindow()
    b.set_window(win)
    b.toggle_maximize_app()   # grow
    b.toggle_maximize_app()   # back to the design floor
    b.toggle_maximize_app()   # grow again
    grown, restored, grown_again = win.sizes
    assert restored == b._DEFAULT_SIZE
    assert grown == grown_again
    assert grown[0] >= b._DEFAULT_SIZE[0] and grown[1] >= b._DEFAULT_SIZE[1]
    assert grown != b._DEFAULT_SIZE
