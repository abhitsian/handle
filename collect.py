#!/usr/bin/env python3
"""Collect open Google Chrome tabs, content snippets, and last-visit times.

Fully local: AppleScript reads the open tabs, and Chrome's own History
SQLite database tells us when each URL was last visited. No network calls,
no API keys. Run directly to refresh state.json, or import `collect()`.
"""
from __future__ import annotations

import base64
import hashlib
import html as _html
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
import zipfile
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


def chrome_recent_visits(hours: float = 48.0, limit: int = 40,
                         skip_urls: set[str] | None = None) -> list[dict]:
    """Recently visited URLs from Chrome's History DB — titles + links only.

    The pointer, not the payload: this returns where you've been (url, title,
    last visit), never page content. Use it to find a tab you closed; reopen
    it to read it. Best-effort across every Chrome profile; the live DB is
    locked while Chrome runs, so each file is copied before being opened
    read-only. Fully local — no network, same trust boundary as everything
    else here.
    """
    base = Path.home() / "Library/Application Support/Google/Chrome"
    if not base.exists():
        return []
    skip = {u for u in (skip_urls or set()) if u}
    cutoff_unix = time.time() - hours * 3600
    cutoff_chrome = int((cutoff_unix + CHROME_EPOCH_OFFSET) * 1_000_000)
    best: dict[str, dict] = {}
    for history_db in base.glob("*/History"):
        tmp = None
        try:
            fd, tmp = tempfile.mkstemp(suffix=".db")
            os.close(fd)
            shutil.copy2(history_db, tmp)
            con = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
            rows = con.execute(
                "SELECT url, title, last_visit_time FROM urls "
                "WHERE last_visit_time > ? ORDER BY last_visit_time DESC",
                (cutoff_chrome,),
            )
            for url, title, lvt in rows:
                if not url or not lvt:
                    continue
                if url in skip or url.startswith(SKIP_PREFIXES):
                    continue
                unix = lvt / 1_000_000 - CHROME_EPOCH_OFFSET
                cur = best.get(url)
                if cur is None or unix > cur["visited"]:
                    best[url] = {"url": url, "title": (title or "").strip(), "visited": unix}
            con.close()
        except Exception:
            continue
        finally:
            if tmp and os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
    out = sorted(best.values(), key=lambda r: r["visited"], reverse=True)
    return out[:limit]


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


