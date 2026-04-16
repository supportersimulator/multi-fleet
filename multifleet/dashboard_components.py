"""Theatrical Dashboard Components -- Superset-inspired visual widgets.

Four embeddable components that make AI intelligence VISIBLE:
  VitalSignsBar   -- real-time fleet health (heartbeat, latency, stress)
  SurgeonFeed     -- live 3-surgeon activity stream
  QuorumPulse     -- bacterial quorum signal flow
  EvidenceTimeline -- hash-chain evidence visualization

Each component: class with render(data)->HTML, get_sample_data()->dict.
All share the dark theme from dashboard_html.py (#1a1a2e, accent #00d4ff).
"""

from __future__ import annotations

import hashlib
import html
import time
from datetime import datetime, timezone
from typing import Any

# ── Theme constants ────────────────────────────────────────────────

BG_PRIMARY = "#1a1a2e"
BG_CARD = "#16213e"
BG_SURFACE = "#0f3460"
ACCENT = "#00d4ff"
ACCENT_DIM = "#0088aa"
TEXT_PRIMARY = "#e2e8f0"
TEXT_MUTED = "#64748b"
GREEN = "#22c55e"
YELLOW = "#eab308"
ORANGE = "#f97316"
RED = "#ef4444"
PURPLE = "#a855f7"
SURGEON_COLORS = {
    "atlas": ACCENT,
    "cardiologist": ORANGE,
    "neurologist": PURPLE,
}


def _esc(text: Any) -> str:
    return html.escape(str(text)) if text else ""


def _ts_fmt(ts: float | int | None) -> str:
    if not ts:
        return "\u2014"
    try:
        dt = datetime.fromtimestamp(float(ts), tz=timezone.utc)
        return dt.strftime("%H:%M:%S")
    except (ValueError, TypeError, OSError):
        return str(ts)


# ── Shared CSS (injected once per page) ────────────────────────────

SHARED_CSS = f"""
/* Theatrical dashboard component shared styles */
.tc-component {{
  font-family: 'JetBrains Mono', 'SF Mono', 'Fira Code', monospace;
  background: {BG_CARD};
  border: 1px solid {BG_SURFACE};
  border-radius: 8px;
  padding: 1rem;
  color: {TEXT_PRIMARY};
  margin-bottom: 1rem;
}}
.tc-component h3 {{
  margin: 0 0 0.75rem;
  font-size: 0.85rem;
  text-transform: uppercase;
  letter-spacing: 0.1em;
  color: {ACCENT};
  border-bottom: 1px solid {BG_SURFACE};
  padding-bottom: 0.4rem;
}}
.tc-component .muted {{ color: {TEXT_MUTED}; font-size: 0.8rem; }}
.tc-component code {{
  background: {BG_PRIMARY};
  padding: 0.1rem 0.35rem;
  border-radius: 3px;
  font-size: 0.8rem;
}}

/* Heartbeat animation */
@keyframes tc-heartbeat {{
  0%, 100% {{ transform: scale(1); opacity: 1; }}
  50% {{ transform: scale(1.3); opacity: 0.7; }}
}}
/* Pulse ring */
@keyframes tc-pulse-ring {{
  0% {{ box-shadow: 0 0 0 0 rgba(0,212,255,0.5); }}
  70% {{ box-shadow: 0 0 0 8px rgba(0,212,255,0); }}
  100% {{ box-shadow: 0 0 0 0 rgba(0,212,255,0); }}
}}
/* Fade-in slide */
@keyframes tc-slide-in {{
  0% {{ opacity: 0; transform: translateY(6px); }}
  100% {{ opacity: 1; transform: translateY(0); }}
}}
/* Signal flow */
@keyframes tc-signal-flow {{
  0% {{ background-position: 0% 50%; }}
  100% {{ background-position: 200% 50%; }}
}}
/* Confidence bar fill */
@keyframes tc-fill {{
  0% {{ width: 0; }}
  100% {{ width: var(--fill-pct); }}
}}
/* Chain link pulse */
@keyframes tc-chain-pulse {{
  0%, 100% {{ border-color: {BG_SURFACE}; }}
  50% {{ border-color: {ACCENT}; }}
}}
"""


def _wrap_component(name: str, inner: str) -> str:
    """Wrap inner HTML in the standard component container."""
    return f'<div class="tc-component tc-{name}">{inner}</div>'


# ═══════════════════════════════════════════════════════════════════
# 1. VitalSignsBar
# ═══════════════════════════════════════════════════════════════════

