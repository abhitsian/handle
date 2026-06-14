#!/usr/bin/env python3
"""Read-only views into Chrome's own local data — bookmarks, downloads,
omnibox searches, history journeys, most-visited, and the session file.

Everything here is the *pointer*, never the payload: titles, links, search
terms, file paths. Page content stays deliberate — you reopen a tab and
`read` it. Fully local: SQLite DBs are copied before being opened read-only
(the live files are locked while Chrome runs), JSON/binary files are read in
place. No network, no credentials — this module never touches Login Data,
Cookies, or Web Data. Pure stdlib.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import struct
import tempfile
import time
from pathlib import Path

CHROME_BASE = Path.home() / "Library/Application Support/Google/Chrome"
CHROME_EPOCH_OFFSET = 11_644_473_600  # seconds between 1601-01-01 and 1970-01-01
SKIP_PREFIXES = ("chrome://", "chrome-extension://", "about:", "edge://", "devtools://")


def _chrome_to_unix(t: int | None) -> float | None:
    if not t:
        return None
    return t / 1_000_000 - CHROME_EPOCH_OFFSET


def _unix_to_chrome(unix: float) -> int:
    return int((unix + CHROME_EPOCH_OFFSET) * 1_000_000)


def profiles() -> list[Path]:
    """Every real Chrome profile dir (Default, Profile 1, …) that has data."""
    if not CHROME_BASE.exists():
        return []
    out = []
    for name in ("Default", *[f"Profile {i}" for i in range(1, 12)]):
        p = CHROME_BASE / name
        if p.is_dir():
            out.append(p)
    # any other profile dirs with a History DB
    for p in CHROME_BASE.glob("*/History"):
        if p.parent not in out:
            out.append(p.parent)
    return out


def _query(db_name: str, sql: str, params: tuple = ()) -> list[tuple]:
    """Run a read-only query against `db_name` in every profile, concatenated.

    The live DB is locked while Chrome runs, so each file is copied first.
    """
    rows: list[tuple] = []
    for prof in profiles():
        db = prof / db_name
        if not db.exists():
            continue
        tmp = None
        try:
            fd, tmp = tempfile.mkstemp(suffix=".db")
            os.close(fd)
            shutil.copy2(db, tmp)
            con = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
            rows.extend(con.execute(sql, params).fetchall())
            con.close()
        except Exception:
            continue
        finally:
            if tmp and os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
    return rows


def _matches(text: str, terms: list[str]) -> bool:
    t = text.lower()
    return all(w in t for w in terms)


# --------------------------------------------------------------------------- #
# bookmarks — Bookmarks is a JSON file, no DB
# --------------------------------------------------------------------------- #
def bookmarks(term: str | None = None, limit: int = 200) -> list[dict]:
    terms = [w for w in (term or "").lower().split() if w]
    out: list[dict] = []
    for prof in profiles():
        f = prof / "Bookmarks"
        if not f.exists():
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue

        def walk(node, folder):
            for ch in node.get("children", []):
                if ch.get("type") == "url":
                    title = ch.get("name", "")
                    url = ch.get("url", "")
                    if url.startswith(SKIP_PREFIXES):
                        continue
                    if terms and not _matches(f"{title} {url} {folder}", terms):
                        continue
                    added = ch.get("date_added")
                    out.append({
                        "title": title, "url": url, "folder": folder,
                        "added": _chrome_to_unix(int(added)) if added else None,
                    })
                elif ch.get("type") == "folder":
                    walk(ch, f"{folder}/{ch.get('name','')}".strip("/"))

        for root in data.get("roots", {}).values():
            if isinstance(root, dict):
                walk(root, root.get("name", ""))
    out.sort(key=lambda r: r["added"] or 0, reverse=True)
    return out[:limit]


# --------------------------------------------------------------------------- #
# downloads — History.downloads, resolves to a local file path
# --------------------------------------------------------------------------- #
def downloads(term: str | None = None, days: float = 30, limit: int = 40) -> list[dict]:
    cutoff = _unix_to_chrome(time.time() - days * 86400)
    rows = _query(
        "History",
        "SELECT target_path, tab_url, start_time, total_bytes, state, mime_type "
        "FROM downloads WHERE start_time > ? ORDER BY start_time DESC",
        (cutoff,),
    )
    terms = [w for w in (term or "").lower().split() if w]
    out = []
    for path, url, start, size, state, mime in rows:
        name = os.path.basename(path or "")
        if terms and not _matches(f"{name} {url or ''}", terms):
            continue
        out.append({
            "name": name, "path": path, "source_url": url,
            "downloaded": _chrome_to_unix(start), "bytes": size,
            "complete": state == 1, "exists": bool(path and os.path.exists(path)),
            "mime": mime or "",
        })
    out.sort(key=lambda r: r["downloaded"] or 0, reverse=True)
    return out[:limit]


# --------------------------------------------------------------------------- #
# omnibox searches — History.keyword_search_terms joined to urls
# --------------------------------------------------------------------------- #
def searches(term: str | None = None, days: float = 30, limit: int = 40) -> list[dict]:
    cutoff = _unix_to_chrome(time.time() - days * 86400)
    rows = _query(
        "History",
        "SELECT k.term, u.url, u.last_visit_time "
        "FROM keyword_search_terms k JOIN urls u ON k.url_id = u.id "
        "WHERE u.last_visit_time > ? ORDER BY u.last_visit_time DESC",
        (cutoff,),
    )
    terms = [w for w in (term or "").lower().split() if w]
    seen, out = set(), []
    for q, url, lvt in rows:
        key = (q or "").strip().lower()
        if not key or key in seen:
            continue
        if terms and not _matches(key, terms):
            continue
        seen.add(key)
        out.append({"term": q, "result_url": url, "when": _chrome_to_unix(lvt)})
    out.sort(key=lambda r: r["when"] or 0, reverse=True)
    return out[:limit]


# --------------------------------------------------------------------------- #
# journeys — Chrome's own ML clustering of history into topical sessions
# --------------------------------------------------------------------------- #
def journeys(term: str | None = None, days: float = 30, limit: int = 25) -> list[dict]:
    cutoff = _unix_to_chrome(time.time() - days * 86400)
    # keywords per cluster
    kw = _query("History", "SELECT cluster_id, keyword FROM cluster_keywords")
    kw_by_cluster: dict[int, list[str]] = {}
    for cid, k in kw:
        kw_by_cluster.setdefault(cid, []).append(k)
    # member visits per cluster (display urls), only recent visits
    members = _query(
        "History",
        "SELECT cv.cluster_id, cv.url_for_display, v.visit_time "
        "FROM clusters_and_visits cv JOIN visits v ON cv.visit_id = v.id "
        "WHERE v.visit_time > ?",
        (cutoff,),
    )
    by_cluster: dict[int, dict] = {}
    for cid, url, vt in members:
        if not url or url.startswith(SKIP_PREFIXES):
            continue
        c = by_cluster.setdefault(cid, {"urls": set(), "last": 0})
        c["urls"].add(url)
        u = _chrome_to_unix(vt) or 0
        if u > c["last"]:
            c["last"] = u
    terms = [w for w in (term or "").lower().split() if w]
    out = []
    for cid, c in by_cluster.items():
        kws = kw_by_cluster.get(cid, [])
        # a single-page cluster with no keywords is noise, not a journey
        if not kws and len(c["urls"]) < 2:
            continue
        label = ", ".join(kws[:6]) if kws else _domains_label(c["urls"])
        hay = f"{label} {' '.join(c['urls'])}".lower()
        if terms and not _matches(hay, terms):
            continue
        out.append({
            "keywords": kws[:8], "label": label,
            "urls": sorted(c["urls"])[:12], "page_count": len(c["urls"]),
            "last": c["last"],
        })
    out.sort(key=lambda r: (r["last"], r["page_count"]), reverse=True)
    return out[:limit]


def _domains_label(urls) -> str:
    """Fallback journey label from the distinct domains in the cluster."""
    from urllib.parse import urlparse
    seen, doms = set(), []
    for u in urls:
        try:
            netloc = urlparse(u).netloc
            # url_for_display is often scheme-less ("stratechery.com/x")
            d = (netloc or u.split("/")[0]).replace("www.", "")
        except Exception:
            continue
        if d and d not in seen:
            seen.add(d)
            doms.append(d)
    return ", ".join(doms[:4])


# --------------------------------------------------------------------------- #
# most-visited — History.segments / segment_usage (Chrome's own ranking)
# --------------------------------------------------------------------------- #
def most_visited(limit: int = 25) -> list[dict]:
    rows = _query(
        "History",
        "SELECT u.url, u.title, SUM(su.visit_count) AS visits, MAX(u.last_visit_time) "
        "FROM segments s "
        "JOIN segment_usage su ON su.segment_id = s.id "
        "JOIN urls u ON s.url_id = u.id "
        "GROUP BY s.id ORDER BY visits DESC",
    )
    out = []
    for url, title, visits, lvt in rows:
        if not url or url.startswith(SKIP_PREFIXES):
            continue
        out.append({
            "url": url, "title": title or "", "visits": int(visits or 0),
            "last": _chrome_to_unix(lvt),
        })
    out.sort(key=lambda r: r["visits"], reverse=True)
    return out[:limit]


# --------------------------------------------------------------------------- #
# session file (SNSS) — navigation entries + best-effort tab groups
# --------------------------------------------------------------------------- #
class _Pickle:
    """Chrome's Pickle reader: 4-byte-aligned ints and length-prefixed strings.
    The leading uint32 is the payload size, which we skip."""

    def __init__(self, b: bytes):
        self.b = b
        self.o = 4

    def _align(self):
        self.o = (self.o + 3) & ~3

    def i32(self) -> int:
        v = struct.unpack_from("<i", self.b, self.o)[0]
        self.o += 4
        return v

    def u32(self) -> int:
        v = struct.unpack_from("<I", self.b, self.o)[0]
        self.o += 4
        return v

    def s8(self) -> str:
        n = self.u32()
        raw = self.b[self.o:self.o + n]
        self.o += n
        self._align()
        return raw.decode("utf-8", "replace")

    def s16(self) -> str:
        n = self.u32()
        raw = self.b[self.o:self.o + n * 2]
        self.o += n * 2
        self._align()
        return raw.decode("utf-16-le", "replace")


# SessionService command ids (stable subset across Chrome versions)
_CMD_UPDATE_TAB_NAVIGATION = 6
_CMD_SET_TAB_WINDOW = 0
_CMD_SET_PINNED_STATE = 12
_CMD_TAB_CLOSED = 16
_CMD_SET_SELECTED_NAVIGATION_INDEX = 7


def _latest_session_file() -> Path | None:
    best = None
    best_m = -1.0
    for prof in profiles():
        sess = prof / "Sessions"
        if not sess.is_dir():
            continue
        for f in sess.glob("Session_*"):
            m = f.stat().st_mtime
            if m > best_m:
                best, best_m = f, m
    return best


def _read_commands(data: bytes):
    """Yield (command_id, payload_without_id) from an SNSS file body."""
    if data[:4] != b"SNSS":
        return
    off = 8  # 'SNSS' + int32 version
    while off + 2 <= len(data):
        size = struct.unpack_from("<H", data, off)[0]
        if size == 0:
            break
        off += 2
        if off + size > len(data):
            break
        yield data[off], data[off + 1:off + size]
        off += size


def session_tabs() -> list[dict]:
    """Tabs reconstructed from the latest session file: url, title, window.

    The session file holds every tab the current session knows about with its
    real title — richer than History (whose titles are often blank). We keep
    the last navigation seen per tab id.
    """
    f = _latest_session_file()
    if not f:
        return []
    try:
        data = f.read_bytes()
    except Exception:
        return []
    navs: dict[int, dict] = {}
    windows: dict[int, int] = {}
    pinned: set[int] = set()
    for cid, payload in _read_commands(data):
        try:
            if cid == _CMD_UPDATE_TAB_NAVIGATION:
                p = _Pickle(payload)
                tab_id = p.i32()
                p.i32()  # nav index
                url = p.s8()
                title = p.s16()
                if url and not url.startswith(SKIP_PREFIXES):
                    navs[tab_id] = {"tab_id": tab_id, "url": url, "title": title}
            elif cid == _CMD_SET_TAB_WINDOW:
                p = _Pickle(payload)
                win = p.i32()
                tab = p.i32()
                windows[tab] = win
            elif cid == _CMD_SET_PINNED_STATE:
                p = _Pickle(payload)
                tab = p.i32()
                if p.i32():
                    pinned.add(tab)
        except Exception:
            continue
    out = []
    for tab_id, nav in navs.items():
        nav["window"] = windows.get(tab_id)
        nav["pinned"] = tab_id in pinned
        out.append(nav)
    return out


def recently_closed(open_urls: set[str], limit: int = 40) -> list[dict]:
    """Session tabs whose URL is no longer open — the 'reopen what I closed'
    set, with real titles from the session file."""
    open_set = {u for u in open_urls if u}
    out = [t for t in session_tabs() if t["url"] not in open_set]
    # de-dupe by url, keep first (latest nav wins via dict order)
    seen, deduped = set(), []
    for t in out:
        if t["url"] in seen:
            continue
        seen.add(t["url"])
        deduped.append(t)
    return deduped[:limit]


if __name__ == "__main__":
    import sys
    fn = sys.argv[1] if len(sys.argv) > 1 else "bookmarks"
    print(json.dumps(globals()[fn](), indent=2, ensure_ascii=False, default=str))
