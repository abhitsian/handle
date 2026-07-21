"""stash — Handle's curated link library.

Open tabs are ephemeral; bundles are content snapshots. The stash is the
durable set of links you deliberately keep — the SharePoint doc, the Figma
file, the Drive spec — so you can find them weeks later without hunting
through history. Drop a URL in from chat, the CLI, or the board; tag it,
star it, search it; and let a session scan surface the doc links you pasted
into Claude Code but never saved.

Store: stash.json  {"links": {url: {...}}, "dismissed": [url, ...]}
Self-contained — imported by tab.py (CLI) and app.py (board UI).
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote

import collect

APP_DIR = Path(__file__).resolve().parent
STASH_PATH = APP_DIR / "stash.json"
STATE_PATH = APP_DIR / "state.json"
DEDUCTIONS_PATH = APP_DIR / "deductions.json"
SESSIONS_DIR = Path.home() / ".claude" / "projects"


# --------------------------------------------------------------------------- #
# source detection — order matters (OneDrive before SharePoint; specific first)
# --------------------------------------------------------------------------- #
SOURCES = [
    ("OneDrive",     re.compile(r"-my\.sharepoint\.com|onedrive\.live\.com|1drv\.ms", re.I)),
    ("SharePoint",   re.compile(r"\.sharepoint\.com|/sites/|/_layouts/", re.I)),
    ("Google Drive", re.compile(r"docs\.google\.com|drive\.google\.com", re.I)),
    ("Figma",        re.compile(r"figma\.com", re.I)),
    ("Notion",       re.compile(r"notion\.so|notion\.site", re.I)),
    ("Confluence",   re.compile(r"atlassian\.net/wiki|/confluence/", re.I)),
    ("Jira",         re.compile(r"atlassian\.net/(browse|jira|secure)", re.I)),
    ("GitHub",       re.compile(r"github\.com|github\.io", re.I)),
    ("Loom",         re.compile(r"loom\.com", re.I)),
    ("Miro",         re.compile(r"miro\.com", re.I)),
    ("Box",          re.compile(r"\.box\.com|app\.box\.com", re.I)),
    ("Dropbox",      re.compile(r"dropbox\.com", re.I)),
]

# hosts that are never worth suggesting from a session scan (noise)
_SCAN_IGNORE = re.compile(
    r"^(localhost|127\.0\.0\.1|0\.0\.0\.0)|claude\.ai|anthropic\.com|"
    r"google\.com/search|duckduckgo\.com|bing\.com", re.I)


def detect_source(url: str) -> str:
    for name, pat in SOURCES:
        if pat.search(url or ""):
            return name
    return "Web"


def is_doc_worthy(url: str) -> bool:
    """A recognized doc/collab host — the kind of link worth keeping."""
    return detect_source(url) != "Web"


# code repos aren't the "files I open and lose" the scan is meant to catch;
# they're addable by hand but don't clutter the passive suggestion panel
_SCAN_EXCLUDE_SOURCES = {"GitHub"}


def is_scan_worthy(url: str) -> bool:
    """Stricter than is_doc_worthy: a real document link (has a path), from a
    file/doc host, excluding code repos — what the session scan suggests."""
    if not is_doc_worthy(url) or detect_source(url) in _SCAN_EXCLUDE_SOURCES:
        return False
    try:
        p = urlparse(url)
    except Exception:
        return False
    if not (p.query or len(p.path.strip("/")) >= 2):
        return False
    return not is_portal_noise(url)


# SharePoint/Office/SSO plumbing that shows up in history but isn't a document:
# auth redirects, sign-in/out, SAML, folder & list views, site landing pages.
_OFFICE_NOISE = re.compile(
    r"/_layouts/15/(authenticate|signout|start\b|userdisp|settings|"
    r"sharepoint\.aspx|onedrive\.aspx|os/|listform)|"
    r"/_forms/|/_login|/_api/|/saml/|/oauth|/login\.|login\.microsoftonline|"
    r"email_only|safelinks|isso=true|SignOut|ReturnUrl=|/:f:/|/:fl:/", re.I)


def is_portal_noise(url: str) -> bool:
    """True for auth/redirect/folder/landing URLs that aren't an actual doc."""
    try:
        p = urlparse(url)
    except Exception:
        return True
    path, q = p.path.lower(), p.query.lower()
    if _OFFICE_NOISE.search(url):
        return True
    if "allitems.aspx" in path or "rootfolder=" in q or "/forms/" in path:
        return True
    if path.rstrip("/").endswith("/shared documents") or path.endswith("/sitepages"):
        return True
    # a bare site/personal landing page with nothing after it
    if re.fullmatch(r"/(sites|personal|:[a-z]:|teams)/[^/]+/?", p.path, re.I):
        return True
    return False


