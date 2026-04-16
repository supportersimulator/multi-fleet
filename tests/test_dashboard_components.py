"""Tests for theatrical dashboard components."""

import pytest

from multifleet.dashboard_components import (
    COMPONENTS,
    SHARED_CSS,
    CorrigibilityGauge,
    EvidenceTimeline,
    FleetConstellation,
    MemoryHeatMap,
    ProbeStatusGrid,
    QuorumPulse,
    SessionGoldStream,
    SurgeonFeed,
    VitalSignsBar,
    get_component_css,
    render_all_components,
)


# ── VitalSignsBar ──────────────────────────────────────────────────


class TestVitalSignsBar:
    def test_sample_data_has_required_keys(self):
        data = VitalSignsBar.get_sample_data()
        assert "nodes" in data
        assert len(data["nodes"]) == 3
        for node in data["nodes"]:
            assert "id" in node
            assert "status" in node
            assert "stress" in node
            assert "channels" in node
            assert "latency_history" in node

    def test_render_contains_node_ids(self):
        comp = VitalSignsBar()
        html = comp.render(VitalSignsBar.get_sample_data())
        assert "mac1" in html
        assert "mac2" in html
        assert "mac3" in html

    def test_render_contains_vital_signs_header(self):
        html = VitalSignsBar().render(VitalSignsBar.get_sample_data())
        assert "Vital Signs" in html

    def test_render_contains_stress_gauge(self):
        html = VitalSignsBar().render(VitalSignsBar.get_sample_data())
        assert "STRESS" in html

    def test_render_contains_channel_indicators(self):
        html = VitalSignsBar().render(VitalSignsBar.get_sample_data())
        # Channel dots have title attributes P1-P7
        assert 'title="P1"' in html
        assert 'title="P7"' in html

    def test_render_contains_sparkline_svg(self):
        html = VitalSignsBar().render(VitalSignsBar.get_sample_data())
        assert "<svg" in html
        assert "polyline" in html

    def test_render_empty_nodes(self):
        html = VitalSignsBar().render({"nodes": []})
        assert "No node data" in html

    def test_render_heartbeat_animation(self):
        html = VitalSignsBar().render(VitalSignsBar.get_sample_data())
        assert "tc-heartbeat" in html

    def test_sparkline_flat_data(self):
        """All-zero latency should render a dashed line, not a polyline."""
        html = VitalSignsBar._render_sparkline([0] * 20)
        assert "stroke-dasharray" in html
        assert "polyline" not in html

    def test_component_wrapper_class(self):
        html = VitalSignsBar().render(VitalSignsBar.get_sample_data())
        assert 'class="tc-component tc-vitals"' in html


# ── SurgeonFeed ────────────────────────────────────────────────────


class TestSurgeonFeed:
    def test_sample_data_has_entries(self):
        data = SurgeonFeed.get_sample_data()
        assert "entries" in data
        assert len(data["entries"]) >= 3
        assert "active_gates" in data

    def test_render_contains_surgeon_names(self):
        html = SurgeonFeed().render(SurgeonFeed.get_sample_data())
        assert "ATLAS" in html
        assert "CARDIOLOGIST" in html
        assert "NEUROLOGIST" in html

    def test_render_contains_feed_header(self):
        html = SurgeonFeed().render(SurgeonFeed.get_sample_data())
        assert "Surgeon Feed" in html

    def test_render_contains_confidence_bars(self):
        html = SurgeonFeed().render(SurgeonFeed.get_sample_data())
        assert "82%" in html or "82" in html

    def test_render_contains_gate_badges(self):
        html = SurgeonFeed().render(SurgeonFeed.get_sample_data())
        assert "architectural-gate" in html
        assert "pre-implementation" in html

    def test_render_dissent_styling(self):
        """Dissent entries should have red border."""
        html = SurgeonFeed().render(SurgeonFeed.get_sample_data())
        # The dissent entry has "Dissent" as action
        assert "Dissent" in html

    def test_render_phase_indicators(self):
        html = SurgeonFeed().render(SurgeonFeed.get_sample_data())
        # Phase labels are single letters in bubbles
        assert ">I<" in html  # initial
        assert ">S<" in html  # synthesis
        assert ">X<" in html  # cross_review

    def test_render_empty_feed(self):
        html = SurgeonFeed().render({"entries": [], "active_gates": []})
        assert "No surgeon activity" in html

    def test_render_slide_in_animation(self):
        html = SurgeonFeed().render(SurgeonFeed.get_sample_data())
        assert "tc-slide-in" in html


