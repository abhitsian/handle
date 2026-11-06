#!/usr/bin/env python3
"""This Mac's side of the Handle relay.

A linked browser (on another computer) and this Mac both connect OUT to a small
Cloudflare Worker (relay/worker.js), so neither opens a port. Every message is
sealed with e2e.py using a key that is in the pairing code and never reaches
the Worker.

The bridge makes a linked browser look exactly like a local extension to the
rest of Handle:
  - up loop:  long-polls the relay for sealed envelopes from linked browsers
              ({device, kind, body}: sync | result | index | hello) and feeds
              them to collect.collect_from_ext / extbridge / capture.
  - down loop (one per device): takes that device's next command from extbridge
              (the same queue `tab --device <name>` writes to), seals it, and
              posts it to the relay for the browser to pick up.

Config (devices.json, gitignored): {"relay": {"url", "room", "token", "key"}}.
"""
from __future__ import annotations

import json
import secrets
import threading
import time
import urllib.request

import e2e

PREFIX = "handle1:"


def new_pairing(url: str) -> dict:
    return {"url": url.rstrip("/"),
            "room": e2e.b64u(secrets.token_bytes(16)),
            "token": e2e.b64u(secrets.token_bytes(24)),
            "key": e2e.b64u(secrets.token_bytes(32))}


def pairing_code(cfg: dict) -> str:
    """One string for the other browser's Options: url, room, token, key."""
    blob = json.dumps({"u": cfg["url"], "r": cfg["room"], "t": cfg["token"], "k": cfg["key"]},
                      separators=(",", ":")).encode()
    return PREFIX + e2e.b64u(blob)


def _req(cfg: dict, path: str, body: str | None = None, timeout: float = 35):
    req = urllib.request.Request(
        f"{cfg['url']}/r/{cfg['room']}/{path}",
        data=body.encode() if body is not None else None,
        method="POST" if body is not None else "GET",
        headers={"Authorization": "Bearer " + cfg["token"],
                 "Content-Type": "text/plain", "User-Agent": "handle-relay/1"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"null")


def claim(cfg: dict) -> None:
    _req(cfg, "claim", "", timeout=15)


class Bridge:
    def __init__(self, cfg: dict, on_sync, on_result, on_index, poll_cmd, log=print):
        self.cfg = cfg
        self.key = e2e.unb64u(cfg["key"])
        self.on_sync, self.on_result, self.on_index = on_sync, on_result, on_index
        self.poll_cmd = poll_cmd          # (device) -> cmd | None, blocks ~25s
        self.log = log
        self.down: set[str] = set()
        self.lock = threading.Lock()

    def start(self) -> None:
        threading.Thread(target=self._up_loop, daemon=True, name="relay-up").start()
        self.log(f"Handle  →  relay {self.cfg['url']} (end-to-end encrypted)")

    def _ensure_down(self, device: str) -> None:
        with self.lock:
            if device in self.down:
                return
            self.down.add(device)
        threading.Thread(target=self._down_loop, args=(device,), daemon=True,
                         name=f"relay-down-{device}").start()

    def _up_loop(self) -> None:
        import devices as dev_lib
        backoff = 1
        while True:
            try:
                msgs = _req(self.cfg, "up") or []
                backoff = 1
            except Exception as exc:
                self.log(f"relay up: {exc}; retrying in {backoff}s")
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
                continue
            for m in msgs:
                try:
                    env = json.loads(e2e.open_(self.key, m))
                except Exception:
                    continue  # not sealed with our key: drop silently
                dev = env.get("device", "")
                if not dev_lib.valid_name(dev):
                    continue
                kind, body = env.get("kind"), env.get("body") or {}
                try:
                    if kind == "sync":
                        self.on_sync(dev, body)
                    elif kind == "result":
                        self.on_result(dev, body)
                    elif kind == "index":
                        self.on_index(dev, body)
                    # "hello" (Options page test) needs nothing beyond the down loop
                except Exception as exc:
                    self.log(f"relay {kind} from {dev}: {exc}")
                self._ensure_down(dev)

    def _down_loop(self, device: str) -> None:
        while True:
            cmd = self.poll_cmd(device)
            if not cmd:
                continue
            sealed = e2e.seal(self.key, json.dumps(cmd).encode())
            for attempt in range(3):
                try:
                    _req(self.cfg, f"down/{device}", sealed, timeout=20)
                    break
                except Exception as exc:
                    self.log(f"relay down/{device}: {exc}")
                    time.sleep(1 + attempt)