def _junk_title(title: str) -> bool:
    return bool(re.match(
        r"^\s*(sign(ing)?[ -]?(in|out)|sign in to your account|working\.\.\.|"
        r"authenticat|redirect|loading|home|"
        r"(sharepoint|figma|notion|onedrive|google drive)\s*$)", title or "", re.I))


def doc_key(url: str) -> str:
    """A stable identity for a document, so the same file reached by different
    redirect URLs collapses to one candidate."""
    try:
        p = urlparse(url)
    except Exception:
        return url.lower()
    qs = parse_qs(p.query)
    if qs.get("sourcedoc"):
        return "sd:" + unquote(qs["sourcedoc"][0]).strip("{}").lower()
    m = re.search(r"/(IQ[A-Za-z0-9_\-]{12,})", url)          # Office share-link id
    if m:
        return "id:" + m.group(1)
    for seg in reversed([s for s in p.path.split("/") if s]):
        if re.search(r"\.(docx|xlsx|pptx|pdf|one)$", seg, re.I):
            return "fn:" + unquote(seg).lower()
    if p.netloc.endswith("figma.com"):
        fm = re.search(r"/(?:design|file)/([A-Za-z0-9]+)", p.path)  # figma file key
        if fm:
            return "fig:" + fm.group(1)
    return "u:" + (p.netloc + p.path).lower().rstrip("/")


def _canonical_score(url: str) -> int:
    """Higher = cleaner link to keep among duplicates of one doc."""
    s = 0
    if re.search(r"/:[wxpo]:/r/", url):     # clean Word/Excel/PPT/OneNote share link
        s += 3
    if "/sites/" in url or "/personal/" in url:
        s += 1
    if "_layouts/15" in url.lower():
        s -= 2
    if "authenticate" in url.lower() or "returnurl=" in url.lower():
        s -= 3
    return s


# --------------------------------------------------------------------------- #
# store
# --------------------------------------------------------------------------- #
def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load() -> dict:
    d = collect.load_json(STASH_PATH, {})
    d.setdefault("links", {})
    d.setdefault("dismissed", [])
    return d


def save(d: dict) -> None:
    collect.write_json(STASH_PATH, d)


def _open_tab_title(url: str) -> str | None:
    state = collect.load_json(STATE_PATH, {"tabs": {}})
    tab = (state.get("tabs") or {}).get(url)
    return tab.get("title") if tab else None


def _deduced(url: str) -> dict:
    ded = collect.load_json(DEDUCTIONS_PATH, {"tabs": {}})
    return (ded.get("tabs") or {}).get(url, {})


