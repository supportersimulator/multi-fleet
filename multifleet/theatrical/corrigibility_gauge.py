"""Component 8: Corrigibility Gauge — current corrigibility score with trend."""

import math
import time
from typing import Optional

from .base import TheatricalComponent


class CorrigibilityGauge(TheatricalComponent):
    """Semicircular gauge showing system corrigibility score and trend history."""

    COMPONENT_NAME = "corrigibility_gauge"
    REFRESH_INTERVAL_S = 30

    def render(self, data: Optional[dict] = None) -> str:
        if not data:
            return self.render_unavailable("No corrigibility data")

        score = data.get("score", 0.0)
        trend = data.get("trend", [])  # list of {timestamp, score}
        factors = data.get("factors", {})
        last_check = data.get("last_check")

        # Gauge arc (semicircle from 180 to 0 degrees)
        # Score 0-1 maps to angle 180 (left) to 0 (right)
        angle = 180 - (score * 180)
        # Convert to SVG arc endpoint
        rad = math.radians(angle)
        end_x = 100 + 70 * math.cos(rad)
        end_y = 100 - 70 * math.sin(rad)

        # Gauge color
        if score >= 0.8:
            gauge_color = "#22c55e"
            label = "High"
        elif score >= 0.6:
            gauge_color = "#eab308"
            label = "Moderate"
        elif score >= 0.4:
            gauge_color = "#f97316"
            label = "Low"
        else:
            gauge_color = "#ef4444"
            label = "Critical"

        # SVG gauge
        gauge_svg = f'''
<svg viewBox="0 0 200 120" xmlns="http://www.w3.org/2000/svg" style="width:100%;max-height:110px;">
  <!-- Background arc -->
  <path d="M 30 100 A 70 70 0 0 1 170 100" fill="none" stroke="#334155" stroke-width="12"
        stroke-linecap="round"/>
  <!-- Score arc -->
  <path d="M 30 100 A 70 70 0 {1 if score > 0.5 else 0} 1 {end_x:.1f} {end_y:.1f}"
        fill="none" stroke="{gauge_color}" stroke-width="12" stroke-linecap="round"/>
  <!-- Needle -->
  <line x1="100" y1="100" x2="{end_x:.1f}" y2="{end_y:.1f}"
        stroke="#e2e8f0" stroke-width="2" stroke-linecap="round"/>
  <circle cx="100" cy="100" r="4" fill="#e2e8f0"/>
  <!-- Score text -->
  <text x="100" y="90" text-anchor="middle" fill="{gauge_color}"
        font-size="24" font-weight="700">{score:.0%}</text>
  <text x="100" y="108" text-anchor="middle" fill="#94a3b8" font-size="11">{label}</text>
  <!-- Scale labels -->
  <text x="25" y="115" fill="#64748b" font-size="9">0%</text>
  <text x="170" y="115" fill="#64748b" font-size="9">100%</text>
</svg>'''

        # Trend sparkline
        trend_svg = ""
        if trend and len(trend) >= 2:
            points = []
            min_t = min(t.get("timestamp", 0) for t in trend)
            max_t = max(t.get("timestamp", 0) for t in trend)
            t_range = max_t - min_t or 1
            for i, t in enumerate(trend[-20:]):
                x = ((t.get("timestamp", 0) - min_t) / t_range) * 180 + 10
                y = 30 - (t.get("score", 0) * 25)
                points.append(f"{x:.1f},{y:.1f}")
            polyline = " ".join(points)
            trend_svg = f'''
<div class="tc-trend-wrap">
  <svg viewBox="0 0 200 35" style="width:100%;height:35px;" xmlns="http://www.w3.org/2000/svg">
    <line x1="10" y1="30" x2="190" y2="30" stroke="#334155" stroke-width="0.5"/>
    <line x1="10" y1="5" x2="190" y2="5" stroke="#334155" stroke-width="0.5" stroke-dasharray="2,2"/>
    <polyline points="{polyline}" fill="none" stroke="{gauge_color}" stroke-width="1.5"/>
  </svg>
  <span class="tc-muted">24h trend</span>
</div>'''

        # Factor breakdown
        factor_items = []
        for name, value in factors.items():
            try:
                v = float(value)
                f_color = self._health_color(v)
                factor_items.append(
                    f'<div class="tc-factor-row">'
                    f'<span class="tc-muted">{self._esc(name)}</span>'
                    f'{self._bar_html(v, 60, f_color)}'
                    f'<span style="color:{f_color};">{v:.0%}</span>'
                    f'</div>'
                )
            except (ValueError, TypeError):
                pass
        factors_html = "".join(factor_items) if factor_items else ""

        ts_str = self._format_ts(last_check) if last_check else "---"

        # Gate history section — shows recent gate firings (pass/fail/override)
        gate_history = data.get("gate_history", [])
        gate_html = ""
        if gate_history:
            gate_items = []
            for g in gate_history[:8]:  # Show last 8 gate activations
                passed = g.get("passed", True)
                gate_name = self._esc(g.get("gate", "?"))
                summary = self._esc(g.get("summary", ""))
                failures = g.get("failures", [])
                ts_gate = self._format_ts(g.get("timestamp"))
                dur = g.get("duration_ms", 0)

                if passed:
                    icon = '<span style="color:#22c55e;">&#10003;</span>'
                    border_color = "#22c55e"
                else:
                    icon = '<span style="color:#ef4444;">&#10007;</span>'
                    border_color = "#ef4444"

                failure_html = ""
                if failures:
                    fail_names = ", ".join(self._esc(f) for f in failures)
                    failure_html = (
                        f'<div style="color:#ef4444;font-size:0.7rem;'
                        f'margin-top:2px;">Blocked: {fail_names}</div>'
                    )

                gate_items.append(
                    f'<div style="padding:0.3rem 0;border-left:2px solid {border_color};'
                    f'padding-left:0.5rem;margin-bottom:0.3rem;">'
                    f'<div style="display:flex;align-items:center;gap:0.3rem;'
                    f'font-size:0.8rem;">'
                    f'{icon} <span style="font-weight:600;">{gate_name}</span>'
                    f'<span class="tc-ts">{ts_gate}</span>'
                    f'<span class="tc-muted">{dur:.0f}ms</span>'
                    f'</div>'
                    f'{failure_html}'
                    f'</div>'
                )
            gate_html = (
                f'<div style="padding:0.3rem 0.75rem 0.5rem;">'
                f'<div class="tc-muted" style="margin-bottom:0.3rem;'
                f'text-transform:uppercase;font-size:0.7rem;">Gate Activity</div>'
                f'{"".join(gate_items)}'
                f'</div>'
            )

        # Recent disagreements section
        disagreements = data.get("recent_disagreements", [])
        disagree_html = ""
        if disagreements:
            disagree_items = []
            for d in disagreements[:3]:
                topic = self._esc(d.get("topic", "?"))
                pos_a = self._esc(d.get("surgeon_a_position", ""))[:80]
                pos_b = self._esc(d.get("surgeon_b_position", ""))[:80]
                resolution = self._esc(d.get("resolution", ""))
                ts_d = self._format_ts(d.get("ts"))
                disagree_items.append(
                    f'<div style="padding:0.3rem 0;border-bottom:1px solid #1e293b;">'
                    f'<div style="font-size:0.8rem;font-weight:500;color:#f97316;">'
                    f'&#9888; {topic}</div>'
                    f'<div style="font-size:0.7rem;color:#94a3b8;margin-top:2px;">'
                    f'A: {pos_a}</div>'
                    f'<div style="font-size:0.7rem;color:#94a3b8;">'
                    f'B: {pos_b}</div>'
                    f'<div style="font-size:0.7rem;color:#22c55e;margin-top:2px;">'
                    f'Resolution: {resolution} '
                    f'<span class="tc-ts">{ts_d}</span></div>'
                    f'</div>'
                )
            disagree_html = (
                f'<div style="padding:0.3rem 0.75rem 0.5rem;">'
                f'<div class="tc-muted" style="margin-bottom:0.3rem;'
                f'text-transform:uppercase;font-size:0.7rem;">Disagreements</div>'
                f'{"".join(disagree_items)}'
                f'</div>'
            )

        # Impact stats
        stats = data.get("stats", {})
        stats_html = ""
        if stats.get("total_decisions", 0) > 0:
            total_d = stats["total_decisions"]
            changed_d = stats.get("changed_by_surgeon", 0)
            stats_html = (
                f'<div style="padding:0.2rem 0.75rem;font-size:0.75rem;'
                f'color:#94a3b8;display:flex;gap:1rem;">'
                f'<span>{total_d} decisions</span>'
                f'<span style="color:#22c55e;">{changed_d} changed by surgeon</span>'
                f'<span>{stats.get("total_disagreements", 0)} disagreements</span>'
                f'</div>'
            )

        return f'''<div class="tc-card" id="tc-corrigibility-gauge">
  <div class="tc-card-header">
    Corrigibility Gauge
    <span class="tc-muted">checked {ts_str}</span>
  </div>
  <div class="tc-gauge-wrap">{gauge_svg}</div>
  {trend_svg}
  <div class="tc-factors">{factors_html}</div>
  {stats_html}
  {gate_html}
  {disagree_html}
</div>'''
