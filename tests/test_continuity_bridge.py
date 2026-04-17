"""Tests for ContinuityBridge — Claude/Codex handoff without session mutation."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from multifleet.continuity_bridge import ContinuityBridge, ContinuityBundle
from multifleet.model_swap import SwapCheckpointData


def _checkpoint(**overrides) -> SwapCheckpointData:
    base = {
        "checkpoint_id": "swap-20260416-120000",
        "timestamp": "2026-04-16T12:00:00Z",
        "reason": "anthropic-limit-failover",
        "source_model": "claude-opus",
        "target_model": "codex-gpt5.4",
        "branch": "main",
        "last_5_commits": ["abc123 fix", "def456 add"],
        "dirty_files": ["M multi-fleet/foo.py"],
        "staged_files": [],
        "active_task": "Finish continuity bridge",
        "fleet_inbox_count": 2,
        "claude_md_hash": "deadbeef",
        "claude_md_summary": "Fleet continuity and safety rules.",
        "session_id": "claude-session-123",
        "node_id": "mac3",
        "working_dir": "/tmp/repo",
        "last_session_summary": "Anthropic limit reached mid-task.",
        "pending_critical_findings": 1,
    }
    base.update(overrides)
    return SwapCheckpointData(**base)


class TestContinuityBridgeBuild:
    def test_build_bundle_uses_checkpoint_and_rehydration(self, tmp_path):
        bridge = ContinuityBridge(repo_root=str(tmp_path), node_id="mac3")
        cp = _checkpoint(working_dir=str(tmp_path))

        with patch.object(bridge._model_swap, "capture", return_value=cp):
            with patch.object(bridge, "_get_structured_rehydration", return_value="SESSION REHYDRATION"):
                with patch.object(bridge, "_get_session_briefing_summary", return_value="Recent threads: 2"):
                    bundle = bridge.build_bundle()

        assert bundle.node_id == "mac3"
        assert bundle.active_task == "Finish continuity bridge"
        assert bundle.session_rehydration == "SESSION REHYDRATION"
        assert bundle.session_briefing == "Recent threads: 2"
        assert bundle.checkpoint_id == cp.checkpoint_id

    def test_build_bundle_redacts_and_compacts_sensitive_text(self, tmp_path):
        bridge = ContinuityBridge(repo_root=str(tmp_path), node_id="mac3")
        cp = _checkpoint(
            working_dir=str(tmp_path),
            active_task="Rotate OPENAI_API_KEY=sk-secret-1234567890",
            last_session_summary="Bearer super-secret-token-used",
        )

        with patch.object(bridge._model_swap, "capture", return_value=cp):
            with patch.object(
                bridge,
                "_get_structured_rehydration",
                return_value="token=abc123\n" + ("x" * 5000),
            ):
                with patch.object(bridge, "_get_session_briefing_summary", return_value="api_key=shh"):
                    bundle = bridge.build_bundle(compact=True)

        assert "sk-secret" not in bundle.active_task
        assert "REDACTED" in bundle.active_task
        assert "Bearer super-secret" not in bundle.last_session_summary
        assert "REDACTED" in bundle.last_session_summary
        assert "[... truncated ...]" in bundle.session_rehydration

    def test_render_markdown_contains_resume_sections(self, tmp_path):
        bridge = ContinuityBridge(repo_root=str(tmp_path), node_id="mac3")
        bundle = ContinuityBundle(
            created_at="2026-04-16T12:00:00Z",
            node_id="mac3",
            reason="anthropic-limit-failover",
            source_model="claude-opus",
            target_model="codex-gpt5.4",
            checkpoint_id="swap-123",
            checkpoint_path="/tmp/model-swap-checkpoint.json",
            session_id="claude-session-123",
            working_dir="/tmp/repo",
            branch="main",
            active_task="Fix swarm client",
            claude_md_summary="Use additive hooks only.",
            last_session_summary="Claude hit weekly limit.",
            pending_critical_findings=1,
            fleet_inbox_count=2,
            session_rehydration="SESSION REHYDRATION — CRASH RECOVERY CONTEXT",
            session_briefing="Recent threads: 3",
        )

        text = bridge.render_markdown(bundle, to="codex")

        assert "subject: Continuity handoff: anthropic-limit-failover" in text
        assert "## Active Task" in text
        assert "Fix swarm client" in text
        assert "## Session Rehydration" in text
        assert "Claude Code session files were not rewritten." in text


class TestContinuityBridgeDelivery:
    def test_relay_bundle_prefers_http_when_daemon_accepts(self, tmp_path):
        bridge = ContinuityBridge(repo_root=str(tmp_path), node_id="mac3")
        bundle = ContinuityBundle(
            created_at="2026-04-16T12:00:00Z",
            node_id="mac3",
            reason="anthropic-limit-failover",
            source_model="claude-opus",
            target_model="codex-gpt5.4",
            checkpoint_id="swap-123",
            checkpoint_path="/tmp/checkpoint.json",
            session_id="sid",
            working_dir=str(tmp_path),
            branch="main",
            active_task="Task",
            claude_md_summary="Summary",
            last_session_summary="Last session",
            pending_critical_findings=0,
            fleet_inbox_count=0,
            session_rehydration="rehydration",
            session_briefing="briefing",
        )

        with patch.object(bridge, "_post_message", return_value={"delivered": True, "method": "P2_http"}):
            result = bridge.relay_bundle(bundle, "mac2")

        assert result["delivered"] is True
        assert result["method"] == "P2_http"
        assert not list((tmp_path / ".fleet-messages").glob("**/*.md"))

    def test_relay_bundle_falls_back_to_git_message(self, tmp_path):
        bridge = ContinuityBridge(repo_root=str(tmp_path), node_id="mac3")
        bundle = ContinuityBundle(
            created_at="2026-04-16T12:00:00Z",
            node_id="mac3",
            reason="anthropic-limit-failover",
            source_model="claude-opus",
            target_model="codex-gpt5.4",
            checkpoint_id="swap-123",
            checkpoint_path="/tmp/checkpoint.json",
            session_id="sid",
            working_dir=str(tmp_path),
            branch="main",
            active_task="Task",
            claude_md_summary="Summary",
            last_session_summary="Last session",
            pending_critical_findings=0,
            fleet_inbox_count=0,
            session_rehydration="rehydration",
            session_briefing="briefing",
        )

        with patch.object(bridge, "_post_message", return_value={"delivered": False, "error": "down"}):
            result = bridge.relay_bundle(bundle, "mac2")

        assert result["delivered"] is True
        assert result["method"] == "p7_git_file"
        path = Path(result["path"])
        assert path.exists()
        assert path.parent.name == "mac2"
        assert "Continuity Handoff" in path.read_text()

    def test_post_message_parses_daemon_response(self, tmp_path):
        bridge = ContinuityBridge(repo_root=str(tmp_path), node_id="mac3")
        fake = MagicMock()
        fake.read.return_value = json.dumps({"delivered": True, "method": "P1_nats"}).encode("utf-8")
        fake.__enter__.return_value = fake
        fake.__exit__.return_value = None

        with patch("multifleet.continuity_bridge.urlopen", return_value=fake):
            result = bridge._post_message("mac2", "subject", "body")

        assert result["delivered"] is True
        assert result["method"] == "P1_nats"

    def test_alignment_broadcast_mentions_additive_rollout(self, tmp_path):
        bridge = ContinuityBridge(repo_root=str(tmp_path), node_id="mac3")
        bundle = ContinuityBundle(
            created_at="2026-04-16T12:00:00Z",
            node_id="mac3",
            reason="anthropic-limit-failover",
            source_model="claude-opus",
            target_model="codex-gpt5.4",
            checkpoint_id="swap-123",
            checkpoint_path="/tmp/checkpoint.json",
            session_id="sid",
            working_dir=str(tmp_path),
            branch="main",
            active_task="Review OPENAI_API_KEY=secret",
            claude_md_summary="Summary",
            last_session_summary="Last session",
            pending_critical_findings=0,
            fleet_inbox_count=0,
            session_rehydration="rehydration",
            session_briefing="briefing",
        )

        text = bridge.render_alignment_broadcast(bundle)
        assert "Additive rollout only." in text
        assert "Each node may run Codex as a backup continuity agent" in text
        assert "OPENAI_API_KEY" in text
        assert "OPENAI_API_KEY=secret" not in text
        assert "OPENAI_API_KEY=REDACTED" in text
