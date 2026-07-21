"""webindex.py — a local, private, full-text index of the pages you actually
read — your logged-in web, made searchable.

Enterprise search (Glean etc.) can only index what IT connected. It can't read
the SharePoint doc you opened this morning, the Confluence page behind SSO, or
the Jira ticket in your session. Handle can — it's YOUR authenticated browser.

Two things fill this index:
  · `tab read` — an agent deliberately reads a page          (source="read")
  · passive capture — you dwelled on a page long enough to
    have actually read it, and the extension captured it     (source="passive")

The second is what makes the corpus grow on its own. See capture.py for the
policy that decides what's eligible.

SQLite FTS5 — stdlib, no embeddings, no API, no network. One row per url;
re-reading replaces the row (freshest wins). The `hash` column makes an unchanged
re-visit cheap to skip, and gives a later temporal layer something to diff.
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

_DB = Path(__file__).resolve().parent / "webindex.db"
_MIN_CHARS = 200          # don't index thin pages (nav shells, error stubs)
_COLS = ["url", "title", "content", "kind", "ts", "source", "hash"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8", "replace")).hexdigest()[:16]


def _create(c: sqlite3.Connection, name: str = "pages") -> None:
    c.execute(
        f"CREATE VIRTUAL TABLE IF NOT EXISTS {name} USING fts5("
        "url UNINDEXED, title, content, kind UNINDEXED, ts UNINDEXED, "
        "source UNINDEXED, hash UNINDEXED, tokenize='porter unicode61')")


def _migrate(c: sqlite3.Connection) -> None:
    """v1 (url,title,content,kind,ts) → v2 (+source,+hash). FTS5 can't ALTER, so
    rebuild into a new table and swap. Existing rows are tagged source='read',
    which is what they were."""
    have = [r[1] for r in c.execute("PRAGMA table_info(pages)")]
    if not have or set(_COLS).issubset(have):
        return
    _create(c, "pages_v2")
    for url, title, content, kind, ts in c.execute(
            "SELECT url, title, content, kind, ts FROM pages"):
        c.execute("INSERT INTO pages_v2(url,title,content,kind,ts,source,hash) "
                  "VALUES(?,?,?,?,?,?,?)",
                  (url, title, content, kind, ts, "read", _hash(content)))
    c.execute("DROP TABLE pages")
    c.execute("ALTER TABLE pages_v2 RENAME TO pages")
    c.commit()


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(_DB)
    _create(c)
    _migrate(c)
    return c


def index_page(url: str, title: str, content: str, kind: str = "html",
               source: str = "read") -> bool:
    """Upsert a page's text. Returns True if indexed, False if skipped (too thin
    / non-indexable kind / local file)."""
    if not url or not content or len(content) < _MIN_CHARS:
        return False
    if url.startswith("file://") or kind in ("figma", "pdf", "office"):
        return False  # pointers, not text we can search usefully
    try:
        c = _conn()
        c.execute("DELETE FROM pages WHERE url = ?", (url,))
        c.execute("INSERT INTO pages(url,title,content,kind,ts,source,hash) "
                  "VALUES(?,?,?,?,?,?,?)",
                  (url, title or "", content, kind, _now(), source, _hash(content)))
        c.commit()
        c.close()
        return True
    except Exception:
        return False


def is_fresh(url: str, content: str, within_hours: int = 6) -> bool:
    """True if this exact content is already indexed for this url and was seen
    recently — so a passive re-visit doesn't churn the row on every tab switch.
    Changed content always returns False (it's worth re-indexing)."""
    if not _DB.exists():
        return False
    try:
        c = _conn()
        row = c.execute("SELECT ts, hash FROM pages WHERE url = ?", (url,)).fetchone()
        c.close()
    except Exception:
        return False
    if not row or row[1] != _hash(content):
        return False
    try:
        seen = datetime.fromisoformat(row[0])
        if seen.tzinfo is None:
            seen = seen.replace(tzinfo=timezone.utc)
    except Exception:
        return False
    return datetime.now(timezone.utc) - seen < timedelta(hours=within_hours)


def _fts_query(q: str) -> str:
    """Turn a plain phrase into a safe FTS5 OR-query (bare words, quoted, ORed)."""
    words = re.findall(r"[\w']+", q.lower())
    return " OR ".join('"%s"' % w for w in words) if words else '""'


def search(query: str, limit: int = 8, source: str | None = None,
           since_days: int | None = None) -> list[dict]:
    """BM25-ranked passages for a query. [{url,title,kind,ts,source,snippet,score}].

    `limit` doubles as the knob a caller uses to widen recall before re-ranking
    semantically — pull 40, let the agent pick the 5 that actually mean it.
    """
    if not _DB.exists() or not query.strip():
        return []
    sql = ("SELECT url, title, kind, ts, source, "
           "snippet(pages, 2, '«', '»', '…', 18) AS snip, bm25(pages) AS score "
           "FROM pages WHERE pages MATCH ?")
    params: list = [_fts_query(query)]
    if source:
        sql += " AND source = ?"
        params.append(source)
    if since_days:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=since_days)
                  ).isoformat(timespec="seconds")
        sql += " AND ts >= ?"
        params.append(cutoff)
    sql += " ORDER BY bm25(pages) LIMIT ?"
    params.append(limit)
    try:
        c = _conn()
        rows = c.execute(sql, params).fetchall()
        c.close()
    except Exception:
        return []
    return [{"url": r[0], "title": r[1], "kind": r[2], "ts": r[3], "source": r[4],
             "snippet": " ".join((r[5] or "").split()), "score": round(r[6], 3)}
            for r in rows]


