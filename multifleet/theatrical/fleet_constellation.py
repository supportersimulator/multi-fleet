"""Component 3: Fleet Constellation — node topology with connection quality."""

import math
from typing import Optional

from .base import TheatricalComponent


class FleetConstellation(TheatricalComponent):
    """SVG-based node topology showing nodes as stars with connection lines."""

    COMPONENT_NAME = "fleet_constellation"
    REFRESH_INTERVAL_S = 5

    NODE_COLORS = {
        "online": "#22c55e",
        "stale": "#eab308",
        "offline": "#ef4444",
    }

    def render(self, data: Optional[dict] = None) -> str:
        if not data:
            return self.render_unavailable("No topology data")

        nodes = data.get("nodes", [])
        if not nodes:
            return self.render_unavailable("No nodes discovered")

        # Layout nodes in a circle within SVG
        cx, cy, radius = 150, 120, 80
        n = len(nodes)
        positions = []
        for i in range(n):
            angle = (2 * math.pi * i / n) - math.pi / 2
            x = cx + radius * math.cos(angle)
            y = cy + radius * math.sin(angle)
            positions.append((x, y))

        # Draw connection lines between all node pairs
        lines = []
        for i in range(n):
            for j in range(i + 1, n):
                ni, nj = nodes[i], nodes[j]
                # Connection quality based on both nodes being online
                both_online = ni.get("status") == "online" and nj.get("status") == "online"
                color = "#334155" if not both_online else "#3b82f6"
                opacity = "0.8" if both_online else "0.2"
                dash = "" if both_online else 'stroke-dasharray="4,4"'
                x1, y1 = positions[i]
                x2, y2 = positions[j]
                lines.append(
                    f'<line x1="{x1:.0f}" y1="{y1:.0f}" x2="{x2:.0f}" '
                    f'y2="{y2:.0f}" stroke="{color}" stroke-width="1.5" '
                    f'opacity="{opacity}" {dash}/>'
                )

        # Draw nodes as circles with labels
        node_elements = []
        for i, node in enumerate(nodes):
            x, y = positions[i]
            status = node.get("status", "offline")
            color = self.NODE_COLORS.get(status, "#6b7280")
            name = self._esc(node.get("id", f"node-{i}"))
            sessions = node.get("sessions", 0)
            is_self = node.get("is_self", False)
            r = 18 if is_self else 14
            stroke_w = 3 if is_self else 1.5

            # Pulsing animation for self node
            pulse = (
                f'<circle cx="{x:.0f}" cy="{y:.0f}" r="{r + 6}" '
                f'fill="none" stroke="{color}" stroke-width="1" opacity="0.3">'
                f'<animate attributeName="r" from="{r + 2}" to="{r + 10}" '
                f'dur="2s" repeatCount="indefinite"/>'
                f'<animate attributeName="opacity" from="0.4" to="0" '
                f'dur="2s" repeatCount="indefinite"/>'
                f'</circle>'
            ) if is_self and status == "online" else ""

            # Channel score badge
            ch_score = node.get("channel_score", "?")
            score_el = ""
            if ch_score not in ("?", "self"):
                score_el = (
                    f'<text x="{x:.0f}" y="{y + r + 14:.0f}" '
                    f'text-anchor="middle" fill="#94a3b8" font-size="10">'
                    f'{self._esc(str(ch_score))}</text>'
                )

            node_elements.append(f'''
                {pulse}
                <circle cx="{x:.0f}" cy="{y:.0f}" r="{r}" fill="{color}"
                  fill-opacity="0.15" stroke="{color}" stroke-width="{stroke_w}"/>
                <text x="{x:.0f}" y="{y - r - 4:.0f}" text-anchor="middle"
                  fill="{color}" font-size="11" font-weight="600">{name}</text>
                <text x="{x:.0f}" y="{y + 4:.0f}" text-anchor="middle"
                  fill="#e2e8f0" font-size="11">{sessions}s</text>
                {score_el}
            ''')

        lines_html = "\n".join(lines)
        nodes_html = "\n".join(node_elements)

        return f'''<div class="tc-card" id="tc-fleet-constellation">
  <div class="tc-card-header">Fleet Constellation</div>
  <div class="tc-constellation-wrap">
    <svg viewBox="0 0 300 240" xmlns="http://www.w3.org/2000/svg"
         style="width:100%;max-height:220px;">
      {lines_html}
      {nodes_html}
    </svg>
  </div>
</div>'''
