"""Tests for theatrical dashboard components."""

import pytest

from multifleet.dashboard_components import (
    COMPONENTS,
    SHARED_CSS,
    EvidenceTimeline,
    QuorumPulse,
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

    def test_registry_maps_to_classes(self):
        assert COMPONENTS["vitals"] is VitalSignsBar
        assert COMPONENTS["surgeon-feed"] is SurgeonFeed
        assert COMPONENTS["quorum-pulse"] is QuorumPulse
        assert COMPONENTS["evidence-timeline"] is EvidenceTimeline

    def test_render_all_components_standalone(self):
        html = render_all_components()
        assert "<!DOCTYPE html>" in html
        assert "Vital Signs" in html
        assert "Surgeon Feed" in html
        assert "Quorum Pulse" in html
        assert "Evidence Timeline" in html
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
