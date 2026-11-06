#!/usr/bin/env python3
"""In-memory bridge between Handle and its Chrome extension(s).

Each extension connects OUTBOUND to Handle's server (no inbound port on
Chrome): it pushes the current tabs/groups (`sync`) and long-polls for live
commands (`poll`), returning each result (`result`). The `tab` CLI issues a
live command through the server (`enqueue`) and blocks until the extension
fulfils it.

Devices: "" is the extension in this Mac's Chrome. A linked device (another
computer whose extension points at this server over the tailnet) has its own
name, its own command queue and its own heartbeat, so a command for one
browser is never picked up by another. All state is process memory; nothing
is persisted here. Pure stdlib.
"""
from __future__ import annotations

import threading
import time
import uuid

_cond = threading.Condition()
_queues: dict[str, list[dict]] = {}   # device -> commands awaiting its extension
_results: dict[str, dict] = {}        # command id -> result payload
_last_seen: dict[str, float] = {}     # device -> when its extension last synced/polled
_meta: dict[str, dict] = {}           # device -> last sync metadata (counts, version)
_last_poll: dict[str, float] = {}     # device -> when its command loop last asked for work


def _dev(device: str | None) -> str:
    return (device or "").strip()


def mark_sync(meta: dict | None = None, device: str = "") -> None:
    d = _dev(device)
    with _cond:
        _last_seen[d] = time.time()
        if meta is not None:
            _meta[d] = meta


# The extension long-polls /ext/poll (~25s) and re-syncs on a 30s alarm, so a
# heartbeat lands at least every ~25-30s. Keep the liveness TTL comfortably
# above that window or `tab ext` reads "not connected" between heartbeats.
_ALIVE_TTL = 60.0


def alive(ttl: float = _ALIVE_TTL, device: str = "") -> bool:
    """True if that device's extension pushed or polled within `ttl` seconds."""
    with _cond:
        return (time.time() - _last_seen.get(_dev(device), 0.0)) < ttl


def status(device: str = "") -> dict:
    d = _dev(device)
    with _cond:
        seen = _last_seen.get(d, 0.0)
        lp = _last_poll.get(d, 0.0)
        return {
            # syncs can arrive before the command loop starts (just after a reload),
            # so "connected" alone doesn't mean commands will run
            "polling": (time.time() - lp) < 40,
            "device": d or "this-mac",
            "connected": (time.time() - seen) < _ALIVE_TTL,
            "last_sync_ago": round(time.time() - seen, 1) if seen else None,
            "pending": len(_queues.get(d, [])),
            **_meta.get(d, {}),
        }


def devices() -> list[dict]:
    """Every device that has connected since the server started."""
    with _cond:
        names = sorted(set(_last_seen) | set(_meta))
    return [status(n) for n in names]


def enqueue(method: str, params: dict | None = None, timeout: float = 20.0,
            device: str = "") -> dict:
    """Queue a command for one device's extension and block until its result."""
    d = _dev(device)
    cmd_id = uuid.uuid4().hex[:12]
    cmd = {"id": cmd_id, "method": method, "params": params or {}}
    deadline = time.time() + timeout
    with _cond:
        q = _queues.setdefault(d, [])
        q.append(cmd)
        _cond.notify_all()
        while cmd_id not in _results:
            remaining = deadline - time.time()
            if remaining <= 0:
                q[:] = [c for c in q if c["id"] != cmd_id]
                return {"error": "extension did not respond in time"
                                 + (f" (device {d!r})" if d else "")}
            _cond.wait(remaining)
        return _results.pop(cmd_id)


def poll(wait: float = 25.0, device: str = "", heartbeat: bool = True) -> dict | None:
    """Long-poll used by an extension: return its next command, or None.

    The relay bridge polls on a linked browser's behalf, so it passes
    heartbeat=False: only that browser's own syncs prove it is alive."""
    d = _dev(device)
    if heartbeat:
        mark_sync(None, d)  # polling also counts as a heartbeat
        with _cond:
            _last_poll[d] = time.time()
    deadline = time.time() + wait
    with _cond:
        q = _queues.setdefault(d, [])
        while not q:
            remaining = deadline - time.time()
            if remaining <= 0:
                return None
            _cond.wait(remaining)
        return q.pop(0)


def deliver_result(cmd_id: str, result: dict, device: str = "") -> None:
    mark_sync(None, device)  # a posted result is definitive proof the extension is live
    with _cond:
        _results[cmd_id] = result
        _cond.notify_all()
