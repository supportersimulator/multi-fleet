"""Component 2: Surgeon Feed — live stream of cross-examinations and consensus.

Shows individual surgeon positions when disagreements occur, with visual emphasis
on dissent (red border, warning icon). Integrates corrigibility ledger deltas.
"""

import time
from typing import Optional

from .base import TheatricalComponent


class SurgeonFeed(TheatricalComponent):
    """Live-updating feed of 3-surgeon cross-examinations, verdicts, and consensus.

    When surgeons disagree, each position is shown separately with dissent
    highlighted via red borders and warning icons. Before/after deltas from the
    corrigibility ledger are rendered inline so the user sees the real impact
    of surgeon input on decisions.
    """

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
        disagreements = data.get("disagreements", [])
        decision_points = data.get("decision_points", [])

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

            # Individual surgeon positions (shown when present)
            positions = exam.get("surgeon_positions", [])
            positions_html = self._render_surgeon_positions(positions, consensus)

            feed_items.append(
                f'<div class="tc-feed-entry">'
                f'<code class="tc-ts">{ts}</code> '
                f'<span class="tc-feed-topic">{topic}</span> '
                f'<span style="color:{cons_color};">{consensus:.0%}</span> '
                f'<span class="tc-muted">{mode} ${cost:.4f}</span>'
                f'{positions_html}'
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

        # Disagreement section — each surgeon position shown separately
        disagreement_items = self._render_disagreements(disagreements)

        # Decision point deltas from corrigibility ledger
        delta_items = self._render_decision_deltas(decision_points)

        feed_html = "".join(feed_items) if feed_items else '<div class="tc-muted">No recent cross-examinations</div>'
        rebuttal_html = "".join(rebuttal_items) if rebuttal_items else '<div class="tc-muted">No active rebuttals</div>'
        disagreement_html = "".join(disagreement_items) if disagreement_items else ""
        delta_html = "".join(delta_items) if delta_items else ""

        # Build disagreement section with count badge
        disagreement_section = ""
        if disagreement_items:
            count = len(disagreement_items)
            disagreement_section = (
                f'<div class="tc-feed-section">'
                f'<div class="tc-feed-label">&#9888; Surgeon Disagreements '
                f'<span class="tc-badge-red">{count}</span></div>'
                f'{disagreement_html}'
                f'</div>'
            )

        # Build delta section
        delta_section = ""
        if delta_items:
            delta_section = (
                f'<div class="tc-feed-section">'
                f'<div class="tc-feed-label">Corrigibility Deltas</div>'
                f'{delta_html}'
                f'</div>'
            )

        return f'''<div class="tc-card" id="tc-surgeon-feed">
  <div class="tc-card-header">Surgeon Feed</div>
  <div class="tc-surgeon-status">{status_row}</div>
  {disagreement_section}
  <div class="tc-feed-section">
    <div class="tc-feed-label">Cross-Examinations</div>
    {feed_html}
  </div>
  <div class="tc-feed-section">
    <div class="tc-feed-label">Active Rebuttals</div>
    {rebuttal_html}
  </div>
  {delta_section}
</div>'''

    def _render_surgeon_positions(self, positions: list, consensus: float) -> str:
        """Render individual surgeon positions inline under a cross-exam entry.

        When consensus < 0.7 (disagreement territory), positions get red
        dissent borders. Otherwise neutral borders.
        """
        if not positions:
            return ""

        is_dissent = consensus < 0.7
        items = []
        for pos in positions:
            name = pos.get("name", "unknown").lower()
            icon = self.SURGEON_ICONS.get(name, "?")
            color = self.SURGEON_COLORS.get(name, "#94a3b8")
            stance = self._esc(pos.get("stance", ""))
            confidence = pos.get("confidence", 0)

            # Red border for dissent, normal border otherwise
            border_color = "#ef4444" if is_dissent else color
            border_width = "2px" if is_dissent else "1px"
            dissent_icon = ' &#9888;' if is_dissent and confidence < 0.6 else ''

            items.append(
                f'<div class="tc-surgeon-position" style="'
                f'border-left:{border_width} solid {border_color};'
                f'padding-left:8px;margin:4px 0 4px 12px;">'
                f'<span>{icon}</span> '
                f'<span style="color:{color};">{self._esc(name.title())}</span>'
                f'{dissent_icon} '
                f'<span class="tc-muted">({confidence:.0%})</span> '
                f'{stance}'
                f'</div>'
            )
        return "".join(items)

    def _render_disagreements(self, disagreements: list) -> list:
        """Render corrigibility ledger disagreements with visual emphasis.

        Each disagreement shows both surgeon positions separately, the
        resolution, and a red dissent border to draw the user's eye.
        """
        items = []
        for d in disagreements[:8]:
            ts = self._format_ts(d.get("ts"))
            topic = self._esc(d.get("topic", "?"))
            pos_a = self._esc(d.get("surgeon_a_position", ""))
            pos_b = self._esc(d.get("surgeon_b_position", ""))
            resolution = self._esc(d.get("resolution", "unresolved"))
            evidence = d.get("evidence", {})

            # Determine surgeon names from evidence if available
            surgeon_a_name = evidence.get("surgeon_a_name", "Surgeon A") if isinstance(evidence, dict) else "Surgeon A"
            surgeon_b_name = evidence.get("surgeon_b_name", "Surgeon B") if isinstance(evidence, dict) else "Surgeon B"
            surgeon_a_icon = self.SURGEON_ICONS.get(surgeon_a_name.lower(), "?")
            surgeon_b_icon = self.SURGEON_ICONS.get(surgeon_b_name.lower(), "?")
            surgeon_a_color = self.SURGEON_COLORS.get(surgeon_a_name.lower(), "#94a3b8")
            surgeon_b_color = self.SURGEON_COLORS.get(surgeon_b_name.lower(), "#94a3b8")

            res_color = "#22c55e" if resolution != "unresolved" else "#eab308"

            items.append(
                f'<div class="tc-disagreement" style="'
                f'border:2px solid #ef4444;border-radius:6px;'
                f'padding:8px;margin:4px 0;background:rgba(239,68,68,0.08);">'
                f'<div style="margin-bottom:4px;">'
                f'<code class="tc-ts">{ts}</code> '
                f'<span class="tc-feed-topic">&#9888; {topic}</span>'
                f'</div>'
                f'<div class="tc-surgeon-position" style="'
                f'border-left:2px solid {surgeon_a_color};padding-left:8px;margin:4px 0;">'
                f'{surgeon_a_icon} <span style="color:{surgeon_a_color};">'
                f'{self._esc(surgeon_a_name)}</span>: {pos_a}'
                f'</div>'
                f'<div class="tc-surgeon-position" style="'
                f'border-left:2px solid {surgeon_b_color};padding-left:8px;margin:4px 0;">'
                f'{surgeon_b_icon} <span style="color:{surgeon_b_color};">'
                f'{self._esc(surgeon_b_name)}</span>: {pos_b}'
                f'</div>'
                f'<div style="margin-top:4px;font-size:0.85em;">'
                f'<span style="color:{res_color};">Resolution: {resolution}</span>'
                f'</div>'
                f'</div>'
            )
        return items

    def _render_decision_deltas(self, decision_points: list) -> list:
        """Render before/after deltas from the corrigibility ledger.

        Shows proposed action vs final action, with visual indicator
        of whether surgeon input changed the decision.
        """
        items = []
        for dp in decision_points[:5]:
            ts = self._format_ts(dp.get("ts"))
            context = self._esc(dp.get("context", "?"))
            proposed = self._esc(dp.get("proposed_action", ""))
            final = self._esc(dp.get("final_action", ""))
            changed = dp.get("changed", False)

            change_icon = "&#8644;" if changed else "&#10003;"
            change_color = "#eab308" if changed else "#22c55e"
            border_color = "#eab308" if changed else "#334155"

            items.append(
                f'<div class="tc-decision-delta" style="'
                f'border-left:2px solid {border_color};padding-left:8px;margin:4px 0;">'
                f'<code class="tc-ts">{ts}</code> '
                f'<span class="tc-feed-topic">{context}</span> '
                f'<span style="color:{change_color};">{change_icon}</span>'
                f'<div class="tc-muted" style="font-size:0.85em;margin-top:2px;">'
                f'<span style="text-decoration:line-through;opacity:0.6;">{proposed}</span>'
                f' &#8594; {final}'
                f'</div>'
                f'</div>'
            )
        return items
