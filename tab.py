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
import io
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import collect

APP_DIR = Path(__file__).resolve().parent
STATE_PATH = APP_DIR / "state.json"
DEDUCTIONS_PATH = APP_DIR / "deductions.json"
BUNDLES_DIR = APP_DIR / "bundles"  # saved research captures (gitignored)
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


def _export_url(kind: str, url: str, fmt: str = "txt") -> str | None:
    if kind == "gdoc":
        m = re.search(r"/document/d/([^/]+)", url)
        return f"https://docs.google.com/document/d/{m.group(1)}/export?format={fmt}" if m else None
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


# A read rung that returns the editor's chrome instead of the document is a
# failure, not content — catch the tell-tale toolbar so the cascade falls
# through (e.g. a Google Doc/Sheet whose body is canvas-rendered).
_CHROME_RE = re.compile(
    r"File\s+Edit\s+View\s+Insert\s+Format\s+Tools|Share\s+Ask\s+Gemini|"
    r"Ask Gemini\s+File|Menu\s+Home\s+Insert\s+Draw", re.I)


def _is_junk(text: str, kind: str) -> bool:
    t = (text or "").strip()
    if not t:
        return True
    if _CHROME_RE.search(t[:240]):
        return True
    # a "doc" kind that comes back as a sliver is almost certainly chrome
    if kind in ("gdoc", "gsheet", "gslides", "office") and len(t) < 40:
        return True
    return False


def _csv_to_md(csv_text: str, max_rows: int = 200) -> str:
    """CSV → a Markdown table (first row as header)."""
    import csv as _csv
    rows = list(_csv.reader(io.StringIO(csv_text)))
    rows = [r for r in rows if any(c.strip() for c in r)][:max_rows]
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    def line(cells):
        cells = (cells + [""] * width)[:width]
        return "| " + " | ".join(c.replace("|", "\\|").replace("\n", " ").strip() for c in cells) + " |"
    out = [line(rows[0]), "| " + " | ".join(["---"] * width) + " |"]
    out += [line(r) for r in rows[1:]]
    return "\n".join(out)


def _figma_ref(url: str) -> dict:
    m = re.search(r"figma\.com/(?:file|design|proto|board|slides)/([A-Za-z0-9]+)", url)
    node = re.search(r"[?&]node-id=([^&]+)", url)
    return {"file_key": m.group(1) if m else None,
            "node_id": node.group(1).replace("-", ":") if node else None}


def _screenshot_payload(tab: dict, kind: str, args) -> dict:
    """The catch-all: capture the tab as image(s) for the agent to read with
    vision. The only path for Figma/PDF/Office (no DOM text), and forced by
    --shot for anything visual."""
    base = {"id": tab["id"], "title": tab["title"], "url": tab["url"], "kind": kind}
    shots, err = collect.screenshot_tab(tab["url"], full=getattr(args, "full", False))
    p = {**base, "source": "screenshot", "chars": 0, "content": "", "shots": shots, "note": ""}
    if kind == "figma":
        p.update(_figma_ref(tab["url"]))
    if not shots:
        hint = {"figma": " Or read it via the Figma MCP get_design_context (file_key above).",
                "pdf": " Or WebFetch the URL if the PDF is public.",
                "office": ""}.get(kind, "")
        p["note"] = err + hint
    return p


def _gexport(url, kind, fmt, chars):
    """One Google-export attempt → (text, note). Markdown-first where supported."""
    exp = _export_url(kind, url, fmt)
    if not exp:
        return "", "Could not parse the file id from the URL."
    raw = collect.run_tab_js(url, _sync_xhr_js(exp), chars)
    if raw.startswith("__HANDLE_HTTP__"):
        code = raw[len("__HANDLE_HTTP__"):][:3]
        return "", (f"Export blocked (HTTP {code}) — the owner may have disabled "
                    "download/export, or you're not signed in to this Google account.")
    if kind == "gsheet" and raw:
        return _csv_to_md(raw), ""
    return raw, ""


