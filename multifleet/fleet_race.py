"""Fleet Race — Competitive Branch Racing across fleet nodes.

One command triggers parallel work on the same problem across multiple
machines. Each node works in an isolated worktree. The fleet formally
proposes, rebuts, and converges to the best solution.

Usage:
    race = FleetRace(daemon_port=8855, node_id="mac2")
    result = race.start("Fix the auth flow", nodes=["mac1", "mac2", "mac3"])
    status = race.status(result["race_id"])
"""

import json
import logging
import time
import uuid
import urllib.request
import urllib.error
from typing import Optional

logger = logging.getLogger("fleet-nerve.race")


class FleetRace:
    """Orchestrates competitive branch racing across fleet nodes.

    Lifecycle:
        start() -> dispatched to all nodes -> racing -> first_complete ->
        cross_review -> synthesis -> done

    All state is in-memory (_races dict). Evidence is persisted to the
    EvidenceLedger via the daemon's /evidence endpoint.
    """

    def __init__(self, daemon_port: int = 8855, node_id: str = "unknown"):
        self._port = daemon_port
        self._node_id = node_id
        self._races: dict[str, dict] = {}  # race_id -> race state

    # ── Public API ──────────────────────────────────────────────────

    def start(
        self,
        task: str,
        nodes: list[str] | None = None,
        branch_prefix: str = "race",
        timeout_s: int = 600,
    ) -> dict:
        """Start a competitive branch race.

        Args:
            task: The task/problem description.
            nodes: Node IDs to participate (default: all discovered peers + self).
            branch_prefix: Git branch prefix for race worktrees.
            timeout_s: Max wall-clock seconds for the race (default 10 min).

        Returns:
            Race state dict with race_id, participants, status, timestamps.
        """
        race_id = str(uuid.uuid4())[:8]
        now = time.time()

        race: dict = {
            "race_id": race_id,
            "task": task,
            "status": "starting",
            "started_at": now,
            "timeout_s": timeout_s,
            "branch_prefix": branch_prefix,
            "participants": {},
            "completions": [],
            "proposals": [],
            "rebuttals": [],
            "synthesis": None,
            "winner": None,
            "error": None,
        }

        # 1. Discover nodes
        if not nodes:
            nodes = self._discover_nodes()
        if not nodes:
            race["status"] = "failed"
            race["error"] = "no nodes available"
            self._races[race_id] = race
            return race

        # 2. Broadcast race start to all participants
        self._broadcast_race_start(race_id, task, nodes, branch_prefix, timeout_s)

        # 3. Acquire lease + dispatch per node
        dispatched = 0
        for node in nodes:
            branch = f"{branch_prefix}/{race_id}/{node}"
            participant: dict = {
                "node": node,
                "branch": branch,
                "status": "pending",
                "dispatched_at": None,
                "completed_at": None,
                "lease_acquired": False,
                "result": None,
                "error": None,
            }

            # Acquire lease (prevents duplicate work on same resource)
            lease = self._acquire_lease(f"race-{race_id}-{node}")
            participant["lease_acquired"] = lease.get("acquired", False)
            if not participant["lease_acquired"]:
                participant["status"] = "lease_denied"
                participant["error"] = lease.get("reason", "lease unavailable")
                race["participants"][node] = participant
                continue

            # Dispatch task
            dispatch_ok = self._dispatch_to_node(node, task, branch, race_id)
            if dispatch_ok.get("sent") or dispatch_ok.get("dispatched"):
                participant["status"] = "dispatched"
                participant["dispatched_at"] = time.time()
                dispatched += 1
            else:
                participant["status"] = "dispatch_failed"
                participant["error"] = dispatch_ok.get("error", "dispatch failed")

            race["participants"][node] = participant

        race["status"] = "racing" if dispatched > 0 else "failed"
        if dispatched == 0:
            race["error"] = "no nodes accepted dispatch"
        self._races[race_id] = race

        # Record in evidence ledger
        self._record_evidence("race_started", {
            "race_id": race_id,
            "task": task,
            "nodes": nodes,
            "dispatched": dispatched,
            "total": len(nodes),
        })

        logger.info(
            "Race %s started: task=%r dispatched=%d/%d",
            race_id, task[:60], dispatched, len(nodes),
        )
        return race

    def submit_result(self, race_id: str, node: str, result: dict) -> dict:
        """Record a node's completion of the race task.

        Called when a node finishes its work. Triggers cross-review if
        this is the first completion.

        Args:
            race_id: Race identifier.
            node: Node that completed.
            result: Dict with branch, summary, findings, diff_stat, etc.

        Returns:
            Updated race state.
        """
        race = self._races.get(race_id)
        if not race:
            return {"error": f"unknown race: {race_id}"}

        if node not in race["participants"]:
            return {"error": f"node {node} not a participant in race {race_id}"}

        now = time.time()
        race["participants"][node]["status"] = "completed"
        race["participants"][node]["completed_at"] = now
        race["participants"][node]["result"] = result

        completion = {
            "node": node,
            "completed_at": now,
            "summary": result.get("summary", ""),
            "branch": race["participants"][node]["branch"],
        }
        race["completions"].append(completion)

        # Release lease
        self._release_lease(f"race-{race_id}-{node}")

        # Record evidence
        self._record_evidence("race_node_completed", {
            "race_id": race_id,
            "node": node,
            "elapsed_s": round(now - race["started_at"], 1),
            "summary": result.get("summary", "")[:200],
        })

        # Trigger cross-review on first completion
        if len(race["completions"]) == 1:
            race["status"] = "cross_review"
            self._trigger_cross_review(race)

        # Check if all nodes done
        all_done = all(
            p["status"] in ("completed", "dispatch_failed", "lease_denied", "timed_out")
            for p in race["participants"].values()
        )
        if all_done and race["status"] != "done":
            race["status"] = "synthesizing"
            self._run_synthesis(race)

        return race

    def status(self, race_id: str) -> dict:
        """Get current race status."""
        race = self._races.get(race_id)
        if not race:
            return {"error": f"unknown race: {race_id}"}

        # Check for timeout
        elapsed = time.time() - race["started_at"]
        if race["status"] in ("racing", "cross_review") and elapsed > race["timeout_s"]:
            race["status"] = "timed_out"
            for p in race["participants"].values():
                if p["status"] == "dispatched":
                    p["status"] = "timed_out"

        return race

    def list_races(self) -> list[dict]:
        """List all races with summary info."""
        return [
            {
                "race_id": r["race_id"],
                "task": r["task"][:80],
                "status": r["status"],
                "started_at": r["started_at"],
                "participants": len(r["participants"]),
                "completions": len(r["completions"]),
                "winner": r["winner"],
            }
            for r in self._races.values()
        ]

    # ── Node Discovery ──────────────────────────────────────────────

    def _discover_nodes(self) -> list[str]:
        """Discover available fleet nodes from daemon health endpoint."""
        try:
            data = self._daemon_get("/health")
            if "error" in data:
                logger.warning("Health probe failed: %s", data["error"])
                return [self._node_id]

            nodes = [self._node_id]

            # Extract peers from channel_health or peers section
            channel_health = data.get("channel_health", {})
            for peer in channel_health:
                if peer not in nodes:
                    nodes.append(peer)

            peers = data.get("peers", {})
            for peer in peers:
                if peer not in nodes:
                    nodes.append(peer)

            return nodes
        except Exception as e:
            logger.warning("Node discovery failed: %s", e)
            return [self._node_id]

    # ── Broadcast ───────────────────────────────────────────────────

    def _broadcast_race_start(
        self, race_id: str, task: str, nodes: list[str],
        branch_prefix: str, timeout_s: int,
    ):
        """Broadcast race_start packet to all participants."""
        payload = {
            "type": "race_start",
            "to": "all",
            "payload": {
                "subject": f"race-start:{race_id}",
                "body": json.dumps({
                    "race_id": race_id,
                    "task": task,
                    "branch_prefix": branch_prefix,
                    "participants": nodes,
                    "timeout_s": timeout_s,
                    "started_by": self._node_id,
                }),
            },
        }
        self._daemon_post("/message", payload)

    # ── Lease Management ────────────────────────────────────────────

    def _acquire_lease(self, resource_id: str) -> dict:
        """Acquire a lease for a race participant slot."""
        result = self._daemon_post("/lease", {
            "action": "acquire",
            "resource_id": resource_id,
            "lease_type": "long_task",
        })
        if "error" in result:
            logger.warning("Lease acquire failed for %s: %s", resource_id, result["error"])
            # Degrade gracefully — proceed without lease
            return {"acquired": True, "degraded": True, "reason": result["error"]}
        return result

    def _release_lease(self, resource_id: str) -> dict:
        """Release a lease after race completion."""
        return self._daemon_post("/lease", {
            "action": "release",
            "resource_id": resource_id,
        })

    # ── Task Dispatch ───────────────────────────────────────────────

    def _dispatch_to_node(
        self, node: str, task: str, branch: str, race_id: str,
    ) -> dict:
        """Dispatch the race task to a specific node.

        Tries Superset bridge first (create_workspace + start_agent_session),
        then falls back to fleet message dispatch.
        """
        # Try Superset bridge for isolated worktree + agent
        superset_result = self._try_superset_dispatch(node, task, branch, race_id)
        if superset_result and not superset_result.get("error"):
            return superset_result

        # Fallback: send task via fleet messaging
        race_prompt = (
            f"[FLEET RACE {race_id}] Work on branch: {branch}\n\n"
            f"Task: {task}\n\n"
            f"Instructions:\n"
            f"1. Create/checkout branch {branch}\n"
            f"2. Solve the task in an isolated worktree\n"
            f"3. Commit your solution\n"
            f"4. When done, send a fleet message with your summary and findings"
        )
        result = self._daemon_post("/message", {
            "type": "task",
            "to": node,
            "payload": {
                "subject": f"race-task:{race_id}",
                "body": race_prompt,
            },
        })
        return result

    def _try_superset_dispatch(
        self, node: str, task: str, branch: str, race_id: str,
    ) -> Optional[dict]:
        """Attempt dispatch via Superset MCP bridge."""
        try:
            from multifleet.superset_bridge import SupersetBridge
            bridge = SupersetBridge()
            if not bridge.available:
                return None

            # Create workspace for isolated work
            ws = bridge.call_tool("create_workspace", {
                "name": f"race-{race_id}-{node}",
                "description": f"Fleet race {race_id}: {task[:100]}",
            })

            # Start agent session in the workspace
            session = bridge.call_tool("start_agent_session", {
                "task": task,
                "workspace_id": ws.get("workspace_id", ""),
            })

            return {
                "dispatched": True,
                "via": "superset",
                "workspace_id": ws.get("workspace_id"),
                "session_id": session.get("session_id"),
            }
        except Exception as e:
            logger.debug("Superset dispatch unavailable: %s", e)
            return None

    # ── Cross-Review ────────────────────────────────────────────────

    def _trigger_cross_review(self, race: dict):
        """Send the first completer's proposal to other nodes for rebuttal."""
        if not race["completions"]:
            return

        first = race["completions"][0]
        first_result = race["participants"][first["node"]].get("result", {})

        # Create a proposal from the first completion
        proposal_body = (
            f"Race {race['race_id']} — first completion by {first['node']}\n\n"
            f"Branch: {first['branch']}\n"
            f"Summary: {first_result.get('summary', 'No summary')}\n\n"
            f"Findings:\n{first_result.get('findings', 'None reported')}\n\n"
            f"Review this approach and provide rebuttal or acceptance."
        )

        # Send as proposal for rebuttal
        proposal_payload = {
            "type": "proposal",
            "to": "all",
            "payload": {
                "subject": f"race-review:{race['race_id']}",
                "body": proposal_body,
                "scope": "general",
            },
        }
        self._daemon_post("/message", proposal_payload)

        race["proposals"].append({
            "from": first["node"],
            "branch": first["branch"],
            "submitted_at": time.time(),
        })

    # ── Synthesis ───────────────────────────────────────────────────

    def _run_synthesis(self, race: dict):
        """Synthesize results from all completed nodes.

        Produces a comparison of all approaches with recommendation.
        Tries chief relay for LLM-backed synthesis, falls back to
        structured comparison.
        """
        completed = [
            p for p in race["participants"].values()
            if p["status"] == "completed" and p["result"]
        ]

        if not completed:
            race["status"] = "done"
            race["synthesis"] = {"recommendation": "No completions to synthesize."}
            return

        if len(completed) == 1:
            sole = completed[0]
            race["winner"] = sole["node"]
            race["synthesis"] = {
                "recommendation": f"Single completion by {sole['node']}.",
                "branch": sole["branch"],
                "summary": sole["result"].get("summary", ""),
            }
            race["status"] = "done"
            self._record_evidence("race_completed", {
                "race_id": race["race_id"],
                "winner": race["winner"],
                "completions": len(completed),
                "elapsed_s": round(time.time() - race["started_at"], 1),
            })
            return

        # Multiple completions — build comparison
        approaches = []
        for p in completed:
            approaches.append({
                "node": p["node"],
                "branch": p["branch"],
                "summary": p["result"].get("summary", ""),
                "findings": p["result"].get("findings", ""),
                "elapsed_s": round(
                    (p["completed_at"] or time.time()) - race["started_at"], 1
                ),
            })

        # Pick winner: first to complete with a result (speed + completeness)
        winner = min(completed, key=lambda p: p["completed_at"] or float("inf"))

        race["winner"] = winner["node"]
        race["synthesis"] = {
            "recommendation": f"Winner: {winner['node']} (first complete).",
            "approaches": approaches,
            "winner_branch": winner["branch"],
            "dissent": [
                r for r in race["rebuttals"]
            ],
        }
        race["status"] = "done"

        self._record_evidence("race_completed", {
            "race_id": race["race_id"],
            "winner": race["winner"],
            "completions": len(completed),
            "approaches": len(approaches),
            "elapsed_s": round(time.time() - race["started_at"], 1),
        })

        logger.info(
            "Race %s completed: winner=%s completions=%d",
            race["race_id"], race["winner"], len(completed),
        )

    # ── Evidence Recording ──────────────────────────────────────────

    def _record_evidence(self, event_type: str, data: dict):
        """Record a race event in the evidence ledger via daemon."""
        try:
            self._daemon_post("/evidence", {
                "action": "record",
                "event_type": event_type,
                "node_id": self._node_id,
                "subject": f"{event_type}:{data.get('race_id', '?')}",
                "payload": json.dumps(data),
            })
        except Exception as e:
            logger.warning("Evidence recording failed: %s", e)

    # ── HTTP Helpers ────────────────────────────────────────────────

    def _daemon_get(self, path: str, timeout: int = 5) -> dict:
        """GET from fleet daemon."""
        try:
            url = f"http://127.0.0.1:{self._port}{path}"
            resp = urllib.request.urlopen(url, timeout=timeout)
            return json.loads(resp.read())
        except Exception as e:
            return {"error": str(e)}

    def _daemon_post(self, path: str, body: dict, timeout: int = 10) -> dict:
        """POST JSON to fleet daemon."""
        try:
            url = f"http://127.0.0.1:{self._port}{path}"
            data = json.dumps(body).encode()
            req = urllib.request.Request(
                url, data=data,
                headers={"Content-Type": "application/json"},
            )
            resp = urllib.request.urlopen(req, timeout=timeout)
            return json.loads(resp.read())
        except Exception as e:
            return {"error": str(e)}
