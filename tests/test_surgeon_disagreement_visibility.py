"""Tests for surgeon disagreement visibility in the theatrical dashboard.

Verifies that when 3 surgeons disagree, each position is shown separately
with visual emphasis (red borders, warning icons), and corrigibility
ledger before/after deltas are rendered.
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from multifleet.theatrical.surgeon_feed import SurgeonFeed


# ── Test Data Factories ──

def _base_surgeon_data(**overrides):
    base = {
        "surgeons": {"atlas": "active", "cardiologist": "ok", "neurologist": "ok"},
        "cross_exams": [],
        "rebuttals": [],
        "disagreements": [],
        "decision_points": [],
    }
    base.update(overrides)
    return base


def _cross_exam_with_positions(consensus=0.5, **overrides):
    """Cross-exam where surgeons have individual positions."""
    base = {
        "timestamp": time.time(),
        "topic": "memory refactor approach",
        "consensus": consensus,
        "cost": 0.008,
        "mode": "standard",
        "surgeon_positions": [
            {"name": "atlas", "stance": "Refactor using adapter pattern", "confidence": 0.9},
            {"name": "cardiologist", "stance": "Full rewrite cleaner long-term", "confidence": 0.7},
            {"name": "neurologist", "stance": "Incremental migration safer", "confidence": 0.6},
        ],
    }
    base.update(overrides)
    return base


def _disagreement_entry(**overrides):
    base = {
        "ts": time.time(),
        "topic": "Redis caching strategy",
        "surgeon_a_position": "Use TTL-based eviction with 5min window",
        "surgeon_b_position": "Use LRU with manual invalidation",
        "resolution": "TTL chosen after evidence review",
        "evidence": {
            "surgeon_a_name": "Atlas",
            "surgeon_b_name": "Cardiologist",
        },
    }
    base.update(overrides)
    return base


def _decision_point(**overrides):
    base = {
        "ts": time.time(),
        "context": "webhook retry logic",
        "proposed_action": "exponential backoff with 3 retries",
        "final_action": "linear backoff with 5 retries per surgeon recommendation",
        "changed": True,
    }
    base.update(overrides)
    return base


# ── Disagreement Rendering Tests ──

class TestDisagreementVisibility:
    """Surgeon disagreements must be visually prominent."""

    def test_disagreement_section_rendered(self):
        """Disagreements section appears when data contains disagreements."""
        data = _base_surgeon_data(disagreements=[_disagreement_entry()])
        html = SurgeonFeed().render(data)
        assert "Surgeon Disagreements" in html
        assert "tc-badge-red" in html

    def test_disagreement_count_badge(self):
        """Badge shows correct count of disagreements."""
        data = _base_surgeon_data(disagreements=[
            _disagreement_entry(topic="issue 1"),
            _disagreement_entry(topic="issue 2"),
            _disagreement_entry(topic="issue 3"),
        ])
        html = SurgeonFeed().render(data)
        assert ">3<" in html  # badge content

    def test_both_positions_shown_separately(self):
        """Each surgeon's position appears as a separate element."""
        data = _base_surgeon_data(disagreements=[_disagreement_entry()])
        html = SurgeonFeed().render(data)
        assert "TTL-based eviction" in html
        assert "LRU with manual invalidation" in html

    def test_surgeon_names_from_evidence(self):
        """Surgeon names pulled from evidence metadata."""
        data = _base_surgeon_data(disagreements=[_disagreement_entry()])
        html = SurgeonFeed().render(data)
        assert "Atlas" in html
        assert "Cardiologist" in html

    def test_red_border_on_disagreement(self):
        """Disagreement cards have red border for visual emphasis."""
        data = _base_surgeon_data(disagreements=[_disagreement_entry()])
        html = SurgeonFeed().render(data)
        assert "border:2px solid #ef4444" in html

    def test_warning_icon_on_disagreement(self):
        """Warning icon (&#9888;) appears on disagreement entries."""
        data = _base_surgeon_data(disagreements=[_disagreement_entry()])
        html = SurgeonFeed().render(data)
        assert "&#9888;" in html

    def test_resolution_shown(self):
        """Resolution text appears in disagreement card."""
        data = _base_surgeon_data(disagreements=[_disagreement_entry()])
        html = SurgeonFeed().render(data)
        assert "TTL chosen after evidence review" in html

    def test_unresolved_disagreement(self):
        """Unresolved disagreements render with yellow color."""
        entry = _disagreement_entry(resolution="unresolved")
        data = _base_surgeon_data(disagreements=[entry])
        html = SurgeonFeed().render(data)
        assert "unresolved" in html
        assert "#eab308" in html  # yellow for unresolved

    def test_no_disagreements_hides_section(self):
        """No disagreements = no disagreement section rendered."""
        data = _base_surgeon_data(disagreements=[])
        html = SurgeonFeed().render(data)
        assert "Surgeon Disagreements" not in html

    def test_xss_in_disagreement_positions(self):
        """XSS in surgeon positions is escaped."""
        entry = _disagreement_entry(
            surgeon_a_position="<script>alert(1)</script>",
            surgeon_b_position='<img onerror="hack">',
        )
        data = _base_surgeon_data(disagreements=[entry])
        html = SurgeonFeed().render(data)
        assert "<script>" not in html
        assert "&lt;script&gt;" in html
        assert "<img" not in html


