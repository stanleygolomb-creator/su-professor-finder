import requests
import re
import unicodedata
import concurrent.futures
import json
import os
import time
import string

GRAPHQL_URL = "https://www.ratemyprofessors.com/graphql"
AUTH_TOKEN = "dGVzdDp0ZXN0"  # Public RMP token
HEADERS = {
    "Authorization": f"Basic {AUTH_TOKEN}",
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://www.ratemyprofessors.com/",
}

SU_SCHOOL_ID = None  # Will be fetched once


def get_su_school_id():
    global SU_SCHOOL_ID
    if SU_SCHOOL_ID:
        return SU_SCHOOL_ID

    query = """
    query NewSearchSchoolsQuery($query: SchoolSearchQuery!) {
      newSearch {
        schools(query: $query) {
          edges {
            node {
              id
              name
              city
              state
            }
          }
        }
      }
    }
    """
    variables = {"query": {"text": "Syracuse University"}}
    resp = requests.post(
        GRAPHQL_URL, json={"query": query, "variables": variables}, headers=HEADERS, timeout=10
    )
    resp.raise_for_status()
    schools = resp.json()["data"]["newSearch"]["schools"]["edges"]
    for edge in schools:
        node = edge["node"]
        if "Syracuse" in node["name"] and node["state"] == "NY":
            SU_SCHOOL_ID = node["id"]
            return SU_SCHOOL_ID
    raise ValueError("Could not find Syracuse University on RateMyProfessors")


def search_professors(name: str):
    school_id = get_su_school_id()
    query = """
    query NewSearchTeachersQuery($query: TeacherSearchQuery!) {
      newSearch {
        teachers(query: $query) {
          edges {
            node {
              id
              firstName
              lastName
              department
              avgRating
              avgDifficulty
              numRatings
              wouldTakeAgainPercent
              school {
                name
              }
            }
          }
        }
      }
    }
    """
    variables = {"query": {"text": name, "schoolID": school_id}}
    resp = requests.post(
        GRAPHQL_URL, json={"query": query, "variables": variables}, headers=HEADERS, timeout=10
    )
    resp.raise_for_status()
    edges = resp.json()["data"]["newSearch"]["teachers"]["edges"]
    return [edge["node"] for edge in edges]


def get_professor_ratings(professor_id: str):
    query = """
    query TeacherRatingsPageQuery($id: ID!) {
      node(id: $id) {
        ... on Teacher {
          id
          firstName
          lastName
          department
          avgRating
          avgDifficulty
          numRatings
          wouldTakeAgainPercent
          ratings(first: 20) {
            edges {
              node {
                class
                comment
                helpfulRating
                clarityRating
                difficultyRating
                wouldTakeAgain
                grade
                date
                flagStatus
                thumbsUpTotal
                thumbsDownTotal
                teacherNote {
                  comment
                }
              }
            }
          }
          courseCodes {
            courseName
            courseCount
          }
        }
      }
    }
    """
    variables = {"id": professor_id}
    resp = requests.post(
        GRAPHQL_URL, json={"query": query, "variables": variables}, headers=HEADERS, timeout=10
    )
    resp.raise_for_status()
    data = resp.json()["data"]["node"]
    return data


def parse_exam_info(ratings):
    """Scan review comments for exam/test mentions."""
    exam_keywords = ["exam", "exams", "midterm", "midterms", "final", "quiz", "quizzes", "test", "tests"]
    mentions = []
    for r in ratings:
        comment = (r.get("comment") or "").lower()
        found = [kw for kw in exam_keywords if kw in comment]
        if found:
            mentions.append({"comment": r.get("comment"), "keywords": found, "class": r.get("class")})
    return mentions


def compute_easy_a(data):
    """
    Heuristic: difficulty < 2.5 AND avg grade skews A/B = likely Easy A
    """
    ratings = [e["node"] for e in (data.get("ratings") or {}).get("edges", [])]
    if not ratings:
        return None

    difficulty = data.get("avgDifficulty", 3)
    grade_counts = {}
    for r in ratings:
        g = r.get("grade")
        if g:
            grade_counts[g] = grade_counts.get(g, 0) + 1

    total_graded = sum(grade_counts.values())
    a_grades = sum(v for k, v in grade_counts.items() if k.startswith("A"))
    b_grades = sum(v for k, v in grade_counts.items() if k.startswith("B"))

    easy_a_score = 0
    reasons = []

    if difficulty <= 2.0:
        easy_a_score += 2
        reasons.append(f"Very low difficulty ({difficulty}/5)")
    elif difficulty <= 2.5:
        easy_a_score += 1
        reasons.append(f"Low difficulty ({difficulty}/5)")

    if total_graded > 0:
        a_pct = a_grades / total_graded
        ab_pct = (a_grades + b_grades) / total_graded
        if a_pct >= 0.5:
            easy_a_score += 2
            reasons.append(f"{int(a_pct*100)}% of students got an A")
        elif ab_pct >= 0.7:
            easy_a_score += 1
            reasons.append(f"{int(ab_pct*100)}% of students got an A or B")

    wtag = data.get("wouldTakeAgainPercent", -1)
    if wtag >= 80:
        easy_a_score += 1
        reasons.append(f"{int(wtag)}% would take again")

    return {
        "is_easy_a": easy_a_score >= 3,
        "score": easy_a_score,
        "reasons": reasons,
        "grade_distribution": grade_counts,
    }


def _normalize(s: str) -> str:
    """Lowercase, strip accents, remove non-alphanumeric for fuzzy matching."""
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9 ]", "", s.lower()).strip()