# ── QuorumPulse ────────────────────────────────────────────────────


class TestQuorumPulse:
    def test_sample_data_has_signals(self):
        data = QuorumPulse.get_sample_data()
        assert "signals" in data
        for key in ("las", "rhl", "pqs", "iqs"):
            assert key in data["signals"]
        assert "proposals" in data
        assert "quorum_threshold" in data

    def test_render_contains_signal_names(self):
        html = QuorumPulse().render(QuorumPulse.get_sample_data())
        assert "LAS" in html
        assert "RHL" in html
        assert "PQS" in html
        assert "IQS" in html

    def test_render_contains_header(self):
        html = QuorumPulse().render(QuorumPulse.get_sample_data())
        assert "Quorum Pulse" in html

    def test_render_contains_quorum_status(self):
        html = QuorumPulse().render(QuorumPulse.get_sample_data())
        assert "MET" in html
        assert "67%" in html

    def test_render_contains_proposals(self):
        html = QuorumPulse().render(QuorumPulse.get_sample_data())
        assert "prop-001" in html
        assert "Migrate packet store" in html

    def test_render_quorum_not_met(self):
        data = QuorumPulse.get_sample_data()
        data["quorum_met"] = False
        html = QuorumPulse().render(data)
        assert "NOT MET" in html

    def test_render_signal_flow_animation(self):
        html = QuorumPulse().render(QuorumPulse.get_sample_data())
        assert "tc-signal-flow" in html

    def test_render_empty_signals(self):
        html = QuorumPulse().render({"signals": {}, "proposals": [],
                                      "quorum_threshold": 0.5, "quorum_met": False})
        assert "Quorum Pulse" in html

    def test_render_proposal_status_colors(self):
        html = QuorumPulse().render(QuorumPulse.get_sample_data())
        assert "APPROVED" in html
        assert "PENDING" in html


# ── EvidenceTimeline ───────────────────────────────────────────────


class TestEvidenceTimeline:
    def test_sample_data_has_entries(self):
        data = EvidenceTimeline.get_sample_data()
        assert "entries" in data
        assert len(data["entries"]) == 10
        assert "chain_valid" in data

    def test_render_contains_header(self):
        html = EvidenceTimeline().render(EvidenceTimeline.get_sample_data())
        assert "Evidence Timeline" in html

    def test_render_contains_truncated_hashes(self):
        data = EvidenceTimeline.get_sample_data()
        html = EvidenceTimeline().render(data)
        # First hash truncated to 12 chars
        h12 = data["entries"][0]["hash"][:12]
        assert h12 in html

    def test_render_contains_node_names(self):
        html = EvidenceTimeline().render(EvidenceTimeline.get_sample_data())
        assert "mac1" in html
        assert "mac2" in html
        assert "mac3" in html

    def test_render_contains_analyzer_names(self):
        html = EvidenceTimeline().render(EvidenceTimeline.get_sample_data())
        assert "probe_engine" in html
        assert "ghost_layer" in html

    def test_render_chain_valid_status(self):
        html = EvidenceTimeline().render(EvidenceTimeline.get_sample_data())
        assert "UNBROKEN" in html

    def test_render_chain_broken_status(self):
        data = EvidenceTimeline.get_sample_data()
        data["chain_valid"] = False
        html = EvidenceTimeline().render(data)
        assert "BROKEN" in html

    def test_render_clickable_entries(self):
        html = EvidenceTimeline().render(EvidenceTimeline.get_sample_data())
        assert "onclick" in html
        assert "display" in html

    def test_render_chain_pulse_animation(self):
        html = EvidenceTimeline().render(EvidenceTimeline.get_sample_data())
        assert "tc-chain-pulse" in html

    def test_render_entry_count(self):
        html = EvidenceTimeline().render(EvidenceTimeline.get_sample_data())
        assert "10 entries" in html

    def test_render_empty_entries(self):
        html = EvidenceTimeline().render({"entries": [], "chain_valid": True})
        assert "No evidence entries" in html