# ── Individual Position Tests (in cross-exam entries) ──

class TestSurgeonPositionsInCrossExam:
    """When cross-exams have low consensus, individual positions appear inline."""

    def test_positions_shown_on_low_consensus(self):
        """Individual positions render when consensus < 0.7."""
        exam = _cross_exam_with_positions(consensus=0.5)
        data = _base_surgeon_data(cross_exams=[exam])
        html = SurgeonFeed().render(data)
        assert "Refactor using adapter pattern" in html
        assert "Full rewrite cleaner long-term" in html
        assert "Incremental migration safer" in html

    def test_positions_shown_with_surgeon_colors(self):
        """Each position uses the surgeon's designated color."""
        exam = _cross_exam_with_positions(consensus=0.4)
        data = _base_surgeon_data(cross_exams=[exam])
        html = SurgeonFeed().render(data)
        assert "#3b82f6" in html  # atlas blue
        assert "#ef4444" in html  # cardiologist red
        assert "#a855f7" in html  # neurologist purple

    def test_confidence_shown_per_surgeon(self):
        """Each surgeon's confidence percentage appears."""
        exam = _cross_exam_with_positions(consensus=0.5)
        data = _base_surgeon_data(cross_exams=[exam])
        html = SurgeonFeed().render(data)
        assert "90%" in html
        assert "70%" in html
        assert "60%" in html

    def test_dissent_border_on_low_consensus(self):
        """Red dissent border applied when consensus < 0.7."""
        exam = _cross_exam_with_positions(consensus=0.5)
        data = _base_surgeon_data(cross_exams=[exam])
        html = SurgeonFeed().render(data)
        assert "border-left:2px solid #ef4444" in html

    def test_no_positions_on_high_consensus(self):
        """No individual positions rendered when all agree (no positions data)."""
        exam = {
            "timestamp": time.time(), "topic": "unanimous decision",
            "consensus": 0.95, "cost": 0.01, "mode": "standard",
        }
        data = _base_surgeon_data(cross_exams=[exam])
        html = SurgeonFeed().render(data)
        assert "tc-surgeon-position" not in html

    def test_warning_icon_on_low_confidence_dissent(self):
        """Warning icon appears for low-confidence positions in dissent."""
        exam = _cross_exam_with_positions(consensus=0.4)
        # neurologist has 0.6 confidence — should NOT get warning (threshold is < 0.6)
        # adjust to trigger warning
        exam["surgeon_positions"][2]["confidence"] = 0.5
        data = _base_surgeon_data(cross_exams=[exam])
        html = SurgeonFeed().render(data)
        assert "&#9888;" in html


