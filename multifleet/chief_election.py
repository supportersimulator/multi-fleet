"""Chief Election Protocol -- heartbeat + majority vote leader election.

No Raft/Paxos complexity. Simple protocol:
1. Heartbeat: chief must be seen within timeout or is presumed dead
2. Any node can propose an election (with reason)
3. Nodes vote for their preferred candidate
4. Simple majority (2 of 3) wins. Ties broken alphabetically.

Election criteria (scored in order):
- Must be healthy (not offline)
- Highest uptime
- Most channels operational
- Best reputation score
- Alphabetical tie-break (deterministic)
"""

import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

logger = logging.getLogger("fleet-nerve.chief-election")


class ElectionState(Enum):
    """State of an election proposal."""
    PROPOSED = "proposed"
    VOTING = "voting"
    DECIDED = "decided"
    EXPIRED = "expired"


@dataclass
class ElectionProposal:
    """A proposal to elect a new chief."""
    proposal_id: str
    reason: str
    proposed_by: str
    proposed_at: float
    state: ElectionState = ElectionState.PROPOSED
    votes: dict = field(default_factory=dict)   # voter_id -> candidate_id
    winner: Optional[str] = None
    decided_at: Optional[float] = None

    def to_dict(self) -> dict:
        return {
            "proposal_id": self.proposal_id,
            "reason": self.reason,
            "proposed_by": self.proposed_by,
            "proposed_at": self.proposed_at,
            "state": self.state.value,
            "votes": dict(self.votes),
            "winner": self.winner,
            "decided_at": self.decided_at,
        }


