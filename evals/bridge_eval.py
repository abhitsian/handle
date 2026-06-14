#!/usr/bin/env python3
"""Eval for the extension bridge, the ext→state merge, and the CDP client shape.

  - extbridge: a command enqueued by the CLI is delivered to a poller and its
    result round-trips back; heartbeat/alive; a command with no poller times
    out cleanly (never hangs forever).
  - collect_from_ext: merging extension-pushed tabs preserves notes + handles,
    carries the native tab group, stamps ext_tab_id, and tags source. Runs
    against a TEMP state file — never touches your real state.json.
  - cdp.py: full-page screenshot plumbing exists; no debug port degrades to
    None/[] instead of raising.
  - console / live extension: SKIP unless the extension is actually connected.

    python3 evals/bridge_eval.py
"""
from __future__ import annotations

import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import extbridge  # noqa: E402
import collect  # noqa: E402
import cdp  # noqa: E402
import tab  # noqa: E402

PASS, FAIL, SKIP = [], [], []
def ok(n, c, d=""): (PASS if c else FAIL).append((n, d))
def skip(n, w): SKIP.append((n, w))


# ---------------------------------------------------- 1. extbridge round-trip
box = {}
def _producer():
    box["r"] = extbridge.enqueue("ping", {"x": 1}, timeout=6)

th = threading.Thread(target=_producer)
th.start()
cmd = extbridge.poll(wait=6)             # poller picks up the queued command
ok("bridge · poll receives the enqueued command",
   bool(cmd) and cmd.get("method") == "ping" and cmd.get("params", {}).get("x") == 1, str(cmd))
if cmd:
    extbridge.deliver_result(cmd["id"], {"pong": True})
th.join(timeout=6)
ok("bridge · result round-trips back to the caller", box.get("r") == {"pong": True}, str(box.get("r")))

extbridge.mark_sync({"tab_count": 3, "ext_version": "1.0.0"})
ok("bridge · alive after a sync heartbeat", extbridge.alive())
st = extbridge.status()
ok("bridge · status reports connected + meta",
   st.get("connected") and st.get("tab_count") == 3, str(st))

t0 = time.time()
res = extbridge.enqueue("noop", {}, timeout=1)      # nobody polls → must give up
ok("bridge · unanswered command times out cleanly (no hang)",
   isinstance(res, dict) and "error" in res and (time.time() - t0) < 4, f"{res} in {time.time()-t0:.1f}s")


# ----------------------------------------------- 2. collect_from_ext (temp state)
orig_state = collect.STATE_PATH
tmpdir = Path(tempfile.mkdtemp())
try:
    collect.STATE_PATH = tmpdir / "state.json"
    collect.write_json(collect.STATE_PATH, {
        "tabs": {"https://example.com/a": {"id": "t1", "user_note": "keepme",
                                           "user_cluster": "Mine", "first_seen": "2026-01-01"}},
        "next_id": 2,
    })
    ext_tabs = [
        {"tab_id": 11, "url": "https://example.com/a", "title": "Alpha", "window": 1,
         "index": 0, "group": "Research", "group_color": "blue"},
        {"tab_id": 12, "url": "https://example.com/b", "title": "Beta", "window": 1,
         "index": 1, "pinned": True},
        {"tab_id": 13, "url": "chrome://settings", "title": "skip me", "window": 1, "index": 2},
    ]
    state = collect.collect_from_ext(ext_tabs)
    tabs = state["tabs"]
    a = tabs.get("https://example.com/a", {})
    b = tabs.get("https://example.com/b", {})
    ok("merge · preserves stable handle + note + cluster across refresh",
       a.get("id") == "t1" and a.get("user_note") == "keepme" and a.get("user_cluster") == "Mine", str(a))
    ok("merge · carries the native tab group + color",
       a.get("chrome_group") == "Research" and a.get("chrome_group_color") == "blue", str(a.get("chrome_group")))
    ok("merge · stamps ext_tab_id for the live-read path", a.get("ext_tab_id") == 11)
    ok("merge · new tab gets a fresh handle + pinned flag",
       b.get("id") and b.get("id") != "t1" and b.get("pinned") is True, str(b))
    ok("merge · skips chrome:// urls", "chrome://settings" not in tabs)
    ok("merge · tags source=extension", state.get("source") == "extension")
finally:
    collect.STATE_PATH = orig_state


# --------------------------------------------------------- 3. cdp client shape
import inspect  # noqa: E402
ok("cdp · screenshot supports full-page capture", "full" in inspect.signature(cdp.screenshot).parameters)
ok("cdp · has the reveal-then-scroll helper for full pages", bool(getattr(cdp, "_REVEAL_SCROLL_JS", "")))
ok("cdp · default debug port is 9222", cdp.DEFAULT_PORT == 9222)
# no debug port listening → graceful, not an exception
ok("cdp · no debug port degrades gracefully",
   cdp.version(59999) is None and cdp.targets(59999) == [])


# --------------------------------------------- 4. live extension / console check
if tab._ext_alive():
    tabs_now = tab.load_tabs()
    target = next((t for t in tabs_now if t.get("ext_tab_id") is not None), None)
    if target:
        r = tab._ext_cmd("console", {"tab_id": target["ext_tab_id"], "ms": 600}, timeout=12)
        ok("console · extension returns a logs payload",
           isinstance(r, dict) and "logs" in r and not r.get("error"), str(r)[:80])
    else:
        skip("console · live extension", "extension connected but no ext_tab_id in state")
else:
    skip("console · live extension", "extension not connected (load it + run the board to cover this)")


print(f"\n{'=' * 52}")
print(f"  BRIDGE EVAL — {len(PASS)} pass · {len(FAIL)} fail · {len(SKIP)} skip")
print(f"{'=' * 52}")
for n, d in PASS: print(f"  ✅ {n}" + (f"   — {d}" if d else ""))
for n, d in SKIP: print(f"  ⏭  {n}   ({d})")
for n, d in FAIL: print(f"  ❌ {n}   — {d}")
print()
sys.exit(1 if FAIL else 0)
