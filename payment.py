import os
import jwt
import stripe
import datetime
import functools
from flask import request, redirect, make_response

stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
JWT_SECRET = os.environ.get("JWT_SECRET", "dev-secret-change-in-prod")
COOKIE_NAME = "su_access"
COOKIE_DAYS = 365 * 10  # 10 years = permanent


def _is_paid(req):
    token = req.cookies.get(COOKIE_NAME)
    if not token:
        return False
    try:
        jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        return True
    except Exception:
        return False


def require_payment(f):
    """Decorator — redirects to /pay if no valid access cookie."""
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        if not _is_paid(request):
            return redirect("/pay")
        return f(*args, **kwargs)
    return wrapper


def issue_access_cookie(response, session_id: str):
    """Mint a signed JWT and set it as a permanent cookie."""
    payload = {
        "paid": True,
        "session": session_id,
        "iat": datetime.datetime.utcnow(),
    }
    token = jwt.encode(payload, JWT_SECRET, algorithm="HS256")
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=60 * 60 * 24 * COOKIE_DAYS,
        httponly=True,
        secure=True,
        samesite="Lax",
    )
    return response


def create_checkout_session(base_url: str):
    """Create a Stripe Checkout session for a $2 one-time payment."""
    session = stripe.checkout.Session.create(
        payment_method_types=["card"],
        line_items=[{
            "price_data": {
                "currency": "usd",
                "unit_amount": 200,  # $2.00
                "product_data": {
                    "name": "SU Professor Finder — Lifetime Access",
                    "description": "One-time $2 payment for unlimited access to Syracuse University professor and course reviews.",
                },
            },
            "quantity": 1,
        }],
        mode="payment",
        success_url=f"{base_url}/payment-success?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{base_url}/pay",
    )
    return session


def verify_session(session_id: str):
    """Confirm Stripe payment was actually completed."""
    session = stripe.checkout.Session.retrieve(session_id)
    return session.payment_status == "paid"
