"""Tests for Stripe integration — all Stripe API calls are mocked."""

import json
import os
from unittest.mock import MagicMock, patch

import pytest

from multifleet.stripe_integration import (
    StripeError,
    StripeIntegration,
    get_stripe,
    reset_stripe,
)
from multifleet.billing import BillingManager, BillingTier


# ── Fixtures ──


@pytest.fixture(autouse=True)
def _reset():
    """Reset singleton between tests."""
    reset_stripe()
    yield
    reset_stripe()


def _make_si(available: bool = True) -> StripeIntegration:
    """Create a StripeIntegration with mocked stripe module."""
    si = StripeIntegration.__new__(StripeIntegration)
    si._key = "sk_test_fake"
    si._webhook_secret = "whsec_test"
    si._stripe = MagicMock()
    si._available = available
    return si


# ── Init / availability ──


def test_unavailable_when_no_key():
    """Without API key, stripe is not available."""
    with patch.dict(os.environ, {}, clear=True):
        si = StripeIntegration(api_key="")
    assert si.available is False


def test_unavailable_when_import_fails():
    """When stripe package not installed, graceful fallback."""
    with patch.dict(os.environ, {"STRIPE_SECRET_KEY": "sk_test_x"}):
        with patch("builtins.__import__", side_effect=ImportError("no stripe")):
            si = StripeIntegration(api_key="sk_test_x")
    assert si.available is False


def test_products_defined():
    """All expected tiers have product definitions."""
    si = _make_si()
    assert "pro" in si.PRODUCTS
    assert "team" in si.PRODUCTS
    assert "enterprise" in si.PRODUCTS
    assert si.PRODUCTS["pro"]["price_monthly"] == 2900
    assert si.PRODUCTS["team"]["price_monthly"] == 4900


# ── Checkout ──


def test_checkout_unknown_tier():
    si = _make_si()
    result = si.create_checkout_session("gold", 1, "a@b.com")
    assert "error" in result
    assert "unknown tier" in result["error"]


def test_checkout_zero_nodes():
    si = _make_si()
    result = si.create_checkout_session("pro", 0, "a@b.com")
    assert "error" in result
    assert "node_count" in result["error"]


def test_checkout_exceeds_node_limit():
    si = _make_si()
    result = si.create_checkout_session("pro", 15, "a@b.com")
    assert "error" in result
    assert "10" in result["error"]


def test_checkout_missing_price_id():
    """When env var for price ID is not set, return helpful error."""
    si = _make_si()
    with patch.dict(os.environ, {}, clear=True):
        result = si.create_checkout_session("pro", 3, "user@test.com")
    assert "error" in result
    assert "MULTIFLEET_STRIPE_PRICE_PRO_MONTHLY" in result["error"]


def test_checkout_success():
    si = _make_si()
    mock_session = MagicMock()
    mock_session.url = "https://checkout.stripe.com/pay/cs_test_123"
    mock_session.id = "cs_test_123"
    si._stripe.checkout.Session.create.return_value = mock_session

    with patch.dict(os.environ, {"MULTIFLEET_STRIPE_PRICE_PRO_MONTHLY": "price_pro_m"}):
        result = si.create_checkout_session("pro", 3, "user@test.com")

    assert result["url"] == "https://checkout.stripe.com/pay/cs_test_123"
    assert result["session_id"] == "cs_test_123"
    assert result["tier"] == "pro"
    assert result["node_count"] == 3
    si._stripe.checkout.Session.create.assert_called_once()


# ── Verify subscription ──


def test_verify_active_subscription():
    si = _make_si()
    mock_sub = MagicMock()
    mock_sub.status = "active"
    mock_sub.metadata = {"fleet_tier": "pro"}
    mock_sub.current_period_end = 1700000000
    mock_sub.customer = "cus_123"
    mock_sub.id = "sub_123"
    si._stripe.Subscription.retrieve.return_value = mock_sub

    result = si.verify_subscription("sub_123")
    assert result["active"] is True
    assert result["tier"] == "pro"
    assert result["node_limit"] == 10


