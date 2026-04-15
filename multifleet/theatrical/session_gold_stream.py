"""Component 9: Session Gold Stream — live gold mining output from session historian."""

import time
from typing import Optional

from .base import TheatricalComponent


class SessionGoldStream(TheatricalComponent):
    """Live stream of gold nuggets extracted by the 16-pass session historian."""

    COMPONENT_NAME = "session_gold_stream"
    REFRESH_INTERVAL_S = 15

    PASS_COLORS = {
        "extract_decisions": "#3b82f6",
        "extract_bugs": "#ef4444",
        "extract_patterns": "#a855f7",
        "extract_sops": "#22c55e",
        "extract_gotchas": "#f97316",
        "eval_webhook_quality": "#06b6d4",
        "quality_cardiologist": "#ec4899",
        "cv_sentinel": "#eab308",
        "corrigibility_gate": "#3b82f6",
        "extract_architecture": "#a855f7",
    }

    SEVERITY_COLORS = {
        "critical": "#ef4444",
        "high": "#f97316",
        "medium": "#eab308",
        "low": "#22c55e",
        "architectural": "#a855f7",
        "info": "#94a3b8",
    }

    def render(self, data: Optional[dict] = None) -> str:
        if not data:
            return self.render_unavailable("No session gold data")

        nuggets = data.get("nuggets", [])
        stats = data.get("stats", {})
        active_session = data.get("active_session")
        critical_count = data.get("critical_count", 0)

        # Stats bar
        total_mined = stats.get("total_mined", 0)
        sessions_processed = stats.get("sessions_processed", 0)
        reclaimed_mb = stats.get("reclaimed_mb", 0.0)

        stats_html = (
            f'<div class="tc-gold-stats">'
            f'<span>&#9878; {total_mined} mined</span>'
            f'<span>&#128196; {sessions_processed} sessions</span>'
            f'<span>&#128190; {reclaimed_mb:.1f}MB reclaimed</span>'
            f'</div>'
        )

        # Critical findings alert
        critical_html = ""
        if critical_count > 0:
            critical_html = (
                f'<div class="tc-gold-alert">'
                f'<span class="tc-badge tc-badge-red">{critical_count} Critical Finding'
                f'{"s" if critical_count != 1 else ""}</span>'
                f'</div>'
            )

        # Gold nuggets feed
        nugget_items = []
        for nugget in nuggets[-15:]:
            pass_id = nugget.get("pass", nugget.get("pass_id", "?"))
            finding = nugget.get("finding", nugget.get("content", ""))
            severity = nugget.get("severity", "info")
            ts = nugget.get("timestamp")

            pass_color = self.PASS_COLORS.get(pass_id, "#94a3b8")
            sev_color = self.SEVERITY_COLORS.get(severity, "#94a3b8")

            ts_str = self._format_ts(ts) if ts else ""

            nugget_items.append(f'''
<div class="tc-gold-nugget">
  <div class="tc-gold-header">
    <span class="tc-gold-pass" style="color:{pass_color};">{self._esc(str(pass_id))}</span>
    <span class="tc-gold-severity" style="color:{sev_color};">{self._esc(severity)}</span>
    <code class="tc-ts">{ts_str}</code>
  </div>
  <div class="tc-gold-content">{self._esc(str(finding)[:200])}</div>
</div>''')

        nuggets_html = "".join(nugget_items) if nugget_items else '<div class="tc-muted">No gold nuggets mined yet</div>'

        # Active session indicator
        session_html = ""
        if active_session:
            sid = self._esc(str(active_session.get("session_id", "?"))[:12])
            phase = self._esc(active_session.get("phase", "?"))
            progress = active_session.get("progress", 0)
            session_html = (
                f'<div class="tc-gold-active">'
                f'<span class="tc-badge tc-badge-blue">Mining</span> '
                f'<code>{sid}</code> '
                f'<span class="tc-muted">{phase}</span> '
                f'{self._bar_html(progress, 60, "#3b82f6")}'
                f'</div>'
            )

        return f'''<div class="tc-card" id="tc-session-gold-stream">
  <div class="tc-card-header">
    Session Gold Stream
    <span class="tc-card-count">&#9878; {total_mined}</span>
  </div>
  {stats_html}
  {critical_html}
  {session_html}
  <div class="tc-gold-feed">{nuggets_html}</div>
</div>'''
