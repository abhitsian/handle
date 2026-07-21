"""capture.py — passive indexing: the pages you actually read, indexed as you read them.

`tab search` was only ever as good as its corpus, and the corpus only grew when
an agent explicitly ran `tab read`. That indexed a handful of pages a week while
hundreds went past unrecorded. This closes that gap: the extension watches which
tab you're actually dwelling on, extracts the readable text once you've stayed a
while, and hands it here. Same FTS5 index, same `tab search`, no extra work.

Policy lives HERE (not in the extension) so there is one place to reason about
what gets recorded. The extension pulls this policy and applies it before it
extracts any text, and the server re-checks on ingest — an excluded domain is
dropped on both sides, so its content never lands on disk.

Defaults are deliberately conservative: auth flows, banking, health, password
managers, mail, and Handle's own local apps are excluded out of the box. Widen it
yourself (`tab capture include mail.google.com`) rather than discovering after
the fact that something personal got indexed.

No network, no API, no keys. Config in capture.json (gitignored).
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import webindex

_CFG = Path(__file__).resolve().parent / "capture.json"

# Conservative out-of-the-box exclusions. Matched against the URL's host+path,
# case-insensitively, as substrings — a blunt instrument on purpose: over-
# excluding costs you a search hit, under-excluding writes something private to
# disk. `tab capture include <term>` removes an entry.
DEFAULT_EXCLUDE = [
    # auth / SSO — transient, and the URLs carry tokens
    "accounts.google.com", "login.microsoftonline.com", "login.live.com",
    "okta.com", "auth0.com", "/oauth", "/signin", "/login", "/sso",
    # money
    "bank", "chase.com", "wellsfargo.com", "hdfc", "icici", "axisbank", "sbi.co",
    "groww.in", "zerodha.com", "kite.zerodha", "paypal.com", "coinbase.com",
    "stripe.com/dashboard",
    # health
    "myhealth", "patient", "healthcare", "practo", "1mg",
    # secrets
    "1password.com", "lastpass.com", "bitwarden.com",
    # mail — high value but personal; opt IN deliberately
    "mail.google.com", "outlook.office.com/mail", "outlook.live.com/mail",
    # Handle's own local apps and browser chrome — noise, not reading
    "127.0.0.1", "localhost", "chrome://", "chrome-extension://", "about:",
]

# Pages that have plenty of TEXT but no CONTENT. These are the index poisoners:
# a Google results page or a YouTube watch page clears any character floor you set
# (result snippets, video descriptions, comments) and then degrades every query you
# ever run against the corpus. A month of real browsing is ~31% this stuff, so the
# character floor alone is not a filter — the page's KIND has to be judged.
#
# Note the feed/permalink split: x.com/home is a scroll of other people's noise,
# but x.com/user/status/123 is a specific thing you chose to read. Index the second.
JUNK_KINDS = {
    "serp":   [r"google\.[a-z.]+/search", r"/search\?", r"bing\.com/search",
               r"duckduckgo\.com/\?", r"google\.[a-z.]+/?$"],
    "video":  [r"youtube\.com", r"youtu\.be", r"vimeo\.com", r"netflix\.com",
               r"hotstar", r"primevideo"],
    "feed":   [r"^https?://(www\.)?(x|twitter)\.com/?(home)?$",
               r"linkedin\.com/feed", r"^https?://(www\.)?reddit\.com/?$",
               r"facebook\.com", r"instagram\.com",
               r"^https?://(www\.)?teamblind\.com/?$"],
    "shop":   [r"amazon\.[a-z.]+/(gp|dp|s\?|cart)", r"flipkart", r"myntra",
               r"/checkout", r"/cart", r"swiggy", r"zomato", r"blinkit"],
    # App CHROME, not documents. The blind spot in a dwell-based filter: you sit
    # in a web app for twenty minutes, so it clears every bar — but a snapshot of
    # a live channel or an inbox is stale the moment it's taken, and re-indexes
    # with different content on every visit. A Zoom join page is a button.
    "appui":  [r"zoom\.us/j/", r"zoom\.us/postattendee", r"/notifications",
               r"linkedin\.com/jobs", r"linkedin\.com/mynetwork",
               r"/activity-inbox", r"teams\.microsoft\.com/.*\/conversations"],
}

DEFAULTS = {
    "enabled": True,
    "min_dwell_ms": 20000,   # 20s on screen and focused before a page counts as "read"
    "min_chars": 500,        # passive bar is higher than `tab read`'s 200 — skip stubs
    "recapture_after_h": 6,  # same URL again after this long → refresh the row
    "skip_kinds": ["serp", "video", "feed", "shop", "appui"],
    "exclude": list(DEFAULT_EXCLUDE),
}


def load() -> dict:
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.loads(_CFG.read_text()))
    except Exception:
        pass  # missing or corrupt → defaults, and the next save() heals it
    return cfg


def save(cfg: dict) -> None:
    _CFG.write_text(json.dumps(cfg, indent=2) + "\n")


def excluded_by(url: str, cfg: dict | None = None) -> str | None:
    """The exclusion term that blocks this URL, or None if it may be indexed.

    A term WITHOUT a slash matches the HOST only; a term WITH one matches
    host+path. That distinction matters: a bare "bank" checked against the whole
    URL would also block an article slugged `/the-banking-crisis`, and quietly
    losing pages to a substring collision is exactly the failure you'd never
    notice. Host terms block sites; path terms ("/oauth") block flows.
    """
    cfg = cfg or load()
    u = (url or "").lower()
    if not u.startswith(("http://", "https://")):
        return "not a web page"
    parts = urlparse(u)
    host = parts.netloc
    hostpath = host + parts.path
    for term in cfg.get("exclude", []):
        t = str(term).lower()
        hay = hostpath if "/" in t else host
        if t and t in hay:
            return term
    return None


def junk_kind(url: str, cfg: dict | None = None) -> str | None:
    """The junk kind this URL is (serp / video / feed / shop), or None if it's a
    real page. Judged on the URL alone so it can run before any text is pulled."""
    cfg = cfg or load()
    u = (url or "").lower()
    for kind in cfg.get("skip_kinds", []):
        for pat in JUNK_KINDS.get(kind, []):
            if re.search(pat, u):
                return kind
    return None


def policy() -> dict:
    """What the extension needs to enforce the same rules before extracting text."""
    cfg = load()
    return {
        "enabled": bool(cfg["enabled"]),
        "min_dwell_ms": int(cfg["min_dwell_ms"]),
        "min_chars": int(cfg["min_chars"]),
        "recapture_after_h": int(cfg["recapture_after_h"]),
        "exclude": list(cfg["exclude"]),
        # the extension re-derives junk from these, so both sides skip the same pages
        "junk": {k: JUNK_KINDS[k] for k in cfg.get("skip_kinds", []) if k in JUNK_KINDS},
    }


def ingest(url: str, title: str, text: str, dwell_ms: int = 0) -> dict:
    """Take a page the extension says you dwelled on and index it. The server's
    half of the policy check — the extension already filtered, this is the net
    under it. Returns {indexed: bool, reason: str}."""
    cfg = load()
    if not cfg["enabled"]:
        return {"indexed": False, "reason": "capture disabled"}
    term = excluded_by(url, cfg)
    if term:
        return {"indexed": False, "reason": f"excluded ({term})"}
    junk = junk_kind(url, cfg)
    if junk:
        return {"indexed": False, "reason": f"junk kind ({junk})"}
    if dwell_ms and dwell_ms < cfg["min_dwell_ms"]:
        return {"indexed": False, "reason": f"dwell {dwell_ms}ms below bar"}
    text = (text or "").strip()
    if len(text) < cfg["min_chars"]:
        return {"indexed": False, "reason": f"thin ({len(text)} chars)"}

    fresh = webindex.is_fresh(url, text, within_hours=cfg["recapture_after_h"])
    if fresh:
        return {"indexed": False, "reason": "already indexed, unchanged"}
    ok = webindex.index_page(url, title, text, kind="html", source="passive")
    return {"indexed": bool(ok), "reason": "" if ok else "index rejected"}


def status() -> dict:
    cfg = load()
    st = webindex.stats()
    st.update({
        "enabled": cfg["enabled"],
        "min_dwell_ms": cfg["min_dwell_ms"],
        "min_chars": cfg["min_chars"],
        "exclusions": len(cfg["exclude"]),
        "checked": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })
    return st


def set_enabled(on: bool) -> dict:
    cfg = load()
    cfg["enabled"] = bool(on)
    save(cfg)
    return cfg


def exclude_add(term: str) -> list[str]:
    cfg = load()
    term = term.strip().lower()
    if term and term not in cfg["exclude"]:
        cfg["exclude"].append(term)
        save(cfg)
    return cfg["exclude"]


def exclude_remove(term: str) -> tuple[list[str], bool]:
    """Drop an exclusion (i.e. start indexing it). Matches loosely so
    `tab capture include gmail` clears `mail.google.com` if that's the only hit."""
    cfg = load()
    term = term.strip().lower()
    hits = [t for t in cfg["exclude"] if t == term or term in t or t in term]
    if not hits:
        return cfg["exclude"], False
    cfg["exclude"] = [t for t in cfg["exclude"] if t not in hits]
    save(cfg)
    return cfg["exclude"], True
