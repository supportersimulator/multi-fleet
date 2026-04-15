"""Component 1: Vital Signs Bar — real-time fleet health (heartbeat, latency, stress)."""

import time
from typing import Optional

from .base import TheatricalComponent


class VitalSignsBar(TheatricalComponent):
    """Horizontal bar showing fleet heartbeat, latency, stress, and uptime."""

    COMPONENT_NAME = "vital_signs"
    REFRESH_INTERVAL_S = 3

    def render(self, data: Optional[dict] = None) -> str:
        if not data:
            return self.render_unavailable("No fleet health data")

        nodes = data.get("nodes", [])
        daemon = data.get("daemon", {})
        messages = data.get("messages", {})
        uptime_s = daemon.get("uptime_s", 0)

        # Compute aggregate vitals
        total_nodes = len(nodes)
        online = sum(1 for n in nodes if n.get("status") == "online")
        avg_latency = self._compute_avg_latency(nodes)
        error_rate = self._compute_error_rate(messages)
        stress = self._compute_stress(messages, nodes)

        # Heartbeat state
        hb_color = "#22c55e" if online > 0 else "#ef4444"
        hb_label = f"{online}/{total_nodes} online"

        # Latency indicator
        lat_color = self._health_color(1.0 - min(avg_latency / 5000.0, 1.0))
        lat_label = f"{avg_latency:.0f}ms" if avg_latency > 0 else "---"

        # Stress level (0-1)
        stress_color = self._health_color(1.0 - stress)
        stress_pct = stress * 100

        # Error rate
        err_color = self._health_color(1.0 - min(error_rate, 1.0))

        return f'''<div class="tc-card" id="tc-vital-signs">
  <div class="tc-card-header">Vital Signs</div>
  <div class="tc-vital-bar">
    <div class="tc-vital-item">
      <div class="tc-vital-icon" style="color:{hb_color};">&#9829;</div>
      <div class="tc-vital-label">Heartbeat</div>
      <div class="tc-vital-value" style="color:{hb_color};">{self._esc(hb_label)}</div>
    </div>
    <div class="tc-vital-divider"></div>
    <div class="tc-vital-item">
      <div class="tc-vital-icon" style="color:{lat_color};">&#9201;</div>
      <div class="tc-vital-label">Avg Latency</div>
      <div class="tc-vital-value" style="color:{lat_color};">{self._esc(lat_label)}</div>
    </div>
    <div class="tc-vital-divider"></div>
    <div class="tc-vital-item">
      <div class="tc-vital-icon" style="color:{stress_color};">&#9889;</div>
      <div class="tc-vital-label">Stress</div>
      <div class="tc-vital-value">{self._bar_html(stress, 80, stress_color)} {stress_pct:.0f}%</div>
    </div>
    <div class="tc-vital-divider"></div>
    <div class="tc-vital-item">
      <div class="tc-vital-icon" style="color:{err_color};">&#9888;</div>
      <div class="tc-vital-label">Errors</div>
      <div class="tc-vital-value" style="color:{err_color};">{messages.get("errors", 0)}</div>
    </div>
    <div class="tc-vital-divider"></div>
    <div class="tc-vital-item">
      <div class="tc-vital-icon" style="color:#06b6d4;">&#8987;</div>
      <div class="tc-vital-label">Uptime</div>
      <div class="tc-vital-value">{self._ago(uptime_s)}</div>
    </div>
  </div>
</div>'''

    def _compute_avg_latency(self, nodes: list) -> float:
        """Average last_seen_s across peers (proxy for latency)."""
        peer_seen = [n.get("last_seen_s", 0) for n in nodes if not n.get("is_self")]
        if not peer_seen:
            return 0.0
        return (sum(peer_seen) / len(peer_seen)) * 1000  # convert to ms-ish

    def _compute_error_rate(self, messages: dict) -> float:
        """Error rate as fraction of total messages."""
        total = messages.get("sent", 0) + messages.get("received", 0)
        errors = messages.get("errors", 0)
        if total == 0:
            return 0.0
        return errors / total

    def _compute_stress(self, messages: dict, nodes: list) -> float:
        """Stress metric: high errors + offline nodes = high stress (0-1)."""
        err_rate = self._compute_error_rate(messages)
        total_nodes = len(nodes) or 1
        offline = sum(1 for n in nodes if n.get("status") != "online")
        offline_rate = offline / total_nodes
        return min(1.0, err_rate * 2 + offline_rate * 0.5)
