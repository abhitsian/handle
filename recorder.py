#!/usr/bin/env python3
"""Workflow recorder — the capture half of the capability-recorder.

A recording is a passive trace of what you did across Chrome while a session is
open: every navigation, tab switch, and API call (XHR/Fetch/Document). The
extension streams those events here over CDP's Network + Page domains; this
module appends them, in order, to one session file. Nothing drives the page —
you do the task normally and the trace is the by-product.

Claude never sits in the capture loop. It enters later: the /record-workflow
skill reads `events.jsonl`, segments it into steps, and emits an SOP + a draft
skill. Capture is passive; analysis is on-demand — same split as sessions-mcp.

Storage (gitignored):
    recordings/<id>/meta.json     label, started/stopped, counts
    recordings/<id>/events.jsonl  one event per line, append-only

State is a single "active session" held in process memory. Events that arrive
with no active session are dropped (the extension may still be flushing after a
stop). Pure stdlib.
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
REC_DIR = APP_DIR / "recordings"

_lock = threading.Lock()
_active: dict | None = None        # {"id", "label", "started", "path", "count"}


def _slug(label: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", (label or "").strip().lower()).strip("-")
    return s[:40] or "workflow"


def _stamp() -> str:
    # local time, filename-safe; avoids importing datetime formatting elsewhere
    lt = time.localtime()
    return time.strftime("%Y%m%d-%H%M%S", lt)


def start(label: str = "") -> dict:
    """Begin a recording session. Replaces any session already active."""
    global _active
    with _lock:
        sid = f"{_stamp()}-{_slug(label)}"
        path = REC_DIR / sid
        path.mkdir(parents=True, exist_ok=True)
        meta = {
            "id": sid,
            "label": label or "",
            "started": time.time(),
            "stopped": None,
            "count": 0,
        }
        (path / "meta.json").write_text(json.dumps(meta, indent=2))
        (path / "events.jsonl").touch()
        _active = {**meta, "path": path}
        return {"id": sid, "label": meta["label"], "started": meta["started"]}


def append(events: list[dict]) -> int:
    """Append a batch of events to the active session. No-op if none active."""
    with _lock:
        if not _active or not events:
            return 0
        path: Path = _active["path"]
        with (path / "events.jsonl").open("a") as f:
            for ev in events:
                f.write(json.dumps(ev, separators=(",", ":")) + "\n")
        _active["count"] += len(events)
        return len(events)


def stop() -> dict | None:
    """Finalize the active session; write final counts to meta.json."""
    global _active
    with _lock:
        if not _active:
            return None
        path: Path = _active["path"]
        meta = json.loads((path / "meta.json").read_text())
        meta["stopped"] = time.time()
        meta["count"] = _active["count"]
        meta["duration_s"] = round(meta["stopped"] - meta["started"], 1)
        (path / "meta.json").write_text(json.dumps(meta, indent=2))
        done = {k: meta[k] for k in ("id", "label", "count", "duration_s")}
        _active = None
        return done


def status() -> dict:
    with _lock:
        if not _active:
            return {"recording": False}
        return {
            "recording": True,
            "id": _active["id"],
            "label": _active["label"],
            "count": _active["count"],
            "elapsed_s": round(time.time() - _active["started"], 1),
        }


def listing(limit: int = 20) -> list[dict]:
    """Recent recordings, newest first, from their meta.json files."""
    if not REC_DIR.exists():
        return []
    out = []
    for d in sorted(REC_DIR.iterdir(), reverse=True):
        mp = d / "meta.json"
        if not mp.is_file():
            continue
        try:
            m = json.loads(mp.read_text())
        except Exception:
            continue
        out.append({
            "id": m.get("id", d.name),
            "label": m.get("label", ""),
            "count": m.get("count", 0),
            "duration_s": m.get("duration_s"),
            "stopped": m.get("stopped"),
        })
        if len(out) >= limit:
            break
    return out


def path_for(rec_id: str = "") -> Path | None:
    """Resolve a recording id (or '' for the latest) to its folder."""
    if not REC_DIR.exists():
        return None
    if rec_id:
        p = REC_DIR / rec_id
        return p if p.is_dir() else None
    dirs = [d for d in sorted(REC_DIR.iterdir(), reverse=True)
            if (d / "events.jsonl").is_file()]
    return dirs[0] if dirs else None
