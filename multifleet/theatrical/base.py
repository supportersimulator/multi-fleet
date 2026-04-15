"""Base class for all theatrical dashboard components."""

import html
import time
from datetime import datetime, timezone
from typing import Optional


class TheatricalComponent:
    """Base class providing shared rendering utilities for dashboard components.

    Subclasses implement render(data) -> str returning an HTML fragment.
    """

    COMPONENT_NAME: str = "base"
    REFRESH_INTERVAL_S: int = 5

    # ── Shared color palette (Superset dark theme) ──
    COLORS = {
        "bg_primary": "#0f172a",
        "bg_card": "#1e293b",
        "bg_card_hover": "#334155",
        "border": "#334155",
        "text_primary": "#e2e8f0",
        "text_secondary": "#94a3b8",
        "text_muted": "#64748b",
        "accent_blue": "#3b82f6",
        "accent_cyan": "#06b6d4",
        "accent_green": "#22c55e",
        "accent_yellow": "#eab308",
        "accent_red": "#ef4444",
        "accent_purple": "#a855f7",
        "accent_orange": "#f97316",
        "accent_pink": "#ec4899",
    }

    def render(self, data: Optional[dict] = None) -> str:
        """Render component HTML. Override in subclasses.

        Args:
            data: Component-specific data dict. None means unavailable.

        Returns:
            HTML string fragment (not a full page).
        """
        raise NotImplementedError

    def render_unavailable(self, message: str = "Data unavailable") -> str:
        """Render graceful unavailable state."""
        return (
            f'<div class="tc-card tc-unavailable" id="tc-{self.COMPONENT_NAME}">'
            f'<div class="tc-card-header">{self._esc(self.COMPONENT_NAME.replace("_", " ").title())}</div>'
            f'<div class="tc-card-body tc-muted">{self._esc(message)}</div>'
            f'</div>'
        )

    # ── Utilities ──

    @staticmethod
    def _esc(text) -> str:
        """HTML-escape a value."""
        return html.escape(str(text)) if text else ""

    @staticmethod
    def _format_ts(ts) -> str:
        """Format unix timestamp to HH:MM:SS UTC."""
        if not ts:
            return "---"
        try:
            dt = datetime.fromtimestamp(float(ts), tz=timezone.utc)
            return dt.strftime("%H:%M:%S")
        except (ValueError, TypeError, OSError):
            return str(ts)

    @staticmethod
    def _ago(seconds) -> str:
        """Format seconds-ago as human string."""
        try:
            s = int(seconds)
        except (ValueError, TypeError):
            return "?"
        if s < 0:
            return "future"
        if s < 60:
            return f"{s}s"
        if s < 3600:
            return f"{s // 60}m {s % 60}s"
        return f"{s // 3600}h {(s % 3600) // 60}m"

    @staticmethod
    def _health_color(value: float, thresholds: tuple = (0.7, 0.4)) -> str:
        """Return green/yellow/red based on value (0-1) and thresholds."""
        good, warn = thresholds
        if value >= good:
            return "#22c55e"
        if value >= warn:
            return "#eab308"
        return "#ef4444"

    @staticmethod
    def _bar_html(value: float, width_px: int = 100, color: str = "#3b82f6",
                  bg: str = "#334155") -> str:
        """Render a horizontal progress bar."""
        pct = max(0.0, min(1.0, value)) * 100
        return (
            f'<div style="width:{width_px}px;height:8px;background:{bg};'
            f'border-radius:4px;overflow:hidden;display:inline-block;">'
            f'<div style="width:{pct:.0f}%;height:100%;background:{color};'
            f'border-radius:4px;"></div></div>'
        )

    @staticmethod
    def _dot_html(color: str) -> str:
        """Render a colored status dot."""
        return f'<span style="color:{color};font-size:1.2em;">&#9679;</span>'
