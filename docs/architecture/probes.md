# Probe Engine

19 probes continuously scan fleet and codebase state, feeding the intelligence pipeline. Probes run on triggers (message received, session idle, task complete) and produce scored findings for chief synthesis.

## Fleet Probes

| Probe | What it detects |
|-------|----------------|
| `agent_readiness` | Whether target node can accept work |
| `architectural_hotspots` | Files with high change frequency / coupling |
| `blast_radius` | How many files/systems a change affects |
| `branch_intent` | What a branch is trying to accomplish |
| `dependency_drift` | Version mismatches across nodes |
| `distributed` | Cross-node consistency issues |
| `fleet_health` | Overall fleet liveness and connectivity |
| `hidden_dependency` | Undeclared dependencies in code |
| `injection_prioritizer` | Which context to inject first |
| `invariant_coverage` | How well invariance gates cover the codebase |
| `mirror_drift` | Config/state divergence between nodes |
| `repeated_failure` | Recurring failure patterns |
| `repeated_failure_cluster` | Clustered failures indicating systemic issues |
| `security_surface` | Security exposure in changes |
| `stale_context` | Outdated context that needs refresh |
| `task_diff_mismatch` | Task description vs actual diff divergence |
| `worktree_contamination` | Cross-worktree state leaks |
| `worktree_divergence` | Worktrees drifting from base branch |

## Code Scanner Probes

Deeper static analysis variants:

| Probe | What it detects |
|-------|----------------|
| `blast_radius` | File-level impact analysis |
| `hidden_dependency` | Import graph anomalies |
| `injection_prioritizer` | Context ranking for injection |
| `mirror_drift` | Config divergence detection |
| `security_surface` | Security exposure scanning |
| `stale_context` | Staleness detection |

## Signal Scoring

Probe findings are scored using three dimensions:

- **Severity** -- how critical is the finding (info, warning, error, critical)
- **Recency** -- how fresh is the finding (decays over time)
- **Relevance** -- how related to current work context

Final score = `severity x recency x relevance`. Findings are sorted by score for prioritized injection into active sessions.

## Probe Intensity

The engine adjusts probe depth based on context:

- **Light scan** -- during active coding (minimal interruption)
- **Standard scan** -- on message receive or task complete
- **Deep scan** -- during idle periods (thorough analysis)

## Probe Interpreter

Translates raw probe results into actionable recommendations. For example, a `mirror_drift` finding becomes: "Config divergence detected between mac1 and mac2 on `.multifleet/config.json`. Recommend sync."

## Integration with Chief Synthesis

Probe findings feed directly into the chief synthesis engine, which aggregates them with verdicts from multiple nodes to produce confidence-weighted fleet decisions.
