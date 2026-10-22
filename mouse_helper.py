"""Moves and clicks the real macOS cursor for Handle's page autopilot.

Runs on 127.0.0.1:4911. POST /move {"x":..,"y":..,"click":bool} glides the
cursor to screen point (x, y) in global display points, then optionally
clicks. With "paste": text it then pastes that text (Cmd+V); with "clear": true
it selects all and deletes (Cmd+A, Delete). It never sends Return. Must be started from a process with Accessibility permission
(e.g. Terminal): `python3 mouse_helper.py`.
"""
import json
import math
import subprocess
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import Quartz

PORT = 4911


def _pos():
    p = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
    return p.x, p.y


def _post(kind, x, y):
    Quartz.CGEventPost(Quartz.kCGHIDEventTap,
                       Quartz.CGEventCreateMouseEvent(None, kind, (x, y), Quartz.kCGMouseButtonLeft))


def glide(x, y):
    x0, y0 = _pos()
    dist = math.hypot(x - x0, y - y0)
    steps = max(12, min(45, int(dist / 18)))
    dur = min(0.85, 0.25 + dist / 2200)
    for i in range(1, steps + 1):
        t = i / steps
        e = 1 - (1 - t) ** 3  # ease-out, like a hand settling on a target
        _post(Quartz.kCGEventMouseMoved, x0 + (x - x0) * e, y0 + (y - y0) * e)
        time.sleep(dur / steps)


def click(x, y):
    _post(Quartz.kCGEventLeftMouseDown, x, y)
    time.sleep(0.06)
    _post(Quartz.kCGEventLeftMouseUp, x, y)


def key(code, cmd=False):
    for down in (True, False):
        e = Quartz.CGEventCreateKeyboardEvent(None, code, down)
        if cmd:
            Quartz.CGEventSetFlags(e, Quartz.kCGEventFlagMaskCommand)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, e)
        time.sleep(0.03)


KEY_A, KEY_V, KEY_DELETE = 0, 9, 51


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self._send(200, {})

    def do_GET(self):
        self._send(200, {"ok": True, "pos": _pos()})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        a = json.loads(self.rfile.read(n) or b"{}")
        x, y = float(a["x"]), float(a["y"])
        glide(x, y)
        if a.get("click"):
            time.sleep(0.12)
            click(x, y)
        if a.get("paste"):
            if a.get("replace"):
                time.sleep(0.1)
                key(KEY_A, cmd=True)
            subprocess.run(["pbcopy"], input=a["paste"].encode(), check=True)
            time.sleep(0.25)
            key(KEY_V, cmd=True)
        if a.get("clear"):
            time.sleep(0.15)
            key(KEY_A, cmd=True)
            key(KEY_DELETE)
        self._send(200, {"ok": True, "pos": _pos()})


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