def derive_title(url: str) -> str:
    """Best-effort human title without fetching. Prefer an open tab's real
    title; then a SharePoint/Office doc name from the query string; then the
    last meaningful path segment; then the host."""
    live = _open_tab_title(url)
    if live:
        return live
    try:
        p = urlparse(url)
    except Exception:
        return url
    qs = parse_qs(p.query)
    for key in ("file", "title", "sourcedoc", "id"):
        if qs.get(key):
            val = unquote(qs[key][0]).strip("{}")
            # a bare GUID / opaque id is a worse title than the site name below
            if val and not val.startswith("http") and not re.fullmatch(r"[0-9a-fA-F\-]{16,}", val):
                return re.sub(r"\.(docx|xlsx|pptx|pdf|one)$", "", val, flags=re.I)
    segs = [unquote(s) for s in p.path.split("/") if s]
    # an actual document filename in the path wins over everything below
    for seg in reversed(segs):
        m = re.search(r"(.+)\.(docx|xlsx|pptx|pdf|one|key|pages|numbers)$", seg, re.I)
        if m:
            return re.sub(r"\s+", " ", m.group(1).replace("_", " ").replace("-", " ")).strip()
    # SharePoint/Confluence: the /sites/<name> or /spaces/<name> segment names it
    for marker in ("sites", "spaces", "teams"):
        if marker in [s.lower() for s in segs]:
            i = [s.lower() for s in segs].index(marker)
            if i + 1 < len(segs):
                name = segs[i + 1].replace("-", " ").replace("_", " ").strip()
                if len(name) > 2:
                    return name
    skip = {"edit", "view", "d", "document", "spreadsheets", "presentation",
            "_layouts", "15", "doc.aspx", "file", "sites", "browse", "blob", "tree"}
    meaningful = [s for s in segs
                  if s.lower() not in skip and not re.fullmatch(r"[0-9a-f\-]{16,}", s)]
    if meaningful:
        last = meaningful[-1]
        last = re.sub(r"\.(docx|xlsx|pptx|pdf|aspx|one)$", "", last, flags=re.I)
        last = last.replace("-", " ").replace("_", " ").strip()
        if len(last) > 2:
            return last
    return (p.netloc or url).replace("www.", "")


# --------------------------------------------------------------------------- #
# mutations
# --------------------------------------------------------------------------- #
def add(url: str, title: str | None = None, tags=None, note: str | None = None,
        origin: str = "manual", star: bool = False) -> tuple[dict, bool]:
    """Add a link, or merge into an existing one. Returns (record, is_new)."""
    url = (url or "").strip()
    if not url:
        raise ValueError("empty url")
    d = load()
    links = d["links"]
    now = _now()
    tags = [t.strip() for t in (tags or []) if t.strip()]

    existing = links.get(url)
    if existing:
        if title:
            existing["title"] = title
        if tags:
            existing["tags"] = sorted(set(existing.get("tags", [])) | set(tags))
        if note:
            existing["note"] = note
        if star:
            existing["starred"] = True
        existing["updated"] = now
        save(d)
        return existing, False

    ded = _deduced(url)
    rec = {
        "url": url,
        "title": title or derive_title(url),
        "source": detect_source(url),
        "tags": sorted(set(tags)),
        "note": note or "",
        "starred": bool(star),
        "added": now,
        "updated": now,
        "open_count": 0,
        "last_opened": None,
        "origin": origin,
        "workstream": ded.get("workstream", ""),
        "initiative": ded.get("initiative", ""),
    }
    links[url] = rec
    d["dismissed"] = [u for u in d["dismissed"] if u != url]  # keeping wins
    save(d)
    return rec, True


def remove(url: str) -> bool:
    d = load()
    if url in d["links"]:
        del d["links"][url]
        save(d)
        return True
    return False


def dismiss(url: str) -> None:
    """Never suggest this URL from a session scan again (without stashing it)."""
    d = load()
    if url not in d["dismissed"]:
        d["dismissed"].append(url)
    save(d)


def set_star(url: str, on: bool = True) -> bool:
    d = load()
    if url in d["links"]:
        d["links"][url]["starred"] = bool(on)
        d["links"][url]["updated"] = _now()
        save(d)
        return True
    return False


def set_tags(url: str, tags, replace: bool = False) -> bool:
    tags = [t.strip() for t in tags if t.strip()]
    d = load()
    if url not in d["links"]:
        return False
    cur = set() if replace else set(d["links"][url].get("tags", []))
    d["links"][url]["tags"] = sorted(cur | set(tags))
    d["links"][url]["updated"] = _now()
    save(d)
    return True


