import requests
import re
import unicodedata
import concurrent.futures
import json
import os
import time
import string

GRAPHQL_URL = "https://www.ratemyprofessors.com/graphql"
AUTH_TOKEN  = "dGVzdDp0ZXN0"
HEADERS = {
    "Authorization": f"Basic {AUTH_TOKEN}",
    "Content-Type":  "application/json",
    "User-Agent":    "Mozilla/5.0",
    "Referer":       "https://www.ratemyprofessors.com/",
}

_CACHE_DIR = "/tmp" if os.path.isdir("/tmp") else os.path.dirname(__file__)
_CACHE_TTL = 60 * 60 * 24  # 24 hours

SU_SCHOOL_ID = None  # cached after first fetch


# ── School search ─────────────────────────────────────────────────────────────

def search_schools(query: str) -> list:
    """Search RMP for schools by name. Returns list of {id, name, city, state}."""
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
    edges = resp.json()["data"]["newSearch"]["schools"]["edges"]
    return [e["node"] for e in edges]


def get_su_school_id() -> str:
    global SU_SCHOOL_ID
    if SU_SCHOOL_ID:
        return SU_SCHOOL_ID
    schools = search_schools("Syracuse University")
    for s in schools:
        if "Syracuse" in s["name"] and s["state"] == "NY":
            SU_SCHOOL_ID = s["id"]
            return SU_SCHOOL_ID
    raise ValueError("Could not find Syracuse University on RateMyProfessors")


# ── Per-school cache helpers ──────────────────────────────────────────────────

def _cache_file(school_id: str) -> str:
    safe = re.sub(r"[^a-z0-9]", "_", school_id.lower())[:40]
    return os.path.join(_CACHE_DIR, f".prof_cache_{safe}.json")


def is_cache_fresh(school_id: str = None) -> bool:
    if not school_id:
        school_id = get_su_school_id()
    path = _cache_file(school_id)
    if not os.path.exists(path):
        return False
    try:
        with open(path) as f:
            cached = json.load(f)
        return time.time() - cached.get("ts", 0) < _CACHE_TTL
    except Exception:
        return False


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
    edges = resp.json()["data"]["newSearch"]["teachers"]["edges"]
    return [e["node"] for e in edges]


def get_professor_ratings(professor_id: str):
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


# ── Full-school index (for course search) ────────────────────────────────────

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


def build_professor_index(school_id: str = None, force: bool = False) -> dict:
    """
    Fetch all professors at a school via parallel alphabet sweeps and cache to disk.
    Returns a dict keyed by professor ID.
    """
    if not school_id:
        school_id = get_su_school_id()

    path = _cache_file(school_id)

    if not force and os.path.exists(path):
        try:
            with open(path) as f:
                cached = json.load(f)
            if time.time() - cached.get("ts", 0) < _CACHE_TTL:
                return cached["profs"]
        except Exception:
            pass

    single  = list(string.ascii_lowercase)
    two_ltr = [a + b for a in "sbcmhwtgjp" for b in string.ascii_lowercase]
    queries = single + two_ltr  # ~286 queries

    profs: dict = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as ex:
        futures = {ex.submit(_fetch_professor_page, q, school_id): q for q in queries}
        for fut in concurrent.futures.as_completed(futures):
            for p in fut.result():
                profs[p["id"]] = p

    try:
        with open(path, "w") as f:
            json.dump({"ts": time.time(), "profs": profs}, f)
    except Exception:
        pass

    return profs


# Keep old name for backward compat
def build_su_professor_index(force: bool = False) -> dict:
    return build_professor_index(get_su_school_id(), force)


# ── Course search ─────────────────────────────────────────────────────────────

def _rank_score(prof: dict) -> float:
    rating = prof.get("avgRating") or 0
    diff   = prof.get("avgDifficulty") or 3
    wtag   = max(prof.get("wouldTakeAgainPercent") or 0, 0)
    inv_diff = (5 - diff) / 4
    rating_n = rating / 5
    wtag_n   = wtag / 100
    num = prof.get("numRatings") or 0
    confidence = 0.5 + 0.5 * (num / (num + 10))
    return round((0.40 * rating_n + 0.35 * inv_diff + 0.25 * wtag_n) * confidence * 100, 1)


def search_by_course(course: str, school_id: str = None) -> list:
    if not school_id:
        school_id = get_su_school_id()
    profs = build_professor_index(school_id)
    q_norm  = re.sub(r"\s+", "", course.upper())
    q_lower = course.lower().strip()
    matched = []
    for p in profs.values():
        for cc in (p.get("courseCodes") or []):
            name_norm  = re.sub(r"\s+", "", (cc.get("courseName") or "").upper())
            name_lower = (cc.get("courseName") or "").lower()
            if (q_norm and q_norm in name_norm) or (q_lower and q_lower in name_lower):
                matched.append(p)
                break
    for p in matched:
        p["rankScore"] = _rank_score(p)
    matched.sort(key=lambda p: p["rankScore"], reverse=True)
    return matched


# ── Easy A / exam helpers ─────────────────────────────────────────────────────

def parse_exam_info(ratings: list) -> list:
    keywords = ["exam", "exams", "midterm", "midterms", "final", "quiz", "quizzes", "test", "tests"]
    mentions = []
    for r in ratings:
        comment = (r.get("comment") or "").lower()
        found = [kw for kw in keywords if kw in comment]
        if found:
            mentions.append({"comment": r.get("comment"), "keywords": found, "class": r.get("class")})
    return mentions


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
    total_graded = sum(grade_counts.values())
    a_grades = sum(v for k, v in grade_counts.items() if k.startswith("A"))
    b_grades = sum(v for k, v in grade_counts.items() if k.startswith("B"))
    score = 0
    reasons = []
    if difficulty <= 2.0:
        score += 2; reasons.append(f"Very low difficulty ({difficulty}/5)")
    elif difficulty <= 2.5:
        score += 1; reasons.append(f"Low difficulty ({difficulty}/5)")
    if total_graded > 0:
        a_pct  = a_grades / total_graded
        ab_pct = (a_grades + b_grades) / total_graded
        if a_pct >= 0.5:
            score += 2; reasons.append(f"{int(a_pct*100)}% of students got an A")
        elif ab_pct >= 0.7:
            score += 1; reasons.append(f"{int(ab_pct*100)}% of students got an A or B")
    wtag = data.get("wouldTakeAgainPercent", -1)
    if wtag >= 80:
        score += 1; reasons.append(f"{int(wtag)}% would take again")
    return {"is_easy_a": score >= 3, "score": score, "reasons": reasons, "grade_distribution": grade_counts}


def build_rmp_url(professor_id: str) -> str:
    import base64
    try:
        decoded = base64.b64decode(professor_id).decode("utf-8")
        numeric_id = decoded.split("-")[-1]
        return f"https://www.ratemyprofessors.com/professor/{numeric_id}"
    except Exception:
        return "https://www.ratemyprofessors.com"
