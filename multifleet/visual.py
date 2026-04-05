"""
Fleet Nerve Visual — Theatrical rendering for Multi-Fleet and 3-Surgeons output.

Generates rich markdown formatted for Claude Code sessions. The "theatrical
experience" is structured markdown that Claude presents with visual clarity.

Used by:
- Fleet Nerve daemon (seed file injection)
- fleet-swarm.sh (terminal output)
- 3-Surgeons cross-exam integration (fleet-wide surgical reviews)

Design principle: Sports commentator narrating a live event.
Each operation has phases, each node/surgeon has a visual identity,
progress is quantified, and results are summarized dramatically.
"""

import time
from datetime import datetime


# ── Node visual identities ──

NODE_ICONS = {
    "mac1": "🏛️",   # chief/temple
    "mac2": "⚡",   # lightning/speed
    "mac3": "🔬",   # microscope/research
}

def _get_node_role(node_id: str) -> str:
    """Get role from config, not hardcoded."""
    try:
        from .config import load_peers
        peers, chief = load_peers()
        if node_id == chief:
            return "Chief / Synthesis"
        return peers.get(node_id, {}).get("role", "Worker / Execution").replace("worker", "Worker / Execution").replace("chief", "Chief / Synthesis")
    except Exception:
        return "Node"

NODE_ROLES = type("DynamicRoles", (), {"get": lambda self, k, d="Node": _get_node_role(k)})()

SURGEON_ICONS = {
    "atlas": "🧠",
    "cardiologist": "❤️",
    "neurologist": "🔮",
}


# ── Phase banners ──

def phase_banner(phase_num: int, total: int, title: str, subtitle: str = "") -> str:
    bar = "█" * phase_num + "░" * (total - phase_num)
    sub = f"\n> {subtitle}" if subtitle else ""
    return (
        f"\n---\n"
        f"### Phase {phase_num}/{total}: {title}\n"
        f"`[{bar}]` {phase_num}/{total}{sub}\n"
    )


def fleet_header(operation: str, nodes_online: int, total_nodes: int) -> str:
    ts = datetime.now().strftime("%H:%M:%S")
    return (
        f"\n---\n"
        f"## ⚡ MULTI-FLEET: {operation}\n"
        f"> {ts} | {nodes_online}/{total_nodes} nodes online\n\n"
    )


def fleet_complete(operation: str, duration_s: int, replies: int, cost: float = 0) -> str:
    cost_str = f" | ${cost:.3f}" if cost > 0 else ""
    return (
        f"\n---\n"
        f"## ✅ Fleet Complete: {operation}\n"
        f"> {duration_s}s | {replies} replies{cost_str}\n"
        f"---\n"
    )


# ── Node status cards ──

def node_card(name: str, status: str, sessions: int = 0, branch: str = "",
              active_tasks: int = 0, idle_s: int = 0, gold_chars: int = 0,
              surgeons: dict = None) -> str:
    icon = NODE_ICONS.get(name, "●")
    role = NODE_ROLES.get(name, "Node")

    if status == "offline":
        return f"| {icon} **{name}** | 🔴 Offline | — | — |\n"

    task_str = f"⏳ {active_tasks} running" if active_tasks > 0 else "idle"
    s = surgeons or {}
    s_str = ""
    if s.get("cardiologist") == "ok" or s.get("cardiologist") == "configured":
        s_str += "❤️"
    else:
        s_str += "💔"
    if s.get("neurologist") == "ok":
        s_str += "🔮"
    else:
        s_str += "⭕"

    return f"| {icon} **{name}** ({role}) | 🟢 {sessions}s | {branch} | {task_str} | {s_str} | {gold_chars}c |\n"


def fleet_status_table(nodes: list) -> str:
    header = "| Node | Status | Branch | Tasks | Surgeons | Gold |\n|------|--------|--------|-------|----------|------|\n"
    rows = ""
    for n in nodes:
        rows += node_card(
            n["name"], n.get("status", "offline"),
            sessions=n.get("sessions", 0),
            branch=n.get("branch", "?"),
            active_tasks=n.get("activeTasks", 0),
            surgeons=n.get("surgeons"),
            gold_chars=n.get("goldChars", 0),
        )
    return header + rows