def set_note(url: str, text: str) -> bool:
    d = load()
    if url not in d["links"]:
        return False
    d["links"][url]["note"] = text
    d["links"][url]["updated"] = _now()
    save(d)
    return True


def touch(url: str) -> None:
    """Record that a stashed link was opened (bumps count + recency)."""
    d = load()
    if url in d["links"]:
        d["links"][url]["open_count"] = d["links"][url].get("open_count", 0) + 1
        d["links"][url]["last_opened"] = _now()
        save(d)


# --------------------------------------------------------------------------- #
# query
# --------------------------------------------------------------------------- #
def _match(rec: dict, terms: list[str]) -> bool:
    hay = " ".join([
        rec.get("title", ""), rec.get("url", ""), rec.get("note", ""),
        rec.get("source", ""), rec.get("workstream", ""),
        rec.get("initiative", ""), " ".join(rec.get("tags", [])),
    ]).lower()
    return all(t in hay for t in terms)


def search(query: str | None = None, source: str | None = None,
           tag: str | None = None, starred: bool = False,
           sort: str = "recent") -> list[dict]:
    d = load()
    rows = list(d["links"].values())
    if query:
        terms = [w for w in query.lower().split() if w]
        rows = [r for r in rows if _match(r, terms)]
    if source:
        rows = [r for r in rows if r.get("source", "").lower() == source.lower()]
    if tag:
        rows = [r for r in rows if tag.lower() in [t.lower() for t in r.get("tags", [])]]
    if starred:
        rows = [r for r in rows if r.get("starred")]
    if sort == "opened":
        rows.sort(key=lambda r: r.get("open_count", 0), reverse=True)
    elif sort == "title":
        rows.sort(key=lambda r: r.get("title", "").lower())
    else:  # recent — by updated, newest first
        rows.sort(key=lambda r: r.get("updated") or r.get("added") or "", reverse=True)
    return rows


def source_counts() -> dict:
    d = load()
    out: dict[str, int] = {}
    for r in d["links"].values():
        out[r.get("source", "Web")] = out.get(r.get("source", "Web"), 0) + 1
    return dict(sorted(out.items(), key=lambda kv: (-kv[1], kv[0])))


def all_tags() -> list[str]:
    d = load()
    tags: set[str] = set()
    for r in d["links"].values():
        tags.update(r.get("tags", []))
    return sorted(tags)


# --------------------------------------------------------------------------- #
# session scan — surface doc links you pasted into Claude Code but never kept
# --------------------------------------------------------------------------- #
_URL_RE = re.compile(r"https?://[^\s\)\]\}\"'>,<`]+")
# template/example URLs pasted as illustrations, not real links to keep
_PLACEHOLDER = re.compile(
    r"\$\{|%7B|<[a-z_.]+>|/owner/repo|example\.(com|org|net)|/path/to/|"
    r"YOUR[_-]|xxx+|placeholder|releases/download", re.I)


def _user_text(obj: dict) -> str:
    """Extract only what the user actually typed/pasted (string content or
    text blocks) — not tool_result output."""
    if obj.get("type") != "user":
        return ""
    msg = obj.get("message", {})
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(b.get("text", ""))
        return "\n".join(parts)
    return ""


