# Packet Types

Multi-Fleet uses a typed packet system with JSON Schema validation. All messages are wrapped in a `FleetPacket` envelope.

## Packet Envelope

Every packet contains:

| Field | Type | Description |
|-------|------|-------------|
| `id` | UUID | Unique packet ID for deduplication |
| `version` | string | Protocol version (`"fleet/v1"`) |
| `type` | string | Packet type (must be registered) |
| `from_node` | string | Sender node ID |
| `to_node` | string | Recipient node ID |
| `timestamp` | float | Unix timestamp |
| `payload` | object | Type-specific data (opaque to transport) |
| `ttl_minutes` | int | Time-to-live before expiry |

## Built-in Types

### Core Protocol

| Type | Purpose | Key Fields |
|------|---------|------------|
| `ack` | Delivery confirmation | `original_id`, `status` |
| `heartbeat` | Liveness signal | `git_branch`, `git_commit`, `idle_seconds` |
| `repair` | Channel repair instruction | `action`, `broken_channels`, `evidence` |
| `sync_hold` | Pause operations for consensus | `reason`, `scope`, `duration_minutes` |

### Resource Management

| Type | Purpose | Key Fields |
|------|---------|------------|
| `lease_request` | Request resource ownership | `resource_id`, `lease_type`, `duration_minutes` |
| `lease_grant` | Chief grants a lease | `resource_id`, `lease_id`, `granted_to`, `expires_at` |
| `lease_release` | Voluntary lease release | `lease_id`, `resource_id` |

### Task Lifecycle

| Type | Purpose | Key Fields |
|------|---------|------------|
| `task_brief` | Work dispatch | `task_id`, `description`, `timeout` |
| `execution_ledger` | Execution step tracking | `task_id`, `step_id`, `status`, `evidence` |

### Intelligence

| Type | Purpose | Key Fields |
|------|---------|------------|
| `local_verdict` | Node's assessment | `task_id`, `verdict`, `confidence`, `evidence` |
| `chief_decision` | Chief's final decision | `task_id`, `decision`, `confidence`, `reasoning` |
| `chief_decision_v2` | Extended chief decision | `task_id`, `decision`, `verdicts`, `synthesis` |
| `chief_escalation` | Escalation to human | `task_id`, `reason`, `urgency` |
| `truth_promotion` | Evidence promotion | `finding_id`, `from_layer`, `to_layer`, `evidence` |

### Consensus

| Type | Purpose | Key Fields |
|------|---------|------------|
| `proposal` | Submit a proposal | `subject`, `body`, `scope` |
| `critique` | Review a proposal | `proposal_id`, `position`, `reasoning` |
| `rebuttal` | Counter-argument | `proposal_id`, `counter_argument` |
| `resolution` | Final resolution | `proposal_id`, `outcome`, `reasoning` |

### Vision

| Type | Purpose | Key Fields |
|------|---------|------------|
| `brainstorm` | Brainstorm session | `task_id`, `options`, `constraints` |
| `surgeon_decision` | Surgeon adjudication | `brainstorm_id`, `surgeon`, `vote`, `reasoning` |
| `vision_contract` | Locked vision spec | `brainstorm_id`, `done_criteria`, `assigned_nodes` |
| `supervisor_phase` | Execution phase tracking | `contract_id`, `phase`, `status` |

### Coordination

| Type | Purpose | Key Fields |
|------|---------|------------|
| `branch_status` | Branch state report | `branch`, `commits_ahead`, `conflicts` |
| `merge_candidate` | Ready-to-merge branch | `branch`, `evidence_refs`, `acceptance_score` |
| `race_start` | Competitive race init | `task`, `nodes`, `timeout` |

## Custom Packet Types

Register custom types via the packet registry:

```python
from multifleet.packet_registry import PacketRegistry

registry = PacketRegistry()
registry.register("my_custom_type", schema={
    "type": "object",
    "properties": {
        "data": {"type": "string"}
    },
    "required": ["data"]
})
```

All packets are validated against their schema before sending. Invalid packets are rejected at the transport layer.
