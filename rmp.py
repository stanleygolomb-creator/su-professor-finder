import requests
import re
import unicodedata
import concurrent.futures
import json
import os
import time
import string
import threading

GRAPHQL_URL = "https://www.ratemyprofessors.com/graphql"
AUTH_TOKEN  = "dGVzdDp0ZXN0"
HEADERS = {
    "Authorization": f"Basic {AUTH_TOKEN}",
    "Content-Type":  "application/json",
    "User-Agent":    "Mozilla/5.0",
    "Referer":       "https://www.ratemyprofessors.com/",
}

_CACHE_DIR       = "/tmp" if os.path.isdir("/tmp") else os.path.dirname(__file__)
_CACHE_TTL       = 60 * 60 * 12   # Rebuild after 12 hours
_STALE_SERVE_TTL = 60 * 60 * 48   # Serve stale cache up to 48h while rebuilding
_ACTIVE_SCHOOLS_FILE = os.path.join(_CACHE_DIR, ".active_schools.json")

# Tracks which schools are currently being rebuilt to avoid double-rebuilds
_rebuilding: set = set()
_rebuild_lock = threading.Lock()

SU_SCHOOL_ID = None


# ── School search ─────────────────────────────────────────────────────────────

def search_schools(query: str) -> list:
    """Search RMP for schools by name."""
    gql = """
    query NewSearchSchoolsQuery($query: SchoolSearchQuery!) {
      newSearch {
        schools(query: $query) {
          edges { node { id name city state } }
        }
      }
    }
    """
    resp = requests.post(
        GRAPHQL_URL,
        json={"query": gql, "variables": {"query": {"text": query}}},
        headers=HEADERS, timeout=10,
    )
    resp.raise_for_status()
    return [e["node"] for e in resp.json()["data"]["newSearch"]["schools"]["edges"]]


def get_su_school_id() -> str:
    global SU_SCHOOL_ID
    if SU_SCHOOL_ID:
        return SU_SCHOOL_ID
    for s in search_schools("Syracuse University"):
        if "Syracuse" in s["name"] and s["state"] == "NY":
            SU_SCHOOL_ID = s["id"]
            return SU_SCHOOL_ID
    raise ValueError("Could not find Syracuse University on RateMyProfessors")


# ── Active schools tracking ───────────────────────────────────────────────────

