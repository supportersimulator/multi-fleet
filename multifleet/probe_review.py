"""Probe Review — runs probes then feeds findings to 3-Surgeons for cross-exam.

This is a practical integration test of the full organism:
probes detect → EpistemicPacket builds → 3-surgeons review → acceptance gate decides.

Usage:
    from multifleet.probe_review import run_probe_review
    result = run_probe_review(repo_root=".", task_intent="Fix auth flow")

CLI:
    python3 -m multifleet.probe_review "Fix auth flow"
"""

import json
import logging
import os
import time
from typing import Optional

logger = logging.getLogger("fleet-nerve.probe-review")


def run_probe_review(
    repo_root: str = ".",
    task_intent: str = "",
    consult_surgeons: bool = True,
) -> dict:
    """Run probes, build packet, optionally consult 3-surgeons, run acceptance gate.

    Returns a comprehensive review result with probe findings, surgeon verdict,
    and acceptance decision.
    """
    t0 = time.time()
    node_id = os.environ.get("MULTIFLEET_NODE_ID", "unknown")
    result = {
        "node_id": node_id,
        "task_intent": task_intent,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    # Step 1: Run all probes
    try:
        from multifleet.probes import create_engine
        engine = create_engine()
        probe_results = engine.scan("all", {
            "repo_root": repo_root,
            "task_intent": task_intent,
        })
        result["probes"] = {
            "total": len(probe_results),
            "findings": sum(len(r.findings) for r in probe_results),
            "surgeon_intensity": engine.determine_surgeon_intensity(),
            "blocking": len(engine.blocking_findings()),
            "results": [r.to_dict() for r in probe_results],
        }
        logger.info(
            "Probes: %d run, %d findings, intensity=%s",
            len(probe_results),
            result["probes"]["findings"],
            result["probes"]["surgeon_intensity"],
        )
    except Exception as e:
        result["probes"] = {"error": str(e)}
        logger.warning("Probe scan failed: %s", e)

    # Step 2: Build EpistemicPacket
    try:
        from multifleet.epistemic_packet import EpistemicPacketBuilder
        builder = EpistemicPacketBuilder(repo_root=repo_root, node_id=node_id)
        packet = builder.build(task_intent=task_intent, run_probes=False)  # Probes already ran
        # Inject probe results into packet
        if "results" in result.get("probes", {}):
            packet.probe_results = result["probes"]["results"]
            packet.required_surgeon_intensity = result["probes"].get("surgeon_intensity", "light")
        result["packet"] = packet.summary()
    except Exception as e:
        result["packet"] = {"error": str(e)}

    # Step 3: Consult 3-Surgeons (if requested and findings warrant it)
    if consult_surgeons and result.get("probes", {}).get("findings", 0) > 0:
        intensity = result.get("probes", {}).get("surgeon_intensity", "light")
        if intensity in ("standard", "full"):
            try:
                from multifleet.product_bridge import ProductBridge
                bridge = ProductBridge()
                bridge.discover_products()

                # Build a claim from probe findings
                findings_summary = []
                for r in result.get("probes", {}).get("results", []):
                    for f in r.get("findings", []):
                        findings_summary.append(f.get("title", ""))

                verdict = bridge.fleet_to_surgeons({
                    "task_id": f"probe-review-{int(time.time())}",
                    "claim": f"Task '{task_intent}' is safe to proceed despite {len(findings_summary)} probe findings",
                    "evidence": findings_summary[:5],
                    "severity": intensity,
                })
                result["surgeon_verdict"] = verdict
                logger.info("3-Surgeons verdict: %s", verdict.get("verdict", "unknown"))
            except Exception as e:
                result["surgeon_verdict"] = {"skipped": True, "reason": str(e)}
        else:
            result["surgeon_verdict"] = {"skipped": True, "reason": "light intensity — no review needed"}

    # Step 4: Acceptance Gate
    try:
        from multifleet.acceptance_gate import AcceptanceGate
        gate = AcceptanceGate()
        decision = gate.evaluate(
            probe_results=result.get("probes", {}).get("results", []),
            surgeon_resolved=result.get("surgeon_verdict", {}).get("verdict") == "approved",
            surgeon_intensity=result.get("probes", {}).get("surgeon_intensity", "light"),
            evidence_count=1 if task_intent else 0,
        )
        result["acceptance"] = decision.to_dict()
        logger.info("Acceptance: %s", decision.summary())
    except Exception as e:
        result["acceptance"] = {"error": str(e)}

    result["elapsed_s"] = round(time.time() - t0, 1)
    return result


if __name__ == "__main__":
    import sys
    intent = sys.argv[1] if len(sys.argv) > 1 else "General codebase health check"
    print(f"Running probe review: {intent}")
    result = run_probe_review(repo_root=".", task_intent=intent, consult_surgeons=False)
    print(json.dumps(result, indent=2, default=str))
