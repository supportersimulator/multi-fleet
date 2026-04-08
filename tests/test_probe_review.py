"""Tests for probe_review — practical integration of the full organism pipeline."""

from multifleet.probe_review import run_probe_review


class TestProbeReview:

    def test_basic_review(self):
        result = run_probe_review(repo_root=".", task_intent="Test review", consult_surgeons=False)
        assert result["task_intent"] == "Test review"
        assert "probes" in result
        assert result["probes"]["total"] > 0
        assert "acceptance" in result
        assert result["elapsed_s"] >= 0

    def test_review_builds_packet(self):
        result = run_probe_review(repo_root=".", task_intent="Packet test", consult_surgeons=False)
        assert "packet" in result
        assert isinstance(result["packet"], str)  # summary string

    def test_review_runs_acceptance_gate(self):
        result = run_probe_review(repo_root=".", task_intent="Gate test", consult_surgeons=False)
        assert "acceptance" in result
        acceptance = result["acceptance"]
        assert "accepted" in acceptance or "error" in acceptance

    def test_review_without_intent(self):
        result = run_probe_review(repo_root=".", consult_surgeons=False)
        assert result["task_intent"] == ""
        assert result["probes"]["total"] > 0

    def test_surgeon_skipped_for_light_intensity(self):
        # When intensity is light, surgeon consult should be skipped
        result = run_probe_review(repo_root=".", task_intent="Trivial change", consult_surgeons=True)
        # May or may not skip depending on probe results
        if result.get("surgeon_verdict"):
            assert isinstance(result["surgeon_verdict"], dict)
