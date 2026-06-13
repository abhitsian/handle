#!/usr/bin/env python3
"""Initiative workspaces — park and resume tab sets per initiative.

A "workspace" is the set of Chrome tabs that belong to one initiative
(e.g. ``org-chart-conversations``). Parking snapshots those tabs to a
``_tabs.json`` file inside the initiative's folder under
``~/Work/_releases/<release>/<initiative>/`` — co-located with that
initiative's ``_discussion.md`` thread — and hands the URLs back so the
caller can close them in Chrome. Resuming reads the snapshot back.

The active initiatives are discovered live from the release tree, so this
stays in sync with ``/initiative`` and the ``_releases/`` convention — no
hardcoded list. A lightweight index (``workspaces.json``) records what's
currently parked so the board can offer "Resume" without globbing.

Pure standard library.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
INDEX_PATH = APP_DIR / "workspaces.json"
RELEASES = Path.home() / "Work" / "_releases"


def _load(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def _write(path, data) -> None:
    Path(path).write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def active_initiatives() -> list[str]:
    """Initiative slugs = subfolders of each release (skip _shared, _*.md)."""
    out: set[str] = set()
    if RELEASES.exists():
        for rel in RELEASES.iterdir():
            if not rel.is_dir():
                continue
            for init in rel.iterdir():
                if init.is_dir() and not init.name.startswith("_"):
                    out.add(init.name)
    return sorted(out)


_WS_FALLBACK = [
    "home-page", "browse", "canvas", "inbox", "notifications", "org-chart",
    "mobile", "generative-widgets", "persona-xp", "communications",
    "multi-instance",
]


def all_workstreams() -> list[str]:
    """Workstream slugs, read live from the `_active.md` table (backticked slug
    in the 2nd column). Falls back to the known list if the file is unreadable.
    """
    path = Path.home() / "Work" / "_workstreams" / "_active.md"
    slugs: list[str] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.lstrip().startswith("|"):
                continue
            cols = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cols) >= 2 and cols[1].startswith("`") and cols[1].endswith("`"):
                slug = cols[1].strip("`").strip()
                if slug:
                    slugs.append(slug)
    except Exception:
        pass
    return slugs or list(_WS_FALLBACK)


def _initiative_dir(slug: str) -> Path | None:
    if not RELEASES.exists():
        return None
    for rel in RELEASES.iterdir():
        if rel.is_dir() and (rel / slug).is_dir():
            return rel / slug
    return None


def park(slug: str, tabs: list[dict]) -> list[str]:
    """Snapshot ``tabs`` ([{url,title}, ...]) for ``slug``. Returns their URLs.

    Writes ``_tabs.json`` into the initiative folder when one exists, else
    falls back to the app dir. Updates the workspaces index either way.
    """
    snap = {
        "initiative": slug,
        "parked_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "tabs": [{"url": t["url"], "title": t.get("title", "")} for t in tabs],
    }
    d = _initiative_dir(slug)
    if d is not None:
        loc = d / "_tabs.json"
    else:
        loc = APP_DIR / f"_tabs_{slug}.json"
    _write(loc, snap)

    idx = _load(INDEX_PATH, {"workspaces": {}})
    idx.setdefault("workspaces", {})[slug] = {
        "path": str(loc),
        "count": len(snap["tabs"]),
        "parked_at": snap["parked_at"],
    }
    _write(INDEX_PATH, idx)
    return [t["url"] for t in snap["tabs"]]


def list_parked() -> dict:
    """Return {slug: {path, count, parked_at}} for currently parked sets."""
    return _load(INDEX_PATH, {"workspaces": {}}).get("workspaces", {})


def resume(slug: str) -> list[str]:
    """Return the parked URLs for ``slug`` (empty if none), then forget it."""
    idx = _load(INDEX_PATH, {"workspaces": {}})
    rec = idx.get("workspaces", {}).get(slug)
    if not rec:
        return []
    snap = _load(rec["path"], {})
    urls = [t["url"] for t in snap.get("tabs", []) if t.get("url")]
    # parking is consumed on resume; the snapshot file stays for history
    idx["workspaces"].pop(slug, None)
    _write(INDEX_PATH, idx)
    return urls
