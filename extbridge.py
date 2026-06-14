#!/usr/bin/env python3
"""In-memory bridge between Handle and its Chrome extension.

The extension connects OUTBOUND to Handle's local server (no inbound port on
Chrome): it pushes the current tabs/groups (`sync`) and long-polls for live
commands (`poll`), returning each result (`result`). The `tab` CLI issues a
live command through the server (`enqueue`) and blocks until the extension
fulfils it. All state is process-memory in the running board server; nothing
is persisted here. Pure stdlib.
"""
from __future__ import annotations

import threading
import time
import uuid

_cond = threading.Condition()
_queue: list[dict] = []          # commands awaiting the extension
_results: dict[str, dict] = {}   # command id -> result payload
_last_sync = 0.0                 # when the extension last pushed tabs
_ext_meta: dict = {}             # last sync metadata (counts, version)


def mark_sync(meta: dict | None = None) -> None:
    global _last_sync, _ext_meta
    with _cond:
        _last_sync = time.time()
        _ext_meta = meta or {}


# The extension long-polls /ext/poll (~25s) and re-syncs on a 30s alarm, so a
# heartbeat lands at least every ~25-30s. Keep the liveness TTL comfortably
# above that window or `tab ext` reads "not connected" between heartbeats.
_ALIVE_TTL = 60.0


def alive(ttl: float = _ALIVE_TTL) -> bool:
    """True if the extension pushed or polled within `ttl` seconds."""
    with _cond:
        return (time.time() - _last_sync) < ttl


def status() -> dict:
    with _cond:
        return {
            "connected": (time.time() - _last_sync) < _ALIVE_TTL,
            "last_sync_ago": round(time.time() - _last_sync, 1) if _last_sync else None,
            "pending": len(_queue),
            **_ext_meta,
        }


def enqueue(method: str, params: dict | None = None, timeout: float = 20.0) -> dict:
    """Queue a command for the extension and block until its result (or time out)."""
    cmd_id = uuid.uuid4().hex[:12]
    cmd = {"id": cmd_id, "method": method, "params": params or {}}
    deadline = time.time() + timeout
    with _cond:
        _queue.append(cmd)
        _cond.notify_all()
        while cmd_id not in _results:
            remaining = deadline - time.time()
            if remaining <= 0:
                # give up; drop the command if still queued
                _queue[:] = [c for c in _queue if c["id"] != cmd_id]
                return {"error": "extension did not respond in time"}
            _cond.wait(remaining)
        return _results.pop(cmd_id)


def poll(wait: float = 25.0) -> dict | None:
    """Long-poll used by the extension: return the next command, or None."""
    # polling also counts as a heartbeat
    mark_sync(_ext_meta)
    deadline = time.time() + wait
    with _cond:
        while not _queue:
            remaining = deadline - time.time()
            if remaining <= 0:
                return None
            _cond.wait(remaining)
        return _queue.pop(0)


def deliver_result(cmd_id: str, result: dict) -> None:
    mark_sync(_ext_meta)  # a posted result is definitive proof the extension is live
    with _cond:
        _results[cmd_id] = result
        _cond.notify_all()