class VitalSignsBar:
    """Real-time fleet health visualization.

    Data keys:
      nodes: list of {id, status, last_seen, latency_history, stress, channels}
        - id: str (mac1, mac2, mac3)
        - status: str (online|degraded|offline)
        - last_seen: float (unix ts)
        - latency_history: list[float] (last 20 data points, ms)
        - stress: float (0.0-1.0)
        - channels: dict {P1..P7: bool}
    """

    @classmethod
    def get_sample_data(cls) -> dict:
        now = time.time()
        return {
            "nodes": [
                {
                    "id": "mac1",
                    "status": "online",
                    "last_seen": now - 3,
                    "latency_history": [12, 14, 11, 13, 15, 12, 18, 14, 13, 11,
                                        12, 16, 14, 13, 12, 11, 15, 13, 14, 12],
                    "stress": 0.25,
                    "channels": {"P1": True, "P2": True, "P3": True,
                                 "P4": True, "P5": True, "P6": False, "P7": True},
                },
                {
                    "id": "mac2",
                    "status": "degraded",
                    "last_seen": now - 45,
                    "latency_history": [22, 28, 35, 42, 38, 45, 50, 48, 44, 40,
                                        38, 35, 42, 55, 60, 52, 48, 44, 40, 38],
                    "stress": 0.65,
                    "channels": {"P1": False, "P2": True, "P3": True,
                                 "P4": True, "P5": True, "P6": False, "P7": True},
                },
                {
                    "id": "mac3",
                    "status": "offline",
                    "last_seen": now - 300,
                    "latency_history": [0] * 20,
                    "stress": 0.0,
                    "channels": {"P1": False, "P2": False, "P3": False,
                                 "P4": False, "P5": False, "P6": True, "P7": True},
                },
            ]
        }

    def render(self, data: dict) -> str:
        nodes = data.get("nodes", [])
        if not nodes:
            return _wrap_component("vitals", "<h3>Vital Signs</h3><p class='muted'>No node data</p>")

        cards = []
        for node in nodes:
            nid = _esc(node.get("id", "?"))
            status = node.get("status", "offline")
            last_seen = node.get("last_seen")
            stress = float(node.get("stress", 0))
            latency = node.get("latency_history", [])
            channels = node.get("channels", {})

            # Status color + heartbeat
            status_color = GREEN if status == "online" else YELLOW if status == "degraded" else RED
            heartbeat_style = (
                f"animation: tc-heartbeat 1s ease-in-out infinite;"
                if status == "online"
                else f"animation: tc-heartbeat 2.5s ease-in-out infinite;"
                if status == "degraded"
                else ""
            )

            # Stress gauge color
            if stress < 0.3:
                stress_color = GREEN
            elif stress < 0.6:
                stress_color = YELLOW
            elif stress < 0.8:
                stress_color = ORANGE
            else:
                stress_color = RED

            # Sparkline SVG (last 20 points)
            sparkline = self._render_sparkline(latency)

            # Channel indicators
            ch_dots = ""
            for ch_name in ("P1", "P2", "P3", "P4", "P5", "P6", "P7"):
                up = channels.get(ch_name, False)
                ch_color = GREEN if up else f"{RED}55"
                ch_dots += (
                    f'<span title="{ch_name}" style="display:inline-block;width:8px;height:8px;'
                    f'border-radius:50%;background:{ch_color};margin:0 1px;"></span>'
                )

            # Last seen
            age = ""
            if last_seen:
                delta = int(time.time() - last_seen)
                if delta < 60:
                    age = f"{delta}s ago"
                elif delta < 3600:
                    age = f"{delta // 60}m ago"
                else:
                    age = f"{delta // 3600}h ago"

            cards.append(f"""
            <div style="flex:1;min-width:180px;background:{BG_PRIMARY};border-radius:6px;padding:0.6rem;
                        border-left:3px solid {status_color};">
              <div style="display:flex;align-items:center;gap:0.4rem;margin-bottom:0.4rem;">
                <span style="display:inline-block;width:10px;height:10px;border-radius:50%;
                             background:{status_color};{heartbeat_style}"></span>
                <strong style="font-size:0.9rem;">{nid}</strong>
                <span class="muted" style="margin-left:auto;">{_esc(age)}</span>
              </div>
              <div style="margin-bottom:0.3rem;">{sparkline}</div>
              <div style="display:flex;align-items:center;gap:0.5rem;margin-bottom:0.3rem;">
                <span class="muted" style="font-size:0.75rem;">STRESS</span>
                <div style="flex:1;height:6px;background:{BG_SURFACE};border-radius:3px;overflow:hidden;">
                  <div style="width:{stress*100:.0f}%;height:100%;background:{stress_color};
                              border-radius:3px;--fill-pct:{stress*100:.0f}%;
                              animation:tc-fill 0.8s ease-out;"></div>
                </div>
                <span style="font-size:0.75rem;color:{stress_color};">{stress:.2f}</span>
              </div>
              <div style="display:flex;align-items:center;gap:0.3rem;">
                <span class="muted" style="font-size:0.7rem;">CH</span>
                {ch_dots}
              </div>
            </div>""")

        inner = f"""<h3>Vital Signs</h3>
        <div style="display:flex;gap:0.75rem;flex-wrap:wrap;">{"".join(cards)}</div>"""
        return _wrap_component("vitals", inner)

    @staticmethod
    def _render_sparkline(values: list[float], width: int = 120, height: int = 24) -> str:
        if not values or all(v == 0 for v in values):
            return (
                f'<svg width="{width}" height="{height}" style="display:block;">'
                f'<line x1="0" y1="{height//2}" x2="{width}" y2="{height//2}" '
                f'stroke="{TEXT_MUTED}" stroke-width="1" stroke-dasharray="4,3"/></svg>'
            )
        mn, mx = min(values), max(values)
        rng = mx - mn if mx != mn else 1
        step = width / max(len(values) - 1, 1)
        points = []
        for i, v in enumerate(values):
            x = i * step
            y = height - ((v - mn) / rng) * (height - 4) - 2
            points.append(f"{x:.1f},{y:.1f}")
        poly = " ".join(points)
        return (
            f'<svg width="{width}" height="{height}" style="display:block;">'
            f'<polyline points="{poly}" fill="none" stroke="{ACCENT}" stroke-width="1.5" '
            f'stroke-linecap="round" stroke-linejoin="round"/></svg>'
        )


# ═══════════════════════════════════════════════════════════════════
# 2. SurgeonFeed
# ═══════════════════════════════════════════════════════════════════

