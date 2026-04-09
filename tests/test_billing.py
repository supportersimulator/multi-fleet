"""Tests for fleet billing — tier management, feature gating, usage tracking."""

import pytest

from multifleet.billing import (
    BillingError,
    BillingManager,
    BillingTier,
    TIER_LIMITS,
    TOOL_FEATURE_MAP,
    get_billing,
    reset_billing,
)


# ── Tier structure tests ──


def test_all_tiers_have_limits():
    """Every BillingTier enum value has a TIER_LIMITS entry."""
    for tier in BillingTier:
        assert tier in TIER_LIMITS
        info = TIER_LIMITS[tier]
        assert "max_nodes" in info
        assert "price_per_node" in info
        assert "features" in info


def test_community_is_free():
    """Community tier has $0 price and no node cap."""
    info = TIER_LIMITS[BillingTier.COMMUNITY]
    assert info["price_per_node"] == 0
    assert info["max_nodes"] == 999


def test_pro_pricing():
    """Pro tier is $29/node with 10-node cap."""
    info = TIER_LIMITS[BillingTier.PRO]
    assert info["price_per_node"] == 29
    assert info["max_nodes"] == 10


def test_team_pricing():
    """Team tier is $49/node with 50-node cap."""
    info = TIER_LIMITS[BillingTier.TEAM]
    assert info["price_per_node"] == 49
    assert info["max_nodes"] == 50


def test_enterprise_has_all_features():
    """Enterprise tier includes the 'all' wildcard."""
    assert "all" in TIER_LIMITS[BillingTier.ENTERPRISE]["features"]


# ── Feature gating tests ──


def test_community_allows_transport():
    bm = BillingManager(tier=BillingTier.COMMUNITY)
    assert bm.check_feature("transport") is True
    assert bm.check_feature("probes") is True


def test_community_blocks_chief_synthesis():
    bm = BillingManager(tier=BillingTier.COMMUNITY)
    assert bm.check_feature("chief_synthesis") is False
    assert bm.check_feature("fleet_synthesize") is False


def test_pro_allows_synthesis_and_arbiter():
    bm = BillingManager(tier=BillingTier.PRO)
    assert bm.check_feature("fleet_synthesize") is True
    assert bm.check_feature("fleet_arbiter") is True
    assert bm.check_feature("chief_synthesis") is True


def test_pro_blocks_cross_rebuttal():
    bm = BillingManager(tier=BillingTier.PRO)
    assert bm.check_feature("fleet_cross_rebuttal") is False
    assert bm.check_feature("fleet_merge") is False


def test_team_allows_cross_rebuttal_and_merge():
    bm = BillingManager(tier=BillingTier.TEAM)
    assert bm.check_feature("fleet_cross_rebuttal") is True
    assert bm.check_feature("fleet_merge") is True
    assert bm.check_feature("fleet_orchestrate") is True


def test_enterprise_allows_everything():
    bm = BillingManager(tier=BillingTier.ENTERPRISE)
    assert bm.check_feature("fleet_synthesize") is True
    assert bm.check_feature("fleet_cross_rebuttal") is True
    assert bm.check_feature("nonexistent_feature") is True  # 'all' wildcard


# ── Node limit tests ──


def test_community_node_limit():
    bm = BillingManager(tier=BillingTier.COMMUNITY)
    assert bm.check_node_limit(500) is True
    assert bm.check_node_limit(1000) is False


def test_pro_node_limit():
    bm = BillingManager(tier=BillingTier.PRO)
    assert bm.check_node_limit(10) is True
    assert bm.check_node_limit(11) is False


def test_team_node_limit():
    bm = BillingManager(tier=BillingTier.TEAM)
    assert bm.check_node_limit(50) is True
    assert bm.check_node_limit(51) is False


# ── require_feature raises BillingError ──


def test_require_feature_raises_on_community():
    bm = BillingManager(tier=BillingTier.COMMUNITY)
    with pytest.raises(BillingError) as exc_info:
        bm.require_feature("fleet_synthesize")
    assert exc_info.value.current_tier == "community"
    assert exc_info.value.required_tier == "pro"
    assert "Pro" in str(exc_info.value)


