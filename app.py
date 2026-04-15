from flask import Flask, request, jsonify, render_template, redirect, make_response
from flask_cors import CORS
import threading
import os
import rmp
import reddit_scraper
import payment

app = Flask(__name__)
CORS(app)


def _warmup():
    """Pre-build the professor index in the background at startup."""
    try:
        rmp.build_su_professor_index()
    except Exception:
        pass


# Kick off index build immediately so the first course search is fast
threading.Thread(target=_warmup, daemon=True).start()


# ── Owner bypass (unprotected, secret) ───────────────────────────────────────

BYPASS_KEY = os.environ.get("BYPASS_KEY", "")

@app.route("/access/<key>")
def bypass(key):
    if not BYPASS_KEY or key != BYPASS_KEY:
        return "", 404
    resp = make_response(redirect("/"))
    payment.issue_access_cookie(resp, "owner-bypass")
    return resp


# ── Payment routes (unprotected) ─────────────────────────────────────────────

@app.route("/pay")
def pay_page():
    error = request.args.get("error")
    return render_template("pay.html", error=error)


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

    resp = make_response(redirect("/"))
    payment.issue_access_cookie(resp, session_id)
    return resp


# ── Free routes ───────────────────────────────────────────────────────────────

@app.route("/")
def index():
    premium = payment.is_premium(request)
    return render_template("index.html", premium=premium)


@app.route("/api/search")
def search():
    name = request.args.get("name", "").strip()
    if not name or len(name) < 2:
        return jsonify({"error": "Please enter a professor name"}), 400
    try:
        professors = rmp.search_professors(name)
        return jsonify({"results": professors})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/index-status")
def index_status():
    return jsonify({"cached": rmp.is_cache_fresh()})


# ── Premium routes ────────────────────────────────────────────────────────────

@app.route("/api/course")
def course_search():
    if not payment.is_premium(request):
        return jsonify({"error": "premium_required"}), 403

    course = request.args.get("course", "").strip()
    if not course or len(course) < 2:
        return jsonify({"error": "Please enter a course name or code"}), 400

    try:
        ranked = rmp.search_by_course(course)
        cache_fresh = rmp.is_cache_fresh()
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
        full_name = f"{data.get('firstName', '')} {data.get('lastName', '')}".strip()
        rmp_url = rmp.build_rmp_url(professor_id)
        prof_base = {
            "id": data.get("id"),
            "name": full_name,
            "department": data.get("department"),
            "avgRating": data.get("avgRating"),
            "avgDifficulty": data.get("avgDifficulty"),
            "numRatings": data.get("numRatings"),
            "wouldTakeAgainPercent": data.get("wouldTakeAgainPercent"),
            "rmpUrl": rmp_url,
            "courseCodes": data.get("courseCodes", []),
        }

        premium = payment.is_premium(request)

        if not premium:
            # Free tier: basic stats + 3 reviews, no premium features
            return jsonify({
                "professor": prof_base,
                "ratings": ratings_list[:3],
                "easyA": None,
                "examMentions": [],
                "redditPosts": [],
                "isPremium": False,
            })

        # Premium tier: full data
        easy_a = rmp.compute_easy_a(data)
        exam_mentions = rmp.parse_exam_info(ratings_list)
        reddit_posts = reddit_scraper.search_reddit_multi(full_name)

        return jsonify({
            "professor": prof_base,
            "ratings": ratings_list,
            "easyA": easy_a,
            "examMentions": exam_mentions,
            "redditPosts": reddit_posts,
            "isPremium": True,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(debug=True, port=5050)
