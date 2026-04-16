from flask import Flask, request, jsonify, render_template, redirect, make_response, session
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
import threading
import os
import time
import bcrypt
import rmp
import reddit_scraper
import payment
import auth

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", os.urandom(24))
CORS(app)

limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["200 per hour"],
    storage_uri="memory://",
)

ADMIN_USER      = os.environ.get("ADMIN_USER", "")
ADMIN_PASS_HASH = os.environ.get("ADMIN_PASS_HASH", "")


def _admin_ok():
    return session.get("admin") is True


def _check_admin_creds(username: str, password: str) -> bool:
    if not ADMIN_USER or not ADMIN_PASS_HASH:
        return False
    if username != ADMIN_USER:
        return False
    try:
        return bcrypt.checkpw(password.encode(), ADMIN_PASS_HASH.encode())
    except Exception:
        return False

# Init database on startup
auth.init_db()


def _warmup():
    try:
        rmp.build_su_professor_index()
    except Exception:
        pass

def _self_ping():
    """Ping ourselves every 14 min to prevent Render free-tier sleep."""
    import urllib.request
    time.sleep(60)  # wait for server to be ready first
    while True:
        try:
            host = os.environ.get("RENDER_EXTERNAL_URL", "")
            if host:
                urllib.request.urlopen(f"{host}/ping", timeout=10)
        except Exception:
            pass
        time.sleep(840)  # 14 minutes

threading.Thread(target=_warmup, daemon=True).start()
threading.Thread(target=_self_ping, daemon=True).start()
rmp.start_auto_refresh()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _is_premium(request):
    """Check premium via account OR legacy one-time payment cookie."""
    user = auth.get_current_user(request)
    if user and user.get("premium"):
        return True
    # Refresh from DB in case premium was set after token was issued
    if user:
        db_user = auth.get_user(user["uid"])
        if db_user and db_user.get("premium"):
            return True
    return payment.is_premium(request)


# ── Health / keep-alive ───────────────────────────────────────────────────────

@app.route("/ping")
def ping():
    return jsonify({"ok": True}), 200


# ── Owner bypass ──────────────────────────────────────────────────────────────

BYPASS_KEY = os.environ.get("BYPASS_KEY", "")

@app.route("/access/<key>")
def bypass(key):
    if not BYPASS_KEY or key != BYPASS_KEY:
        return "", 404
    resp = make_response(redirect("/"))
    payment.issue_access_cookie(resp, "owner-bypass")
    return resp


# ── Auth routes ───────────────────────────────────────────────────────────────

@app.route("/signup", methods=["POST"])
def signup():
    data     = request.get_json() or {}
    email    = data.get("email", "").strip()
    password = data.get("password", "")
    user, err = auth.create_user(email, password)
    if err:
        return jsonify({"error": err}), 400
    resp = make_response(jsonify({"ok": True, "email": user["email"], "premium": False}))
    auth.set_auth_cookie(resp, user["id"], user["email"], False)
    return resp


@app.route("/login", methods=["POST"])
def login():
    data     = request.get_json() or {}
    email    = data.get("email", "").strip()
    password = data.get("password", "")
    user     = auth.verify_user(email, password)
    if not user:
        return jsonify({"error": "Incorrect email or password."}), 401
    has_legacy_premium = payment.is_premium(request)
    premium = bool(user.get("premium")) or has_legacy_premium
    # Promote legacy cookie premium to the account permanently
    if has_legacy_premium and not user.get("premium"):
        auth.set_premium(user["id"], "legacy-cookie-promotion")
        premium = True
    resp = make_response(jsonify({"ok": True, "email": user["email"], "premium": premium}))
    auth.set_auth_cookie(resp, user["id"], user["email"], premium)
    return resp


@app.route("/logout", methods=["POST"])
def logout():
    resp = make_response(jsonify({"ok": True}))
    auth.clear_auth_cookie(resp)
    return resp


@app.route("/api/me")
def me():
    user = auth.get_current_user(request)
    if not user:
        return jsonify({"loggedIn": False, "premium": payment.is_premium(request)})
    db_user = auth.get_user(user["uid"])
    premium = bool(db_user and db_user.get("premium")) or payment.is_premium(request)
    return jsonify({"loggedIn": True, "email": user["email"], "premium": premium})


# ── Bookmark routes ───────────────────────────────────────────────────────────

@app.route("/api/bookmarks", methods=["GET"])
def get_bookmarks():
    user = auth.get_current_user(request)
    if not user:
        return jsonify({"error": "Not logged in"}), 401
    return jsonify({"bookmarks": auth.get_bookmarks(user["uid"])})


@app.route("/api/bookmarks", methods=["POST"])
def add_bookmark():
    user = auth.get_current_user(request)
    if not user:
        return jsonify({"error": "Not logged in"}), 401
    data   = request.get_json() or {}
    ref_id = data.get("ref_id", "")
    name   = data.get("name", "")
    dept   = data.get("dept", "")
    rating = data.get("rating")
    extra  = data.get("data", {})
    if not ref_id:
        return jsonify({"error": "Missing ref_id"}), 400
    auth.add_bookmark(user["uid"], ref_id, name, dept, rating, extra)
    return jsonify({"ok": True})