# ── Task progress cards ──

def task_card(task_id: str, node: str, status: str, age_s: int = 0,
              last_line: str = "") -> str:
    icon = NODE_ICONS.get(node, "●")
    if status == "running":
        status_str = f"⏳ Running ({age_s}s)"
    elif status == "completed":
        status_str = f"✅ Complete ({age_s}s)"
    elif status == "failed":
        status_str = f"❌ Failed ({age_s}s)"
    elif status == "timeout":
        status_str = f"⏰ Timeout ({age_s}s)"
    else:
        status_str = status

    tail = f"\n> `{last_line[:120]}`" if last_line else ""
    return f"- {icon} **{node}** `{task_id[:8]}` — {status_str}{tail}\n"


# ── Surgeon cross-exam visual ──

def surgeon_section(surgeon: str, report: str) -> str:
    icon = SURGEON_ICONS.get(surgeon, "🔬")
    return (
        f"\n#### {icon} {surgeon.title()}\n\n"
        f"{report}\n"
    )


def cross_exam_header(topic: str, mode: str, iteration: int = 0, max_iter: int = 0) -> str:
    iter_str = f" | Iteration {iteration}/{max_iter}" if max_iter > 0 else ""
    return (
        f"\n---\n"
        f"## 🏥 3-Surgeons Cross-Examination\n"
        f"> **Topic:** {topic}\n"
        f"> **Mode:** {mode}{iter_str}\n\n"
    )


def cross_exam_synthesis(synthesis: str, cost: float, latency_ms: int,
                         consensus: float = 0, escalation: bool = False) -> str:
    consensus_bar = "█" * int(consensus * 10) + "░" * (10 - int(consensus * 10))
    esc = "\n> ⚠️ **Escalation needed** — consensus not reached" if escalation else ""
    return (
        f"\n#### 🧬 Synthesis\n\n"
        f"{synthesis}\n\n"
        f"> Consensus: `[{consensus_bar}]` {consensus:.0%} | "
        f"Cost: ${cost:.4f} | Latency: {latency_ms}ms{esc}\n"
        f"---\n"
    )


# ── Fleet activity summary (for seed file injection) ──

def fleet_activity_summary(nodes_data: list) -> str:
    """Generate theatrical seed file content for fleet activity broadcast."""
    ts = datetime.now().strftime("%H:%M:%S")
    running = [n for n in nodes_data if n.get("activeTasks", 0) > 0]

    if not running:
        return ""

    lines = [f"## ⚡ [FLEET LIVE] {ts} — {len(running)} node(s) active\n"]
    lines.append("| Node | Task | Status | Output |")
    lines.append("|------|------|--------|--------|")

    for n in nodes_data:
        icon = NODE_ICONS.get(n["name"], "●")
        for t in n.get("tasks", []):
            if t.get("running"):
                tail = t.get("lastLine", "")[:60]
                lines.append(f"| {icon} {n['name']} | `{t['id'][:8]}` | ⏳ Running | {tail} |")
            elif t.get("age", 999) < 120:  # show recently completed
                lines.append(f"| {icon} {n['name']} | `{t['id'][:8]}` | ✅ Done ({t['age']}s ago) | |")

    lines.append("\n---\n")
    return "\n".join(lines)


# ── Combined fleet + 3-surgeons visual for swarm cross-exam ──

def swarm_cross_exam_header(topic: str, nodes: list) -> str:
    node_str = ", ".join(f"{NODE_ICONS.get(n, '●')} {n}" for n in nodes)
    return (
        f"\n---\n"
        f"## ⚡🏥 Fleet Swarm Cross-Examination\n"
        f"> **Topic:** {topic}\n"
        f"> **Nodes:** {node_str}\n"
        f"> **Protocol:** Each node runs 3-Surgeons locally → results synthesized by chief\n\n"
    )
