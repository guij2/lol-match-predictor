"""
Sample overlay with mock data for screenshots / thesis.

Opens two overlay instances side-by-side:
  1. Compact view  — shows the arc gauge with a probability.
  2. Expanded view  — shows the arc gauge + recommendation cards.

Both use fake data (no LoL client needed).
Press Escape or click ✕ to close.

Usage:
    pythonw sample_overlay.pyw        (no console)
    python  sample_overlay.pyw        (with console for debugging)
"""

import sys
import os

# Add project root so "overlay.native.overlay" imports work when run standalone
sys.path.insert(0, os.path.dirname(__file__))

from overlay import (
    QApplication, QFont, QFontDatabase, Qt,
    ModernOverlay, Recommendation, Colors,
    WINDOW_WIDTH, WINDOW_HEIGHT_COMPACT, WINDOW_HEIGHT_EXPANDED,
)


# ── Mock data ────────────────────────────────────────────────────────────────

MOCK_PREDICTION_COMPACT = {
    'status': 'ok',
    'probability': 0.67,           # 67% blue win → player is blue → 67% shown
    'player_team': 'blue',
    'game_time': 942.0,            # 15:42
    'time_formatted': '15:42',
    'recommendations': [
        Recommendation(name='Barão', delta_prob=0.054),
        Recommendation(name='Torre', delta_prob=0.031),
        Recommendation(name='Dragão', delta_prob=0.022),
    ],
}

MOCK_PREDICTION_EXPANDED = {
    'status': 'ok',
    'probability': 0.65,           # 65% blue win → player is red → 35% shown
    'player_team': 'red',
    'game_time': 1587.0,           # 26:27
    'time_formatted': '26:27',
    'recommendations': [
        Recommendation(name='Barão', delta_prob=0.072),
        Recommendation(name='Elder', delta_prob=0.048),
        Recommendation(name='Teamfight (2 kills)', delta_prob=0.035),
    ],
}


# ── Patched overlay that uses mock data instead of the real API ──────────────

class MockOverlay(ModernOverlay):
    """Overlay that skips model loading and API polling."""

    def __init__(self, mock_data: dict, start_expanded: bool = False):
        self._mock_data = mock_data
        self._start_expanded = start_expanded
        # __init__ calls _start_polling → _fetch_prediction, so mock_data
        # must be set before calling super().__init__().
        super().__init__()

    # Skip model/feature loading entirely
    def _load_predictor(self):
        pass

    def _start_polling(self):
        """Instead of polling the API, inject mock data once."""
        self._on_prediction(self._mock_data)
        # Auto-expand if requested (after a tiny delay so layout settles)
        if self._start_expanded and self._current_recommendations:
            from PySide6.QtCore import QTimer
            QTimer.singleShot(100, self._toggle_expand)

    def _fetch_prediction(self):
        """No-op — we already injected data."""
        pass


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    app = QApplication(sys.argv)
    app.setStyle('Fusion')

    font_family = "Segoe UI"
    available = QFontDatabase.families()
    if "Segoe UI Variable" in available:
        font_family = "Segoe UI Variable"
    font = QFont(font_family, 10)
    font.setHintingPreference(QFont.PreferNoHinting)
    app.setFont(font)

    screen = app.primaryScreen().geometry()

    # ── Instance 1: compact (blue team, 67%) ─────────────────────────────
    overlay_compact = MockOverlay(MOCK_PREDICTION_COMPACT, start_expanded=False)
    overlay_compact.move(screen.width() - WINDOW_WIDTH - 30, 30)
    overlay_compact.show()

    # ── Instance 2: expanded (red team, 58%) ─────────────────────────────
    overlay_expanded = MockOverlay(MOCK_PREDICTION_EXPANDED, start_expanded=True)
    overlay_expanded.move(
        screen.width() - WINDOW_WIDTH - 30,
        30 + WINDOW_HEIGHT_COMPACT + 40,
    )
    overlay_expanded.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