def _course_match(course_query: str, course_codes: list) -> bool:
    """Return True if any of a professor's courseCodes match the query."""
    q = _normalize(course_query)
    for cc in course_codes:
        name = _normalize(cc.get("courseName", ""))
        if q in name or name in q:
            return True
    return False


def _rank_score(prof: dict) -> float:
    """
    Composite rank score for course comparison.
    Higher = better (easier A, higher-rated, more popular).
    Weighted: rating 40%, inverse-difficulty 35%, would-take-again 25%.
    """
    rating = prof.get("avgRating") or 0
    diff   = prof.get("avgDifficulty") or 3
    wtag   = prof.get("wouldTakeAgainPercent") or 0
    wtag   = max(wtag, 0)  # -1 means N/A

    inv_diff = (5 - diff) / 4   # 0–1, higher = easier
    rating_n = rating / 5        # 0–1
    wtag_n   = wtag / 100        # 0–1

    # Confidence weight: ranges 0.5 (0 reviews) → 1.0 (many reviews)
    # Softer penalty so a 4.8-star prof with 12 reviews still ranks near the top
    num = prof.get("numRatings") or 0
    confidence = 0.5 + 0.5 * (num / (num + 10))

    score = (0.40 * rating_n + 0.35 * inv_diff + 0.25 * wtag_n) * confidence
    return round(score * 100, 1)


TEACHER_FIELDS = """
  id
  firstName
  lastName
  department
  avgRating
  avgDifficulty
  numRatings
  wouldTakeAgainPercent
  courseCodes {
    courseName
    courseCount
  }
"""

SEARCH_TEACHERS_QUERY = """
query NewSearchTeachersQuery($query: TeacherSearchQuery!) {
  newSearch {
    teachers(query: $query) {
      edges {
        node {
          %s
        }
      }
    }
  }
}
""" % TEACHER_FIELDS


# Prefer /tmp (always writable on Linux/Mac), fall back to app dir on Windows
_CACHE_DIR  = "/tmp" if os.path.isdir("/tmp") else os.path.dirname(__file__)
_CACHE_FILE = os.path.join(_CACHE_DIR, ".su_prof_cache.json")
_CACHE_TTL  = 60 * 60 * 24  # 24 hours


def _fetch_su_professor_page(text: str, school_id: str) -> list:
    variables = {"query": {"text": text, "schoolID": school_id, "fallback": False}}
    try:
        resp = requests.post(
            GRAPHQL_URL,
            json={"query": SEARCH_TEACHERS_QUERY, "variables": variables},
            headers=HEADERS,
            timeout=8,
        )
        resp.raise_for_status()
        return [e["node"] for e in resp.json()["data"]["newSearch"]["teachers"]["edges"]]
    except Exception:
        return []


def is_cache_fresh() -> bool:
    if not os.path.exists(_CACHE_FILE):
        return False
    try:
        with open(_CACHE_FILE) as f:
            cached = json.load(f)
        return time.time() - cached.get("ts", 0) < _CACHE_TTL
    except Exception:
        return False


def build_su_professor_index(force: bool = False) -> dict:
    """
    Fetch all SU professors via parallel alphabet sweeps and cache to disk.
    Returns a dict keyed by professor ID.
    """
    # Return cached version if fresh enough
    if not force and os.path.exists(_CACHE_FILE):
        try:
            with open(_CACHE_FILE) as f:
                cached = json.load(f)
            if time.time() - cached.get("ts", 0) < _CACHE_TTL:
                return cached["profs"]
        except Exception:
            pass

    school_id = get_su_school_id()

    # Single-letter sweep + 2-letter sweep for 10 most-common surname starters
    single  = list(string.ascii_lowercase)
    two_ltr = [a + b for a in "sbcmhwtgjp" for b in string.ascii_lowercase]
    queries = single + two_ltr  # ~286 total

    profs: dict = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as ex:
        futures = {ex.submit(_fetch_su_professor_page, q, school_id): q for q in queries}
        for fut in concurrent.futures.as_completed(futures):
            for p in fut.result():
                profs[p["id"]] = p

    try:
        with open(_CACHE_FILE, "w") as f:
            json.dump({"ts": time.time(), "profs": profs}, f)
    except Exception:
        pass

    return profs


def search_by_course(course: str) -> list:
    """
    Search for SU professors who teach a given course and return them ranked.
    Uses a cached full index of SU professors, filtered by courseCodes.
    """
    profs = build_su_professor_index()

    # Normalize query: remove spaces, uppercase  (e.g. "MAT 295" -> "MAT295")
    q_norm = re.sub(r"\s+", "", course.upper())
    q_lower = course.lower().strip()

    matched = []
    for p in profs.values():
        for cc in (p.get("courseCodes") or []):
            name_norm = re.sub(r"\s+", "", (cc.get("courseName") or "").upper())
            name_lower = (cc.get("courseName") or "").lower()
            if (q_norm and q_norm in name_norm) or (q_lower and q_lower in name_lower):
                matched.append(p)
                break

    # Attach rank score and sort descending
    for p in matched:
        p["rankScore"] = _rank_score(p)
    matched.sort(key=lambda p: p["rankScore"], reverse=True)
    return matched


def build_rmp_url(professor_id: str):
    import base64
    try:
        decoded = base64.b64decode(professor_id).decode("utf-8")
        numeric_id = decoded.split("-")[-1]
        return f"https://www.ratemyprofessors.com/professor/{numeric_id}"
    except Exception:
        return "https://www.ratemyprofessors.com"
