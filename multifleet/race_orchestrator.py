"""
Race Orchestrator — the glue that connects dispatch → collect → debate → decide.

3-surgeon review found: race.py, cross_rebuttal.py, and synthesis.py are
well-designed but disconnected. This module wires the pipeline end-to-end.

Usage:
    from multifleet.race_orchestrator import run_race
    result = run_race("Implement auth refresh", nodes=["mac1", "mac2", "mac3"])
    print(result["decision"])
"""

import json
import logging
import time
import urllib.request
import urllib.error
from typing import Optional

from multifleet.cross_rebuttal import RebuttalManager, RebuttalPhase
from multifleet.synthesis import ChiefSynthesis

logger = logging.getLogger("fleet-nerve.race-orchestrator")

DAEMON_URL = "http://127.0.0.1:8855"


def _daemon_get(endpoint: str) -> Optional[dict]:
    try:
        with urllib.request.urlopen(f"{DAEMON_URL}/{endpoint}", timeout=5) as r:
            return json.loads(r.read())
    except Exception:
        return None


def _daemon_post(endpoint: str, data: dict) -> Optional[dict]:
    try:
        req = urllib.request.Request(
            f"{DAEMON_URL}/{endpoint}",
            data=json.dumps(data).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except Exception as e:
        logger.warning("Daemon POST %s failed: %s", endpoint, e)
        return None


def run_race(
    prompt: str,
    nodes: list[str] = None,
    timeout_s: int = 600,
    poll_interval_s: int = 10,
    deadline_minutes: int = 30,
) -> dict:
    """Run a complete race: dispatch → collect → debate → synthesize.

    This is the orchestrator that wires RebuttalManager and ChiefSynthesis
    into a single blocking pipeline.

    Returns dict with: status, decision, winner, verdicts, critiques,
    synthesis, elapsed_s, rebuttal_session
    """
    if nodes is None:
        nodes = ["mac1", "mac2", "mac3"]

    t0 = time.time()
    logger.info("Race starting: %d nodes, timeout=%ds", len(nodes), timeout_s)

    # Initialize rebuttal session
    mgr = RebuttalManager()
    session = mgr.start_session({
        "assigned_to": nodes,
        "deadline_minutes": deadline_minutes,
    })
    task_id = session.task_id

    # Step 1: Advance to LOCAL_SURGERY and dispatch to all nodes
    mgr.advance_session(task_id)  # ASSIGNED -> LOCAL_SURGERY

    dispatched = {}
    for node in nodes:
        result = _daemon_post("message", {
            "type": "task",
            "to": node,
            "payload": {
                "subject": f"race-task:{task_id[:8]}",
                "body": prompt,
                "task_id": task_id,
                "timeout_s": timeout_s,
            },
        })
        dispatched[node] = result or {"error": "dispatch failed"}
        logger.info("Dispatched to %s: %s", node, "ok" if result else "failed")

    # Step 2: Collect verdicts (poll until all arrive or timeout)
    deadline = time.time() + timeout_s
    verdicts_received = 0

    while not session.all_verdicts_in() and time.time() < deadline:
        time.sleep(poll_interval_s)

        # Check daemon for verdict messages
        health = _daemon_get("health")
        if not health:
            continue

        # Check for incoming verdict packets (in production, the daemon
        # routes these via packet_registry; here we poll for completeness)
        for node in nodes:
            if node in session.verdicts:
                continue
            # Check if node has sent a verdict via fleet message
            # The daemon would route local_verdict packets here
            # For now, check if the node reports completion in health
            peer_info = health.get("peers", {}).get(node, {})
            if peer_info and peer_info.get("lastSeen", 999) < 30:
                # Peer is alive — check for task completion signals
                pass

        elapsed = time.time() - t0
        logger.info(
            "Polling: %d/%d verdicts (%.0fs elapsed, phase=%s)",
            len(session.verdicts), len(nodes), elapsed, session.phase.value,
        )

    # If we timed out with partial verdicts, advance anyway
    if session.verdicts and session.phase == RebuttalPhase.LOCAL_SURGERY:
        mgr.advance_session(task_id)  # -> VERDICTS_COLLECTED

    # Step 3: Cross-review phase (pairwise critiques)
    critiques = {}
    if len(session.verdicts) >= 2:
        mgr.advance_session(task_id)  # VERDICTS_COLLECTED -> UNDER_REVIEW

        # Generate pairwise critiques: each node reviews others' verdicts
        for reviewer_node in session.verdicts:
            other_verdicts = {
                k: v for k, v in session.verdicts.items() if k != reviewer_node
            }
            # Send critique request to each node
            _daemon_post("message", {
                "type": "task",
                "to": reviewer_node,
                "payload": {
                    "subject": f"race-critique:{task_id[:8]}",
                    "body": json.dumps({
                        "action": "critique",
                        "task_id": task_id,
                        "verdicts_to_review": other_verdicts,
                    }),
                },
            })
            logger.info("Critique request sent to %s", reviewer_node)

        # Poll for critiques (shorter timeout — critiques are faster)
        critique_deadline = time.time() + min(timeout_s / 3, 120)
        while not session.all_critiques_in() and time.time() < critique_deadline:
            time.sleep(poll_interval_s)
            logger.info(
                "Critiques: %d/%d collected",
                len(session.critiques), len(nodes),
            )

        critiques = dict(session.critiques)

        # Advance past critique collection
        if session.phase == RebuttalPhase.UNDER_REVIEW and session.critiques:
            mgr.advance_session(task_id)  # -> CRITIQUES_COLLECTED

    # Step 4: Chief Synthesis
    synthesis_result = None
    chief = ChiefSynthesis()

    if session.verdicts:
        synthesis_result = chief.synthesize(task_id, session.verdicts)
        chief_decision = synthesis_result.to_chief_decision()

        # Feed decision into rebuttal session
        mgr.handle_incoming_packet("chief_decision", {
            **chief_decision,
            "task_id": task_id,
        })

        logger.info(
            "Synthesis complete: decision=%s winner=%s confidence=%.3f",
            synthesis_result.decision,
            synthesis_result.winner_branch,
            synthesis_result.confidence,
        )

    elapsed = time.time() - t0

    result = {
        "task_id": task_id,
        "status": "completed" if len(session.verdicts) == len(nodes) else "partial",
        "nodes": nodes,
        "dispatched": len(dispatched),
        "verdicts_collected": len(session.verdicts),
        "verdicts": session.verdicts,
        "critiques": critiques,
        "rebuttal_phase": session.phase.value,
        "elapsed_s": round(elapsed, 1),
        "timeout_hit": time.time() >= deadline,
    }

    if synthesis_result:
        result["decision"] = synthesis_result.decision
        result["winner"] = synthesis_result.winner_branch
        result["confidence"] = synthesis_result.confidence
        result["reasoning"] = synthesis_result.reasoning
        result["cherry_pick_from"] = synthesis_result.cherry_pick_from
        result["dissent_preserved"] = synthesis_result.dissent_preserved
        result["followups"] = synthesis_result.followups
        result["synthesis"] = synthesis_result.to_chief_decision()
    else:
        result["decision"] = "needs_human"
        result["winner"] = None
        result["confidence"] = 0.0
        result["synthesis"] = None

    logger.info("Race complete: %s (%.1fs)", result["status"], elapsed)
    return result


def submit_verdict(task_id: str, node: str, verdict: dict) -> dict:
    """Submit a verdict for an active race.

    Called by nodes when they complete their race task.
    The verdict should contain: confidence, summary, branch, risks,
    strengths, dissent, findings, commit.
    """
    verdict["from"] = node
    verdict["machine"] = node
    verdict["task_id"] = task_id
    return _daemon_post("message", {
        "type": "context",
        "to": "chief",
        "payload": {
            "subject": f"race-verdict:{task_id[:8]}",
            "body": json.dumps(verdict),
            "packet_type": "local_verdict",
        },
    }) or {"error": "daemon unreachable"}


def force_complete(race_id: str = None) -> dict:
    """Force-complete a race with whatever verdicts are available."""
    logger.info("Force-completing race %s", race_id or "current")
    return {"action": "force_complete", "race_id": race_id, "status": "not_implemented_yet"}


if __name__ == "__main__":
    import sys
    prompt = sys.argv[1] if len(sys.argv) > 1 else "Test race: implement a hello world function"
    nodes = sys.argv[2].split(",") if len(sys.argv) > 2 else ["mac1", "mac2", "mac3"]

    print(f"Starting race: {prompt}")
    print(f"Nodes: {nodes}")
    result = run_race(prompt, nodes, timeout_s=120, poll_interval_s=5)
    print(json.dumps(result, indent=2, default=str))
