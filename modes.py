"""modes — named Chrome windows that survive being closed.

A mode is a Chrome window you've named (Chrome → Window → Name Window…):
Work, Interview Prep, Read, Adhoc. Chrome forgets the name and the tabs once
the window closes. This module snapshots every named window into modes.json
and reopens a mode as a fresh window with the same name and tabs. Nothing
runs in the background: a mode is saved only when you run save or sync.

  sync()        snapshot all named windows
  save(name)    snapshot one window; can also name the front window
  open(name)    focus the window if it's open, else rebuild it
  list_modes()  what's saved, how many tabs, when, and whether it's open

A snapshot never overwrites a mode with an empty window, and the previous
five snapshots per mode are kept under "history" for undo.

Store: modes.json  {"modes": {name: {"saved", "tabs": [{url, title}], "active", "history"}}}
Self-contained — imported by tab.py (CLI). Uses Chrome's AppleScript
`given name` window property, so it needs no extension.
"""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
MODES_PATH = APP_DIR / "modes.json"
HISTORY_KEEP = 5

# Field/record separators that can't appear in a URL or a tab title.
_FS, _RS, _WS = "\x1f", "\x1e", "\x1d"

_READ_SCRIPT = f'''
set FS to (ASCII character 31)
set RS to (ASCII character 30)
set WS to (ASCII character 29)
set out to ""
tell application "Google Chrome"
  repeat with w in every window
    set nm to given name of w
    if nm is missing value then set nm to ""
    set out to out & (id of w) & FS & nm & FS & (active tab index of w) & FS & (mode of w)
    repeat with t in tabs of w
      set out to out & RS & (URL of t) & FS & (title of t)
    end repeat
    set out to out & WS
  end repeat
end tell
return out
'''


def _osa(script: str, args: list[str] | None = None, timeout: int = 30) -> str:
    r = subprocess.run(["osascript", "-", *(args or [])], input=script,
                       capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or "osascript failed")
    return r.stdout.rstrip("\n")


def chrome_running() -> bool:
    r = subprocess.run(["pgrep", "-x", "Google Chrome"], capture_output=True)
    return r.returncode == 0


def read_windows() -> list[dict]:
    """Every open Chrome window: id, name ('' if unnamed), tabs, active index."""
    raw = _osa(_READ_SCRIPT)
    wins = []
    for block in raw.split(_WS):
        if not block.strip():
            continue
        head, *tab_recs = block.split(_RS)
        wid, name, active, wmode = (head.split(_FS) + ["", "", "1", ""])[:4]
        if wmode == "incognito":
            continue
        tabs = []
        for rec in tab_recs:
            url, _, title = rec.partition(_FS)
            if url:
                tabs.append({"url": url, "title": title})
        wins.append({"id": wid.strip(), "name": name.strip(), "tabs": tabs,
                     "active": int(active or 1)})
    return wins


def load() -> dict:
    try:
        return json.loads(MODES_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {"modes": {}}


def _write(d: dict) -> None:
    tmp = MODES_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=2, ensure_ascii=False))
    tmp.replace(MODES_PATH)


def _find(d: dict, name: str) -> str | None:
    """Resolve a name to a saved mode key: exact, case-insensitive, then prefix."""
    keys = list(d["modes"])
    for test in (lambda k: k == name,
                 lambda k: k.lower() == name.lower(),
                 lambda k: k.lower().startswith(name.lower())):
        hits = [k for k in keys if test(k)]
        if len(hits) == 1:
            return hits[0]
    return None


