from flask import Flask, request, jsonify, render_template, redirect, make_response
from flask_cors import CORS
import threading
import os
import rmp
import reddit_scraper
import payment
import auth

app = Flask(__name__)
CORS(app)

# Init database on startup
auth.init_db()


def _warmup():
    try:
        rmp.build_su_professor_index()
    except Exception:
        pass

threading.Thread(target=_warmup, daemon=True).start()
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
    premium = bool(user.get("premium")) or payment.is_premium(request)
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
def search():
    if not _is_premium(request):
        return jsonify({"error": "premium_required"}), 403
    name      = request.args.get("name", "").strip()
    school_id = request.args.get("school_id", "").strip() or None
    if not name or len(name) < 2:
        return jsonify({"error": "Please enter a professor name"}), 400
    try:
        return jsonify({"results": rmp.search_professors(name, school_id)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/index-status")
def index_status():
    school_id = request.args.get("school_id", "").strip() or None
    return jsonify({"cached": rmp.is_cache_fresh(school_id)})


# ── Premium routes ────────────────────────────────────────────────────────────

@app.route("/api/course")
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


if __name__ == "__main__":
    app.run(debug=True, port=5050)
