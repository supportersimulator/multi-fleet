"""Fleet Task Orchestrator — unified lifecycle from idea to wisdom.

Ties together: Supervisor (phases) + Fleet Race (parallel execution) +
Truth Ladder (promotion) + Acceptance Gate (verification) + Evidence
Ledger (audit trail) + Probes (code reality) into one workflow.

Lifecycle:
    1. Inbox capture   — truth ladder inbox entry
    2. Baseline probes — snapshot code reality before work
    3. Execute         — parallel (fleet_race) or local
    4. Review          — probes + acceptance gate
    5. Promote         — inbox -> vision -> snapshot through truth ladder
    6. Record          — evidence ledger audit trail

Usage:
    orch = TaskOrchestrator(daemon_port=8855, node_id="mac2")
    result = orch.run_task("Fix the auth flow",
                           done_criteria=["Tests pass"], parallel=True)
"""

import json
import logging
import time
import uuid
import urllib.request
import urllib.error
from typing import Optional

logger = logging.getLogger("fleet-nerve.orchestrator")


class TaskOrchestrator:
    """Drives a task through the entire fleet lifecycle in one call.

    All phases communicate via the daemon HTTP API so this works
    whether the daemon is local or remote. Each phase records its
    outcome in the task dict for full traceability.
    """

    def __init__(self, daemon_port: int = 8855, node_id: str = "unknown"):
        self._port = daemon_port
        self._node_id = node_id
        self._tasks: dict[str, dict] = {}

    # ── Public API ──────────────────────────────────────────────────

    def run_task(
        self,
        description: str,
        done_criteria: list[str],
        parallel: bool = False,
        nodes: list[str] | None = None,
        timeout_s: int = 600,
    ) -> dict:
        """Run the complete task lifecycle: idea -> wisdom.

        Args:
            description: What needs to be done.
            done_criteria: List of conditions that define "done".
            parallel: If True, use fleet_race across nodes.
            nodes: Target nodes for parallel execution.
            timeout_s: Max seconds for execution phase.

        Returns:
            Full task dict with phase results, timing, and status.
        """
        task_id = str(uuid.uuid4())[:8]
        task: dict = {
            "id": task_id,
            "description": description,
            "done_criteria": done_criteria,
            "status": "starting",
            "node_id": self._node_id,
            "parallel": parallel,
            "phases": {},
            "started_at": time.time(),
            "errors": [],
        }
        self._tasks[task_id] = task

        # Phase 1: Capture idea in truth ladder inbox
        self._phase_inbox(task)

        # Phase 2: Baseline probe scan
        self._phase_probe_baseline(task)

        # Phase 3: Execute (parallel via fleet_race or local supervisor)
        if parallel:
            self._phase_race(task, nodes, timeout_s)
        else:
            self._phase_local_execute(task, timeout_s)

        # Phase 4: Review — post-execution probes + acceptance gate
        self._phase_review(task)

        # Phase 5: Truth promotion (inbox -> vision, vision -> snapshot)
        self._phase_promote(task)

        # Phase 6: Record everything in evidence ledger
        self._phase_record(task)

        task["status"] = "complete" if not task["errors"] else "complete_with_errors"
        task["completed_at"] = time.time()
        task["elapsed_s"] = round(task["completed_at"] - task["started_at"], 2)

        logger.info(
            "orchestrator complete task=%s status=%s elapsed=%.1fs phases=%d errors=%d",
            task_id, task["status"], task["elapsed_s"],
            len(task["phases"]), len(task["errors"]),
        )
        return task

    def status(self, task_id: str) -> dict:
        """Get current state of a task."""
        return self._tasks.get(task_id, {"error": f"unknown task: {task_id}"})

    def list_tasks(self) -> list[dict]:
        """List all tasks with summary info."""
        return [
            {
                "id": t["id"],
                "description": t["description"][:80],
                "status": t["status"],
                "parallel": t.get("parallel", False),
                "elapsed_s": t.get("elapsed_s", round(time.time() - t["started_at"], 2)),
                "error_count": len(t.get("errors", [])),
            }
            for t in self._tasks.values()
        ]

    # ── Phase implementations ───────────────────────────────────────

    def _phase_inbox(self, task: dict):
        """Phase 1: Add idea to truth ladder inbox."""
        phase = {"name": "inbox", "started_at": time.time()}
        try:
            result = self._daemon_post("/truth", {
                "action": "add_inbox",
                "title": task["description"][:100],
                "content": task["description"],
                "metadata": {
                    "task_id": task["id"],
                    "done_criteria": task["done_criteria"],
                    "source": "orchestrator",
                },
            })
            phase["truth_id"] = result.get("id", result.get("entry_id", ""))
            phase["status"] = "ok"
            task["truth_id"] = phase["truth_id"]
        except Exception as e:
            phase["status"] = "error"
            phase["error"] = str(e)
            task["errors"].append(f"inbox: {e}")
        phase["elapsed_ms"] = _elapsed_ms(phase["started_at"])
        task["phases"]["inbox"] = phase
        task["status"] = "inbox_captured"

    def _phase_probe_baseline(self, task: dict):
        """Phase 2: Run probes to establish baseline code reality."""
        phase = {"name": "probe_baseline", "started_at": time.time()}
        try:
            result = self._daemon_post("/scan", {
                "trigger": "manual",
                "context": {"task_id": task["id"], "purpose": "baseline"},
            })
            phase["finding_count"] = len(result.get("findings", []))
            phase["summary"] = result.get("summary", {})
            phase["status"] = "ok"
            task["baseline_findings"] = phase["finding_count"]
        except Exception as e:
            phase["status"] = "error"
            phase["error"] = str(e)
            task["errors"].append(f"probe_baseline: {e}")
        phase["elapsed_ms"] = _elapsed_ms(phase["started_at"])
        task["phases"]["probe_baseline"] = phase
        task["status"] = "baseline_probed"

    def _phase_race(self, task: dict, nodes: list[str] | None, timeout_s: int):
        """Phase 3a: Execute via competitive fleet race."""
        phase = {"name": "race", "started_at": time.time()}
        try:
            body: dict = {"task": task["description"], "timeout_s": timeout_s}
            if nodes:
                body["nodes"] = nodes
            result = self._daemon_post("/race", body)
            phase["race_id"] = result.get("race_id", "")
            phase["participants"] = list(result.get("participants", {}).keys())
            phase["race_status"] = result.get("status", "unknown")
            phase["status"] = "ok"
            task["race_id"] = phase["race_id"]
        except Exception as e:
            phase["status"] = "error"
            phase["error"] = str(e)
            task["errors"].append(f"race: {e}")
        phase["elapsed_ms"] = _elapsed_ms(phase["started_at"])
        task["phases"]["execute"] = phase
        task["status"] = "executing"

    def _phase_local_execute(self, task: dict, timeout_s: int):
        """Phase 3b: Execute locally via supervisor."""
        phase = {"name": "local_execute", "started_at": time.time()}
        try:
            result = self._daemon_post("/supervisor", {
                "action": "start",
                "task_id": task["id"],
                "description": task["description"],
                "done_criteria": task["done_criteria"],
            })
            phase["supervisor_phase"] = result.get("phase", "unknown")
            phase["status"] = "ok"

            # Advance supervisor through execution
            advanced = self._daemon_post("/supervisor", {
                "action": "advance",
                "task_id": task["id"],
                "evidence": {"notes": "orchestrator advancing through execution"},
            })
            phase["advanced_to"] = advanced.get("phase", "unknown")
        except Exception as e:
            phase["status"] = "error"
            phase["error"] = str(e)
            task["errors"].append(f"local_execute: {e}")
        phase["elapsed_ms"] = _elapsed_ms(phase["started_at"])
        task["phases"]["execute"] = phase
        task["status"] = "executing"

    def _phase_review(self, task: dict):
        """Phase 4: Post-execution probes + acceptance gate."""
        phase = {"name": "review", "started_at": time.time()}

        # Run post-execution probes
        try:
            probe_result = self._daemon_post("/scan", {
                "trigger": "manual",
                "context": {"task_id": task["id"], "purpose": "review"},
            })
            phase["post_findings"] = len(probe_result.get("findings", []))
        except Exception as e:
            phase["post_findings"] = -1
            task["errors"].append(f"review_probes: {e}")

        # Run acceptance gate
        try:
            accept_result = self._daemon_post("/accept", {
                "task_id": task["id"],
                "branch": "",
                "context": {
                    "done_criteria": task["done_criteria"],
                    "description": task["description"],
                },
            })
            phase["accepted"] = accept_result.get("accepted", False)
            phase["acceptance_reason"] = accept_result.get("reason", "")
            phase["blocking_issues"] = accept_result.get("blocking_issues", [])
            phase["status"] = "accepted" if phase["accepted"] else "blocked"
            task["accepted"] = phase["accepted"]
        except Exception as e:
            phase["accepted"] = False
            phase["status"] = "error"
            phase["error"] = str(e)
            task["errors"].append(f"acceptance_gate: {e}")
            task["accepted"] = False

        phase["elapsed_ms"] = _elapsed_ms(phase["started_at"])
        task["phases"]["review"] = phase
        task["status"] = "reviewed"

    def _phase_promote(self, task: dict):
        """Phase 5: Promote through truth ladder (inbox -> vision -> snapshot)."""
        phase = {"name": "promote", "started_at": time.time(), "promotions": []}
        truth_id = task.get("truth_id", "")

        if not truth_id:
            phase["status"] = "skipped"
            phase["reason"] = "no truth_id from inbox phase"
            task["phases"]["promote"] = phase
            return

        # Promote inbox -> vision
        vision_id = self._try_promote(task, phase, truth_id, "vision")

        # Promote vision -> snapshot (only if review passed)
        if vision_id and task.get("accepted", False):
            self._try_promote(task, phase, vision_id, "snapshot")

        phase["status"] = "ok" if not task["errors"] or phase["promotions"] else "partial"
        phase["elapsed_ms"] = _elapsed_ms(phase["started_at"])
        task["phases"]["promote"] = phase
        task["status"] = "promoted"

    def _try_promote(self, task: dict, phase: dict, entry_id: str, target: str) -> str:
        """Attempt a single truth ladder promotion. Returns new ID or empty."""
        try:
            result = self._daemon_post("/truth", {
                "action": "promote",
                "entry_id": entry_id,
                "target_layer": target,
                "promoted_by": self._node_id,
                "evidence_refs": [task["id"]],
            })
            new_id = result.get("id", result.get("entry_id", ""))
            phase["promotions"].append({
                "from_id": entry_id,
                "to_layer": target,
                "new_id": new_id,
            })
            return new_id
        except Exception as e:
            task["errors"].append(f"promote_{target}: {e}")
            return ""

    def _phase_record(self, task: dict):
        """Phase 6: Record full lifecycle in evidence ledger."""
        phase = {"name": "record", "started_at": time.time()}
        try:
            result = self._daemon_post("/evidence", {
                "event_type": "task_orchestrated",
                "node_id": self._node_id,
                "subject": task["description"][:100],
                "payload": {
                    "task_id": task["id"],
                    "parallel": task.get("parallel", False),
                    "accepted": task.get("accepted", False),
                    "phases": list(task["phases"].keys()),
                    "error_count": len(task["errors"]),
                    "elapsed_s": round(time.time() - task["started_at"], 2),
                },
            })
            phase["entry_id"] = result.get("entry_id", "")
            phase["status"] = "ok"
        except Exception as e:
            phase["status"] = "error"
            phase["error"] = str(e)
            task["errors"].append(f"evidence: {e}")
        phase["elapsed_ms"] = _elapsed_ms(phase["started_at"])
        task["phases"]["record"] = phase
        task["status"] = "recorded"

    # ── HTTP helpers ────────────────────────────────────────────────

    def _daemon_post(self, path: str, body: dict, timeout: float = 10.0) -> dict:
        """POST JSON to daemon, return parsed response."""
        url = f"http://127.0.0.1:{self._port}{path}"
        data = json.dumps(body).encode()
        req = urllib.request.Request(
            url, data=data,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.URLError as exc:
            raise ConnectionError(f"daemon unreachable at {url}: {exc.reason}") from exc
        except Exception as exc:
            raise RuntimeError(f"daemon error at {url}: {exc}") from exc

    def _daemon_get(self, path: str, timeout: float = 5.0) -> dict:
        """GET from daemon, return parsed response."""
        url = f"http://127.0.0.1:{self._port}{path}"
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.URLError as exc:
            raise ConnectionError(f"daemon unreachable at {url}: {exc.reason}") from exc
        except Exception as exc:
            raise RuntimeError(f"daemon error at {url}: {exc}") from exc


def _elapsed_ms(start: float) -> float:
    """Milliseconds since start."""
    return round((time.time() - start) * 1000, 1)
