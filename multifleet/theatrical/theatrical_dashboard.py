"""Theatrical Dashboard — composes all 9 components into a unified Superset-inspired page.

Renders a complete self-contained HTML page with inline CSS/JS, dark theme,
and auto-refresh. Pulls data from FleetDashboardData and EvidenceLedger.

Usage:
    from multifleet.theatrical.theatrical_dashboard import TheatricalDashboard
    dash = TheatricalDashboard()
    html = dash.render()
"""

import html
import json
import logging
import time
from datetime import datetime, timezone
from typing import Optional

from .vital_signs import VitalSignsBar
from .surgeon_feed import SurgeonFeed
from .fleet_constellation import FleetConstellation
from .evidence_timeline import EvidenceTimeline
from .quorum_pulse import QuorumPulse
from .memory_heat_map import MemoryHeatMap
from .probe_status_grid import ProbeStatusGrid
from .corrigibility_gauge import CorrigibilityGauge
from .session_gold_stream import SessionGoldStream

logger = logging.getLogger("fleet-nerve.theatrical")


class TheatricalDashboard:
    """Composes all 9 theatrical components into a single HTML dashboard.

    Data sources are injected or fetched from FleetDashboardData.
    Each component degrades gracefully when its data source is unavailable.
    """

    def __init__(self, dashboard_data=None, evidence_ledger=None):
        """Initialize with optional data sources.

        Args:
            dashboard_data: FleetDashboardData instance (or None for standalone).
            evidence_ledger: EvidenceLedger instance (or None).
        """
        self._data = dashboard_data
        self._ledger = evidence_ledger

        # Initialize all 9 components
        self.vital_signs = VitalSignsBar()
        self.surgeon_feed = SurgeonFeed()
        self.fleet_constellation = FleetConstellation()
        self.evidence_timeline = EvidenceTimeline()
        self.quorum_pulse = QuorumPulse()
        self.memory_heat_map = MemoryHeatMap()
        self.probe_status_grid = ProbeStatusGrid()
        self.corrigibility_gauge = CorrigibilityGauge()
        self.session_gold_stream = SessionGoldStream()

    def gather_data(self) -> dict:
        """Gather data from all sources into a unified dict.

        Each key corresponds to a component's data needs. Returns safe
        defaults when sources are unavailable.
        """
        result = {
            "timestamp": time.time(),
            "fleet": None,
            "evidence": None,
            "surgeon": None,
            "memory": None,
            "probes": None,
            "corrigibility": None,
            "gold": None,
        }

        # Fleet data (nodes, messages, gains gate, etc.)
        if self._data:
            try:
                snapshot = self._data.snapshot()
                # Ensure daemon key exists for VitalSignsBar compatibility
                if "daemon" not in snapshot:
                    snapshot["daemon"] = {
                        "uptime_s": 0,
                        "version": "unknown",
                    }
                result["fleet"] = snapshot
            except Exception as exc:
                logger.debug("Fleet data unavailable: %s", exc)

        # TODO: Wire remaining data sources when available:
        # result["surgeon"]       — from 3-Surgeons probe/status endpoint
        # result["memory"]        — from ContextDNA memory heat map query
        # result["probes"]        — from probe engine last-fire results
        # result["gold"]          — from session_historian gold mining output

        # Synaptic 8th Intelligence data
        try:
            result["synaptic"] = self._gather_synaptic_data()
        except Exception as exc:
            logger.debug("Synaptic data unavailable: %s", exc)

        # Corrigibility data — from CorrigibilityLedger + gate event history
        try:
            result["corrigibility"] = self._gather_corrigibility_data()
        except Exception as exc:
            logger.debug("Corrigibility data unavailable: %s", exc)

        # Evidence ledger
        if self._ledger:
            try:
                entries = self._ledger.latest(count=12)
                stats = self._ledger.stats
                chain = self._ledger.verify_chain(limit=20)
                result["evidence"] = {
                    "entries": entries,
                    "total": stats.get("total", 0),
                    "chain_valid": chain.get("valid"),
                    "chain_checked": chain.get("checked", 0),
                }
            except Exception as exc:
                logger.debug("Evidence ledger unavailable: %s", exc)

        return result

    def render(self, data: Optional[dict] = None) -> str:
        """Render complete HTML dashboard page.

        Args:
            data: Pre-gathered data dict. If None, calls gather_data().

        Returns:
            Complete self-contained HTML string.
        """
        if data is None:
            data = self.gather_data()

        ts = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
        fleet = data.get("fleet")

        # Render each component with its data slice
        vital_html = self.vital_signs.render(fleet)
        surgeon_html = self.surgeon_feed.render(data.get("surgeon") or self._extract_surgeon_data(fleet))
        constellation_html = self.fleet_constellation.render(fleet)
        evidence_html = self.evidence_timeline.render(data.get("evidence"))
        quorum_html = self.quorum_pulse.render(fleet)
        memory_html = self.memory_heat_map.render(data.get("memory"))
        probe_html = self.probe_status_grid.render(data.get("probes"))
        corrigibility_html = self.corrigibility_gauge.render(data.get("corrigibility"))
        gold_html = self.session_gold_stream.render(data.get("gold"))
        synaptic_html = self._render_synaptic(data.get("synaptic"))

        return f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="10">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Context DNA — Theatrical Dashboard</title>
