"""Component 2: Surgeon Feed — live stream of cross-examinations and consensus."""

import time
from typing import Optional

from .base import TheatricalComponent


class SurgeonFeed(TheatricalComponent):
    """Live-updating feed of 3-surgeon cross-examinations, verdicts, and consensus."""

    COMPONENT_NAME = "surgeon_feed"
    REFRESH_INTERVAL_S = 5

    SURGEON_ICONS = {
        "atlas": "&#129504;",       # brain
        "cardiologist": "&#10084;",  # heart
        "neurologist": "&#128302;",  # crystal ball
    }
    SURGEON_COLORS = {
        "atlas": "#3b82f6",
        "cardiologist": "#ef4444",
        "neurologist": "#a855f7",
    }

    def render(self, data: Optional[dict] = None) -> str:
        if not data:
            return self.render_unavailable("No surgeon activity")

        surgeons = data.get("surgeons", {})
        cross_exams = data.get("cross_exams", [])
        rebuttals = data.get("rebuttals", [])

        # Surgeon status row
        status_items = []
        for name in ["atlas", "cardiologist", "neurologist"]:
            st = surgeons.get(name, "unknown")
            icon = self.SURGEON_ICONS.get(name, "?")
            color = self.SURGEON_COLORS.get(name, "#94a3b8")
            dot_c = "#22c55e" if st in ("ok", "active", "configured") else "#ef4444"
            status_items.append(
                f'<div class="tc-surgeon-badge" style="border-color:{color};">'
                f'<span>{icon}</span> '
                f'<span style="color:{color};">{self._esc(name.title())}</span> '
                f'{self._dot_html(dot_c)}'
                f'</div>'
            )
        status_row = "".join(status_items)

        # Cross-exam feed entries
        feed_items = []
        for exam in cross_exams[-8:]:
            ts = self._format_ts(exam.get("timestamp"))
            topic = self._esc(exam.get("topic", "?"))
            consensus = exam.get("consensus", 0)
            cost = exam.get("cost", 0)
            mode = self._esc(exam.get("mode", "standard"))
            cons_color = self._health_color(consensus)
            feed_items.append(
                f'<div class="tc-feed-entry">'
                f'<code class="tc-ts">{ts}</code> '
                f'<span class="tc-feed-topic">{topic}</span> '
                f'<span style="color:{cons_color};">{consensus:.0%}</span> '
                f'<span class="tc-muted">{mode} ${cost:.4f}</span>'
                f'</div>'
            )

        # Active rebuttal sessions
        rebuttal_items = []
        for rb in rebuttals[:5]:
            task_id = self._esc(str(rb.get("task_id", "?"))[:8])
            phase = self._esc(rb.get("phase", "?"))
            verdicts = rb.get("verdicts_count", 0)
            expected = rb.get("expected", 0)
            phase_color = "#eab308" if phase != "resolved" else "#22c55e"
            rebuttal_items.append(
                f'<div class="tc-feed-entry">'
                f'<code>{task_id}</code> '
                f'<span style="color:{phase_color};">{phase}</span> '
                f'<span class="tc-muted">{verdicts}/{expected} verdicts</span>'
                f'</div>'
            )

        feed_html = "".join(feed_items) if feed_items else '<div class="tc-muted">No recent cross-examinations</div>'
        rebuttal_html = "".join(rebuttal_items) if rebuttal_items else '<div class="tc-muted">No active rebuttals</div>'

        return f'''<div class="tc-card" id="tc-surgeon-feed">
  <div class="tc-card-header">Surgeon Feed</div>
  <div class="tc-surgeon-status">{status_row}</div>
  <div class="tc-feed-section">
    <div class="tc-feed-label">Cross-Examinations</div>
    {feed_html}
  </div>
  <div class="tc-feed-section">
    <div class="tc-feed-label">Active Rebuttals</div>
    {rebuttal_html}
  </div>
</div>'''
