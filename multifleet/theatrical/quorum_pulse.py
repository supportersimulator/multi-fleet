"""Component 5: Quorum Pulse — bacterial quorum sensing signal flow visualization.

Models fleet consensus as bacterial quorum sensing with 4 signal types:
  Las  — Leadership signals (chief directives)
  Rhl  — Resource health levels (node capacity)
  PQS  — Peer quality signals (channel health)
  IQS  — Integration quality signals (cross-rebuttal consensus)
"""

import time
from typing import Optional

from .base import TheatricalComponent


class QuorumPulse(TheatricalComponent):
    """Animated quorum sensing visualization with 4 signal channels."""

    COMPONENT_NAME = "quorum_pulse"
    REFRESH_INTERVAL_S = 5

    SIGNALS = {
        "Las": {"label": "Leadership", "color": "#3b82f6", "icon": "&#9733;"},
        "Rhl": {"label": "Resource Health", "color": "#22c55e", "icon": "&#9829;"},
        "PQS": {"label": "Peer Quality", "color": "#a855f7", "icon": "&#9830;"},
        "IQS": {"label": "Integration", "color": "#f97316", "icon": "&#9827;"},
    }

    def render(self, data: Optional[dict] = None) -> str:
        if not data:
            return self.render_unavailable("No quorum data")

        # Extract signal levels from fleet state
        signals = self._compute_signals(data)
        quorum_reached = signals.get("quorum_reached", False)
        threshold = signals.get("threshold", 0.6)

        # Signal bars
        signal_bars = []
        for key, meta in self.SIGNALS.items():
            level = signals.get(key, 0.0)
            color = meta["color"]
            icon = meta["icon"]
            label = meta["label"]
            above = level >= threshold

            bar_color = color if above else "#64748b"
            status_dot = self._dot_html("#22c55e") if above else self._dot_html("#64748b")

            signal_bars.append(f'''
<div class="tc-signal-row">
  <div class="tc-signal-label">
    <span>{icon}</span> <span style="color:{color};">{self._esc(key)}</span>
    <span class="tc-muted">{self._esc(label)}</span>
  </div>
  <div class="tc-signal-bar-wrap">
    {self._bar_html(level, 120, bar_color)}
    <span class="tc-signal-pct">{level:.0%}</span>
    {status_dot}
  </div>
</div>''')

        bars_html = "".join(signal_bars)

        # Quorum status
        if quorum_reached:
            q_badge = '<span class="tc-badge tc-badge-green">Quorum Reached</span>'
        else:
            q_badge = '<span class="tc-badge tc-badge-yellow">Below Quorum</span>'

        # Threshold line indicator
        threshold_pct = threshold * 100

        # Animated pulse SVG
        pulse_color = "#22c55e" if quorum_reached else "#eab308"
        pulse_svg = f'''
<svg viewBox="0 0 200 40" style="width:100%;height:40px;" xmlns="http://www.w3.org/2000/svg">
  <line x1="0" y1="20" x2="30" y2="20" stroke="{pulse_color}" stroke-width="2"/>
  <polyline points="30,20 40,5 50,35 60,10 70,30 80,20" fill="none"
    stroke="{pulse_color}" stroke-width="2">
    <animate attributeName="points"
      values="30,20 40,5 50,35 60,10 70,30 80,20;30,20 40,8 50,32 60,12 70,28 80,20;30,20 40,5 50,35 60,10 70,30 80,20"
      dur="1.5s" repeatCount="indefinite"/>
  </polyline>
  <line x1="80" y1="20" x2="200" y2="20" stroke="{pulse_color}" stroke-width="2"
    stroke-dasharray="4,4">
    <animate attributeName="stroke-dashoffset" from="0" to="-16" dur="1s" repeatCount="indefinite"/>
  </line>
  <text x="100" y="14" fill="#94a3b8" font-size="9">threshold: {threshold_pct:.0f}%</text>
</svg>'''

        return f'''<div class="tc-card" id="tc-quorum-pulse">
  <div class="tc-card-header">
    Quorum Pulse
    {q_badge}
  </div>
  <div class="tc-pulse-wave">{pulse_svg}</div>
  <div class="tc-signal-grid">{bars_html}</div>
</div>'''

    def _compute_signals(self, data: dict) -> dict:
        """Derive quorum signal levels from fleet state."""
        nodes = data.get("nodes", [])
        messages = data.get("messages", {})
        gains_gate = data.get("gains_gate", {})
        rebuttals = data.get("rebuttals", {})

        total_nodes = len(nodes) or 1
        online = sum(1 for n in nodes if n.get("status") == "online")

        # Las: leadership signal — chief online and healthy
        chief_online = any(
            n.get("is_self") and n.get("status") == "online" for n in nodes
        )
        las = 1.0 if chief_online else 0.3

        # Rhl: resource health — fraction of nodes online with good channel scores
        healthy = sum(
            1 for n in nodes
            if n.get("status") == "online" and n.get("channel_score") not in ("?", None)
        )
        rhl = healthy / total_nodes

        # PQS: peer quality — based on message success rate
        total_msgs = messages.get("sent", 0) + messages.get("received", 0)
        errors = messages.get("errors", 0)
        pqs = 1.0 - (errors / max(total_msgs, 1))

        # IQS: integration quality — gains gate passing + rebuttals resolved
        gate_passed = 1.0 if gains_gate.get("passed") else 0.3
        active_rebuttals = rebuttals.get("active", 0) if isinstance(rebuttals, dict) else 0
        rebuttal_factor = 1.0 if active_rebuttals == 0 else max(0.3, 1.0 - active_rebuttals * 0.15)
        iqs = (gate_passed + rebuttal_factor) / 2.0

        threshold = 0.6
        all_above = all(v >= threshold for v in [las, rhl, pqs, iqs])

        return {
            "Las": las,
            "Rhl": rhl,
            "PQS": pqs,
            "IQS": iqs,
            "threshold": threshold,
            "quorum_reached": all_above,
        }
