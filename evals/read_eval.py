#!/usr/bin/env python3
"""Eval for `tab read` across content types + reference resolution.

Two layers:
  1. Deterministic checks — type detection, Figma/Google URL parsing — that
     run anywhere, no browser needed.
  2. Live checks — actual reads against whatever tabs are open right now. A
     kind that isn't open is SKIPped, not failed (this is a browser-dependent
     tool; the live set is the user's real Chrome).

Pass criteria are about *behaviour*, not exact content: an HTML tab must return
real text; a Google doc must return its export OR a precise failure note (a
shared file with export disabled is a graceful pass, not a bug); Figma must
hand back a file_key rather than scraping; PDF/Office must explain themselves.

    python3 evals/read_eval.py        # scorecard; exits non-zero on any FAIL
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import collect  # noqa: E402
import tab  # noqa: E402

PASS, FAIL, SKIP = [], [], []


def ok(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))


def skip(name, why):
    SKIP.append((name, why))


class Args:
    """Stand-in for the argparse namespace _read_one expects."""
    def __init__(self, **kw):
        self.__dict__.update({"live": False, "md": False, "chars": None, "json": False})
        self.__dict__.update(kw)


# ---------------------------------------------------------------- 1. detection
DETECT = {
    "https://www.figma.com/design/ABC123/X?node-id=25-613": "figma",
    "https://www.figma.com/proto/ABC123/X?node-id=9-9": "figma",
    "https://docs.google.com/document/d/AAA/edit?tab=t.0": "gdoc",
    "https://docs.google.com/spreadsheets/d/BBB/edit#gid=7": "gsheet",
    "https://docs.google.com/presentation/d/CCC/edit": "gslides",
    "https://example.com/paper.pdf?dl=1": "pdf",
    "https://x.sharepoint.com/:w:/r/sites/y/Doc.docx": "office",
    "https://stratechery.com/": "html",
}
for url, expect in DETECT.items():
    got = tab.detect_kind(url)
    ok(f"detect · {expect}", got == expect, f"{got} ← {url[:48]}")

fr = tab._figma_ref("https://www.figma.com/design/ABC123/Name?node-id=25-613&p=f")
ok("figma · file_key", fr["file_key"] == "ABC123", str(fr))
ok("figma · node_id normalized to colon", fr["node_id"] == "25:613", str(fr))

ok("export · gdoc txt",
   tab._export_url("gdoc", "https://docs.google.com/document/d/AAA/edit")
   == "https://docs.google.com/document/d/AAA/export?format=txt")
ok("export · gsheet csv+gid",
   tab._export_url("gsheet", "https://docs.google.com/spreadsheets/d/BBB/edit#gid=7")
   == "https://docs.google.com/spreadsheets/d/BBB/export?format=csv&gid=7")
ok("export · gslides txt",
   tab._export_url("gslides", "https://docs.google.com/presentation/d/CCC/edit")
   == "https://docs.google.com/presentation/d/CCC/export/txt")


# ----------------------------------------------------------------- 2. live read
tabs = tab.load_tabs()
by_kind: dict[str, list] = {}
for t in tabs:
    by_kind.setdefault(tab.detect_kind(t["url"]), []).append(t)


def read(t, **kw):
    return tab._read_one(t, Args(**kw))


# HTML — must return real text, and --md must add structure
if by_kind.get("html"):
    t = by_kind["html"][0]
    p = read(t)
    ok("html · returns text", p["kind"] == "html" and p["chars"] > 20,
       f"{t['id']} {p['chars']}c source={p['source']}")
    pm = read(t, md=True)
    has_struct = any(m in pm["content"] for m in ("# ", "](", "- ", "**", "\n\n"))
    ok("html · --md returns structured markdown",
       pm["source"] == "markdown" and pm["chars"] > 0 and has_struct,
       f"{t['id']} {pm['chars']}c")
else:
    skip("html", "none open")

# Google Doc — export text, OR a precise graceful note
if by_kind.get("gdoc"):
    t = by_kind["gdoc"][0]
    p = read(t)
    graceful = p["chars"] > 0 or bool(p.get("note"))
    ok("gdoc · export or precise note", p["kind"] == "gdoc" and graceful,
       f"{t['id']} {p['chars']}c note={(p.get('note') or '')[:46]}")
else:
    skip("gdoc", "none open")

# Google Sheet — same contract
if by_kind.get("gsheet"):
    t = by_kind["gsheet"][0]
    p = read(t)
    ok("gsheet · export or precise note",
       p["kind"] == "gsheet" and (p["chars"] > 0 or bool(p.get("note"))),
       f"{t['id']} {p['chars']}c note={(p.get('note') or '')[:46]}")
else:
    skip("gsheet", "none open")

# Office — must NOT pretend; empty + explanation
if by_kind.get("office"):
    t = by_kind["office"][0]
    p = read(t)
    ok("office · empty + explains itself",
       p["kind"] == "office" and p["chars"] == 0 and bool(p.get("note")), t["id"])
else:
    skip("office", "none open")

# Figma — pointer, never a scrape
if by_kind.get("figma"):
    t = by_kind["figma"][0]
    p = read(t)
    ok("figma · file_key, no scraped text",
       p["kind"] == "figma" and bool(p.get("file_key")) and p["chars"] == 0,
       str(p.get("file_key")))
else:
    skip("figma", "none open")

# PDF — pointer + reason
if by_kind.get("pdf"):
    t = by_kind["pdf"][0]
    p = read(t)
    ok("pdf · empty + explains itself",
       p["kind"] == "pdf" and bool(p.get("note")), t["id"])
else:
    skip("pdf", "none open")


# ----------------------------------------------- 3. describe without the number
if tabs:
    sample = (by_kind.get("html") or [tabs[0]])[0]
    word = next((w for w in sample["title"].split() if len(w) > 4), None)
    if word:
        hits = tab.resolve(word.lower(), tabs)
        ok("resolve · by a word from the title",
           any(h["id"] == sample["id"] for h in hits),
           f"'{word}' → {[h['id'] for h in hits][:5]}")
    au = collect.active_tab_url()
    ok("resolve · active = a real frontmost tab", bool(au), (au or "none")[:50])


# ------------------------------------------------------------------- scorecard
print(f"\n{'=' * 52}")
print(f"  READ EVAL — {len(PASS)} pass · {len(FAIL)} fail · {len(SKIP)} skip")
print(f"{'=' * 52}")
for n, d in PASS:
    print(f"  ✅ {n}" + (f"   — {d}" if d else ""))
for n, d in SKIP:
    print(f"  ⏭  {n}   ({d})")
for n, d in FAIL:
    print(f"  ❌ {n}   — {d}")
print()
sys.exit(1 if FAIL else 0)