# ── Registry + Integration ─────────────────────────────────────────


class TestComponentRegistry:
    def test_all_components_registered(self):
        assert "vitals" in COMPONENTS
        assert "surgeon-feed" in COMPONENTS
        assert "quorum-pulse" in COMPONENTS
        assert "evidence-timeline" in COMPONENTS
        assert "fleet-constellation" in COMPONENTS
        assert "memory-heatmap" in COMPONENTS
        assert "probe-status-grid" in COMPONENTS
        assert "corrigibility-gauge" in COMPONENTS
        assert "session-gold-stream" in COMPONENTS

    def test_registry_maps_to_classes(self):
        assert COMPONENTS["vitals"] is VitalSignsBar
        assert COMPONENTS["surgeon-feed"] is SurgeonFeed
        assert COMPONENTS["quorum-pulse"] is QuorumPulse
        assert COMPONENTS["evidence-timeline"] is EvidenceTimeline
        assert COMPONENTS["fleet-constellation"] is FleetConstellation
        assert COMPONENTS["memory-heatmap"] is MemoryHeatMap
        assert COMPONENTS["probe-status-grid"] is ProbeStatusGrid
        assert COMPONENTS["corrigibility-gauge"] is CorrigibilityGauge
        assert COMPONENTS["session-gold-stream"] is SessionGoldStream

    def test_render_all_components_standalone(self):
        html = render_all_components()
        assert "<!DOCTYPE html>" in html
        assert "Vital Signs" in html
        assert "Surgeon Feed" in html
        assert "Quorum Pulse" in html
        assert "Evidence Timeline" in html
        assert "Fleet Constellation" in html
        assert "Memory Heat Map" in html
        assert "Probe Status Grid" in html
        assert "Corrigibility Gauge" in html
        assert "Session Gold Stream" in html
        assert "THEATRICAL DASHBOARD" in html

    def test_render_all_with_custom_data(self):
        custom = {"vitals": VitalSignsBar.get_sample_data()}
        html = render_all_components(custom)
        assert "mac1" in html

    def test_get_component_css(self):
        css = get_component_css()
        assert ".tc-component" in css
        assert "tc-heartbeat" in css
        assert "tc-signal-flow" in css
        assert "tc-slide-in" in css

    def test_shared_css_contains_all_animations(self):
        assert "tc-heartbeat" in SHARED_CSS
        assert "tc-pulse-ring" in SHARED_CSS
        assert "tc-slide-in" in SHARED_CSS
        assert "tc-signal-flow" in SHARED_CSS
        assert "tc-fill" in SHARED_CSS
        assert "tc-chain-pulse" in SHARED_CSS


class TestComponentEmbeddability:
    """Verify components produce valid HTML fragments for embedding."""

    @pytest.mark.parametrize("name,cls", list(COMPONENTS.items()))
    def test_component_renders_div(self, name, cls):
        comp = cls()
        html = comp.render(cls.get_sample_data())
        assert html.strip().startswith("<div")
        assert html.strip().endswith("</div>")

    @pytest.mark.parametrize("name,cls", list(COMPONENTS.items()))
    def test_component_has_tc_class(self, name, cls):
        comp = cls()
        html = comp.render(cls.get_sample_data())
        assert f"tc-{name}" in html

    @pytest.mark.parametrize("name,cls", list(COMPONENTS.items()))
    def test_component_has_h3_header(self, name, cls):
        comp = cls()
        html = comp.render(cls.get_sample_data())
        assert "<h3>" in html


