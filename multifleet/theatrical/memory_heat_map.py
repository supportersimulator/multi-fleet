"""Component 6: Memory Heat Map — ContextDNA knowledge density by topic."""

from typing import Optional

from .base import TheatricalComponent


class MemoryHeatMap(TheatricalComponent):
    """Grid heat map showing knowledge density across topic areas."""

    COMPONENT_NAME = "memory_heat_map"
    REFRESH_INTERVAL_S = 30

    # Default topic areas to display
    DEFAULT_TOPICS = [
        "architecture", "webhooks", "memory", "fleet",
        "surgeons", "probes", "security", "performance",
        "testing", "deployment", "schemas", "protocols",
        "redis", "sqlite", "llm", "sessions",
    ]

    # Heat colors from cold to hot
    HEAT_SCALE = [
        "#1e293b",  # 0 — empty
        "#1e3a5f",  # 1 — cool
        "#1e4d7a",  # 2
        "#1a6b3a",  # 3 — warm
        "#2d8a4e",  # 4
        "#3ba55d",  # 5 — medium
        "#7ac74f",  # 6
        "#c4d42e",  # 7 — hot
        "#eab308",  # 8
        "#f97316",  # 9 — very hot
        "#ef4444",  # 10 — max
    ]

    def render(self, data: Optional[dict] = None) -> str:
        if not data:
            return self.render_unavailable("No memory data")

        topics = data.get("topics", {})
        total_memories = data.get("total_memories", 0)
        last_updated = data.get("last_updated")

        # Build heat cells
        display_topics = list(topics.keys()) if topics else self.DEFAULT_TOPICS
        max_count = max((topics.get(t, {}).get("count", 0) for t in display_topics), default=1) or 1

        cells = []
        for topic in display_topics[:20]:  # cap at 20
            info = topics.get(topic, {})
            count = info.get("count", 0)
            quality = info.get("quality", 0.0)
            last_hit = info.get("last_hit")

            # Normalize to 0-10 scale
            intensity = min(10, int((count / max_count) * 10)) if max_count > 0 else 0
            bg_color = self.HEAT_SCALE[intensity]

            # Quality overlay
            q_indicator = ""
            if quality > 0:
                q_color = self._health_color(quality)
                q_indicator = (
                    f'<div class="tc-heat-quality" style="color:{q_color};">'
                    f'{quality:.0%}</div>'
                )

            cells.append(f'''
<div class="tc-heat-cell" style="background:{bg_color};"
     title="{self._esc(topic)}: {count} entries, quality {quality:.0%}">
  <div class="tc-heat-label">{self._esc(topic)}</div>
  <div class="tc-heat-count">{count}</div>
  {q_indicator}
</div>''')

        cells_html = "".join(cells)

        # Legend
        legend_items = []
        for i in range(0, 11, 2):
            legend_items.append(
                f'<div class="tc-legend-item" style="background:{self.HEAT_SCALE[i]};"></div>'
            )
        legend_html = "".join(legend_items)

        ts_str = self._format_ts(last_updated) if last_updated else "---"

        return f'''<div class="tc-card" id="tc-memory-heat-map">
  <div class="tc-card-header">
    Memory Heat Map
    <span class="tc-card-count">{total_memories} memories</span>
  </div>
  <div class="tc-heat-grid">{cells_html}</div>
  <div class="tc-heat-legend">
    <span class="tc-muted">cold</span>
    {legend_html}
    <span class="tc-muted">hot</span>
    <span class="tc-muted" style="margin-left:auto;">updated {ts_str}</span>
  </div>
</div>'''
