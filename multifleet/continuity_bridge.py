"""Continuity bridge between Claude Code sessions, Codex failover, and Multi-Fleet.

This module intentionally does NOT modify Claude Code's native session files.
It builds a resumable handoff bundle from existing sources of truth:

- Model swap checkpoint (git/task/session boundary)
- Session historian structured rehydration
- Optional Context DNA session briefing endpoint

The resulting bundle can be:
- rendered as Markdown for Codex/manual continuation
- relayed to other fleet nodes via the local daemon (/message)
- written to .fleet-messages/ as a P7 git-safe fallback
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import socket
from typing import Optional
from urllib.error import URLError
from urllib.request import Request, urlopen

from .model_swap import ModelSwapCheckpoint, SwapCheckpointData


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _default_node_id() -> str:
    return os.environ.get("MULTIFLEET_NODE_ID") or socket.gethostname().split(".")[0].lower()


def _slugify(value: str, default: str = "handoff") -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or default


def _compact_text(text: str, limit: int) -> str:
    text = (text or "").strip()
    if not text or len(text) <= limit:
        return text
    return text[: max(0, limit - 20)].rstrip() + "\n[... truncated ...]"


def _redact_text(text: str) -> str:
    text = text or ""
    if not text:
        return ""

    try:
        from memory.secret_refs import get_secret_registry

        text = get_secret_registry().redact(text)
    except Exception:
        pass

    patterns = [
        (r"\bsk-[A-Za-z0-9_-]{12,}\b", "sk-REDACTED"),
        (r"\b(sk-ant|sk-proj|sk-live|sk-test)-[A-Za-z0-9_-]{8,}\b", r"\1-REDACTED"),
        (r"(?i)\b(Bearer)\s+[A-Za-z0-9._-]{12,}", r"\1 REDACTED"),
        (r"(?im)\b(OPENAI_API_KEY|ANTHROPIC_API_KEY|CONTEXT_DNA_OPENAI|FLEET_OPENAI_API_KEY|SUPERSET_API_KEY|AWS_SECRET_ACCESS_KEY|AWS_ACCESS_KEY_ID)\s*[:=]\s*['\"]?[^'\"\n ]+['\"]?", r"\1=REDACTED"),
        (r"(?im)\b(api[_-]?key|token|secret|password)\s*[:=]\s*['\"]?[^'\"\n ]+['\"]?", r"\1=REDACTED"),
    ]
    for pattern, repl in patterns:
        text = re.sub(pattern, repl, text)
    return text


@dataclass
class ContinuityBundle:
    """Serializable summary for cross-session / cross-node continuation."""

    created_at: str
    node_id: str
    reason: str
    source_model: str
    target_model: str
    checkpoint_id: str
    checkpoint_path: str
    session_id: str
    working_dir: str
    branch: str
    active_task: str
    claude_md_summary: str
    last_session_summary: str
    pending_critical_findings: int
    fleet_inbox_count: int
    session_rehydration: str
    session_briefing: str

    def to_dict(self) -> dict:
        return {
            "created_at": self.created_at,
            "node_id": self.node_id,
            "reason": self.reason,
            "source_model": self.source_model,
            "target_model": self.target_model,
            "checkpoint_id": self.checkpoint_id,
            "checkpoint_path": self.checkpoint_path,
            "session_id": self.session_id,
            "working_dir": self.working_dir,
            "branch": self.branch,
            "active_task": self.active_task,
            "claude_md_summary": self.claude_md_summary,
            "last_session_summary": self.last_session_summary,
            "pending_critical_findings": self.pending_critical_findings,
            "fleet_inbox_count": self.fleet_inbox_count,
            "session_rehydration": self.session_rehydration,
            "session_briefing": self.session_briefing,
        }


class ContinuityBridge:
    """Build and relay non-destructive handoff capsules."""

    def __init__(
        self,
        repo_root: str = ".",
        daemon_url: str = "http://127.0.0.1:8855",
        checkpoint_path: Optional[str] = None,
        node_id: Optional[str] = None,
    ):
        self.repo_root = Path(repo_root).resolve()
        self.daemon_url = daemon_url.rstrip("/")
        self.node_id = node_id or _default_node_id()
        self._model_swap = ModelSwapCheckpoint(
            repo_root=str(self.repo_root),
            checkpoint_path=checkpoint_path,
        )
        self._checkpoint_path = str(self._model_swap._checkpoint_path)

    def build_bundle(
        self,
        reason: str = "anthropic-limit-failover",
        source_model: str = "",
        target_model: str = "codex-gpt5.4",
        compact: bool = False,
    ) -> ContinuityBundle:
        """Capture current project/session state into a resumable bundle."""
        checkpoint = self._model_swap.capture(
            reason=reason,
            source_model=source_model,
            target_model=target_model,
        )
        rehydration = self._get_structured_rehydration(
            session_id=checkpoint.session_id or None,
            compact=compact,
        )
        briefing = self._get_session_briefing_summary()
        rehydration_limit = 2500 if compact else 8000

        return ContinuityBundle(
            created_at=_iso_now(),
            node_id=self.node_id,
            reason=reason,
            source_model=checkpoint.source_model,
            target_model=checkpoint.target_model,
            checkpoint_id=checkpoint.checkpoint_id,
            checkpoint_path=self._checkpoint_path,
            session_id=checkpoint.session_id,
            working_dir=checkpoint.working_dir,
            branch=checkpoint.branch,
            active_task=_compact_text(_redact_text(checkpoint.active_task), 600),
            claude_md_summary=_compact_text(_redact_text(checkpoint.claude_md_summary), 600),
            last_session_summary=_compact_text(_redact_text(checkpoint.last_session_summary), 900),
            pending_critical_findings=checkpoint.pending_critical_findings,
            fleet_inbox_count=checkpoint.fleet_inbox_count,
            session_rehydration=_compact_text(_redact_text(rehydration), rehydration_limit),
            session_briefing=_compact_text(_redact_text(briefing), 1200),
        )

    def render_markdown(
        self,
        bundle: ContinuityBundle,
        to: str = "codex",
        subject: Optional[str] = None,
        msg_type: str = "handoff",
    ) -> str:
        """Render a human-readable continuity handoff in fleet message format."""
        subject = subject or f"Continuity handoff: {bundle.reason}"
        parts = [
            "---",
            f"from: {bundle.node_id}",
            f"to: {to}",
            f"subject: {subject}",
            f"timestamp: {bundle.created_at}",
            f"type: {msg_type}",
            "priority: high",
            "---",
            "",
            "# Continuity Handoff",
            "",
            "## Resume Boundary",
            f"- Reason: {bundle.reason}",
            f"- Source model: {bundle.source_model or 'unknown'}",
            f"- Target model: {bundle.target_model or 'unknown'}",
            f"- Checkpoint: {bundle.checkpoint_id or 'none'}",
            f"- Session: {bundle.session_id or 'unknown'}",
            f"- Repo: {bundle.working_dir or str(self.repo_root)}",
            f"- Branch: {bundle.branch or 'unknown'}",
        ]

        if bundle.active_task:
            parts.extend([
                "",
                "## Active Task",
                _redact_text(bundle.active_task),
            ])

        if bundle.claude_md_summary or bundle.last_session_summary:
            parts.extend(["", "## Orientation"])
            if bundle.claude_md_summary:
                parts.append(f"- CLAUDE.md: {_redact_text(bundle.claude_md_summary)}")
            if bundle.last_session_summary:
                parts.append(f"- Last session summary: {_redact_text(bundle.last_session_summary)}")
            if bundle.pending_critical_findings:
                parts.append(f"- Pending critical findings: {bundle.pending_critical_findings}")
            if bundle.fleet_inbox_count:
                parts.append(f"- Fleet inbox count: {bundle.fleet_inbox_count}")

        if bundle.session_briefing:
            parts.extend([
                "",
                "## Session Briefing",
                _redact_text(bundle.session_briefing.strip()),
            ])

        if bundle.session_rehydration:
            parts.extend([
                "",
                "## Session Rehydration",
                _redact_text(bundle.session_rehydration.strip()),
            ])

        parts.extend([
            "",
            "## Safety Boundary",
            "- This handoff is additive only.",
            "- Claude Code session files were not rewritten.",
            "- Normal Claude Code hook injection can resume unchanged on the next VS Code prompt.",
            "",
            "## Deep Follow-Up",
            f"- Checkpoint file: `{bundle.checkpoint_path}`",
            "- Full archive: `PYTHONPATH=. .venv/bin/python3 memory/session_historian.py rehydrate --full`",
        ])
        return "\n".join(parts) + "\n"

    def render_alignment_broadcast(
        self,
        bundle: ContinuityBundle,
        to: str = "all",
        subject: str = "Continuity alignment rollout",
    ) -> str:
        """Render a compact, additive-only rollout note for all nodes."""
        active_task = _redact_text(bundle.active_task or "(no explicit task captured)")
        parts = [
            "---",
            f"from: {bundle.node_id}",
            f"to: {to}",
            f"subject: {subject}",
            f"timestamp: {bundle.created_at}",
            "type: context",
            "priority: high",
            "---",
            "",
            "# Continuity Alignment",
            "",
            "Additive rollout only. Do not rewrite Claude Code session JSONL, do not replace existing UserPromptSubmit hooks, and do not change local secrets handling.",
            "",
            "## Intent",
            f"- Source node: {bundle.node_id}",
            f"- Active task boundary: {active_task}",
            "- Each node may run Codex as a backup continuity agent when Claude/Anthropic limits hit.",
            "- Normal VS Code Claude Code use must resume unchanged on the next human prompt.",
            "",
            "## Required Checks",
            "- Pull latest `main` and inspect `multi-fleet/multifleet/continuity_bridge.py`.",
            "- Verify local fleet daemon health on `:8855` and existing Claude hook wiring.",
            "- Keep rollout additive: use checkpoints, session historian, and fleet messages; do not mutate Claude native session storage.",
            "- Confirm secrets remain out of git-tracked files and only flow through env/secure stores.",
            "",
            "## Scale Posture",
            "- Prefer P1 NATS / P2 HTTP for live relay; reserve P7 git for guaranteed async fallback.",
            "- Use compact alignment broadcasts for fleet-wide rollout; reserve full rehydration capsules for targeted failover/handoff only.",
            "",
            "## Operator Note",
            f"- Checkpoint: `{bundle.checkpoint_id}`",
            f"- Target model: `{bundle.target_model or 'unknown'}`",
        ]
        return "\n".join(parts) + "\n"

    def relay_bundle(
        self,
        bundle: ContinuityBundle,
        target: str,
        subject: Optional[str] = None,
        use_http: bool = True,
        write_git_fallback: bool = True,
    ) -> dict:
        """Relay a handoff bundle to another fleet node."""
        markdown = self.render_markdown(bundle, to=target, subject=subject)
        subject = subject or f"Continuity handoff: {bundle.reason}"

        if use_http:
            result = self._post_message(target, subject, markdown)
            if result.get("delivered"):
                return result

        if write_git_fallback:
            path = self.write_git_fallback(target, markdown, subject=subject)
            return {
                "delivered": True,
                "method": "p7_git_file",
                "target": target,
                "path": str(path),
            }

        return {
            "delivered": False,
            "target": target,
            "error": "http delivery failed and git fallback disabled",
        }

    def write_git_fallback(
        self,
        target: str,
        markdown: str,
        subject: str = "Continuity handoff",
    ) -> Path:
        """Write a P7 fleet message without committing/pushing."""
        msg_dir = self.repo_root / ".fleet-messages" / target
        msg_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        name = f"{ts}-{_slugify(subject)}-from-{self.node_id}.md"
        path = msg_dir / name
        path.write_text(markdown, encoding="utf-8")
        return path

    def _post_message(self, target: str, subject: str, body: str) -> dict:
        payload = {
            "type": "handoff",
            "to": target,
            "payload": {
                "subject": subject,
                "body": body,
            },
        }
        req = Request(
            f"{self.daemon_url}/message",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(req, timeout=5) as resp:
                raw = resp.read().decode("utf-8")
            parsed = json.loads(raw) if raw.strip() else {}
            if parsed.get("delivered"):
                parsed.setdefault("method", "p2_http")
                parsed.setdefault("target", target)
                return parsed
            return {
                "delivered": False,
                "target": target,
                "error": parsed.get("error", "daemon rejected message"),
            }
        except (URLError, TimeoutError, OSError) as exc:
            return {
                "delivered": False,
                "target": target,
                "error": str(exc),
            }
        except Exception as exc:
            return {
                "delivered": False,
                "target": target,
                "error": str(exc),
            }

    def _get_structured_rehydration(self, session_id: Optional[str], compact: bool) -> str:
        try:
            from memory.session_historian import SessionHistorian

            historian = SessionHistorian()
            text = historian.get_structured_rehydration(
                session_id=session_id,
                compact=compact,
            )
            return (text or "").strip()
        except Exception:
            return ""

    def _get_session_briefing_summary(self) -> str:
        req = Request("http://127.0.0.1:8080/contextdna/session-briefing", method="GET")
        try:
            with urlopen(req, timeout=3) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except Exception:
            return ""

        sections = payload.get("sections", {})
        last = sections.get("last_session", {})
        meta = sections.get("meta_analysis", {})
        failure = sections.get("failure_patterns", {})
        parts: list[str] = []

        threads = last.get("threads", [])
        if threads:
            main = threads[0]
            parts.append(
                f"Recent threads: {len(threads)}; main session {main.get('session_id', 'unknown')} "
                f"with {main.get('message_count', 0)} messages"
            )

        user_msgs = last.get("user_recent_messages", [])
        if user_msgs:
            preview = user_msgs[0].get("content", "").strip()
            if preview:
                parts.append(f"Latest user intent: {preview[:180]}")

        if meta.get("status"):
            parts.append(f"Meta-analysis: {meta.get('status')}")

        patterns = failure.get("patterns", [])
        if patterns:
            parts.append(f"Failure patterns: {patterns[0][:160]}")

        return "\n".join(parts)