<style>
{self._css()}
</style>
</head>
<body>
<header class="tc-header">
  <div class="tc-header-left">
    <h1 class="tc-title">Context DNA</h1>
    <span class="tc-subtitle">Theatrical Dashboard</span>
  </div>
  <div class="tc-header-right">
    <span class="tc-timestamp">{ts}</span>
    <span class="tc-refresh-dot" title="Auto-refresh 10s"></span>
  </div>
</header>

<div class="tc-row tc-row-vital">
  {vital_html}
</div>

<div class="tc-grid">
  <div class="tc-col tc-col-left">
    {surgeon_html}
    {constellation_html}
    {quorum_html}
  </div>
  <div class="tc-col tc-col-center">
    {evidence_html}
    {probe_html}
  </div>
  <div class="tc-col tc-col-right">
    {corrigibility_html}
    {memory_html}
    {gold_html}
    {synaptic_html}
  </div>
</div>

<footer class="tc-footer">
  <span class="tc-muted">Context DNA Theatrical Dashboard — Superset-inspired fleet intelligence visualization</span>
</footer>
{self._js()}
</body>
</html>'''

    def render_component(self, component_name: str, data: Optional[dict] = None) -> str:
        """Render a single component by name. For partial updates."""
        components = {
            "vital_signs": self.vital_signs,
            "surgeon_feed": self.surgeon_feed,
            "fleet_constellation": self.fleet_constellation,
            "evidence_timeline": self.evidence_timeline,
            "quorum_pulse": self.quorum_pulse,
            "memory_heat_map": self.memory_heat_map,
            "probe_status_grid": self.probe_status_grid,
            "corrigibility_gauge": self.corrigibility_gauge,
            "session_gold_stream": self.session_gold_stream,
        }
        component = components.get(component_name)
        if not component:
            return f'<div class="tc-muted">Unknown component: {html.escape(component_name)}</div>'
        return component.render(data)

    def _gather_corrigibility_data(self) -> Optional[dict]:
        """Gather corrigibility data from ledger + gate event history."""
        from ..corrigibility_ledger import CorrigibilityLedger
        from ..event_bus import IDEEventBus

        ledger = CorrigibilityLedger()
        stats = ledger.get_impact_stats()
        disagreements = ledger.get_recent_disagreements(limit=5)

        # Compute corrigibility score from decision stats
        total = stats.get("total_decisions", 0)
        changed = stats.get("changed_by_surgeon", 0)
        # Score: higher when system actually changes behavior on surgeon input
        # Base 0.5 + up to 0.5 based on change rate (changing is good = corrigible)
        if total > 0:
            change_rate = changed / total
            score = min(1.0, 0.5 + change_rate * 0.5)
        else:
            score = 0.5  # neutral when no data

        # Gate event history from EventBus ring buffer
        gate_history = []
        try:
            bus = IDEEventBus.get()
            gate_events = bus.history("gate", limit=20, pattern="gate.result")
            for evt in gate_events:
                payload = evt.get("payload", {})
                gate_history.append({
                    "gate": payload.get("gate", "?"),
                    "passed": payload.get("passed", True),
                    "summary": payload.get("summary", ""),
                    "failures": payload.get("critical_failures", []),
                    "duration_ms": payload.get("duration_ms", 0),
                    "timestamp": payload.get("timestamp", 0),
                })
        except Exception:
            pass  # EventBus may not be initialized

        # Factors breakdown
        factors = {
            "gate_pass_rate": 1.0,
            "surgeon_influence": stats.get("change_rate", 0.0),
            "disagreement_resolution": 1.0,
        }
        if gate_history:
            passed_count = sum(1 for g in gate_history if g["passed"])
            factors["gate_pass_rate"] = passed_count / len(gate_history)
        if disagreements:
            resolved = sum(1 for d in disagreements
                          if d.get("resolution", "") != "unresolved")
            factors["disagreement_resolution"] = (
                resolved / len(disagreements) if disagreements else 1.0
            )

        # Trend from gate history (timestamp + pass/fail as score proxy)
        trend = []
        for g in reversed(gate_history[:20]):
            trend.append({
                "timestamp": g.get("timestamp", 0),
                "score": 1.0 if g["passed"] else 0.3,
            })

        return {
            "score": score,
            "trend": trend,
            "factors": factors,
            "last_check": time.time(),
            "stats": stats,
            "recent_disagreements": disagreements,
            "gate_history": gate_history,
        }

    def _gather_synaptic_data(self) -> Optional[dict]:
        """Gather Synaptic personality, patterns, and beliefs for dashboard."""
        import sys
        from pathlib import Path
        # Add repo root so memory package is importable
        repo_root = str(Path(__file__).resolve().parents[3])
        if repo_root not in sys.path:
            sys.path.insert(0, repo_root)

        data: dict = {"personality": None, "patterns": [], "belief_count": 0}

        # Personality traits
        try:
            from memory.synaptic_personality import get_personality
            p = get_personality()
            traits = p.get_traits() if hasattr(p, "get_traits") else {}
            data["personality"] = {
                "traits": traits if isinstance(traits, dict) else {},
                "wisdom_count": len(p.get_wisdom(limit=100)) if hasattr(p, "get_wisdom") else 0,
            }
        except Exception as exc:
            logger.debug("Synaptic personality unavailable: %s", exc)

        # Recent patterns
        try:
            from memory.synaptic_pattern_engine import get_pattern_engine
            engine = get_pattern_engine()
            recent = engine.get_recent(limit=5)
            data["patterns"] = [
                {
                    "title": p.title,
                    "confidence": getattr(p, "confidence", 0),
                    "type": getattr(p, "pattern_type", "unknown"),
                }
                for p in recent
            ]
        except Exception as exc:
            logger.debug("Synaptic patterns unavailable: %s", exc)

        # Active beliefs from evolution engine
        try:
            from memory.synaptic_evolution import SynapticEvolution
            evo = SynapticEvolution()
            proposals = evo.get_pending_proposals()
            data["belief_count"] = len(proposals) if proposals else 0
        except Exception as exc:
            logger.debug("Synaptic evolution unavailable: %s", exc)

        return data

    def _extract_surgeon_data(self, fleet: Optional[dict]) -> Optional[dict]:
        """Extract surgeon-relevant data from fleet snapshot."""
        if not fleet:
            return None
        # Build minimal surgeon data from available fleet info
        return {
            "surgeons": {},
            "cross_exams": [],
            "rebuttals": [],
        }

    def _render_synaptic(self, data: Optional[dict]) -> str:
        """Render Synaptic 8th Intelligence section."""
        if not data:
            return '''<div class="tc-card tc-unavailable">
  <div class="tc-card-header">Synaptic — 8th Intelligence</div>
  <div class="tc-card-body"><span class="tc-muted">Synaptic unavailable</span></div>
