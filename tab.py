#!/usr/bin/env python3
"""tab — the Claude Code bridge to your real, logged-in Chrome.

Every open tab has a stable short handle (t1, t2, …) that survives refreshes.
Reference a tab by its handle, its number, a fuzzy title/url match, or `active`
(the tab you're looking at right now). The headline command is `read`: it pulls
the *live rendered text* of a tab you're already logged into — past login walls
and JS rendering that defeat a plain fetch — for a few hundred tokens.

Designed to be driven from a Claude Code session. Output is compact and
greppable; pass --json to any command for machine-readable output.

    tab list                 # every open tab, with handles + groups + stale
    tab find figma           # resolve a fuzzy query to handle(s) by title/url
    tab grep "fable"         # search the on-page content of every tab
    tab read t7              # page text of that tab (cached; --live for full)
    tab read t29 t30 t4      # read several tabs at once
    tab show t7 t3           # full detail for specific tabs
    tab active               # the frontmost tab ("what I'm looking at")
    tab open t7              # bring that tab to the front
    tab close t7             # close it in Chrome for real
    tab note t7 "ship this"  # attach a note (wins over Claude's guess)
    tab group t7 "Org Chart" # move it to a group
    tab pin t7 / unpin t7
    tab refresh              # re-read Chrome into state.json

Reference forms accepted everywhere a tab is expected:
    t7    (handle)   ·   7   (bare number)   ·   active   ·   a fuzzy substring
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import collect

APP_DIR = Path(__file__).resolve().parent
STATE_PATH = APP_DIR / "state.json"
DEDUCTIONS_PATH = APP_DIR / "deductions.json"
STALE_DAYS = 3


# --------------------------------------------------------------------------- #
# loading + enrichment
# --------------------------------------------------------------------------- #
def _now_unix() -> float:
    return datetime.now(timezone.utc).timestamp()


def load_tabs(refresh: bool = False) -> list[dict]:
    """Return every tab as a flat, enriched record sorted by handle number.

    Each record merges state.json (handle, note, pin, timestamps) with the
    Claude-written deductions.json (group, one-line summary) and a computed
    `stale` flag, so a single record has everything a caller needs.
    """
    if refresh:
        collect.collect()
    state = collect.load_json(STATE_PATH, {})
    _backfill_ids(state)
    ded = collect.load_json(DEDUCTIONS_PATH, {"tabs": {}})
    ded_tabs = ded.get("tabs", {})
    now = _now_unix()

    out: list[dict] = []
    for url, tab in (state.get("tabs", {}) or {}).items():
        guess = ded_tabs.get(url, {})
        last_visit = tab.get("last_visit")
        age = (now - last_visit) / 86400 if last_visit else None
        pinned = bool(tab.get("pinned"))
        out.append({
            "id": tab.get("id", "?"),
            "url": url,
            "title": tab.get("title") or url,
            "group": tab.get("user_cluster") or guess.get("cluster", ""),
            "summary": guess.get("deduction", ""),
            "note": tab.get("user_note", ""),
            "snippet": tab.get("snippet", ""),
            "window": tab.get("window", 0),
            "pinned": pinned,
            "age_days": round(age, 1) if age is not None else None,
            "stale": bool(age is not None and age >= STALE_DAYS and not pinned),
            "last_seen": tab.get("last_seen"),
        })
    out.sort(key=lambda t: _id_num(t["id"]))
    return out


def _backfill_ids(state: dict) -> None:
    """Assign stable handles to any tab missing one (e.g. pre-handle state).

    Writes back only when something changed, so the handles persist for the
    next refresh just as if collect() had assigned them.
    """
    tabs = state.get("tabs", {})
    if not tabs:
        return
    next_id = state.get("next_id", 1)
    changed = False
    for tab in tabs.values():
        if isinstance(tab, dict) and not tab.get("id"):
            tab["id"] = f"t{next_id}"
            next_id += 1
            changed = True
    if changed:
        state["next_id"] = next_id
        collect.write_json(STATE_PATH, state)


def _id_num(handle: str) -> int:
    try:
        return int(str(handle).lstrip("t"))
    except (ValueError, AttributeError):
        return 1 << 30


# --------------------------------------------------------------------------- #
# reference resolution: tN | N | active | fuzzy substring
# --------------------------------------------------------------------------- #
def resolve(ref: str, tabs: list[dict]) -> list[dict]:
    """Resolve one reference string to matching tab record(s).

    Exact handle / number / `active` return at most one; a fuzzy term returns
    every tab whose title, url, group, or note contains it (case-insensitive).
    """
    ref = ref.strip()
    if not ref:
        return []
    by_id = {t["id"]: t for t in tabs}

    if ref.lower() == "active":
        url = collect.active_tab_url()
        return [t for t in tabs if t["url"] == url] if url else []

    handle = ref if ref.startswith("t") else f"t{ref}"
    if ref.isdigit() or (handle in by_id and ref.lstrip("t").isdigit()):
        if handle in by_id:
            return [by_id[handle]]

    if ref in by_id:  # someone passed an exact handle like "t7"
        return [by_id[ref]]

    term = ref.lower()
    hits = [t for t in tabs
            if term in t["title"].lower()
            or term in t["url"].lower()
            or term in t["group"].lower()
            or term in t["note"].lower()]
    return hits


def resolve_one(ref: str, tabs: list[dict]) -> dict | None:
    """Resolve to exactly one tab; on ambiguity print the candidates and exit."""
    hits = resolve(ref, tabs)
    # `active` can point at a tab opened since the last collect — re-scan once
    # so "read active" / "open active" work first-try on a fresh frontmost tab.
    if not hits and ref.strip().lower() == "active":
        hits = resolve(ref, load_tabs(refresh=True))
    if not hits:
        print(f"No tab matches {ref!r}. Try `tab list` or `tab find <term>`.")
        sys.exit(1)
    if len(hits) > 1:
        print(f"{ref!r} matches {len(hits)} tabs — be specific:")
        for t in hits:
            print(_line(t))
        sys.exit(1)
    return hits[0]


# --------------------------------------------------------------------------- #
# formatting
# --------------------------------------------------------------------------- #
def _line(t: dict) -> str:
    """One compact, greppable line per tab."""
    flags = ""
    if t["pinned"]:
        flags += "📌"
    if t["stale"]:
        flags += "⏳"
    group = f"[{t['group']}] " if t["group"] else ""
    note = f"  «{t['note']}»" if t["note"] else ""
    age = ""
    if t["age_days"] is not None:
        age = f"  ({t['age_days']:g}d)"
    title = t["title"][:70]
    return f"{t['id']:>4}  {flags:<3} {group}{title}{age}{note}\n        {t['url']}"


def _emit(data, as_json: bool, human: str) -> None:
    if as_json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        print(human)


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
def cmd_list(args) -> None:
    tabs = load_tabs(refresh=args.refresh)
    if args.group:
        tabs = [t for t in tabs if args.group.lower() in t["group"].lower()]
    if args.stale:
        tabs = [t for t in tabs if t["stale"]]
    if args.window:
        tabs = [t for t in tabs if t["window"] == args.window]
    if args.json:
        print(json.dumps(tabs, indent=2, ensure_ascii=False))
        return
    if not tabs:
        print("No tabs. Run `tab refresh` (and make sure Chrome is open).")
        return
    print(f"{len(tabs)} open tab(s)  ·  📌 pinned  ⏳ stale ({STALE_DAYS}d+)\n")
    print("\n".join(_line(t) for t in tabs))


def cmd_find(args) -> None:
    tabs = load_tabs()
    hits = resolve(args.query, tabs)
    if args.json:
        print(json.dumps(hits, indent=2, ensure_ascii=False))
        return
    if not hits:
        print(f"No tab matches {args.query!r}.")
        return
    print("\n".join(_line(t) for t in hits))


# --------------------------------------------------------------------------- #
# content-type-aware reading
# --------------------------------------------------------------------------- #
# Most tabs are HTML and read fine via innerText. The rich formats don't put
# their content in the DOM — Docs/Sheets/Slides render to a canvas, Figma is
# WebGL, PDFs are a plugin, Office viewers are iframed — so each needs its own
# extractor. The Google formats are pulled by a SYNCHRONOUS in-tab XHR against
# their export endpoint: same-origin, so it carries the user's login, and
# synchronous so `execute javascript` gets the text (not an unresolved Promise).

def detect_kind(url: str) -> str:
    u = url.lower()
    if "figma.com" in u:
        return "figma"
    if "docs.google.com/document" in u:
        return "gdoc"
    if "docs.google.com/spreadsheets" in u:
        return "gsheet"
    if "docs.google.com/presentation" in u:
        return "gslides"
    if u.split("?")[0].split("#")[0].endswith(".pdf"):
        return "pdf"
    if "sharepoint.com" in u or "officeapps.live.com" in u or "office.com" in u:
        return "office"
    return "html"


def _sync_xhr_js(export_url: str) -> str:
    """JS that synchronously GETs `export_url` (same-origin, authenticated) and
    returns the body on 200, else ''. Sync XHR is required — execute javascript
    can't await a fetch()."""
    safe = export_url.replace("'", "\\'")
    return ("(function(){try{var x=new XMLHttpRequest();"
            f"x.open('GET','{safe}',false);x.send();"
            "return x.status===200?x.responseText:('__HANDLE_HTTP__'+x.status);}"
            "catch(e){return '';}})()")