def _load_active_schools() -> dict:
    """Returns {school_id: {name, last_used_ts}}"""
    try:
        with open(_ACTIVE_SCHOOLS_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_active_schools(schools: dict):
    try:
        with open(_ACTIVE_SCHOOLS_FILE, "w") as f:
            json.dump(schools, f)
    except Exception:
        pass


def record_school_usage(school_id: str, school_name: str = ""):
    """Record that a school was searched so it gets auto-refreshed."""
    schools = _load_active_schools()
    schools[school_id] = {"name": school_name, "last_used": time.time()}
    # Keep at most 30 most-recently-used schools
    if len(schools) > 30:
        schools = dict(sorted(schools.items(), key=lambda x: x[1]["last_used"], reverse=True)[:30])
    _save_active_schools(schools)


# ── Per-school cache ──────────────────────────────────────────────────────────

def _cache_file(school_id: str) -> str:
    safe = re.sub(r"[^a-z0-9]", "_", school_id.lower())[:40]
    return os.path.join(_CACHE_DIR, f".prof_cache_{safe}.json")


def _load_cache(school_id: str):
    """Returns (profs_dict, cache_age_seconds) or (None, inf) if missing."""
    path = _cache_file(school_id)
    if not os.path.exists(path):
        return None, float("inf")
    try:
        with open(path) as f:
            cached = json.load(f)
        age = time.time() - cached.get("ts", 0)
        return cached.get("profs"), age
    except Exception:
        return None, float("inf")


def is_cache_fresh(school_id: str = None) -> bool:
    if not school_id:
        try:
            school_id = get_su_school_id()
        except Exception:
            return False
    _, age = _load_cache(school_id)
    return age < _CACHE_TTL


# ── Professor search ──────────────────────────────────────────────────────────

TEACHER_FIELDS = """
  id firstName lastName department
  avgRating avgDifficulty numRatings wouldTakeAgainPercent
  courseCodes { courseName courseCount }
"""

SEARCH_TEACHERS_QUERY = """
query NewSearchTeachersQuery($query: TeacherSearchQuery!) {
  newSearch {
    teachers(query: $query) {
      edges { node { %s } }
    }
  }
}
""" % TEACHER_FIELDS


def search_professors(name: str, school_id: str = None) -> list:
    if not school_id:
        school_id = get_su_school_id()
    variables = {"query": {"text": name, "schoolID": school_id}}
    resp = requests.post(
        GRAPHQL_URL,
        json={"query": SEARCH_TEACHERS_QUERY, "variables": variables},
        headers=HEADERS, timeout=10,
    )
    resp.raise_for_status()
    return [e["node"] for e in resp.json()["data"]["newSearch"]["teachers"]["edges"]]


def get_professor_ratings(professor_id: str):
    """Always fetches live from RMP — no cache, always current."""
    gql = """
    query TeacherRatingsPageQuery($id: ID!) {
      node(id: $id) {
        ... on Teacher {
          id firstName lastName department
          avgRating avgDifficulty numRatings wouldTakeAgainPercent
          ratings(first: 20) {
            edges {
              node {
                class comment helpfulRating clarityRating difficultyRating
                wouldTakeAgain grade date flagStatus thumbsUpTotal thumbsDownTotal
                teacherNote { comment }
              }
            }
          }
          courseCodes { courseName courseCount }
        }
      }
    }
    """
    resp = requests.post(
        GRAPHQL_URL,
        json={"query": gql, "variables": {"id": professor_id}},
        headers=HEADERS, timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["data"]["node"]


# ── Full-school index ─────────────────────────────────────────────────────────

def _fetch_professor_page(text: str, school_id: str) -> list:
    variables = {"query": {"text": text, "schoolID": school_id, "fallback": False}}
    try:
        resp = requests.post(
            GRAPHQL_URL,
            json={"query": SEARCH_TEACHERS_QUERY, "variables": variables},
            headers=HEADERS, timeout=8,
        )
        resp.raise_for_status()
        return [e["node"] for e in resp.json()["data"]["newSearch"]["teachers"]["edges"]]
    except Exception:
        return []


def _do_build_index(school_id: str) -> dict:
    """Actually hits RMP and builds a fresh professor index."""
    single  = list(string.ascii_lowercase)
    two_ltr = [a + b for a in "sbcmhwtgjp" for b in string.ascii_lowercase]
    queries = single + two_ltr

    profs: dict = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as ex:
        futures = {ex.submit(_fetch_professor_page, q, school_id): q for q in queries}
        for fut in concurrent.futures.as_completed(futures):
            for p in fut.result():
                profs[p["id"]] = p

    try:
        with open(_cache_file(school_id), "w") as f:
            json.dump({"ts": time.time(), "profs": profs}, f)
    except Exception:
        pass

    with _rebuild_lock:
        _rebuilding.discard(school_id)

    return profs


def _rebuild_in_background(school_id: str):
    """Trigger a background rebuild if one isn't already running."""
    with _rebuild_lock:
        if school_id in _rebuilding:
            return
        _rebuilding.add(school_id)
    t = threading.Thread(target=_do_build_index, args=(school_id,), daemon=True)
    t.start()


def build_professor_index(school_id: str = None, force: bool = False) -> dict:
    """
    Stale-while-revalidate:
    - Fresh cache  → return immediately
    - Stale cache  → return stale data, trigger background rebuild
    - No cache     → block until built
    """
    if not school_id:
        school_id = get_su_school_id()

    record_school_usage(school_id)
    profs, age = _load_cache(school_id)

    if force or profs is None:
        # No cache at all — must build synchronously
        return _do_build_index(school_id)

    if age > _CACHE_TTL:
        # Stale — serve old data, rebuild quietly in background
        _rebuild_in_background(school_id)

    return profs


# Keep old name for backward compat
def build_su_professor_index(force: bool = False) -> dict:
    return build_professor_index(get_su_school_id(), force)


# ── Auto-refresh scheduler ────────────────────────────────────────────────────

def start_auto_refresh():
    """
    Background thread: every 6 hours, refresh indexes for all active schools.
    Runs as a daemon so it dies with the server.
    """
    def loop():
        while True:
            time.sleep(60 * 60 * 6)  # wait 6 hours
            schools = _load_active_schools()
            for school_id, meta in schools.items():
                try:
                    _, age = _load_cache(school_id)
                    if age > _CACHE_TTL:
                        print(f"[auto-refresh] Rebuilding index for {meta.get('name', school_id)}")
                        _rebuild_in_background(school_id)
                        time.sleep(30)  # stagger rebuilds to avoid hammering RMP
                except Exception as e:
                    print(f"[auto-refresh] Error for {school_id}: {e}")

    t = threading.Thread(target=loop, daemon=True)
    t.start()


# ── Course search ─────────────────────────────────────────────────────────────

def _rank_score(prof: dict) -> float:
    rating = prof.get("avgRating") or 0
    diff   = prof.get("avgDifficulty") or 3
    wtag   = max(prof.get("wouldTakeAgainPercent") or 0, 0)
    inv_diff = (5 - diff) / 4
    num = prof.get("numRatings") or 0
    confidence = 0.5 + 0.5 * (num / (num + 10))
    return round((0.40 * rating/5 + 0.35 * inv_diff + 0.25 * wtag/100) * confidence * 100, 1)


def search_by_course(course: str, school_id: str = None) -> list:
    if not school_id:
        school_id = get_su_school_id()
    profs = build_professor_index(school_id)
    q_norm  = re.sub(r"\s+", "", course.upper())
    q_lower = course.lower().strip()
    matched = []
    for p in profs.values():
        for cc in (p.get("courseCodes") or []):
            n  = re.sub(r"\s+", "", (cc.get("courseName") or "").upper())
            nl = (cc.get("courseName") or "").lower()
            if (q_norm and q_norm in n) or (q_lower and q_lower in nl):
                matched.append(p)
                break
    for p in matched:
        p["rankScore"] = _rank_score(p)
    matched.sort(key=lambda p: p["rankScore"], reverse=True)
    return matched


# ── Easy A / exam helpers ─────────────────────────────────────────────────────

def parse_exam_info(ratings: list) -> list:
    keywords = ["exam", "exams", "midterm", "midterms", "final", "quiz", "quizzes", "test", "tests"]
    return [
        {"comment": r.get("comment"), "keywords": [kw for kw in keywords if kw in (r.get("comment") or "").lower()], "class": r.get("class")}
        for r in ratings
        if any(kw in (r.get("comment") or "").lower() for kw in keywords)
    ]


def compute_easy_a(data: dict):
    ratings = [e["node"] for e in (data.get("ratings") or {}).get("edges", [])]
    if not ratings:
        return None
    difficulty   = data.get("avgDifficulty", 3)
    grade_counts = {}
    for r in ratings:
        g = r.get("grade")
        if g:
            grade_counts[g] = grade_counts.get(g, 0) + 1
    total = sum(grade_counts.values())
    a = sum(v for k, v in grade_counts.items() if k.startswith("A"))
    b = sum(v for k, v in grade_counts.items() if k.startswith("B"))
    score = 0; reasons = []
    if difficulty <= 2.0:   score += 2; reasons.append(f"Very low difficulty ({difficulty}/5)")
    elif difficulty <= 2.5: score += 1; reasons.append(f"Low difficulty ({difficulty}/5)")
    if total > 0:
        ap = a/total; abp = (a+b)/total
        if ap >= .5:   score += 2; reasons.append(f"{int(ap*100)}% of students got an A")
        elif abp >= .7:score += 1; reasons.append(f"{int(abp*100)}% got an A or B")
    wtag = data.get("wouldTakeAgainPercent", -1)
    if wtag >= 80: score += 1; reasons.append(f"{int(wtag)}% would take again")
    return {"is_easy_a": score >= 3, "score": score, "reasons": reasons, "grade_distribution": grade_counts}


def build_rmp_url(professor_id: str) -> str:
    import base64
    try:
        return f"https://www.ratemyprofessors.com/professor/{base64.b64decode(professor_id).decode().split('-')[-1]}"
    except Exception:
        return "https://www.ratemyprofessors.com"