class TestFlaskRegistration:
    """Test register_components with a minimal Flask app."""

    def test_register_adds_routes(self):
        try:
            from flask import Flask
        except ImportError:
            pytest.skip("Flask not installed")

        from multifleet.dashboard_components import register_components

        app = Flask(__name__)
        register_components(app)

        # Check routes were added
        rules = [r.rule for r in app.url_map.iter_rules()]
        assert "/dashboard/component/<name>" in rules
        assert "/dashboard/components" in rules

    def test_component_route_returns_html(self):
        try:
            from flask import Flask
        except ImportError:
            pytest.skip("Flask not installed")

        from multifleet.dashboard_components import register_components

        app = Flask(__name__)
        register_components(app)

        with app.test_client() as client:
            resp = client.get("/dashboard/component/vitals")
            assert resp.status_code == 200
            assert b"Vital Signs" in resp.data

    def test_unknown_component_returns_404(self):
        try:
            from flask import Flask
        except ImportError:
            pytest.skip("Flask not installed")

        from multifleet.dashboard_components import register_components

        app = Flask(__name__)
        register_components(app)

        with app.test_client() as client:
            resp = client.get("/dashboard/component/nonexistent")
            assert resp.status_code == 404

    def test_full_page_mode(self):
        try:
            from flask import Flask
        except ImportError:
            pytest.skip("Flask not installed")

        from multifleet.dashboard_components import register_components

        app = Flask(__name__)
        register_components(app)

        with app.test_client() as client:
            resp = client.get("/dashboard/component/vitals?full=1")
            assert resp.status_code == 200
            assert b"<!DOCTYPE html>" in resp.data

    def test_all_components_route(self):
        try:
            from flask import Flask
        except ImportError:
            pytest.skip("Flask not installed")

        from multifleet.dashboard_components import register_components

        app = Flask(__name__)
        register_components(app)

        with app.test_client() as client:
            resp = client.get("/dashboard/components")
            assert resp.status_code == 200
            assert b"THEATRICAL DASHBOARD" in resp.data


# ── FleetConstellation ────────────────────────────────────────────


class TestFleetConstellation:
    def test_sample_data_has_required_keys(self):
        data = FleetConstellation.get_sample_data()
        assert "nodes" in data
        assert "connections" in data
        assert len(data["nodes"]) == 3
        assert len(data["connections"]) == 3

    def test_render_contains_node_ids(self):
        html = FleetConstellation().render(FleetConstellation.get_sample_data())
        assert "mac1" in html
        assert "mac2" in html
        assert "mac3" in html

    def test_render_contains_header(self):
        html = FleetConstellation().render(FleetConstellation.get_sample_data())
        assert "Fleet Constellation" in html

    def test_render_contains_svg(self):
        html = FleetConstellation().render(FleetConstellation.get_sample_data())
        assert "<svg" in html
        assert "<circle" in html
        assert "<line" in html

    def test_render_contains_connection_channels(self):
        html = FleetConstellation().render(FleetConstellation.get_sample_data())
        assert "P1" in html
        assert "P7" in html

    def test_render_contains_status_labels(self):
        html = FleetConstellation().render(FleetConstellation.get_sample_data())
        assert "ONLINE" in html
        assert "DEGRADED" in html
        assert "OFFLINE" in html

    def test_render_empty_nodes(self):
        html = FleetConstellation().render({"nodes": [], "connections": []})
        assert "No topology data" in html

    def test_component_wrapper_class(self):
        html = FleetConstellation().render(FleetConstellation.get_sample_data())
        assert 'tc-fleet-constellation' in html

    def test_render_health_percentages(self):
        html = FleetConstellation().render(FleetConstellation.get_sample_data())
        assert "90%" in html
        assert "40%" in html
        assert "10%" in html


