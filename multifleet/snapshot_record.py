"""SnapshotRecord — observed reality artifact for truth ladder snapshot layer.

When a task reaches SNAPSHOT_RECORD phase, this captures what IS true in code,
not what was hoped. Probes feed code truths. Docs feed doc truths. Runtime
checks feed runtime truths. Gaps and failures are recorded honestly.

The SnapshotRecord is the evidence bundle that justifies promoting a vision
entry to the snapshot layer of the truth ladder.

"Snapshot = what IS, not what was hoped." — GhostScan integration plan
"""

import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Optional

logger = logging.getLogger("fleet-nerve.snapshot-record")


@dataclass
class CodeTruth:
    """A verified fact about code state, typically from a probe."""
    file_path: str
    assertion: str  # What is true about this file/module
    probe_id: str = ""  # Which probe verified this
    confidence: float = 0.9
    evidence_hash: str = ""  # SHA of the file at verification time

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class DocTruth:
    """A verified fact about documentation state."""
    doc_path: str
    assertion: str
    matches_code: bool = True  # Does the doc match observed code reality?
    drift_summary: str = ""  # If not matching, what drifted

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class RuntimeTruth:
    """A verified fact about runtime behavior (tests, health checks)."""
    check_name: str
    passed: bool
    detail: str = ""
    measured_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SnapshotRecord:
    """Observed reality artifact — the evidence bundle for truth ladder promotion.

    Fields:
        code_truths: Verified facts about code state (from probes).
        doc_truths: Verified facts about documentation (from mirror_drift probe).
        runtime_truths: Verified facts about runtime behavior (tests, health).
        known_gaps: Things we know we don't know — honestly recorded.
        failed_expectations: Things we expected to be true but aren't.
        validated_outcomes: Done-criteria that were empirically validated.
    """

    record_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    task_id: str = ""
    created_at: float = field(default_factory=time.time)
    created_by: str = ""  # node_id

    code_truths: list[CodeTruth] = field(default_factory=list)
    doc_truths: list[DocTruth] = field(default_factory=list)
    runtime_truths: list[RuntimeTruth] = field(default_factory=list)
    known_gaps: list[str] = field(default_factory=list)
    failed_expectations: list[str] = field(default_factory=list)
    validated_outcomes: list[str] = field(default_factory=list)

    # Probe summary
    probe_count: int = 0
    finding_count: int = 0
    blocking_count: int = 0
    surgeon_intensity: str = "light"

    def to_dict(self) -> dict:
        return {
            "record_id": self.record_id,
            "task_id": self.task_id,
            "created_at": self.created_at,
            "created_by": self.created_by,
            "code_truths": [t.to_dict() for t in self.code_truths],
            "doc_truths": [t.to_dict() for t in self.doc_truths],
            "runtime_truths": [t.to_dict() for t in self.runtime_truths],
            "known_gaps": self.known_gaps,
            "failed_expectations": self.failed_expectations,
            "validated_outcomes": self.validated_outcomes,
            "probe_count": self.probe_count,
            "finding_count": self.finding_count,
            "blocking_count": self.blocking_count,
            "surgeon_intensity": self.surgeon_intensity,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)

    @classmethod
    def from_dict(cls, data: dict) -> "SnapshotRecord":
        """Reconstruct from dict (e.g., from JSON storage)."""
        record = cls(
            record_id=data.get("record_id", str(uuid.uuid4())),
            task_id=data.get("task_id", ""),
            created_at=data.get("created_at", time.time()),
            created_by=data.get("created_by", ""),
            known_gaps=data.get("known_gaps", []),
            failed_expectations=data.get("failed_expectations", []),
            validated_outcomes=data.get("validated_outcomes", []),
            probe_count=data.get("probe_count", 0),
            finding_count=data.get("finding_count", 0),
            blocking_count=data.get("blocking_count", 0),
            surgeon_intensity=data.get("surgeon_intensity", "light"),
        )
        for ct in data.get("code_truths", []):
            record.code_truths.append(CodeTruth(**ct))
        for dt in data.get("doc_truths", []):
            record.doc_truths.append(DocTruth(**dt))
        for rt in data.get("runtime_truths", []):
            record.runtime_truths.append(RuntimeTruth(**rt))
        return record

    def summary(self) -> str:
        """One-line summary for logging."""
        return (
            f"Snapshot {self.record_id[:8]}: "
            f"{len(self.code_truths)} code, "
            f"{len(self.doc_truths)} doc, "
            f"{len(self.runtime_truths)} runtime, "
            f"{len(self.known_gaps)} gaps, "
            f"{len(self.failed_expectations)} failures, "
            f"{len(self.validated_outcomes)} validated"
        )


