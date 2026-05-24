#!/usr/bin/env python3
"""Tab Tasks — an on-demand board over your open Chrome tabs.

Pulls your current Chrome tabs when you ask, shows them grouped into the
tasks they belong to (deduced by Claude Code), lets you annotate, pin, move,
group, and close tabs, and lets you queue actions (summarize, push to Notion,
ingest, synthesize a cluster) that Claude Code then executes. Group the board
by task or by browser window.

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

APP_DIR = Path(__file__).resolve().parent
STATE_PATH = APP_DIR / "state.json"
DEDUCTIONS_PATH = APP_DIR / "deductions.json"
ACTIONS_PATH = APP_DIR / "actions.json"
STALE_DAYS = 3
PORT = 4910

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
    if group_by not in ("task", "window"):
        group_by = "task"
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
        last_visit = tab.get("last_visit")
        if last_visit:
            age = (now - last_visit) / 86400
        else:
            seen = _parse_iso(tab.get("first_seen"))
            age = (now - seen) / 86400 if seen else 0.0
        items.append({
            "url": url,
            "title": tab.get("title") or url,
            "domain": _domain(url),
            "user_note": tab.get("user_note", ""),
            "deduction": guess.get("deduction", ""),
            "cluster": tab.get("user_cluster") or guess.get("cluster", ""),
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

    if group_by == "window":
        windows: dict = {}
        for it in items:
            windows.setdefault(it["window"], []).append(it)
        for win in sorted(windows):
            tabs = windows[win]
            tabs.sort(key=lambda x: (not x["pinned"], x["index"]))
            non_pinned = [t for t in tabs if not t["pinned"]]
            sections.append({
                "name": f"Window {win}",
                "summary": "",
                "stale": _stale_section(non_pinned),
                "count": len(tabs),
                "tabs": tabs,
                "actions": [],
            })
    else:
        groups: dict = {}
        for it in items:
            groups.setdefault(it["cluster"], []).append(it)
        for name, tabs in groups.items():
            tabs.sort(key=lambda x: (not x["pinned"], -x["age_days"]))
            if not name:
                unsorted = tabs
                continue
            non_pinned = [t for t in tabs if not t["pinned"]]
            sections.append({
                "name": name,
                "summary": ded_clusters.get(name, {}).get("summary", ""),
                "stale": _stale_section(non_pinned),
                "count": len(tabs),
                "tabs": tabs,
                "actions": cluster_actions.get(name, []),
            })
        sections.sort(key=lambda s: (not s["stale"], -s["count"]))

    return {
        "group_by": group_by,
        "last_collected": state.get("last_collected"),
        "last_collected_h": _humanize(_parse_iso(state.get("last_collected"))),
        "sections": sections,
        "unsorted": unsorted,
        "all_clusters": all_clusters,
        "total": len(items),
        "needs_deduction": len([it for it in items if not it["cluster"]]),
        "pending_actions": pending_actions,
    }


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------
HEAD = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Tab Tasks</title>
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
         padding:9px 14px; border-radius:8px; font:600 12px/1 inherit; cursor:pointer; }
  .btn:active { transform:translateY(1px); }
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
  .tab-title { font-size:14px; font-weight:500; color:var(--ink);
               text-decoration:none; word-break:break-word; }
  .tab-title:hover { text-decoration:underline; }
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


def render_tab(t: dict, all_clusters: list, show_move: bool = True) -> str:
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

    move = _move_select(t, all_clusters) if show_move else ""
    actions_row = f'<div class="tab-actions">{move}{_action_select(t)}</div>'

    pin_cls = "iconbtn pin on" if t["pinned"] else "iconbtn pin"
    pin_title = "Unpin" if t["pinned"] else "Pin to top of this group"
    return (
        f'<div class="tab{" pinned" if t["pinned"] else ""}">'
        '<img class="fav" alt="" referrerpolicy="no-referrer" '
        f'src="https://www.google.com/s2/favicons?sz=32&domain={esc(t["domain"])}">'
        '<div class="tab-main">'
        f'<a class="tab-title" href="{esc(t["url"])}" target="_blank" '
        f'rel="noopener">{esc(t["title"])}</a>'
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


def _render_section(s: dict, all_clusters: list, show_move: bool,
                    cluster_actions: bool) -> str:
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
    head = (
        f'<section class="cluster{stale}"><div class="cl-head{stale}">'
        f'<p class="cl-title">{esc(s["name"])} {pill}'
        f'<span class="pill">{cnt} tab{"" if cnt == 1 else "s"}</span></p>'
        f'{summary}{actions_row}</div>'
    )
    body = "".join(render_tab(t, all_clusters, show_move) for t in s["tabs"])
    return head + body + "</section>"


def render_page(v: dict, error: str | None = None) -> str:
    out = [HEAD, '<div class="wrap">']
    total = v["total"]
    collected = esc(v["last_collected_h"]) if v["last_collected"] else "not yet"
    by_task = v["group_by"] == "task"
    out.append(
        '<header><div><h1>Tab Tasks</h1>'
        f'<p class="sub">{total} open tab{"" if total == 1 else "s"} '
        f'&middot; collected {collected}</p></div>'
        '<div class="head-right"><div class="toggle">'
        f'<button data-mode="task" class="{"on" if by_task else ""}">Task</button>'
        f'<button data-mode="window" class="{"" if by_task else "on"}">Window</button>'
        '</div><form method="post" action="/api/refresh">'
        '<button class="btn" type="submit">&#8635; Refresh</button>'
        '</form></div></header>'
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
    for s in v["sections"]:
        out.append(_render_section(s, v["all_clusters"], show_move=by_task,
                                   cluster_actions=by_task))
    if v["unsorted"]:
        out.append('<h2 class="section">Unsorted</h2>')
        out.append(_render_section(
            {"name": "Unsorted", "summary": "", "stale": False,
             "count": len(v["unsorted"]), "tabs": v["unsorted"], "actions": []},
            v["all_clusters"], show_move=by_task, cluster_actions=False,
        ))
    out.append('</div>')
    out.append(SCRIPT)
    return "".join(out)


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
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, location: str):
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

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
        elif parsed.path == "/api/state":
            payload = {
                "state": collector.load_json(STATE_PATH, {}),
                "deductions": collector.load_json(DEDUCTIONS_PATH, {}),
                "actions": collector.load_json(ACTIONS_PATH, {}),
            }
            self._send(200, json.dumps(payload, indent=2),
                       "application/json; charset=utf-8")
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
            if mode in ("task", "window"):
                state = collector.load_json(STATE_PATH, {"tabs": {}})
                state["group_by"] = mode
                collector.write_json(STATE_PATH, state)
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
        elif parsed.path == "/api/close":
            collector.close_tab(url)
            if url in tabs:
                del tabs[url]
                collector.write_json(STATE_PATH, state)
            self._ok()
        else:
            self._send(404, "not found", "text/plain; charset=utf-8")

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
    print(f"Tab Tasks  →  http://127.0.0.1:{PORT}")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
