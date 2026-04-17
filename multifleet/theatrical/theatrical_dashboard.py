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

        # Corrigibility data — gate events + ledger stats
        result["corrigibility"] = self._gather_corrigibility_data()

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
        """Pull real gate event data from EventBus + CorrigibilityLedger.

        Returns dict with: score, trend, factors, last_check,
        recent_gate_events, override_count, block_count.
        """
        now = time.time()
        gate_events: list = []
        pass_count = 0
        fail_count = 0
        block_count = 0
        override_count = 0

        # 1. Gate events from EventBus NS_GATE ring
        try:
            from multifleet.event_bus import IDEEventBus, NS_GATE
            bus = IDEEventBus.get()
            raw_events = bus.history(NS_GATE, limit=50, pattern="gate.*")
            for ev in raw_events:
                payload = ev.get("payload", {})
                passed = payload.get("passed", True)
                gate_name = payload.get("gate", "unknown")
                ts = payload.get("timestamp") or ev.get("timestamp", 0)
                failures = payload.get("failures", [])

                if passed:
                    pass_count += 1
                else:
                    fail_count += 1
                    block_count += 1

                gate_events.append({
                    "gate": gate_name,
                    "passed": passed,
                    "failures": failures,
                    "timestamp": ts,
                })
        except Exception as exc:
            logger.debug("Gate event history unavailable: %s", exc)

        # 2. Corrigibility ledger — decision impact stats
        ledger_stats: dict = {}
        try:
            from multifleet.corrigibility_ledger import CorrigibilityLedger
            cl = CorrigibilityLedger()
            ledger_stats = cl.get_impact_stats()
            override_count = ledger_stats.get("changed_by_surgeon", 0)
        except Exception as exc:
            logger.debug("Corrigibility ledger unavailable: %s", exc)

        # 3. Compute score from real data
        total_gate_events = pass_count + fail_count
        if total_gate_events > 0:
            gate_pass_rate = pass_count / total_gate_events
        else:
            gate_pass_rate = 1.0  # no events = assume healthy

        # Corrigibility score: weighted blend of gate health + change rate
        # High change rate = system IS corrigible (changes when surgeons say so)
        change_rate = ledger_stats.get("change_rate", 0.0)
        # Score: 60% gate pass rate + 40% corrigibility responsiveness
        # A system that blocks when it should AND changes when told is maximally corrigible
        if ledger_stats.get("total_decisions", 0) > 0:
            score = 0.6 * gate_pass_rate + 0.4 * min(change_rate * 5, 1.0)
        else:
            score = gate_pass_rate

        # 4. Build trend from gate events (newest first, reverse for chronological)
        trend = []
        running_pass = 0
        running_total = 0
        for ev in reversed(gate_events[:20]):
            running_total += 1
            if ev.get("passed"):
                running_pass += 1
            trend.append({
                "timestamp": ev.get("timestamp", 0),
                "score": running_pass / running_total if running_total else 1.0,
            })

        # 5. Factors breakdown
        factors = {
            "gate_pass_rate": round(gate_pass_rate, 3),
        }
        if ledger_stats.get("total_decisions", 0) > 0:
            factors["change_rate"] = round(change_rate, 3)
        if ledger_stats.get("total_disagreements", 0) > 0:
            factors["disagreements"] = min(
                ledger_stats["total_disagreements"] / 10.0, 1.0
            )

        return {
            "score": round(score, 3),
            "trend": trend,
            "factors": factors,
            "last_check": now,
            "recent_gate_events": gate_events[:10],
            "override_count": override_count,
            "block_count": block_count,
        }

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
.tc-corr-stats { display: flex; gap: 0.5rem; padding: 0.3rem 0.75rem; }
.tc-gate-feed { padding: 0.3rem 0.75rem 0.5rem; max-height: 200px; overflow-y: auto; }
.tc-gate-event { display: flex; align-items: center; gap: 0.3rem; padding: 0.15rem 0; font-size: 0.8rem; border-bottom: 1px solid #1e293b; }

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