# ── MemoryHeatMap ─────────────────────────────────────────────────


class TestMemoryHeatMap:
    def test_sample_data_has_topics(self):
        data = MemoryHeatMap.get_sample_data()
        assert "topics" in data
        assert len(data["topics"]) == 7

    def test_render_contains_header(self):
        html = MemoryHeatMap().render(MemoryHeatMap.get_sample_data())
        assert "Memory Heat Map" in html

    def test_render_contains_topic_names(self):
        html = MemoryHeatMap().render(MemoryHeatMap.get_sample_data())
        assert "FLEET" in html
        assert "SURGEONS" in html
        assert "WEBHOOK" in html
        assert "SYNAPTIC" in html

    def test_render_contains_counts(self):
        html = MemoryHeatMap().render(MemoryHeatMap.get_sample_data())
        assert "347" in html
        assert "412" in html

    def test_render_contains_density_bars(self):
        html = MemoryHeatMap().render(MemoryHeatMap.get_sample_data())
        assert "tc-fill" in html

    def test_render_contains_clickable_expand(self):
        html = MemoryHeatMap().render(MemoryHeatMap.get_sample_data())
        assert "onclick" in html

    def test_render_empty_topics(self):
        html = MemoryHeatMap().render({"topics": []})
        assert "No topic data" in html

    def test_component_wrapper_class(self):
        html = MemoryHeatMap().render(MemoryHeatMap.get_sample_data())
        assert 'tc-memory-heatmap' in html

    def test_render_recent_entries_hidden(self):
        html = MemoryHeatMap().render(MemoryHeatMap.get_sample_data())
        assert 'display:none' in html


# ── ProbeStatusGrid ───────────────────────────────────────────────


class TestProbeStatusGrid:
    def test_sample_data_has_18_probes(self):
        data = ProbeStatusGrid.get_sample_data()
        assert "probes" in data
        assert len(data["probes"]) == 18

    def test_render_contains_header(self):
        html = ProbeStatusGrid().render(ProbeStatusGrid.get_sample_data())
        assert "Probe Status Grid" in html

    def test_render_contains_probe_names(self):
        html = ProbeStatusGrid().render(ProbeStatusGrid.get_sample_data())
        assert "mirror_drift" in html
        assert "webhook_health" in html
        assert "constitutional_compliance" in html

    def test_render_contains_summary_counts(self):
        html = ProbeStatusGrid().render(ProbeStatusGrid.get_sample_data())
        assert "passing" in html
        assert "stale" in html
        assert "failing" in html

    def test_render_contains_score_percentages(self):
        html = ProbeStatusGrid().render(ProbeStatusGrid.get_sample_data())
        assert "%" in html

    def test_render_uses_grid_layout(self):
        html = ProbeStatusGrid().render(ProbeStatusGrid.get_sample_data())
        assert "grid-template-columns" in html

    def test_render_empty_probes(self):
        html = ProbeStatusGrid().render({"probes": []})
        assert "No probe data" in html

    def test_component_wrapper_class(self):
        html = ProbeStatusGrid().render(ProbeStatusGrid.get_sample_data())
        assert 'tc-probe-status-grid' in html

    def test_render_status_icons(self):
        html = ProbeStatusGrid().render(ProbeStatusGrid.get_sample_data())
        assert "\u2713" in html  # checkmark for passing
        assert "\u2717" in html  # x for failing


# ── CorrigibilityGauge ────────────────────────────────────────────


