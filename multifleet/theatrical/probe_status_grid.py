"""Component 7: Probe Status Grid — 18 probes with last-fire timestamps and scores."""

import time
from typing import Optional

from .base import TheatricalComponent


class ProbeStatusGrid(TheatricalComponent):
    """Grid display of all 18 system probes with status, scores, and timing."""

    COMPONENT_NAME = "probe_status_grid"
    REFRESH_INTERVAL_S = 15

    # The 18 probes in the ContextDNA system
    PROBE_DEFINITIONS = [
        {"id": "webhook_health", "label": "Webhook Health", "category": "infra"},
        {"id": "llm_local", "label": "LLM Local", "category": "infra"},
        {"id": "llm_remote", "label": "LLM Remote", "category": "infra"},
        {"id": "redis_health", "label": "Redis Health", "category": "infra"},
        {"id": "scheduler_health", "label": "Scheduler", "category": "infra"},
        {"id": "gpu_lock", "label": "GPU Lock", "category": "infra"},
        {"id": "contextdna_store", "label": "ContextDNA Store", "category": "memory"},
        {"id": "synaptic_health", "label": "Synaptic", "category": "memory"},
        {"id": "memory_coherence", "label": "Memory Coherence", "category": "memory"},
        {"id": "anticipation_cache", "label": "Anticipation Cache", "category": "memory"},
        {"id": "fleet_daemon", "label": "Fleet Daemon", "category": "fleet"},
        {"id": "fleet_nats", "label": "Fleet NATS", "category": "fleet"},
        {"id": "fleet_peers", "label": "Fleet Peers", "category": "fleet"},
        {"id": "gains_gate", "label": "Gains Gate", "category": "quality"},
        {"id": "evidence_chain", "label": "Evidence Chain", "category": "quality"},
        {"id": "corrigibility", "label": "Corrigibility", "category": "quality"},
        {"id": "critical_findings", "label": "Critical Findings", "category": "quality"},
        {"id": "session_gold", "label": "Session Gold", "category": "quality"},
    ]

    CATEGORY_COLORS = {
        "infra": "#3b82f6",
        "memory": "#a855f7",
        "fleet": "#06b6d4",
        "quality": "#22c55e",
    }

    def render(self, data: Optional[dict] = None) -> str:
        if not data:
            return self.render_unavailable("No probe data")

        probes = data.get("probes", {})
        now = time.time()

        # Build probe cards
        cards = []
        for probe_def in self.PROBE_DEFINITIONS:
            pid = probe_def["id"]
            label = probe_def["label"]
            category = probe_def["category"]
            cat_color = self.CATEGORY_COLORS.get(category, "#94a3b8")

            probe_data = probes.get(pid, {})
            status = probe_data.get("status", "unknown")
            score = probe_data.get("score", None)
            last_fire = probe_data.get("last_fire")
            message = probe_data.get("message", "")

            # Status styling
            if status == "pass":
                status_color = "#22c55e"
                status_icon = "&#10003;"
            elif status == "warn":
                status_color = "#eab308"
                status_icon = "&#9888;"
            elif status == "fail":
                status_color = "#ef4444"
                status_icon = "&#10007;"
            else:
                status_color = "#64748b"
                status_icon = "&#8226;"

            # Score bar
            score_html = ""
            if score is not None:
                try:
                    s = float(score)
                    s_color = self._health_color(s)
                    score_html = f'{self._bar_html(s, 50, s_color)} <span style="color:{s_color};">{s:.0%}</span>'
                except (ValueError, TypeError):
                    score_html = f'<span class="tc-muted">{self._esc(str(score))}</span>'

            # Last fire timing
            fire_html = ""
            if last_fire:
                try:
                    age = now - float(last_fire)
                    fire_html = f'<span class="tc-muted">{self._ago(age)} ago</span>'
                except (ValueError, TypeError):
                    fire_html = f'<span class="tc-muted">{self._format_ts(last_fire)}</span>'
            else:
                fire_html = '<span class="tc-muted">never</span>'

            cards.append(f'''
<div class="tc-probe-card">
  <div class="tc-probe-header">
    <span style="color:{status_color};font-size:1.1em;">{status_icon}</span>
    <span class="tc-probe-name">{self._esc(label)}</span>
    <span class="tc-probe-cat" style="color:{cat_color};">{self._esc(category)}</span>
  </div>
  <div class="tc-probe-body">
    {score_html}
    {fire_html}
  </div>
</div>''')

        cards_html = "".join(cards)

        # Summary counts
        total = len(self.PROBE_DEFINITIONS)
        passing = sum(1 for p in self.PROBE_DEFINITIONS if probes.get(p["id"], {}).get("status") == "pass")
        failing = sum(1 for p in self.PROBE_DEFINITIONS if probes.get(p["id"], {}).get("status") == "fail")
        unknown = total - passing - failing - sum(
            1 for p in self.PROBE_DEFINITIONS if probes.get(p["id"], {}).get("status") == "warn"
        )

        return f'''<div class="tc-card" id="tc-probe-status-grid">
  <div class="tc-card-header">
    Probe Status Grid
    <span class="tc-card-count">{passing}/{total} passing</span>
  </div>
  <div class="tc-probe-grid">{cards_html}</div>
</div>'''
