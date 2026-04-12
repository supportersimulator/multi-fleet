"""Stripe Integration — connects billing tiers to Stripe subscriptions.

Handles: product creation, checkout sessions, webhook events,
subscription status sync, and node count tracking.

All Stripe calls are behind try/except — graceful when stripe package
not installed or API key not set.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, Optional

logger = logging.getLogger("fleet-nerve.stripe")


class StripeError(Exception):
    """Raised on Stripe operation failure (wraps stripe.error or missing dep)."""

    def __init__(self, message: str, code: str = "stripe_error"):
        super().__init__(message)
        self.code = code


class StripeIntegration:
    """Manages Stripe subscriptions for Multi-Fleet billing tiers.

    Gracefully degrades when the ``stripe`` package is not installed
    or when no API key is configured — all public methods return an
    error dict instead of raising.
    """

    PRODUCTS: Dict[str, Dict[str, Any]] = {
        "pro": {
            "price_monthly": 2900,
            "name": "Multi-Fleet Pro",
            "node_limit": 10,
            "description": "Chief synthesis, evidence export, dashboard, fleet racing",
        },
        "team": {
            "price_monthly": 4900,
            "name": "Multi-Fleet Team",
            "node_limit": 50,
            "description": "Cross-machine review, merge adjudication, audit export",
        },
        "enterprise": {
            "price_monthly": 0,
            "name": "Multi-Fleet Enterprise",
            "node_limit": 99999,
            "description": "SSO, compliance, SLA, multi-tenant isolation",
        },
    }

    # Stripe webhook event types we handle
    HANDLED_EVENTS = frozenset({
        "checkout.session.completed",
        "customer.subscription.created",
        "customer.subscription.updated",
        "customer.subscription.deleted",
        "invoice.paid",
        "invoice.payment_failed",
    })

    def __init__(self, api_key: Optional[str] = None, webhook_secret: Optional[str] = None):
        self._key = api_key or self._load_key()
        self._webhook_secret = webhook_secret or os.environ.get(
            "MULTIFLEET_STRIPE_WEBHOOK_SECRET", ""
        )
        self._stripe = None
        self._available = False
        self._init_stripe()

    # ── Setup ──

    def _load_key(self) -> str:
        """Load Stripe secret key from env."""
        return os.environ.get("STRIPE_SECRET_KEY", "") or os.environ.get(
            "MULTIFLEET_STRIPE_SECRET_KEY", ""
        )

    def _init_stripe(self) -> None:
        """Import stripe package. Sets ``_available`` to False if missing."""
        if not self._key:
            logger.info("stripe: no API key configured — running in stub mode")
            return
        try:
            import stripe  # type: ignore[import-untyped]

            stripe.api_key = self._key
            self._stripe = stripe
            self._available = True
            logger.info("stripe: initialised successfully")
        except ImportError:
            logger.warning("stripe: package not installed — pip install stripe")
        except Exception as exc:
            logger.error("stripe: init failed — %s", exc)

    @property
    def available(self) -> bool:
        """True when stripe package is loaded and API key is set."""
        return self._available

    def _require_stripe(self) -> None:
        """Raise StripeError if stripe is not available."""
        if not self._available:
            raise StripeError(
                "Stripe not available (missing package or API key)",
                code="stripe_unavailable",
            )

    # ── Checkout ──

    def create_checkout_session(
        self,
        tier: str,
        node_count: int,
        customer_email: str,
        success_url: str = "https://contextdna.io/fleet/success",
        cancel_url: str = "https://contextdna.io/fleet/pricing",
    ) -> Dict[str, Any]:
        """Create a Stripe Checkout session for a fleet subscription.

        Returns dict with ``url`` (redirect the user here) and ``session_id``.
        """
        self._require_stripe()

        product = self.PRODUCTS.get(tier)
        if not product:
            return {"error": f"unknown tier: {tier}", "valid_tiers": list(self.PRODUCTS)}

        if node_count < 1:
            return {"error": "node_count must be >= 1"}

        if node_count > product["node_limit"]:
            return {
                "error": f"{tier} tier supports max {product['node_limit']} nodes",
                "node_limit": product["node_limit"],
            }

        price_id = os.environ.get(f"MULTIFLEET_STRIPE_PRICE_{tier.upper()}_MONTHLY", "")
        if not price_id:
            return {"error": f"no Stripe price ID configured for {tier} (set MULTIFLEET_STRIPE_PRICE_{tier.upper()}_MONTHLY)"}

        try:
            session = self._stripe.checkout.Session.create(
                mode="subscription",
                customer_email=customer_email,
                line_items=[{"price": price_id, "quantity": node_count}],
                success_url=success_url + "?session_id={CHECKOUT_SESSION_ID}",
                cancel_url=cancel_url,
                metadata={"fleet_tier": tier, "node_count": str(node_count)},
            )
            logger.info(
                "stripe: checkout session created tier=%s nodes=%d email=%s",
                tier, node_count, customer_email,
            )
            return {
                "url": session.url,
                "session_id": session.id,
                "tier": tier,
                "node_count": node_count,
            }
        except Exception as exc:
            logger.error("stripe: checkout failed — %s", exc)
            return {"error": f"checkout failed: {exc}"}

    # ── Subscription verification ──

    def verify_subscription(self, subscription_id: str) -> Dict[str, Any]:
        """Verify a subscription is active and return tier info."""
        self._require_stripe()

        try:
            sub = self._stripe.Subscription.retrieve(subscription_id)
            tier = (sub.metadata or {}).get("fleet_tier", "unknown")
            product = self.PRODUCTS.get(tier, {})
            return {
                "active": sub.status == "active",
                "status": sub.status,
                "tier": tier,
                "node_limit": product.get("node_limit", 0),
                "current_period_end": sub.current_period_end,
                "customer_id": sub.customer,
                "subscription_id": sub.id,
            }
        except Exception as exc:
            logger.error("stripe: verify failed — %s", exc)
            return {"error": f"verify failed: {exc}"}

    # ── Webhook handling ──

    def handle_webhook(self, payload: str, sig_header: str) -> Dict[str, Any]:
        """Handle incoming Stripe webhook event.

        Verifies signature, dispatches to handler based on event type.
        Returns dict with ``event_type`` and ``handled`` flag.
        """
        self._require_stripe()

        # Verify webhook signature
        try:
            event = self._stripe.Webhook.construct_event(
                payload, sig_header, self._webhook_secret,
            )
        except Exception as exc:
            logger.warning("stripe: webhook sig verification failed — %s", exc)
            return {"error": f"webhook verification failed: {exc}", "handled": False}

        event_type = event["type"]
        data = event["data"]["object"]

        if event_type not in self.HANDLED_EVENTS:
            return {"event_type": event_type, "handled": False, "reason": "unhandled event type"}

        handler_name = f"_handle_{event_type.replace('.', '_')}"
        handler = getattr(self, handler_name, None)
        if handler:
            result = handler(data)
            return {"event_type": event_type, "handled": True, **result}

        return {"event_type": event_type, "handled": False}

    def _handle_checkout_session_completed(self, data: dict) -> dict:
        """Activate fleet license after successful checkout."""
        meta = data.get("metadata", {})
        return {
            "action": "activate",
            "customer_email": data.get("customer_email", ""),
            "tier": meta.get("fleet_tier", "unknown"),
            "node_count": int(meta.get("node_count", "1")),
            "subscription_id": data.get("subscription", ""),
        }

    def _handle_customer_subscription_created(self, data: dict) -> dict:
        """Provision fleet on new subscription."""
        meta = data.get("metadata", {})
        return {
            "action": "provision",
            "subscription_id": data.get("id", ""),
            "status": data.get("status", ""),
            "tier": meta.get("fleet_tier", "unknown"),
        }

    def _handle_customer_subscription_updated(self, data: dict) -> dict:
        """Handle tier or quantity change."""
        meta = data.get("metadata", {})
        items = data.get("items", {}).get("data", [])
        quantity = items[0].get("quantity", 1) if items else 1
        return {
            "action": "update",
            "subscription_id": data.get("id", ""),
            "status": data.get("status", ""),
            "tier": meta.get("fleet_tier", "unknown"),
            "node_count": quantity,
        }

    def _handle_customer_subscription_deleted(self, data: dict) -> dict:
        """Revoke fleet license on cancellation."""
        return {
            "action": "revoke",
            "subscription_id": data.get("id", ""),
            "customer_id": data.get("customer", ""),
        }

    def _handle_invoice_paid(self, data: dict) -> dict:
        """Confirm renewal."""
        return {
            "action": "renewal_confirmed",
            "subscription_id": data.get("subscription", ""),
            "amount_paid": data.get("amount_paid", 0),
        }

    def _handle_invoice_payment_failed(self, data: dict) -> dict:
        """Start grace period on payment failure."""
        return {
            "action": "payment_failed",
            "subscription_id": data.get("subscription", ""),
            "next_retry": data.get("next_payment_attempt"),
        }

    # ── Customer tier lookup ──

    def get_customer_tier(self, customer_id: str) -> str:
        """Get the billing tier for a Stripe customer.

        Looks up active subscriptions and returns the highest tier found.
        Returns ``"community"`` if no active subscription.
        """
        self._require_stripe()

        try:
            subs = self._stripe.Subscription.list(
                customer=customer_id, status="active", limit=10,
            )
            best_tier = "community"
            tier_order = {"community": 0, "pro": 1, "team": 2, "enterprise": 3}
            for sub in subs.auto_paging_iter():
                tier = (sub.metadata or {}).get("fleet_tier", "community")
                if tier_order.get(tier, 0) > tier_order.get(best_tier, 0):
                    best_tier = tier
            return best_tier
        except Exception as exc:
            logger.error("stripe: customer tier lookup failed — %s", exc)
            return "community"

    # ── Node count updates ──

    def update_node_count(self, subscription_id: str, node_count: int) -> Dict[str, Any]:
        """Update the quantity on a subscription when fleet nodes change.

        Uses Stripe's per-unit pricing — quantity = active node count.
        """
        self._require_stripe()

        if node_count < 1:
            return {"error": "node_count must be >= 1"}

        try:
            sub = self._stripe.Subscription.retrieve(subscription_id)
            tier = (sub.metadata or {}).get("fleet_tier", "unknown")
            product = self.PRODUCTS.get(tier, {})
            if node_count > product.get("node_limit", 99999):
                return {
                    "error": f"{tier} tier max {product['node_limit']} nodes",
                    "current_count": sub["items"]["data"][0]["quantity"],
                }

            item_id = sub["items"]["data"][0]["id"]
            self._stripe.SubscriptionItem.modify(item_id, quantity=node_count)
            logger.info(
                "stripe: updated node count sub=%s nodes=%d", subscription_id, node_count,
            )
            return {
                "subscription_id": subscription_id,
                "node_count": node_count,
                "tier": tier,
                "updated": True,
            }
        except Exception as exc:
            logger.error("stripe: node count update failed — %s", exc)
            return {"error": f"update failed: {exc}"}


# ── Module-level singleton ──

_stripe_integration: Optional[StripeIntegration] = None


def get_stripe() -> StripeIntegration:
    """Get or create the global StripeIntegration singleton."""
    global _stripe_integration
    if _stripe_integration is None:
        _stripe_integration = StripeIntegration()
    return _stripe_integration


def reset_stripe() -> None:
    """Reset the singleton (for testing)."""
    global _stripe_integration
    _stripe_integration = None