def test_verify_inactive_subscription():
    si = _make_si()
    mock_sub = MagicMock()
    mock_sub.status = "canceled"
    mock_sub.metadata = {"fleet_tier": "pro"}
    mock_sub.current_period_end = 1700000000
    mock_sub.customer = "cus_123"
    mock_sub.id = "sub_123"
    si._stripe.Subscription.retrieve.return_value = mock_sub

    result = si.verify_subscription("sub_123")
    assert result["active"] is False
    assert result["status"] == "canceled"


# ── Webhook handling ──


def test_webhook_unhandled_event():
    si = _make_si()
    event = {"type": "charge.succeeded", "data": {"object": {}}}
    si._stripe.Webhook.construct_event.return_value = event

    result = si.handle_webhook("{}", "sig_header")
    assert result["handled"] is False
    assert result["event_type"] == "charge.succeeded"


def test_webhook_checkout_completed():
    si = _make_si()
    event = {
        "type": "checkout.session.completed",
        "data": {"object": {
            "customer_email": "user@test.com",
            "subscription": "sub_abc",
            "metadata": {"fleet_tier": "team", "node_count": "5"},
        }},
    }
    si._stripe.Webhook.construct_event.return_value = event

    result = si.handle_webhook("{}", "sig_header")
    assert result["handled"] is True
    assert result["action"] == "activate"
    assert result["tier"] == "team"
    assert result["node_count"] == 5


def test_webhook_subscription_deleted():
    si = _make_si()
    event = {
        "type": "customer.subscription.deleted",
        "data": {"object": {"id": "sub_del", "customer": "cus_456"}},
    }
    si._stripe.Webhook.construct_event.return_value = event

    result = si.handle_webhook("{}", "sig_header")
    assert result["handled"] is True
    assert result["action"] == "revoke"
    assert result["subscription_id"] == "sub_del"


# ── Customer tier ──


def test_get_customer_tier_with_active_sub():
    si = _make_si()
    mock_sub = MagicMock()
    mock_sub.metadata = {"fleet_tier": "team"}
    mock_subs = MagicMock()
    mock_subs.auto_paging_iter.return_value = [mock_sub]
    si._stripe.Subscription.list.return_value = mock_subs

    tier = si.get_customer_tier("cus_123")
    assert tier == "team"


def test_get_customer_tier_no_subs():
    si = _make_si()
    mock_subs = MagicMock()
    mock_subs.auto_paging_iter.return_value = []
    si._stripe.Subscription.list.return_value = mock_subs

    tier = si.get_customer_tier("cus_empty")
    assert tier == "community"


# ── Node count update ──


def test_update_node_count_success():
    si = _make_si()
    mock_sub = MagicMock()
    mock_sub.metadata = {"fleet_tier": "pro"}
    mock_sub.__getitem__ = lambda self, k: {"items": {"data": [{"id": "si_1", "quantity": 2}]}}[k]
    si._stripe.Subscription.retrieve.return_value = mock_sub

    result = si.update_node_count("sub_123", 5)
    assert result["updated"] is True
    assert result["node_count"] == 5
    si._stripe.SubscriptionItem.modify.assert_called_once_with("si_1", quantity=5)


def test_update_node_count_zero():
    si = _make_si()
    result = si.update_node_count("sub_123", 0)
    assert "error" in result
    assert "node_count" in result["error"]


# ── Stripe unavailable guard ──


def test_require_stripe_raises_when_unavailable():
    si = _make_si(available=False)
    with pytest.raises(StripeError) as exc_info:
        si.create_checkout_session("pro", 1, "a@b.com")
    assert exc_info.value.code == "stripe_unavailable"


# ── BillingManager.sync_from_stripe ──


def test_billing_sync_from_stripe():
    """BillingManager can sync tier from Stripe subscription."""
    bm = BillingManager(tier=BillingTier.COMMUNITY)

    mock_si = MagicMock()
    mock_si.available = True
    mock_si.verify_subscription.return_value = {
        "active": True,
        "tier": "pro",
        "node_limit": 10,
        "status": "active",
    }

    with patch("multifleet.stripe_integration.get_stripe", return_value=mock_si):
        result = bm.sync_from_stripe("sub_xyz")

    assert result["active"] is True
    assert bm.tier == BillingTier.PRO
    assert result["tier_change"]["direction"] == "upgrade"