def _text_rungs(tab, url, kind, args, chars):
    """Ordered (source, fn→(text, note)) rungs for a kind. Markdown is the
    default text shape; flat text is its fallback; screenshot is the floor."""
    md_mode, live = getattr(args, "md", False), getattr(args, "live", False)
    if kind == "html":
        rungs = []
        if not live and not md_mode:
            rungs.append(("cache", lambda: (tab.get("snippet", ""), "")))
        rungs.append(("markdown", lambda: (collect.read_tab_markdown(url, chars), "")))
        rungs.append(("live", lambda: (collect.read_tab_content(url, chars), "")))
        return rungs
    if kind == "gdoc":
        return [("export-md", lambda: _gexport(url, kind, "markdown", chars)),
                ("export-txt", lambda: _gexport(url, kind, "txt", chars))]
    if kind == "gslides":
        return [("export-txt", lambda: _gexport(url, kind, "txt", chars))]
    if kind == "gsheet":
        return [("export-csv", lambda: _gexport(url, kind, "csv", chars))]
    if kind == "office":
        return [("docx", lambda: (collect.read_office_docx(url, limit=max(chars, 40000)), ""))]
    return []  # figma / pdf / office-non-word → straight to the screenshot floor


def _read_one(tab: dict, args) -> dict:
    """Read one tab as a cascade: try the best text rung(s) for the content,
    fall through when a rung returns nothing or just the editor's chrome, then
    the screenshot floor (vision), and finally a precise note. Markdown is the
    default text shape; never a silent blank."""
    url, kind = tab["url"], detect_kind(tab["url"])
    base = {"id": tab["id"], "title": tab["title"], "url": url, "kind": kind}
    chars = args.chars if args.chars is not None else (20000 if getattr(args, "md", False) else 8000)

    # explicit overrides
    if getattr(args, "clipboard", False):
        clip = collect.read_clipboard()
        return {**base, "source": "clipboard", "chars": len(clip), "content": clip[:chars],
                "note": "" if clip else "Clipboard is empty — copy something first."}
    if getattr(args, "shot", False):
        return _screenshot_payload(tab, kind, args)

    tried, last_note = [], ""
    for source, fn in _text_rungs(tab, url, kind, args, chars):
        try:
            text, note = fn()
        except Exception as exc:
            text, note = "", str(exc)
        tried.append(source)
        if note:
            last_note = note
        if text and not _is_junk(text, kind):
            return {**base, "source": source, "chars": len(text), "content": text[:chars], "note": ""}

    # floor 1 — screenshot → vision (reads anything rendered)
    shot = _screenshot_payload(tab, kind, args)
    if shot.get("shots"):
        shot["tried"] = tried
        return shot

    # floor 2 — nothing worked; say exactly why + the one fix
    note = last_note or {
        "html": "No readable content. Enable Chrome → View → Developer → 'Allow JavaScript from Apple Events'.",
    }.get(kind, "Couldn't read this tab.")
    if shot.get("note"):
        note = f"{note} (screenshot also failed: {shot['note']})"
    note += " — or copy it (⌘A ⌘C) and read with --clipboard."
    return {**base, "source": "none", "chars": 0, "content": "", "tried": tried, "note": note}


def _format_shot_block(p: dict) -> str:
    head = f"# {p['title']}  ·  {p['kind']} (screenshot)"
    figref = ""
    if p["kind"] == "figma" and p.get("file_key"):
        figref = (f"\n[Figma file {p['file_key']}"
                  + (f", node {p['node_id']}" if p.get("node_id") else "") + "]")
    if p["shots"]:
        return (f"{head}\n{p['url']}{figref}\n\n"
                f"{len(p['shots'])} screenshot(s) — Read these image files to see the tab:\n"
                + "\n".join(p["shots"]))
    return f"{head}\n{p['url']}{figref}\n\n({p['note']})"


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
        if p.get("source") == "screenshot":
            blocks.append(_format_shot_block(p))
            continue
        head = f"# {p['title']}"
        if p["kind"] != "html":
            head += f"  ·  {p['kind']}"
        elif p["source"] == "cache":
            head += "  ·  cached (use --live for full page)"
        body = p["content"] or (
            f"({p['note']})" if p.get("note") else
            "(No content. Enable Chrome → View → Developer → 'Allow JavaScript from Apple Events'.)")
        blocks.append(f"{head}\n{p['url']}\n\n{body}")
    print("\n\n———\n\n".join(blocks))


def cmd_shot(args) -> None:
    """Screenshot a tab so the agent can read it with vision — the catch-all
    for anything the DOM can't give you (Figma, PDFs, Office, charts)."""
    tabs = load_tabs()
    tab = resolve_one(args.ref, tabs)
    shots, err = collect.screenshot_tab(tab["url"], full=args.full)
    if args.json:
        print(json.dumps({"id": tab["id"], "title": tab["title"], "url": tab["url"],
                          "shots": shots, "error": err}, indent=2, ensure_ascii=False))
        return
    if not shots:
        print(f"Could not screenshot {tab['id']}  {tab['title']}\n({err})")
        return
    print(f"# {tab['title']} — {len(shots)} screenshot(s)\n{tab['url']}\n\n"
          "Read these image files to see the tab:\n" + "\n".join(shots))