class SurgeonFeed:
    """Live stream of 3-surgeon activity.

    Data keys:
      entries: list of {
        timestamp: float,
        surgeon: str (atlas|cardiologist|neurologist),
        action: str,
        detail: str,
        confidence: float (0-1),
        phase: str (initial|cross_review|synthesis|verdict),
      }
      active_gates: list of {name, status (pass|fail), reason}
    """

    @classmethod
    def get_sample_data(cls) -> dict:
        now = time.time()
        return {
            "entries": [
                {
                    "timestamp": now - 120,
                    "surgeon": "atlas",
                    "action": "Initiating cross-examination",
                    "detail": "transport adapter design review",
                    "confidence": 0.0,
                    "phase": "initial",
                },
                {
                    "timestamp": now - 115,
                    "surgeon": "cardiologist",
                    "action": "Initial analysis",
                    "detail": "Hidden coupling between packet_store and nats_client detected",
                    "confidence": 0.82,
                    "phase": "initial",
                },
                {
                    "timestamp": now - 112,
                    "surgeon": "neurologist",
                    "action": "Pattern match",
                    "detail": "adapter pattern matches prior failure in session 3c8a",
                    "confidence": 0.71,
                    "phase": "initial",
                },
                {
                    "timestamp": now - 100,
                    "surgeon": "atlas",
                    "action": "Cross-review synthesis",
                    "detail": "Key tension: coupling risk vs migration cost",
                    "confidence": 0.85,
                    "phase": "synthesis",
                },
                {
                    "timestamp": now - 90,
                    "surgeon": "cardiologist",
                    "action": "Dissent",
                    "detail": "Migration cost underestimated -- 3 downstream consumers affected",
                    "confidence": 0.78,
                    "phase": "cross_review",
                },
            ],
            "active_gates": [
                {"name": "architectural-gate", "status": "pass", "reason": "Blast radius < 3 files"},
                {"name": "pre-implementation", "status": "fail", "reason": "Missing test coverage plan"},
            ],
        }

    def render(self, data: dict) -> str:
        entries = data.get("entries", [])
        gates = data.get("active_gates", [])

        # Gate badges
        gate_html = ""
        if gates:
            badges = []
            for g in gates:
                color = GREEN if g.get("status") == "pass" else RED
                icon = "\u2713" if g.get("status") == "pass" else "\u2717"
                badges.append(
                    f'<span title="{_esc(g.get("reason", ""))}" '
                    f'style="display:inline-block;padding:0.15rem 0.5rem;border-radius:12px;'
                    f'font-size:0.7rem;background:{color}22;color:{color};border:1px solid {color}44;'
                    f'margin-right:0.3rem;">{icon} {_esc(g.get("name", "?"))}</span>'
                )
            gate_html = f'<div style="margin-bottom:0.75rem;">{"".join(badges)}</div>'

        # Feed entries
        items = []
        for i, entry in enumerate(entries):
            surgeon = entry.get("surgeon", "unknown")
            s_color = SURGEON_COLORS.get(surgeon, TEXT_MUTED)
            ts = _ts_fmt(entry.get("timestamp"))
            action = _esc(entry.get("action", ""))
            detail = _esc(entry.get("detail", ""))
            confidence = entry.get("confidence", 0)
            phase = entry.get("phase", "")

            # Phase indicator
            phase_labels = {
                "initial": ("I", TEXT_MUTED),
                "cross_review": ("X", YELLOW),
                "synthesis": ("S", ACCENT),
                "verdict": ("V", GREEN),
            }
            p_label, p_color = phase_labels.get(phase, ("?", TEXT_MUTED))

            # Confidence bar
            conf_html = ""
            if confidence > 0:
                conf_color = GREEN if confidence >= 0.7 else YELLOW if confidence >= 0.4 else RED
                conf_html = (
                    f'<div style="display:flex;align-items:center;gap:0.3rem;margin-top:0.2rem;">'
                    f'<div style="width:60px;height:4px;background:{BG_PRIMARY};border-radius:2px;overflow:hidden;">'
                    f'<div style="width:{confidence*100:.0f}%;height:100%;background:{conf_color};'
                    f'border-radius:2px;--fill-pct:{confidence*100:.0f}%;'
                    f'animation:tc-fill 0.6s ease-out;"></div></div>'
                    f'<span style="font-size:0.7rem;color:{conf_color};">{confidence:.0%}</span></div>'
                )

            # Dissent styling
            is_dissent = "dissent" in action.lower()
            border = f"border-left:2px solid {RED};" if is_dissent else f"border-left:2px solid {s_color}33;"

            items.append(
                f'<div style="padding:0.4rem 0.6rem;margin-bottom:0.3rem;background:{BG_PRIMARY};'
                f'border-radius:4px;{border}animation:tc-slide-in 0.3s ease-out;">'
                f'<div style="display:flex;align-items:center;gap:0.4rem;">'
                f'<span style="font-size:0.7rem;color:{TEXT_MUTED};">{ts}</span>'
                f'<span style="font-size:0.65rem;width:14px;height:14px;border-radius:50%;'
                f'background:{p_color}22;color:{p_color};display:flex;align-items:center;'
                f'justify-content:center;font-weight:700;">{p_label}</span>'
                f'<strong style="color:{s_color};font-size:0.8rem;">{_esc(surgeon.upper())}</strong>'
                f'<span style="font-size:0.8rem;">{action}</span></div>'
                f'<div style="font-size:0.8rem;color:{TEXT_MUTED};margin-top:0.15rem;'
                f'padding-left:1.8rem;">{detail}</div>'
                f'{conf_html}</div>'
            )

        if not items:
            items = ['<p class="muted">No surgeon activity</p>']

        inner = f"<h3>Surgeon Feed</h3>{gate_html}{''.join(items)}"
        return _wrap_component("surgeon-feed", inner)


# ═══════════════════════════════════════════════════════════════════
# 3. QuorumPulse
# ═══════════════════════════════════════════════════════════════════

class QuorumPulse:
    """Bacterial quorum signal flow visualization.

    Data keys:
      signals: dict {las: int, rhl: int, pqs: int, iqs: int}
      proposals: list of {id, title, support, needed, status}
      quorum_threshold: float (0-1)
      quorum_met: bool
    """

    SIGNAL_META = {
        "las": ("LAS", "Strategic", ACCENT),
        "rhl": ("RHL", "Tactical", GREEN),
        "pqs": ("PQS", "State", YELLOW),
        "iqs": ("IQS", "Stress", RED),
    }

    @classmethod
    def get_sample_data(cls) -> dict:
        return {
            "signals": {"las": 42, "rhl": 128, "pqs": 7, "iqs": 3},
            "proposals": [
                {"id": "prop-001", "title": "Migrate packet store to SQLite",
                 "support": 2, "needed": 2, "status": "approved"},
                {"id": "prop-002", "title": "Add P8 mesh channel",
                 "support": 1, "needed": 2, "status": "pending"},
            ],
            "quorum_threshold": 0.67,
            "quorum_met": True,
        }

    def render(self, data: dict) -> str:
        signals = data.get("signals", {})
        proposals = data.get("proposals", [])
        threshold = data.get("quorum_threshold", 0.67)
        quorum_met = data.get("quorum_met", False)

        # Signal bars
        max_count = max((signals.get(k, 0) for k in self.SIGNAL_META), default=1) or 1
        signal_bars = []
        for key, (label, desc, color) in self.SIGNAL_META.items():
            count = signals.get(key, 0)
            pct = min(count / max_count * 100, 100)
            signal_bars.append(
                f'<div style="display:flex;align-items:center;gap:0.5rem;margin-bottom:0.35rem;">'
                f'<span style="width:28px;font-size:0.7rem;font-weight:700;color:{color};">{label}</span>'
                f'<div style="flex:1;height:8px;background:{BG_PRIMARY};border-radius:4px;overflow:hidden;'
                f'position:relative;">'
                f'<div style="width:{pct:.0f}%;height:100%;background:linear-gradient(90deg,{color},{color}88);'
                f'border-radius:4px;background-size:200% 100%;'
                f'animation:tc-signal-flow 2s linear infinite;"></div></div>'
                f'<span style="font-size:0.75rem;min-width:30px;text-align:right;">{count}</span>'
                f'<span class="muted" style="font-size:0.65rem;">{desc}</span></div>'
            )

        # Quorum threshold indicator
        q_color = GREEN if quorum_met else RED
        q_icon = "\u2713" if quorum_met else "\u2717"
        threshold_html = (
            f'<div style="display:flex;align-items:center;gap:0.4rem;margin:0.6rem 0 0.5rem;'
            f'padding:0.3rem 0.5rem;background:{q_color}11;border:1px solid {q_color}33;'
            f'border-radius:4px;">'
            f'<span style="color:{q_color};font-weight:700;">{q_icon}</span>'
            f'<span style="font-size:0.8rem;">Quorum {threshold:.0%}</span>'
            f'<span style="color:{q_color};font-size:0.8rem;font-weight:600;margin-left:auto;">'
            f'{"MET" if quorum_met else "NOT MET"}</span></div>'
        )

        # Proposals
        prop_html = ""
        if proposals:
            rows = []
            for p in proposals:
                status = p.get("status", "pending")
                s_color = GREEN if status == "approved" else YELLOW if status == "pending" else RED
                support = p.get("support", 0)
                needed = p.get("needed", 1)
                title = _esc(p.get("title", "?"))
                pid = _esc(p.get("id", "?"))
                rows.append(
                    f'<div style="display:flex;align-items:center;gap:0.5rem;padding:0.3rem 0;'
                    f'border-bottom:1px solid {BG_SURFACE};">'
                    f'<code style="font-size:0.7rem;">{pid}</code>'
                    f'<span style="font-size:0.8rem;flex:1;">{title}</span>'
                    f'<span style="font-size:0.75rem;">{support}/{needed}</span>'
                    f'<span style="font-size:0.7rem;color:{s_color};font-weight:600;">'
                    f'{_esc(status.upper())}</span></div>'
                )
            prop_html = f'<div style="margin-top:0.4rem;">{"".join(rows)}</div>'

        inner = (
            f"<h3>Quorum Pulse</h3>"
            f'<div style="margin-bottom:0.3rem;">{"".join(signal_bars)}</div>'
            f"{threshold_html}"
            f"{prop_html}"
        )
        return _wrap_component("quorum-pulse", inner)


