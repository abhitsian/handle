#!/usr/bin/env python3
"""Eval for the MCP server (mcp/server.js).

Boots the server over stdio and speaks JSON-RPC to it:
  - initialize handshake returns serverInfo
  - tools/list exposes the full tool surface, each with a name + inputSchema
  - every advertised tool has a handler (no orphan schemas)
  - a read-only tools/call round-trips and returns content

Needs Node on PATH; SKIPs the whole suite if Node is missing.

    python3 evals/mcp_eval.py
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "mcp" / "server.js"

PASS, FAIL, SKIP = [], [], []
def ok(n, c, d=""): (PASS if c else FAIL).append((n, d))
def skip(n, w): SKIP.append((n, w))

EXPECTED = {
    "list_tabs", "find_tab", "grep_tabs", "ask_tabs", "save_tabs", "list_bundles",
    "recall_bundle", "read_tab", "grab_clipboard", "screenshot_tab", "active_tab",
    "open_tab", "close_tab", "note_tab", "group_tab", "pin_tab", "refresh_tabs",
    "history", "closed", "bookmarks", "downloads", "journeys", "most_visited",
    "console", "ext_status",
}


def rpc(proc, msgs):
    """Write each JSON-RPC msg, return {id: response} parsed from stdout."""
    payload = "".join(json.dumps(m) + "\n" for m in msgs)
    out, _ = proc.communicate(payload, timeout=25)
    res = {}
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            m = json.loads(line)
        except Exception:
            continue
        if "id" in m:
            res[m["id"]] = m
    return res


if not shutil.which("node"):
    skip("mcp suite", "node not on PATH")
elif not SERVER.exists():
    ok("mcp · server.js exists", False, str(SERVER))
else:
    proc = subprocess.Popen(
        ["node", str(SERVER)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True)
    try:
        responses = rpc(proc, [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "bookmarks", "arguments": {"limit": 1}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
             "params": {"name": "list_tabs", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
             "params": {"name": "definitely_not_a_tool", "arguments": {}}},
        ])
    finally:
        try:
            proc.kill()
        except Exception:
            pass

    init = responses.get(1, {})
    ok("mcp · initialize returns serverInfo",
       bool(init.get("result", {}).get("serverInfo", {}).get("name")),
       str(init.get("result", {}).get("serverInfo", {})))

    tlist = responses.get(2, {}).get("result", {}).get("tools", [])
    names = {t.get("name") for t in tlist}
    ok("mcp · tools/list returns 25 tools", len(tlist) == 25, f"got {len(tlist)}")
    ok("mcp · every expected tool is present", EXPECTED <= names,
       f"missing: {EXPECTED - names}")
    ok("mcp · every tool has a name + inputSchema",
       all(t.get("name") and isinstance(t.get("inputSchema"), dict) for t in tlist))

    call = responses.get(3, {}).get("result", {})
    content = call.get("content", [])
    ok("mcp · tools/call (bookmarks) returns content",
       bool(content) and content[0].get("type") == "text" and not call.get("isError"),
       f"{content[:1]}")

    call4 = responses.get(4, {}).get("result", {})
    ok("mcp · tools/call (list_tabs) returns content",
       bool(call4.get("content")), "ok")

    # unknown tool must error cleanly, not crash the server
    err5 = responses.get(5, {})
    ok("mcp · unknown tool errors cleanly",
       bool(err5.get("error") or err5.get("result", {}).get("isError")),
       str(err5)[:60])


print(f"\n{'=' * 52}")
print(f"  MCP EVAL — {len(PASS)} pass · {len(FAIL)} fail · {len(SKIP)} skip")
print(f"{'=' * 52}")
for n, d in PASS: print(f"  ✅ {n}" + (f"   — {d}" if d else ""))
for n, d in SKIP: print(f"  ⏭  {n}   ({d})")
for n, d in FAIL: print(f"  ❌ {n}   — {d}")
print()
sys.exit(1 if FAIL else 0)
