"""IDE Panel Server — structured JSON endpoints for Context DNA IDE panels.

Bridge between current Flask/HTTP dashboard and future IDE panels (Electron,
VS Code webview, or any browser). Each panel method returns a standardized
envelope: {panel_id, title, updated_at, data, summary}.

Panels:
  - fleet_status_panel()     — nodes, channels, sessions, health
  - evidence_timeline_panel() — recent evidence ledger entries with hash chain
  - synthesis_decisions_panel() — ChiefSynthesis decisions with confidence
  - probe_findings_panel()    — latest probe scan results
  - wisdom_feedback_panel()   — wisdom contradictions/supporting entries

Usage:
    from multifleet.ide_panels import IDEPanelServer
    server = IDEPanelServer()
    data = server.fleet_status_panel()

Or via web_server integration:
    GET /api/panels/fleet-status
    GET /api/panels/evidence-timeline?hours=24
    GET /api/panels/synthesis-decisions?limit=20
    GET /api/panels/probe-findings
    GET /api/panels/wisdom-feedback
"""

import json
import logging
import os
import time
import urllib.request
from pathlib import Path
from typing import Optional

logger = logging.getLogger("fleet-nerve.ide-panels")

DAEMON_PORT = int(os.environ.get("FLEET_DAEMON_PORT", "8855"))