</div>'''

        personality = data.get("personality") or {}
        traits = personality.get("traits", {})
        wisdom_count = personality.get("wisdom_count", 0)
        patterns = data.get("patterns", [])
        belief_count = data.get("belief_count", 0)

        # Personality traits row
        trait_chips = ""
        if traits:
            for name, val in list(traits.items())[:6]:
                score = val if isinstance(val, (int, float)) else 0
                color = "#a78bfa" if score >= 0.7 else "#94a3b8"
                trait_chips += (
                    f'<span class="tc-syn-trait" style="color:{color}">'
                    f'{html.escape(str(name))}'
                    f'</span> '
                )
        else:
            trait_chips = '<span class="tc-muted">no traits loaded</span>'

        # Patterns list
        pattern_rows = ""
        if patterns:
            for p in patterns:
                conf = p.get("confidence", 0)
                badge_cls = "tc-badge-green" if conf >= 0.7 else "tc-badge-yellow" if conf >= 0.4 else "tc-badge-gray"
                pattern_rows += (
                    f'<div class="tc-syn-pattern">'
                    f'<span class="tc-badge {badge_cls}">{conf:.0%}</span> '
                    f'<span class="tc-syn-pattern-title">{html.escape(str(p.get("title", "?")))}</span>'
                    f'</div>'
                )
        else:
            pattern_rows = '<div class="tc-muted">no recent patterns</div>'

        return f'''<div class="tc-card">
  <div class="tc-card-header">Synaptic — 8th Intelligence
    <span class="tc-card-count">{wisdom_count} wisdom · {belief_count} beliefs</span>
  </div>
  <div class="tc-card-body">
    <div class="tc-syn-section">
      <div class="tc-feed-label">Personality</div>
      <div class="tc-syn-traits">{trait_chips}</div>
    </div>
    <div class="tc-syn-section">
      <div class="tc-feed-label">Recent Patterns</div>
      {pattern_rows}
    </div>
  </div>