def recent(limit: int = 15, source: str | None = None) -> list[dict]:
    """Most recently indexed pages — what the corpus just learned."""
    if not _DB.exists():
        return []
    sql = "SELECT url, title, ts, source, length(content) FROM pages"
    params: list = []
    if source:
        sql += " WHERE source = ?"
        params.append(source)
    sql += " ORDER BY ts DESC LIMIT ?"
    params.append(limit)
    try:
        c = _conn()
        rows = c.execute(sql, params).fetchall()
        c.close()
    except Exception:
        return []
    return [{"url": r[0], "title": r[1], "ts": r[2], "source": r[3], "chars": r[4]}
            for r in rows]


def domains(limit: int = 200) -> list[dict]:
    """Every site in the corpus, with how much of it you've actually read.
    This is the answer to 'what is even in here' — and the handle you grab to
    throw a site out of the index."""
    if not _DB.exists():
        return []
    try:
        c = _conn()
        rows = c.execute(
            "SELECT url, length(content), ts, source FROM pages").fetchall()
        c.close()
    except Exception:
        return []
    agg: dict[str, dict] = {}
    for url, n, ts, source in rows:
        try:
            host = urlparse(url).netloc.lower()
        except Exception:
            continue
        d = agg.setdefault(host, {"domain": host, "pages": 0, "chars": 0,
                                  "last": "", "passive": 0})
        d["pages"] += 1
        d["chars"] += n or 0
        d["passive"] += 1 if source == "passive" else 0
        if (ts or "") > d["last"]:
            d["last"] = ts or ""
    out = sorted(agg.values(), key=lambda d: -d["pages"])
    return out[:limit]


def pages(domain: str | None = None, source: str | None = None,
          q: str | None = None, limit: int = 100, offset: int = 0) -> list[dict]:
    """Browse the corpus: what's in it, newest first, optionally filtered to one
    site / one source, or narrowed by a text query."""
    if not _DB.exists():
        return []
    if q and q.strip():
        hits = search(q, limit=limit * 3, source=source)
        if domain:
            hits = [h for h in hits if urlparse(h["url"]).netloc.lower() == domain.lower()]
        return hits[:limit]
    sql = "SELECT url, title, kind, ts, source, length(content) FROM pages WHERE 1=1"
    params: list = []
    if source:
        sql += " AND source = ?"
        params.append(source)
    sql += " ORDER BY ts DESC"
    try:
        c = _conn()
        rows = c.execute(sql, params).fetchall()
        c.close()
    except Exception:
        return []
    out = []
    for url, title, kind, ts, src, n in rows:
        if domain and urlparse(url).netloc.lower() != domain.lower():
            continue
        out.append({"url": url, "title": title, "kind": kind, "ts": ts,
                    "source": src, "chars": n, "snippet": ""})
    return out[offset:offset + limit]


def forget(domain: str) -> int:
    """Drop every page from a site out of the index. Pairs with an exclusion —
    the exclusion stops new pages, this removes what's already there."""
    if not _DB.exists() or not domain:
        return 0
    try:
        c = _conn()
        urls = [r[0] for r in c.execute("SELECT url FROM pages").fetchall()
                if urlparse(r[0]).netloc.lower() == domain.lower()]
        for u in urls:
            c.execute("DELETE FROM pages WHERE url = ?", (u,))
        c.commit()
        c.close()
        return len(urls)
    except Exception:
        return 0


def stats() -> dict:
    if not _DB.exists():
        return {"pages": 0, "passive": 0, "read": 0, "today": 0, "chars": 0}
    try:
        c = _conn()
        n = c.execute("SELECT count(*) FROM pages").fetchone()[0]
        by = dict(c.execute("SELECT source, count(*) FROM pages GROUP BY source").fetchall())
        chars = c.execute("SELECT coalesce(sum(length(content)),0) FROM pages").fetchone()[0]
        today = c.execute("SELECT count(*) FROM pages WHERE ts >= ?",
                          (datetime.now(timezone.utc).strftime("%Y-%m-%d"),)).fetchone()[0]
        c.close()
    except Exception:
        return {"pages": 0, "passive": 0, "read": 0, "today": 0, "chars": 0}
    return {"pages": n, "passive": by.get("passive", 0), "read": by.get("read", 0),
            "today": today, "chars": chars,
            "db_mb": round(_DB.stat().st_size / 1e6, 1)}