def scan_sessions(days: int = 7, max_files: int = 400) -> list[dict]:
    """Scan recent Claude Code transcripts for doc-worthy URLs the user pasted,
    not already stashed or dismissed. Returns newest-first candidates with a
    paste count and the surrounding line for context."""
    d = load()
    known = set(d["links"]) | set(d["dismissed"])
    cutoff = time.time() - days * 86400

    found: dict[str, dict] = {}
    files = []
    try:
        for jf in SESSIONS_DIR.rglob("*.jsonl"):
            try:
                if jf.stat().st_mtime >= cutoff:
                    files.append(jf)
            except OSError:
                continue
    except Exception:
        return []
    files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
    files = files[:max_files]

    for jf in files:
        try:
            lines = jf.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            continue
        for line in lines:
            if "http" not in line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            text = _user_text(obj)
            if not text or "http" not in text:
                continue
            ts = obj.get("timestamp", "")
            for m in _URL_RE.finditer(text):
                url = m.group(0).rstrip(".,);]")
                if url in known or _PLACEHOLDER.search(url):
                    continue
                if _SCAN_IGNORE.search(urlparse(url).netloc + urlparse(url).path):
                    continue
                if not is_scan_worthy(url):
                    continue
                rec = found.get(url)
                if rec is None:
                    ctx = text[max(0, m.start() - 60):m.start()].strip()
                    ctx = re.sub(r"\s+", " ", ctx)[-70:]
                    found[url] = {
                        "url": url,
                        "source": detect_source(url),
                        "title": derive_title(url),
                        "count": 1,
                        "last_seen": ts,
                        "context": ctx,
                    }
                else:
                    rec["count"] += 1
                    if ts > (rec["last_seen"] or ""):
                        rec["last_seen"] = ts

    out = list(found.values())
    out.sort(key=lambda r: (r["last_seen"] or ""), reverse=True)
    return out


def scan_history(days: int = 7, limit: int = 4000) -> list[dict]:
    """Scan Chrome history for doc-worthy URLs the user opened but never kept,
    filtering out auth/redirect/folder plumbing. The richer source — it catches
    every doc opened, not only those pasted into a session."""
    d = load()
    known = set(d["links"]) | set(d["dismissed"])
    try:
        rows = collect.chrome_recent_visits(hours=days * 24, limit=limit)
    except Exception:
        return []
    found: dict[str, dict] = {}
    for r in rows:
        url = (r.get("url") or "").rstrip(".,);]")
        if not url or url in known or _PLACEHOLDER.search(url):
            continue
        if _SCAN_IGNORE.search(urlparse(url).netloc + urlparse(url).path):
            continue
        if not is_scan_worthy(url):
            continue
        title = r.get("title") or ""
        if _junk_title(title) or not title:
            title = derive_title(url)
        v = r.get("visited")
        ts = ""
        if v:
            try:
                ts = datetime.fromtimestamp(v, tz=timezone.utc).isoformat()
            except Exception:
                ts = ""
        rec = found.get(url)
        if rec is None:
            found[url] = {"url": url, "source": detect_source(url), "title": title,
                          "count": 1, "last_seen": ts, "origin": "history"}
        else:
            rec["count"] += 1
            if ts > (rec["last_seen"] or ""):
                rec["last_seen"] = ts
    return list(found.values())


def scan(days: int = 7, history: bool = True, sessions: bool = True) -> list[dict]:
    """Unified doc-link scan across Chrome history and Claude Code sessions.
    Collapses the same document reached by different redirect URLs into one
    candidate (keeping the cleanest link + best title), and drops anything
    already stashed or dismissed. Newest first."""
    d = load()
    known_keys = {doc_key(u) for u in (set(d["links"]) | set(d["dismissed"]))}

    cands: list[dict] = []
    if history:
        cands += scan_history(days)
    if sessions:
        cands += scan_sessions(days)

    groups: dict[str, dict] = {}
    for c in cands:
        k = doc_key(c["url"])
        if k in known_keys:
            continue
        g = groups.get(k)
        if g is None:
            groups[k] = {**c, "_origins": {c.get("origin", "session")}}
            continue
        g["count"] = g.get("count", 1) + c.get("count", 1)
        g["_origins"].add(c.get("origin", "session"))
        if (c.get("last_seen") or "") > (g.get("last_seen") or ""):
            g["last_seen"] = c.get("last_seen")
        if _canonical_score(c["url"]) > _canonical_score(g["url"]):
            g["url"] = c["url"]
        if (_junk_title(g.get("title", "")) or len(g.get("title", "")) < 4) \
                and not _junk_title(c.get("title", "")):
            g["title"] = c["title"]

    out = []
    for g in groups.values():
        g["origin"] = "+".join(sorted(g.pop("_origins")))
        out.append(g)
    out.sort(key=lambda r: (r["last_seen"] or ""), reverse=True)
    return out
