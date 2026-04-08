"""Human Arbiter View -- present synthesis decisions for human override.

When machines disagree, Aaron sees:
1. Top recommendation (winner branch + reasoning)
2. Alternatives (non-winning branches with their strengths)
3. Evidence trail (which memories influenced the decision)
4. Dissent (what surgeons disagreed about)
5. One-click actions: Approve / Request Changes / Override / Defer

This is the core human-in-the-loop interface for the beehive.
"""

import html
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Optional

logger = logging.getLogger("fleet-nerve.arbiter")

ACTIONS = ["approve", "request_changes", "override", "defer"]


@dataclass
class ArbiterDecision:
    """Snapshot of a decision awaiting human review."""
    task_id: str
    status: str = "pending"  # pending | approved | overridden | changes_requested | deferred
    recommendation: dict = field(default_factory=dict)
    alternatives: list = field(default_factory=list)
    evidence_trail: list = field(default_factory=list)
    dissent: list = field(default_factory=list)
    confidence: float = 0.0
    memory_influences: list = field(default_factory=list)
    actions: list = field(default_factory=lambda: list(ACTIONS))
    audit_trail: list = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    resolved_at: Optional[float] = None
    feedback: Optional[str] = None
    chosen_branch: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


class ArbiterView:
    """Present synthesis decisions for human override."""

    def __init__(self, synthesis=None, evidence_ledger=None, rebuttal_mgr=None):
        self._synthesis = synthesis
        self._evidence_ledger = evidence_ledger
        self._rebuttal_mgr = rebuttal_mgr
        self._decisions: dict[str, ArbiterDecision] = {}

    def present_decision(self, task_id: str) -> dict:
        """Build the full arbiter view for a synthesis decision."""
        decision = self._decisions.get(task_id)
        if decision:
            return decision.to_dict()

        # Build from synthesis + evidence + rebuttal data
        recommendation = self._get_recommendation(task_id)
        alternatives = self._get_alternatives(task_id)
        evidence_trail = self._get_evidence_trail(task_id)
        dissent = self._get_dissent(task_id)
        confidence = recommendation.get("confidence", 0.0) if recommendation else 0.0
        memory_influences = recommendation.get("memory_influences", []) if recommendation else []

        decision = ArbiterDecision(
            task_id=task_id,
            recommendation=recommendation,
            alternatives=alternatives,
            evidence_trail=evidence_trail,
            dissent=dissent,
            confidence=confidence,
            memory_influences=memory_influences,
        )
        self._decisions[task_id] = decision
        self._record_audit(task_id, "presented", "Decision presented for human review")
        return decision.to_dict()

    def approve(self, task_id: str) -> dict:
        """Human approves the recommendation."""
        decision = self._decisions.get(task_id)
        if not decision:
            return {"error": f"No decision found for task {task_id}"}
        if decision.status != "pending":
            return {"error": f"Decision already resolved: {decision.status}"}

        decision.status = "approved"
        decision.resolved_at = time.time()
        self._record_audit(task_id, "approved", "Human approved recommendation")
        self._record_evidence(task_id, "arbiter_approved", {
            "winner_branch": decision.recommendation.get("winner_branch"),
            "confidence": decision.confidence,
        })
        return {"status": "approved", "task_id": task_id}

    def override(self, task_id: str, chosen_branch: str, reason: str) -> dict:
        """Human overrides with a different choice."""
        decision = self._decisions.get(task_id)
        if not decision:
            return {"error": f"No decision found for task {task_id}"}
        if decision.status != "pending":
            return {"error": f"Decision already resolved: {decision.status}"}

        decision.status = "overridden"
        decision.chosen_branch = chosen_branch
        decision.feedback = reason
        decision.resolved_at = time.time()
        self._record_audit(task_id, "overridden", f"Human override: {chosen_branch} — {reason}")
        self._record_evidence(task_id, "arbiter_overridden", {
            "chosen_branch": chosen_branch,
            "reason": reason,
            "original_winner": decision.recommendation.get("winner_branch"),
        })
        return {"status": "overridden", "task_id": task_id, "chosen_branch": chosen_branch}

    def request_changes(self, task_id: str, feedback: str) -> dict:
        """Human requests modifications before approving."""
        decision = self._decisions.get(task_id)
        if not decision:
            return {"error": f"No decision found for task {task_id}"}
        if decision.status != "pending":
            return {"error": f"Decision already resolved: {decision.status}"}

        decision.status = "changes_requested"
        decision.feedback = feedback
        decision.resolved_at = time.time()
        self._record_audit(task_id, "changes_requested", f"Feedback: {feedback}")
        self._record_evidence(task_id, "arbiter_changes_requested", {
            "feedback": feedback,
        })
        return {"status": "changes_requested", "task_id": task_id, "feedback": feedback}

    def defer(self, task_id: str) -> dict:
        """Human defers decision -- machines continue with current approach."""
        decision = self._decisions.get(task_id)
        if not decision:
            return {"error": f"No decision found for task {task_id}"}
        if decision.status != "pending":
            return {"error": f"Decision already resolved: {decision.status}"}

        decision.status = "deferred"
        decision.resolved_at = time.time()
        self._record_audit(task_id, "deferred", "Human deferred — machines continue")
        self._record_evidence(task_id, "arbiter_deferred", {})
        return {"status": "deferred", "task_id": task_id}

    def list_pending(self) -> list[dict]:
        """List decisions awaiting human review."""
        return [
            d.to_dict() for d in self._decisions.values()
            if d.status == "pending"
        ]

    def get_decision(self, task_id: str) -> Optional[dict]:
        """Get a specific decision by task_id."""
        d = self._decisions.get(task_id)
        return d.to_dict() if d else None

    # ── Data extraction ──

    def _get_recommendation(self, task_id: str) -> dict:
        """Extract top recommendation from synthesis."""
        if not self._synthesis:
            return {}
        try:
            decisions = self._synthesis.recent_decisions(limit=20)
            for d in decisions:
                if d.get("task_id") == task_id:
                    return {
                        "winner_branch": d.get("winner_branch"),
                        "decision": d.get("decision"),
                        "confidence": d.get("confidence", 0.0),
                        "reasoning": d.get("reasoning", ""),
                        "memory_influences": d.get("metadata", {}).get("memory_reasons", [])
                            if isinstance(d.get("metadata"), dict) else [],
                    }
        except Exception as e:
            logger.warning("Failed to get recommendation: %s", e)
        return {}

    def _get_alternatives(self, task_id: str) -> list:
        """Extract non-winning branches with their strengths."""
        if not self._synthesis:
            return []
        try:
            decisions = self._synthesis.recent_decisions(limit=20)
            for d in decisions:
                if d.get("task_id") == task_id:
                    cherry = d.get("cherry_pick_from", [])
                    return [
                        {
                            "machine": cp.get("machine", "unknown"),
                            "branch": cp.get("commit", cp.get("branch", "")),
                            "strength": cp.get("reason", ""),
                        }
                        for cp in cherry
                    ]
        except Exception as e:
            logger.warning("Failed to get alternatives: %s", e)
        return []

    def _get_evidence_trail(self, task_id: str) -> list:
        """Extract evidence entries related to this task."""
        if not self._evidence_ledger:
            return []
        try:
            entries = self._evidence_ledger.query(limit=50)
            trail = []
            for entry in entries:
                payload = entry.get("payload", {})
                if isinstance(payload, str):
                    try:
                        payload = json.loads(payload)
                    except (json.JSONDecodeError, TypeError):
                        payload = {}
                if payload.get("task_id") == task_id or task_id in entry.get("subject", ""):
                    trail.append({
                        "entry_id": entry.get("entry_id", ""),
                        "event_type": entry.get("event_type", ""),
                        "node_id": entry.get("node_id", ""),
                        "subject": entry.get("subject", ""),
                        "timestamp": entry.get("timestamp", 0),
                    })
            return trail
        except Exception as e:
            logger.warning("Failed to get evidence trail: %s", e)
        return []

    def _get_dissent(self, task_id: str) -> list:
        """Extract dissent from synthesis and rebuttals."""
        dissent = []
        # From synthesis decisions
        if self._synthesis:
            try:
                decisions = self._synthesis.recent_decisions(limit=20)
                for d in decisions:
                    if d.get("task_id") == task_id:
                        for dp in d.get("dissent_preserved", d.get("dissent", [])):
                            if isinstance(dp, dict):
                                dissent.append({
                                    "surgeon": dp.get("surgeon", dp.get("machine", "unknown")),
                                    "claim": dp.get("claim", dp.get("message", "")),
                                    "machine": dp.get("machine", ""),
                                })
                            else:
                                dissent.append({"surgeon": "unknown", "claim": str(dp), "machine": ""})
            except Exception as e:
                logger.warning("Failed to get synthesis dissent: %s", e)

        # From rebuttal manager
        if self._rebuttal_mgr:
            try:
                session = self._rebuttal_mgr.get_session(task_id)
                if session:
                    for machine_id, critique in session.critiques.items():
                        if isinstance(critique, dict):
                            dissent.append({
                                "surgeon": critique.get("surgeon", machine_id),
                                "claim": critique.get("critique", critique.get("message", "")),
                                "machine": machine_id,
                            })
            except Exception as e:
                logger.warning("Failed to get rebuttal dissent: %s", e)

        return dissent

    # ── Audit + Evidence ──

    def _record_audit(self, task_id: str, action: str, detail: str):
        """Append to in-memory audit trail."""
        decision = self._decisions.get(task_id)
        if decision:
            decision.audit_trail.append({
                "action": action,
                "detail": detail,
                "timestamp": time.time(),
            })

    def _record_evidence(self, task_id: str, event_type: str, payload: dict):
        """Record arbiter action to the evidence ledger."""
        if not self._evidence_ledger:
            return
        try:
            node_id = os.environ.get("MULTIFLEET_NODE_ID", "unknown")
            self._evidence_ledger.record(
                event_type=event_type,
                node_id=node_id,
                subject=f"arbiter:{task_id}",
                payload={"task_id": task_id, **payload},
            )
        except Exception as e:
            logger.warning("Failed to record evidence: %s", e)

    # ── Rendering ──

    def render_html(self, decision: dict) -> str:
        """Render arbiter view as interactive HTML (for dashboard)."""
        task_id = _esc(decision.get("task_id", ""))
        status = decision.get("status", "pending")
        rec = decision.get("recommendation", {})
        alternatives = decision.get("alternatives", [])
        evidence = decision.get("evidence_trail", [])
        dissent_list = decision.get("dissent", [])
        confidence = decision.get("confidence", 0.0)
        memory_inf = decision.get("memory_influences", [])

        # Confidence bar color
        conf_pct = int(confidence * 100)
        conf_color = "#22c55e" if confidence >= 0.7 else "#eab308" if confidence >= 0.4 else "#ef4444"

        # Status badge
        status_colors = {
            "pending": "#f59e0b", "approved": "#22c55e",
            "overridden": "#8b5cf6", "changes_requested": "#3b82f6",
            "deferred": "#6b7280",
        }
        status_color = status_colors.get(status, "#6b7280")

        # Recommendation card
        rec_html = ""
        if rec:
            rec_html = f"""<div class="card rec-card">
  <h3>Recommendation</h3>
  <div class="winner"><strong>{_esc(rec.get('winner_branch', 'N/A'))}</strong>
    <span class="badge" style="background:{conf_color};">{conf_pct}%</span>
  </div>
  <div class="conf-bar"><div class="conf-fill" style="width:{conf_pct}%;background:{conf_color};"></div></div>
  <p class="reasoning">{_esc(rec.get('reasoning', ''))}</p>
  <p class="decision-type">Decision: <code>{_esc(rec.get('decision', ''))}</code></p>
</div>"""
        else:
            rec_html = '<div class="card"><h3>Recommendation</h3><p class="muted">No synthesis data available</p></div>'

        # Alternatives
        alt_html = ""
        if alternatives:
            alt_items = ""
            for alt in alternatives:
                alt_items += f"""<div class="alt-card">
  <strong>{_esc(alt.get('machine', ''))}</strong> &mdash; <code>{_esc(alt.get('branch', ''))}</code>
  <p>{_esc(alt.get('strength', ''))}</p>
</div>"""
            alt_html = f'<div class="card"><h3>Alternatives</h3>{alt_items}</div>'
        else:
            alt_html = '<div class="card"><h3>Alternatives</h3><p class="muted">No alternatives</p></div>'

        # Evidence trail
        ev_html = ""
        if evidence:
            ev_items = ""
            for ev in evidence[:10]:
                ts = _format_ts(ev.get("timestamp"))
                ev_items += f'<li><code>{ts}</code> [{_esc(ev.get("event_type", ""))}] {_esc(ev.get("subject", ""))} <span class="muted">by {_esc(ev.get("node_id", ""))}</span></li>'
            ev_html = f'<div class="card"><h3>Evidence Trail</h3><ul>{ev_items}</ul></div>'
        else:
            ev_html = '<div class="card"><h3>Evidence Trail</h3><p class="muted">No evidence recorded</p></div>'

        # Dissent
        dis_html = ""
        if dissent_list:
            dis_items = ""
            for d in dissent_list:
                dis_items += f"""<div class="dissent-item">
  <strong>{_esc(d.get('surgeon', 'unknown'))}</strong>
  {(' <span class="muted">(' + _esc(d.get('machine', '')) + ')</span>') if d.get('machine') else ''}
  <blockquote>{_esc(d.get('claim', ''))}</blockquote>
</div>"""
            dis_html = f'<div class="card"><h3>Dissent ({len(dissent_list)})</h3>{dis_items}</div>'
        else:
            dis_html = '<div class="card"><h3>Dissent</h3><p class="muted">No dissent — unanimous agreement</p></div>'

        # Memory influences
        mem_html = ""
        if memory_inf:
            mem_items = "".join(f"<li>{_esc(str(m))}</li>" for m in memory_inf)
            mem_html = f'<div class="card"><h3>Memory Influences</h3><ul>{mem_items}</ul></div>'

        # Action buttons (only for pending)
        actions_html = ""
        if status == "pending":
            actions_html = f"""<div class="actions">
  <button class="btn btn-approve" onclick="arbiterAction('{task_id}','approve')">Approve</button>
  <button class="btn btn-changes" onclick="arbiterAction('{task_id}','request_changes')">Request Changes</button>
  <button class="btn btn-override" onclick="arbiterAction('{task_id}','override')">Override</button>
  <button class="btn btn-defer" onclick="arbiterAction('{task_id}','defer')">Defer</button>
</div>
<script>
function arbiterAction(taskId, action) {{
  let body = {{}};
  if (action === 'override') {{
    const branch = prompt('Enter branch to use:');
    const reason = prompt('Reason for override:');
    if (!branch || !reason) return;
    body = {{chosen_branch: branch, reason: reason}};
  }} else if (action === 'request_changes') {{
    const feedback = prompt('What changes are needed?');
    if (!feedback) return;
    body = {{feedback: feedback}};
  }}
  fetch('/arbiter/' + taskId + '/' + action, {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify(body),
  }}).then(r => r.json()).then(d => {{
    if (d.error) alert('Error: ' + d.error);
    else location.reload();
  }});
}}
</script>"""

        return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Arbiter View — {task_id}</title>
