#!/usr/bin/env python3
"""Handle — give every open Chrome tab a referenceable handle, on a board.

Pulls your current Chrome tabs when you ask, assigns each a stable handle
(t1, t2…) you can reference from Claude Code, shows them grouped into the
tasks they belong to (deduced by Claude Code), lets you annotate, pin, move,
group, and close tabs, and lets you queue actions (summarize, push to Notion,
ingest, synthesize a cluster) that Claude Code then executes. Group the board
by task or by browser window. The `tab` CLI + /tabs skill drive it from a
session; the headline is `read`, which pulls a tab's live content past logins.

Pure Python standard library — no dependencies, no install step.
"""
from __future__ import annotations

import html
import json
import time
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

import collect as collector
import workspaces
import extbridge
import recorder
import stash
import capture
import webindex

APP_DIR = Path(__file__).resolve().parent
STATE_PATH = APP_DIR / "state.json"
DEDUCTIONS_PATH = APP_DIR / "deductions.json"
ACTIONS_PATH = APP_DIR / "actions.json"
STALE_DAYS = 3
PORT = 4910

# Group-by modes that group tabs by a per-tab field (window is special-cased).
GROUP_MODES = ("task", "initiative", "workstream", "window")
GROUP_FIELD = {"task": "cluster", "initiative": "initiative",
               "workstream": "workstream"}
# Which deductions.json map holds the summary/stale for each grouping mode.
SUMMARY_KEY = {"task": "clusters", "initiative": "initiatives",
               "workstream": "workstreams"}
EMPTY_LABEL = {"task": "Unsorted", "initiative": "No initiative",
               "workstream": "No workstream", "window": "Unsorted"}

ACTION_LABELS = {
    "summarize": "Summarize",
    "notion_task": "→ Notion task",
    "ingest": "Ingest",
    "synthesize": "Synthesize",
}


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def esc(value) -> str:
    return html.escape("" if value is None else str(value))


def _pretty(slug: str) -> str:
    """Human label for a slug, e.g. org-chart-conversations -> Org Chart Conversations."""
    return slug.replace("-", " ").replace("_", " ").title() if slug else slug


def _domain(url: str) -> str:
    try:
        net = urlparse(url).netloc.lower()
        return net[4:] if net.startswith("www.") else net
    except Exception:
        return ""


def _parse_iso(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).timestamp()
    except Exception:
        return None


def _humanize(unix) -> str:
    if not unix:
        return "—"
    delta = max(0.0, time.time() - unix)
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    return f"{int(delta // 86400)}d ago"


def _stale_section(non_pinned: list) -> bool:
    """A section is stale when at least half its non-pinned tabs are stale."""
    if not non_pinned:
        return False
    stale = sum(1 for t in non_pinned if t["stale"])
    return stale * 2 >= len(non_pinned)


