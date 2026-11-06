// Handle relay: a mailbox between this Mac's Handle and a linked browser.
//
// Both sides connect OUT to this Worker, so neither computer opens a port.
// Everything that passes through is encrypted on each end with a key the
// Worker never sees (see e2e.py / background.js); the Worker only moves
// opaque strings between two queues per room:
//   up           linked browsers -> the Mac   (tab syncs, command results)
//   down/<dev>   the Mac -> one linked browser (commands)
//
// A room is claimed once, by the Mac at pairing time, which stores the hash of
// its bearer token. Every later request must present the same token.

const ROOM_RE = /^\/r\/([A-Za-z0-9_-]{22,64})\/(claim|up|down\/([a-z0-9][a-z0-9-]{0,31}))$/;
const WAIT_MS = 25000;
const MAX_QUEUE = 200;               // messages per queue; oldest dropped first
const MAX_BODY = 20 * 1024 * 1024;   // one envelope (a screenshot fits)

export default {
  async fetch(req, env) {
    const url = new URL(req.url);
    const m = url.pathname.match(ROOM_RE);
    if (!m) return new Response("handle relay\n", { status: 404 });
    const stub = env.ROOM.get(env.ROOM.idFromName(m[1]));
    return stub.fetch(req);
  },
};

async function sha256hex(s) {
  const d = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(s));
  return [...new Uint8Array(d)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

function timingSafeEq(a, b) {
  if (a.length !== b.length) return false;
  let r = 0;
  for (let i = 0; i < a.length; i++) r |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return r === 0;
}

export class Room {
  constructor(state) {
    this.state = state;
    this.queues = {};   // key -> [string]
    this.waiters = {};  // key -> [resolve]
    this.tokenHash = undefined;
  }

  async fetch(req) {
    const url = new URL(req.url);
    const [, , action, device] = url.pathname.match(ROOM_RE);
    const token = (req.headers.get("authorization") || "").replace(/^Bearer\s+/i, "");
    if (!token || token.length < 20) return new Response("token required", { status: 401 });
    const h = await sha256hex(token);
    if (this.tokenHash === undefined) this.tokenHash = (await this.state.storage.get("th")) || null;

    if (action === "claim") {
      if (this.tokenHash && !timingSafeEq(h, this.tokenHash)) return new Response("taken", { status: 409 });
      await this.state.storage.put("th", h);
      this.tokenHash = h;
      return Response.json({ ok: true });
    }
    if (!this.tokenHash || !timingSafeEq(h, this.tokenHash)) return new Response("forbidden", { status: 403 });

    const key = action === "up" ? "up" : "down:" + device;
    if (req.method === "POST") {
      const len = Number(req.headers.get("content-length") || 0);
      if (len > MAX_BODY) return new Response("too large", { status: 413 });
      const body = await req.text();
      if (body.length > MAX_BODY) return new Response("too large", { status: 413 });
      this.push(key, body);
      return Response.json({ ok: true });
    }
    return Response.json(await this.take(key));
  }

  push(key, msg) {
    const w = this.waiters[key];
    if (w && w.length) { w.shift()([msg]); return; }
    const q = (this.queues[key] ||= []);
    q.push(msg);
    if (q.length > MAX_QUEUE) q.splice(0, q.length - MAX_QUEUE);
  }

  take(key) {
    const q = this.queues[key];
    if (q && q.length) { this.queues[key] = []; return q; }
    return new Promise((resolve) => {
      const w = (this.waiters[key] ||= []);
      let done = false;
      const finish = (v) => { if (!done) { done = true; resolve(v); } };
      w.push(finish);
      setTimeout(() => {
        const i = w.indexOf(finish);
        if (i >= 0) w.splice(i, 1);
        finish([]);
      }, WAIT_MS);
    });
  }
}