def _now_iso() -> str:
    """Current UTC time in ISO format."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _panel_envelope(panel_id: str, title: str, data: list,
                    summary: str) -> dict:
    """Standard panel response envelope."""
    return {
        "panel_id": panel_id,
        "title": title,
        "updated_at": _now_iso(),
        "data": data,
        "summary": summary,
    }


def _fetch_daemon(path: str, port: int = DAEMON_PORT,
                  timeout: float = 3.0) -> Optional[dict]:
    """Fetch JSON from fleet daemon. Returns None on failure."""
    try:
        url = f"http://127.0.0.1:{port}{path}"
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:
        return None


class IDEPanelServer:
    """Serves structured JSON for IDE panel consumption.

    Lazy-initializes heavy dependencies (evidence ledger, probe engine,
    synthesis, wisdom consumer) only when the corresponding panel is
    requested.
    """

    def __init__(self, daemon_port: int = DAEMON_PORT,
                 evidence_ledger=None, probe_engine=None,
                 synthesis=None, wisdom_consumer=None,
                 professor_bridge=None, synaptic_bridge=None):
        self._daemon_port = daemon_port
        self._evidence_ledger = evidence_ledger
        self._probe_engine = probe_engine
        self._synthesis = synthesis
        self._wisdom_consumer = wisdom_consumer
        self._professor_bridge = professor_bridge
        self._synaptic_bridge = synaptic_bridge

    # ── Lazy accessors ───────────────────────────────────────────────

    def _get_evidence_ledger(self):
        if self._evidence_ledger is not None:
            return self._evidence_ledger
        try:
            from multifleet.evidence_ledger import EvidenceLedger
            self._evidence_ledger = EvidenceLedger()
        except Exception as e:
            logger.debug("EvidenceLedger unavailable: %s", e)
        return self._evidence_ledger

    def _get_probe_engine(self):
        if self._probe_engine is not None:
            return self._probe_engine
        try:
            from multifleet.probe_engine import ProbeEngine
            self._probe_engine = ProbeEngine()
        except Exception as e:
            logger.debug("ProbeEngine unavailable: %s", e)
        return self._probe_engine

    def _get_synthesis(self):
        if self._synthesis is not None:
            return self._synthesis
        try:
            from multifleet.synthesis import ChiefSynthesis
            self._synthesis = ChiefSynthesis()
        except Exception as e:
            logger.debug("ChiefSynthesis unavailable: %s", e)
        return self._synthesis

    def _get_wisdom_consumer(self):
        if self._wisdom_consumer is not None:
            return self._wisdom_consumer
        try:
            from multifleet.wisdom_consumer import WisdomConsumer
            ledger = self._get_evidence_ledger()
            self._wisdom_consumer = WisdomConsumer(evidence_ledger=ledger)
        except Exception as e:
            logger.debug("WisdomConsumer unavailable: %s", e)
        return self._wisdom_consumer

    def _get_professor_bridge(self):
        if self._professor_bridge is not None:
            return self._professor_bridge
        try:
            from multifleet.professor_bridge import get_professor_bridge
            self._professor_bridge = get_professor_bridge()
        except Exception as e:
            logger.debug("ProfessorBridge unavailable: %s", e)
        return self._professor_bridge

    def _get_synaptic_bridge(self):
        if self._synaptic_bridge is not None:
            return self._synaptic_bridge
        try:
            from multifleet.synaptic_bridge import SynapticBridge
            self._synaptic_bridge = SynapticBridge()
        except Exception as e:
            logger.debug("SynapticBridge unavailable: %s", e)
        return self._synaptic_bridge

    # ── Panel: Fleet Status ──────────────────────────────────────────

    def fleet_status_panel(self) -> dict:
        """Nodes, channels, sessions, health from daemon /health.

        Data items: one dict per node with id, status, nats, branch,
        sessions, last_seen fields.
        """
        health = _fetch_daemon("/health", port=self._daemon_port)

        if not health:
            return _panel_envelope(
                "fleet-status", "Fleet Status", [],
                "Daemon unreachable",
            )

        nodes = []
        node_id = health.get("node_id", "unknown")
        nats_info = health.get("nats", {})
        nats_ok = (nats_info.get("connected", False)
                   if isinstance(nats_info, dict)
                   else health.get("nats_connected", False))

        # Self node
        nodes.append({
            "id": node_id,
            "status": health.get("status", "unknown"),
            "nats_connected": nats_ok,
            "branch": health.get("branch", "unknown"),
            "sessions": health.get("sessions", 0),
            "role": "self",
        })

        # Peer nodes
        peers = health.get("peers", {})
        if isinstance(peers, dict):
            for pid, info in peers.items():
                if isinstance(info, dict):
                    nodes.append({
                        "id": pid,
                        "status": info.get("status", "peer"),
                        "nats_connected": info.get("nats_connected", None),
                        "branch": info.get("branch", None),
                        "sessions": info.get("sessions", 0),
                        "last_seen": info.get("last_seen",
                                              info.get("lastSeen", None)),
                        "role": "peer",
                    })
                else:
                    nodes.append({"id": pid, "status": "peer", "role": "peer"})
        elif isinstance(peers, list):
            for pid in peers:
                nodes.append({"id": pid, "status": "peer", "role": "peer"})

        online = sum(1 for n in nodes
                     if n.get("status") in ("ok", "online", "reachable", "peer"))
        total = len(nodes)

        return _panel_envelope(
            "fleet-status", "Fleet Status", nodes,
            f"{online}/{total} nodes reachable",
        )

    # ── Panel: Evidence Timeline ─────────────────────────────────────

    def evidence_timeline_panel(self, hours: float = 24) -> dict:
        """Recent evidence ledger entries with hash chain verification.

        Args:
            hours: How far back to look (default 24h).

        Data items: evidence entries with seq, entry_id, timestamp,
        event_type, node_id, subject, entry_hash (prefix), verified.
        """
        ledger = self._get_evidence_ledger()
        if ledger is None:
            return _panel_envelope(
                "evidence-timeline", "Evidence Timeline", [],
                "Evidence ledger unavailable",
            )

        since = time.time() - (hours * 3600)
        entries = ledger.query(since=since, limit=100)

        data = []
        for entry in entries:
            payload = entry.get("payload", {})
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except (json.JSONDecodeError, TypeError):
                    payload = {}

            data.append({
                "seq": entry.get("seq"),
                "entry_id": entry.get("entry_id", ""),
                "timestamp": entry.get("timestamp"),
                "event_type": entry.get("event_type", ""),
                "node_id": entry.get("node_id", ""),
                "subject": entry.get("subject", ""),
                "entry_hash": entry.get("entry_hash", "")[:16],
                "prev_hash": entry.get("prev_hash", "")[:16],
                "verified": bool(entry.get("verified", True)),
                "payload_summary": _payload_summary(payload),
            })

        # Quick chain check on recent entries
        chain_ok = True
        try:
            check = ledger.verify_chain(limit=min(len(entries), 50))
            chain_ok = check.get("valid", False)
        except Exception:
            pass

        chain_str = "intact" if chain_ok else "BROKEN"
        return _panel_envelope(
            "evidence-timeline", "Evidence Timeline", data,
            f"{len(data)} entries in last {hours}h, chain {chain_str}",
        )

    # ── Panel: Synthesis Decisions ───────────────────────────────────

    def synthesis_decisions_panel(self, limit: int = 20) -> dict:
        """Recent ChiefSynthesis decisions with confidence, winner, cherry-picks.

        Reads from the decisions.jsonl ledger file.
        """
        synthesis = self._get_synthesis()
        if synthesis is None:
            return _panel_envelope(
                "synthesis-decisions", "Synthesis Decisions", [],
                "Synthesis engine unavailable",
            )

        ledger_path = synthesis._ledger_path
        data = _read_decisions_jsonl(ledger_path, limit)

        if not data:
            return _panel_envelope(
                "synthesis-decisions", "Synthesis Decisions", [],
                "No decisions recorded",
            )

        # Compute stats
        avg_conf = (sum(d.get("confidence", 0) for d in data) / len(data)
                    if data else 0)
        by_decision = {}
        for d in data:
            dec = d.get("decision", "unknown")
            by_decision[dec] = by_decision.get(dec, 0) + 1
        top_decision = max(by_decision, key=by_decision.get) if by_decision else "none"

        return _panel_envelope(
            "synthesis-decisions", "Synthesis Decisions", data,
            (f"{len(data)} decisions, avg confidence {avg_conf:.0%}, "
             f"most common: {top_decision}"),
        )

    # ── Panel: Probe Findings ────────────────────────────────────────

    def probe_findings_panel(self) -> dict:
        """Latest probe scan results with findings and severity.

        Returns cached results from the probe engine (does NOT trigger
        a new scan). Use engine.scan() separately if fresh results needed.
        """
        engine = self._get_probe_engine()
        if engine is None:
            return _panel_envelope(
                "probe-findings", "Probe Findings", [],
                "Probe engine unavailable",
            )

        results = engine.latest_results()
        summary_dict = engine.findings_summary()

        data = []
        for r in results:
            result_dict = r.to_dict() if hasattr(r, "to_dict") else dict(r)
            data.append(result_dict)

        total_probes = summary_dict.get("total_probes", 0)
        total_findings = summary_dict.get("total_findings", 0)
        has_blocking = summary_dict.get("has_blocking", False)
        intensity = summary_dict.get("surgeon_intensity", "unknown")

        blocking_str = " (HAS BLOCKING)" if has_blocking else ""
        return _panel_envelope(
            "probe-findings", "Probe Findings", data,
            (f"{total_probes} probes, {total_findings} findings"
             f"{blocking_str}, intensity: {intensity}"),
        )

    # ── Panel: Wisdom Feedback ───────────────────────────────────────

    def wisdom_feedback_panel(self) -> dict:
        """Wisdom consumer contradictions and supporting entries.

        Aggregates: recent wisdom entries, active gotchas (from professor),
        and wisdom feedback from synthesis decisions.
        """
        items = []

        # Professor wisdom (gotchas + recent)
        prof = self._get_professor_bridge()
        if prof is not None:
            try:
                panel_data = prof.get_panel_data()
                for w in panel_data.get("recent_wisdom", []):
                    items.append({
                        "type": "wisdom",
                        "source": "professor",
                        "topic": w.get("topic", ""),
                        "the_one_thing": w.get("the_one_thing", ""),
                        "landmine_count": w.get("landmine_count", 0),
                        "queried_at": w.get("queried_at"),
                        "expired": w.get("expired", False),
                    })
                for g in panel_data.get("active_gotchas", []):
                    items.append({
                        "type": "gotcha",
                        "source": "professor",
                        "topic": g.get("topic", ""),
                        "gotcha": g.get("gotcha", ""),
                        "queried_at": g.get("queried_at"),
                    })
            except Exception as e:
                logger.debug("Professor panel data failed: %s", e)

        # Wisdom consumer feedback from evidence ledger
        consumer = self._get_wisdom_consumer()
        if consumer is not None:
            try:
                wisdom_entries = consumer.get_all()
                for entry in wisdom_entries[:20]:
                    items.append({
                        "type": "wisdom_entry",
                        "source": "evidence_ledger",
                        "topic": getattr(entry, "topic", str(entry)),
                        "content": getattr(entry, "content", ""),
                        "confidence": getattr(entry, "confidence", 0.0),
                        "source_type": getattr(entry, "source_type", ""),
                    })
            except Exception as e:
                logger.debug("WisdomConsumer get_all failed: %s", e)

        wisdom_count = sum(1 for i in items if i["type"] == "wisdom")
        gotcha_count = sum(1 for i in items if i["type"] == "gotcha")
        ledger_count = sum(1 for i in items if i["type"] == "wisdom_entry")

        parts = []
        if wisdom_count:
            parts.append(f"{wisdom_count} wisdom")
        if gotcha_count:
            parts.append(f"{gotcha_count} gotchas")
        if ledger_count:
            parts.append(f"{ledger_count} ledger entries")

        return _panel_envelope(
            "wisdom-feedback", "Wisdom Feedback", items,
            ", ".join(parts) if parts else "No wisdom data",
        )

    # ── Panel: Synaptic Consciousness ──────────────────────────────

    def synaptic_panel(self) -> dict:
        """Synaptic 8th Intelligence — active insights and fleet awareness.

        Surfaces pattern detections, anomalies, drift warnings, and
        coordination observations from the SynapticBridge.
        """
        bridge = self._get_synaptic_bridge()
        if bridge is None:
            return _panel_envelope(
                "synaptic", "Synaptic Consciousness", [],
                "Synaptic bridge unavailable",
            )

        insights = bridge.get_active_insights()
        alert = bridge.should_alert_aaron()
        stats = bridge.stats

        alert_str = " — ALERT: Aaron's attention needed" if alert else ""
        severity_counts: dict[str, int] = {}
        for i in insights:
            sev = i.get("severity", "info")
            severity_counts[sev] = severity_counts.get(sev, 0) + 1

        parts = []
        for sev in ("critical", "warning", "info"):
            count = severity_counts.get(sev, 0)
            if count:
                parts.append(f"{count} {sev}")

        summary_text = ", ".join(parts) if parts else "No active insights"
        summary_text += f" | {stats.get('total_events_processed', 0)} events processed"
        summary_text += alert_str

        return _panel_envelope(
            "synaptic", "Synaptic Consciousness", insights,
            summary_text,
        )

    # ── All Panels ───────────────────────────────────────────────────

    def all_panels(self) -> dict:
        """Return all panels in a single response for dashboard init."""
        return {
            "panels": [
                self.fleet_status_panel(),
                self.evidence_timeline_panel(),
                self.synthesis_decisions_panel(),
                self.probe_findings_panel(),
                self.wisdom_feedback_panel(),
                self.synaptic_panel(),
            ],
            "generated_at": _now_iso(),
        }


# ── Helpers ──────────────────────────────────────────────────────────

def _payload_summary(payload: dict, max_len: int = 80) -> str:
    """One-line summary of a payload dict."""
    if not payload:
        return ""
    # Try common summary keys
    for key in ("summary", "reasoning", "subject", "message", "details"):
        val = payload.get(key)
        if val and isinstance(val, str):
            return val[:max_len]
    # Fallback: first string value
    for val in payload.values():
        if isinstance(val, str) and len(val) > 5:
            return val[:max_len]
    return ""


def _read_decisions_jsonl(path: Path, limit: int = 20) -> list[dict]:
    """Read last N entries from a JSONL file (tail-read for efficiency)."""
    if not path.exists():
        return []
    try:
        lines = path.read_text().strip().splitlines()
        recent = lines[-limit:] if len(lines) > limit else lines
        recent.reverse()  # newest first
        entries = []
        for line in recent:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return entries
    except Exception as e:
        logger.debug("Failed to read decisions JSONL: %s", e)
        return []
