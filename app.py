from flask import Flask, request, jsonify, render_template, redirect, make_response, session
from flask_cors import CORS
import threading
import os
import rmp
import reddit_scraper
import payment
import auth

app = Flask(__name__)
CORS(app)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-in-prod")

# Initialise the user DB at startup
auth.init_db()


def _warmup():
    try:
        rmp.build_su_professor_index()
    except Exception:
        pass

threading.Thread(target=_warmup, daemon=True).start()

# Auto-refresh all active school indexes every 6 hours in the background
rmp.start_auto_refresh()


# ── Premium helper ────────────────────────────────────────────────────────────

def _is_premium():
    """True if the current request has a valid premium entitlement.

    Checks the logged-in user's DB subscription first, then falls back to
    the legacy JWT cookie so users who paid without an account still work.
    """
    user = auth.current_user()
    if user:
        # Lazily re-check Stripe when the stored period end is within 2 days
        import time
        expires_at = user.get("subscription_expires_at") or 0
        if auth.is_active(user) and float(expires_at) - time.time() < 2 * 24 * 3600:
            sub_id = user.get("stripe_subscription_id")
            if sub_id:
                active, new_end = payment._check_stripe_subscription(sub_id)
                new_status = "active" if active else "inactive"
                auth.update_subscription(
                    user["id"],
                    user.get("stripe_customer_id"),
                    sub_id,
                    new_status,
                    new_end,
                )
                # Refresh user from DB
                user = auth.get_user_by_id(user["id"])
        if user and auth.is_active(user):
            return True
    return payment.is_premium(request)


# ── Owner bypass ──────────────────────────────────────────────────────────────

BYPASS_KEY = os.environ.get("BYPASS_KEY", "")

@app.route("/access/<key>")
def bypass(key):
    if not BYPASS_KEY or key != BYPASS_KEY:
        return "", 404
    resp = make_response(redirect("/"))
    payment.issue_access_cookie(resp, "owner-bypass")
    return resp


# ── Payment routes ────────────────────────────────────────────────────────────

@app.route("/pay")
def pay_page():
    return render_template("pay.html")

@app.route("/create-checkout")
def create_checkout():
    base_url = request.host_url.rstrip("/")
    user = auth.current_user()
    try:
        session_kwargs = {}
        if user:
            session_kwargs["client_reference_id"] = str(user["id"])
            session_kwargs["customer_email"] = user["email"]
        stripe_session = payment.create_checkout_session(base_url, **session_kwargs)
        return redirect(stripe_session.url)
    except Exception as e:
        return redirect(f"/pay?error={str(e)}")

@app.route("/payment-success")
def payment_success():
    session_id = request.args.get("session_id", "")
    if not session_id:
        return redirect("/pay?error=Payment+could+not+be+verified")
    try:
        sub_id, customer_id, period_end = payment.get_subscription_from_session(session_id)
    except Exception:
        return redirect("/pay?error=Payment+verification+failed")

    user = auth.current_user()
    if user:
        auth.update_subscription(user["id"], customer_id, sub_id, "active", period_end)
        return redirect("/account")

    # Fallback: legacy JWT cookie for users without accounts
    resp = make_response(redirect("/"))
    payment.issue_access_cookie(resp, session_id,
                                subscription_id=sub_id,
                                customer_id=customer_id,
                                expires_at=period_end)
    return resp


@app.route("/manage-billing")
def manage_billing():
    # Prefer DB-stored customer ID for logged-in users
    user = auth.current_user()
    if user and user.get("stripe_customer_id"):
        customer_id = user["stripe_customer_id"]
    else:
        customer_id = payment.get_customer_id(request)

    if not customer_id:
        return redirect("/pay")
    base_url = request.host_url.rstrip("/")
    try:
        url = payment.create_portal_session(customer_id, base_url)
        return redirect(url)
    except Exception as e:
        return redirect(f"/?error={str(e)}")


