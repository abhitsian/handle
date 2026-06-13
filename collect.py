#!/usr/bin/env python3
"""Collect open Google Chrome tabs, content snippets, and last-visit times.

Fully local: AppleScript reads the open tabs, and Chrome's own History
SQLite database tells us when each URL was last visited. No network calls,
no API keys. Run directly to refresh state.json, or import `collect()`.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
STATE_PATH = APP_DIR / "state.json"
APPLESCRIPT = APP_DIR / "tabs.applescript"

FS = "\x1f"  # field separator within a record
RS = "\x1e"  # record separator between tabs
CHROME_EPOCH_OFFSET = 11_644_473_600  # seconds between 1601-01-01 and 1970-01-01

# Tabs we never want on the board.
SKIP_PREFIXES = ("chrome://", "chrome-extension://", "about:", "edge://", "devtools://")
SKIP_SUBSTRINGS = ("localhost:4910", "127.0.0.1:4910")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(path, data) -> None:
    Path(path).write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def read_chrome_tabs() -> list[dict]:
    """Return every open Chrome tab as {window,index,url,title,snippet}."""
    if not APPLESCRIPT.exists():
        raise RuntimeError(f"Missing AppleScript file: {APPLESCRIPT}")
    proc = subprocess.run(
        ["osascript", str(APPLESCRIPT)],
        capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=180,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "osascript failed")

    tabs: list[dict] = []
    for record in proc.stdout.split(RS):
        if not record.strip():
            continue
        parts = record.split(FS)
        if len(parts) < 4:
            continue
        window, index, url, title = parts[0], parts[1], parts[2], parts[3]
        snippet = parts[4] if len(parts) > 4 else ""
        url = url.strip()
        if not url or url.startswith(SKIP_PREFIXES):
            continue
        if any(s in url for s in SKIP_SUBSTRINGS):
            continue
        tabs.append({
            "window": int(window) if window.strip().isdigit() else 0,
            "index": int(index) if index.strip().isdigit() else 0,
            "url": url,
            "title": title.strip() or url,
            "snippet": " ".join(snippet.split())[:1500],
        })
    return tabs


def chrome_history_visits(urls: list[str]) -> dict[str, float]:
    """Map url -> last visit (unix seconds), read from Chrome's History DB.

    Best-effort across every Chrome profile. The live DB is locked while
    Chrome runs, so each file is copied before being opened read-only.
    """
    wanted = list({u for u in urls if u})
    if not wanted:
        return {}
    base = Path.home() / "Library/Application Support/Google/Chrome"
    visits: dict[str, float] = {}
    if not base.exists():
        return visits
    for history_db in base.glob("*/History"):
        tmp = None
        try:
            fd, tmp = tempfile.mkstemp(suffix=".db")
            os.close(fd)
            shutil.copy2(history_db, tmp)
            con = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
            placeholders = ",".join("?" * len(wanted))
            rows = con.execute(
                f"SELECT url, last_visit_time FROM urls WHERE url IN ({placeholders})",
                wanted,
            )
            for url, last_visit_time in rows:
                if not last_visit_time:
                    continue
                unix = last_visit_time / 1_000_000 - CHROME_EPOCH_OFFSET
                if url not in visits or unix > visits[url]:
                    visits[url] = unix
            con.close()
        except Exception:
            continue
        finally:
            if tmp and os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
    return visits


def collect() -> dict:
    """Read current tabs and merge them into state.json. Returns new state.

    Per-tab notes, statuses, and first-seen timestamps are preserved across
    refreshes. Tabs closed in Chrome are dropped — closing a tab is how you
    tell Handle you're done with it.
    """
    state = load_json(STATE_PATH, {})
    if not isinstance(state, dict):
        state = {}
    previous = state.get("tabs", {})
    next_id = state.get("next_id", 1)
    used_ids = {t.get("id") for t in previous.values() if isinstance(t, dict)}

    chrome_tabs = read_chrome_tabs()
    visits = chrome_history_visits([t["url"] for t in chrome_tabs])
    now = _now()

    tabs: dict[str, dict] = {}
    for t in chrome_tabs:
        url = t["url"]
        prev = previous.get(url, {})
        # Stable handle: a tab keeps its `tN` id across refreshes so Claude
        # Code (and the board) can reference it by a short name, not a URL.
        tab_id = prev.get("id")
        if not tab_id:
            tab_id = f"t{next_id}"
            next_id += 1
            used_ids.add(tab_id)
        tabs[url] = {
            "id": tab_id,
            "url": url,
            "title": t["title"],
            "snippet": t["snippet"] or prev.get("snippet", ""),
            "window": t["window"],
            "index": t["index"],
            "first_seen": prev.get("first_seen", now),
            "last_seen": now,
            "last_visit": visits.get(url, prev.get("last_visit")),
            "user_note": prev.get("user_note", ""),
            "user_cluster": prev.get("user_cluster", ""),
            "pinned": prev.get("pinned", False),
        }
        # carry initiative/workstream overrides only when set; absence = auto
        for key in ("user_initiative", "user_workstream"):
            if key in prev:
                tabs[url][key] = prev[key]

    new_state = {
        "tabs": tabs,
        "next_id": next_id,
        "last_collected": now,
        "last_deduced": state.get("last_deduced"),
        "group_by": state.get("group_by", "task"),
    }
    write_json(STATE_PATH, new_state)
    return new_state


def close_tab(url: str) -> bool:
    """Close the first Chrome tab matching `url`. Returns True on success."""
    safe = url.replace("\\", "\\\\").replace('"', '\\"')
    script = (
        'tell application "Google Chrome"\n'
        '  repeat with w in windows\n'
        '    repeat with t in tabs of w\n'
        f'      if (URL of t) is "{safe}" then\n'
        '        close t\n'
        '        return "ok"\n'
        '      end if\n'
        '    end repeat\n'
        '  end repeat\n'
        '  return "notfound"\n'
        'end tell'
    )
    try:
        proc = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=30,
        )
    except Exception:
        return False
    return proc.returncode == 0 and "ok" in proc.stdout


def open_urls(urls: list[str]) -> int:
    """Open each URL as a new tab in Chrome's front window. Returns count opened.

    Used by workspace "resume" — reopens a parked initiative's tab set.
    """
    urls = [u for u in urls if u]
    if not urls:
        return 0
    lines = [
        'tell application "Google Chrome"',
        '  if (count of windows) = 0 then make new window',
        '  activate',
    ]
    for u in urls:
        safe = u.replace("\\", "\\\\").replace('"', '\\"')
        lines.append(
            f'  tell window 1 to make new tab with properties {{URL:"{safe}"}}'
        )
    lines.append('end tell')
    try:
        proc = subprocess.run(
            ["osascript", "-e", "\n".join(lines)],
            capture_output=True, text=True, timeout=60,
        )
    except Exception:
        return 0
    return len(urls) if proc.returncode == 0 else 0


def active_tab_url() -> str:
    """Return the URL of the frontmost Chrome tab — 'the tab I'm looking at'."""
    script = (
        'tell application "Google Chrome"\n'
        '  if (count of windows) = 0 then return ""\n'
        '  return URL of active tab of front window\n'
        'end tell'
    )
    try:
        proc = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=15,
        )
    except Exception:
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def focus_tab(url: str) -> bool:
    """Bring the tab matching `url` to the front and activate Chrome."""
    safe = url.replace("\\", "\\\\").replace('"', '\\"')
    script = (
        'tell application "Google Chrome"\n'
        '  repeat with w in windows\n'
        '    set i to 0\n'
        '    repeat with t in tabs of w\n'
        '      set i to i + 1\n'
        f'      if (URL of t) is "{safe}" then\n'
        '        set active tab index of w to i\n'
        '        set index of w to 1\n'
        '        activate\n'
        '        return "ok"\n'
        '      end if\n'
        '    end repeat\n'
        '  end repeat\n'
        '  return "notfound"\n'
        'end tell'
    )
    try:
        proc = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=30,
        )
    except Exception:
        return False
    return proc.returncode == 0 and "ok" in proc.stdout