def test_require_feature_ok_on_pro():
    bm = BillingManager(tier=BillingTier.PRO)
    bm.require_feature("fleet_synthesize")  # should not raise


# ── require_tool (MCP tool gating) ──


def test_require_tool_gates_synthesize():
    bm = BillingManager(tier=BillingTier.COMMUNITY)
    with pytest.raises(BillingError):
        bm.require_tool("fleet_synthesize")


def test_require_tool_allows_ungated():
    """Tools not in TOOL_FEATURE_MAP are always allowed."""
    bm = BillingManager(tier=BillingTier.COMMUNITY)
    bm.require_tool("fleet_probe")  # not gated, should not raise
    bm.require_tool("fleet_status")


def test_require_tool_cross_rebuttal_needs_team():
    bm = BillingManager(tier=BillingTier.PRO)
    with pytest.raises(BillingError) as exc_info:
        bm.require_tool("fleet_cross_rebuttal")
    assert exc_info.value.required_tier == "team"


# ── Usage tracking ──


def test_usage_report_empty():
    bm = BillingManager(tier=BillingTier.PRO)
    report = bm.usage_report()
    assert report["tier"] == "pro"
    assert report["total_events"] == 0
    assert report["usage_counts"] == {}


def test_usage_report_with_events():
    bm = BillingManager(tier=BillingTier.PRO)
    bm.record_usage("fleet_synthesize", node_id="mac1")
    bm.record_usage("fleet_synthesize", node_id="mac2")
    bm.record_usage("fleet_arbiter", node_id="mac1")
    report = bm.usage_report()
    assert report["total_events"] == 3
    assert report["usage_counts"]["fleet_synthesize"] == 2
    assert report["usage_counts"]["fleet_arbiter"] == 1


# ── Upgrade/downgrade ──


def test_upgrade_community_to_pro():
    bm = BillingManager(tier=BillingTier.COMMUNITY)
    result = bm.upgrade_tier(BillingTier.PRO)
    assert result["direction"] == "upgrade"
    assert result["old_tier"] == "community"
    assert result["new_tier"] == "pro"
    assert result["new_price"] == 29
    assert "chief_synthesis" in result["features_gained"]
    # After upgrade, feature should work
    assert bm.check_feature("fleet_synthesize") is True


def test_downgrade_pro_to_community():
    bm = BillingManager(tier=BillingTier.PRO)
    result = bm.upgrade_tier(BillingTier.COMMUNITY)
    assert result["direction"] == "downgrade"
    assert "chief_synthesis" in result["features_lost"]
    assert bm.check_feature("fleet_synthesize") is False


def test_same_tier_no_change():
    bm = BillingManager(tier=BillingTier.PRO)
    result = bm.upgrade_tier(BillingTier.PRO)
    assert result["direction"] == "no_change"


# ── tier_for_feature ──


def test_tier_for_feature_transport():
    bm = BillingManager()
    assert bm.tier_for_feature("transport") == "community"


def test_tier_for_feature_synthesis():
    bm = BillingManager()
    assert bm.tier_for_feature("fleet_synthesize") == "pro"


def test_tier_for_feature_cross_rebuttal():
    bm = BillingManager()
    assert bm.tier_for_feature("fleet_cross_rebuttal") == "team"


# ── Singleton ──


def test_singleton_get_billing():
    reset_billing()
    b1 = get_billing(BillingTier.PRO)
    b2 = get_billing()
    assert b1 is b2
    assert b1.tier == BillingTier.PRO
    reset_billing()


def test_singleton_tier_update():
    reset_billing()
    b = get_billing(BillingTier.COMMUNITY)
    assert b.tier == BillingTier.COMMUNITY
    b2 = get_billing(BillingTier.TEAM)
    assert b2.tier == BillingTier.TEAM
    assert b is b2  # same object, tier updated
    reset_billing()
