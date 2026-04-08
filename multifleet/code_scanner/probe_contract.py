"""Probe output contract -- normalized dataclasses for all probe findings.

Every probe in the GhostScan engine returns these types. Uniform output
enables: evidence ledger recording, EventBus emission, 3-surgeon gates,
IDE display, and fleet-wide aggregation.
"""

from dataclasses import dataclass, field
from typing import Literal
import time


@dataclass
class Finding:
    """Single actionable finding from a probe scan."""
    title: str
    summary: str
    severity: Literal["low", "medium", "high"]
    confidence: float  # 0.0-1.0
    blast_radius: Literal["low", "medium", "high"] = "low"
    evidence_refs: list[str] = field(default_factory=list)
    suggested_actions: list[str] = field(default_factory=list)
    file_path: str = ""
    line_number: int = 0

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "summary": self.summary,
            "severity": self.severity,
            "confidence": self.confidence,
            "blast_radius": self.blast_radius,
            "evidence_refs": self.evidence_refs,
            "suggested_actions": self.suggested_actions,
            "file_path": self.file_path,
            "line_number": self.line_number,
        }


@dataclass
class ProbeResult:
    """Normalized output from any probe execution."""
    probe_id: str
    status: Literal["ok", "warn", "error", "skipped"]
    confidence: float
    findings: list[Finding] = field(default_factory=list)
    duration_ms: float = 0.0
    missing_inputs: list[str] = field(default_factory=list)
    generated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "probe_id": self.probe_id,
            "status": self.status,
            "confidence": self.confidence,
            "findings": [f.to_dict() for f in self.findings],
            "duration_ms": round(self.duration_ms, 2),
            "missing_inputs": self.missing_inputs,
            "generated_at": self.generated_at,
        }

    @staticmethod
    def ok(probe_id: str, confidence: float = 0.9) -> "ProbeResult":
        return ProbeResult(probe_id=probe_id, status="ok", confidence=confidence)

    @staticmethod
    def error(probe_id: str, message: str) -> "ProbeResult":
        return ProbeResult(
            probe_id=probe_id, status="error", confidence=0.0,
            findings=[Finding(
                title=f"Probe error: {message}", summary=message,
                severity="low", confidence=0.0,
            )],
        )

    @staticmethod
    def skipped(probe_id: str, missing: list[str]) -> "ProbeResult":
        return ProbeResult(
            probe_id=probe_id, status="skipped", confidence=0.0,
            missing_inputs=missing,
        )


@dataclass
class ProbeContext:
    """Input context passed to every probe."""
    repo_path: str
    branch: str = ""
    changed_files: list[str] = field(default_factory=list)
    task_description: str = ""
    node_id: str = ""

    def to_dict(self) -> dict:
        return {
            "repo_path": self.repo_path,
            "branch": self.branch,
            "changed_files": self.changed_files,
            "task_description": self.task_description,
            "node_id": self.node_id,
        }
