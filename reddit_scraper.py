import requests
import urllib.parse

REDDIT_SEARCH_URL = "https://www.reddit.com/r/Syracuse/search.json"
HEADERS = {"User-Agent": "SU-Professor-Finder/1.0"}


def search_reddit(professor_name: str, course: str = None):
    """Search r/Syracuse for professor mentions without requiring API credentials."""
    query = professor_name
    if course:
        query = f"{professor_name} {course}"

    params = {
        "q": query,
        "restrict_sr": "true",
        "sort": "relevance",
        "limit": 10,
        "t": "all",
    }

    try:
        resp = requests.get(REDDIT_SEARCH_URL, params=params, headers=HEADERS, timeout=8)
        resp.raise_for_status()
        data = resp.json()
        posts = data.get("data", {}).get("children", [])
        results = []
        for post in posts:
            p = post["data"]
            results.append({
                "title": p.get("title"),
                "selftext": p.get("selftext", "")[:400],
                "url": f"https://reddit.com{p.get('permalink')}",
                "score": p.get("score"),
                "num_comments": p.get("num_comments"),
                "created_utc": p.get("created_utc"),
            })
        return results
    except Exception as e:
        return []


def search_reddit_multi(professor_name: str):
    """Search across multiple Syracuse-related subreddits."""
    subreddits = ["Syracuse", "SyracuseUniversity"]
    all_results = []
    seen_urls = set()

    for sub in subreddits:
        params = {
            "q": professor_name,
            "restrict_sr": "true",
            "sort": "relevance",
            "limit": 5,
            "t": "all",
        }
        try:
            url = f"https://www.reddit.com/r/{sub}/search.json"
            resp = requests.get(url, params=params, headers=HEADERS, timeout=8)
            if resp.status_code == 404:
                continue
            resp.raise_for_status()
            data = resp.json()
            posts = data.get("data", {}).get("children", [])
            for post in posts:
                p = post["data"]
                permalink = f"https://reddit.com{p.get('permalink')}"
                if permalink not in seen_urls:
                    seen_urls.add(permalink)
                    all_results.append({
                        "subreddit": p.get("subreddit"),
                        "title": p.get("title"),
                        "selftext": p.get("selftext", "")[:500],
                        "url": permalink,
                        "score": p.get("score"),
                        "num_comments": p.get("num_comments"),
                    })
        except Exception:
            continue

    return all_results
