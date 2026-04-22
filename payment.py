import os
import jwt
import stripe
import datetime
import functools
from flask import request, redirect, make_response

stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
JWT_SECRET  = os.environ.get("JWT_SECRET", "dev-secret-change-in-prod")
COOKIE_NAME = "su_access"

# $2.00 / month
MONTHLY_PRICE_CENTS = 200


# ── Internal helpers ──────────────────────────────────────────────────────────

def _decode(req):
    """Decode the JWT cookie. Returns payload dict or None."""
    token = req.cookies.get(COOKIE_NAME)
    if not token:
        return None
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
    except Exception:
        return None


def _check_stripe_subscription(subscription_id: str):
    """
    Ask Stripe if the subscription is still active.
    Returns (is_active, current_period_end_timestamp).
    """
    try:
        sub = stripe.Subscription.retrieve(subscription_id)
        active = sub.status in ("active", "trialing")
        return active, sub.current_period_end
    except Exception:
        return False, 0


# ── Public API ────────────────────────────────────────────────────────────────

def is_premium(req) -> bool:
    """
    Returns True if the request has a valid, active subscription.

    Cookie stores expires_at (Stripe's current_period_end). When the
    cookie is within 2 days of expiry we re-check with Stripe so the
    cookie stays fresh without making the user do anything.
    """
    payload = _decode(req)
    if not payload:
        return False

    # Legacy one-time-payment cookies (no expires_at) are still honoured.
    if "expires_at" not in payload:
        return True

    now = datetime.datetime.utcnow().timestamp()
    expires_at = payload.get("expires_at", 0)

    # Clearly still valid — skip the Stripe call.
    if expires_at > now + 2 * 24 * 3600:
        return True

    # Near expiry or past — verify with Stripe.
    sub_id = payload.get("subscription_id")
    if not sub_id:
        return False

    active, _ = _check_stripe_subscription(sub_id)
    return active


def issue_access_cookie(resp, session_id: str,
                        subscription_id: str = None,
                        customer_id: str = None,
                        expires_at: float = None):
    """
    Mint a signed JWT cookie. For subscriptions pass the extra fields
    so is_premium() can re-verify with Stripe when the period ends.
    Cookie lasts 35 days — slightly over one billing cycle.
    """
    payload = {
        "paid": True,
        "session": session_id,
        "iat": datetime.datetime.utcnow().timestamp(),
    }
    if subscription_id:
        payload["subscription_id"] = subscription_id
    if customer_id:
        payload["customer_id"] = customer_id
    if expires_at:
        payload["expires_at"] = float(expires_at)

    token = jwt.encode(payload, JWT_SECRET, algorithm="HS256")
    resp.set_cookie(
        COOKIE_NAME, token,
        max_age=60 * 60 * 24 * 35,
        httponly=True, secure=True, samesite="Lax",
    )
    return resp


def create_checkout_session(base_url: str):
    """Create a Stripe Checkout session for a $2/month subscription."""
    return stripe.checkout.Session.create(
        payment_method_types=["card"],
        mode="subscription",
        line_items=[{
            "price_data": {
                "currency": "usd",
                "recurring": {"interval": "month"},
                "unit_amount": MONTHLY_PRICE_CENTS,
                "product_data": {
                    "name": "SU Professor Finder — Premium",
                    "description": (
                        "$2/month · Course search, Easy A detector, "
                        "all reviews, grade distribution & more."
                    ),
                },
            },
            "quantity": 1,
        }],
        success_url=f"{base_url}/payment-success?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{base_url}/pay",
        customer_creation="always",
    )


def get_subscription_from_session(session_id: str):
    """
    After a successful Stripe Checkout, retrieve subscription details.
    Returns (subscription_id, customer_id, current_period_end).
    """
    session = stripe.checkout.Session.retrieve(
        session_id, expand=["subscription"]
    )
    sub = session.subscription
    if not sub:
        raise ValueError("No subscription on this session")
    return sub.id, session.customer, sub.current_period_end


def create_portal_session(customer_id: str, base_url: str) -> str:
    """
    Generate a Stripe Billing Portal URL so the subscriber can
    manage or cancel their subscription.
    """
    portal = stripe.billing_portal.Session.create(
        customer=customer_id,
        return_url=f"{base_url}/",
    )
    return portal.url


def get_customer_id(req):
    """Pull the Stripe customer_id out of the JWT cookie, if present."""
    payload = _decode(req)
    return (payload or {}).get("customer_id")
