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

# Optional theatrical output — never required for race logic
try:
    from multifleet.theatrical import FleetTheater
    _theater = FleetTheater()
except ImportError:
    _theater = None


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
        run_probes: bool = True,
    ) -> dict:
        """Start a competitive branch race with evidence-bearing dispatch.

        Args:
            task: The task/problem description.
            nodes: Node IDs to participate (default: all discovered peers + self).
            branch_prefix: Git branch prefix for race worktrees.
            timeout_s: Max wall-clock seconds for the race (default 10 min).

        Returns:
            Race state dict with race_id, participants, status, timestamps.
        """
        # Wire 5: Check superpowers workflow phase before dispatching
        try:
            from multifleet.superpowers_adapter import get_adapter
            adapter = get_adapter()
            can_dispatch, reason = adapter.can_dispatch_race()
            if not can_dispatch:
                logger.info("Race dispatch blocked: %s", reason)
                adapter.queue_race({"task": task, "nodes": nodes,
                                    "branch_prefix": branch_prefix,
                                    "timeout_s": timeout_s})
                return {
                    "race_id": None,
                    "status": "blocked_by_workflow",
                    "reason": reason,
                    "queued": True,
                }
        except ImportError:
            pass  # Superpowers adapter not available — proceed normally

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
            "epistemic_packet": None,
        }

        # 0. Build EpistemicPacket (evidence-bearing dispatch)
        if run_probes:
            try:
                from multifleet.epistemic_packet import EpistemicPacketBuilder
                builder = EpistemicPacketBuilder(node_id=self._node_id)
                packet = builder.build(task_intent=task)
                race["epistemic_packet"] = packet.to_dict()
                race["surgeon_intensity"] = packet.required_surgeon_intensity
                logger.info("Race %s: %s", race_id, packet.summary())
            except Exception as e:
                logger.warning("EpistemicPacket build failed: %s", e)

        # 0b. Ghost anticipate — predict session needs at race start
        try:
            from multifleet.ghost_anticipate import GhostAnticipate
            anticipator = GhostAnticipate(repo_root=".", node_id=self._node_id)
            anticipation = anticipator.anticipate()
            race["anticipation"] = anticipation.to_dict()
            logger.info("Race %s anticipation: %s", race_id, anticipation.summary())
        except Exception as e:
            logger.debug("Ghost anticipate failed (non-blocking): %s", e)

        # 1. Discover nodes
        if not nodes:
            nodes = self._discover_nodes()
        if not nodes:
            race["status"] = "failed"
            race["error"] = "no nodes available"
            self._races[race_id] = race
            return race

        # Theatrical: show race banner
        if _theater:
            try:
                _theater.race_banner(task, nodes)
            except Exception:
                pass  # Theatrical output must never break race logic

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

        # Theatrical: show initial scoreboard
        if _theater:
            try:
                _theater.race_scoreboard(race)
            except Exception:
                pass

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
        """Discover available fleet nodes.

        Primary: Superset list_devices() — sees all org devices.
        Fallback: daemon /health channel_health + peers.
        """
        nodes = [self._node_id]

        # Primary: Superset device registry
        try:
            from multifleet.superset_bridge import SupersetBridge
            bridge = SupersetBridge()
            raw = bridge.list_devices()
            # MCP responses wrap data: [{content, structuredContent}]
            # Extract actual device list from structuredContent or content text
            device_list = []
            for item in (raw if isinstance(raw, list) else [raw]):
                if isinstance(item, dict):
                    sc = item.get("structuredContent", {})
                    if isinstance(sc, dict) and "devices" in sc:
                        device_list.extend(sc["devices"])
                    elif "name" in item or "id" in item:
                        device_list.append(item)
            if device_list:
                for dev in device_list:
                    name = dev.get("name") or dev.get("id") or dev.get("deviceId", "")
                    if name and name not in nodes:
                        nodes.append(name)
                if len(nodes) > 1:
                    logger.info(
                        "Superset discovery found %d nodes: %s",
                        len(nodes), nodes,
                    )
                    return nodes
        except Exception as e:
            logger.debug("Superset discovery unavailable: %s", e)

        # Fallback: daemon health endpoint
        try:
            data = self._daemon_get("/health")
            if "error" in data:
                logger.warning("Health probe failed: %s", data["error"])
                return nodes

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
            return nodes

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
        """Attempt dispatch via Superset MCP bridge.

        Flow:
            1. Check bridge availability (has API key)
            2. Resolve project_id on the target device
            3. create_workspace(project_id, branch) on target node
            4. launch_agent_with_prompt(workspace_id, race prompt)
            5. Return dispatch result or None to trigger fallback
        """
        try:
            from multifleet.superset_bridge import SupersetBridge
            bridge = SupersetBridge()
            if not bridge._api_key:
                return None

            # Resolve the project on the target device so we can create a workspace
            raw_projects = bridge.list_projects(device_id=node)
            project_id = self._extract_project_id(raw_projects)
            if not project_id:
                logger.debug("Superset: no usable project on %s, skipping", node)
                return None

            # Create isolated worktree workspace on the target device
            ws = bridge.create_workspace(
                project_id=project_id,
                branch=branch,
                base_branch="main",
                device_id=node,
            )
            if isinstance(ws, dict) and ws.get("_available") is False:
                logger.debug("Superset create_workspace failed on %s: %s", node, ws.get("error"))
                return None

            # Extract workspace ID from MCP response
            workspace_id = self._extract_field(ws, "workspaceId", "workspace_id")
            if not workspace_id:
                logger.warning("Superset workspace created but no ID returned for %s", node)
                return None

            # Launch agent with the race prompt in the new workspace
            race_prompt = (
                f"[FLEET RACE {race_id}] Work on branch: {branch}\n\n"
                f"Task: {task}\n\n"
                f"Instructions:\n"
                f"1. You are in an isolated worktree on branch {branch}\n"
                f"2. Solve the task completely\n"
                f"3. Commit your solution\n"
                f"4. When done, send a fleet message with your summary and findings"
            )
            session = bridge.launch_agent_with_prompt(
                workspace_id=workspace_id,
                prompt=race_prompt,
                device_id=node,
            )
            if isinstance(session, dict) and session.get("_available") is False:
                logger.warning("Superset agent launch failed on %s: %s", node, session.get("error"))
                # Workspace was created but agent failed — clean up
                bridge.delete_workspace(workspace_id, device_id=node)
                return None

            session_id = self._extract_field(session, "sessionId", "session_id")
            logger.info(
                "Superset dispatch OK: node=%s workspace=%s session=%s",
                node, workspace_id, session_id or "?",
            )
            return {
                "dispatched": True,
                "via": "superset",
                "workspace_id": workspace_id,
                "session_id": session_id,
            }
        except ImportError:
            logger.debug("superset_bridge not importable")
            return None
        except Exception as e:
            logger.debug("Superset dispatch unavailable: %s", e)
            return None

    # ── MCP Response Helpers ──────────────────────────────────────────

    @staticmethod
    def _extract_project_id(raw_projects: list) -> Optional[str]:
        """Extract first usable project ID from MCP list_projects response.

        MCP tools return [{content: [...], structuredContent: {...}}].
        We need the project id from structuredContent or direct fields.
        """
        for item in (raw_projects if isinstance(raw_projects, list) else [raw_projects]):
            if not isinstance(item, dict):
                continue
            # Check for error responses
            if item.get("isError"):
                return None
            # structuredContent path
            sc = item.get("structuredContent", {})
            if isinstance(sc, dict):
                projects = sc.get("projects", [])
                if projects and isinstance(projects, list):
                    p = projects[0]
                    pid = p.get("id") or p.get("projectId", "")
                    if pid:
                        return pid
            # Direct field path (non-MCP wrapped)
            pid = item.get("id") or item.get("projectId", "")
            if pid:
                return pid
        return None

    @staticmethod
    def _extract_field(response: dict, *field_names: str) -> Optional[str]:
        """Extract a field from an MCP tool response, checking structuredContent first."""
        if not isinstance(response, dict):
            return None
        # Check structuredContent first
        sc = response.get("structuredContent", {})
        if isinstance(sc, dict):
            for name in field_names:
                val = sc.get(name)
                if val:
                    return str(val)
        # Check top-level fields
        for name in field_names:
            val = response.get(name)
            if val:
                return str(val)
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

        Uses ChiefSynthesis engine for evidence-backed decisions with
        confidence scoring, cherry-pick suggestions, and preserved dissent.
        Falls back to structured comparison if synthesis module unavailable.
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
            self._run_acceptance_gate(race)
            if race.get("status") != "blocked":
                race["status"] = "done"
            self._record_evidence("race_completed", {
                "race_id": race["race_id"],
                "winner": race["winner"],
                "completions": len(completed),
                "elapsed_s": round(time.time() - race["started_at"], 1),
            })
            # Wire 2: push race evidence to ContextDNA
            self._push_to_contextdna(race["race_id"], race.get("synthesis"))
            return

        # Multiple completions — build verdict dict for ChiefSynthesis
        verdicts: dict[str, dict] = {}
        for p in completed:
            result = p["result"] or {}
            verdicts[p["node"]] = {
                "branch": p["branch"],
                "confidence": float(result.get("confidence", 0.5)),
                "summary": result.get("summary", ""),
                "findings": result.get("findings", ""),
                "risks": result.get("risks", []),
                "strengths": result.get("strengths", []),
                "dissent": result.get("dissent", []),
                "commit": result.get("commit", p["branch"]),
                "followups": result.get("followups", []),
                "elapsed_s": round(
                    (p["completed_at"] or time.time()) - race["started_at"], 1
                ),
            }

        # Try ChiefSynthesis engine (evidence-backed, pluggable analyzers)
        try:
            from multifleet.synthesis import ChiefSynthesis
            chief = ChiefSynthesis()
            synthesis_result = chief.synthesize(race["race_id"], verdicts)

            race["winner"] = synthesis_result.winner_branch
            race["synthesis"] = {
                "engine": "chief_synthesis",
                "decision": synthesis_result.decision,
                "winner_branch": synthesis_result.winner_branch,
                "confidence": synthesis_result.confidence,
                "reasoning": synthesis_result.reasoning,
                "cherry_pick_from": synthesis_result.cherry_pick_from,
                "dissent_preserved": synthesis_result.dissent_preserved,
                "followups": synthesis_result.followups,
                "chief_decision_packet": synthesis_result.to_chief_decision(),
            }

            # Feed decision back to rebuttal manager if active
            self._finalize_rebuttal(race, synthesis_result.to_chief_decision())

            logger.info(
                "Race %s chief synthesis: decision=%s winner=%s confidence=%.3f",
                race["race_id"], synthesis_result.decision,
                synthesis_result.winner_branch, synthesis_result.confidence,
            )
        except (ImportError, Exception) as e:
            logger.warning("ChiefSynthesis unavailable, using fallback: %s", e)
            # Fallback: first to complete wins
            winner = min(completed, key=lambda p: p["completed_at"] or float("inf"))
            race["winner"] = winner["node"]
            race["synthesis"] = {
                "engine": "fallback",
                "recommendation": f"Winner: {winner['node']} (first complete).",
                "approaches": [
                    {"node": p["node"], "branch": p["branch"],
                     "summary": (p["result"] or {}).get("summary", "")}
                    for p in completed
                ],
                "winner_branch": winner["branch"],
                "dissent": list(race["rebuttals"]),
            }

        # Run acceptance gate before marking done
        self._run_acceptance_gate(race)
        if race.get("status") != "blocked":
            race["status"] = "done"

        self._record_evidence("race_completed", {
            "race_id": race["race_id"],
            "winner": race["winner"],
            "completions": len(completed),
            "engine": race["synthesis"].get("engine", "unknown"),
            "confidence": race["synthesis"].get("confidence", 0.0),
            "accepted": race.get("acceptance", {}).get("accepted", True),
            "elapsed_s": round(time.time() - race["started_at"], 1),
        })
        # Wire 2: push race evidence to ContextDNA
        self._push_to_contextdna(race["race_id"], race.get("synthesis"))

        # Arbiter escalation: low confidence or unresolved dissent -> human review
        synthesis = race.get("synthesis", {})
        synthesis_confidence = synthesis.get("confidence", 1.0)
        unresolved_dissent = synthesis.get("dissent_preserved", [])
        if synthesis_confidence < 0.8 or unresolved_dissent:
            try:
                from multifleet.arbiter_view import create_pending_from_race
                decision_id = create_pending_from_race(race)
                if decision_id:
                    race["status"] = "awaiting_arbiter"
                    race["arbiter_decision_id"] = decision_id
                    # Emit EventBus event for dashboard notification
                    self._emit_event("fleet.arbiter.pending", {
                        "decision_id": decision_id,
                        "race_id": race["race_id"],
                        "task": race["task"],
                        "confidence": synthesis_confidence,
                        "dissent_count": len(unresolved_dissent or []),
                    })
                    logger.info(
                        "Race %s escalated to arbiter: confidence=%.2f dissent=%d decision=%s",
                        race["race_id"], synthesis_confidence,
                        len(unresolved_dissent or []), decision_id,
                    )
            except Exception as e:
                logger.warning("Arbiter escalation failed: %s", e)

        # Ghost digest: auto-trigger after synthesis (3-surgeon recommendation)
        self._run_ghost_digest(race)

        # Theatrical: announce winner
        if _theater and race.get("winner"):
            try:
                synth = race.get("synthesis", {})
                reason = synth.get("decision", synth.get("recommendation", "First to complete"))
                evidence_items = synth.get("cherry_pick_from", [])
                if synth.get("reasoning"):
                    evidence_items = [synth["reasoning"]] + evidence_items
                _theater.race_winner(race["winner"], reason, evidence_items[:5])
                _theater.race_scoreboard(race)
            except Exception:
                pass

        logger.info(
            "Race %s completed: winner=%s completions=%d",
            race["race_id"], race["winner"], len(completed),
        )

    def _finalize_rebuttal(self, race: dict, chief_decision: dict):
        """Feed synthesis decision into active rebuttal session if one exists."""
        try:
            from multifleet.cross_rebuttal import RebuttalManager
            mgr = RebuttalManager()
            session = mgr.get_session(race["race_id"])
            if session:
                result = mgr.handle_incoming_packet("chief_decision", {
                    **chief_decision,
                    "task_id": race["race_id"],
                })
                logger.info("Rebuttal finalized for race %s: %s",
                            race["race_id"], result.get("action", "?"))
        except (ImportError, Exception) as e:
            logger.debug("Rebuttal finalization skipped: %s", e)

    def _run_acceptance_gate(self, race: dict) -> dict:
        """Run acceptance gate on race results — the corrigibility check.

        Returns AcceptanceDecision dict. If gate fails, race status
        stays at 'blocked' instead of 'done'.
        """
        try:
            from multifleet.acceptance_gate import AcceptanceGate

            synthesis = race.get("synthesis", {})
            packet = race.get("epistemic_packet", {})
            policy = (packet or {}).get("acceptance_policy", "standard")

            # Constitutional validation — wire real invariant checks
            invariants_ok = True
            try:
                from multifleet.constitution import validate_before_action
                gate_ctx = {
                    "action": f"accept_race_{race['race_id']}",
                    "has_checkpoint": True,  # race branches are checkpoints
                    "destructive": False,
                    "evidence_count": 1 if synthesis.get("reasoning") else 0,
                    "confidence": synthesis.get("confidence", 0.5),
                    "scope": "minimal",
                    "node_id": self._node_id,
                }
                constitution_result = validate_before_action("accept_race", gate_ctx)
                invariants_ok = constitution_result.passed
                if constitution_result.advisories:
                    for adv in constitution_result.advisories:
                        logger.warning(
                            "Race %s constitution advisory [%s]: %s",
                            race["race_id"], adv.principle_id, adv.message,
                        )
                if not invariants_ok:
                    logger.error(
                        "Race %s constitution BLOCKED: %s",
                        race["race_id"], constitution_result.summary,
                    )
            except Exception as e:
                logger.warning("Constitution check failed, degrading to pass: %s", e)
                invariants_ok = True

            gate = AcceptanceGate(strict=(policy == "strict"))
            decision = gate.evaluate(
                probe_results=(packet or {}).get("probe_results", []),
                surgeon_resolved=synthesis.get("engine") == "chief_synthesis",
                surgeon_intensity=(packet or {}).get("required_surgeon_intensity", "light"),
                surgeon_dissent_count=len(synthesis.get("dissent_preserved", [])),
                invariants_satisfied=invariants_ok,
                evidence_count=1 if synthesis.get("reasoning") else 0,
                epistemic_packet=packet,
            )

            race["acceptance"] = decision.to_dict()
            if not decision.accepted:
                race["status"] = "blocked"
                logger.warning(
                    "Race %s BLOCKED by acceptance gate: %s",
                    race["race_id"], decision.reason,
                )
            else:
                logger.info("Race %s ACCEPTED: %s", race["race_id"], decision.reason)

            self._record_evidence("acceptance_decision", {
                "race_id": race["race_id"],
                "accepted": decision.accepted,
                "reason": decision.reason,
                "blocking_issues": decision.blocking_issues,
            })

            return decision.to_dict()
        except (ImportError, Exception) as e:
            logger.warning("Acceptance gate unavailable: %s", e)
            race["acceptance"] = {"accepted": True, "reason": "gate unavailable (degraded)"}
            return race["acceptance"]

    def _run_ghost_digest(self, race: dict):
        """Auto-trigger ghost digest after synthesis (3-surgeon recommendation).

        Converts race results into evidence candidates for the ContextDNA
        promotion pipeline. This is where "work happened" becomes "the system learned."
        """
        try:
            from multifleet.ghost_digest import GhostDigest
            digest = GhostDigest(node_id=self._node_id)
            result = digest.digest(
                task_id=race["race_id"],
                task_intent=race.get("task", ""),
                synthesis_result=race.get("synthesis"),
                probe_results=(race.get("epistemic_packet") or {}).get("probe_results", []),
                acceptance_decision=race.get("acceptance"),
            )
            race["digest"] = result.to_dict()
            logger.info("Ghost digest for race %s: %s", race["race_id"], result.summary())
        except (ImportError, Exception) as e:
            logger.debug("Ghost digest skipped: %s", e)

    # ── ContextDNA Bridge (Wire 2) ────────────────────────────────

    def _push_to_contextdna(self, task_id: str, result: dict = None):
        """Push race evidence to ContextDNA via bidirectional bridge.

        Non-critical: failure here must never block race logic.
        """
        try:
            from multifleet.contextdna_evidence_bridge import ContextDNAEvidenceBridge
            bridge = ContextDNAEvidenceBridge()
            bridge.on_race_completed(task_id, result or {})
        except Exception as e:
            logger.debug("ContextDNA bridge push failed (non-critical): %s", e)

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

    # Map ad-hoc string event types to typed EventType enum values
    _FLEET_EVENT_MAP: dict = {}

    @classmethod
    def _get_event_type_map(cls):
        if not cls._FLEET_EVENT_MAP:
            from multifleet.event_schema import EventType as ET
            cls._FLEET_EVENT_MAP = {
                "fleet.arbiter.pending": ET.RACE_ARBITER_PENDING,
                "race.started": ET.RACE_STARTED,
                "race.completed": ET.RACE_COMPLETED,
                "race.node.finished": ET.RACE_NODE_FINISHED,
            }
        return cls._FLEET_EVENT_MAP

    def _emit_event(self, event_type: str, payload: dict):
        """Emit an event to the EventBus (best-effort, never blocks race logic).

        Prefers typed EventEmitter path; falls back to FleetEventBus for
        unrecognized event types.
        """
        # Try typed emit
        try:
            from multifleet.event_emitter import emit_typed
            from multifleet.event_schema import EventSource
            type_map = self._get_event_type_map()
            et = type_map.get(event_type)
            if et is not None:
                emit_typed(et, EventSource.MULTI_FLEET, payload=payload)
                return
        except (ImportError, Exception):
            pass
        # Fallback — use unified IDEEventBus
        try:
            from multifleet.event_bus import IDEEventBus
            IDEEventBus.get().emit(f"fleet.{event_type}", payload, source=f"fleet-nerve:{self._node_id}")
        except Exception as e:
            logger.debug("EventBus emit failed (non-critical): %s", e)

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
