#!/usr/bin/env python3
"""Linked devices: other computers whose Handle extension syncs to this server.

The other computer needs only the Chrome extension. In its options page you
set this server's address (its Tailscale IP, port 4910), a device name, and
the pairing token from `tab devices pair`. Its extension then pushes its tabs
here and long-polls for commands exactly like the local one, under its own
name, and `tab --device <name> ...` reads and acts on that browser.

Security model: the server always listens on 127.0.0.1. It listens on another
address only after pairing, and every request from a non-loopback address must
carry the token (header X-Handle-Token) and may only reach the extension
endpoints. Commands (`/ext/cmd`) can only be issued from this Mac.

Config lives in devices.json (gitignored): {"token": "...", "listen": ["100.x.y.z"]}.
"""
from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import subprocess
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
CONFIG = Path(os.environ.get("HANDLE_DEVICES_CONFIG", str(APP_DIR / "devices.json")))
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")

# What a linked device's extension may call. Everything else is this-Mac only.
REMOTE_PATHS = {"/ext/sync", "/ext/poll", "/ext/result", "/ext/policy",
                "/ext/index", "/ext/hello"}


def load() -> dict:
    try:
        d = json.loads(CONFIG.read_text())
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save(cfg: dict) -> None:
    CONFIG.write_text(json.dumps(cfg, indent=2))
    try:
        CONFIG.chmod(0o600)
    except Exception:
        pass


def valid_name(name: str) -> bool:
    return bool(NAME_RE.match(name or ""))


def tailscale_ip() -> str:
    for exe in ("tailscale", "/opt/homebrew/bin/tailscale", "/usr/local/bin/tailscale"):
        try:
            out = subprocess.run([exe, "ip", "-4"], capture_output=True, text=True, timeout=4)
            ip = out.stdout.strip().splitlines()[0] if out.returncode == 0 and out.stdout.strip() else ""
            if ip:
                return ip
        except Exception:
            continue
    return ""


def pair(listen: str = "") -> dict:
    """Create the token (once) and choose the address to listen on."""
    cfg = load()
    cfg.setdefault("token", secrets.token_urlsafe(24))
    ip = listen or tailscale_ip()
    if not ip:
        raise RuntimeError("No Tailscale IP found. Start Tailscale on both computers, "
                           "or pass --listen <this Mac's LAN IP>.")
    if ip in ("0.0.0.0", "::"):
        raise RuntimeError("Refusing to listen on every interface; give one address.")
    cfg["listen"] = [ip]
    save(cfg)
    return cfg


def unpair() -> None:
    cfg = load()
    cfg.pop("listen", None)
    cfg.pop("token", None)
    save(cfg)


def listen_addrs() -> list[str]:
    cfg = load()
    return [a for a in cfg.get("listen", []) if a and a not in ("0.0.0.0", "::")] if cfg.get("token") else []


def token_ok(given: str | None) -> bool:
    tok = load().get("token")
    return bool(tok and given and hmac.compare_digest(tok, given))