# ── Auth routes ───────────────────────────────────────────────────────────────

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")
        user = auth.verify_password(email, password)
        if not user:
            return render_template("login.html", error="Invalid email or password.")
        auth.login_user(user)
        return redirect("/account")
    return render_template("login.html", error=None)


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")
        if not email or not password:
            return render_template("signup.html", error="Email and password are required.")
        if password != confirm:
            return render_template("signup.html", error="Passwords do not match.")
        if len(password) < 6:
            return render_template("signup.html", error="Password must be at least 6 characters.")
        user = auth.create_user(email, password)
        if user is None:
            return render_template("signup.html", error="An account with that email already exists.")
        auth.login_user(user)
        return redirect("/pay")
    return render_template("signup.html", error=None)


@app.route("/logout")
def logout():
    auth.logout_user()
    return redirect("/")


@app.route("/account")
def account():
    user = auth.current_user()
    if not user:
        return redirect("/login")
    premium = _is_premium()
    return render_template("account.html", user=user, premium=premium)


# ── Free routes ───────────────────────────────────────────────────────────────

@app.route("/")
def index():
    premium = _is_premium()
    user = auth.current_user()
    return render_template("index.html", premium=premium, user=user)

@app.route("/api/schools")
def school_search():
    q = request.args.get("q", "").strip()
    if not q or len(q) < 2:
        return jsonify({"error": "Query too short"}), 400
    try:
        schools = rmp.search_schools(q)
        return jsonify({"results": schools[:10]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/search")
def search():
    if not _is_premium():
        return jsonify({"error": "premium_required"}), 403
    name      = request.args.get("name", "").strip()
    school_id = request.args.get("school_id", "").strip() or None
    if not name or len(name) < 2:
        return jsonify({"error": "Please enter a professor name"}), 400
    try:
        professors = rmp.search_professors(name, school_id)
        return jsonify({"results": professors})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/index-status")
def index_status():
    school_id = request.args.get("school_id", "").strip() or None
    return jsonify({"cached": rmp.is_cache_fresh(school_id)})


# ── Free routes (course search) ───────────────────────────────────────────────

@app.route("/api/course")
def course_search():
    course    = request.args.get("course", "").strip()
    school_id = request.args.get("school_id", "").strip() or None
    if not course or len(course) < 2:
        return jsonify({"error": "Please enter a course name or code"}), 400
    try:
        ranked      = rmp.search_by_course(course, school_id)
        cache_fresh = rmp.is_cache_fresh(school_id)
        return jsonify({"results": ranked, "course": course, "fromCache": cache_fresh})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/professor/<professor_id>")
def professor_detail(professor_id):
    try:
        data = rmp.get_professor_ratings(professor_id)
        if not data:
            return jsonify({"error": "Professor not found"}), 404

        ratings_list = [e["node"] for e in (data.get("ratings") or {}).get("edges", [])]
        full_name    = f"{data.get('firstName','')} {data.get('lastName','')}".strip()
        rmp_url      = rmp.build_rmp_url(professor_id)
        prof_base    = {
            "id": data.get("id"), "name": full_name,
            "department": data.get("department"),
            "avgRating": data.get("avgRating"),
            "avgDifficulty": data.get("avgDifficulty"),
            "numRatings": data.get("numRatings"),
            "wouldTakeAgainPercent": data.get("wouldTakeAgainPercent"),
            "rmpUrl": rmp_url,
            "courseCodes": data.get("courseCodes", []),
        }
        premium = _is_premium()
        if not premium:
            return jsonify({"professor": prof_base, "ratings": ratings_list[:3],
                            "easyA": None, "examMentions": [], "redditPosts": [], "isPremium": False})

        easy_a       = rmp.compute_easy_a(data)
        exam_mentions = rmp.parse_exam_info(ratings_list)
        reddit_posts  = reddit_scraper.search_reddit_multi(full_name)
        return jsonify({"professor": prof_base, "ratings": ratings_list,
                        "easyA": easy_a, "examMentions": exam_mentions,
                        "redditPosts": reddit_posts, "isPremium": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(debug=True, port=5050)