# ── Decision Delta Tests (Corrigibility Ledger) ──

class TestDecisionDeltas:
    """Before/after deltas from corrigibility ledger appear in feed."""

    def test_delta_section_rendered(self):
        """Decision points create a Corrigibility Deltas section."""
        data = _base_surgeon_data(decision_points=[_decision_point()])
        html = SurgeonFeed().render(data)
        assert "Corrigibility Deltas" in html

    def test_proposed_action_strikethrough(self):
        """Proposed action shown with line-through style."""
        data = _base_surgeon_data(decision_points=[_decision_point()])
        html = SurgeonFeed().render(data)
        assert "text-decoration:line-through" in html
        assert "exponential backoff" in html

    def test_final_action_shown(self):
        """Final action visible after arrow."""
        data = _base_surgeon_data(decision_points=[_decision_point()])
        html = SurgeonFeed().render(data)
        assert "linear backoff with 5 retries" in html
        assert "&#8594;" in html  # right arrow

    def test_changed_indicator(self):
        """Changed decisions get yellow border and swap icon."""
        data = _base_surgeon_data(decision_points=[_decision_point(changed=True)])
        html = SurgeonFeed().render(data)
        assert "#eab308" in html  # yellow for changed
        assert "&#8644;" in html  # bidirectional arrow

    def test_unchanged_indicator(self):
        """Unchanged decisions get green checkmark."""
        data = _base_surgeon_data(decision_points=[_decision_point(changed=False)])
        html = SurgeonFeed().render(data)
        assert "&#10003;" in html  # checkmark

    def test_no_deltas_hides_section(self):
        """No decision points = no delta section."""
        data = _base_surgeon_data(decision_points=[])
        html = SurgeonFeed().render(data)
        assert "Corrigibility Deltas" not in html

    def test_context_displayed(self):
        """Decision context label is visible."""
        data = _base_surgeon_data(decision_points=[_decision_point()])
        html = SurgeonFeed().render(data)
        assert "webhook retry logic" in html


# ── Integration: All sections together ──

class TestFullFeedIntegration:
    """All feed sections render together without breaking."""

    def test_all_sections_present(self):
        """Feed with disagreements, cross-exams, rebuttals, and deltas."""
        data = _base_surgeon_data(
            cross_exams=[_cross_exam_with_positions(consensus=0.5)],
            rebuttals=[{"task_id": "abc12345", "phase": "under_review",
                        "verdicts_count": 2, "expected": 3}],
            disagreements=[_disagreement_entry()],
            decision_points=[_decision_point()],
        )
        html = SurgeonFeed().render(data)
        assert "Surgeon Feed" in html
        assert "tc-surgeon-feed" in html
        assert "Cross-Examinations" in html
        assert "Active Rebuttals" in html
        assert "Surgeon Disagreements" in html
        assert "Corrigibility Deltas" in html

    def test_empty_data_still_renders(self):
        """Empty data dict renders without crash."""
        html = SurgeonFeed().render(_base_surgeon_data())
        assert "tc-surgeon-feed" in html
        assert "No recent cross-examinations" in html

    def test_none_renders_unavailable(self):
        """None data renders unavailable state."""
        html = SurgeonFeed().render(None)
        assert "unavailable" in html.lower() or "tc-unavailable" in html

    def test_disagreements_above_cross_exams(self):
        """Disagreement section appears before cross-examinations in DOM."""
        data = _base_surgeon_data(
            cross_exams=[_cross_exam_with_positions()],
            disagreements=[_disagreement_entry()],
        )
        html = SurgeonFeed().render(data)
        disagree_pos = html.index("Surgeon Disagreements")
        crossexam_pos = html.index("Cross-Examinations")
        assert disagree_pos < crossexam_pos, "Disagreements must appear above cross-exams"
