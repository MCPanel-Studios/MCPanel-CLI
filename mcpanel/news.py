"""MCPanel news, shown when the TUI starts.

The website publishes two files (format: MCPanel-Website's public/news/README.md):

  latest.json — only the newest item; what the TUI fetches on every start
  all.json    — every item, newest first; what the website's /news page lists

News is decoration: an unreachable host, a 404, a malformed document or a slow
answer all mean "no news" — never an error, and never more than a short wait.
"""

import json
import urllib.request

from .http import USER_AGENT

LATEST_URL = "https://get-mcpanel.vercel.app/news/latest.json"
ALL_URL = "https://get-mcpanel.vercel.app/news/all.json"
NEWS_PAGE = "https://get-mcpanel.vercel.app/news"

TIMEOUT = 3


def _text(value, limit):
    return value.strip()[:limit] if isinstance(value, str) else ""


def _link(value):
    value = _text(value, 500)
    return value if value.startswith(("https://", "http://")) else ""


def fetch_latest():
    """The newest item as {id, title, summary, date, url}, or None."""
    try:
        req = urllib.request.Request(LATEST_URL, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as res:
            doc = json.loads(res.read(64 * 1024).decode("utf-8"))
    except Exception:
        return None
    item = doc.get("news") if isinstance(doc, dict) else None
    if not isinstance(item, dict):
        return None
    title = _text(item.get("title"), 120)
    if not title:
        return None
    return {
        "id": _text(item.get("id"), 80),
        "title": title,
        "summary": _text(item.get("summary"), 400),
        "date": _text(item.get("date"), 10),
        "url": _link(item.get("url")) or NEWS_PAGE,
    }