@app.route("/api/bookmarks/<ref_id>", methods=["DELETE"])
def remove_bookmark(ref_id):
    user = auth.get_current_user(request)
    if not user:
        return jsonify({"error": "Not logged in"}), 401
    auth.remove_bookmark(user["uid"], ref_id)
    return jsonify({"ok": True})


# ── Payment routes ────────────────────────────────────────────────────────────

@app.route("/pay")
def pay_page():
    return render_template("pay.html")


@app.route("/create-checkout")
def create_checkout():
    base_url = request.host_url.rstrip("/")
    try:
        session = payment.create_checkout_session(base_url)
        return redirect(session.url)
    except Exception as e:
        return redirect(f"/pay?error={str(e)}")


@app.route("/payment-success")
def payment_success():
    session_id = request.args.get("session_id", "")
    try:
        if not session_id or not payment.verify_session(session_id):
            return redirect("/pay?error=Payment+could+not+be+verified")
    except Exception:
        return redirect("/pay?error=Payment+verification+failed")

    # Mark account as premium if logged in
    user = auth.get_current_user(request)
    resp = make_response(redirect("/"))
    if user:
        auth.set_premium(user["uid"], session_id)
        auth.set_auth_cookie(resp, user["uid"], user["email"], True)
    else:
        payment.issue_access_cookie(resp, session_id)
    return resp


# ── Free routes ───────────────────────────────────────────────────────────────

@app.route("/")
def index():
    user    = auth.get_current_user(request)
    premium = _is_premium(request)
    return render_template("index.html", premium=premium, user=user)