class SnapshotBuilder:
    """Builds a SnapshotRecord from probe results and task context.

    Usage:
        builder = SnapshotBuilder(task_id="abc", node_id="mac1")
        builder.add_probe_results(probe_results)
        builder.add_runtime_check("tests_pass", True)
        builder.add_validated_outcome("auth works")
        record = builder.build()
    """

    def __init__(self, task_id: str, node_id: str = ""):
        self._task_id = task_id
        self._node_id = node_id
        self._code_truths: list[CodeTruth] = []
        self._doc_truths: list[DocTruth] = []
        self._runtime_truths: list[RuntimeTruth] = []
        self._known_gaps: list[str] = []
        self._failed_expectations: list[str] = []
        self._validated_outcomes: list[str] = []
        self._probe_count = 0
        self._finding_count = 0
        self._blocking_count = 0
        self._surgeon_intensity = "light"

    def add_probe_results(self, results: list) -> "SnapshotBuilder":
        """Extract truths from probe results (code_scanner ProbeResult objects or dicts)."""
        for result in results:
            self._probe_count += 1
            # Handle both code_scanner.ProbeResult and probe_engine.ProbeResult
            if hasattr(result, "findings"):
                findings = result.findings
                probe_id = result.probe_id
                status = result.status
            elif isinstance(result, dict):
                findings = result.get("findings", [])
                probe_id = result.get("probe_id", "unknown")
                status = result.get("status", "unknown")
            else:
                continue

            for finding in findings:
                self._finding_count += 1
                title = finding.title if hasattr(finding, "title") else finding.get("title", "")
                blast = finding.blast_radius if hasattr(finding, "blast_radius") else finding.get("blast_radius", "low")
                file_path = finding.file_path if hasattr(finding, "file_path") else finding.get("file_path", "")
                summary = finding.summary if hasattr(finding, "summary") else finding.get("summary", "")

                if blast == "high":
                    self._blocking_count += 1

                # Probes with OK status → code truths
                if status in ("ok", "warn"):
                    self._code_truths.append(CodeTruth(
                        file_path=file_path or "(global)",
                        assertion=title,
                        probe_id=probe_id,
                        confidence=finding.confidence if hasattr(finding, "confidence") else finding.get("confidence", 0.5),
                    ))
                # Mirror drift findings → doc truths
                if probe_id == "mirror_drift" and summary:
                    self._doc_truths.append(DocTruth(
                        doc_path=file_path,
                        assertion=title,
                        matches_code=blast != "high",
                        drift_summary=summary,
                    ))

        return self

    def add_runtime_check(self, check_name: str, passed: bool,
                          detail: str = "") -> "SnapshotBuilder":
        """Record a runtime truth (test result, health check, etc.)."""
        self._runtime_truths.append(RuntimeTruth(
            check_name=check_name, passed=passed, detail=detail,
        ))
        if not passed:
            self._failed_expectations.append(f"Runtime check '{check_name}' failed: {detail}")
        return self

    def add_validated_outcome(self, outcome: str) -> "SnapshotBuilder":
        """Record a done-criterion that was empirically validated."""
        self._validated_outcomes.append(outcome)
        return self

    def add_known_gap(self, gap: str) -> "SnapshotBuilder":
        """Record something we know we don't know."""
        self._known_gaps.append(gap)
        return self

    def add_failed_expectation(self, expectation: str) -> "SnapshotBuilder":
        """Record something we expected to be true but isn't."""
        self._failed_expectations.append(expectation)
        return self

    def set_surgeon_intensity(self, intensity: str) -> "SnapshotBuilder":
        """Set the required surgeon intensity from probe analysis."""
        self._surgeon_intensity = intensity
        return self

    def build(self) -> SnapshotRecord:
        """Assemble the SnapshotRecord."""
        record = SnapshotRecord(
            task_id=self._task_id,
            created_by=self._node_id,
            code_truths=self._code_truths,
            doc_truths=self._doc_truths,
            runtime_truths=self._runtime_truths,
            known_gaps=self._known_gaps,
            failed_expectations=self._failed_expectations,
            validated_outcomes=self._validated_outcomes,
            probe_count=self._probe_count,
            finding_count=self._finding_count,
            blocking_count=self._blocking_count,
            surgeon_intensity=self._surgeon_intensity,
        )
        logger.info("snapshot_built %s", record.summary())
        return record
