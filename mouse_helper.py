"""Moves and clicks the real macOS cursor for Handle's page autopilot.

Runs on 127.0.0.1:4911. POST /move {"x":..,"y":..,"click":bool} glides the
cursor to screen point (x, y) in global display points, then optionally
clicks. With "paste": text it then pastes that text (Cmd+V); with "clear": true
it selects all and deletes (Cmd+A, Delete); with "scroll": n it turns the
wheel n lines (positive = down). It never sends Return. Must be started from a process with Accessibility permission
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
    e = Quartz.CGEventCreateMouseEvent(None, kind, (x, y), Quartz.kCGMouseButtonLeft)
    Quartz.CGEventSetFlags(e, 0)  # no modifiers: a stray Command flag turns a click into "open in new tab"
    if kind in (Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp):
        Quartz.CGEventSetIntegerValueField(e, Quartz.kCGMouseEventClickState, 1)  # a single click, as macOS sends it
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, e)


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


KEY_CMD = 55


def _key_event(code, down, flags):
    e = Quartz.CGEventCreateKeyboardEvent(None, code, down)
    Quartz.CGEventSetFlags(e, flags)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, e)
    time.sleep(0.03)


def key(code, cmd=False):
    # press and release Command as a real key, so the system never thinks it is still held
    cmdf = Quartz.kCGEventFlagMaskCommand if cmd else 0
    if cmd:
        _key_event(KEY_CMD, True, cmdf)
    _key_event(code, True, cmdf)
    _key_event(code, False, cmdf)
    if cmd:
        _key_event(KEY_CMD, False, 0)


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
        if a.get("scroll"):
            # real wheel ticks, a few at a time like a hand on a mouse; positive scrolls down
            lines = int(a["scroll"])
            for _ in range(abs(lines)):
                e = Quartz.CGEventCreateScrollWheelEvent(None, Quartz.kCGScrollEventUnitLine, 1, -1 if lines > 0 else 1)
                Quartz.CGEventPost(Quartz.kCGHIDEventTap, e)
                time.sleep(0.03)
            self._send(200, {"ok": True, "pos": _pos()})
            return
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
