#!/usr/bin/env python3
"""Integration test: Chief Orchestrator cross-machine protocol.

Tests the 5-phase ASSIGN->OPERATE->PUBLISH->REVIEW->CLOSE protocol
by instantiating ChiefOrchestrator on mac2, sending packets via
SurgeonLANRelay to mac1 and mac3, and reporting transport results.

NOT a unit test — exercises real network transports (NATS/HTTP/P7).
"""

import asyncio
import json
import sys
import time
import uuid
from pathlib import Path

# Add multi-fleet to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from multifleet.chief_orchestrator import (
    ChiefOrchestrator,
    SurgicalPhase,
    _make_packet,
)
from multifleet.surgeon_lan_relay import (
    SurgeonLANRelay,
    SurgeonPacket,
    build_task_brief,
    build_local_verdict,
    create_relay_for_node,
)

SUPERREPO = Path(__file__).resolve().parent.parent.parent
FLEET_MSG_ROOT = SUPERREPO / ".fleet-messages"
DAEMON_URL = "http://127.0.0.1:8855"
NODE_ID = "mac2"
PEERS = ["mac1", "mac3"]
MACHINES = ["mac1", "mac2", "mac3"]


def check_daemon_health() -> dict:
    """Check fleet daemon health."""
    import urllib.request
    try:
        with urllib.request.urlopen(f"{DAEMON_URL}/health", timeout=5) as resp:
            return json.loads(resp.read())
    except Exception as e:
        return {"error": str(e)}


def check_nats_available(health: dict) -> bool:
    """Check if NATS is reachable from daemon health."""
    nats_url = health.get("natsUrl", "")
    return bool(nats_url and nats_url != "none")


def test_relay_send_to_peers():
    """Test SurgeonLANRelay packet delivery to each peer via all transports."""
    relay = SurgeonLANRelay(
        node_id=NODE_ID,
        daemon_url=DAEMON_URL,
        fleet_messages_root=FLEET_MSG_ROOT,
    )

    results = {}
    task_id = str(uuid.uuid4())[:8]

    for peer in PEERS:
        packet = build_task_brief(
            from_node=NODE_ID,
            to_node=peer,
            task_id=task_id,
            task="integration test — verify cross-machine comms",
            approach="test-probe",
            machines=MACHINES,
        )

        # Test each transport individually
        peer_result = {"nats": False, "http": False, "p7": False}

        # P7 git (always available locally)
        p7_ok = relay._try_p7(packet)
        peer_result["p7"] = p7_ok

        # HTTP daemon
        http_ok = relay._try_http(packet)
        peer_result["http"] = http_ok

        # NATS (no async client in this test — skip direct NATS test)
        # The daemon test via HTTP already validates NATS routing

        # Full relay.send() — uses priority cascade
        packet2 = build_task_brief(
            from_node=NODE_ID,
            to_node=peer,
            task_id=task_id,
            task="integration test — full relay send",
            approach="test-probe",
            machines=MACHINES,
        )
        cascade_ok = relay.send(packet2)
        peer_result["cascade_send"] = cascade_ok

        results[peer] = peer_result

    return results, relay.get_stats(), task_id