# ═══════════════════════════════════════════════════════════════════
# 4. EvidenceTimeline
# ═══════════════════════════════════════════════════════════════════

class EvidenceTimeline:
    """Hash-chain evidence visualization.

    Data keys:
      entries: list of {
        hash: str (SHA-256),
        prev_hash: str | None,
        timestamp: float,
        node: str,
        analyzer: str,
        summary: str,
        detail: str (full evidence, shown on expand),
      }
      chain_valid: bool
    """

    @classmethod
    def get_sample_data(cls) -> dict:
        now = time.time()
        entries = []
        prev = None
        for i in range(10):
            h = hashlib.sha256(f"evidence-{i}-{now}".encode()).hexdigest()
            entries.append({
                "hash": h,
                "prev_hash": prev,
                "timestamp": now - (9 - i) * 60,
                "node": ["mac1", "mac2", "mac3"][i % 3],
                "analyzer": ["probe_engine", "ghost_layer", "surgeon_adapter",
                             "invariance_gates", "evidence_ledger"][i % 5],
                "summary": [
                    "mirror_drift probe passed",
                    "hidden_dependency warning: nats_client coupling",
                    "context_staleness check OK",
                    "surgeon cross-exam consensus reached",
                    "invariant coverage verified 94%",
                    "ghost anticipation cache refreshed",
                    "acceptance gate PASS for PR #42",
                    "neurologist challenge resolved",
                    "cardiologist dissent on adapter pattern",
                    "evidence chain integrity verified",
                ][i],
                "detail": f"Full evidence payload for entry {i}. "
                          f"Hash: {h[:16]}... Chain link #{i}.",
            })
            prev = h
        return {"entries": entries, "chain_valid": True}

    def render(self, data: dict) -> str:
        entries = data.get("entries", [])
        chain_valid = data.get("chain_valid", True)

        # Chain status badge
        chain_color = GREEN if chain_valid else RED
        chain_icon = "\u26d3" if chain_valid else "\u26a0"
        chain_label = "UNBROKEN" if chain_valid else "BROKEN"
        chain_badge = (
            f'<div style="display:flex;align-items:center;gap:0.4rem;margin-bottom:0.6rem;">'
            f'<span style="font-size:1rem;">{chain_icon}</span>'
            f'<span style="font-size:0.8rem;color:{chain_color};font-weight:600;">'
            f'Chain: {chain_label}</span>'
            f'<span class="muted" style="margin-left:auto;">{len(entries)} entries</span></div>'
        )

        # Timeline entries
        items = []
        for i, entry in enumerate(entries):
            h = entry.get("hash", "")
            h_short = h[:12] if h else "?"
            ts = _ts_fmt(entry.get("timestamp"))
            node = _esc(entry.get("node", "?"))
            analyzer = _esc(entry.get("analyzer", "?"))
            summary = _esc(entry.get("summary", ""))
            detail = _esc(entry.get("detail", ""))

            # Node color
            node_colors = {"mac1": ACCENT, "mac2": ORANGE, "mac3": PURPLE}
            n_color = node_colors.get(node, TEXT_MUTED)

            # Connector line (except first)
            connector = (
                f'<div style="width:2px;height:12px;background:{BG_SURFACE};margin-left:7px;'
                f'animation:tc-chain-pulse 3s ease-in-out infinite;"></div>'
                if i > 0 else ""
            )

            entry_id = f"ev-{h_short}"
            items.append(
                f'{connector}'
                f'<div style="display:flex;gap:0.5rem;align-items:flex-start;cursor:pointer;" '
                f'onclick="(function(){{var d=document.getElementById(\'{entry_id}\');'
                f'd.style.display=d.style.display===\'none\'?\'block\':\'none\';}})()">'
                f'<div style="width:16px;height:16px;border-radius:50%;border:2px solid {n_color};'
                f'background:{BG_PRIMARY};flex-shrink:0;margin-top:2px;'
                f'animation:tc-pulse-ring 2.5s ease-out infinite;"></div>'
                f'<div style="flex:1;min-width:0;">'
                f'<div style="display:flex;align-items:center;gap:0.4rem;flex-wrap:wrap;">'
                f'<code style="color:{ACCENT};font-size:0.75rem;">{h_short}</code>'
                f'<span style="font-size:0.7rem;color:{TEXT_MUTED};">{ts}</span>'
                f'<span style="font-size:0.7rem;color:{n_color};font-weight:600;">{node}</span>'
                f'<span style="font-size:0.65rem;color:{TEXT_MUTED};">{analyzer}</span></div>'
                f'<div style="font-size:0.8rem;margin-top:0.1rem;">{summary}</div>'
                f'<div id="{entry_id}" style="display:none;font-size:0.75rem;color:{TEXT_MUTED};'
                f'margin-top:0.2rem;padding:0.3rem;background:{BG_PRIMARY};border-radius:3px;'
                f'border-left:2px solid {n_color};">{detail}</div>'
                f'</div></div>'
            )

        if not items:
            items = ['<p class="muted">No evidence entries</p>']

        inner = f"<h3>Evidence Timeline</h3>{chain_badge}{''.join(items)}"
        return _wrap_component("evidence-timeline", inner)