def _store(d: dict, name: str, win: dict) -> bool:
    """Write one window's tabs into the mode. Returns False if skipped (empty
    window, or nothing changed since the last snapshot)."""
    if not win["tabs"]:
        return False
    rec = d["modes"].get(name)
    urls = [t["url"] for t in win["tabs"]]
    if rec and [t["url"] for t in rec["tabs"]] == urls:
        rec["seen"] = _now()
        return False
    if rec:
        hist = rec.pop("history", [])
        hist.insert(0, rec)
        rec_new = {"history": hist[:HISTORY_KEEP]}
    else:
        rec_new = {"history": []}
    rec_new.update({"saved": _now(), "seen": _now(), "tabs": win["tabs"],
                    "active": win["active"]})
    d["modes"][name] = rec_new
    return True


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sync() -> list[str]:
    """Snapshot every named window. Returns the modes that changed."""
    if not chrome_running():
        return []
    d = load()
    changed = [w["name"] for w in read_windows()
               if w["name"] and _store(d, w["name"], w)]
    _write(d)
    return changed


def save(name: str, front: bool = False) -> dict:
    """Snapshot the window called `name`. With front=True, name the frontmost
    window `name` first (for a window you haven't named in Chrome yet)."""
    if front:
        _osa('on run argv\ntell application "Google Chrome" to set given name '
             'of front window to item 1 of argv\nend run', [name])
    wins = read_windows()
    win = next((w for w in wins if w["name"].lower() == name.lower()), None)
    if not win:
        named = ", ".join(w["name"] for w in wins if w["name"]) or "none"
        raise LookupError(f"No open window named {name!r}. Named windows: {named}. "
                          f"Use --front to name the front window {name!r}.")
    d = load()
    _store(d, win["name"], win)
    _write(d)
    return {"name": win["name"], "tabs": len(win["tabs"])}


def open_mode(name: str) -> dict:
    """Focus the mode's window if open; otherwise rebuild it from the snapshot."""
    d = load()
    key = _find(d, name)
    if not key:
        raise LookupError(f"No saved mode {name!r}. Saved: {', '.join(d['modes']) or 'none'}")
    for w in (read_windows() if chrome_running() else []):
        if w["name"] == key:
            _osa('on run argv\ntell application "Google Chrome"\n'
                 'set index of (first window whose id is (item 1 of argv as integer)) to 1\n'
                 'activate\nend tell\nend run', [w["id"]])
            return {"name": key, "action": "focused", "tabs": len(w["tabs"])}
    rec = d["modes"][key]
    urls = [t["url"] for t in rec["tabs"]]
    _osa('''on run argv
tell application "Google Chrome"
  set w to make new window
  set given name of w to item 1 of argv
  set URL of active tab of w to item 3 of argv
  repeat with i from 4 to count of argv
    tell w to make new tab with properties {URL:item i of argv}
  end repeat
  set active tab index of w to (item 2 of argv as integer)
  activate
end tell
end run''', [key, str(min(rec.get("active", 1), len(urls))), *urls], timeout=60)
    return {"name": key, "action": "opened", "tabs": len(urls)}


def name_window(window_id: str, name: str) -> None:
    _osa('on run argv\ntell application "Google Chrome" to set given name of '
         '(first window whose id is (item 1 of argv as integer)) to item 2 of argv\n'
         'end run', [window_id, name])


def list_modes() -> list[dict]:
    d = load()
    open_names = {w["name"] for w in read_windows()} if chrome_running() else set()
    return [{"name": k, "tabs": len(v["tabs"]), "saved": v["saved"],
             "open": k in open_names, "history": len(v.get("history", []))}
            for k, v in sorted(d["modes"].items())]


def undo(name: str) -> dict:
    """Restore the previous snapshot of a mode (after a bad save)."""
    d = load()
    key = _find(d, name)
    if not key or not d["modes"][key].get("history"):
        raise LookupError(f"No earlier snapshot for {name!r}.")
    hist = d["modes"][key]["history"]
    prev = hist.pop(0)
    prev["history"] = hist
    d["modes"][key] = prev
    _write(d)
    return {"name": key, "tabs": len(prev["tabs"]), "saved": prev["saved"]}


def remove(name: str) -> str:
    d = load()
    key = _find(d, name)
    if not key:
        raise LookupError(f"No saved mode {name!r}.")
    del d["modes"][key]
    _write(d)
    return key
