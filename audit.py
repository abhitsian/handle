"""audit.py — append-only record of everything Handle DOES on your behalf.

The "authenticated = trusted" model is breaking: an agent acting in your session
uses your cookies, your tokens, and produces logs indistinguishable from you. The
minimum honest answer is attribution — a local, append-only trail that says
exactly what Handle did, when, to which page, and how it turned out, tagged as
the agent (not you). Reads are noise; we log ACTIONS (click/type/eval/scroll/
watch) and blocked/confirmed guard decisions.

One JSON object per line in audit.jsonl (gitignored). Never rewritten.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

_LOG = Path(__file__).resolve().parent / "audit.jsonl"

# Keys whose values we redact — never persist what was typed into a password /
# secret field to the on-disk trail.
_SECRET_HINT = ("password", "passwd", "secret", "token", "otp", "cvv", "ssn", "pin")


def _redact(method: str, params: dict) -> dict:
    p = dict(params or {})
    sel = (p.get("sel") or "") + " " + (p.get("intent") or "")
    if any(h in sel.lower() for h in _SECRET_HINT) and "text" in p:
        p["text"] = "«redacted»"
    # never log a raw eval body in full — it can be huge / sensitive
    if "expression" in p and isinstance(p["expression"], str) and len(p["expression"]) > 200:
        p["expression"] = p["expression"][:200] + "…"
    return p


def record(method: str, tab: dict | None, params: dict | None,
           result: str = "", risk: str = "", decision: str = "executed") -> None:
    """Append one action to the trail. `decision` ∈ executed | blocked | confirmed."""
    try:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "actor": "handle-agent",
            "method": method,
            "decision": decision,
            "risk": risk or "",
            "url": (tab or {}).get("url", ""),
            "title": ((tab or {}).get("title") or "")[:80],
            "handle": (tab or {}).get("id", ""),
            "params": _redact(method, params or {}),
            "result": (result or "")[:200],
        }
        with open(_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass  # auditing must never break the action


def tail(n: int = 25, method: str = "", decision: str = "") -> list[dict]:
    if not _LOG.exists():
        return []
    try:
        lines = _LOG.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []
    out = []
    for ln in reversed(lines):
        try:
            e = json.loads(ln)
        except Exception:
            continue
        if method and e.get("method") != method:
            continue
        if decision and e.get("decision") != decision:
            continue
        out.append(e)
        if len(out) >= n:
            break
    return out