# ═══════════════════════════════════════════════════════════════════
# 5. FleetConstellation
# ═══════════════════════════════════════════════════════════════════

class FleetConstellation:
    """Node topology visualization with connection quality lines.

    Data keys:
      nodes: list of {id, status (online|degraded|offline), x, y}
      connections: list of {from, to, health (0.0-1.0), channel}
    """

    NODE_POSITIONS = {"mac1": (80, 40), "mac2": (220, 40), "mac3": (150, 140)}

    @classmethod
    def get_sample_data(cls) -> dict:
        return {
            "nodes": [
                {"id": "mac1", "status": "online", "x": 80, "y": 40},
                {"id": "mac2", "status": "degraded", "x": 220, "y": 40},
                {"id": "mac3", "status": "offline", "x": 150, "y": 140},
            ],
            "connections": [
                {"from": "mac1", "to": "mac2", "health": 0.9, "channel": "P1"},
                {"from": "mac1", "to": "mac3", "health": 0.4, "channel": "P7"},
                {"from": "mac2", "to": "mac3", "health": 0.1, "channel": "P7"},
            ],
        }

    def _health_color(self, health: float) -> str:
        if health >= 0.7:
            return GREEN
        if health >= 0.4:
            return YELLOW
        return RED

    def _status_color(self, status: str) -> str:
        if status == "online":
            return GREEN
        if status == "degraded":
            return YELLOW
        return RED

    def render(self, data: dict) -> str:
        nodes = data.get("nodes", [])
        connections = data.get("connections", [])

        if not nodes:
            return _wrap_component("fleet-constellation",
                                   "<h3>Fleet Constellation</h3><p class='muted'>No topology data</p>")

        # Build position lookup
        pos = {}
        for n in nodes:
            pos[n["id"]] = (n.get("x", 150), n.get("y", 80))

        svg_w, svg_h = 300, 180

        # Connection lines
        lines = []
        for conn in connections:
            f_id, t_id = conn.get("from", ""), conn.get("to", "")
            if f_id not in pos or t_id not in pos:
                continue
            fx, fy = pos[f_id]
            tx, ty = pos[t_id]
            health = float(conn.get("health", 0))
            color = self._health_color(health)
            ch = _esc(conn.get("channel", "?"))
            # midpoint label
            mx, my = (fx + tx) / 2, (fy + ty) / 2
            dash = "" if health >= 0.7 else 'stroke-dasharray="6,4"'
            lines.append(
                f'<line x1="{fx}" y1="{fy}" x2="{tx}" y2="{ty}" '
                f'stroke="{color}" stroke-width="2" {dash} opacity="0.8"/>'
                f'<text x="{mx}" y="{my - 6}" fill="{TEXT_MUTED}" '
                f'font-size="9" text-anchor="middle">{ch} {health:.0%}</text>'
            )

        # Node circles
        node_els = []
        for n in nodes:
            nid = n["id"]
            nx, ny = pos[nid]
            status = n.get("status", "offline")
            color = self._status_color(status)
            pulse = 'style="animation:tc-pulse-ring 2s ease-out infinite;"' if status == "online" else ""
            node_els.append(
                f'<circle cx="{nx}" cy="{ny}" r="12" fill="{BG_PRIMARY}" '
                f'stroke="{color}" stroke-width="2" {pulse}/>'
                f'<circle cx="{nx}" cy="{ny}" r="4" fill="{color}"/>'
                f'<text x="{nx}" y="{ny + 24}" fill="{TEXT_PRIMARY}" '
                f'font-size="11" text-anchor="middle" font-weight="600">{_esc(nid)}</text>'
                f'<text x="{nx}" y="{ny + 35}" fill="{color}" '
                f'font-size="8" text-anchor="middle">{_esc(status.upper())}</text>'
            )

        svg = (
            f'<svg width="{svg_w}" height="{svg_h}" '
            f'style="display:block;margin:0 auto;">'
            f'{"".join(lines)}{"".join(node_els)}</svg>'
        )

        inner = f"<h3>Fleet Constellation</h3>{svg}"
        return _wrap_component("fleet-constellation", inner)


# ═══════════════════════════════════════════════════════════════════
# 6. MemoryHeatMap
# ═══════════════════════════════════════════════════════════════════

