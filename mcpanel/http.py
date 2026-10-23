"""HTTP helpers — stdlib equivalents of the fetchJSON/fetchText/downloadFile
functions in main.js. No third-party dependencies."""

import json
import re
import urllib.request

USER_AGENT = "MCPanel/2.1 (https://github.com/DippyCoder/MCPanel; dippycoder@gmail.com)"

# The MCPanel repositories are moving from the DippyCoder account to the
# MCPanel-Studios organisation. Every GitHub URL under the old owner is tried
# at the organisation first and only then at its old path, so fetches keep
# working before, during and after the move.
GITHUB_ORG = "MCPanel-Studios"
_OLD_OWNER_RE = re.compile(
    r"^(https://(?:api\.github\.com/repos|github\.com|raw\.githubusercontent\.com"
    r"|gist\.githubusercontent\.com)/)DippyCoder/", re.IGNORECASE)


def github_candidates(url):
    """[organisation URL, old URL] for a DippyCoder GitHub URL, else [url]."""
    moved = _OLD_OWNER_RE.sub(lambda m: m.group(1) + GITHUB_ORG + "/", url, count=1)
    return [moved, url] if moved != url else [url]


def _with_fallback(url, fn):
    """Runs fn(candidate) for each of github_candidates(url) until one works;
    re-raises the last failure if none does."""
    last = None
    for candidate in github_candidates(url):
        try:
            return fn(candidate)
        except Exception as e:
            last = e
    raise last


def _open(url):
    def attempt(u):
        req = urllib.request.Request(u, headers={"User-Agent": USER_AGENT})
        return urllib.request.urlopen(req, timeout=30)
    return _with_fallback(url, attempt)


def post_json(url, payload):
    body = json.dumps(payload).encode("utf-8")

    def attempt(u):
        req = urllib.request.Request(
            u, data=body,
            headers={"User-Agent": USER_AGENT, "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as res:
            return res.read().decode("utf-8", "replace")
    data = _with_fallback(url, attempt)
    try:
        return json.loads(data)
    except Exception:
        raise RuntimeError("Parse error: " + data[:100])


def fetch_json(url):
    with _open(url) as res:
        data = res.read().decode("utf-8", "replace")
    try:
        return json.loads(data)
    except Exception:
        raise RuntimeError("Parse error: " + data[:100])


def fetch_json_with_headers(url):
    """Like fetch_json, but also returns the response headers — needed for
    APIs (Spiget) that report total result counts in a header rather than
    the JSON body."""
    with _open(url) as res:
        data = res.read().decode("utf-8", "replace")
        headers = res.headers
    try:
        return json.loads(data), headers
    except Exception:
        raise RuntimeError("Parse error: " + data[:100])


def fetch_text(url):
    with _open(url) as res:
        return res.read().decode("utf-8", "replace")


def download_file(url, dest, on_progress=None):
    """Stream `url` to `dest`, following redirects (urllib does this for us),
    reporting integer percent via on_progress(percent).

    The body goes to `<dest>.part` and is only renamed over `dest` once it
    arrived completely, so a dropped connection never leaves a truncated jar
    where the server (or plugin loader) would pick it up — and never clobbers
    a previously working file."""
    _with_fallback(url, lambda u: _download(u, dest, on_progress))


def _download(url, dest, on_progress):
    import os
    tmp = dest + ".part"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=60) as res:
            total = int(res.headers.get("content-length") or 0)
            downloaded = 0
            last = -1
            with open(tmp, "wb") as f:
                while True:
                    chunk = res.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total and on_progress:
                        pct = min(100, round(downloaded / total * 100))
                        if pct != last:
                            last = pct
                            on_progress(pct)
        if total and downloaded != total:
            raise RuntimeError(f"Download incomplete: got {downloaded} of {total} bytes")
        if downloaded == 0:
            raise RuntimeError("Download returned an empty file")
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    if total and on_progress and last != 100:
        on_progress(100)