def build_view() -> dict:
    """Assemble everything the page needs from state + deductions + actions."""
    state = collector.load_json(STATE_PATH, {"tabs": {}, "last_collected": None})
    ded = collector.load_json(DEDUCTIONS_PATH, {"tabs": {}, "clusters": {}})
    queue = collector.load_json(ACTIONS_PATH, {"queue": []}).get("queue", [])
    ded_tabs = ded.get("tabs", {})
    ded_clusters = ded.get("clusters", {})
    group_by = state.get("group_by", "task")
    if group_by not in GROUP_MODES:
        group_by = "task"
    # summary/stale source for the active grouping (task/initiative/workstream)
    summaries = ded.get(SUMMARY_KEY.get(group_by, "clusters"), {})
    now = time.time()

    tab_actions: dict = {}
    cluster_actions: dict = {}
    pending_actions = 0
    for a in queue:
        if a.get("status") == "pending":
            pending_actions += 1
        if a.get("scope") == "tab":
            tab_actions.setdefault(a.get("url"), []).append(a)
        elif a.get("scope") == "cluster":
            cluster_actions.setdefault(a.get("cluster"), []).append(a)

    items = []
    for url, tab in state.get("tabs", {}).items():
        guess = ded_tabs.get(url, {})
        pinned = bool(tab.get("pinned"))
        # user overrides: None (key absent) = use deduced; "" = explicit none.
        ui = tab.get("user_initiative")
        uw = tab.get("user_workstream")
        initiative = ui if ui is not None else guess.get("initiative", "")
        workstream = uw if uw is not None else guess.get("workstream", "")
        last_visit = tab.get("last_visit")
        if last_visit:
            age = (now - last_visit) / 86400
        else:
            seen = _parse_iso(tab.get("first_seen"))
            age = (now - seen) / 86400 if seen else 0.0
        items.append({
            "id": tab.get("id", ""),
            "url": url,
            "title": tab.get("title") or url,
            "domain": _domain(url),
            "user_note": tab.get("user_note", ""),
            "deduction": guess.get("deduction", ""),
            "cluster": tab.get("user_cluster") or guess.get("cluster", ""),
            "initiative": initiative,
            "workstream": workstream,
            "init_override": ui is not None,
            "ws_override": uw is not None,
            "scope": guess.get("scope", ""),
            "window": tab.get("window", 0),
            "index": tab.get("index", 0),
            "pinned": pinned,
            "age_days": round(age, 1),
            "stale": (age >= STALE_DAYS) and not pinned,
            "last_visit_h": _humanize(last_visit),
            "actions": tab_actions.get(url, []),
        })

    all_clusters = sorted(
        {it["cluster"] for it in items if it["cluster"]} | set(ded_clusters.keys())
    )

    sections: list = []
    unsorted: list = []
    active_inits = workspaces.active_initiatives()

    if group_by == "window":
        windows: dict = {}
        for it in items:
            windows.setdefault(it["window"], []).append(it)
        for win in sorted(windows):
            tabs = windows[win]
            tabs.sort(key=lambda x: (not x["pinned"], x["index"]))
            non_pinned = [t for t in tabs if not t["pinned"]]
            sections.append({
                "name": f"Window {win}", "key": str(win), "summary": "",
                "stale": _stale_section(non_pinned), "count": len(tabs),
                "tabs": tabs, "actions": [], "parkable": False,
            })
    else:
        field = GROUP_FIELD[group_by]
        groups: dict = {}
        for it in items:
            groups.setdefault(it[field], []).append(it)
        for key, tabs in groups.items():
            tabs.sort(key=lambda x: (not x["pinned"], -x["age_days"]))
            if not key:
                unsorted = tabs
                continue
            non_pinned = [t for t in tabs if not t["pinned"]]
            # in task mode the key IS the display name; for initiative/workstream
            # the key is a slug, so prettify for display.
            name = key if group_by == "task" else _pretty(key)
            sections.append({
                "name": name, "key": key,
                "summary": summaries.get(key, {}).get("summary", ""),
                "stale": summaries.get(key, {}).get(
                    "stale", _stale_section(non_pinned)),
                "count": len(tabs), "tabs": tabs,
                "actions": cluster_actions.get(key, []) if group_by == "task" else [],
                "parkable": group_by == "initiative" and key in active_inits,
            })
        sections.sort(key=lambda s: (not s["stale"], -s["count"]))

    return {
        "group_by": group_by,
        "last_collected": state.get("last_collected"),
        "last_collected_h": _humanize(_parse_iso(state.get("last_collected"))),
        "sections": sections,
        "unsorted": unsorted,
        "unsorted_label": EMPTY_LABEL.get(group_by, "Unsorted"),
        "all_clusters": all_clusters,
        "active_inits": active_inits,
        "all_workstreams": workspaces.all_workstreams(),
        "total": len(items),
        "needs_deduction": len([it for it in items if not it["cluster"]]),
        "pending_actions": pending_actions,
        "parked": workspaces.list_parked(),
    }


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------
HEAD = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Handle</title>
<style>
  :root {
    --bg:#f6f5f2; --card:#fff; --line:#e7e4dd; --ink:#2c2a26;
    --mut:#8a857c; --accent:#2f6f6a; --amber:#b5761f; --red:#b5402a;
  }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--ink);
         font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }
  .wrap { max-width:880px; margin:0 auto; padding:34px 22px 90px; }
  header { display:flex; align-items:flex-start; justify-content:space-between; gap:16px; }
  h1 { font-size:22px; margin:0; letter-spacing:-0.02em; }
  .sub { color:var(--mut); font-size:13px; margin:5px 0 0; }
  .head-right { display:flex; align-items:center; gap:10px; }
  .toggle { display:flex; border:1px solid var(--line); border-radius:8px; overflow:hidden; }
  .toggle button { border:none; background:var(--card); color:var(--mut);
                   padding:8px 13px; font:600 12px/1 inherit; cursor:pointer; }
  .toggle button + button { border-left:1px solid var(--line); }
  .toggle button.on { background:var(--accent); color:#fff; }
  .btn { border:1px solid var(--accent); background:var(--accent); color:#fff;
         padding:9px 14px; border-radius:8px; font:600 12px/1 inherit; cursor:pointer;
         text-decoration:none; display:inline-flex; align-items:center; }
  .btn:active { transform:translateY(1px); }
  .btn.ghost { background:var(--card); color:var(--accent); }
  .cap { margin:20px 0 4px; padding:16px 18px; background:var(--card);
         border:1px solid var(--line); border-radius:10px; }
  .cap-head { display:flex; align-items:center; gap:8px; font-size:13px; }
  .cap-dot { width:7px; height:7px; border-radius:50%; background:var(--mut); }
  .cap-dot.on { background:var(--accent); }
  .cap-sub { color:var(--mut); font-size:12px; }
  .cap-nums { display:flex; align-items:baseline; gap:6px; flex-wrap:wrap; margin:12px 0 4px; }
  .cap-big { font-size:24px; font-weight:600; letter-spacing:-0.02em; }
  .cap-lbl { color:var(--mut); font-size:12px; margin-right:4px; }
  .cap-sep { color:var(--line); margin:0 6px; }
  .cap-list { list-style:none; margin:12px 0 0; padding:12px 0 0; border-top:1px solid var(--line); }
  .cap-list li { display:flex; justify-content:space-between; gap:12px; padding:3px 0;
                 font-size:13px; }
  .cap-list a { color:var(--ink); text-decoration:none; overflow:hidden;
                text-overflow:ellipsis; white-space:nowrap; }
  .cap-list a:hover { color:var(--accent); }
  .cap-k { color:var(--mut); font-size:11px; font-variant-numeric:tabular-nums; }
  .cap-none { color:var(--mut); font-size:13px; margin:10px 0 0; }
  .cap-foot { color:var(--mut); font-size:12px; margin:12px 0 0; }
  .cap code { background:var(--bg); border:1px solid var(--line); padding:1px 6px;
              border-radius:5px; font-size:12px; }
  .banner { border-radius:10px; padding:12px 14px; font-size:13px; margin:18px 0 4px; }
  .banner.info { background:#eef4f3; border:1px solid #cfe1de; }
  .banner.act  { background:#f3eee2; border:1px solid #e6dcc4; color:#6b5d36; }
  .banner.err  { background:#fbeeea; border:1px solid #e6c4b8; color:#8a3b22; }
  .banner code { background:#fff; border:1px solid var(--line); padding:1px 6px;
                 border-radius:5px; font-size:12px; }
  h2.section { font-size:12px; text-transform:uppercase; letter-spacing:0.07em;
               color:var(--mut); margin:30px 0 8px; }
  .cluster { background:var(--card); border:1px solid var(--line);
             border-radius:12px; margin:12px 0; overflow:hidden; }
  .cluster.stale { border-color:#e7d3b2; }
  .cl-head { padding:14px 16px 12px; }
  .cl-head.stale { border-left:3px solid var(--amber); }
  .cl-title { font-size:15px; font-weight:600; margin:0;
              display:flex; align-items:center; gap:8px; flex-wrap:wrap; }
  .cl-sum { color:var(--mut); font-size:13px; margin:5px 0 0; }
  .cl-actions { margin-top:10px; display:flex; gap:7px; flex-wrap:wrap; align-items:center; }
  .act-btn { font:600 11px/1 inherit; padding:6px 10px; border:1px solid var(--line);
             background:var(--card); color:var(--accent); border-radius:7px; cursor:pointer; }
  .act-btn:hover { border-color:var(--accent); }
  .pill { font-size:11px; font-weight:600; padding:2px 8px; border-radius:999px;
          background:#eef0ee; color:var(--mut); }
  .pill.stale { background:#f6e6cf; color:var(--amber); }
  .tab { border-top:1px solid var(--line); padding:13px 16px; display:flex; gap:12px; }
  .tab.pinned { background:#fbf7ec; }
  .fav { width:18px; height:18px; border-radius:4px; margin-top:2px;
         flex:none; background:#edeae3; }
  .tab-main { flex:1; min-width:0; }
  .tab-titlerow { display:flex; align-items:baseline; gap:7px; }
  .tab-title { font-size:14px; font-weight:500; color:var(--ink);
               text-decoration:none; word-break:break-word; }
  .tab-title:hover { text-decoration:underline; }
  /* Stable handle — the same tN you reference from Claude Code (`read t49`). */
  .handle { flex:0 0 auto; font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
            font-size:11px; font-weight:600; color:var(--mut);
            background:var(--card,#f1f1ef); border:1px solid var(--line,#e2e2dd);
            border-radius:5px; padding:1px 5px; cursor:default; }
  .meta { color:var(--mut); font-size:12px; margin-top:3px; }
  .idle { color:var(--amber); font-weight:600; }
  .chip { font-size:11px; font-weight:600; background:#f0ead9; color:var(--amber);
          padding:1px 7px; border-radius:999px; }
  .ded { font-size:13px; color:#5f5b51; font-style:italic; margin-top:5px; }
  .ai-sum { font-size:13px; color:#33463f; background:#eef4f3; border:1px solid #cfe1de;
            border-radius:7px; padding:7px 9px; margin-top:7px; }
  .note { width:100%; margin-top:8px; border:1px solid var(--line); border-radius:7px;
          padding:7px 9px; font:13px/1.45 inherit; resize:vertical;
          min-height:34px; background:#fcfbf9; color:var(--ink); }
  .note:focus { outline:none; border-color:var(--accent); background:#fff; }
  .tab-actions { margin-top:8px; display:flex; gap:8px; flex-wrap:wrap; align-items:center; }
  .move, .act { font:12px/1 inherit; padding:5px 7px; border:1px solid var(--line);
                border-radius:6px; background:#fcfbf9; color:var(--ink); max-width:230px; }
  .achips { margin-top:7px; display:flex; gap:6px; flex-wrap:wrap; }
  .achip { font:600 11px/1.4 inherit; padding:2px 8px; border-radius:999px;
           text-decoration:none; border:1px solid transparent; }
  .achip.pend { background:#f3eee2; color:#8a7a52; border-color:#e6dcc4; cursor:pointer; }
  .achip.done { background:#e7f0ec; color:var(--accent); }
  a.achip.done:hover { text-decoration:underline; }
  .achip.err { background:#fbeeea; color:var(--red); }
  .tab-side { flex:none; display:flex; flex-direction:column; gap:6px; }
  .iconbtn { width:30px; height:28px; border:1px solid var(--line); cursor:pointer;
             background:var(--card); border-radius:7px; font-size:13px; line-height:1; }
  .pin { filter:grayscale(1); opacity:.5; }
  .pin.on { filter:none; opacity:1; background:#f6e6cf; border-color:#e7d3b2; }
  .x-close:hover { color:var(--red); border-color:#e0b3a6; }
  .empty { text-align:center; color:var(--mut); padding:54px 18px;
           background:var(--card); border:1px dashed var(--line);
           border-radius:12px; margin-top:18px; }
</style>
</head>
<body>
"""

SCRIPT = """
<script>
function post(url, body, after) {
  return fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body)
  }).then(after || function () { location.reload(); });
}
document.querySelectorAll('.note').forEach(function (el) {
  var saved = el.value;
  el.addEventListener('blur', function () {
    if (el.value === saved) return;
    saved = el.value;
    post('/api/note', { url: el.dataset.url, note: el.value }, function () {});
  });
});
document.querySelectorAll('.pin').forEach(function (el) {
  el.addEventListener('click', function () { post('/api/pin', { url: el.dataset.url }); });
});
document.querySelectorAll('.x-close').forEach(function (el) {
  el.addEventListener('click', function () {
    el.disabled = true;
    post('/api/close', { url: el.dataset.url });
  });
});
document.querySelectorAll('.move').forEach(function (sel) {
  var prev = sel.value;
  sel.addEventListener('change', function () {
    var v = sel.value;
    if (v === '__new__') {
      var name = (prompt('New group name:') || '').trim();
      if (!name) { sel.value = prev; return; }
      post('/api/move', { url: sel.dataset.url, cluster: name });
    } else {
      post('/api/move', { url: sel.dataset.url, cluster: v });
    }
  });
});
document.querySelectorAll('.retag').forEach(function (sel) {
  sel.addEventListener('change', function () {
    post('/api/retag', {
      url: sel.dataset.url, field: sel.dataset.field, value: sel.value
    });
  });
});
document.querySelectorAll('.act').forEach(function (sel) {
  sel.addEventListener('change', function () {
    if (!sel.value) return;
    post('/api/action', { action: sel.value, scope: 'tab', url: sel.dataset.url });
  });
});
document.querySelectorAll('.act-btn').forEach(function (el) {
  el.addEventListener('click', function () {
    post('/api/action', {
      action: el.dataset.action, scope: 'cluster', cluster: el.dataset.cluster
    });
  });
});
document.querySelectorAll('.achip.pend').forEach(function (el) {
  el.addEventListener('click', function () {
    post('/api/action/cancel', { id: el.dataset.cancel });
  });
});
document.querySelectorAll('.toggle button').forEach(function (el) {
  el.addEventListener('click', function () {
    if (el.classList.contains('on')) return;
    post('/api/groupby', { mode: el.dataset.mode });
  });
});
document.querySelectorAll('.act-btn.park').forEach(function (el) {
  el.addEventListener('click', function () {
    var n = el.dataset.initiative;
    if (!confirm('Park "' + n + '"? Its tabs get snapshotted to the initiative '
                 + 'folder and closed in Chrome. Resume reopens them.')) return;
    el.disabled = true;
    post('/api/park', { initiative: n });
  });
});
document.querySelectorAll('.act-btn.resume').forEach(function (el) {
  el.addEventListener('click', function () {
    el.disabled = true;
    post('/api/resume', { initiative: el.dataset.initiative });
  });
});
</script>
</body>
</html>
"""


def _move_select(t: dict, all_clusters: list) -> str:
    cur = t["cluster"]
    opts = ['<option value=""' + ("" if cur else " selected") +
            ">&mdash; no group &mdash;</option>"]
    for name in all_clusters:
        sel = " selected" if name == cur else ""
        opts.append(f'<option value="{esc(name)}"{sel}>{esc(name)}</option>')
    opts.append('<option value="__new__">+ New group&hellip;</option>')
    return (f'<select class="move" data-url="{esc(t["url"])}">'
            + "".join(opts) + "</select>")


def _retag_select(t: dict, field: str, current: str, overridden: bool,
                  options: list) -> str:
    """A dropdown to correct a tab's initiative/workstream tag.

    "auto" clears the override (back to the deduced tag); "none" pins it to no
    tag. A real option pins it to that slug. The current value is preselected.
    """
    auto_sel = "" if overridden else " selected"
    none_sel = " selected" if (overridden and not current) else ""
    opts = [
        f'<option value="__auto__"{auto_sel}>&#9881; auto</option>',
        f'<option value=""{none_sel}>&mdash; none &mdash;</option>',
    ]
    for slug in options:
        sel = " selected" if (current == slug) else ""
        opts.append(f'<option value="{esc(slug)}"{sel}>{esc(_pretty(slug))}</option>')
    return (f'<select class="retag" data-url="{esc(t["url"])}" '
            f'data-field="{field}" title="Re-tag {field}">'
            + "".join(opts) + "</select>")


def _action_select(t: dict) -> str:
    return (
        f'<select class="act" data-url="{esc(t["url"])}">'
        '<option value="">&#9889; Run action&hellip;</option>'
        '<option value="summarize">Summarize</option>'
        '<option value="notion_task">&rarr; Notion task</option>'
        '<option value="ingest">Ingest</option>'
        "</select>"
    )


def _action_chip(a: dict) -> str:
    label = ACTION_LABELS.get(a.get("action"), a.get("action", ""))
    status = a.get("status")
    if status == "pending":
        return (f'<button class="achip pend" data-cancel="{esc(a.get("id"))}" '
                f'title="Queued &mdash; click to remove">&#8987; {esc(label)} &times;</button>')
    if status == "error":
        return (f'<span class="achip err" title="{esc(a.get("result", ""))[:300]}">'
                f'&#9888; {esc(label)}</span>')
    result = a.get("result", "")
    if result.startswith(("http://", "https://", "/")):
        return (f'<a class="achip done" href="{esc(result)}" target="_blank" '
                f'rel="noopener">&#10003; {esc(label)}</a>')
    return (f'<span class="achip done" title="{esc(result)[:300]}">'
            f'&#10003; {esc(label)}</span>')


def render_tab(t: dict, all_clusters: list, group_by: str = "task",
               active_inits: list | None = None,
               all_ws: list | None = None) -> str:
    handle = (f'<span class="handle" title="Reference this tab in Claude Code: '
              f'read {t["id"]}">{esc(t["id"])}</span> ' if t.get("id") else "")
    meta = (f'{esc(t["domain"])} &middot; window {t["window"]} &middot; '
            f'last opened {esc(t["last_visit_h"])}')
    if t["pinned"]:
        meta += ' &middot; <span class="chip">&#128204; pinned</span>'
    elif t["stale"]:
        meta += f' &middot; <span class="idle">idle {t["age_days"]}d</span>'

    ded = ""
    if t["deduction"] and not t["user_note"]:
        ded = f'<div class="ded">{esc(t["deduction"])}</div>'

    summary_block, chips = "", []
    for a in t.get("actions", []):
        if (a.get("action") == "summarize" and a.get("status") == "done"
                and a.get("result")):
            summary_block = f'<div class="ai-sum">&#128221; {esc(a["result"])}</div>'
        else:
            chips.append(_action_chip(a))
    chips_html = f'<div class="achips">{"".join(chips)}</div>' if chips else ""

    if group_by == "task":
        retag = _move_select(t, all_clusters)
    elif group_by == "initiative":
        retag = _retag_select(t, "initiative", t["initiative"],
                              t.get("init_override", False), active_inits or [])
    elif group_by == "workstream":
        retag = _retag_select(t, "workstream", t["workstream"],
                              t.get("ws_override", False), all_ws or [])
    else:
        retag = ""
    actions_row = f'<div class="tab-actions">{retag}{_action_select(t)}</div>'

    pin_cls = "iconbtn pin on" if t["pinned"] else "iconbtn pin"
    pin_title = "Unpin" if t["pinned"] else "Pin to top of this group"
    return (
        f'<div class="tab{" pinned" if t["pinned"] else ""}">'
        '<img class="fav" alt="" referrerpolicy="no-referrer" '
        f'src="https://www.google.com/s2/favicons?sz=32&domain={esc(t["domain"])}">'
        '<div class="tab-main">'
        f'<div class="tab-titlerow">{handle}'
        f'<a class="tab-title" href="{esc(t["url"])}" target="_blank" '
        f'rel="noopener">{esc(t["title"])}</a></div>'
        f'<div class="meta">{meta}</div>{ded}{summary_block}'
        f'<textarea class="note" data-url="{esc(t["url"])}" '
        'placeholder="Add a note &mdash; what were you doing here?">'
        f'{esc(t["user_note"])}</textarea>{actions_row}{chips_html}</div>'
        '<div class="tab-side">'
        f'<button class="{pin_cls}" data-url="{esc(t["url"])}" '
        f'title="{pin_title}">&#128204;</button>'
        f'<button class="iconbtn x-close" data-url="{esc(t["url"])}" '
        'title="Close this tab in Chrome">&#10005;</button>'
        '</div></div>'
    )


def _render_section(s: dict, all_clusters: list, group_by: str,
                    cluster_actions: bool, active_inits: list | None = None,
                    all_ws: list | None = None) -> str:
    stale = " stale" if s["stale"] else ""
    pill = '<span class="pill stale">stale</span>' if s["stale"] else ""
    summary = f'<p class="cl-sum">{esc(s["summary"])}</p>' if s["summary"] else ""
    cnt = s["count"]
    actions_row = ""
    if cluster_actions:
        name = esc(s["name"])
        buttons = (
            f'<button class="act-btn" data-action="notion_task" data-cluster="{name}">'
            '&rarr; Notion</button>'
            f'<button class="act-btn" data-action="ingest" data-cluster="{name}">'
            'Ingest</button>'
            f'<button class="act-btn" data-action="synthesize" data-cluster="{name}">'
            'Synthesize</button>'
        )
        chips = "".join(_action_chip(a) for a in s.get("actions", []))
        actions_row = f'<div class="cl-actions">{buttons}{chips}</div>'
    park_row = ""
    if s.get("parkable"):
        cnt = s["count"]
        park_row = (
            '<div class="cl-actions">'
            f'<button class="act-btn park" data-initiative="{esc(s["key"])}" '
            f'title="Snapshot these {cnt} tabs to the initiative folder and '
            f'close them in Chrome">&#9208; Park initiative</button></div>'
        )
    cnt = s["count"]
    head = (
        f'<section class="cluster{stale}"><div class="cl-head{stale}">'
        f'<p class="cl-title">{esc(s["name"])} {pill}'
        f'<span class="pill">{cnt} tab{"" if cnt == 1 else "s"}</span></p>'
        f'{summary}{actions_row}{park_row}</div>'
    )
    body = "".join(
        render_tab(t, all_clusters, group_by, active_inits, all_ws)
        for t in s["tabs"]
    )
    return head + body + "</section>"


def render_capture(v: dict) -> str:
    """The corpus strip — what passive capture has learned while you browsed.

    The board's whole job is to make the invisible legible, and passive capture is
    the most invisible thing Handle does: it runs with no command and no output.
    Without a surface you'd have no idea whether it was working, or what it took.
    """
    try:
        st = capture.status()
        recent = webindex.recent(limit=5, source="passive")
    except Exception:
        return ""
    if not st.get("enabled") and not st.get("pages"):
        return ""

    dwell = st["min_dwell_ms"] // 1000
    state = ("on" if st["enabled"] else "off")
    rows = "".join(
        f'<li><a href="{esc(r["url"])}" target="_blank">{esc(r["title"] or r["url"])[:72]}</a>'
        f'<span class="cap-k">{r["chars"] // 1000}k</span></li>'
        for r in recent
    )
    if recent:
        body = f'<ul class="cap-list">{rows}</ul>'
    elif st["enabled"]:
        body = ('<p class="cap-none">Nothing captured yet &mdash; read a page for '
                f'{dwell}s and it lands here.</p>')
    else:
        body = '<p class="cap-none">Turned off. <code>tab capture on</code></p>'

    return (
        '<section class="cap">'
        f'<div class="cap-head"><b>Passive capture</b>'
        f'<span class="cap-dot {state}"></span>'
        f'<span class="cap-sub">{dwell}s dwell &middot; {st["exclusions"]} exclusions</span></div>'
        f'<div class="cap-nums">'
        f'<span class="cap-big">{st["pages"]}</span><span class="cap-lbl">pages indexed</span>'
        f'<span class="cap-sep">&middot;</span>'
        f'<span class="cap-big">{st["passive"]}</span><span class="cap-lbl">captured while you browsed</span>'
        f'<span class="cap-sep">&middot;</span>'
        f'<span class="cap-big">{st["today"]}</span><span class="cap-lbl">today</span>'
        f'</div>{body}'
        '<p class="cap-foot">Search it from Claude Code: <code>tab search "&hellip;"</code></p>'
        '</section>'
    )


INDEX_STYLE = """
<style>
  /* min-width:0 on the fluid column is load-bearing: a 1fr track refuses to
     shrink below its content's min-width, and one long un-breakable URL would
     otherwise push the whole page into a horizontal scroll. */
  .ix { display:grid; grid-template-columns:230px minmax(0,1fr); gap:22px; margin-top:20px; }
  .ix > * { min-width:0; }
  .ix-sites { list-style:none; margin:0; padding:0; font-size:13px; }
  .ix-sites li a { display:flex; justify-content:space-between; gap:8px; align-items:baseline;
                   padding:6px 9px; border-radius:7px; color:var(--ink); text-decoration:none; }
  .ix-sites li a:hover { background:var(--card); }
  .ix-sites li a.on { background:var(--accent); color:#fff; }
  .ix-sites li a.on .ix-n { color:#cfe4e1; }
  .ix-host { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .ix-n { color:var(--mut); font-size:11px; font-variant-numeric:tabular-nums; }
  .ix-search { display:flex; gap:8px; margin-bottom:14px; }
  .ix-search input { flex:1; padding:9px 12px; border:1px solid var(--line);
                     border-radius:8px; font:14px inherit; background:var(--card); }
  .ix-row { background:var(--card); border:1px solid var(--line); border-radius:9px;
            padding:11px 14px; margin-bottom:8px; min-width:0; overflow:hidden; }
  .ix-row a.t { color:var(--ink); text-decoration:none; font-weight:600; font-size:14px;
                display:block; overflow-wrap:anywhere; }
  .ix-row a.t:hover { color:var(--accent); }
  .ix-meta { color:var(--mut); font-size:11.5px; margin-top:3px;
             overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .ix-snip { font-size:12.5px; color:#5f5b53; margin-top:6px; }
  .ix-snip b { background:#f3eee2; font-weight:600; }
  .ix-tag { font-size:10px; border:1px solid var(--line); border-radius:4px;
            padding:1px 5px; color:var(--mut); margin-right:6px; }
  .ix-bar { display:flex; justify-content:space-between; align-items:center;
            margin-bottom:12px; font-size:12.5px; color:var(--mut); }
  .ix-forget { border:1px solid #e6c4b8; background:#fbeeea; color:#8a3b22;
               font:600 11px/1 inherit; padding:6px 10px; border-radius:7px; cursor:pointer; }
  @media (max-width:720px) { .ix { grid-template-columns:1fr; } }
</style>
"""


def render_index(domain: str = "", q: str = "", source: str = "") -> str:
    """The corpus, browsable. Passive capture is silent by construction, so the
    only honest way to run it is to make what it took inspectable — every site,
    every page, and a one-click way to throw a site back out."""
    doms = webindex.domains()
    rows = webindex.pages(domain=domain or None, source=source or None,
                          q=q or None, limit=150)
    st = webindex.stats()

    def qs(**kw):
        p = {"domain": domain, "q": q, "source": source}
        p.update(kw)
        return "/index?" + "&".join(f"{k}={quote(v)}" for k, v in p.items() if v)

    out = [HEAD, INDEX_STYLE, '<div class="wrap">',
           '<header><div><h1>Index</h1>'
           f'<p class="sub">{st["pages"]} pages &middot; {st["passive"]} captured as you '
           f'browsed &middot; {st["read"]} read by an agent &middot; {st.get("db_mb", 0)} MB</p></div>'
           '<div class="head-right">'
           '<a class="btn ghost" href="/">&larr; Tabs</a></div></header>']

    out.append('<div class="ix"><div>')
    out.append(f'<ul class="ix-sites"><li><a href="{qs(domain="")}" '
               f'class="{"on" if not domain else ""}"><span class="ix-host">All sites</span>'
               f'<span class="ix-n">{st["pages"]}</span></a></li>')
    for d in doms:
        on = "on" if d["domain"] == domain else ""
        out.append(f'<li><a class="{on}" href="{qs(domain=d["domain"])}">'
                   f'<span class="ix-host">{esc(d["domain"])}</span>'
                   f'<span class="ix-n">{d["pages"]}</span></a></li>')
    out.append('</ul></div><div>')

    out.append(f'<form class="ix-search" method="get" action="/index">'
               f'<input type="hidden" name="domain" value="{esc(domain)}">'
               f'<input name="q" value="{esc(q)}" placeholder="Search the full text of everything you\'ve read&hellip;" autofocus>'
               f'<button class="btn" type="submit">Search</button></form>')

    out.append('<div class="ix-bar"><span>')
    if q:
        out.append(f'{len(rows)} hit{"" if len(rows) == 1 else "s"} for &ldquo;{esc(q)}&rdquo;')
    else:
        out.append(f'{len(rows)} page{"" if len(rows) == 1 else "s"}')
    if domain:
        out.append(f' in <b>{esc(domain)}</b>')
    out.append('</span>')
    if domain:
        out.append(f'<button class="ix-forget" data-domain="{esc(domain)}">'
                   f'Forget {esc(domain)} &amp; stop indexing it</button>')
    out.append('</div>')

    if not rows:
        out.append('<div class="empty">Nothing here yet.</div>')
    for r in rows:
        tag = "captured" if r.get("source") == "passive" else "agent-read"
        snip = r.get("snippet") or ""
        snip = esc(snip).replace("«", "<b>").replace("»", "</b>") if snip else ""
        out.append(
            f'<div class="ix-row"><a class="t" href="{esc(r["url"])}" target="_blank">'
            f'{esc(r["title"] or r["url"])}</a>'
            f'<div class="ix-meta"><span class="ix-tag">{tag}</span>'
            f'{esc(r["ts"][:10])} &middot; {r.get("chars", 0) // 1000}k &middot; {esc(r["url"])}</div>'
            + (f'<div class="ix-snip">&hellip;{snip}&hellip;</div>' if snip else "")
            + '</div>')

    out.append('</div></div>')
    out.append("""
<script>
document.querySelectorAll('.ix-forget').forEach(function (b) {
  b.addEventListener('click', async function () {
    var d = b.dataset.domain;
    if (!confirm('Delete every page from ' + d + ' out of the index, and never index it again?')) return;
    b.disabled = true; b.textContent = 'Forgetting…';
    await fetch('/api/capture/forget', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ domain: d }),
    });
    location.href = '/index';
  });
});
</script></div>""")
    return "".join(out)


def render_page(v: dict, error: str | None = None) -> str:
    out = [HEAD, '<div class="wrap">']
    total = v["total"]
    collected = esc(v["last_collected_h"]) if v["last_collected"] else "not yet"
    mode = v["group_by"]
    by_task = mode == "task"

    def _tog(m, label):
        return (f'<button data-mode="{m}" class="{"on" if mode == m else ""}">'
                f'{label}</button>')

    out.append(
        '<header><div><h1>Handle</h1>'
        f'<p class="sub">{total} open tab{"" if total == 1 else "s"} '
        f'&middot; collected {collected}</p></div>'
        '<div class="head-right"><div class="toggle">'
        + _tog("task", "Task") + _tog("initiative", "Initiative")
        + _tog("workstream", "Workstream") + _tog("window", "Window")
        + '</div><a class="btn ghost" href="/index" '
        'title="Everything you have read, searchable">&#9636; Index</a>'
        '<a class="btn ghost" href="/stash" '
        'title="Your curated link library">&#9733; Stash</a>'
        '<form method="post" action="/api/refresh">'
        '<button class="btn" type="submit">&#8635; Refresh</button>'
        '</form></div></header>'
    )
    out.append(render_capture(v))
    parked = v.get("parked", {})
    if parked:
        chips = "".join(
            f'<button class="act-btn resume" data-initiative="{esc(slug)}" '
            f'title="Reopen {rec.get("count", 0)} parked tabs in Chrome">'
            f'&#9654; {esc(_pretty(slug))} '
            f'<span class="pill">{rec.get("count", 0)}</span></button>'
            for slug, rec in sorted(parked.items())
        )
        out.append(
            '<div class="banner info"><b>Parked initiatives.</b> '
            f'Resume to reopen the tab set:<div class="cl-actions" '
            f'style="margin-top:8px">{chips}</div></div>'
        )
    if error:
        out.append(
            '<div class="banner err"><b>Couldn&rsquo;t read Chrome.</b> '
            f'{esc(error)}<br>If this looks like a permissions error, allow it '
            'under System Settings &rarr; Privacy &amp; Security &rarr; '
            'Automation.</div>'
        )
    if v["pending_actions"]:
        n = v["pending_actions"]
        out.append(
            f'<div class="banner act">&#9889; {n} action{"" if n == 1 else "s"} '
            'queued. In Claude Code, say <code>run my tab actions</code> '
            'to execute them.</div>'
        )
    if v["needs_deduction"]:
        n = v["needs_deduction"]
        out.append(
            f'<div class="banner info">{n} tab{"" if n == 1 else "s"} not yet '
            'sorted into tasks. In Claude Code, say <code>deduce my tabs</code> '
            '&mdash; or use the group menu under any tab to sort it yourself.</div>'
        )
    if not v["sections"] and not v["unsorted"]:
        out.append(
            '<div class="empty">No tabs collected yet.<br>'
            'Click <b>Refresh</b> to pull your open Chrome tabs.</div>'
        )
    ai, aw = v.get("active_inits", []), v.get("all_workstreams", [])
    for s in v["sections"]:
        out.append(_render_section(s, v["all_clusters"], group_by=mode,
                                   cluster_actions=by_task,
                                   active_inits=ai, all_ws=aw))
    if v["unsorted"]:
        label = v.get("unsorted_label", "Unsorted")
        out.append(f'<h2 class="section">{esc(label)}</h2>')
        out.append(_render_section(
            {"name": label, "key": "", "summary": "", "stale": False,
             "count": len(v["unsorted"]), "tabs": v["unsorted"],
             "actions": [], "parkable": False},
            v["all_clusters"], group_by=mode, cluster_actions=False,
            active_inits=ai, all_ws=aw,
        ))
    out.append('</div>')
    out.append(SCRIPT)
    return "".join(out)


# --------------------------------------------------------------------------
# stash view — the curated link library
# --------------------------------------------------------------------------
_SRC_EMOJI = {
    "SharePoint": "\U0001F4C4", "OneDrive": "\U0001F4C4", "Google Drive": "\U0001F4DD",
    "Figma": "\U0001F3A8", "Notion": "\U0001F4D4", "Confluence": "\U0001F4D8",
    "Jira": "\U0001F9E9", "GitHub": "\U0001F419", "Loom": "\U0001F3A5",
    "Miro": "\U0001F5FA️", "Box": "\U0001F4E6", "Dropbox": "\U0001F4E6", "Web": "\U0001F517",
}

STASH_STYLE = """
<style>
  .sbar { display:flex; gap:10px; margin:20px 0 6px; flex-wrap:wrap; }
  .sbar input.q { flex:1; min-width:200px; border:1px solid var(--line);
     border-radius:8px; padding:10px 12px; font:14px inherit; background:var(--card); }
  .addrow { display:flex; gap:8px; margin:14px 0 4px; }
  .addrow input { flex:1; border:1px solid var(--line); border-radius:8px;
     padding:10px 12px; font:14px inherit; background:var(--card); }
  .chips { display:flex; gap:7px; flex-wrap:wrap; margin:14px 0 6px; }
  .chip { border:1px solid var(--line); background:var(--card); color:var(--mut);
     padding:6px 11px; border-radius:20px; font:600 12px/1 inherit; cursor:pointer; }
  .chip.on { background:var(--accent); color:#fff; border-color:var(--accent); }
  .link { background:var(--card); border:1px solid var(--line); border-radius:11px;
     padding:13px 15px; margin:9px 0; display:flex; gap:12px; align-items:flex-start; }
  .link .emoji { font-size:19px; line-height:1.2; }
  .link .body { flex:1; min-width:0; }
  .link a.tt { display:block; font-weight:600; color:var(--ink); text-decoration:none;
     font-size:15px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .link a.tt:hover { color:var(--accent); text-decoration:underline; }
  .link .url, .sug .url { color:var(--mut); font-size:12px; margin-top:3px; overflow:hidden;
     text-overflow:ellipsis; white-space:nowrap; }
  .link .meta { margin-top:6px; font-size:12px; color:var(--mut); display:flex;
     gap:8px; flex-wrap:wrap; align-items:center; }
  .link .src { color:var(--accent); font-weight:600; }
  .tag { background:#eef4f3; border:1px solid #cfe1de; color:#3d5f5b;
     padding:1px 8px; border-radius:20px; font-size:11px; }
  .link .snote { font-style:italic; }
  .link .acts { display:flex; gap:6px; align-items:center; }
  .iconbtn { border:none; background:none; cursor:pointer; font-size:15px;
     color:var(--mut); padding:2px 4px; border-radius:6px; }
  .iconbtn:hover { background:var(--bg); }
  .iconbtn.star.on { color:var(--amber); }
  .sug { background:#f3eee2; border:1px solid #e6dcc4; border-radius:11px;
     padding:11px 14px; margin:9px 0; display:flex; gap:12px; align-items:flex-start; }
  .sug.hidden { display:none; }
  .sug .body { flex:1; min-width:0; }
  .sug .tt { display:block; font-weight:600; font-size:14px; color:var(--ink);
     overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .showmore { display:block; width:100%; border:1px dashed #e6dcc4; background:none;
     color:var(--mut); padding:9px 12px; border-radius:9px; font:600 12px/1 inherit;
     cursor:pointer; margin:6px 0 0; }
  .showmore:hover { color:var(--accent); border-color:var(--accent); }
  .sug .keep { border:1px solid var(--accent); background:var(--accent); color:#fff;
     padding:6px 12px; border-radius:7px; font:600 12px/1 inherit; cursor:pointer; }
  .sug .dismiss { border:1px solid var(--line); background:var(--card); color:var(--mut);
     padding:6px 12px; border-radius:7px; font:600 12px/1 inherit; cursor:pointer; }
  .subhead { font-size:12px; text-transform:uppercase; letter-spacing:0.07em;
     color:var(--mut); margin:26px 0 4px; }
  .muted { color:var(--mut); font-size:13px; }
</style>
"""


def _stash_link_html(r: dict) -> str:
    emoji = _SRC_EMOJI.get(r.get("source", "Web"), "\U0001F517")
    url = r["url"]
    title = esc(r.get("title") or url)
    tags = "".join(f'<span class="tag">{esc(t)}</span>' for t in r.get("tags", []))
    note = f'<span class="snote">&laquo;{esc(r["note"])}&raquo;</span>' if r.get("note") else ""
    opens = f'<span>{r["open_count"]}&times; opened</span>' if r.get("open_count") else ""
    ws = f'<span class="tag">{esc(r["workstream"])}</span>' if r.get("workstream") else ""
    starred = "on" if r.get("starred") else ""
    star_glyph = "★" if r.get("starred") else "☆"
    hay = " ".join([r.get("title", ""), url, r.get("note", ""),
                    " ".join(r.get("tags", [])), r.get("source", "")]).lower()
    return (
        f'<div class="link" data-src="{esc(r.get("source", "Web"))}" '
        f'data-hay="{esc(hay)}">'
        f'<div class="emoji">{emoji}</div>'
        f'<div class="body">'
        f'<a class="tt" href="{esc(url)}" target="_blank" rel="noreferrer" '
        f'data-url="{esc(url)}">{title}</a>'
        f'<div class="url">{esc(url)}</div>'
        f'<div class="meta"><span class="src">{esc(r.get("source", "Web"))}</span>'
        f'{ws}{tags}{note}{opens}</div></div>'
        f'<div class="acts">'
        f'<button class="iconbtn star {starred}" data-url="{esc(url)}" '
        f'title="Star">{star_glyph}</button>'
        f'<button class="iconbtn tagbtn" data-url="{esc(url)}" title="Add tag">#</button>'
        f'<button class="iconbtn notebtn" data-url="{esc(url)}" title="Note">&#9998;</button>'
        f'<button class="iconbtn rm" data-url="{esc(url)}" title="Remove">&times;</button>'
        f'</div></div>'
    )


def _stash_sug_html(c: dict, hidden: bool = False) -> str:
    emoji = _SRC_EMOJI.get(c.get("source", "Web"), "\U0001F517")
    return (
        f'<div class="sug{" hidden" if hidden else ""}" data-url="{esc(c["url"])}">'
        f'<div class="emoji">{emoji}</div>'
        f'<div class="body">'
        f'<span class="tt">{esc(c.get("title") or c["url"])}</span>'
        f'<div class="muted">{esc(c.get("source", "Web"))} &middot; '
        f'pasted {c.get("count", 1)}&times;</div>'
        f'<div class="url">{esc(c["url"])}</div></div>'
        f'<div class="acts">'
        f'<button class="keep" data-url="{esc(c["url"])}" '
        f'data-title="{esc(c.get("title", ""))}">Keep</button>'
        f'<button class="dismiss" data-url="{esc(c["url"])}">Dismiss</button>'
        f'</div></div>'
    )


def render_stash() -> str:
    rows = stash.search(sort="recent")
    counts = stash.source_counts()
    try:
        suggestions = stash.scan(days=7)
    except Exception:
        suggestions = []

    out = [HEAD.replace("<title>Handle</title>", "<title>Stash &middot; Handle</title>"),
           STASH_STYLE, '<div class="wrap">']
    out.append(
        '<header><div><h1>Stash</h1>'
        f'<p class="sub">{len(rows)} kept link{"" if len(rows) == 1 else "s"} '
        '&middot; SharePoint, Drive, Figma &amp; other docs you keep</p></div>'
        '<div class="head-right"><a class="btn ghost" href="/">&larr; Tabs</a></div>'
        '</header>'
    )
    # add box
    out.append(
        '<div class="addrow">'
        '<input id="addurl" placeholder="Paste a URL to keep it — SharePoint, '
        'Drive, Figma, Notion…" />'
        '<button class="btn" id="addbtn">Keep</button></div>'
    )
    # suggestions from session scan (passive)
    fresh = [c for c in suggestions]
    if fresh:
        out.append(f'<div class="subhead">Suggested from Chrome history &amp; your '
                   f'sessions &middot; {len(fresh)} doc link'
                   f'{"" if len(fresh) == 1 else "s"} you opened but never kept</div>')
        SUG_VISIBLE = 8
        for i, c in enumerate(fresh):
            out.append(_stash_sug_html(c, hidden=(i >= SUG_VISIBLE)))
        if len(fresh) > SUG_VISIBLE:
            out.append(f'<button class="showmore" id="sugmore">Show all '
                       f'{len(fresh)} suggestions</button>')
    # search + source filter chips
    out.append('<div class="sbar"><input class="q" id="q" '
               'placeholder="Search your stash…" /></div>')
    if counts:
        chips = ['<span class="chip on" data-src="">All</span>']
        for src, n in counts.items():
            chips.append(f'<span class="chip" data-src="{esc(src)}">'
                         f'{esc(src)} {n}</span>')
        out.append('<div class="chips">' + "".join(chips) + '</div>')
    # links
    if not rows:
        out.append('<div class="empty" style="margin-top:20px">'
                   'Nothing stashed yet.<br>Paste a URL above, or keep a suggestion, '
                   'or run <code>tab stash add &lt;url&gt;</code>.</div>')
    else:
        out.append('<div id="links">')
        for r in rows:
            out.append(_stash_link_html(r))
        out.append('</div>')
    out.append('</div>')
    out.append(STASH_SCRIPT)
    return "".join(out)


STASH_SCRIPT = """
<script>
function post(url, body, after) {
  return fetch(url, { method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify(body) }).then(after || function(){ location.reload(); });
}
// add a link
function doAdd() {
  var el = document.getElementById('addurl');
  var v = (el.value || '').trim();
  if (!v) return;
  post('/api/stash/add', { url: v });
}
document.getElementById('addbtn').addEventListener('click', doAdd);
document.getElementById('addurl').addEventListener('keydown', function(e){
  if (e.key === 'Enter') doAdd();
});
// star / remove / tag / note
document.querySelectorAll('.iconbtn.star').forEach(function(b){
  b.addEventListener('click', function(){ post('/api/stash/star', { url: b.dataset.url }); });
});
document.querySelectorAll('.iconbtn.rm').forEach(function(b){
  b.addEventListener('click', function(){
    b.disabled = true; post('/api/stash/remove', { url: b.dataset.url }); });
});
document.querySelectorAll('.iconbtn.tagbtn').forEach(function(b){
  b.addEventListener('click', function(){
    var t = (prompt('Add tag(s), space-separated:') || '').trim();
    if (!t) return;
    post('/api/stash/tag', { url: b.dataset.url, tags: t.split(/\\s+/) }); });
});
document.querySelectorAll('.iconbtn.notebtn').forEach(function(b){
  b.addEventListener('click', function(){
    var n = prompt('Note:');
    if (n === null) return;
    post('/api/stash/note', { url: b.dataset.url, note: n }); });
});
// bump open-count when a stashed link is opened
document.querySelectorAll('a.tt').forEach(function(a){
  a.addEventListener('click', function(){
    fetch('/api/stash/touch', { method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ url: a.dataset.url }) }); });
});
// suggestions: show all
var sm = document.getElementById('sugmore');
if (sm) sm.addEventListener('click', function(){
  document.querySelectorAll('.sug.hidden').forEach(function(el){
    el.classList.remove('hidden'); });
  sm.style.display = 'none';
});
// suggestions: keep / dismiss
document.querySelectorAll('.sug .keep').forEach(function(b){
  b.addEventListener('click', function(){
    post('/api/stash/keep', { url: b.dataset.url, title: b.dataset.title }); });
});
document.querySelectorAll('.sug .dismiss').forEach(function(b){
  b.addEventListener('click', function(){
    var card = b.closest('.sug'); if (card) card.style.display='none';
    fetch('/api/stash/dismiss', { method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ url: b.dataset.url }) }); });
});
// client-side search + source filter
var activeSrc = '';
function applyFilter() {
  var q = (document.getElementById('q').value || '').toLowerCase().trim();
  var terms = q ? q.split(/\\s+/) : [];
  document.querySelectorAll('#links .link').forEach(function(el){
    var hay = el.dataset.hay || '';
    var okSrc = !activeSrc || el.dataset.src === activeSrc;
    var okQ = terms.every(function(t){ return hay.indexOf(t) !== -1; });
    el.style.display = (okSrc && okQ) ? '' : 'none';
  });
}
var qbox = document.getElementById('q');
if (qbox) qbox.addEventListener('input', applyFilter);
document.querySelectorAll('.chip').forEach(function(c){
  c.addEventListener('click', function(){
    document.querySelectorAll('.chip').forEach(function(x){ x.classList.remove('on'); });
    c.classList.add('on'); activeSrc = c.dataset.src || ''; applyFilter();
  });
});
</script>
"""


# --------------------------------------------------------------------------
# server
# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep the console quiet
        pass

    def _send(self, code: int, body, ctype: str = "text/html; charset=utf-8"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        try:
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass  # browser closed the tab or reloaded mid-response

    def _redirect(self, location: str):
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        try:
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError):
            pass  # browser went away before the redirect was sent

    def _json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw or b"{}")
        except Exception:
            return {}

    def _ok(self):
        self._send(200, json.dumps({"ok": True}), "application/json")

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/":
            error = parse_qs(parsed.query).get("error", [None])[0]
            self._send(200, render_page(build_view(), error))
        elif parsed.path == "/stash":
            self._send(200, render_stash())
        elif parsed.path == "/index":
            qp = parse_qs(parsed.query)
            self._send(200, render_index(
                domain=qp.get("domain", [""])[0],
                q=qp.get("q", [""])[0],
                source=qp.get("source", [""])[0]))
        elif parsed.path == "/api/state":
            payload = {
                "state": collector.load_json(STATE_PATH, {}),
                "deductions": collector.load_json(DEDUCTIONS_PATH, {}),
                "actions": collector.load_json(ACTIONS_PATH, {}),
            }
            self._send(200, json.dumps(payload, indent=2),
                       "application/json; charset=utf-8")
        elif parsed.path == "/ext/status":
            self._send(200, json.dumps(extbridge.status()), "application/json")
        elif parsed.path == "/api/record/status":
            self._send(200, json.dumps(recorder.status()), "application/json")
        elif parsed.path == "/api/record/list":
            self._send(200, json.dumps(recorder.listing()), "application/json")
        elif parsed.path == "/ext/poll":
            # extension long-polls here for the next live command
            cmd = extbridge.poll(wait=25.0)
            self._send(200, json.dumps(cmd or {}), "application/json")
        elif parsed.path == "/ext/policy":
            # passive capture rules — the extension applies these BEFORE it
            # extracts any text, so excluded domains are never even read
            self._send(200, json.dumps(capture.policy()), "application/json")
        elif parsed.path == "/api/capture/status":
            self._send(200, json.dumps(capture.status()), "application/json")
        else:
            self._send(404, "not found", "text/plain; charset=utf-8")

    def do_POST(self):
        parsed = urlparse(self.path)

        if parsed.path == "/api/refresh":
            try:
                collector.collect()
                self._redirect("/")
            except Exception as exc:
                msg = str(exc).replace("\n", " ").strip()[:300]
                self._redirect("/?error=" + quote(msg))
            return

        data = self._json_body()

        # --- stash: the curated link library -------------------------------
        if parsed.path.startswith("/api/stash/"):
            self._stash_action(parsed.path.rsplit("/", 1)[-1], data)
            return

        # --- Chrome extension bridge ---------------------------------------
        if parsed.path == "/ext/sync":
            # extension pushed the current tabs/groups → refresh state.json
            ext_tabs = data.get("tabs", [])
            try:
                collector.collect_from_ext(ext_tabs)
            except Exception:
                pass
            extbridge.mark_sync({"tab_count": len(ext_tabs),
                                 "ext_version": data.get("version", "")})
            self._ok()
            return
        if parsed.path == "/ext/result":
            extbridge.deliver_result(data.get("id", ""), data.get("result", {}))
            self._ok()
            return
        if parsed.path == "/ext/cmd":
            # the CLI asks the extension to do something live
            result = extbridge.enqueue(
                data.get("method", ""), data.get("params", {}),
                timeout=float(data.get("timeout", 20)))
            self._send(200, json.dumps(result), "application/json")
            return
        if parsed.path == "/ext/record":
            # the extension streams CDP network/nav/tab events during a session
            n = recorder.append(data.get("events", []))
            self._send(200, json.dumps({"appended": n}), "application/json")
            return
        if parsed.path == "/api/capture/forget":
            # throw a site out of the index AND stop it coming back: delete what's
            # stored, then add the exclusion. Doing only one of the two is the
            # mistake — forgetting without excluding just re-indexes it tomorrow.
            dom = (data.get("domain") or "").strip()
            n = webindex.forget(dom) if dom else 0
            if dom:
                capture.exclude_add(dom)
            self._send(200, json.dumps({"forgotten": n, "excluded": dom}),
                       "application/json")
            return
        if parsed.path == "/ext/index":
            # passive capture: a page you dwelled on long enough to have read.
            # capture.ingest re-checks the policy — the extension's filter is the
            # first gate, this is the net under it.
            res = capture.ingest(data.get("url", ""), data.get("title", ""),
                                 data.get("text", ""), int(data.get("dwell_ms", 0)))
            self._send(200, json.dumps(res), "application/json")
            return

        # --- workflow recorder control (CLI) -------------------------------
        if parsed.path == "/api/record/start":
            sess = recorder.start(data.get("label", ""))
            # tell the extension to begin streaming (best-effort; capture is
            # passive once it starts, so don't block long on the handshake)
            ext = extbridge.enqueue("record_start", {}, timeout=8)
            self._send(200, json.dumps({"session": sess, "ext": ext}),
                       "application/json")
            return
        if parsed.path == "/api/record/stop":
            extbridge.enqueue("record_stop", {}, timeout=8)
            done = recorder.stop()
            self._send(200, json.dumps({"session": done}), "application/json")
            return

        if parsed.path == "/api/action":
            self._queue_action(data)
            return
        if parsed.path == "/api/action/cancel":
            actions = collector.load_json(ACTIONS_PATH, {"queue": []})
            actions["queue"] = [q for q in actions.get("queue", [])
                                if q.get("id") != data.get("id")]
            collector.write_json(ACTIONS_PATH, actions)
            self._ok()
            return
        if parsed.path == "/api/groupby":
            mode = data.get("mode")
            if mode in GROUP_MODES:
                state = collector.load_json(STATE_PATH, {"tabs": {}})
                state["group_by"] = mode
                collector.write_json(STATE_PATH, state)
            self._ok()
            return
        if parsed.path == "/api/park":
            self._park(data.get("initiative", ""))
            return
        if parsed.path == "/api/resume":
            slug = data.get("initiative", "")
            urls = workspaces.resume(slug)
            if urls:
                collector.open_urls(urls)
            self._ok()
            return

        # tab-targeted mutations
        url = data.get("url")
        state = collector.load_json(STATE_PATH, {"tabs": {}})
        tabs = state.get("tabs", {})
        if parsed.path == "/api/note":
            if url in tabs:
                tabs[url]["user_note"] = data.get("note", "")
                collector.write_json(STATE_PATH, state)
            self._ok()
        elif parsed.path == "/api/pin":
            if url in tabs:
                tabs[url]["pinned"] = not bool(tabs[url].get("pinned"))
                collector.write_json(STATE_PATH, state)
            self._ok()
        elif parsed.path == "/api/move":
            if url in tabs:
                tabs[url]["user_cluster"] = (data.get("cluster") or "").strip()
                collector.write_json(STATE_PATH, state)
            self._ok()
        elif parsed.path == "/api/retag":
            field = data.get("field")
            if url in tabs and field in ("initiative", "workstream"):
                key = "user_" + field
                value = data.get("value")
                if value == "__auto__":
                    tabs[url].pop(key, None)          # clear override -> deduced
                else:
                    tabs[url][key] = (value or "").strip()
                collector.write_json(STATE_PATH, state)
            self._ok()
        elif parsed.path == "/api/close":
            collector.close_tab(url)
            if url in tabs:
                del tabs[url]
                collector.write_json(STATE_PATH, state)
            self._ok()
        else:
            self._send(404, "not found", "text/plain; charset=utf-8")

    def _park(self, slug: str):
        """Snapshot an initiative's tabs to its folder, then close them."""
        if not slug:
            self._ok()
            return
        ded = collector.load_json(DEDUCTIONS_PATH, {"tabs": {}})
        ded_tabs = ded.get("tabs", {})
        state = collector.load_json(STATE_PATH, {"tabs": {}})
        tabs = state.get("tabs", {})
        targets = [
            {"url": url, "title": tab.get("title", "")}
            for url, tab in tabs.items()
            if ded_tabs.get(url, {}).get("initiative") == slug
            and not tab.get("pinned")
        ]
        if not targets:
            self._ok()
            return
        workspaces.park(slug, targets)
        for t in targets:
            collector.close_tab(t["url"])
            tabs.pop(t["url"], None)
        collector.write_json(STATE_PATH, state)
        self._ok()

    def _stash_action(self, verb: str, data: dict):
        url = (data.get("url") or "").strip()
        if verb == "add" and url:
            stash.add(url, title=data.get("title") or None,
                      tags=data.get("tags") or [], note=data.get("note") or None,
                      origin="board")
        elif verb == "keep" and url:
            stash.add(url, title=data.get("title") or None, origin="session")
        elif verb == "remove" and url:
            stash.remove(url)
        elif verb == "dismiss" and url:
            stash.dismiss(url)
        elif verb == "star" and url:
            cur = stash.load()["links"].get(url, {})
            stash.set_star(url, not cur.get("starred"))
        elif verb == "tag" and url:
            stash.set_tags(url, data.get("tags") or [])
        elif verb == "note" and url:
            stash.set_note(url, data.get("note", ""))
        elif verb == "touch" and url:
            stash.touch(url)
        self._ok()

    def _queue_action(self, data: dict):
        action = data.get("action")
        scope = data.get("scope")
        if action not in ACTION_LABELS or scope not in ("tab", "cluster"):
            self._ok()
            return
        url = data.get("url")
        cluster = (data.get("cluster") or "").strip()
        actions = collector.load_json(ACTIONS_PATH, {"queue": []})
        queue = actions.setdefault("queue", [])
        already = any(
            q.get("status") == "pending" and q.get("action") == action
            and q.get("scope") == scope and q.get("url") == url
            and q.get("cluster") == cluster
            for q in queue
        )
        if not already:
            queue.append({
                "id": uuid.uuid4().hex[:12],
                "action": action,
                "scope": scope,
                "url": url,
                "cluster": cluster,
                "status": "pending",
                "result": "",
                "queued_at": datetime.now().astimezone().isoformat(),
            })
            collector.write_json(ACTIONS_PATH, actions)
        self._ok()


if __name__ == "__main__":
    print(f"Handle  →  http://127.0.0.1:{PORT}")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