class MemoryHeatMap:
    """ContextDNA knowledge density by topic.

    Data keys:
      topics: list of {name, density (0.0-1.0), count, recent: list[str]}
    """

    TOPIC_DEFAULTS = ["fleet", "surgeons", "probes", "evidence",
                      "quorum", "webhook", "synaptic"]

    @classmethod
    def get_sample_data(cls) -> dict:
        return {
            "topics": [
                {"name": "fleet", "density": 0.92, "count": 347,
                 "recent": ["mac2 SSH fixed", "P1 NATS restored"]},
                {"name": "surgeons", "density": 0.78, "count": 215,
                 "recent": ["cross-exam consensus", "dissent on adapter"]},
                {"name": "probes", "density": 0.65, "count": 128,
                 "recent": ["mirror_drift OK", "context_staleness warning"]},
                {"name": "evidence", "density": 0.85, "count": 290,
                 "recent": ["chain integrity verified", "new ledger entry"]},
                {"name": "quorum", "density": 0.42, "count": 64,
                 "recent": ["proposal prop-002 pending"]},
                {"name": "webhook", "density": 0.95, "count": 412,
                 "recent": ["S2 professor injection OK", "S8 Synaptic active"]},
                {"name": "synaptic", "density": 0.70, "count": 183,
                 "recent": ["pattern recognition: adapter drift"]},
            ]
        }

    def _density_color(self, density: float) -> str:
        """Return color with opacity based on density."""
        if density >= 0.8:
            return ACCENT
        if density >= 0.6:
            return GREEN
        if density >= 0.4:
            return YELLOW
        return TEXT_MUTED

    def render(self, data: dict) -> str:
        topics = data.get("topics", [])
        if not topics:
            return _wrap_component("memory-heatmap",
                                   "<h3>Memory Heat Map</h3><p class='muted'>No topic data</p>")

        cells = []
        for topic in topics:
            name = _esc(topic.get("name", "?"))
            density = float(topic.get("density", 0))
            count = int(topic.get("count", 0))
            recent = topic.get("recent", [])
            color = self._density_color(density)
            opacity = max(0.3, density)

            # Recent entries for expand
            recent_html = ""
            if recent:
                items = "".join(
                    f'<div style="font-size:0.7rem;color:{TEXT_MUTED};padding:0.1rem 0;">'
                    f'\u2022 {_esc(r)}</div>' for r in recent[:3]
                )
                cell_id = f"hm-{name}"
                recent_html = (
                    f'<div id="{cell_id}" style="display:none;margin-top:0.3rem;'
                    f'border-top:1px solid {BG_SURFACE};padding-top:0.2rem;">{items}</div>'
                )
                onclick = (
                    f' onclick="(function(){{var d=document.getElementById(\'{cell_id}\');'
                    f'd.style.display=d.style.display===\'none\'?\'block\':\'none\';}})()"'
                    f' style="cursor:pointer;"'
                )
            else:
                onclick = ""

            cells.append(
                f'<div{onclick} class="tc-heatmap-cell" '
                f'style="background:{BG_PRIMARY};border-radius:6px;padding:0.5rem;'
                f'border-left:3px solid {color};opacity:{opacity:.2f};'
                f'min-width:120px;flex:1;">'
                f'<div style="display:flex;justify-content:space-between;align-items:center;">'
                f'<strong style="font-size:0.8rem;color:{color};">{name.upper()}</strong>'
                f'<span style="font-size:0.7rem;color:{TEXT_MUTED};">{count}</span></div>'
                f'<div style="margin-top:0.3rem;height:6px;background:{BG_SURFACE};'
                f'border-radius:3px;overflow:hidden;">'
                f'<div style="width:{density*100:.0f}%;height:100%;background:{color};'
                f'border-radius:3px;--fill-pct:{density*100:.0f}%;'
                f'animation:tc-fill 0.8s ease-out;"></div></div>'
                f'{recent_html}</div>'
            )

        inner = (
            f"<h3>Memory Heat Map</h3>"
            f'<div style="display:flex;flex-wrap:wrap;gap:0.5rem;">{"".join(cells)}</div>'
        )
        return _wrap_component("memory-heatmap", inner)


# ═══════════════════════════════════════════════════════════════════
# 7. ProbeStatusGrid
# ═══════════════════════════════════════════════════════════════════

class ProbeStatusGrid:
    """Grid of 18 probes with last-fire timestamps and status.

    Data keys:
      probes: list of {name, last_run (unix ts), score (0.0-1.0),
                       status (passing|stale|failing)}
    """

    PROBE_NAMES = [
        "mirror_drift", "hidden_dependency", "context_staleness",
        "ghost_anticipation", "invariant_coverage", "acceptance_gate",
        "neurologist_pulse", "cardiologist_ekg", "evidence_integrity",
        "quorum_consensus", "fleet_connectivity", "webhook_health",
        "memory_density", "session_gold", "priority_queue",
        "surgeon_consensus", "channel_health", "constitutional_compliance",
    ]

    @classmethod
    def get_sample_data(cls) -> dict:
        now = time.time()
        probes = []
        for i, name in enumerate(cls.PROBE_NAMES):
            if i < 10:
                status = "passing"
                score = 0.85 + (i % 5) * 0.03
                last_run = now - (i * 120)
            elif i < 14:
                status = "stale"
                score = 0.6 + (i % 3) * 0.05
                last_run = now - 7200 - (i * 300)
            else:
                status = "failing"
                score = 0.2 + (i % 4) * 0.05
                last_run = now - 300 - (i * 60)
            probes.append({
                "name": name,
                "last_run": last_run,
                "score": round(score, 2),
                "status": status,
            })
        return {"probes": probes}

    def _status_color(self, status: str) -> str:
        if status == "passing":
            return GREEN
        if status == "stale":
            return YELLOW
        return RED

    def render(self, data: dict) -> str:
        probes = data.get("probes", [])
        if not probes:
            return _wrap_component("probe-status-grid",
                                   "<h3>Probe Status Grid</h3><p class='muted'>No probe data</p>")

        # Summary counts
        passing = sum(1 for p in probes if p.get("status") == "passing")
        stale = sum(1 for p in probes if p.get("status") == "stale")
        failing = sum(1 for p in probes if p.get("status") == "failing")

        summary = (
            f'<div style="display:flex;gap:1rem;margin-bottom:0.6rem;font-size:0.8rem;">'
            f'<span style="color:{GREEN};">\u2713 {passing} passing</span>'
            f'<span style="color:{YELLOW};">\u25cb {stale} stale</span>'
            f'<span style="color:{RED};">\u2717 {failing} failing</span></div>'
        )

        cards = []
        for probe in probes:
            name = _esc(probe.get("name", "?"))
            last_run = probe.get("last_run")
            score = float(probe.get("score", 0))
            status = probe.get("status", "failing")
            color = self._status_color(status)

            # Time ago
            age = _ts_fmt(last_run)
            if last_run:
                delta = int(time.time() - last_run)
                if delta < 60:
                    age = f"{delta}s ago"
                elif delta < 3600:
                    age = f"{delta // 60}m ago"
                else:
                    age = f"{delta // 3600}h ago"

            icon = "\u2713" if status == "passing" else "\u25cb" if status == "stale" else "\u2717"

            cards.append(
                f'<div style="background:{BG_PRIMARY};border-radius:4px;padding:0.4rem 0.5rem;'
                f'border-left:3px solid {color};min-width:140px;flex:1;">'
                f'<div style="display:flex;align-items:center;gap:0.3rem;">'
                f'<span style="color:{color};font-size:0.85rem;">{icon}</span>'
                f'<span style="font-size:0.75rem;font-weight:600;">{name}</span></div>'
                f'<div style="display:flex;justify-content:space-between;margin-top:0.2rem;">'
                f'<span class="muted" style="font-size:0.7rem;">{age}</span>'
                f'<span style="font-size:0.7rem;color:{color};">{score:.0%}</span></div>'
                f'</div>'
            )

        inner = (
            f"<h3>Probe Status Grid</h3>"
            f"{summary}"
            f'<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));'
            f'gap:0.4rem;">{"".join(cards)}</div>'
        )
        return _wrap_component("probe-status-grid", inner)


