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
    tell Tab Tasks you're done with it.
    """
    state = load_json(STATE_PATH, {})
    if not isinstance(state, dict):
        state = {}
    previous = state.get("tabs", {})

    chrome_tabs = read_chrome_tabs()
    visits = chrome_history_visits([t["url"] for t in chrome_tabs])
    now = _now()

    tabs: dict[str, dict] = {}
    for t in chrome_tabs:
        url = t["url"]
        prev = previous.get(url, {})
        tabs[url] = {
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

    new_state = {
        "tabs": tabs,
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


def read_tab_content(url: str, limit: int = 8000) -> str:
    """Read the rendered text of an open tab via Chrome JS execution.

    Returns "" if the tab isn't found or Chrome's "Allow JavaScript from
    Apple Events" setting is off. For public pages, callers can fall back
    to fetching the URL instead.
    """
    safe = url.replace("\\", "\\\\").replace('"', '\\"')
    lines = [
        'tell application "Google Chrome"',
        '  repeat with w in windows',
        '    repeat with t in tabs of w',
        f'      if (URL of t) is "{safe}" then',
        '        return execute t javascript '
        '"(document.body ? document.body.innerText : \\"\\")"',
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