# JS that prefers the main article/content element so reads skip nav, header,
# and footer chrome; falls back to the whole body. Single-quoted inside so it
# embeds in the AppleScript double-quoted string without escaping.
# Readability-lite: score every <p> and credit its parent, then take the
# element that holds the most paragraph text — that's the article body, not
# the nav-heavy ancestors. Falls back to article/main/body for pages without
# paragraph structure (docs, sheets). No DOM cloning — returning the chosen
# element's innerText directly keeps it cheap even on huge editor DOMs (a
# cloneNode(true) of a Google Sheet body hangs the per-tab scan).
# Single-quoted throughout so it embeds in the AppleScript double-quoted string.
MAIN_CONTENT_JS = (
    "(function(){"
    "var ps=document.querySelectorAll('p'),s=new Map();"
    "for(var i=0;i<ps.length;i++){var p=ps[i];var t=(p.textContent||'').trim();"
    "if(t.length<25)continue;var pa=p.parentElement;if(!pa)continue;"
    "s.set(pa,(s.get(pa)||0)+Math.min(t.length,1000)/100+1);}"
    "var best=null,bs=0;s.forEach(function(v,k){if(v>bs){bs=v;best=k;}});"
    "var r=best||document.querySelector('article')||document.querySelector('main')||document.body;"
    "return r?r.innerText:'';"
    "})()"
)


def read_tab_content(url: str, limit: int = 8000) -> str:
    """Read the rendered text of an open tab via Chrome JS execution.

    Extracts the page's main content (article/main) rather than the whole
    body, so navigation and footer chrome are dropped. Returns "" if the tab
    isn't found or Chrome's "Allow JavaScript from Apple Events" setting is
    off. For public pages, callers can fall back to fetching the URL instead.
    """
    safe = url.replace("\\", "\\\\").replace('"', '\\"')
    lines = [
        'tell application "Google Chrome"',
        '  repeat with w in windows',
        '    repeat with t in tabs of w',
        f'      if (URL of t) is "{safe}" then',
        f'        return execute t javascript "{MAIN_CONTENT_JS}"',
        '      end if',
        '    end repeat',
        '  end repeat',
        '  return ""',
        'end tell',
    ]
    try:
        proc = subprocess.run(
            ["osascript", "-e", "\n".join(lines)],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=30,
        )
    except Exception:
        return ""
    if proc.returncode != 0:
        return ""
    return " ".join(proc.stdout.split())[:limit]


if __name__ == "__main__":
    result = collect()
    print(f"Collected {len(result['tabs'])} tab(s) into {STATE_PATH}")