<style>
  body {{ font-family: -apple-system, system-ui, sans-serif; background: #0f172a; color: #e2e8f0; margin: 0; padding: 1.5rem; }}
  h2 {{ font-size: 1.3rem; margin: 0 0 1rem; }}
  h3 {{ font-size: 1rem; margin: 0 0 0.5rem; border-bottom: 1px solid #334155; padding-bottom: 0.3rem; }}
  .header {{ display: flex; align-items: center; gap: 1rem; margin-bottom: 1.5rem; }}
  .status-badge {{ padding: 0.2rem 0.6rem; border-radius: 4px; font-size: 0.85rem; font-weight: 600; color: #0f172a; background: {status_color}; }}
  .card {{ background: #1e293b; border-radius: 8px; padding: 1rem; margin-bottom: 1rem; }}
  .rec-card {{ border-left: 4px solid {conf_color}; }}
  .winner {{ font-size: 1.1rem; margin-bottom: 0.5rem; }}
  .badge {{ padding: 0.15rem 0.5rem; border-radius: 3px; font-size: 0.8rem; color: #0f172a; margin-left: 0.5rem; }}
  .conf-bar {{ height: 6px; background: #334155; border-radius: 3px; margin-bottom: 0.5rem; }}
  .conf-fill {{ height: 100%; border-radius: 3px; transition: width 0.3s; }}
  .reasoning {{ color: #94a3b8; font-size: 0.9rem; }}
  .decision-type {{ font-size: 0.85rem; }}
  .alt-card {{ background: #0f172a; border-radius: 4px; padding: 0.6rem; margin: 0.4rem 0; }}
  .dissent-item {{ margin: 0.5rem 0; }}
  blockquote {{ border-left: 3px solid #475569; margin: 0.3rem 0 0.3rem 0.5rem; padding-left: 0.5rem; color: #94a3b8; font-style: italic; }}
  .muted {{ color: #64748b; font-size: 0.85rem; }}
  ul {{ padding-left: 1.2rem; }}
  li {{ margin: 0.2rem 0; font-size: 0.9rem; }}
  code {{ background: #334155; padding: 0.1rem 0.3rem; border-radius: 3px; font-size: 0.85rem; }}
  .actions {{ display: flex; gap: 0.8rem; margin-top: 1rem; flex-wrap: wrap; }}
  .btn {{ padding: 0.5rem 1.2rem; border: none; border-radius: 6px; font-weight: 600; cursor: pointer; font-size: 0.9rem; }}
  .btn-approve {{ background: #22c55e; color: #0f172a; }}
  .btn-changes {{ background: #3b82f6; color: white; }}
  .btn-override {{ background: #8b5cf6; color: white; }}
  .btn-defer {{ background: #475569; color: #e2e8f0; }}
  .btn:hover {{ opacity: 0.85; }}
</style>
</head>
<body>
<div class="header">
  <h2>Arbiter View</h2>
  <span class="status-badge">{_esc(status.upper())}</span>
  <span class="muted">Task: {task_id}</span>
</div>
{rec_html}
{alt_html}
{ev_html}
{dis_html}
{mem_html}
{actions_html}
</body>
</html>"""

    def render_markdown(self, decision: dict) -> str:
        """Render as markdown (for systemMessage injection)."""
        task_id = decision.get("task_id", "")
        status = decision.get("status", "pending")
        rec = decision.get("recommendation", {})
        alternatives = decision.get("alternatives", [])
        evidence = decision.get("evidence_trail", [])
        dissent_list = decision.get("dissent", [])
        confidence = decision.get("confidence", 0.0)

        lines = [
            f"## Arbiter Decision: {task_id}",
            f"**Status:** {status} | **Confidence:** {confidence:.0%}",
            "",
        ]

        if rec:
            lines.append("### Recommendation")
            lines.append(f"**Winner:** {rec.get('winner_branch', 'N/A')} ({rec.get('decision', '')})")
            lines.append(f"**Reasoning:** {rec.get('reasoning', '')}")
            lines.append("")

        if alternatives:
            lines.append("### Alternatives")
            for alt in alternatives:
                lines.append(f"- **{alt.get('machine', '')}** ({alt.get('branch', '')}): {alt.get('strength', '')}")
            lines.append("")

        if dissent_list:
            lines.append(f"### Dissent ({len(dissent_list)})")
            for d in dissent_list:
                lines.append(f"- **{d.get('surgeon', 'unknown')}**: {d.get('claim', '')}")
            lines.append("")

        if evidence:
            lines.append("### Evidence Trail")
            for ev in evidence[:5]:
                lines.append(f"- [{ev.get('event_type', '')}] {ev.get('subject', '')}")
            lines.append("")

        if status == "pending":
            lines.append("### Actions: approve | request_changes | override | defer")

        return "\n".join(lines)


# ── Helpers ──

def _esc(text) -> str:
    return html.escape(str(text)) if text else ""


def _format_ts(ts) -> str:
    if not ts:
        return "--"
    try:
        from datetime import datetime, timezone
        dt = datetime.fromtimestamp(float(ts), tz=timezone.utc)
        return dt.strftime("%H:%M:%S UTC")
    except (ValueError, TypeError, OSError):
        return str(ts)