class ChiefElection:
    """Heartbeat-based chief election with majority vote.

    Usage:
        election = ChiefElection(node_id="mac1", fleet_size=3)

        # Check if chief is alive
        if not election.check_chief_alive("mac2", last_seen=time.time() - 400):
            proposal = election.propose_election("chief mac2 unresponsive")
            election.vote(proposal.proposal_id, candidate="mac1", voter="mac1")
            election.vote(proposal.proposal_id, candidate="mac1", voter="mac3")
            winner = election.tally(proposal.proposal_id)
            # winner == "mac1"
    """

    def __init__(self, node_id: str = "unknown", fleet_size: int = 3):
        self._node_id = node_id
        self._fleet_size = fleet_size
        self._proposals: dict[str, ElectionProposal] = {}
        self._current_chief: Optional[str] = None
        self._chief_last_seen: float = 0.0
        self._on_new_chief_callbacks: list = []
        # Proposal expiry: elections that don't complete within this window
        # are marked expired to prevent stale proposals from accumulating.
        self._proposal_ttl: float = 600.0  # 10 minutes

    @property
    def current_chief(self) -> Optional[str]:
        return self._current_chief

    @current_chief.setter
    def current_chief(self, value: str):
        self._current_chief = value
        self._chief_last_seen = time.time()

    def record_chief_heartbeat(self, chief_id: str) -> None:
        """Record that we've seen the chief alive."""
        if chief_id == self._current_chief:
            self._chief_last_seen = time.time()

    # -- Election criteria --

    def elect(self, candidates: list[str], health: dict) -> str:
        """Pick the best chief from healthy candidates.

        Args:
            candidates: list of node IDs eligible to be chief
            health: {node_id: {status, uptime_s, score, total, ...}}
                status: "healthy" | "degraded" | "offline"
                uptime_s: seconds of uptime
                score: channels operational (int)
                total: total channels (int)

        Returns:
            node_id of the elected chief

        Raises:
            ValueError: if no healthy candidates available
        """
        if not candidates:
            raise ValueError("No candidates provided for election")

        # Filter to healthy candidates (not offline)
        healthy = [c for c in candidates if health.get(c, {}).get("status") != "offline"]
        if not healthy:
            raise ValueError("No healthy candidates available for election")

        # Import reputation scorer
        try:
            from multifleet.quorum.node_reputation import compute_node_reputation
            has_reputation = True
        except ImportError:
            has_reputation = False

        def _score(node_id: str) -> tuple:
            """Score a candidate. Higher tuple = better candidate."""
            info = health.get(node_id, {})
            # Prefer healthy over degraded
            status_score = 1 if info.get("status") == "healthy" else 0
            uptime = info.get("uptime_s", 0)
            channels = info.get("score", 0)

            if has_reputation:
                reputation = compute_node_reputation(
                    successful_tasks=info.get("successful_tasks", 0),
                    failed_tasks=info.get("failed_tasks", 0),
                    avg_chief_sync_age_sec=info.get("avg_chief_sync_age_sec", 0),
                    p3plus_usage_ratio=info.get("p3plus_usage_ratio", 0),
                    context_quality=info.get("context_quality", 1.0),
                )
            else:
                reputation = 0.5

            # Alphabetical tie-break: invert so earlier letter wins
            # Use negative ord sum so "aaa" > "zzz" in sort
            alpha_tiebreak = -sum(ord(c) for c in node_id)

            return (status_score, uptime, channels, reputation, alpha_tiebreak)

        best = max(healthy, key=_score)
        logger.info("Election result: %s chosen from %d candidates", best, len(healthy))
        return best

    # -- Heartbeat detection --

    def check_chief_alive(
        self, chief_id: str, last_seen: float, timeout: float = 300.0
    ) -> bool:
        """Check if chief is alive based on last heartbeat.

        Args:
            chief_id: The current chief node ID
            last_seen: Timestamp of last heartbeat from chief
            timeout: Seconds before chief is considered dead (default 5min)

        Returns:
            True if chief is alive, False if stale
        """
        age = time.time() - last_seen
        alive = age <= timeout
        if not alive:
            logger.warning(
                "Chief %s stale: last seen %.0fs ago (timeout=%.0fs)",
                chief_id, age, timeout,
            )
        return alive

    # -- Election protocol --

    def propose_election(self, reason: str) -> ElectionProposal:
        """Propose a new chief election.

        Args:
            reason: Why the election is needed (e.g. "chief mac2 unresponsive")

        Returns:
            ElectionProposal that other nodes can vote on
        """
        # Expire old proposals first
        self._expire_stale_proposals()

        proposal = ElectionProposal(
            proposal_id=uuid.uuid4().hex[:12],
            reason=reason,
            proposed_by=self._node_id,
            proposed_at=time.time(),
            state=ElectionState.VOTING,
        )
        self._proposals[proposal.proposal_id] = proposal
        logger.info(
            "Election proposed by %s: %s (id=%s)",
            self._node_id, reason, proposal.proposal_id,
        )
        return proposal

    def vote(self, proposal_id: str, candidate: str, voter: str) -> None:
        """Cast a vote in an election.

        Args:
            proposal_id: The election proposal to vote on
            candidate: The node ID being voted for
            voter: The node ID casting the vote

        Raises:
            KeyError: if proposal_id not found
            ValueError: if proposal is not in VOTING state, or voter already voted
        """
        proposal = self._proposals.get(proposal_id)
        if proposal is None:
            raise KeyError(f"Unknown proposal: {proposal_id}")
        if proposal.state != ElectionState.VOTING:
            raise ValueError(
                f"Proposal {proposal_id} is {proposal.state.value}, not voting"
            )
        if voter in proposal.votes:
            raise ValueError(f"Voter {voter} already voted in proposal {proposal_id}")

        proposal.votes[voter] = candidate
        logger.info(
            "Vote cast: %s votes for %s in election %s",
            voter, candidate, proposal_id,
        )

    def tally(self, proposal_id: str) -> Optional[str]:
        """Tally votes and return winner if quorum reached.

        Simple majority: more than half the fleet must vote for the same
        candidate. With 3 nodes, that means 2 votes.

        Args:
            proposal_id: The election proposal to tally

        Returns:
            Winner node_id if quorum reached, None if not enough votes yet

        Raises:
            KeyError: if proposal_id not found
        """
        proposal = self._proposals.get(proposal_id)
        if proposal is None:
            raise KeyError(f"Unknown proposal: {proposal_id}")

        if proposal.state == ElectionState.DECIDED:
            return proposal.winner

        # Count votes per candidate
        vote_counts: dict[str, int] = {}
        for candidate in proposal.votes.values():
            vote_counts[candidate] = vote_counts.get(candidate, 0) + 1

        # Check for majority
        quorum = (self._fleet_size // 2) + 1  # e.g. 2 for fleet of 3
        for candidate, count in sorted(vote_counts.items()):
            if count >= quorum:
                proposal.winner = candidate
                proposal.state = ElectionState.DECIDED
                proposal.decided_at = time.time()
                self._set_chief(candidate)
                logger.info(
                    "Election %s decided: %s wins with %d/%d votes",
                    proposal_id, candidate, count, self._fleet_size,
                )
                return candidate

        return None

    def get_proposal(self, proposal_id: str) -> Optional[ElectionProposal]:
        """Get a proposal by ID."""
        return self._proposals.get(proposal_id)

    def active_proposals(self) -> list[ElectionProposal]:
        """Get all non-expired, non-decided proposals."""
        self._expire_stale_proposals()
        return [
            p for p in self._proposals.values()
            if p.state in (ElectionState.PROPOSED, ElectionState.VOTING)
        ]

    def on_new_chief(self, callback) -> None:
        """Register a callback for when a new chief is elected.

        Callback signature: callback(chief_id: str)
        """
        self._on_new_chief_callbacks.append(callback)

    # -- Health monitor integration --

    def wire_to_health_monitor(self, monitor) -> None:
        """Subscribe to health events and trigger election when chief goes stale.

        Hooks into FleetHealthMonitor's check cycle. After each check,
        evaluates whether the current chief is still alive. If not,
        proposes an election using health data from the monitor.

        Args:
            monitor: FleetHealthMonitor instance
        """
        original_check = monitor.check_fleet

        def _check_with_election():
            result = original_check()
            if not self._current_chief:
                return result

            # Check if chief is still alive in the health data
            nodes = result.get("nodes", {})
            chief_info = nodes.get(self._current_chief)

            if chief_info:
                last_seen_s = chief_info.get("last_seen_s")
                status = chief_info.get("status", "unknown")

                if status == "offline" or (
                    last_seen_s is not None and last_seen_s > 300
                ):
                    # Chief is stale -- propose election
                    if not self.active_proposals():
                        candidates = [
                            nid for nid, info in nodes.items()
                            if info.get("status") != "offline"
                        ]
                        if candidates:
                            proposal = self.propose_election(
                                f"chief {self._current_chief} stale "
                                f"(status={status}, last_seen={last_seen_s}s)"
                            )
                            # Auto-elect based on health data
                            try:
                                winner = self.elect(candidates, nodes)
                                # Self-vote
                                self.vote(
                                    proposal.proposal_id,
                                    candidate=winner,
                                    voter=self._node_id,
                                )
                            except (ValueError, Exception) as e:
                                logger.warning("Auto-election failed: %s", e)

            return result

        monitor.check_fleet = _check_with_election
        logger.info(
            "Chief election wired to health monitor (chief=%s)",
            self._current_chief,
        )

    # -- Internal --

    def _set_chief(self, chief_id: str) -> None:
        """Set the current chief and notify callbacks."""
        old = self._current_chief
        self._current_chief = chief_id
        self._chief_last_seen = time.time()
        if old != chief_id:
            logger.info("Chief changed: %s -> %s", old, chief_id)
            for cb in self._on_new_chief_callbacks:
                try:
                    cb(chief_id)
                except Exception as e:
                    logger.error("Chief callback error: %s", e)

    def _expire_stale_proposals(self) -> None:
        """Mark old proposals as expired."""
        now = time.time()
        for p in self._proposals.values():
            if p.state in (ElectionState.PROPOSED, ElectionState.VOTING):
                if now - p.proposed_at > self._proposal_ttl:
                    p.state = ElectionState.EXPIRED
                    logger.debug("Proposal %s expired", p.proposal_id)