def cmd_grab(args) -> None:
    """Read the macOS clipboard — the human-assisted rung. You copy (⌘A ⌘C in
    the tab, no focus race), I grab it. The reliable floor when in-page reads
    can't get clean content (e.g. a stubborn Office editor)."""
    clip = collect.read_clipboard()
    if args.json:
        print(json.dumps({"chars": len(clip), "content": clip}, ensure_ascii=False))
        return
    if not clip.strip():
        print("Clipboard is empty — copy the content first (⌘A then ⌘C), then run this.")
        return
    print(clip)


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


_STOPWORDS = set((
    "the a an and or of to in on for with is are was were be been being do does did "
    "how what why when where which who whom this that these those my your our their it "
    "its as at by from about into over under after before than then so if not no can "
    "could would should may might will just have has had i you we they me us them "
    "tell show give find read about across all any some more most vs versus between"
).split())


def _terms(question: str) -> list[str]:
    """Content words from the question — lowercased, de-stopworded, length>2."""
    words = re.findall(r"[a-z0-9][a-z0-9'-]*", question.lower())
    seen, out = set(), []
    for w in words:
        if len(w) > 2 and w not in _STOPWORDS and w not in seen:
            seen.add(w)
            out.append(w)
    return out


class _ReadArgs:
    """Minimal args for _read_one — read each source as format-aware markdown."""
    def __init__(self, chars):
        self.live, self.md, self.shot, self.full, self.json, self.chars = False, True, False, False, False, chars


def cmd_ask(args) -> None:
    """Retrieve the open tabs relevant to a question and assemble a cited bundle
    the agent answers from. Handle does no AI — it ranks by the question's terms,
    reads the top matches as clean content, and hands them over with handles to
    cite. Cross-tab, low-token: the thing single-tab browser tools can't do."""
    question = " ".join(args.question).strip()
    terms = _terms(question)
    tabs = load_tabs()

    scored = []
    for t in tabs:
        title, snip = t["title"].lower(), (t["snippet"] or "").lower()
        score = sum(title.count(w) * 3 + snip.count(w) for w in terms)
        if score:
            scored.append((score, t))
    # also search saved research bundles when --saved (capture → recall → ask)
    if getattr(args, "saved", False):
        for src in _saved_sources():
            low, title = src["content"].lower(), src["title"].lower()
            score = sum(title.count(w) * 3 + low.count(w) for w in terms)
            if score:
                scored.append((score, src))
    scored.sort(key=lambda x: -x[0])
    top = scored[:args.tabs]

    if not top:
        where = "open tabs" + (" or saved bundles" if getattr(args, "saved", False) else "")
        msg = (f"Nothing in your {where} mentions {terms or [question]}. "
               "Run `tab refresh`, try `--saved`, or rephrase the question.")
        print(json.dumps({"question": question, "terms": terms, "sources": []}) if args.json else msg)
        return

    sources = []
    for score, t in top:
        if t.get("_saved"):                      # already-captured bundle source
            content, kind = t["content"], t.get("kind", "saved")
        else:                                    # live tab — read it fresh
            p = _read_one(t, _ReadArgs(args.chars))
            content, kind = (p.get("content") or t["snippet"] or t["summary"] or ""), p.get("kind", "html")
        sources.append({"id": t["id"], "title": t["title"], "url": t["url"],
                        "score": score, "kind": kind, "content": content[:args.chars]})

    if args.json:
        print(json.dumps({"question": question, "terms": terms, "sources": sources}, indent=2, ensure_ascii=False))
        return

    print(f"ASK: {question}")
    print(f"Pulled {len(sources)} tab(s) by relevance (term hits: {', '.join(terms) or '—'}):")
    for s in sources:
        print(f"  {s['id']:>4}  [{s['score']:>2}]  {s['title'][:64]}")
    print("\n" + "=" * 60)
    for s in sources:
        print(f"\n### {s['id']} · {s['title']}\n{s['url']}\n\n{s['content']}\n")
    print("=" * 60)
    print("→ Answer the question from these sources; cite tabs by handle (e.g. t3).")


# --------------------------------------------------------------------------- #
# saved research bundles — capture tab content now, recall it later
# --------------------------------------------------------------------------- #
def _slug(s: str) -> str:
    return (re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:60]) or "bundle"


def _strip_frontmatter(text: str) -> str:
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            return parts[2].strip()
    return text