# ═══════════════════════════════════════════════════════════════════
# 8. CorrigibilityGauge
# ═══════════════════════════════════════════════════════════════════

class CorrigibilityGauge:
    """System corrigibility score with circular gauge and trend.

    Data keys:
      score: float (0-100)
      trend: str (up|down|stable)
      history: list[float] (last 5 readings)
      factors: list of {name, score (0-100), weight (0.0-1.0)}
    """

    @classmethod
    def get_sample_data(cls) -> dict:
        return {
            "score": 87.5,
            "trend": "up",
            "history": [82.0, 84.5, 85.0, 86.2, 87.5],
            "factors": [
                {"name": "Surgeon consensus", "score": 92, "weight": 0.35},
                {"name": "Evidence backing", "score": 85, "weight": 0.30},
                {"name": "Constitutional compliance", "score": 88, "weight": 0.35},
            ],
        }

    def render(self, data: dict) -> str:
        score = float(data.get("score", 0))
        trend = data.get("trend", "stable")
        history = data.get("history", [])
        factors = data.get("factors", [])

        # Trend arrow
        trend_arrows = {"up": "\u2191", "down": "\u2193", "stable": "\u2192"}
        trend_colors = {"up": GREEN, "down": RED, "stable": YELLOW}
        arrow = trend_arrows.get(trend, "\u2192")
        arrow_color = trend_colors.get(trend, YELLOW)

        # Score color
        if score >= 80:
            score_color = GREEN
        elif score >= 60:
            score_color = YELLOW
        elif score >= 40:
            score_color = ORANGE
        else:
            score_color = RED

        # SVG circular gauge
        cx, cy, r = 60, 60, 45
        circumference = 2 * 3.14159 * r
        # Arc from 0% to score% (starting from top)
        arc_length = circumference * (score / 100)
        gap = circumference - arc_length

        gauge_svg = (
            f'<svg width="120" height="130" style="display:block;margin:0 auto;">'
            # Background circle
            f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" '
            f'stroke="{BG_SURFACE}" stroke-width="8"/>'
            # Score arc
            f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" '
            f'stroke="{score_color}" stroke-width="8" '
            f'stroke-dasharray="{arc_length:.1f} {gap:.1f}" '
            f'stroke-linecap="round" '
            f'transform="rotate(-90 {cx} {cy})" '
            f'style="animation:tc-fill 1.2s ease-out;"/>'
            # Score text
            f'<text x="{cx}" y="{cy - 4}" fill="{score_color}" '
            f'font-size="20" font-weight="700" text-anchor="middle">{score:.0f}%</text>'
            # Trend arrow
            f'<text x="{cx}" y="{cy + 14}" fill="{arrow_color}" '
            f'font-size="16" text-anchor="middle">{arrow}</text>'
            # Label
            f'<text x="{cx}" y="125" fill="{TEXT_MUTED}" '
            f'font-size="9" text-anchor="middle">CORRIGIBILITY</text>'
            f'</svg>'
        )

        # History sparkline
        history_html = ""
        if history:
            pts = []
            w, h = 100, 20
            mn, mx = min(history), max(history)
            rng = mx - mn if mx != mn else 1
            step = w / max(len(history) - 1, 1)
            for i, v in enumerate(history):
                x = i * step
                y = h - ((v - mn) / rng) * (h - 4) - 2
                pts.append(f"{x:.1f},{y:.1f}")
            poly = " ".join(pts)
            history_html = (
                f'<div style="text-align:center;margin:0.3rem 0;">'
                f'<span class="muted" style="font-size:0.7rem;">Last 5 readings</span>'
                f'<svg width="{w}" height="{h}" style="display:block;margin:0 auto;">'
                f'<polyline points="{poly}" fill="none" stroke="{ACCENT}" '
                f'stroke-width="1.5" stroke-linecap="round"/></svg></div>'
            )

        # Factor breakdown
        factor_html = ""
        if factors:
            rows = []
            for f in factors:
                fname = _esc(f.get("name", "?"))
                fscore = float(f.get("score", 0))
                fweight = float(f.get("weight", 0))
                fcolor = GREEN if fscore >= 80 else YELLOW if fscore >= 60 else RED
                rows.append(
                    f'<div style="display:flex;align-items:center;gap:0.4rem;margin-bottom:0.3rem;">'
                    f'<span style="font-size:0.75rem;flex:1;">{fname}</span>'
                    f'<div style="width:60px;height:4px;background:{BG_PRIMARY};'
                    f'border-radius:2px;overflow:hidden;">'
                    f'<div style="width:{fscore:.0f}%;height:100%;background:{fcolor};'
                    f'border-radius:2px;"></div></div>'
                    f'<span style="font-size:0.7rem;color:{fcolor};min-width:28px;'
                    f'text-align:right;">{fscore:.0f}%</span>'
                    f'<span class="muted" style="font-size:0.65rem;">w{fweight:.0%}</span></div>'
                )
            factor_html = (
                f'<div style="margin-top:0.5rem;padding-top:0.4rem;'
                f'border-top:1px solid {BG_SURFACE};">{"".join(rows)}</div>'
            )

        inner = f"<h3>Corrigibility Gauge</h3>{gauge_svg}{history_html}{factor_html}"
        return _wrap_component("corrigibility-gauge", inner)


# ═══════════════════════════════════════════════════════════════════
# 9. SessionGoldStream
# ═══════════════════════════════════════════════════════════════════