def _export_url(kind: str, url: str) -> str | None:
    if kind == "gdoc":
        m = re.search(r"/document/d/([^/]+)", url)
        return f"https://docs.google.com/document/d/{m.group(1)}/export?format=txt" if m else None
    if kind == "gslides":
        m = re.search(r"/presentation/d/([^/]+)", url)
        return f"https://docs.google.com/presentation/d/{m.group(1)}/export/txt" if m else None
    if kind == "gsheet":
        m = re.search(r"/spreadsheets/d/([^/]+)", url)
        if not m:
            return None
        gid = re.search(r"[?#&]gid=(\d+)", url)
        base = f"https://docs.google.com/spreadsheets/d/{m.group(1)}/export?format=csv"
        return base + (f"&gid={gid.group(1)}" if gid else "")
    return None


def _figma_ref(url: str) -> dict:
    m = re.search(r"figma\.com/(?:file|design|proto|board|slides)/([A-Za-z0-9]+)", url)
    node = re.search(r"[?&]node-id=([^&]+)", url)
    return {"file_key": m.group(1) if m else None,
            "node_id": node.group(1).replace("-", ":") if node else None}


def _read_one(tab: dict, args) -> dict:
    """Read one tab, routing by content type. HTML uses the cached snippet
    (instant) or a live readability read with --live; Google formats are pulled
    fresh from their export endpoint; Figma/PDF/Office return a typed pointer
    instead of scraped text."""
    url, kind = tab["url"], detect_kind(tab["url"])
    base = {"id": tab["id"], "title": tab["title"], "url": url, "kind": kind}

    if kind == "figma":
        ref = _figma_ref(url)
        return {**base, "source": "figma", "chars": 0, "content": "", **ref,
                "note": "Figma is a WebGL canvas — no readable DOM text. Read it "
                        "via the Figma MCP get_design_context with this file_key/node_id."}

    if kind in ("gdoc", "gsheet", "gslides"):
        exp = _export_url(kind, url)
        text = collect.run_tab_js(url, _sync_xhr_js(exp), args.chars) if exp else ""
        note = ""
        if text.startswith("__HANDLE_HTTP__"):
            code = text[len("__HANDLE_HTTP__"):][:3]
            note = (f"Export blocked (HTTP {code}) — the file owner may have disabled "
                    "download/export, or you're not signed in to this Google account in Chrome.")
            text = ""
        elif not exp:
            note = "Could not parse the file id from the URL."
        elif not text:
            note = "Export returned nothing — not signed in, or no access to the file."
        return {**base, "source": "export", "chars": len(text), "content": text, "note": note}

    if kind in ("pdf", "office"):
        note = ("PDF — not readable as tab text. For a public PDF, WebFetch the URL."
                if kind == "pdf" else
                "Office web viewer — content is iframed, not in the DOM. Not supported "
                "(needs the Microsoft Graph API).")
        return {**base, "source": kind, "chars": 0, "content": "", "note": note}

    # html
    source, content = "cache", tab.get("snippet", "")
    if args.live or not content:
        live = collect.read_tab_content(url, limit=args.chars)
        if live:
            source, content = "live", live
    content = content[:args.chars]
    return {**base, "source": source, "chars": len(content), "content": content}


