"""Component 4: Evidence Timeline — hash-chain of decisions with provenance."""

import time
from typing import Optional

from .base import TheatricalComponent


class EvidenceTimeline(TheatricalComponent):
    """Vertical timeline of evidence ledger entries with hash chain visualization."""

    COMPONENT_NAME = "evidence_timeline"
    REFRESH_INTERVAL_S = 10

    EVENT_ICONS = {
        "merge_decision": "&#9879;",      # merged
        "lease_granted": "&#128274;",      # lock
        "rebuttal_resolved": "&#9878;",    # scales
        "gains_gate_result": "&#9989;",    # check
        "channel_repair": "&#128295;",     # wrench
        "consensus_reached": "&#129309;",  # handshake
    }
    EVENT_COLORS = {
        "merge_decision": "#3b82f6",
        "lease_granted": "#a855f7",
        "rebuttal_resolved": "#06b6d4",
        "gains_gate_result": "#22c55e",
        "channel_repair": "#f97316",
        "consensus_reached": "#ec4899",
    }

    def render(self, data: Optional[dict] = None) -> str:
        if not data:
            return self.render_unavailable("No evidence data")

        entries = data.get("entries", [])
        chain_valid = data.get("chain_valid", None)
        chain_checked = data.get("chain_checked", 0)
        total_entries = data.get("total", 0)

        # Chain integrity badge
        if chain_valid is True:
            chain_badge = (
                f'<span class="tc-badge tc-badge-green">Chain Intact</span> '
                f'<span class="tc-muted">{chain_checked} verified</span>'
            )
        elif chain_valid is False:
            chain_badge = '<span class="tc-badge tc-badge-red">Chain Broken</span>'
        else:
            chain_badge = '<span class="tc-badge tc-badge-gray">Unverified</span>'

        # Timeline entries
        items = []
        for entry in entries[:12]:
            evt_type = entry.get("event_type", "unknown")
            icon = self.EVENT_ICONS.get(evt_type, "&#8226;")
            color = self.EVENT_COLORS.get(evt_type, "#94a3b8")
            ts = self._format_ts(entry.get("timestamp"))
            node = self._esc(entry.get("node_id", "?"))
            subject = self._esc(entry.get("subject", ""))
            hash_prefix = self._esc(str(entry.get("entry_hash", ""))[:12])
            seq = entry.get("seq", "?")

            payload = entry.get("payload", {})
            detail = ""
            if isinstance(payload, dict):
                # Show key decision info from payload
                if "decision" in payload:
                    detail = self._esc(str(payload["decision"])[:60])
                elif "result" in payload:
                    detail = self._esc(str(payload["result"])[:60])
                elif "status" in payload:
                    detail = self._esc(str(payload["status"])[:60])

            items.append(f'''
<div class="tc-timeline-entry">
  <div class="tc-timeline-dot" style="background:{color};"></div>
  <div class="tc-timeline-content">
    <div class="tc-timeline-header">
      <span>{icon}</span>
      <span style="color:{color};font-weight:600;">{self._esc(evt_type)}</span>
      <code class="tc-ts">{ts}</code>
      <span class="tc-muted">#{seq}</span>
    </div>
    <div class="tc-timeline-body">
      <span>{subject}</span>
      {f'<span class="tc-muted"> — {detail}</span>' if detail else ''}
    </div>
    <div class="tc-timeline-meta">
      <span class="tc-muted">node:{node}</span>
      <code class="tc-hash">{hash_prefix}</code>
    </div>
  </div>
</div>''')

        items_html = "".join(items) if items else '<div class="tc-muted">No evidence entries</div>'

        return f'''<div class="tc-card" id="tc-evidence-timeline">
  <div class="tc-card-header">
    Evidence Timeline
    <span class="tc-card-count">{total_entries} entries</span>
  </div>
  <div class="tc-chain-status">{chain_badge}</div>
  <div class="tc-timeline">{items_html}</div>
</div>'''
