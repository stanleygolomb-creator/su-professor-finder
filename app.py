from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
import threading
import rmp
import reddit_scraper

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


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/course")
def course_search():
    course = request.args.get("course", "").strip()
    if not course or len(course) < 2:
        return jsonify({"error": "Please enter a course name or code"}), 400

    try:
        ranked = rmp.search_by_course(course)
        cache_fresh = rmp.is_cache_fresh()
        return jsonify({"results": ranked, "course": course, "fromCache": cache_fresh})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/index-status")
def index_status():
    """Let the frontend know if the professor index is cached or needs building."""
    return jsonify({"cached": rmp.is_cache_fresh()})


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


@app.route("/api/professor/<professor_id>")
def professor_detail(professor_id):
    try:
        data = rmp.get_professor_ratings(professor_id)
        if not data:
            return jsonify({"error": "Professor not found"}), 404

        ratings_list = [e["node"] for e in (data.get("ratings") or {}).get("edges", [])]
        easy_a = rmp.compute_easy_a(data)
        exam_mentions = rmp.parse_exam_info(ratings_list)
        rmp_url = rmp.build_rmp_url(professor_id)

        full_name = f"{data.get('firstName', '')} {data.get('lastName', '')}".strip()
        reddit_posts = reddit_scraper.search_reddit_multi(full_name)

        return jsonify({
            "professor": {
                "id": data.get("id"),
                "name": full_name,
                "department": data.get("department"),
                "avgRating": data.get("avgRating"),
                "avgDifficulty": data.get("avgDifficulty"),
                "numRatings": data.get("numRatings"),
                "wouldTakeAgainPercent": data.get("wouldTakeAgainPercent"),
                "rmpUrl": rmp_url,
                "courseCodes": data.get("courseCodes", []),
            },
            "ratings": ratings_list,
            "easyA": easy_a,
            "examMentions": exam_mentions,
            "redditPosts": reddit_posts,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(debug=True, port=5050)