def cmd_read(args) -> None:
    tabs = load_tabs()
    picked = [resolve_one(ref, tabs) for ref in args.refs]
    payloads = [_read_one(t, args) for t in picked]

    if args.json:
        out = payloads[0] if len(payloads) == 1 else payloads
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return

    blocks = []
    for p in payloads:
        head = f"# {p['title']}"
        if p["kind"] != "html":
            head += f"  ·  {p['kind']}"
        elif p["source"] == "cache":
            head += "  ·  cached (use --live for full page)"
        body = p["content"]
        if not body:
            if p["kind"] == "figma":
                body = (f"[Figma file {p.get('file_key')}"
                        + (f", node {p['node_id']}" if p.get("node_id") else "")
                        + f"]\n→ {p['note']}")
            elif p.get("note"):
                body = f"({p['note']})"
            else:
                body = ("(No content. Enable Chrome → View → Developer → "
                        "'Allow JavaScript from Apple Events'.)")
        blocks.append(f"{head}\n{p['url']}\n\n{body}")
    print("\n\n———\n\n".join(blocks))


def cmd_grep(args) -> None:
    """Search the cached content of every open tab for a term.

    Finds tabs by what's *on the page*, not just the title — "which tab
    mentions Fable pricing". Shows a short excerpt around each match.
    """
    tabs = load_tabs()
    term = args.query.lower()
    hits = []
    for t in tabs:
        hay = f"{t['title']}\n{t['snippet']}".lower()
        pos = hay.find(term)
        if pos < 0:
            continue
        start = max(0, pos - 60)
        excerpt = t["snippet"] or t["title"]
        # locate the term within the original-case snippet for a readable excerpt
        snip_low = t["snippet"].lower()
        sp = snip_low.find(term)
        if sp >= 0:
            a = max(0, sp - 60)
            excerpt = ("…" if a > 0 else "") + t["snippet"][a:sp + len(term) + 80].strip() + "…"
        hits.append({**t, "excerpt": excerpt})

    if args.json:
        print(json.dumps(
            [{"id": h["id"], "title": h["title"], "url": h["url"], "excerpt": h["excerpt"]}
             for h in hits], indent=2, ensure_ascii=False))
        return
    if not hits:
        print(f"No open tab's content mentions {args.query!r}. "
              "(Cached snippets only — run `tab refresh` to re-scan, "
              "or the page may not have been captured.)")
        return
    print(f"{len(hits)} tab(s) mention {args.query!r}:\n")
    for h in hits:
        print(f"{h['id']:>4}  {h['title'][:70]}\n        {h['excerpt']}\n        {h['url']}")


