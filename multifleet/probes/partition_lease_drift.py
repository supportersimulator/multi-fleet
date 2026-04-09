"""Partition Lease Drift Probe — detects provisional lease accumulation.

When network partitions cause workers to acquire provisional leases,
those leases must be reconciled when connectivity restores. This probe
flags when:
- Too many provisional leases accumulate (drift)
- Provisional leases age past their TTL without reconciliation (stale)
- Multiple nodes hold provisionals for the same resource (conflict risk)

Part of the Raft-Lite Lease Consensus design (Session B, item 3).
"""

import logging
import os
import time

from multifleet.probe_engine import ProbeFinding, ProbeResult

logger = logging.getLogger("fleet-nerve.probes.partition_lease_drift")

# Thresholds
PROVISIONAL_WARN_COUNT = 3   # warn if >= 3 provisionals
PROVISIONAL_CRIT_COUNT = 10  # critical if >= 10 provisionals
STALE_THRESHOLD_S = 600      # 10 minutes without reconciliation = stale


def probe_partition_lease_drift(context: dict) -> ProbeResult:
    """Check for provisional lease accumulation and staleness."""
    try:
        from multifleet.leases import LeaseManager
        from multifleet.store import FleetNerveStore
    except ImportError as e:
        return ProbeResult.error(
            "partition_lease_drift", "reliability",
            f"Cannot import LeaseManager/FleetNerveStore: {e}",
        )

    node_id = context.get("node_id") or os.environ.get("MULTIFLEET_NODE_ID", "unknown")

    try:
        store = FleetNerveStore()
        mgr = LeaseManager(store=store, node_id=node_id)
    except Exception as e:
        return ProbeResult.error(
            "partition_lease_drift", "reliability",
            f"Cannot create LeaseManager: {e}",
        )

    try:
        provisionals = mgr.list_provisional()
    except Exception as e:
        return ProbeResult.error(
            "partition_lease_drift", "reliability",
            f"list_provisional() failed: {e}",
        )

    if not provisionals:
        return ProbeResult(
            probe_id="partition_lease_drift",
            category="reliability",
            status="ok",
            confidence=1.0,
            severity="info",
            findings=[],
        )

    findings = []
    now = time.time()

    # Check for stale provisionals (acquired long ago, never reconciled)
    stale = [
        p for p in provisionals
        if (now - p.get("acquired_at", now)) > STALE_THRESHOLD_S
    ]
    if stale:
        findings.append(ProbeFinding(
            title=f"{len(stale)} stale provisional lease(s) (>{STALE_THRESHOLD_S}s without reconciliation)",
            evidence=[
                f"resource={p.get('resource_id', '?')} age={now - p.get('acquired_at', now):.0f}s partition={p.get('partition_id', '?')}"
                for p in stale[:5]
            ],
            blast_radius="high",
            suggested_injections=[
                "Reconcile provisionals: POST /lease/reconcile",
                "Check chief reachability: curl http://<chief>:8855/health",
            ],
        ))

    # Check for resource conflicts (same resource held provisionally by multiple nodes)
    resource_nodes: dict[str, list[str]] = {}
    for p in provisionals:
        rid = p.get("resource_id", "?")
        nid = p.get("node_id", "?")
        resource_nodes.setdefault(rid, []).append(nid)

    conflicts = {r: nodes for r, nodes in resource_nodes.items() if len(nodes) > 1}
    if conflicts:
        findings.append(ProbeFinding(
            title=f"{len(conflicts)} resource(s) with multi-node provisional conflict",
            evidence=[
                f"resource={r} nodes={nodes}"
                for r, nodes in list(conflicts.items())[:5]
            ],
            blast_radius="high",
            suggested_injections=["Resolve via arbiter: GET /leases/conflicts"],
        ))

    # Determine severity
    count = len(provisionals)
    if count >= PROVISIONAL_CRIT_COUNT or conflicts:
        severity = "critical"
        status = "critical"
    elif count >= PROVISIONAL_WARN_COUNT or stale:
        severity = "high"
        status = "warning"
    else:
        severity = "medium"
        status = "warning"

    # Always add a count finding
    findings.insert(0, ProbeFinding(
        title=f"{count} active provisional lease(s) detected (partition drift)",
        evidence=[
            f"total_provisionals={count}",
            f"stale={len(stale)}",
            f"conflict_resources={len(conflicts)}",
        ],
        blast_radius="medium" if count < PROVISIONAL_CRIT_COUNT else "high",
        suggested_injections=["Monitor lease reconciliation on partition heal"],
    ))

    return ProbeResult(
        probe_id="partition_lease_drift",
        category="reliability",
        status=status,
        confidence=0.95,
        severity=severity,
        findings=findings,
    )
