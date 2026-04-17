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

    # Consensus threshold below which we flag dissent
    DISSENT_THRESHOLD = 0.7

    def _detect_dissent(self, exam: dict) -> tuple[bool, list[dict]]:
        """Detect surgeon disagreement in a cross-exam result.

        Returns (has_dissent, positions) where positions is a list of
        {surgeon, position} dicts showing each surgeon's stance.
        """
        # Explicit disagreements field
        disagreements = exam.get("disagreements", [])
        if disagreements:
            positions = []
            for s in exam.get("surgeon_positions", exam.get("surgeons", [])):
                if isinstance(s, dict):
                    positions.append({
                        "surgeon": s.get("name", s.get("surgeon", "?")),
                        "position": s.get("position", s.get("analysis", s.get("verdict", ""))),
                    })
            return True, positions

        # Low consensus score
        consensus = exam.get("consensus", 1.0)
        if consensus < self.DISSENT_THRESHOLD:
            positions = []
            for s in exam.get("surgeon_positions", exam.get("surgeons", [])):
                if isinstance(s, dict):
                    positions.append({
                        "surgeon": s.get("name", s.get("surgeon", "?")),
                        "position": s.get("position", s.get("analysis", s.get("verdict", ""))),
                    })
            return True, positions

        return False, []

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

        # Cross-exam feed entries with dissent detection
        feed_items = []
        dissent_count = 0
        for exam in cross_exams[-8:]:
            ts = self._format_ts(exam.get("timestamp"))
            topic = self._esc(exam.get("topic", "?"))
            consensus = exam.get("consensus", 0)
            cost = exam.get("cost", 0)
            mode = self._esc(exam.get("mode", "standard"))

            has_dissent, positions = self._detect_dissent(exam)

            if has_dissent:
                dissent_count += 1
                # Dissent entry: red border, warning icon, per-surgeon positions
                position_html = ""
                if positions:
                    pos_items = []
                    for p in positions:
                        s_name = p["surgeon"]
                        s_color = self.SURGEON_COLORS.get(s_name.lower(), "#94a3b8")
                        s_icon = self.SURGEON_ICONS.get(s_name.lower(), "?")
                        pos_text = self._esc(str(p["position"])[:120])
                        pos_items.append(
                            f'<div class="tc-dissent-position">'
                            f'<span>{s_icon}</span> '
                            f'<span style="color:{s_color};font-weight:600;">'
                            f'{self._esc(s_name.title())}</span>: '
                            f'<span class="tc-muted">{pos_text}</span>'
                            f'</div>'
                        )
                    position_html = "".join(pos_items)

                disagreements = exam.get("disagreements", [])
                dissent_detail = ""
                if disagreements:
                    dissent_detail = (
                        f'<div class="tc-dissent-summary">'
                        + "".join(
                            f'<div class="tc-muted">&bull; {self._esc(str(d)[:100])}</div>'
                            for d in disagreements[:3]
                        )
                        + f'</div>'
                    )

                feed_items.append(
                    f'<div class="tc-feed-entry tc-dissent"'
                    f' style="border-left:3px solid #ef4444;background:rgba(239,68,68,0.08);">'
                    f'<code class="tc-ts">{ts}</code> '
                    f'<span style="color:#ef4444;">&#9888;</span> '
                    f'<span class="tc-feed-topic">{topic}</span> '
                    f'<span style="color:#ef4444;">{consensus:.0%}</span> '
                    f'<span class="tc-muted">{mode} ${cost:.4f}</span>'
                    f'{dissent_detail}'
                    f'{position_html}'
                    f'</div>'
                )
            else:
                # Consensus entry: green border
                cons_color = self._health_color(consensus)
                feed_items.append(
                    f'<div class="tc-feed-entry"'
                    f' style="border-left:3px solid #22c55e;">'
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

        dissent_badge = ""
        if dissent_count > 0:
            dissent_badge = (
                f' <span style="background:#ef4444;color:#fff;padding:1px 7px;'
                f'border-radius:9px;font-size:0.8em;margin-left:8px;">'
                f'&#9888; {dissent_count} dissent{"s" if dissent_count != 1 else ""}</span>'
            )

        return f'''<div class="tc-card" id="tc-surgeon-feed">
  <div class="tc-card-header">Surgeon Feed{dissent_badge}</div>
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