def cmd_show(args) -> None:
    tabs = load_tabs()
    picked = []
    for ref in args.refs:
        picked.extend(resolve(ref, tabs))
    seen, uniq = set(), []
    for t in picked:
        if t["id"] not in seen:
            seen.add(t["id"])
            uniq.append(t)
    if args.json:
        print(json.dumps(uniq, indent=2, ensure_ascii=False))
        return
    if not uniq:
        print("No matching tabs.")
        return
    for t in uniq:
        print(_line(t))
        if t["summary"]:
            print(f"        summary: {t['summary']}")
        print(f"        window {t['window']}  ·  group: {t['group'] or '—'}"
              f"  ·  last seen {t['last_seen'] or '—'}")


def cmd_active(args) -> None:
    tabs = load_tabs()
    url = collect.active_tab_url()
    hit = next((t for t in tabs if t["url"] == url), None)
    if not hit and url:  # frontmost tab not yet in state — refresh once
        tabs = load_tabs(refresh=True)
        hit = next((t for t in tabs if t["url"] == url), None)
    if args.json:
        print(json.dumps(hit or {}, indent=2, ensure_ascii=False))
        return
    print(_line(hit) if hit else f"Frontmost tab: {url or '(none — is Chrome open?)'}")


def cmd_open(args) -> None:
    tabs = load_tabs()
    tab = resolve_one(args.ref, tabs)
    ok = collect.focus_tab(tab["url"])
    print(f"{'Focused' if ok else 'Could not focus'} {tab['id']}  {tab['title']}")


def cmd_close(args) -> None:
    tabs = load_tabs()
    for ref in args.refs:
        tab = resolve_one(ref, tabs)
        ok = collect.close_tab(tab["url"])
        print(f"{'Closed' if ok else 'Could not close'} {tab['id']}  {tab['title']}")


