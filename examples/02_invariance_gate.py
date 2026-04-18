"""Example 2: Constitutional gate validation.

Shows how to run `validate_before_action` against an arbitrary operation.
Constitutional Physics enforces determinism, evidence, reversibility, minimalism.

Run: python3 examples/02_invariance_gate.py
"""
from __future__ import annotations

import sys

from multifleet.constitution import validate_before_action, get_constitution


def main() -> int:
    print("Constitutional Principles:")
    for p in get_constitution():
        print(f"  {p.id}  {p.name}  [{p.severity}]")
    print()

    # Case 1: reversible action with evidence
    ctx = {"has_checkpoint": True, "is_destructive": False, "evidence_count": 3}
    r = validate_before_action("deploy_feature_flag", ctx)
    print(f"[reversible+evidence] passed={r.passed}  checks={len(r.checks)}  advisories={len([c for c in r.checks if not c.passed and not c.critical])}")

    # Case 2: destructive action without checkpoint — should fail CP-6
    ctx = {"has_checkpoint": False, "is_destructive": True, "evidence_count": 0}
    r = validate_before_action("drop_table", ctx)
    print(f"[destructive+no-checkpoint] passed={r.passed}")
    for c in r.checks:
        if not c.passed and c.critical:
            print(f"  BLOCKED by {c.name}: {c.message}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