def collect_from_ext(ext_tabs: list[dict]) -> dict:
    """Merge tabs pushed by the Chrome extension into state.json.

    Same preservation contract as collect() — stable handles, notes, clusters,
    first-seen survive — but the source is the extension's chrome.tabs /
    chrome.tabGroups data, so it needs no AppleScript and carries the user's
    real native tab group (name + color) when present.
    """
    state = load_json(STATE_PATH, {})
    if not isinstance(state, dict):
        state = {}
    previous = state.get("tabs", {})
    next_id = state.get("next_id", 1)
    now = _now()

    urls = [t.get("url", "") for t in ext_tabs if t.get("url")]
    visits = chrome_history_visits(urls)

    tabs: dict[str, dict] = {}
    for t in ext_tabs:
        url = t.get("url", "")
        if not url or url.startswith(SKIP_PREFIXES):
            continue
        prev = previous.get(url, {})
        tab_id = prev.get("id")
        if not tab_id:
            tab_id = f"t{next_id}"
            next_id += 1
        rec = {
            "id": tab_id,
            "url": url,
            "title": t.get("title", "") or url,
            "snippet": t.get("snippet", "") or prev.get("snippet", ""),
            "window": t.get("window", 0),
            "index": t.get("index", 0),
            "first_seen": prev.get("first_seen", now),
            "last_seen": now,
            "last_visit": visits.get(url, prev.get("last_visit")),
            "user_note": prev.get("user_note", ""),
            "user_cluster": prev.get("user_cluster", ""),
            "pinned": bool(t.get("pinned", prev.get("pinned", False))),
            "ext_tab_id": t.get("tab_id"),
        }
        # the user's real Chrome tab group, when the tab is in one
        if t.get("group"):
            rec["chrome_group"] = t["group"]
            rec["chrome_group_color"] = t.get("group_color", "")
        for key in ("user_initiative", "user_workstream"):
            if key in prev:
                rec[key] = prev[key]
        tabs[url] = rec

    new_state = {
        "tabs": tabs,
        "next_id": next_id,
        "last_collected": now,
        "last_deduced": state.get("last_deduced"),
        "group_by": state.get("group_by", "task"),
        "source": "extension",
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


def run_tab_js(url: str, js: str, limit: int = 8000) -> str:
    """Execute arbitrary JS in the open tab matching `url`; return its string.

    The generic primitive behind every extractor. Chrome's `execute javascript`
    evaluates synchronously and returns the expression's value — so the JS must
    return a value directly (a Promise would come back unresolved; use a
    synchronous XMLHttpRequest for in-tab fetches). Returns "" if the tab isn't
    found or Chrome's "Allow JavaScript from Apple Events" is off. Raw text is
    returned (newlines preserved) so callers can format as they need.
    """
    safe_url = url.replace("\\", "\\\\").replace('"', '\\"')
    # Run the JS via eval(atob('<base64>')) so the source crosses the AppleScript
    # string boundary untouched — no quote/backslash/newline escaping to get
    # wrong. The wrapper itself is pure [A-Za-z0-9+/=()'.], safe to embed.
    b64 = base64.b64encode(js.encode("utf-8")).decode("ascii")
    wrapper = "eval(atob('" + b64 + "'))"
    lines = [
        'tell application "Google Chrome"',
        '  repeat with w in windows',
        '    repeat with t in tabs of w',
        f'      if (URL of t) is "{safe_url}" then',
        f'        return execute t javascript "{wrapper}"',
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
    return proc.stdout.strip()[:limit]


# DOM → Markdown for the main content element. Same paragraph-scoring root
# selection as MAIN_CONTENT_JS, then a compact recursive serializer that keeps
# the structure flat text throws away: headings, lists, links, tables, code,
# blockquotes, images. Runs via base64 (run_tab_js), so regex/newlines are safe.
MARKDOWN_JS = r"""(function(){
  var ps=document.querySelectorAll('p'),sc=new Map();
  for(var i=0;i<ps.length;i++){var p=ps[i];var t=(p.textContent||'').trim();
    if(t.length<25)continue;var pa=p.parentElement;if(!pa)continue;
    sc.set(pa,(sc.get(pa)||0)+Math.min(t.length,1000)/100+1);}
  var best=null,bs=0;sc.forEach(function(v,k){if(v>bs){bs=v;best=k;}});
  var root=best||document.querySelector('article')||document.querySelector('main')||document.body;
  if(!root)return '';
  var SKIP={SCRIPT:1,STYLE:1,NAV:1,HEADER:1,FOOTER:1,ASIDE:1,NOSCRIPT:1,FORM:1,BUTTON:1,SVG:1,IFRAME:1};
  function inline(node){var out='';node.childNodes.forEach(function(c){
    if(c.nodeType===3){out+=c.textContent.replace(/\s+/g,' ');return;}
    if(c.nodeType!==1)return;var tag=c.tagName.toLowerCase();
    if(SKIP[c.tagName])return;
    if(tag==='a'){var h=c.getAttribute('href')||'';var tx=inline(c).trim();out+=h?('['+tx+']('+h+')'):tx;}
    else if(tag==='strong'||tag==='b'){out+='**'+inline(c).trim()+'**';}
    else if(tag==='em'||tag==='i'){out+='*'+inline(c).trim()+'*';}
    else if(tag==='code'){out+='`'+(c.textContent||'').replace(/\s+/g,' ').trim()+'`';}
    else if(tag==='br'){out+='\n';}
    else{out+=inline(c);}});return out;}
  function tableMd(tb){var rows=tb.querySelectorAll('tr'),o=[];
    rows.forEach(function(r,ri){var cs=r.querySelectorAll('th,td'),line=[];
      cs.forEach(function(cell){line.push(inline(cell).trim().replace(/\|/g,'\\|').replace(/\n/g,' '));});
      o.push('| '+line.join(' | ')+' |');
      if(ri===0)o.push('| '+line.map(function(){return '---';}).join(' | ')+' |');});
    return o.join('\n');}
  function block(node,depth){var md='';node.childNodes.forEach(function(c){
    if(c.nodeType===3){var t=c.textContent.replace(/\s+/g,' ');if(t.trim())md+=t;return;}
    if(c.nodeType!==1)return;var tag=c.tagName;if(SKIP[tag])return;var lt=tag.toLowerCase();
    if(/^H[1-6]$/.test(tag)){var tx=inline(c).trim();if(tx)md+='\n\n'+Array(+tag[1]+1).join('#')+' '+tx+'\n';}
    else if(lt==='p'){var t=inline(c).trim();if(t)md+='\n\n'+t+'\n';}
    else if(lt==='ul'||lt==='ol'){md+='\n';var idx=1;
      c.childNodes.forEach(function(li){if(li.nodeType===1&&li.tagName.toLowerCase()==='li'){
        var mark=lt==='ol'?(idx++)+'. ':'- ';var tx=inline(li).trim();
        if(tx)md+='\n'+Array(depth+1).join('  ')+mark+tx;}});md+='\n';}
    else if(lt==='blockquote'){var tx=inline(c).trim();if(tx)md+='\n\n> '+tx.replace(/\n/g,'\n> ')+'\n';}
    else if(lt==='pre'){md+='\n\n```\n'+(c.textContent||'').replace(/\n+$/,'')+'\n```\n';}
    else if(lt==='hr'){md+='\n\n---\n';}
    else if(lt==='table'){md+='\n\n'+tableMd(c)+'\n';}
    else if(lt==='img'){var src=c.getAttribute('src')||'';var alt=c.getAttribute('alt')||'';
      if(src&&src.indexOf('data:')!==0)md+='\n\n!['+alt+']('+src+')\n';}
    else{md+=block(c,depth+1);}});return md;}
  return block(root,0).replace(/\n{3,}/g,'\n\n').replace(/[ \t]+\n/g,'\n').trim();
})()"""


def read_tab_markdown(url: str, limit: int = 16000) -> str:
    """Convert an open HTML tab's main content to Markdown — headings, lists,
    links, tables preserved. Best for text-heavy pages (articles, docs, wikis)."""
    return run_tab_js(url, MARKDOWN_JS, limit)


def read_tab_content(url: str, limit: int = 8000) -> str:
    """Read the rendered main-content text of an open HTML tab.

    Extracts the page's main content (article/main) rather than the whole
    body, so navigation and footer chrome are dropped, and flattens whitespace.
    Returns "" if the tab isn't found, Chrome's "Allow JavaScript from Apple
    Events" is off, or the page has no DOM text (a canvas app, a PDF viewer).
    """
    raw = run_tab_js(url, MAIN_CONTENT_JS, limit * 3)
    return " ".join(raw.split())[:limit]


SHOTS_DIR = APP_DIR / "shots"


def _front_window_region() -> tuple[int, int, int, int] | None:
    """Screen rectangle (x, y, w, h) of Chrome's front window, global coords."""
    try:
        out = subprocess.run(
            ["osascript", "-e",
             'tell application "Google Chrome" to get bounds of front window'],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:
        return None
    if out.returncode != 0:
        return None
    try:
        b = [int(x) for x in out.stdout.strip().split(", ")]
        return (b[0], b[1], b[2] - b[0], b[3] - b[1])
    except Exception:
        return None


def _capture_region(region, path: Path) -> tuple[bool, str]:
    """screencapture the rect to `path`. Returns (ok, error). A failure here is
    almost always the macOS Screen Recording permission missing for this
    terminal ('could not create image')."""
    x, y, w, h = region
    try:
        r = subprocess.run(
            ["screencapture", "-x", "-o", "-R", f"{x},{y},{w},{h}", str(path)],
            capture_output=True, text=True, timeout=20,
        )
    except Exception as exc:
        return False, str(exc)
    err = (r.stderr or r.stdout or "").strip()
    if r.returncode != 0 or not path.exists() or path.stat().st_size < 1000:
        if "could not create image" in err.lower() or not path.exists():
            err = ("screencapture couldn't capture — grant Screen Recording to "
                   "your terminal in System Settings → Privacy & Security → "
                   "Screen Recording, then retry.")
        return False, err
    return True, ""


def screenshot_tab(url: str, full: bool = False, max_shots: int = 6):
    """Bring the tab to the front and capture its window to PNG(s).

    The catch-all reader: works for anything rendered — Figma, PDFs, Office
    viewers — because it reads pixels, not the DOM. `full` scrolls the page and
    captures successive viewports. Returns (list_of_paths, error_message).
    Needs macOS Screen Recording permission for the terminal running this.
    """
    if not focus_tab(url):
        return [], "tab not found in Chrome"
    SHOTS_DIR.mkdir(exist_ok=True)
    time.sleep(0.6)  # let the window come forward and settle
    key = hashlib.md5(url.encode("utf-8")).hexdigest()[:8]
    paths: list[str] = []

    if not full:
        region = _front_window_region()
        if not region:
            return [], "could not read Chrome window bounds"
        p = SHOTS_DIR / f"{key}-0.png"
        ok, err = _capture_region(region, p)
        return ([str(p)], "") if ok else ([], err)

    run_tab_js(url, "window.scrollTo(0,0)")
    time.sleep(0.3)
    last = -1
    for idx in range(max_shots):
        region = _front_window_region()
        if not region:
            break
        p = SHOTS_DIR / f"{key}-{idx}.png"
        ok, err = _capture_region(region, p)
        if not ok:
            return (paths, "") if paths else ([], err)
        paths.append(str(p))
        pos = run_tab_js(url, "(function(){window.scrollBy(0,Math.round(innerHeight*0.92));"
                              "return ''+Math.round(window.scrollY);})()")
        try:
            pos = int(pos)
        except (TypeError, ValueError):
            break
        if pos <= last:  # reached the bottom — no further progress
            break
        last = pos
        time.sleep(0.45)
    return paths, ""


def read_office_docx(url: str, limit: int = 40000) -> str:
    """Extract the full text of a SharePoint/Office Word doc, no clicking.

    A .docx is a zip of XML. We download the file itself via a synchronous
    in-tab XHR — same-origin, so it rides the user's SharePoint login — pull
    it back as base64 (byte-masked, since sync XHR can't do arraybuffer),
    unzip word/document.xml, and strip the tags. Beats screenshotting the
    cross-origin Office viewer (which only yields one page). Word only;
    returns "" if it isn't a SharePoint Word URL or the download fails.
    """
    low = url.lower()
    is_word = ":w:" in low or low.split("?")[0].endswith(".docx")
    if not is_word:
        return ""
    # The viewer URL looks like https://host/:w:/r/sites/<site>/_layouts/15/Doc.aspx?sourcedoc=%7BGUID%7D
    host = re.match(r"(https://[^/]+)", url)
    site = re.search(r"/sites/([^/?]+)", url)
    guid = re.search(r"sourcedoc=%7B([0-9A-Fa-f-]+)%7D", url) or re.search(r"sourcedoc=\{?([0-9A-Fa-f-]+)", url)
    if not (host and site and guid):
        return ""
    dl = (f"{host.group(1)}/sites/{site.group(1)}"
          f"/_layouts/15/download.aspx?UniqueId=%7B{guid.group(1)}%7D")
    js = ("(function(){try{var x=new XMLHttpRequest();x.open('GET','" + dl + "',false);"
          "x.overrideMimeType('text/plain; charset=x-user-defined');x.send();"
          "if(x.status!==200)return '';var t=x.responseText,s='';"
          "for(var i=0;i<t.length;i++)s+=String.fromCharCode(t.charCodeAt(i)&255);"
          "return btoa(s);}catch(e){return '';}})()")
    data = run_tab_js(url, js, limit=30_000_000)
    if not data or data.startswith(("HTTP", "ERR")):
        return ""
    try:
        z = zipfile.ZipFile(io.BytesIO(base64.b64decode(data)))
        xml = z.read("word/document.xml").decode("utf-8", "replace")
    except Exception:
        return ""
    return _docx_xml_to_md(xml)[:limit]


def _docx_xml_to_md(xml: str) -> str:
    """Word document.xml → Markdown: heading styles become #, list items become
    -, paragraphs are kept; text comes from the <w:t> runs (the proper way)."""
    out = []
    for p in re.findall(r"<w:p\b.*?</w:p>", xml, re.S):
        runs = re.findall(r"<w:t[^>]*>(.*?)</w:t>", p, re.S)
        txt = _html.unescape("".join(runs)).strip()
        if not txt:
            out.append("")
            continue
        style = re.search(r'<w:pStyle\s+w:val="([^"]+)"', p)
        sval = style.group(1) if style else ""
        h = re.match(r"Heading\s?([1-6])", sval)
        if h:
            out.append("\n" + "#" * int(h.group(1)) + " " + txt)
        elif sval == "Title":
            out.append("\n# " + txt)
        elif "<w:numPr" in p:
            out.append("- " + txt)
        else:
            out.append(txt)
    md = re.sub(r"\n{3,}", "\n\n", "\n".join(out))
    return md.strip()


def read_clipboard() -> str:
    """The macOS clipboard (pbpaste) — the human-assisted read rung: the user
    copies (no focus race, any app), the agent grabs it."""
    try:
        return subprocess.run(["pbpaste"], capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return ""


if __name__ == "__main__":
    result = collect()
    print(f"Collected {len(result['tabs'])} tab(s) into {STATE_PATH}")