def _mutate_state(url: str, **fields) -> None:
    state = collect.load_json(STATE_PATH, {})
    tab = state.get("tabs", {}).get(url)
    if not tab:
        print("Tab not in state — run `tab refresh`.")
        sys.exit(1)
    tab.update(fields)
    collect.write_json(STATE_PATH, state)


def cmd_note(args) -> None:
    tabs = load_tabs()
    tab = resolve_one(args.ref, tabs)
    _mutate_state(tab["url"], user_note=" ".join(args.text))
    print(f"Noted {tab['id']}: {' '.join(args.text)!r}")


def cmd_group(args) -> None:
    tabs = load_tabs()
    tab = resolve_one(args.ref, tabs)
    _mutate_state(tab["url"], user_cluster=" ".join(args.name))
    print(f"Moved {tab['id']} → {' '.join(args.name)!r}")


def cmd_pin(args) -> None:
    tabs = load_tabs()
    tab = resolve_one(args.ref, tabs)
    _mutate_state(tab["url"], pinned=(args.cmd == "pin"))
    print(f"{'Pinned' if args.cmd == 'pin' else 'Unpinned'} {tab['id']}  {tab['title']}")


def cmd_refresh(args) -> None:
    state = collect.collect()
    print(f"Refreshed — {len(state['tabs'])} open tab(s) in state.json.")


# --------------------------------------------------------------------------- #
# argument wiring
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tab", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    sl = sub.add_parser("list", help="list every open tab")
    sl.add_argument("--group", help="only tabs whose group matches")
    sl.add_argument("--stale", action="store_true", help="only stale tabs")
    sl.add_argument("--window", type=int, help="only tabs in this window")
    sl.add_argument("--refresh", action="store_true", help="re-read Chrome first")
    sl.add_argument("--json", action="store_true")
    sl.set_defaults(func=cmd_list)

    sf = sub.add_parser("find", help="resolve a fuzzy query to handle(s) by title/url")
    sf.add_argument("query")
    sf.add_argument("--json", action="store_true")
    sf.set_defaults(func=cmd_find)

    sgr = sub.add_parser("grep", help="search the on-page content of all tabs")
    sgr.add_argument("query")
    sgr.add_argument("--json", action="store_true")
    sgr.set_defaults(func=cmd_grep)

    sr = sub.add_parser("read", help="page text of one or more tabs (cached; --live for full)")
    sr.add_argument("refs", nargs="+", help="one or more tab references")
    sr.add_argument("--live", action="store_true",
                    help="read the live rendered page now instead of the cached snippet")
    sr.add_argument("--chars", type=int, default=8000, help="max characters per tab")
    sr.add_argument("--json", action="store_true")
    sr.set_defaults(func=cmd_read)

    ss = sub.add_parser("show", help="full detail for one or more tabs")
    ss.add_argument("refs", nargs="+")
    ss.add_argument("--json", action="store_true")
    ss.set_defaults(func=cmd_show)

    sa = sub.add_parser("active", help="the frontmost tab")
    sa.add_argument("--json", action="store_true")
    sa.set_defaults(func=cmd_active)

    so = sub.add_parser("open", help="bring a tab to the front")
    so.add_argument("ref")
    so.set_defaults(func=cmd_open)

    sc = sub.add_parser("close", help="close tab(s) in Chrome")
    sc.add_argument("refs", nargs="+")
    sc.set_defaults(func=cmd_close)

    sn = sub.add_parser("note", help="attach a note to a tab")
    sn.add_argument("ref")
    sn.add_argument("text", nargs="+")
    sn.set_defaults(func=cmd_note)

    sg = sub.add_parser("group", help="move a tab to a group")
    sg.add_argument("ref")
    sg.add_argument("name", nargs="+")
    sg.set_defaults(func=cmd_group)

    for name in ("pin", "unpin"):
        sp = sub.add_parser(name, help=f"{name} a tab")
        sp.add_argument("ref")
        sp.set_defaults(func=cmd_pin)

    sx = sub.add_parser("refresh", help="re-read Chrome into state.json")
    sx.set_defaults(func=cmd_refresh)
    return p


def main(argv=None) -> None:
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