def _saved_sources() -> list[dict]:
    """Every captured tab across all bundles, as ask-able source records."""
    out = []
    if not BUNDLES_DIR.exists():
        return out
    for mf in BUNDLES_DIR.glob("*/manifest.json"):
        try:
            m = json.loads(mf.read_text(encoding="utf-8"))
        except Exception:
            continue
        for e in m.get("tabs", []):
            f = mf.parent / e["file"]
            if not f.exists():
                continue
            out.append({"id": f"{e['handle']}@{m.get('slug', mf.parent.name)}",
                        "title": f"{e['title']} (saved: {m.get('label', '')})",
                        "url": e["url"], "kind": e.get("kind", "saved"),
                        "content": _strip_frontmatter(f.read_text(encoding="utf-8")),
                        "_saved": True})
    return out


def cmd_save(args) -> None:
    """Snapshot the content of a set of open tabs into a labeled, dated bundle —
    so the research survives the tabs being closed and the session ending."""
    tabs = load_tabs()
    if args.group:
        picked = [t for t in tabs if args.group.lower() in t["group"].lower()]
    elif args.all:
        picked = tabs
    elif args.refs:
        seen, picked = set(), []
        for ref in args.refs:
            for t in resolve(ref, tabs):
                if t["id"] not in seen:
                    seen.add(t["id"])
                    picked.append(t)
    else:
        print("Nothing to save. Give tab refs, --group <name>, or --all.")
        sys.exit(1)
    if not picked:
        print("No tabs matched.")
        sys.exit(1)

    label = args.as_ or args.group or "research"
    slug = _slug(label)
    now = datetime.now(timezone.utc)
    bdir = BUNDLES_DIR / f"{now.strftime('%Y-%m-%d')}-{slug}"
    bdir.mkdir(parents=True, exist_ok=True)
    created = now.isoformat()

    entries = []
    for t in picked:
        p = _read_one(t, _ReadArgs(args.chars))
        content = p.get("content") or t["snippet"] or ""
        fname = f"{t['id']}-{_slug(t['title'])[:40]}.md"
        fm = (f"---\ntitle: {t['title']}\nurl: {t['url']}\nhandle: {t['id']}\n"
              f"kind: {p.get('kind', 'html')}\ncaptured: {created}\n---\n\n")
        (bdir / fname).write_text(fm + (content or "(no readable content captured)"), encoding="utf-8")
        entries.append({"handle": t["id"], "title": t["title"], "url": t["url"],
                        "kind": p.get("kind", "html"), "file": fname, "chars": len(content)})

    manifest = {"label": label, "slug": slug, "created": created,
                "tab_count": len(entries), "tabs": entries}
    (bdir / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    idx = [f"# {label}", f"_Captured {created[:10]} · {len(entries)} tab(s)_", ""]
    idx += [f"- **{e['handle']}** [{e['title']}]({e['url']}) — {e['kind']}, {e['chars']} chars → `{e['file']}`"
            for e in entries]
    (bdir / "INDEX.md").write_text("\n".join(idx) + "\n", encoding="utf-8")

    if args.json:
        print(json.dumps({"saved": str(bdir), **manifest}, indent=2, ensure_ascii=False))
        return
    print(f"Saved {len(entries)} tab(s) → bundle '{slug}'  ({bdir})")
    for e in entries:
        print(f"  {e['handle']:>4}  {e['title'][:58]}  ({e['chars']}c)")
    print(f"Recall later: tab recall {slug}   ·   search it: tab ask \"…\" --saved")


def cmd_bundles(args) -> None:
    BUNDLES_DIR.mkdir(exist_ok=True)
    items = []
    for mf in BUNDLES_DIR.glob("*/manifest.json"):
        try:
            items.append((mf.parent.name, json.loads(mf.read_text(encoding="utf-8"))))
        except Exception:
            continue
    items.sort(key=lambda x: x[1].get("created", ""), reverse=True)
    if args.json:
        print(json.dumps([{"dir": d, **m} for d, m in items], indent=2, ensure_ascii=False))
        return
    if not items:
        print("No saved bundles yet. Capture one with `tab save <refs> --as <name>`.")
        return
    print(f"{len(items)} saved bundle(s):\n")
    for d, m in items:
        print(f"  {m.get('label', '?')}  ·  {m.get('tab_count', 0)} tab(s)  ·  {m.get('created', '')[:10]}  ·  {d}")


def cmd_recall(args) -> None:
    BUNDLES_DIR.mkdir(exist_ok=True)
    q = args.name.lower()
    matches = []
    for mf in BUNDLES_DIR.glob("*/manifest.json"):
        try:
            m = json.loads(mf.read_text(encoding="utf-8"))
        except Exception:
            continue
        if q in mf.parent.name.lower() or q in m.get("label", "").lower():
            matches.append((mf.parent, m))
    if not matches:
        print(f"No bundle matches {args.name!r}. `tab bundles` to list them.")
        return
    if len(matches) > 1 and not args.json:
        print(f"{len(matches)} bundles match {args.name!r} — be specific:")
        for d, m in matches:
            print(f"  {m.get('label')}  ·  {d.name}")
        return
    d, m = matches[0]
    srcs = []
    for e in m["tabs"]:
        f = d / e["file"]
        srcs.append({"handle": e["handle"], "title": e["title"], "url": e["url"],
                     "content": _strip_frontmatter(f.read_text(encoding="utf-8")) if f.exists() else ""})
    if args.json:
        print(json.dumps({"label": m.get("label"), "created": m.get("created"), "sources": srcs},
                         indent=2, ensure_ascii=False))
        return
    print(f"# {m.get('label')} (captured {m.get('created', '')[:10]} · {len(srcs)} tab(s))")
    for s in srcs:
        body = s["content"][:args.chars] if args.chars else s["content"]
        print(f"\n### {s['handle']} · {s['title']}\n{s['url']}\n\n{body}\n")


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

    sa2 = sub.add_parser("ask", help="assemble the open tabs relevant to a question (cited bundle to answer from)")
    sa2.add_argument("question", nargs="+", help="the question, in plain words")
    sa2.add_argument("--tabs", type=int, default=5, help="max tabs to pull (default 5)")
    sa2.add_argument("--chars", type=int, default=4000, help="max characters per source")
    sa2.add_argument("--saved", action="store_true", help="also search saved research bundles, not just open tabs")
    sa2.add_argument("--json", action="store_true")
    sa2.set_defaults(func=cmd_ask)

    sv = sub.add_parser("save", help="snapshot tab content into a dated bundle for later reference")
    sv.add_argument("refs", nargs="*", help="tab references to save (or use --group / --all)")
    sv.add_argument("--as", dest="as_", help="label for the bundle (e.g. 'vendor-research')")
    sv.add_argument("--group", help="save all tabs in this group")
    sv.add_argument("--all", action="store_true", help="save every open tab")
    sv.add_argument("--chars", type=int, default=20000, help="max characters captured per tab")
    sv.add_argument("--json", action="store_true")
    sv.set_defaults(func=cmd_save)

    sgrab = sub.add_parser("grab", help="read the clipboard (you copy, I grab) — the reliable read floor")
    sgrab.add_argument("--json", action="store_true")
    sgrab.set_defaults(func=cmd_grab)

    sb = sub.add_parser("bundles", help="list saved research bundles")
    sb.add_argument("--json", action="store_true")
    sb.set_defaults(func=cmd_bundles)

    srec = sub.add_parser("recall", help="load a saved bundle's content as context")
    srec.add_argument("name", help="bundle name/label (or a substring)")
    srec.add_argument("--chars", type=int, default=6000, help="max characters per source (0 = full)")
    srec.add_argument("--json", action="store_true")
    srec.set_defaults(func=cmd_recall)

    sr = sub.add_parser("read", help="page text of one or more tabs (cached; --live for full)")
    sr.add_argument("refs", nargs="+", help="one or more tab references")
    sr.add_argument("--live", action="store_true",
                    help="read the live rendered page now instead of the cached snippet")
    sr.add_argument("--md", action="store_true",
                    help="convert the page to Markdown (headings/lists/links/tables) — best for text-heavy HTML")
    sr.add_argument("--shot", action="store_true",
                    help="screenshot the tab instead of reading text (the agent reads it with vision)")
    sr.add_argument("--clipboard", action="store_true",
                    help="read the macOS clipboard (you copy with ⌘A ⌘C, I grab it) instead of the tab")
    sr.add_argument("--full", action="store_true",
                    help="with --shot/figma/pdf: scroll and capture the whole page, not just the viewport")
    sr.add_argument("--chars", type=int, default=None,
                    help="max characters per tab (default 8000, or 20000 with --md)")
    sr.add_argument("--json", action="store_true")
    sr.set_defaults(func=cmd_read)

    ssh = sub.add_parser("shot", help="screenshot a tab for the agent to read with vision")
    ssh.add_argument("ref")
    ssh.add_argument("--full", action="store_true", help="scroll and capture the whole page")
    ssh.add_argument("--json", action="store_true")
    ssh.set_defaults(func=cmd_shot)

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