@app.route("/api/schools")
def school_search():
    q = request.args.get("q", "").strip()
    if not q or len(q) < 2:
        return jsonify({"error": "Query too short"}), 400
    try:
        return jsonify({"results": rmp.search_schools(q)[:10]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/search")
@limiter.limit("60 per minute")
def search():
    name      = request.args.get("name", "").strip()
    school_id = request.args.get("school_id", "").strip() or None
    if not name or len(name) < 2:
        return jsonify({"error": "Please enter a professor name"}), 400
    try:
        results  = rmp.search_professors(name, school_id)
        premium  = _is_premium(request)
        if premium:
            return jsonify({"results": results, "truncated": False})
        # Free: show first 3 as teaser, signal there are more
        return jsonify({
            "results":   results[:3],
            "truncated": len(results) > 3,
            "total":     len(results),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/index-status")
def index_status():
    school_id = request.args.get("school_id", "").strip() or None
    return jsonify({"cached": rmp.is_cache_fresh(school_id)})


@app.route("/api/suggest")
@limiter.limit("120 per minute")
def suggest():
    q         = request.args.get("q", "").strip()
    school_id = request.args.get("school_id", "").strip() or None
    if not q or len(q) < 2:
        return jsonify({"results": []})
    try:
        return jsonify({"results": rmp.suggest_professors(q, school_id)})
    except Exception:
        return jsonify({"results": []})


@app.route("/api/departments")
def departments():
    school_id = request.args.get("school_id", "").strip() or None
    try:
        return jsonify({"departments": rmp.get_department_list(school_id)})
    except Exception:
        return jsonify({"departments": []})


@app.route("/api/department")
@limiter.limit("30 per minute")
def department_search():
    dept      = request.args.get("dept", "").strip()
    school_id = request.args.get("school_id", "").strip() or None
    if not dept:
        return jsonify({"error": "Missing dept"}), 400
    premium = _is_premium(request)
    try:
        results = rmp.search_by_department(dept, school_id)
        if not premium:
            return jsonify({"results": results[:5], "truncated": len(results) > 5, "total": len(results)})
        return jsonify({"results": results, "truncated": False, "total": len(results)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Premium routes ────────────────────────────────────────────────────────────

@app.route("/api/trending")
def trending():
    school_id = request.args.get("school_id", "").strip()
    results   = auth.get_trending_professors(school_id, limit=8)
    return jsonify({"trending": results})


@app.route("/api/course")
@limiter.limit("30 per minute")
def course_search():
    course    = request.args.get("course", "").strip()
    school_id = request.args.get("school_id", "").strip() or None
    if not course or len(course) < 2:
        return jsonify({"error": "Please enter a course"}), 400
    try:
        ranked      = rmp.search_by_course(course, school_id)
        cache_fresh = rmp.is_cache_fresh(school_id)
        return jsonify({"results": ranked, "course": course, "fromCache": cache_fresh})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/professor/<professor_id>")
@limiter.limit("120 per minute")
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

        premium = _is_premium(request)

        # Track view for trending
        school_id = request.args.get("school_id", "")
        auth.record_professor_view(
            professor_id, school_id, full_name,
            data.get("department", ""), data.get("avgRating")
        )

        # Check if this professor is bookmarked by the current user
        user = auth.get_current_user(request)
        bookmarked = auth.is_bookmarked(user["uid"], professor_id) if user else False

        if not premium:
            return jsonify({
                "professor": prof_base, "ratings": ratings_list[:3],
                "easyA": None, "examMentions": [], "redditPosts": [],
                "isPremium": False, "bookmarked": bookmarked,
            })

        easy_a        = rmp.compute_easy_a(data)
        exam_mentions = rmp.parse_exam_info(ratings_list)
        reddit_posts  = reddit_scraper.search_reddit_multi(full_name)

        return jsonify({
            "professor": prof_base, "ratings": ratings_list,
            "easyA": easy_a, "examMentions": exam_mentions,
            "redditPosts": reddit_posts, "isPremium": True,
            "bookmarked": bookmarked,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Admin routes ──────────────────────────────────────────────────────────────

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    error = ""
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if _check_admin_creds(username, password):
            session["admin"] = True
            return redirect("/admin")
        error = "Invalid credentials."
    return render_template("admin_login.html", error=error)


@app.route("/admin/logout")
def admin_logout():
    session.pop("admin", None)
    return redirect("/admin/login")


@app.route("/admin")
def admin_dashboard():
    if not _admin_ok():
        return redirect("/admin/login")
    stats = {}
    users = []
    reset_tokens = []
    try:
        with auth._db() as c:
            stats["total_users"]   = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            stats["premium_users"] = c.execute("SELECT COUNT(*) FROM users WHERE premium=1").fetchone()[0]
            stats["total_bm"]      = c.execute("SELECT COUNT(*) FROM bookmarks").fetchone()[0]
            stats["revenue_est"]   = round(stats["premium_users"] * 1.99, 2)
            stats["total_views"]   = c.execute("SELECT COALESCE(SUM(views),0) FROM professor_views").fetchone()[0]
            users = [dict(r) for r in c.execute(
                "SELECT id, email, premium, created_at, stripe_session FROM users ORDER BY created_at DESC LIMIT 200"
            ).fetchall()]
            reset_tokens = [dict(r) for r in c.execute(
                """SELECT rt.token, u.email, rt.expires_at, rt.used
                   FROM reset_tokens rt JOIN users u ON u.id=rt.user_id
                   WHERE rt.used=0 AND rt.expires_at > datetime('now')
                   ORDER BY rt.expires_at DESC LIMIT 20"""
            ).fetchall()]
    except Exception as e:
        stats = {"error": str(e)}
    return render_template("admin.html", stats=stats, users=users, reset_tokens=reset_tokens)


# ── Password reset routes ──────────────────────────────────────────────────────

@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "GET":
        return render_template("forgot_password.html", sent=False, error="")
    email = (request.form.get("email") or "").strip()
    if not email or "@" not in email:
        return render_template("forgot_password.html", sent=False, error="Please enter a valid email.")
    token, err = auth.create_reset_token(email)
    # Always show "sent" to avoid leaking whether email exists
    reset_url = f"{request.host_url.rstrip('/')}/reset-password/{token}"
    # TODO: send email. For now surface the link in the admin dashboard.
    app.logger.info(f"[RESET] {email} → {reset_url}")
    return render_template("forgot_password.html", sent=True, error="", reset_url=reset_url if app.debug else "")


@app.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    if request.method == "GET":
        user_id = auth.consume_reset_token.__wrapped__(token) if hasattr(auth.consume_reset_token, '__wrapped__') else None
        # Just show form — validate on POST
        return render_template("reset_password.html", token=token, error="", done=False)
    password = request.form.get("password", "")
    confirm  = request.form.get("confirm", "")
    if len(password) < 6:
        return render_template("reset_password.html", token=token, error="Password must be at least 6 characters.", done=False)
    if password != confirm:
        return render_template("reset_password.html", token=token, error="Passwords don't match.", done=False)
    user_id = auth.consume_reset_token(token)
    if not user_id:
        return render_template("reset_password.html", token=token, error="This reset link has expired or already been used.", done=False)
    auth.set_password(user_id, password)
    return render_template("reset_password.html", token=token, error="", done=True)


# ── Error handlers ─────────────────────────────────────────────────────────────

@app.errorhandler(404)
def not_found(e):
    # Return JSON for /api/ routes, HTML otherwise
    if request.path.startswith("/api/"):
        return jsonify({"error": "Not found"}), 404
    return render_template("404.html"), 404


@app.errorhandler(500)
def server_error(e):
    if request.path.startswith("/api/"):
        return jsonify({"error": "Server error — please try again"}), 500
    return render_template("404.html", code=500, message="Something went wrong on our end."), 500


@app.errorhandler(429)
def rate_limited(e):
    return jsonify({"error": "Too many requests — slow down and try again."}), 429


if __name__ == "__main__":
    app.run(debug=True, port=5050)