</div>'''

    def _css(self) -> str:
        """Return all CSS for the theatrical dashboard."""
        return '''
/* === Base Reset & Theme === */
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
  background: #0f172a;
  color: #e2e8f0;
  padding: 0;
  min-height: 100vh;
}

/* === Header === */
.tc-header {
  display: flex; align-items: center; justify-content: space-between;
  padding: 0.75rem 1.5rem;
  background: #1e293b;
  border-bottom: 1px solid #334155;
}
.tc-header-left { display: flex; align-items: baseline; gap: 0.75rem; }
.tc-title { font-size: 1.3rem; font-weight: 700; color: #e2e8f0; }
.tc-subtitle { font-size: 0.85rem; color: #64748b; }
.tc-header-right { display: flex; align-items: center; gap: 0.75rem; }
.tc-timestamp { font-family: monospace; color: #06b6d4; font-size: 0.85rem; }
.tc-refresh-dot {
  width: 8px; height: 8px; border-radius: 50%; background: #22c55e;
  animation: tc-pulse 2s ease-in-out infinite;
}
@keyframes tc-pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.3; } }

/* === Grid Layout === */
.tc-row { padding: 0.5rem 1rem; }
.tc-row-vital { padding-bottom: 0; }
.tc-grid {
  display: grid;
  grid-template-columns: 1fr 1fr 1fr;
  gap: 0.75rem;
  padding: 0.75rem 1rem;
  min-height: calc(100vh - 120px);
}
.tc-col { display: flex; flex-direction: column; gap: 0.75rem; }

/* === Card === */
.tc-card {
  background: #1e293b;
  border: 1px solid #334155;
  border-radius: 8px;
  overflow: hidden;
}
.tc-card-header {
  display: flex; align-items: center; justify-content: space-between;
  padding: 0.5rem 0.75rem;
  background: #0f172a;
  border-bottom: 1px solid #334155;
  font-weight: 600; font-size: 0.85rem; color: #94a3b8;
  text-transform: uppercase; letter-spacing: 0.05em;
}
.tc-card-body { padding: 0.75rem; }
.tc-card-count { font-weight: 400; color: #64748b; font-size: 0.8rem; text-transform: none; }
.tc-unavailable .tc-card-body { text-align: center; padding: 2rem 1rem; }

/* === Shared === */
.tc-muted { color: #64748b; font-size: 0.8rem; }
.tc-ts { background: #0f172a; padding: 0.1rem 0.3rem; border-radius: 3px; font-size: 0.75rem; color: #94a3b8; }
.tc-hash { background: #0f172a; padding: 0.1rem 0.3rem; border-radius: 3px; font-size: 0.7rem; color: #64748b; font-family: monospace; }
.tc-badge {
  display: inline-block; padding: 0.15rem 0.5rem; border-radius: 10px;
  font-size: 0.7rem; font-weight: 600; text-transform: uppercase; letter-spacing: 0.03em;
}
.tc-badge-green { background: rgba(34,197,94,0.15); color: #22c55e; }
.tc-badge-yellow { background: rgba(234,179,8,0.15); color: #eab308; }
.tc-badge-red { background: rgba(239,68,68,0.15); color: #ef4444; }
.tc-badge-blue { background: rgba(59,130,246,0.15); color: #3b82f6; }
.tc-badge-gray { background: rgba(100,116,139,0.15); color: #64748b; }

/* === Component 1: Vital Signs === */
.tc-vital-bar {
  display: flex; align-items: center; gap: 0; padding: 0.5rem 0.75rem;
  flex-wrap: wrap;
}
.tc-vital-item { display: flex; align-items: center; gap: 0.4rem; padding: 0.3rem 0.6rem; }
.tc-vital-icon { font-size: 1.2em; }
.tc-vital-label { font-size: 0.75rem; color: #94a3b8; }
.tc-vital-value { font-size: 0.85rem; font-weight: 600; display: flex; align-items: center; gap: 0.3rem; }
.tc-vital-divider { width: 1px; height: 24px; background: #334155; margin: 0 0.3rem; }

/* === Component 2: Surgeon Feed === */
.tc-surgeon-status { display: flex; gap: 0.5rem; padding: 0.5rem 0.75rem; flex-wrap: wrap; }
.tc-surgeon-badge {
  display: flex; align-items: center; gap: 0.3rem;
  padding: 0.25rem 0.5rem; border: 1px solid; border-radius: 6px; font-size: 0.8rem;
}
.tc-feed-section { padding: 0.3rem 0.75rem 0.5rem; }
.tc-feed-label { font-size: 0.75rem; color: #64748b; margin-bottom: 0.3rem; text-transform: uppercase; }
.tc-feed-entry { padding: 0.2rem 0; font-size: 0.8rem; border-bottom: 1px solid #1e293b; display: flex; gap: 0.4rem; align-items: center; flex-wrap: wrap; }
.tc-feed-topic { color: #e2e8f0; font-weight: 500; }

/* === Component 3: Fleet Constellation === */
.tc-constellation-wrap { padding: 0.5rem; display: flex; justify-content: center; }

/* === Component 4: Evidence Timeline === */
.tc-chain-status { padding: 0.4rem 0.75rem; display: flex; align-items: center; gap: 0.5rem; }
.tc-timeline { padding: 0 0.75rem 0.5rem; max-height: 400px; overflow-y: auto; }
.tc-timeline-entry { display: flex; gap: 0.5rem; padding: 0.4rem 0; border-bottom: 1px solid #1e293b; position: relative; }
.tc-timeline-dot { width: 8px; height: 8px; border-radius: 50%; margin-top: 6px; flex-shrink: 0; }
.tc-timeline-content { flex: 1; min-width: 0; }
.tc-timeline-header { display: flex; align-items: center; gap: 0.4rem; font-size: 0.8rem; flex-wrap: wrap; }
.tc-timeline-body { font-size: 0.8rem; color: #e2e8f0; margin: 0.1rem 0; }
.tc-timeline-meta { display: flex; gap: 0.5rem; font-size: 0.75rem; }

/* === Component 5: Quorum Pulse === */
.tc-pulse-wave { padding: 0.3rem 0.75rem; }
.tc-signal-grid { padding: 0.3rem 0.75rem 0.5rem; }
.tc-signal-row { display: flex; align-items: center; justify-content: space-between; padding: 0.25rem 0; }
.tc-signal-label { display: flex; align-items: center; gap: 0.3rem; font-size: 0.8rem; min-width: 140px; }
.tc-signal-bar-wrap { display: flex; align-items: center; gap: 0.4rem; }
.tc-signal-pct { font-size: 0.8rem; font-weight: 600; min-width: 35px; }

/* === Component 6: Memory Heat Map === */
.tc-heat-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 4px; padding: 0.5rem 0.75rem; }
.tc-heat-cell {
  padding: 0.4rem; border-radius: 4px; text-align: center; cursor: default;
  transition: transform 0.15s; min-height: 50px;
  display: flex; flex-direction: column; justify-content: center;
}
.tc-heat-cell:hover { transform: scale(1.05); }
.tc-heat-label { font-size: 0.65rem; color: #94a3b8; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.tc-heat-count { font-size: 0.9rem; font-weight: 600; color: #e2e8f0; }
.tc-heat-quality { font-size: 0.65rem; }
.tc-heat-legend { display: flex; align-items: center; gap: 2px; padding: 0.3rem 0.75rem; }
.tc-legend-item { width: 16px; height: 8px; border-radius: 2px; }

/* === Component 7: Probe Status Grid === */
.tc-probe-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 4px; padding: 0.5rem 0.75rem; }
.tc-probe-card { padding: 0.4rem; background: #0f172a; border-radius: 4px; }
.tc-probe-header { display: flex; align-items: center; gap: 0.3rem; font-size: 0.8rem; }
.tc-probe-name { font-weight: 500; color: #e2e8f0; flex: 1; }
.tc-probe-cat { font-size: 0.65rem; text-transform: uppercase; }
.tc-probe-body { display: flex; align-items: center; gap: 0.4rem; margin-top: 0.2rem; font-size: 0.75rem; }

/* === Component 8: Corrigibility Gauge === */
.tc-gauge-wrap { display: flex; justify-content: center; padding: 0.5rem; }
.tc-trend-wrap { padding: 0 0.75rem; text-align: center; }
.tc-factors { padding: 0.3rem 0.75rem 0.5rem; }
.tc-factor-row { display: flex; align-items: center; gap: 0.4rem; padding: 0.15rem 0; font-size: 0.8rem; }
.tc-factor-row .tc-muted { min-width: 100px; }

/* === Component 9: Session Gold Stream === */
.tc-gold-stats { display: flex; gap: 1rem; padding: 0.4rem 0.75rem; font-size: 0.8rem; color: #94a3b8; }
.tc-gold-alert { padding: 0.3rem 0.75rem; }
.tc-gold-active { padding: 0.3rem 0.75rem; display: flex; align-items: center; gap: 0.4rem; font-size: 0.8rem; }
.tc-gold-feed { padding: 0.3rem 0.75rem 0.5rem; max-height: 300px; overflow-y: auto; }
.tc-gold-nugget { padding: 0.3rem 0; border-bottom: 1px solid #1e293b; }
.tc-gold-header { display: flex; align-items: center; gap: 0.4rem; font-size: 0.75rem; }
.tc-gold-pass { font-weight: 600; }
.tc-gold-severity { font-size: 0.7rem; text-transform: uppercase; }
.tc-gold-content { font-size: 0.8rem; color: #e2e8f0; margin-top: 0.1rem; word-break: break-word; }

/* === Component 10: Synaptic === */
.tc-syn-section { margin-bottom: 0.5rem; }
.tc-syn-traits { display: flex; flex-wrap: wrap; gap: 0.3rem; }
.tc-syn-trait {
  font-size: 0.75rem; font-weight: 500;
  padding: 0.15rem 0.4rem; background: rgba(167,139,250,0.1);
  border-radius: 4px;
}
.tc-syn-pattern { display: flex; align-items: center; gap: 0.4rem; padding: 0.2rem 0; font-size: 0.8rem; }
.tc-syn-pattern-title { color: #e2e8f0; }

/* === Footer === */
.tc-footer {
  padding: 0.5rem 1.5rem;
  border-top: 1px solid #334155;
  text-align: center;
}

/* === Scrollbar === */
::-webkit-scrollbar { width: 6px; }
::-webkit-scrollbar-track { background: #0f172a; }
::-webkit-scrollbar-thumb { background: #334155; border-radius: 3px; }
::-webkit-scrollbar-thumb:hover { background: #475569; }

/* === Responsive === */
@media (max-width: 1200px) {
  .tc-grid { grid-template-columns: 1fr 1fr; }
}
@media (max-width: 768px) {
  .tc-grid { grid-template-columns: 1fr; }
  .tc-heat-grid { grid-template-columns: repeat(3, 1fr); }
  .tc-probe-grid { grid-template-columns: 1fr; }
}
'''

    def _js(self) -> str:
        """Return minimal JS for auto-refresh indicator."""
        return '''
<script>
(function() {
  var dot = document.querySelector('.tc-refresh-dot');
  if (!dot) return;
  var remaining = 10;
  setInterval(function() {
    remaining--;
    if (remaining <= 0) {
      dot.style.background = '#eab308';
      dot.title = 'Refreshing...';
    }
  }, 1000);
})();
</script>'''