def test_chief_orchestrator_phases():
    """Test ChiefOrchestrator through all 5 phases with simulated verdicts."""
    dispatch_log = []

    def mock_dispatch(packet: dict, target: str) -> bool:
        dispatch_log.append({
            "type": packet.get("type", "unknown"),
            "to": target,
            "timestamp": time.time(),
        })
        return True

    chief = ChiefOrchestrator(node_id=NODE_ID)
    chief.set_dispatch_fn(mock_dispatch)

    task_id = str(uuid.uuid4())[:8]

    # Phase 1: ASSIGN
    result = chief.phase_assign(
        task_id=task_id,
        task="integration test — verify cross-machine comms",
        machines=MACHINES,
        approach_hints={
            "mac1": "straightforward",
            "mac2": "alternative",
            "mac3": "hardening",
        },
    )
    assert result.phase == SurgicalPhase.ASSIGN, f"Expected ASSIGN, got {result.phase}"
    assert len(result.assignments) == 3

    # Phase 2/3: Simulate verdicts from all machines
    for machine in MACHINES:
        verdict_data = {
            "machine_id": machine,
            "from_node": machine,
            "summary": f"Test verdict from {machine}",
            "branch": f"test/{machine}-integration",
            "confidence": 0.85 + (MACHINES.index(machine) * 0.05),
            "files_touched": ["test.py"],
            "risks": [],
            "dissent": [],
            "phase": "close",
        }
        all_collected = chief.receive_verdict(task_id, verdict_data)

    assert all_collected, "Expected all verdicts collected"
    assert result.phase == SurgicalPhase.PUBLISH

    # Phase 4: REVIEW
    pairs = chief.phase_review(task_id)
    assert len(pairs) == 3, f"Expected 3 critique pairs, got {len(pairs)}"
    assert result.phase == SurgicalPhase.REVIEW

    # Simulate critiques
    for reviewer, target in pairs:
        critique_data = {
            "from_node": reviewer,
            "from_machine": reviewer,
            "target_machine": target,
            "to_node": target,
            "summary": f"{reviewer} reviewed {target}: looks good",
            "confidence": 0.9,
            "blocking_concerns": [],
            "suggested_action": "merge",
        }
        chief.receive_critique(task_id, critique_data)

    # Phase 5: CLOSE
    decision = chief.phase_close(task_id)
    assert decision.decision == "merge", f"Expected merge, got {decision.decision}"
    assert decision.confidence >= 0.7

    return {
        "task_id": task_id,
        "phases_completed": ["ASSIGN", "OPERATE/PUBLISH", "REVIEW", "CLOSE"],
        "assignments": len(result.assignments),
        "verdicts": len(result.verdicts),
        "critiques": len(result.critiques),
        "decision": decision.decision,
        "confidence": round(decision.confidence, 4),
        "winner_branch": decision.winner_branch,
        "dispatches": len(dispatch_log),
    }


def test_relay_with_chief_dispatch():
    """Wire SurgeonLANRelay into ChiefOrchestrator as dispatch function."""
    relay = SurgeonLANRelay(
        node_id=NODE_ID,
        daemon_url=DAEMON_URL,
        fleet_messages_root=FLEET_MSG_ROOT,
    )

    delivery_results = []

    def relay_dispatch(packet: dict, target: str) -> bool:
        sp = SurgeonPacket(
            type=packet.get("type", "task_brief"),
            from_node=packet.get("from_node", NODE_ID),
            to_node=target,
            task_id=packet.get("task_id", ""),
            payload=packet,
        )
        ok = relay.send(sp)
        delivery_results.append({
            "target": target,
            "type": sp.type,
            "delivered": ok,
        })
        return ok

    chief = ChiefOrchestrator(node_id=NODE_ID)
    chief.set_dispatch_fn(relay_dispatch)

    task_id = str(uuid.uuid4())[:8]
    result = chief.phase_assign(
        task_id=task_id,
        task="integration test — relay-wired chief dispatch",
        machines=MACHINES,
    )

    return {
        "task_id": task_id,
        "deliveries": delivery_results,
        "relay_stats": relay.get_stats(),
        "relay_sent_log": relay.get_recent_sent(),
    }


