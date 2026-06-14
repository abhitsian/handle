#!/usr/bin/env python3
"""Eval for Chrome's-own-data readers (chrome_data.py) + history window.

Two layers, like read_eval:
  1. Deterministic — invariants that hold regardless of what's in Chrome:
     pointer-not-payload (rows carry links/titles/paths, never page content),
     never-touch-credentials, time conversions, keyword filter, the
     recently-closed exclusion rule.
  2. Live best-effort — the readers run against your real Chrome data; an
     empty store is SKIP, not FAIL (browser-dependent).

    python3 evals/chrome_data_eval.py
"""
from __future__ import annotations

import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import chrome_data as cd  # noqa: E402
import collect  # noqa: E402

PASS, FAIL, SKIP = [], [], []
def ok(n, c, d=""): (PASS if c else FAIL).append((n, d))
def skip(n, w): SKIP.append((n, w))

# fields that would mean we leaked page CONTENT (the pointer-not-payload line)
CONTENT_KEYS = {"content", "text", "body", "html", "snippet"}


def assert_pointer_only(name, rows):
    """No row may carry page content — only metadata pointers."""
    if not rows:
        return
    leaked = [k for r in rows for k in (r.keys() if isinstance(r, dict) else []) if k in CONTENT_KEYS]
    ok(f"{name} · pointer-not-payload (no page content)", not leaked, f"leaked keys: {set(leaked)}")


# --------------------------------------------------------- 1. deterministic
# credential safety: the module must never OPEN Login Data / Cookies / Web Data.
# (mentions in the docstring are fine; opening them is not.)
src = (Path(__file__).resolve().parent.parent / "chrome_data.py").read_text()
code = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
# strip the module docstring
code_nodoc = re.sub(r'""".*?"""', "", code, count=1, flags=re.S)
opens_creds = any(db in code_nodoc for db in ('"Login Data"', "'Login Data'", "Cookies", "Web Data"))
ok("safety · never opens Login Data / Cookies / Web Data", not opens_creds)
ok("safety · reads DBs read-only off a copy", "mode=ro" in src and "shutil.copy2" in src)

# time conversions round-trip
now = int(time.time())
back = cd._chrome_to_unix(cd._unix_to_chrome(now))
ok("time · chrome epoch round-trips", abs(back - now) < 1.0, f"{back} vs {now}")
ok("time · None visit → None", cd._chrome_to_unix(None) is None)

# scheme-less display-url domain label (journeys fallback)
lbl = cd._domains_label(["stratechery.com/2024/aggregation", "https://www.techcrunch.com/x"])
ok("journeys · domain label handles scheme-less urls",
   "stratechery.com" in lbl and "techcrunch.com" in lbl, lbl)

# recently_closed must EXCLUDE currently-open urls (the core filter contract)
st = cd.session_tabs()
if st:
    open_url = st[0]["url"]
    closed = cd.recently_closed({open_url})
    ok("closed · excludes currently-open urls",
       all(r["url"] != open_url for r in closed), f"{len(closed)} rows, excluded {open_url[:40]}")
    assert_pointer_only("closed", closed)
else:
    skip("closed · excludes open urls", "no session file / no tabs")

# history window (collect.chrome_recent_visits) — pointer rows + skip_urls honored
recent = collect.chrome_recent_visits(hours=720, limit=50)
if recent:
    keys_ok = all({"url", "title", "visited"} <= set(r) and not (CONTENT_KEYS & set(r)) for r in recent)
    ok("history · rows are pointers (url/title/visited, no content)", keys_ok)
    skipped = collect.chrome_recent_visits(hours=720, limit=50, skip_urls={recent[0]["url"]})
    ok("history · skip_urls drops the excluded url",
       all(r["url"] != recent[0]["url"] for r in skipped))
else:
    skip("history · pointer rows", "no history in window")


# --------------------------------------------------------- 2. live readers
def live(name, fn, must_have, sample_filter=None):
    """Run a reader; SKIP if empty; assert keys + pointer-only if it returned rows."""
    try:
        rows = fn()
    except Exception as e:
        ok(f"{name} · runs", False, f"raised {e!r}")
        return
    if not rows:
        skip(name, "no data in store")
        return
    has = all(must_have <= set(r) for r in rows)
    ok(f"{name} · well-formed rows", has, f"{len(rows)} rows; want keys {must_have}")
    assert_pointer_only(name, rows)
    if sample_filter:
        sample_filter(rows)


live("bookmarks", lambda: cd.bookmarks(limit=100), {"title", "url", "folder"})
live("downloads", lambda: cd.downloads(limit=50), {"name", "path", "exists"})
live("searches", lambda: cd.searches(limit=50), {"term"})
live("journeys", lambda: cd.journeys(limit=25), {"label", "urls", "page_count"})
live("most_visited", lambda: cd.most_visited(limit=25), {"url", "visits"})

# keyword prefilter actually narrows (bookmarks): a nonsense term yields nothing
ok("bookmarks · nonsense keyword returns nothing",
   cd.bookmarks("zzzznotarealbookmarkterm") == [])

bms = cd.bookmarks(limit=200)
if bms:
    # pick a word from a real bookmark title and confirm the filter finds it
    word = next((w for r in bms for w in re.findall(r"[A-Za-z]{5,}", r["title"])), None)
    if word:
        hits = cd.bookmarks(word.lower())
        ok("bookmarks · keyword prefilter finds a known title",
           any(word.lower() in (h["title"] + h["url"]).lower() for h in hits), f"'{word}' → {len(hits)}")
    else:
        skip("bookmarks · keyword prefilter", "no word-y bookmark titles")
else:
    skip("bookmarks · keyword prefilter", "no bookmarks")


# ------------------------------------------------------------- scorecard
print(f"\n{'=' * 52}")
print(f"  CHROME-DATA EVAL — {len(PASS)} pass · {len(FAIL)} fail · {len(SKIP)} skip")
print(f"{'=' * 52}")
for n, d in PASS: print(f"  ✅ {n}" + (f"   — {d}" if d else ""))
for n, d in SKIP: print(f"  ⏭  {n}   ({d})")
for n, d in FAIL: print(f"  ❌ {n}   — {d}")
print()
sys.exit(1 if FAIL else 0)
