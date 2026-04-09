"""Fleet Billing — tier management + usage tracking for Pro/Team/Enterprise.

Tiers:
- Community: Free, unlimited nodes, local only
- Pro: $29/node/month, <=10 nodes, chief synthesis + evidence export
- Team: $49/node/month, <=50 nodes, cross-machine review + merge adjudication
- Enterprise: Custom, unlimited, SSO + compliance + SLA
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional


class BillingTier(Enum):
    COMMUNITY = "community"
    PRO = "pro"
    TEAM = "team"
    ENTERPRISE = "enterprise"


# Tier ordering for comparison
_TIER_ORDER = {
    BillingTier.COMMUNITY: 0,
    BillingTier.PRO: 1,
    BillingTier.TEAM: 2,
    BillingTier.ENTERPRISE: 3,
}

TIER_LIMITS: Dict[BillingTier, dict] = {
    BillingTier.COMMUNITY: {
        "max_nodes": 999,
        "price_per_node": 0,
        "features": [
            "transport", "local_surgery", "probes", "gossip_discovery",
            "lease_ownership", "p7_git_fallback", "mcp_tools_basic",
        ],
    },
    BillingTier.PRO: {
        "max_nodes": 10,
        "price_per_node": 29,
        "features": [
            "transport", "local_surgery", "probes", "gossip_discovery",
            "lease_ownership", "p7_git_fallback", "mcp_tools_basic",
            "chief_synthesis", "evidence_export", "dashboard",
            "fleet_race", "fleet_evidence", "gains_gate",
            "arbiter_view", "fleet_synthesize", "fleet_arbiter",
        ],
    },
    BillingTier.TEAM: {
        "max_nodes": 50,
        "price_per_node": 49,
        "features": [
            "transport", "local_surgery", "probes", "gossip_discovery",
            "lease_ownership", "p7_git_fallback", "mcp_tools_basic",
            "chief_synthesis", "evidence_export", "dashboard",
            "fleet_race", "fleet_evidence", "gains_gate",
            "arbiter_view", "fleet_synthesize", "fleet_arbiter",
            "cross_review", "merge_adjudication", "audit_trail",
            "fleet_cross_rebuttal", "fleet_merge", "fleet_orchestrate",
            "fleet_supervisor",
        ],
    },
    BillingTier.ENTERPRISE: {
        "max_nodes": 99999,
        "price_per_node": 0,  # custom pricing
        "features": ["all"],
    },
}

# Map MCP tool names to required billing features
TOOL_FEATURE_MAP: Dict[str, str] = {
    "fleet_synthesize": "fleet_synthesize",
    "fleet_arbiter": "fleet_arbiter",
    "fleet_cross_rebuttal": "fleet_cross_rebuttal",
    "fleet_merge": "fleet_merge",
    "fleet_orchestrate": "fleet_orchestrate",
    "fleet_race": "fleet_race",
    "fleet_evidence": "fleet_evidence",
    "fleet_gains_gate": "gains_gate",
}

# Minimum tier required per feature (computed, not hardcoded)
_MIN_TIER_CACHE: Dict[str, BillingTier] = {}


def _min_tier_for_feature(feature: str) -> Optional[BillingTier]:
    """Return the lowest tier that includes a feature."""
    if feature in _MIN_TIER_CACHE:
        return _MIN_TIER_CACHE[feature]
    for tier in (BillingTier.COMMUNITY, BillingTier.PRO, BillingTier.TEAM, BillingTier.ENTERPRISE):
        feats = TIER_LIMITS[tier]["features"]
        if "all" in feats or feature in feats:
            _MIN_TIER_CACHE[feature] = tier
            return tier
    return None


class BillingError(Exception):
    """Raised when a billing check fails."""

    def __init__(self, message: str, current_tier: str = "", required_tier: str = "", feature: str = ""):
        super().__init__(message)
        self.current_tier = current_tier
        self.required_tier = required_tier
        self.feature = feature


@dataclass
class UsageEvent:
    feature: str
    timestamp: float
    node_id: str = ""


@dataclass
class BillingManager:
    """Manages tier checks, feature gating, and usage tracking."""

    tier: BillingTier = BillingTier.COMMUNITY
    _usage_log: List[UsageEvent] = field(default_factory=list)

    def check_feature(self, feature: str) -> bool:
        """Return True if the current tier allows this feature."""
        allowed = TIER_LIMITS[self.tier]["features"]
        if "all" in allowed:
            return True
        return feature in allowed

    def check_node_limit(self, current_nodes: int) -> bool:
        """Return True if adding another node is within tier limits."""
        return current_nodes <= TIER_LIMITS[self.tier]["max_nodes"]

    def require_feature(self, feature: str) -> None:
        """Raise BillingError if feature is not available in current tier."""
        if self.check_feature(feature):
            return
        min_tier = _min_tier_for_feature(feature)
        required_name = min_tier.value if min_tier else "unknown"
        price = TIER_LIMITS[min_tier]["price_per_node"] if min_tier else 0
        raise BillingError(
            f"{feature} requires {required_name.title()} tier (${price}/node/month)",
            current_tier=self.tier.value,
            required_tier=required_name,
            feature=feature,
        )

    def require_tool(self, tool_name: str) -> None:
        """Check if an MCP tool is allowed under the current tier.

        Uses TOOL_FEATURE_MAP to resolve tool_name -> feature.
        Tools not in the map are allowed (community-tier tools).
        """
        feature = TOOL_FEATURE_MAP.get(tool_name)
        if feature is None:
            return  # ungated tool
        self.require_feature(feature)

    def record_usage(self, feature: str, node_id: str = "") -> None:
        """Record a feature usage event for reporting."""
        self._usage_log.append(UsageEvent(
            feature=feature,
            timestamp=time.time(),
            node_id=node_id,
        ))

    def usage_report(self) -> dict:
        """Generate a usage summary."""
        tier_info = TIER_LIMITS[self.tier]
        feature_counts: Dict[str, int] = {}
        for evt in self._usage_log:
            feature_counts[evt.feature] = feature_counts.get(evt.feature, 0) + 1
        return {
            "tier": self.tier.value,
            "max_nodes": tier_info["max_nodes"],
            "price_per_node": tier_info["price_per_node"],
            "features_available": tier_info["features"],
            "usage_counts": feature_counts,
            "total_events": len(self._usage_log),
        }

    def upgrade_tier(self, new_tier: BillingTier) -> dict:
        """Upgrade (or change) the billing tier. Returns change summary."""
        old = self.tier
        old_info = TIER_LIMITS[old]
        new_info = TIER_LIMITS[new_tier]

        if _TIER_ORDER[new_tier] < _TIER_ORDER[old]:
            direction = "downgrade"
        elif _TIER_ORDER[new_tier] > _TIER_ORDER[old]:
            direction = "upgrade"
        else:
            direction = "no_change"

        self.tier = new_tier

        # Compute newly available features
        old_feats = set(old_info["features"])
        new_feats = set(new_info["features"])
        gained = sorted(new_feats - old_feats) if "all" not in new_feats else ["all"]
        lost = sorted(old_feats - new_feats) if "all" not in old_feats else []

        return {
            "direction": direction,
            "old_tier": old.value,
            "new_tier": new_tier.value,
            "old_price": old_info["price_per_node"],
            "new_price": new_info["price_per_node"],
            "features_gained": gained,
            "features_lost": lost,
        }

    def tier_for_feature(self, feature: str) -> Optional[str]:
        """Return the minimum tier name required for a feature, or None."""
        t = _min_tier_for_feature(feature)
        return t.value if t else None


# Module-level singleton (daemon initializes once)
_billing: Optional[BillingManager] = None


def get_billing(tier: Optional[BillingTier] = None) -> BillingManager:
    """Get or create the global BillingManager singleton."""
    global _billing
    if _billing is None:
        _billing = BillingManager(tier=tier or BillingTier.COMMUNITY)
    elif tier is not None and tier != _billing.tier:
        _billing.tier = tier
    return _billing


def reset_billing() -> None:
    """Reset the singleton (for testing)."""
    global _billing
    _billing = None