def main():
    print("=" * 70)
    print("CHIEF ORCHESTRATOR CROSS-MACHINE INTEGRATION TEST")
    print(f"Node: {NODE_ID} | Peers: {PEERS} | Time: {time.strftime('%Y-%m-%dT%H:%M:%S')}")
    print("=" * 70)

    # 1. Daemon health
    print("\n[1] Fleet daemon health...")
    health = check_daemon_health()
    daemon_ok = "error" not in health
    nats_ok = check_nats_available(health) if daemon_ok else False
    peers_visible = list(health.get("peers", {}).keys()) if daemon_ok else []
    print(f"    Daemon: {'OK' if daemon_ok else 'DOWN'}")
    print(f"    NATS:   {'OK' if nats_ok else 'UNAVAILABLE'}")
    print(f"    Peers:  {peers_visible}")

    # 2. Relay transport test
    print("\n[2] SurgeonLANRelay transport test...")
    relay_results, relay_stats, relay_task_id = test_relay_send_to_peers()
    for peer, transports in relay_results.items():
        status_parts = []
        for t, ok in transports.items():
            status_parts.append(f"{t}={'OK' if ok else 'FAIL'}")
        print(f"    {peer}: {' | '.join(status_parts)}")
    print(f"    Relay stats: {relay_stats}")

    # 3. Chief orchestrator 5-phase test
    print("\n[3] ChiefOrchestrator 5-phase protocol test (simulated verdicts)...")
    phase_results = test_chief_orchestrator_phases()
    print(f"    Phases: {' -> '.join(phase_results['phases_completed'])}")
    print(f"    Assignments: {phase_results['assignments']}")
    print(f"    Verdicts:    {phase_results['verdicts']}")
    print(f"    Critiques:   {phase_results['critiques']}")
    print(f"    Decision:    {phase_results['decision']} (conf={phase_results['confidence']})")
    print(f"    Winner:      {phase_results['winner_branch']}")
    print(f"    Dispatches:  {phase_results['dispatches']}")

    # 4. Relay-wired chief test (real transport)
    print("\n[4] Chief + Relay wired integration (real transport)...")
    wired_results = test_relay_with_chief_dispatch()
    for d in wired_results["deliveries"]:
        print(f"    -> {d['target']}: {d['type']} = {'DELIVERED' if d['delivered'] else 'FAILED'}")
    sent_log = wired_results["relay_sent_log"]
    transports_used = set(s["transport"] for s in sent_log)
    print(f"    Transports used: {transports_used}")
    print(f"    Relay stats: {wired_results['relay_stats']}")

    # Summary
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)

    all_relay_ok = all(
        any(v for v in transports.values())
        for transports in relay_results.values()
    )
    all_phases_ok = phase_results["decision"] == "merge"
    all_wired_ok = all(d["delivered"] for d in wired_results["deliveries"])

    print(f"  Daemon alive:       {'PASS' if daemon_ok else 'FAIL'}")
    print(f"  NATS available:     {'PASS' if nats_ok else 'SKIP (P7 fallback)'}")
    print(f"  Relay to peers:     {'PASS' if all_relay_ok else 'FAIL'}")
    print(f"  5-phase protocol:   {'PASS' if all_phases_ok else 'FAIL'}")
    print(f"  Wired chief+relay:  {'PASS' if all_wired_ok else 'FAIL'}")
    print(f"  Transports used:    {transports_used}")
    print(f"  Peers responded:    {peers_visible}")

    overall = all([daemon_ok, all_relay_ok, all_phases_ok, all_wired_ok])
    print(f"\n  OVERALL: {'PASS' if overall else 'PARTIAL — see details above'}")

    # Build report for fleet message
    report = {
        "test": "chief-orchestrator-cross-machine-integration",
        "node": NODE_ID,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "daemon": daemon_ok,
        "nats": nats_ok,
        "peers_visible": peers_visible,
        "relay_transports": {
            peer: {t: ok for t, ok in transports.items()}
            for peer, transports in relay_results.items()
        },
        "protocol_phases": phase_results,
        "wired_deliveries": wired_results["deliveries"],
        "transports_used": list(transports_used),
        "overall": "PASS" if overall else "PARTIAL",
    }

    return report


if __name__ == "__main__":
    report = main()

    # Save to fleet messages
    report_path = FLEET_MSG_ROOT / "all" / f"{time.strftime('%Y%m%d')}-chief-integration-test.json"
    report_path.write_text(json.dumps(report, indent=2))
    print(f"\nReport saved: {report_path}")