class SessionGoldStream:
    """Live gold mining output stream.

    Data keys:
      nuggets: list of {
        timestamp: float,
        pass_name: str,
        finding: str,
        severity: str (critical|architectural|quality|info),
      }
    """

    SEVERITY_META = {
        "critical": ("\u26a0", RED, "CRIT"),
        "architectural": ("\U0001f3d7", ORANGE, "ARCH"),
        "quality": ("\u2728", YELLOW, "QUAL"),
        "info": ("\u2139", TEXT_MUTED, "INFO"),
    }

    @classmethod
    def get_sample_data(cls) -> dict:
        now = time.time()
        return {
            "nuggets": [
                {"timestamp": now - 300, "pass_name": "eval_webhook_quality",
                 "finding": "S2 professor injection latency exceeds 2s threshold",
                 "severity": "critical"},
                {"timestamp": now - 240, "pass_name": "quality_cardiologist",
                 "finding": "Adapter pattern coupling risk in nats_client.py",
                 "severity": "architectural"},
                {"timestamp": now - 180, "pass_name": "eval_webhook_quality",
                 "finding": "S8 Synaptic voice quality score: 0.87",
                 "severity": "quality"},
                {"timestamp": now - 120, "pass_name": "cv_sentinel",
                 "finding": "Vector drift within acceptable bounds (delta 0.03)",
                 "severity": "info"},
                {"timestamp": now - 60, "pass_name": "corrigibility_gate",
                 "finding": "Constitutional compliance verified across all 6 physics",
                 "severity": "quality"},
                {"timestamp": now - 30, "pass_name": "quality_cardiologist",
                 "finding": "Hidden dependency detected: packet_store -> nats_client",
                 "severity": "critical"},
                {"timestamp": now - 10, "pass_name": "session_gold",
                 "finding": "New architectural pattern mined: transport adapter abstraction",
                 "severity": "architectural"},
            ]
        }

    def render(self, data: dict) -> str:
        nuggets = data.get("nuggets", [])
        if not nuggets:
            return _wrap_component("session-gold-stream",
                                   "<h3>Session Gold Stream</h3><p class='muted'>No gold mined yet</p>")

        # Summary counts
        counts = {}
        for n in nuggets:
            sev = n.get("severity", "info")
            counts[sev] = counts.get(sev, 0) + 1

        summary_parts = []
        for sev in ("critical", "architectural", "quality", "info"):
            c = counts.get(sev, 0)
            if c:
                _, color, label = self.SEVERITY_META.get(sev, ("\u2022", TEXT_MUTED, "?"))
                summary_parts.append(
                    f'<span style="color:{color};font-size:0.75rem;">{c} {label}</span>'
                )
        summary = (
            f'<div style="display:flex;gap:0.8rem;margin-bottom:0.5rem;">'
            f'{"".join(summary_parts)}</div>'
        )

        # Nugget feed (newest first)
        sorted_nuggets = sorted(nuggets, key=lambda n: n.get("timestamp", 0), reverse=True)
        items = []
        for i, nugget in enumerate(sorted_nuggets):
            ts = _ts_fmt(nugget.get("timestamp"))
            pass_name = _esc(nugget.get("pass_name", "?"))
            finding = _esc(nugget.get("finding", ""))
            severity = nugget.get("severity", "info")
            icon, color, label = self.SEVERITY_META.get(severity, ("\u2022", TEXT_MUTED, "?"))

            delay = f"animation-delay:{i * 0.08:.2f}s;"

            items.append(
                f'<div style="display:flex;gap:0.4rem;padding:0.35rem 0.5rem;'
                f'background:{BG_PRIMARY};border-radius:4px;margin-bottom:0.25rem;'
                f'border-left:3px solid {color};'
                f'animation:tc-slide-in 0.3s ease-out both;{delay}">'
                f'<span style="font-size:0.8rem;flex-shrink:0;" title="{_esc(label)}">{icon}</span>'
                f'<div style="flex:1;min-width:0;">'
                f'<div style="display:flex;align-items:center;gap:0.3rem;flex-wrap:wrap;">'
                f'<span class="muted" style="font-size:0.7rem;">{ts}</span>'
                f'<code style="font-size:0.65rem;">{pass_name}</code>'
                f'<span style="font-size:0.65rem;color:{color};font-weight:600;">{label}</span></div>'
                f'<div style="font-size:0.8rem;margin-top:0.1rem;'
                f'white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">{finding}</div>'
                f'</div></div>'
            )

        inner = f"<h3>Session Gold Stream</h3>{summary}{''.join(items)}"
        return _wrap_component("session-gold-stream", inner)


# ═══════════════════════════════════════════════════════════════════
# Component Registry + Flask Integration
# ═══════════════════════════════════════════════════════════════════

COMPONENTS: dict[str, type] = {
    "vitals": VitalSignsBar,
    "surgeon-feed": SurgeonFeed,
    "quorum-pulse": QuorumPulse,
    "evidence-timeline": EvidenceTimeline,
    "fleet-constellation": FleetConstellation,
    "memory-heatmap": MemoryHeatMap,
    "probe-status-grid": ProbeStatusGrid,
    "corrigibility-gauge": CorrigibilityGauge,
    "session-gold-stream": SessionGoldStream,
}


def render_all_components(data: dict | None = None) -> str:
    """Render all components into a single HTML page (standalone demo).

    Args:
        data: Optional dict with keys matching component names.
              Missing keys use sample data.
    """
    data = data or {}
    sections = []
    for name, cls in COMPONENTS.items():
        comp = cls()
        comp_data = data.get(name) or cls.get_sample_data()
        sections.append(comp.render(comp_data))

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="15">
<title>Theatrical Dashboard Components</title>
<style>
  body {{
    font-family: -apple-system, system-ui, sans-serif;
    background: {BG_PRIMARY};
    color: {TEXT_PRIMARY};
    margin: 0;
    padding: 1.5rem;
  }}
  h1 {{
    margin: 0 0 1rem;
    font-size: 1.3rem;
    color: {ACCENT};
    letter-spacing: 0.05em;
  }}
  {SHARED_CSS}
</style>
</head>
<body>
<h1>THEATRICAL DASHBOARD</h1>
{"".join(sections)}
</body>
</html>"""


def get_component_css() -> str:
    """Return the shared CSS for embedding components in another page."""
    return SHARED_CSS


def register_components(app) -> None:
    """Register Flask routes for each component.

    Routes:
      GET /dashboard/component/<name>         -- rendered HTML fragment
      GET /dashboard/component/<name>?full=1  -- full standalone page
      GET /dashboard/components               -- all components (standalone page)

    Args:
        app: Flask application instance.
    """
    from flask import request as flask_request

    @app.route("/dashboard/component/<name>")
    def _serve_component(name: str):
        cls = COMPONENTS.get(name)
        if not cls:
            return f"Unknown component: {_esc(name)}", 404
        comp = cls()
        comp_data = cls.get_sample_data()
        html_out = comp.render(comp_data)
        if flask_request.args.get("full"):
            html_out = (
                f"<!DOCTYPE html><html><head><meta charset='utf-8'>"
                f"<style>body{{background:{BG_PRIMARY};color:{TEXT_PRIMARY};"
                f"font-family:sans-serif;padding:1rem;}}{SHARED_CSS}</style>"
                f"</head><body>{html_out}</body></html>"
            )
        return html_out

    @app.route("/dashboard/components")
    def _serve_all_components():
        return render_all_components()
