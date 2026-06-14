#!/usr/bin/env python3
"""Eval for the cross-tab workflows: find · grep · ask · save→recall.

These are live (they run against whatever Chrome has open) and exercise the
real CLI over subprocess, the way an agent does. No open tabs / no match is a
SKIP, not a FAIL. save→recall writes a throwaway bundle and cleans it up.

    python3 evals/workflow_eval.py
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import tab  # noqa: E402

PASS, FAIL, SKIP = [], [], []
def ok(n, c, d=""): (PASS if c else FAIL).append((n, d))
def skip(n, w): SKIP.append((n, w))

TAB = [sys.executable, str(ROOT / "tab.py")]
BUNDLE_SLUG = "eval-tmp-bundle"


def run(args, timeout=90):
    p = subprocess.run(TAB + args, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


tabs = tab.load_tabs()
if not tabs:
    skip("workflows", "no tabs open — open some Chrome tabs to cover these")
else:
    # ---- find: a word from a real title resolves to that tab ----
    sample = tabs[0]
    word = next((w for t in tabs for w in t["title"].split() if len(w) > 4), None)
    if word:
        rc, out, _ = run(["find", word.lower(), "--json"])
        try:
            hits = json.loads(out)
        except Exception:
            hits = []
        ok("find · a title word resolves to handle(s)",
           rc == 0 and isinstance(hits, list) and len(hits) >= 1,
           f"'{word}' → {[h.get('id') for h in hits][:5]}")
    else:
        skip("find", "no word-y tab titles")

    # ---- grep: search on-page content; needs a tab with a captured snippet ----
    snippet_tab = next((t for t in tabs if (t.get("snippet") or "").strip()), None)
    if snippet_tab:
        term = next((w for w in snippet_tab["snippet"].split() if w.isalpha() and len(w) > 4), None)
        if term:
            rc, out, _ = run(["grep", term.lower(), "--json"])
            try:
                hits = json.loads(out)
            except Exception:
                hits = []
            ok("grep · finds a term that's in a tab's content",
               rc == 0 and isinstance(hits, list) and len(hits) >= 1, f"'{term}' → {len(hits)} hit(s)")
        else:
            skip("grep", "no word-y snippet token")
    else:
        skip("grep", "no cached on-page content to search (run `tab refresh`)")

    # ---- ask: returns a non-empty cited assembly across open tabs ----
    rc, out, _ = run(["ask", "what is this about", "--tabs", "2"], timeout=120)
    ok("ask · returns a non-empty answer/bundle across tabs",
       rc == 0 and len(out.strip()) > 40, f"rc={rc} {len(out)}c")

    # ---- save → recall round-trip (throwaway bundle, cleaned up) ----
    try:
        rc_s, out_s, _ = run(["save", sample["id"], "--as", BUNDLE_SLUG], timeout=120)
        rc_b, out_b, _ = run(["bundles"])
        listed = BUNDLE_SLUG in out_b
        rc_r, out_r, _ = run(["recall", BUNDLE_SLUG])
        ok("save · captures a tab into a named bundle", rc_s == 0 and listed, f"listed={listed}")
        ok("recall · loads the saved bundle back as content",
           rc_r == 0 and len(out_r.strip()) > 20, f"{len(out_r)}c")
    finally:
        for d in (ROOT / "bundles").glob(f"*{BUNDLE_SLUG}*"):
            shutil.rmtree(d, ignore_errors=True)


print(f"\n{'=' * 52}")
print(f"  WORKFLOW EVAL — {len(PASS)} pass · {len(FAIL)} fail · {len(SKIP)} skip")
print(f"{'=' * 52}")
for n, d in PASS: print(f"  ✅ {n}" + (f"   — {d}" if d else ""))
for n, d in SKIP: print(f"  ⏭  {n}   ({d})")
for n, d in FAIL: print(f"  ❌ {n}   — {d}")
print()
sys.exit(1 if FAIL else 0)