class TestCorrigibilityGauge:
    def test_sample_data_has_required_keys(self):
        data = CorrigibilityGauge.get_sample_data()
        assert "score" in data
        assert "trend" in data
        assert "history" in data
        assert "factors" in data
        assert len(data["history"]) == 5
        assert len(data["factors"]) == 3

    def test_render_contains_header(self):
        html = CorrigibilityGauge().render(CorrigibilityGauge.get_sample_data())
        assert "Corrigibility Gauge" in html

    def test_render_contains_svg_gauge(self):
        html = CorrigibilityGauge().render(CorrigibilityGauge.get_sample_data())
        assert "<svg" in html
        assert "<circle" in html

    def test_render_contains_score(self):
        html = CorrigibilityGauge().render(CorrigibilityGauge.get_sample_data())
        assert "88%" in html or "87%" in html

    def test_render_contains_trend_arrow(self):
        html = CorrigibilityGauge().render(CorrigibilityGauge.get_sample_data())
        assert "\u2191" in html  # up arrow

    def test_render_contains_corrigibility_label(self):
        html = CorrigibilityGauge().render(CorrigibilityGauge.get_sample_data())
        assert "CORRIGIBILITY" in html

    def test_render_contains_factor_names(self):
        html = CorrigibilityGauge().render(CorrigibilityGauge.get_sample_data())
        assert "Surgeon consensus" in html
        assert "Evidence backing" in html
        assert "Constitutional compliance" in html

    def test_render_contains_history_sparkline(self):
        html = CorrigibilityGauge().render(CorrigibilityGauge.get_sample_data())
        assert "polyline" in html
        assert "Last 5 readings" in html

    def test_render_trend_down(self):
        data = CorrigibilityGauge.get_sample_data()
        data["trend"] = "down"
        html = CorrigibilityGauge().render(data)
        assert "\u2193" in html  # down arrow

    def test_component_wrapper_class(self):
        html = CorrigibilityGauge().render(CorrigibilityGauge.get_sample_data())
        assert 'tc-corrigibility-gauge' in html


# ── SessionGoldStream ─────────────────────────────────────────────


class TestSessionGoldStream:
    def test_sample_data_has_nuggets(self):
        data = SessionGoldStream.get_sample_data()
        assert "nuggets" in data
        assert len(data["nuggets"]) >= 5

    def test_render_contains_header(self):
        html = SessionGoldStream().render(SessionGoldStream.get_sample_data())
        assert "Session Gold Stream" in html

    def test_render_contains_severity_labels(self):
        html = SessionGoldStream().render(SessionGoldStream.get_sample_data())
        assert "CRIT" in html
        assert "ARCH" in html
        assert "QUAL" in html

    def test_render_contains_pass_names(self):
        html = SessionGoldStream().render(SessionGoldStream.get_sample_data())
        assert "eval_webhook_quality" in html
        assert "quality_cardiologist" in html
        assert "corrigibility_gate" in html

    def test_render_contains_findings(self):
        html = SessionGoldStream().render(SessionGoldStream.get_sample_data())
        assert "S2 professor injection" in html

    def test_render_contains_summary_counts(self):
        html = SessionGoldStream().render(SessionGoldStream.get_sample_data())
        # Should show count of each severity
        assert "2 CRIT" in html
        assert "2 ARCH" in html

    def test_render_contains_slide_animation(self):
        html = SessionGoldStream().render(SessionGoldStream.get_sample_data())
        assert "tc-slide-in" in html

    def test_render_sorted_newest_first(self):
        html = SessionGoldStream().render(SessionGoldStream.get_sample_data())
        # session_gold (most recent) should appear before cv_sentinel (older)
        pos_recent = html.find("session_gold")
        pos_older = html.find("cv_sentinel")
        assert pos_recent < pos_older

    def test_render_empty_nuggets(self):
        html = SessionGoldStream().render({"nuggets": []})
        assert "No gold mined yet" in html

    def test_component_wrapper_class(self):
        html = SessionGoldStream().render(SessionGoldStream.get_sample_data())
        assert 'tc-session-gold-stream' in html
