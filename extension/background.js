// Handle companion extension — connects OUTBOUND to Handle's local server.
//
// Two jobs:
//   1. push the current tabs + native tab groups to /ext/sync (replaces the
//      AppleScript scan: no Automation permission, includes real groups)
//   2. long-poll /ext/poll for live commands (read / screenshot / console /
//      eval / click / type) and return each result to /ext/result
//
// Mostly read (content, screenshots, console). It can also act on a tab —
// click and type — when asked, for monitor/automation flows. Click/type run a
// real injected function in the isolated world so they survive page CSP. It
// talks to nothing but 127.0.0.1:4910 and never navigates on its own.

const HANDLE = "http://127.0.0.1:4910";
const VERSION = "1.9.0";

// Never let a debugger op hang forever (e.g. attach stalls because DevTools or
// another CDP client already holds the tab). Reject fast so the caller gets a
// clean error and Handle falls back, instead of the CLI waiting out its timeout.
function withTimeout(promise, ms, label) {
  return Promise.race([
    promise,
    new Promise((_, rej) => setTimeout(() => rej(new Error(label + " timed out after " + ms + "ms — is DevTools or another debugger attached to this tab?")), ms)),
  ]);
}

// ---- push tabs + groups ---------------------------------------------------
async function syncTabs() {
  let tabs, groups = [];
  try {
    tabs = await chrome.tabs.query({});
    if (chrome.tabGroups) groups = await chrome.tabGroups.query({});
  } catch (e) {
    return;
  }
  const groupById = {};
  for (const g of groups) groupById[g.id] = g;
  const payload = tabs
    .filter((t) => t.url && /^https?:|^file:/.test(t.url))
    .map((t) => {
      const g = t.groupId != null && t.groupId !== -1 ? groupById[t.groupId] : null;
      return {
        tab_id: t.id,
        url: t.url,
        title: t.title || "",
        window: t.windowId,
        index: t.index,
        pinned: !!t.pinned,
        active: !!t.active,
        group: g ? (g.title || "(unnamed group)") : "",
        group_color: g ? g.color : "",
      };
    });
  try {
    await fetch(`${HANDLE}/ext/sync`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tabs: payload, version: VERSION }),
    });
  } catch (e) {
    /* server not running — fine, the CLI falls back to AppleScript */
  }
}

// ---- live command handlers ------------------------------------------------
// Chrome discards stale background tabs to save memory; script can't inject
// into a discarded tab (it fails with a misleading host-permission error).
// Wake it — reload and wait for load — before any scripting call.
async function wakeTab(tabId) {
  let tab;
  try { tab = await chrome.tabs.get(tabId); } catch (e) { return; }
  if (!tab.discarded && tab.status === "complete") return;
  await new Promise((resolve) => {
    let settled = false;
    const finish = () => {
      if (settled) return;
      settled = true;
      chrome.tabs.onUpdated.removeListener(done);
      resolve();
    };
    const done = (id, info) => { if (id === tabId && info.status === "complete") finish(); };
    chrome.tabs.onUpdated.addListener(done);
    if (tab.discarded) chrome.tabs.reload(tabId).catch(() => {});
    setTimeout(finish, 12000);  // SharePoint can redirect through auth; cap the wait
  });
}

async function readTab(tabId) {
  await wakeTab(tabId);
  const [res] = await chrome.scripting.executeScript({
    target: { tabId },
    func: () => ({
      title: document.title,
      url: location.href,
      text: document.body ? document.body.innerText : "",
    }),
  });
  return res.result;
}

async function evalIn(tabId, expression) {
  await wakeTab(tabId);
  const [res] = await chrome.scripting.executeScript({
    target: { tabId },
    world: "MAIN",
    func: (e) => {
      try { return String(eval(e)); } catch (err) { return "ERR: " + err; }
    },
    args: [expression],
  });
  return { value: res.result };
}

// click / type run a REAL injected function in the default (isolated) world, so
// they work even on pages whose CSP blocks string-eval (Okta, banks, etc.).
// Both pierce shadow DOM (modern apps like Teams put inputs in shadow roots that
// document.querySelector can't see) via a deep element walk.
// nth (1-based, document order) and near ("A.Y. 2025-26") pick one element when
// several match — e.g. a "Download JSON" button repeated on every card. `near`
// keeps the matches whose closest ancestor containing that text is smallest, so
// the button inside the card that says "A.Y. 2025-26" wins over the other cards'.
async function clickIn(tabId, sel, text, nth, near) {
  await wakeTab(tabId);
  const [res] = await chrome.scripting.executeScript({
    target: { tabId },
    func: (sel, text, nth, near) => {
      const deep = (root, acc) => {
        let kids; try { kids = root.querySelectorAll("*"); } catch (e) { return acc; }
        for (const el of kids) { acc.push(el); if (el.shadowRoot) deep(el.shadowRoot, acc); }
        return acc;
      };
      const all = deep(document, []);
      let matches;
      if (text) {
        const q = (text + "").toLowerCase();
        const ROLES = ["button", "option", "menuitem", "menuitemradio", "menuitemcheckbox", "link", "tab", "treeitem", "listitem"];
        const clickable = all.filter((e) => /^(BUTTON|A)$/.test(e.tagName) || (e.getAttribute && (ROLES.includes(e.getAttribute("role")) || e.hasAttribute("onclick") || e.hasAttribute("tabindex"))) || /^(submit|button)$/.test(e.type || ""));
        const hit = (e) => ((e.innerText || e.value || e.getAttribute("aria-label") || "") + "").trim().toLowerCase().indexOf(q) >= 0;
        matches = clickable.filter(hit);
        // drop a match that merely wraps another match (keep the most specific node)
        matches = matches.filter((m) => !matches.some((o) => o !== m && m.contains(o)));
        // without nth/near, prefer the smallest (most specific) node, as before
        if (!nth && !near) matches.sort((a, b) => (a.innerText || "").length - (b.innerText || "").length);
      } else {
        matches = all.filter((e) => { try { return e.matches(sel); } catch (x) { return false; } });
      }
      if (near && matches.length > 1) {
        const n = (near + "").toLowerCase();
        const scope = (m) => {
          for (let e = m; e; e = e.parentElement) {
            const t = (e.innerText || "").toLowerCase();
            if (t.indexOf(n) >= 0) return t.length;
          }
          return Infinity;
        };
        const scored = matches.map((m) => [m, scope(m)]);
        const best = Math.min(...scored.map((x) => x[1]));
        matches = best === Infinity ? [] : scored.filter((x) => x[1] === best).map((x) => x[0]);
      }
      const total = matches.length;
      const el = nth ? matches[nth - 1] : matches[0];
      if (!el) return total && nth ? { miss: "only " + total + " match(es); nth=" + nth + " is out of range" } : "NOT_FOUND";
      // a selector that matches exactly this element: grow the path until unique
      const cssPath = (node) => {
        if (!node || node.nodeType !== 1) return "";
        const parts = []; let e = node;
        while (e && e.nodeType === 1 && parts.length < 25) {
          if (e.id) { parts.unshift("#" + CSS.escape(e.id)); }
          else {
            let s = e.tagName.toLowerCase();
            const p = e.parentElement;
            if (p) {
              const sib = Array.prototype.filter.call(p.children, (c) => c.tagName === e.tagName);
              if (sib.length > 1) s += ":nth-of-type(" + (sib.indexOf(e) + 1) + ")";
            }
            parts.unshift(s);
          }
          const path = parts.join(" > ");
          try { if (document.querySelectorAll(path).length === 1) return path; } catch (x) {}
          if (e.id) break;
          e = e.parentElement;
        }
        return parts.join(" > ");
      };
      el.scrollIntoView({ block: "center" });
      el.click();
      const label = ((el.innerText || el.value || el.getAttribute("aria-label") || el.tagName) + "").trim().slice(0, 60);
      const of = total > 1 ? "  (" + (matches.indexOf(el) + 1) + " of " + total + " matches — pass nth/near to pick another)" : "";
      return { r: "clicked: " + label + of, sel: cssPath(el) };
    },
    args: [sel || null, text || null, nth || null, near || null],
  });
  const out = res.result;
  if (out === "NOT_FOUND" || !out) return { value: "NOT_FOUND" };
  if (out.miss) return { error: out.miss };
  return { value: out.r, selector: out.sel };
}

// resolve — rank candidate elements for an INTENT (text / role / aria) without
// clicking. Used for self-healing: when a cached selector breaks, re-find the
// element by what it MEANS, and let the caller (or agent) pick.
async function resolveEls(tabId, intent) {
  await wakeTab(tabId);
  const [res] = await chrome.scripting.executeScript({
    target: { tabId },
    func: (intent) => {
      const q = (intent + "").toLowerCase().trim();
      const deep = (root, acc) => {
        let kids; try { kids = root.querySelectorAll("*"); } catch (e) { return acc; }
        for (const el of kids) { acc.push(el); if (el.shadowRoot) deep(el.shadowRoot, acc); }
        return acc;
      };
      const cssPath = (node) => {
        if (!node || node.nodeType !== 1) return "";
        const parts = []; let e = node;
        while (e && e.nodeType === 1 && parts.length < 25) {
          if (e.id) { parts.unshift("#" + CSS.escape(e.id)); }
          else {
            let s = e.tagName.toLowerCase();
            const p = e.parentElement;
            if (p) {
              const sib = Array.prototype.filter.call(p.children, (c) => c.tagName === e.tagName);
              if (sib.length > 1) s += ":nth-of-type(" + (sib.indexOf(e) + 1) + ")";
            }
            parts.unshift(s);
          }
          const path = parts.join(" > ");
          try { if (document.querySelectorAll(path).length === 1) return path; } catch (x) {}
          if (e.id) break;
          e = e.parentElement;
        }
        return parts.join(" > ");
      };
      const ROLES = ["button", "option", "menuitem", "menuitemradio", "menuitemcheckbox", "link", "tab", "treeitem", "listitem", "checkbox", "radio"];
      const label = (e) => ((e.innerText || e.value || e.getAttribute("aria-label") || e.getAttribute("title") || "") + "").trim();
      const clickable = deep(document, []).filter((e) =>
        /^(BUTTON|A)$/.test(e.tagName) ||
        (e.getAttribute && (ROLES.includes(e.getAttribute("role")) || e.hasAttribute("onclick") || e.hasAttribute("tabindex"))) ||
        /^(submit|button)$/.test(e.type || ""));
      const scored = clickable.map((e) => {
        const t = label(e).toLowerCase();
        let s = 0;
        if (t === q) s = 100; else if (t.indexOf(q) >= 0) s = 60 - Math.min(40, t.length - q.length);
        else { const qw = q.split(/\s+/), n = qw.filter((w) => w && t.indexOf(w) >= 0).length; s = n ? 30 * n / qw.length : 0; }
        return { e, s, label: label(e).slice(0, 60), role: e.getAttribute && e.getAttribute("role") || e.tagName.toLowerCase() };
      }).filter((x) => x.s > 0).sort((a, b) => b.s - a.s).slice(0, 6);
      return scored.map((x) => ({ label: x.label, role: x.role, score: Math.round(x.s), selector: cssPath(x.e) }));
    },
    args: [intent || ""],
  });
  return { value: res.result || [] };
}

async function typeIn(tabId, sel, text, submit) {
  await wakeTab(tabId);
  const [res] = await chrome.scripting.executeScript({
    target: { tabId },
    func: (sel, text, submit) => {
      const deep = (root, acc) => {
        let kids; try { kids = root.querySelectorAll("*"); } catch (e) { return acc; }
        for (const el of kids) { acc.push(el); if (el.shadowRoot) deep(el.shadowRoot, acc); }
        return acc;
      };
      const all = deep(document, []);
      const isCE = (e) => e && (e.isContentEditable || (e.getAttribute && e.getAttribute("contenteditable") === "true"));
      const lbl = (e) => ((e.getAttribute && (e.getAttribute("aria-label") || e.getAttribute("data-placeholder") || e.getAttribute("placeholder"))) || e.placeholder || "");
      let el = null;
      if (sel) el = document.querySelector(sel) || all.find((e) => { try { return e.matches(sel); } catch (x) { return false; } });
      if (!el) {
        // heuristic: a message/Copilot composer — prefer a labelled editable
        el = all.find((e) => isCE(e) && /copilot|message|ask|reply|compose/i.test(lbl(e)))
          || all.find((e) => isCE(e))
          || all.find((e) => /^(TEXTAREA|INPUT)$/.test(e.tagName) && /copilot|message|ask/i.test(lbl(e) + ""));
      }
      if (!el) return "NOT_FOUND";
      el.focus();
      if (isCE(el)) {
        try { const s = window.getSelection(); s.removeAllRanges(); const r = document.createRange(); r.selectNodeContents(el); s.addRange(r); } catch (e) {}
        let ok = false; try { ok = document.execCommand("insertText", false, text); } catch (e) {}
        if (!ok || !(el.innerText || "").trim()) {
          el.textContent = text;
          el.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: text }));
        }
      } else {
        el.value = text;
        el.dispatchEvent(new Event("input", { bubbles: true }));
        el.dispatchEvent(new Event("change", { bubbles: true }));
      }
      let sent = false;
      if (submit) {
        const fire = (t) => el.dispatchEvent(new KeyboardEvent(t, { key: "Enter", code: "Enter", keyCode: 13, which: 13, bubbles: true, cancelable: true }));
        fire("keydown"); fire("keypress"); fire("keyup"); sent = true;
      }
      return "typed" + (isCE(el) ? "(ce)" : "") + (sent ? "+enter" : "") + ": " + ((el.innerText || el.value || "") + "").trim().slice(0, 40);
    },
    args: [sel || null, text, !!submit],
  });
  return { value: res.result };
}

// Scroll a tab from the isolated world (a real injected function, so it works
// on CSP-strict pages like X/Okta where MAIN-world `eval` is refused). Scrolls
// the window, and if the window doesn't move, nudges the tallest inner
// scrollable (apps that scroll a container, not the page). Returns position so
// the caller can detect the end of an infinite feed (height stops growing).
async function scrollIn(tabId, dy) {
  await wakeTab(tabId);
  const [res] = await chrome.scripting.executeScript({
    target: { tabId },
    func: (dy) => {
      const pos = () => (document.scrollingElement || document.documentElement).scrollTop;
      const before = pos();
      window.scrollBy(0, dy);
      if (pos() === before) {
        let best = null, bestH = 0;
        for (const el of document.querySelectorAll("div,main,section,ul")) {
          if (el.scrollHeight > el.clientHeight + 200 && el.scrollHeight > bestH) { best = el; bestH = el.scrollHeight; }
        }
        if (best) best.scrollTop += dy;
      }
      const s = document.scrollingElement || document.documentElement;
      return { y: s.scrollTop, h: s.scrollHeight, vh: window.innerHeight };
    },
    args: [dy],
  });
  return res.result;
}

// harvest — CSP-safe structured DOM reader. Runs in the ISOLATED world (a real
// function, never string-eval) so it works on strict-CSP pages like Zoom/Okta
// where `eval`/`read` are refused. Given a row selector and a {field: subsel}
// map, returns one object per matching row: {field: innerText}. A field whose
// subsel is "" (or ".") reads the row element's own text. Also returns the raw
// row count so callers can probe for presence without pulling data.
async function harvestIn(tabId, sel, fields, limit) {
  await wakeTab(tabId);
  const [res] = await chrome.scripting.executeScript({
    target: { tabId },
    func: (sel, fields, limit) => {
      try {
        const rows = Array.prototype.slice.call(document.querySelectorAll(sel));
        const cap = limit && limit > 0 ? limit : rows.length;
        const keys = Object.keys(fields || {});
        const out = rows.slice(0, cap).map((el) => {
          if (!keys.length) return { text: (el.innerText || "").trim() };
          const o = {};
          for (const k of keys) {
            // "sel", "sel@href", "@datetime": an attribute after @, the element itself when the selector is empty.
            const spec = fields[k] || "";
            const at = spec.lastIndexOf("@");
            const sub = at > -1 ? spec.slice(0, at) : spec;
            const attr = at > -1 ? spec.slice(at + 1) : "";
            const n = !sub || sub === "." ? el : el.querySelector(sub);
            if (!n) { o[k] = ""; continue; }
            if (!attr || attr === "text") o[k] = (n.innerText || n.value || "").trim();
            else if (attr === "html") o[k] = n.innerHTML;
            // href and src read off the property, which resolves relative links to absolute ones.
            else if ((attr === "href" || attr === "src") && n[attr]) o[k] = String(n[attr]);
            else o[k] = String(n.getAttribute(attr) ?? n[attr] ?? "").trim();
          }
          return o;
        });
        return JSON.stringify({ count: rows.length, rows: out });
      } catch (e) {
        return "ERR: " + (e && e.message ? e.message : e);
      }
    },
    args: [sel || "", fields || {}, limit || 0],
  });
  const out = res && res.result;
  if (typeof out === "string" && out.indexOf("ERR: ") === 0) return { value: out };
  return { value: out };
}

// Attach cleanly: if a stale attachment is lingering (e.g. a prior dispatch
// died before its finally{} could detach), clear it and retry once.
async function attachClean(target) {
  try {
    await chrome.debugger.attach(target, "1.3");
  } catch (e) {
    try { await chrome.debugger.detach(target); } catch (_) {}
    await chrome.debugger.attach(target, "1.3");
  }
}

async function screenshot(tabId) {
  await attachClean({ tabId });
  try {
    await chrome.debugger.sendCommand({ tabId }, "Page.enable");
    // viewport capture — captureBeyondViewport errors (-32603) on background tabs
    const { data } = await chrome.debugger.sendCommand(
      { tabId }, "Page.captureScreenshot", { format: "png" });
    return { data }; // base64 PNG
  } finally {
    try { await chrome.debugger.detach({ tabId }); } catch (e) {}
  }
}

async function consoleLogs(tabId, ms = 1800) {
  await attachClean({ tabId });
  const logs = [];
  const onEvent = (src, method, params) => {
    if (src.tabId !== tabId) return;
    if (method === "Runtime.consoleAPICalled") {
      logs.push({ level: params.type,
        text: (params.args || []).map((a) => a.value ?? a.description ?? "").join(" ") });
    } else if (method === "Runtime.exceptionThrown") {
      const d = params.exceptionDetails || {};
      logs.push({ level: "error",
        text: (d.text || "") + " " + (d.exception?.description || "") });
    } else if (method === "Log.entryAdded") {
      logs.push({ level: params.entry.level, text: params.entry.text });
    }
  };
  chrome.debugger.onEvent.addListener(onEvent);
  try {
    await chrome.debugger.sendCommand({ tabId }, "Runtime.enable");
    await chrome.debugger.sendCommand({ tabId }, "Log.enable");
    await new Promise((r) => setTimeout(r, ms));
    return { logs, note: "captured after attach (live window); reload the tab to catch load-time errors" };
  } finally {
    chrome.debugger.onEvent.removeListener(onEvent);
    try { await chrome.debugger.detach({ tabId }); } catch (e) {}
  }
}

// ---- workflow recording ---------------------------------------------------
// Passive capture of a task you do once, in two decoupled layers:
//   • skeleton — navigations + tab switches, from reliable WebExtension events
//     (tabs.onUpdated / onActivated / webNavigation). Never races, never needs
//     the debugger. This alone segments the workflow into steps.
//   • enrichment — the API calls (XHR/Fetch/Document) inside each step, from the
//     CDP Network domain. Best-effort: if the debugger attaches a beat late and
//     misses a page's load, the skeleton still has the step.
// The write calls (POST/PUT/PATCH/DELETE) are the actions that matter; the
// skeleton tells you where they happened. Handle persists it; Claude reads it
// later (/record-workflow). Nothing drives the page.
//
// MV3 note: the service worker is suspended between events, so its in-memory
// state cannot be trusted across two events. Two rules make capture survive it:
//   1. The "am I recording" flag and the attached-tab set live in
//      chrome.storage.session, not module memory — every event reads the flag.
//   2. Events are NOT buffered in the worker. Each one is POSTed to the server
//      immediately, so the server (recorder.py) is the only accumulator and a
//      suspend can never strand a buffer.
const REC_TYPES = new Set(["XHR", "Fetch", "Document"]);  // skip static assets
const REC_WRITES = new Set(["POST", "PUT", "PATCH", "DELETE"]);  // capture body for these
// keys whose values are redacted before a body ever touches disk
const REC_SECRET = /pass(word)?|secret|token|authorization|^auth$|bearer|ssn|cvv|otp|card(num|number)?|api[-_]?key|client[-_]?secret|cookie/i;

// Redact sensitive values and cap size, so a recorded body is safe to persist.
function recRedactBody(body) {
  if (!body) return "";
  let s = String(body).slice(0, 8192);
  try {
    const walk = (v) => {
      if (Array.isArray(v)) v.forEach(walk);
      else if (v && typeof v === "object") {
        for (const k of Object.keys(v)) {
          if (REC_SECRET.test(k)) v[k] = "***";
          else walk(v[k]);
        }
      }
    };
    const o = JSON.parse(s);
    walk(o);
    return JSON.stringify(o).slice(0, 4096);
  } catch (e) {
    // not JSON (form-encoded / raw) — redact obvious sensitive pairs
    return s
      .replace(/((?:pass(?:word)?|secret|token|auth|bearer|ssn|cvv|otp|api[-_]?key)[^=&]*=)[^&]*/gi, "$1***")
      .replace(/("(?:pass(?:word)?|secret|token|auth|bearer|ssn|cvv|otp|api[-_]?key)[^"]*"\s*:\s*)"[^"]*"/gi, '$1"***"')
      .slice(0, 4096);
  }
}

async function recIsOn() {
  try { return !!(await chrome.storage.session.get("rec_on")).rec_on; }
  catch (e) { return false; }
}

// POST one event immediately if a recording is active. No worker-memory buffer.
async function recPush(ev) {
  if (!(await recIsOn())) return;
  try {
    await fetch(`${HANDLE}/ext/record`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ events: [ev] }),
    });
  } catch (e) { /* server gone — drop; capture is best-effort */ }
}

// --- skeleton: reliable nav + tab events (no debugger, never races) ---
async function recNav(tabId, url) {
  if (!url || !/^https?:/.test(url)) return;
  await recPush({ t: Date.now(), type: "nav", tabId, url });
}

async function recTabSwitch(tabId) {
  if (!(await recIsOn())) return;
  const tab = await chrome.tabs.get(tabId).catch(() => null);
  if (tab && /^https?:/.test(tab.url || "")) {
    await recPush({ t: Date.now(), type: "tab", tabId,
      url: tab.url || "", title: tab.title || "" });
  }
  recAttach(tabId);  // enrich this tab too (best-effort)
}

// --- enrichment: CDP Network events (best-effort) ---
function recOnEvent(src, method, params) {
  if (src.tabId == null) return;
  const tabId = src.tabId;
  if (method === "Network.requestWillBeSent") {
    if (!REC_TYPES.has(params.type)) return;
    const req = params.request || {};
    const m = (req.method || "").toUpperCase();
    const ev = { t: Date.now(), type: "request", tabId, reqId: params.requestId,
      method: m, url: req.url || "", rtype: params.type };
    // for state-changing calls, capture the payload — the data you submitted
    if (REC_WRITES.has(m)) {
      if (req.postData) {
        ev.body = recRedactBody(req.postData);
      } else if (req.hasPostData) {
        // body too large to ride inline — fetch it explicitly, then push
        chrome.debugger.sendCommand({ tabId }, "Network.getRequestPostData",
          { requestId: params.requestId })
          .then((r) => recPush({ ...ev, body: recRedactBody(r && r.postData) }))
          .catch(() => recPush(ev));
        return;
      }
    }
    recPush(ev);
  } else if (method === "Network.responseReceived") {
    if (!REC_TYPES.has(params.type)) return;
    const r = params.response || {};
    recPush({ t: Date.now(), type: "response", tabId, reqId: params.requestId,
      status: r.status, mime: r.mimeType || "", url: r.url || "" });
  }
}

// attached-tab set persisted in storage.session so stop can detach across a wake
async function recAttachedTabs() {
  try { return (await chrome.storage.session.get("rec_tabs")).rec_tabs || []; }
  catch (e) { return []; }
}

async function recAttach(tabId) {
  if (!(await recIsOn())) return;
  const attached = await recAttachedTabs();
  if (attached.includes(tabId)) return;
  try {
    await attachClean({ tabId });
    await chrome.debugger.sendCommand({ tabId }, "Network.enable");
    attached.push(tabId);
    await chrome.storage.session.set({ rec_tabs: attached });
  } catch (e) { /* chrome:// or another debugger holds it — skeleton still covers it */ }
}

async function recDetachAll() {
  const attached = await recAttachedTabs();
  for (const tabId of attached) {
    try { await chrome.debugger.detach({ tabId }); } catch (e) {}
  }
  try { await chrome.storage.session.set({ rec_tabs: [] }); } catch (e) {}
}

async function startRecording() {
  if (await recIsOn()) return { recording: true, note: "already recording" };
  try { await chrome.storage.session.set({ rec_on: true, rec_tabs: [] }); } catch (e) {}
  try {
    const [active] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (active) await recTabSwitch(active.id);
  } catch (e) {}
  return { recording: true };
}

async function stopRecording() {
  if (!(await recIsOn())) return { recording: false };
  try { await chrome.storage.session.set({ rec_on: false }); } catch (e) {}
  await recDetachAll();
  return { recording: false };
}

// After a worker wake mid-session, re-attach the active tab so enrichment
// resumes. The flag itself is in storage, so the skeleton never needed restoring.
async function recRestore() {
  if (!(await recIsOn())) return;
  try {
    const [active] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (active) recAttach(active.id);
  } catch (e) {}
}

// Move tabs to another window, keeping page state (chrome.tabs.move, not a
// reload). With no window_id, the tabs go into a new window. Returns its id.
async function moveTabs(tabIds, windowId) {
  if (!tabIds || !tabIds.length) return { error: "no tabs" };
  let target = windowId;
  let rest = tabIds;
  if (!target) {
    const w = await chrome.windows.create({ tabId: tabIds[0], focused: false });
    target = w.id;
    rest = tabIds.slice(1);
  }
  if (rest.length) await chrome.tabs.move(rest, { windowId: target, index: -1 });
  return { window_id: target, moved: tabIds.length };
}

// Open a new window whose tabs sit in named, colored native tab groups.
// groups: [{ title, color, urls: [..] }]; ungrouped: urls opened after the groups.
async function openGroupedWindow(groups, ungrouped) {
  if (!groups || !groups.length) return { error: "no groups" };
  const w = await chrome.windows.create({ url: groups[0].urls[0], focused: true });
  const first = w.tabs[0].id;
  const made = [];
  for (const [gi, g] of groups.entries()) {
    const ids = gi === 0 ? [first] : [];
    for (const url of gi === 0 ? g.urls.slice(1) : g.urls) {
      ids.push((await chrome.tabs.create({ windowId: w.id, url, active: false })).id);
    }
    const groupId = await chrome.tabs.group({ tabIds: ids, createProperties: { windowId: w.id } });
    await chrome.tabGroups.update(groupId, { title: g.title, color: g.color || "grey", collapsed: false });
    made.push({ title: g.title, group_id: groupId, tabs: ids.length });
  }
  for (const url of ungrouped || []) await chrome.tabs.create({ windowId: w.id, url, active: false });
  await chrome.tabs.update(first, { active: true });
  return { window_id: w.id, groups: made };
}

// Run: a fixed sequence of steps (a slide image or the nth tab of a named group),
// stepped with the run-next / run-prev keyboard commands. Slides are images in
// deck/ (gitignored) shown by deck/deck.html#N in the run's window.
const DECK_URL = chrome.runtime.getURL("deck/deck.html");

async function runSet(steps, windowId) {
  if (!steps || !steps.length) return { error: "no steps" };
  // The run's window is the one holding its first named group, else the focused one.
  const firstGroup = steps.find((s) => s.group);
  const [g] = firstGroup ? await chrome.tabGroups.query({ title: firstGroup.group }) : [];
  const win = windowId || g?.windowId || (await chrome.windows.getLastFocused()).id;
  await chrome.storage.local.set({ run: { steps, windowId: win, i: -1 } });
  await chrome.action?.setBadgeText?.({ text: "" });
  return { ok: true, steps: steps.length, window_id: win };
}

async function runTarget(step, windowId) {
  if (step.slide) {
    const url = `${DECK_URL}#${step.slide}`;
    const [deck] = await chrome.tabs.query({ windowId, url: DECK_URL + "*" });
    if (deck) return (await chrome.tabs.update(deck.id, { url, active: true })).id;
    return (await chrome.tabs.create({ windowId, url, active: true })).id;
  }
  const [group] = await chrome.tabGroups.query({ windowId, title: step.group });
  if (!group) throw new Error(`no tab group "${step.group}" in the run window`);
  const tabs = (await chrome.tabs.query({ groupId: group.id })).sort((a, b) => a.index - b.index);
  const tab = tabs[step.n || 0];
  if (!tab) throw new Error(`group "${step.group}" has no tab ${step.n || 0}`);
  if (group.collapsed) await chrome.tabGroups.update(group.id, { collapsed: false });
  await chrome.tabs.update(tab.id, { active: true });
  return tab.id;
}

async function runGo({ delta, index }) {
  const { run } = await chrome.storage.local.get("run");
  if (!run) return { error: "no run set" };
  const i = index !== undefined ? index : Math.min(Math.max(run.i + delta, 0), run.steps.length - 1);
  const step = run.steps[i];
  const tabId = await runTarget(step, run.windowId);
  await chrome.windows.update(run.windowId, { focused: true });
  run.i = i;
  await chrome.storage.local.set({ run });
  await chrome.action?.setBadgeText?.({ text: `${i + 1}` });
  return { i, of: run.steps.length, step, tab_id: tabId };
}

async function runStatus() {
  const { run } = await chrome.storage.local.get("run");
  const shortcuts = (await chrome.commands.getAll()).map((c) => ({ name: c.name, shortcut: c.shortcut }));
  return { run, shortcuts };
}

// Step to the adjacent tab in the focused window; stops at the first and last tab.
async function stepTab(delta) {
  const win = await chrome.windows.getLastFocused({ windowTypes: ["normal"] });
  const [cur] = await chrome.tabs.query({ active: true, windowId: win.id });
  if (!cur) return { error: "no active tab" };
  const [next] = await chrome.tabs.query({ windowId: cur.windowId, index: cur.index + delta });
  if (!next) return { window_id: win.id, at: cur.index, error: "no tab there" };
  if (next.groupId !== -1) await chrome.tabGroups.update(next.groupId, { collapsed: false });
  await chrome.tabs.update(next.id, { active: true });
  return { window_id: win.id, from: cur.index, to: next.index };
}

// Show: per-page demo actions. Each press of show-next runs the next action on the
// active tab: scroll an element into view and ring it, or ring it and click it.
// Scripts live in chrome.storage.local "shows" (set over the bridge), matched by
// tab group title and the URL the tab had on its first press.
async function showAction(tabId, a) {
  // wheel the page (real scroll events) until the target is on screen, then act
  for (let k = 0; k < 8; k++) {
    const out = await showActionOnce(tabId, a);
    if (!out || !(out.offscreen || out.moved)) return out;
    if (out.moved) { await new Promise((r) => setTimeout(r, 700)); continue; } // measure again once the page settles
    const win = await chrome.windows.get((await chrome.tabs.get(tabId)).windowId);
    const z = await chrome.tabs.getZoom(tabId).catch(() => 1); // page px are CSS px: scale by the tab's zoom
    const x = win.left + (win.width - out.iw * z) + out.cx * z, y = win.top + (win.height - out.ih * z) + out.ih * z / 2;
    await fetch("http://127.0.0.1:4911/move", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ x, y, scroll: 4 * out.offscreen }) }).catch(() => {});
    await new Promise((r) => setTimeout(r, 450));
  }
  return { error: `"${a.text}" stayed off screen or kept moving; not clicking` };
}

async function showActionOnce(tabId, a) {
  // "{ts}" in pasted text becomes a short unique token (e.g. a fresh widget ID per run)
  if (a.paste && a.paste.includes("{ts}")) a = { ...a, paste: a.paste.replace(/\{ts\}/g, Date.now().toString(36).slice(-5)) };
  const [res] = await chrome.scripting.executeScript({
    target: { tabId },
    args: [a],
    func: async (a) => {
      const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
      const norm = (s) => (s || "").replace(/\s+/g, " ").trim().toLowerCase();
      const deep = (root, acc) => {
        let kids; try { kids = root.querySelectorAll("*"); } catch (e) { return acc; }
        for (const el of kids) {
          acc.push(el);
          if (el.shadowRoot) deep(el.shadowRoot, acc);
          if (el.tagName === "IFRAME") { try { if (el.contentDocument) deep(el.contentDocument, acc); } catch (e) {} } // same-origin frames (classic forms)
        }
        return acc;
      };
      const own = (e) => { let t = ""; for (const n of e.childNodes) if (n.nodeType === 3) t += n.textContent; return norm(t); };
      const visible = (e) => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
      const ROLES = ["button", "link", "tab", "option", "menuitem"];
      const clickable = (e) => /^(BUTTON|A)$/.test(e.tagName) || (e.getAttribute && ROLES.includes(e.getAttribute("role")));
      const label = (e) => own(e) || norm(e.getAttribute && (e.getAttribute("aria-label") || e.getAttribute("alt") || e.getAttribute("placeholder"))) || (a.click ? norm(e.innerText) : "");
      const area = (e) => { const r = e.getBoundingClientRect(); return r.width * r.height; };
      const find = () => {
        for (const t of [a.text, ...(a.alts || [])]) { const e = findOne(norm(t)); if (e) return e; }
        return null;
      };
      const findOne = (q) => {
        const vis = deep(document, []).filter(visible);
        // clicks prefer real controls, then fall back to any element with the text
        // (some nav items are plain divs with click handlers)
        for (const all of a.click ? [vis.filter(clickable), vis] : [vis]) {
          for (const test of a.exact ? [(t) => t === q] : [(t) => t === q, (t) => t.startsWith(q), (t) => t.includes(q)]) {
            const hits = all.filter((e) => test(label(e)));
            if (hits.length) return hits.sort((x, y) => area(x) - area(y))[a.nth || 0] || hits[0];
          }
        }
        return null;
      };
      let el = null;
      const deadline = Date.now() + (a.wait || 8000);
      while (!(el = find()) && Date.now() < deadline) await sleep(300);
      if (!el) return { error: `not found: ${a.text}` };
      if (a.probe) return { ok: true, did: "found", name: label(el).slice(0, 60) };
      for (let k = 0; k < (a.up || 0); k++) el = el.parentElement || (el.getRootNode() && el.getRootNode().host) || el;
      const name = label(el).slice(0, 60);
      if (a.click && !a.allow && /\b(save|publish|reset|delete|remove|clone|approve|reject|submit|update|create new theme)\b/.test(name))
        return { error: `refused to click "${name}"` };
      el.scrollIntoView({ block: "center", behavior: "smooth" });
      // wait for the smooth scroll to settle; if the element is still outside the window, jump it into view
      const inView = () => {
        const b = el.getBoundingClientRect();
        let oy = 0;
        for (let w = el.ownerDocument.defaultView; w && w.frameElement; w = w.parent) oy += w.frameElement.getBoundingClientRect().top;
        return b.top + oy >= 0 && b.bottom + oy <= window.top.innerHeight;
      };
      let last = -1;
      for (let k = 0; k < 20; k++) {
        await sleep(100);
        const t = el.getBoundingClientRect().top;
        if (t === last && inView()) break;
        last = t;
      }
      if (!inView()) { el.scrollIntoView({ block: "center" }); await sleep(250); }
      if (!inView() && !a.probe) {
        // the page does not scroll from script (custom scroller): ask for real wheel ticks
        window.__handleShowOwnUntil = Date.now() + 2500;
        const b = el.getBoundingClientRect();
        let ox = 0;
        for (let w = el.ownerDocument.defaultView; w && w.frameElement; w = w.parent) ox += w.frameElement.getBoundingClientRect().left;
        const iw = window.top.innerWidth;
        return { offscreen: b.top > 0 ? 1 : -1, name: label(el).slice(0, 60), iw, ih: window.top.innerHeight,
          cx: Math.max(10, Math.min(iw - 10, ox + b.left + Math.min(b.width / 2, 60))) };
      }
      // element box in top-window coordinates (targets inside same-origin frames are offset by their frame)
      const r0 = el.getBoundingClientRect();
      let ox = 0, oy = 0;
      for (let w = el.ownerDocument.defaultView; w && w.frameElement; w = w.parent) {
        const fr = w.frameElement.getBoundingClientRect(); ox += fr.left; oy += fr.top;
      }
      const r = { left: r0.left + ox, top: r0.top + oy, width: r0.width, height: r0.height };
      // screen point for the real cursor: window screen origin + browser chrome
      // (vertical tabs on the left, toolbars on top) + the element's left-centre
      const W = window.top;
      const px = Math.min(r.width / 2, 60), py = r.height / 2;
      // page-relative point; the background adds the window's screen position
      // (window.screenX/outerWidth read 0 on some pages, so they are not used)
      const cx = r.left + px, cy = r.top + py, iw = W.innerWidth, ih = W.innerHeight;
      if (cx < 0 || cy < 0 || cx > iw || cy > ih) return { error: `"${name}" is off screen at ${Math.round(cx)},${Math.round(cy)}; not clicking` };
      if (a.click) {
        // only click if this element is what sits under that point
        let hit = el.ownerDocument.elementFromPoint(r0.left + px, r0.top + py);
        while (hit && hit.shadowRoot) {
          const inner = hit.shadowRoot.elementFromPoint(r0.left + px, r0.top + py);
          if (!inner || inner === hit) break;
          hit = inner;
        }
        const within = (x, y) => { for (let n = x; n; n = n.parentElement || (n.getRootNode && n.getRootNode().host)) if (n === y) return true; return false; };
        if (!hit || !(within(hit, el) || within(el, hit))) return { error: `"${name}" is covered at its position; not clicking` };
      }
      // retry path: the real click did not take, so click from script
      if (a.click && a.synthetic) {
        // a link navigates its own frame (el.click() can open a new tab from inside classic-UI frames)
        const link = el.closest ? el.closest("a[href]") : null;
        if (link && !/^javascript:/i.test(link.getAttribute("href"))) link.ownerDocument.defaultView.location.href = link.href;
        else el.click();
        return { ok: true, did: "clicked (script)", name };
      }
      window.__handleShowTarget = el; // re-measured after the glide, before the real click
      // the coming real click is ours: the override watcher ignores input until then
      if (a.click) window.__handleShowOwnUntil = Date.now() + (a.paste || a.clear ? 5000 : 2500);
      return { ok: true, did: a.click ? "clicked" : "showed", name, cx, cy, iw, ih };
    },
  });
  const out = res.result;
  if (out && out.ok && !a.probe && !a.synthetic) {
    // screen point = window origin + browser chrome (vertical tabs on the left, toolbars on top) + page point
    const win = await chrome.windows.get((await chrome.tabs.get(tabId)).windowId);
    // keystrokes go to the focused app: bring the demo window forward first
    // a click or keystroke into a window in the background is spent focusing it: bring it forward first
    if ((a.click || a.paste || a.clear) && !win.focused) { await chrome.windows.update(win.id, { focused: true }); await new Promise((r) => setTimeout(r, 250)); }
    // focusing can take a second or more: mark the coming input as ours from now, so it does not read as an override
    if (a.click || a.paste || a.clear) {
      await chrome.scripting.executeScript({ target: { tabId }, args: [a.paste || a.clear ? 5000 : 2500],
        func: (ms) => { window.__handleShowOwnUntil = Date.now() + ms; } }).catch(() => {});
    }
    // page px are CSS px: scale by the tab's zoom (a flow can set zoom to fit a long form)
    const z = await chrome.tabs.getZoom(tabId).catch(() => 1);
    out.x = win.left + (win.width - out.iw * z) + out.cx * z;
    out.y = win.top + (win.height - out.ih * z) + out.cy * z;
    // a click glides first, then checks what is under the point before pressing: the page can
    // move during the glide (a late load shifted a "Child Topics" tab and the click hit "Delete")
    if (a.click && !a.paste && !a.clear) {
      await fetch("http://127.0.0.1:4911/move", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ x: out.x, y: out.y }) }).catch(() => {});
      const [chk] = await chrome.scripting.executeScript({
        target: { tabId }, args: [a, out.cx, out.cy],
        func: (a, cx, cy) => {
          // the target found before the glide: still where the cursor is now?
          const el = window.__handleShowTarget;
          if (!el || !el.isConnected) return { ok: false, what: "target gone" };
          const r0 = el.getBoundingClientRect();
          let ox = 0, oy = 0;
          for (let w = el.ownerDocument.defaultView; w && w.frameElement; w = w.parent) {
            const fr = w.frameElement.getBoundingClientRect(); ox += fr.left; oy += fr.top;
          }
          const px = Math.min(r0.width / 2, 60), py = r0.height / 2;
          const dx = r0.left + ox + px - cx, dy = r0.top + oy + py - cy;
          return { ok: Math.abs(dx) < 4 && Math.abs(dy) < 4, what: `target moved ${Math.round(dx)},${Math.round(dy)}px` };
        },
      }).catch((e) => [{ result: { ok: false, what: String(e) } }]);
      if (!chk.result.ok) return { moved: true, error: `page moved during the glide (${chk.result.what}); not clicking` };
      await chrome.scripting.executeScript({ target: { tabId }, func: () => { window.__handleShowOwnUntil = Date.now() + 2500; } }).catch(() => {});
    }
    // the real cursor glides there (and clicks): handled by mouse_helper.py on :4911
    const m = await fetch("http://127.0.0.1:4911/move", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ x: out.x, y: out.y, click: !!a.click, paste: a.paste || "", clear: !!a.clear, replace: !!a.replace }),
    }).then((x) => x.json()).catch((e) => ({ error: `mouse helper not running: ${e}` }));
    if (m.error) return { error: m.error };
    if (a.paste || a.clear) {
      // check the box got the text; if the keystrokes missed, set it the way a paste would
      const [chk] = await chrome.scripting.executeScript({
        target: { tabId }, args: [a],
        func: (a) => {
          const boxes = [];
          const walk = (root) => { for (const el of root.querySelectorAll("*")) { if (el.shadowRoot) walk(el.shadowRoot); if (/^(TEXTAREA|INPUT)$/.test(el.tagName) && (el.getAttribute("aria-label") || el.placeholder || "") === a.text) boxes.push(el); } };
          walk(document);
          const box = boxes[0];
          if (!box) return { box: false };
          const want = a.paste || "";
          if (box.value === want) return { box: true, typed: "keys" };
          const set = Object.getOwnPropertyDescriptor((box.tagName === "INPUT" ? HTMLInputElement : HTMLTextAreaElement).prototype, "value").set;
          box.focus(); set.call(box, want);
          box.dispatchEvent(new InputEvent("input", { bubbles: true, composed: true, inputType: "insertFromPaste", data: want }));
          box.dispatchEvent(new Event("change", { bubbles: true, composed: true }));
          return { box: true, typed: "set" };
        },
      });
      out.typed = chk.result;
    }
  }
  return out;
}

// Autopilot over a page's show script. play() runs the remaining steps, waiting
// each step's `dwell` seconds (time to say the matching talk-track lines).
// pause() stops after the current step; pause() while paused resets the page.
// A real click, scroll or keypress by the presenter on the page pauses it, and a
// A `manual` step points at the element and pauses for the presenter to act.
const showRuns = {}; // tabId -> { playing, skip, error, dwellEnd }

// The presentation window is the one holding the first named group in the shows,
// so keys and the panel act on the demo window even when focus is elsewhere.
async function presentationWindow() {
  const { shows = [] } = await chrome.storage.local.get("shows");
  const g = shows.find((s) => s.group)?.group;
  if (g) { const [grp] = await chrome.tabGroups.query({ title: g }); if (grp) return grp.windowId; }
  return (await chrome.windows.getLastFocused({ windowTypes: ["normal"] })).id;
}

async function showCtx() {
  const winId = await presentationWindow();
  const [tab] = await chrome.tabs.query({ active: true, windowId: winId });
  if (!tab) return { winId };
  const { shows = [] } = await chrome.storage.local.get("shows");
  const key = `show-${tab.id}`;
  const st = (await chrome.storage.session.get(key))[key] || { i: 0, start: tab.url };
  const group = tab.groupId !== -1 ? (await chrome.tabGroups.get(tab.groupId)).title : "";
  // a tab pinned to a flow keeps it wherever the page has wandered; otherwise match by URL
  const { pins = {} } = await chrome.storage.local.get("pins");
  const script = pins[tab.id] != null ? shows[pins[tab.id]]
    : shows.find((x) => (!x.group || x.group === group) && new RegExp(x.url).test(st.start));
  if (script?.start) st.start = script.start;
  return { tab, key, st, script, group, winId };
}

const badge = (tabId, text, color) => {
  chrome.action.setBadgeText({ text, tabId });
  if (color) chrome.action.setBadgeBackgroundColor({ color, tabId });
};

const stepLabel = (s) => s.paste ? `Paste prompt: “${s.paste.slice(0, 48)}…”` : s.clear ? "Clear the prompt box" : `${s.click ? "Click" : "Point at"} “${s.text}”`;

// Publish what the panel shows: current page, its steps, position and state.
async function publish() {
  const c = await showCtx();
  const state = { at: Date.now(), tabs: [] };
  if (c.winId) {
    const { shows = [] } = await chrome.storage.local.get("shows");
    const groups = {};
    for (const g of await chrome.tabGroups.query({ windowId: c.winId })) groups[g.id] = g.title;
    for (const t of (await chrome.tabs.query({ windowId: c.winId })).sort((a, b) => a.index - b.index)) {
      const group = groups[t.groupId] || "";
      const st = (await chrome.storage.session.get(`show-${t.id}`))[`show-${t.id}`];
      const { pins = {} } = await chrome.storage.local.get("pins");
      const has = pins[t.id] != null || shows.some((x) => (!x.group || x.group === group) && new RegExp(x.url).test(st?.start || t.url));
      state.tabs.push({ id: t.id, title: t.title, group, active: t.active, flow: has });
    }
  }
  if (c.tab) {
    const run = showRuns[c.tab.id] || {};
    const n = c.script ? c.script.steps.length : 0;
    Object.assign(state, {
      tabId: c.tab.id, title: c.tab.title, group: c.group, flow: !!c.script,
      steps: c.script ? c.script.steps.map(stepLabel) : [], says: c.script ? c.script.steps.map((x) => x.say || "") : [],
      intro: c.script?.say || "", i: c.st.i, n,
      status: !c.script ? "none" : run.playing ? "playing" : run.error ? "error" : c.st.i >= n ? "done" : c.st.i > 0 ? "paused" : "ready",
      error: run.error || "", pausedBy: run.playing ? "" : run.pausedBy || "", pausedLate: run.pausedLate, dwellEnd: run.playing ? run.dwellEnd || 0 : 0, dwellMs: run.dwellMs || 0,
    });
  }
  state.lastPlay = lastPlay;
  await chrome.storage.session.set({ panel: state });
}

async function watchOverride(tabId) {
  await chrome.scripting.executeScript({
    target: { tabId },
    func: () => {
      if (window.__handleShowWatch) return;
      window.__handleShowWatch = true;
      const stop = (e) => {
        if (!e.isTrusted || e.altKey || Date.now() < (window.__handleShowOwnUntil || 0)) return;
        chrome.runtime.sendMessage({ type: "show-override", kind: e.type, late: Date.now() - (window.__handleShowOwnUntil || 0) });
      };
      for (const t of ["pointerdown", "wheel", "touchstart"]) addEventListener(t, stop, { capture: true, passive: true });
      addEventListener("keydown", stop, { capture: true });
    },
  }).catch(() => {});
}

// Run one step at the current position; returns false if it failed.
async function showRunOne(c, run) {
  const step = c.script.steps[c.st.i];
  let out = await showAction(c.tab.id, step).catch((e) => ({ error: String(e) }));
  // the click before this step may not have taken (page still settling): redo it once, then retry
  const prev = c.st.i > 0 && c.script.steps[c.st.i - 1];
  if (out?.error?.startsWith("not found") && prev?.click && !prev.paste) {
    await showAction(c.tab.id, { ...prev, synthetic: true }).catch(() => {});
    await new Promise((r) => setTimeout(r, 1500));
    out = await showAction(c.tab.id, step).catch((e) => ({ error: String(e) }));
  }
  if (!out || !out.ok) {
    run.playing = false; run.error = (out && out.error) || "step failed";
    badge(c.tab.id, "!", "#d1242f"); await publish(); return false;
  }
  run.error = "";
  c.st.i += 1;
  await chrome.storage.session.set({ [c.key]: c.st });
  await watchOverride(c.tab.id); // a click may have loaded a new page
  return true;
}

let lastPlay = { src: "", at: 0 };
async function showPlay(src = "bridge") {
  lastPlay = { src, at: Date.now() };
  const c = await showCtx();
  if (!c.tab) return { error: "no active tab" };
  if (!c.script) { badge(c.tab.id, "?", "#888"); await publish(); return { error: "no show for this page" }; }
  const prev = showRuns[c.tab.id];
  if (prev?.playing) { prev.skip = true; return { skipped: true }; } // play while playing: next step now
  const run = (showRuns[c.tab.id] = { playing: true });
  // a flow can set the page zoom so every target fits in the window (e.g. tall editors)
  if (c.script.zoom) await chrome.tabs.setZoom(c.tab.id, c.script.zoom).catch(() => {});
  const n = c.script.steps.length;
  await watchOverride(c.tab.id);
  while (run.playing && c.st.i < n) {
    badge(c.tab.id, `▶${c.st.i + 1}`, "#1a7f37");
    await publish();
    const step = c.script.steps[c.st.i];
    run.busy = true;
    const ok = await showRunOne(c, run);
    run.busy = false;
    if (!ok) return { error: run.error };
    if (!run.playing) break; // paused or jumped while the step ran: leave the position to whoever stopped us
    if (step.manual) { run.playing = false; break; }
    run.skip = false;
    run.dwellMs = (step.dwell || 3) * 1000; run.dwellEnd = Date.now() + run.dwellMs;
    await publish();
    while (Date.now() < run.dwellEnd && run.playing && !run.skip) await new Promise((r) => setTimeout(r, 100));
  }
  run.playing = false;
  badge(c.tab.id, c.st.i >= n ? "end" : "II", c.st.i >= n ? "#57606a" : "#bf8700");
  await publish();
  return { at: c.st.i, of: n };
}

// jump to a step and play from it; a step still running is let finish first, so it cannot move the position after the jump
async function showJumpPlay(i, src) {
  const c = await showCtx();
  const old = c.tab && showRuns[c.tab.id];
  if (old) { old.playing = false; for (let k = 0; k < 150 && old.busy; k++) await new Promise((r) => setTimeout(r, 100)); }
  await showSetPosition(i);
  return await showPlay(src);
}

async function showPauseOnly() {
  const c = await showCtx();
  const run = c.tab && showRuns[c.tab.id];
  if (run?.playing) { run.playing = false; badge(c.tab.id, "II", "#bf8700"); }
  await publish();
}

async function showReset() {
  const c = await showCtx();
  if (!c.tab) return {};
  const run = showRuns[c.tab.id];
  if (run) { run.playing = false; run.error = ""; }
  await chrome.storage.session.set({ [c.key]: { i: 0, start: c.st.start } });
  // always reload: single-page apps keep their inner state when the URL is unchanged
  await chrome.tabs.update(c.tab.id, { url: c.st.start });
  badge(c.tab.id, "", null);
  await publish();
  return { reset: true };
}

// Reset all: every pinned flow tab back to its start URL and step 1, then the first one in front
async function showResetAll() {
  for (const r of Object.values(showRuns)) { r.playing = false; r.error = ""; }
  const { pins = {} } = await chrome.storage.local.get("pins");
  const winId = await presentationWindow();
  const tabs = (await chrome.tabs.query({ windowId: winId })).filter((t) => pins[t.id] != null).sort((a, b) => a.index - b.index);
  for (const t of tabs) {
    await chrome.tabs.update(t.id, { active: true });
    await new Promise((r) => setTimeout(r, 300));
    await showReset();
  }
  if (tabs.length) await chrome.tabs.update(tabs[0].id, { active: true });
  await publish();
  return { reset: tabs.length };
}

// ⌥↑: pause; when already paused, reset the page
async function showPause() {
  const c = await showCtx();
  if (c.tab && showRuns[c.tab.id]?.playing) return showPauseOnly();
  return showReset();
}

// Next: while playing, skip the wait; while paused, run the current step once.
async function showNextStep() {
  const c = await showCtx();
  if (!c.tab || !c.script) return;
  const run = showRuns[c.tab.id] || (showRuns[c.tab.id] = {});
  if (run.playing) { run.skip = true; return; }
  if (c.st.i >= c.script.steps.length) return;
  await watchOverride(c.tab.id);
  await showRunOne(c, run);
  badge(c.tab.id, "II", "#bf8700");
  await publish();
}

// Move the position without running anything; Play or Next runs from there.
async function showSetPosition(i) {
  const c = await showCtx();
  if (!c.tab || !c.script) return;
  const run = showRuns[c.tab.id] || (showRuns[c.tab.id] = {});
  run.playing = false; run.error = "";
  c.st.i = Math.max(0, Math.min(i, c.script.steps.length));
  await chrome.storage.session.set({ [c.key]: c.st });
  badge(c.tab.id, "II", "#bf8700");
  await publish();
}

async function showTab(delta, tabId) {
  const winId = await presentationWindow();
  if (tabId) await chrome.tabs.update(tabId, { active: true });
  else {
    const [cur] = await chrome.tabs.query({ active: true, windowId: winId });
    const [next] = cur ? await chrome.tabs.query({ windowId: winId, index: cur.index + delta }) : [];
    if (next) {
      if (next.groupId !== -1) await chrome.tabGroups.update(next.groupId, { collapsed: false });
      await chrome.tabs.update(next.id, { active: true });
    }
  }
  await publish();
}

async function openPanel() {
  const url = chrome.runtime.getURL("panel/panel.html");
  const [open] = await chrome.tabs.query({ url });
  if (open) { await chrome.windows.update(open.windowId, { focused: true }); return { window_id: open.windowId }; }
  const w = await chrome.windows.create({ url, type: "popup", width: 400, height: 780, left: 1500, top: 60 });
  return { window_id: w.id };
}

chrome.action.onClicked.addListener(() => openPanel());

chrome.tabs.onActivated.addListener(() => publish().catch(() => {}));
chrome.tabs.onUpdated.addListener((id, info) => { if (info.status === "complete") publish().catch(() => {}); });

chrome.runtime.onMessage.addListener((msg, sender, reply) => {
  if (msg?.type === "show-override" && sender.tab && showRuns[sender.tab.id]?.playing) {
    showRuns[sender.tab.id].playing = false;
    showRuns[sender.tab.id].pausedBy = `${msg.kind} on the page`;
    showRuns[sender.tab.id].pausedLate = msg.late;
    badge(sender.tab.id, "II", "#bf8700");
    publish();
  }
  const p = msg?.panel;
  if (!p) return;
  panelAct(msg, "panel").catch((e) => console.warn("panel", e)).finally(() => reply && reply({ ok: true }));
  return true;
});

// One entry point for the controls: the extension panel and the Mac app (over the bridge)
async function panelAct(msg, src) {
  const act = {
    play: () => showPlay(src), pause: () => showPauseOnly(), stop: () => showReset(), resetall: () => showResetAll(),
    toggle: async () => { const c = await showCtx(); return c.tab && showRuns[c.tab.id]?.playing ? showPauseOnly() : showPlay(src); },
    next: () => showNextStep(), prev: async () => { const c = await showCtx(); return showSetPosition((c.st?.i || 0) - 1); },
    jump: () => showSetPosition(msg.i), jumpplay: () => showJumpPlay(msg.i, src), tab: () => showTab(msg.delta, msg.tabId), refresh: () => publish(),
  }[msg.panel];
  if (act) await act();
  await publish();
  return (await chrome.storage.session.get("panel")).panel || {};
}

chrome.commands.onCommand.addListener((command) => {
  if (command === "tab-next") showTab(1).catch((e) => console.warn("tab", e));
  if (command === "tab-prev") showTab(-1).catch((e) => console.warn("tab", e));
});

async function dispatch(cmd) {
  const { method, params = {} } = cmd;
  const tabId = params.tab_id;
  switch (method) {
    case "read": return await withTimeout(readTab(tabId), 12000, "read");
    case "eval": return await evalIn(tabId, params.expression || "");
    case "click": return await clickIn(tabId, params.sel, params.text, params.nth, params.near);
    case "type": return await typeIn(tabId, params.sel, params.text, params.submit);
    case "resolve": return await resolveEls(tabId, params.intent || "");
    case "scroll": return await scrollIn(tabId, params.dy || 2000);
    case "harvest": return await withTimeout(harvestIn(tabId, params.sel || "", params.fields || {}, params.limit || 0), 12000, "harvest");
    case "screenshot": return await withTimeout(screenshot(tabId), 8000, "screenshot");
    case "console": return await withTimeout(consoleLogs(tabId, params.ms || 1800), (params.ms || 1800) + 6000, "console");
    case "record_start": return await startRecording();
    case "record_stop": return await stopRecording();
    case "move": return await moveTabs(params.tab_ids, params.window_id);
    case "window": return await openGroupedWindow(params.groups, params.ungrouped);
    case "run_set": return await runSet(params.steps, params.window_id);
    case "run_go": return await runGo(params);
    case "run_status": return await runStatus();
    case "tab_step": return await stepTab(params.delta || 1);
    case "show_set": await chrome.storage.local.set({ shows: params.shows || [], pins: params.pins || {} }); return { ok: true, shows: (params.shows || []).length };
    case "show_play": return await showPlay();
    case "tab_new": {
      const after = await chrome.tabs.get(params.after_tab_id);
      const t = await chrome.tabs.create({ windowId: after.windowId, index: after.index + 1, url: params.url, active: false });
      if (after.groupId !== -1) await chrome.tabs.group({ tabIds: [t.id], groupId: after.groupId });
      return { tab_id: t.id };
    }
    case "show_tab": return await showTab(params.delta || 0, params.tab_id);
    case "panel_open": return await openPanel();
    case "tab_close": await chrome.tabs.remove(params.tab_ids); return { ok: true };
    case "panel_get": return (await chrome.storage.session.get("panel")).panel || {};
    case "panel_msg": return await panelAct(params, params.src || "app");
    case "panel_state": await publish(); return (await chrome.storage.session.get("panel")).panel || {};
    case "show_clear": for (const k of Object.keys(showRuns)) showRuns[k].playing = false; await chrome.storage.session.clear(); return { ok: true };
    case "tab_move": return await chrome.tabs.move(params.tab_id, { index: params.index });
    case "show_pause": return await showPause();
    case "show_action": return await showAction(tabId, params.action);
    case "reload_ext": setTimeout(() => chrome.runtime.reload(), 200); return { reloading: true };
    case "ping": return { pong: true, version: VERSION };
    default: return { error: "unknown method: " + method };
  }
}

// ---- passive capture ------------------------------------------------------
// The index was only ever as good as its corpus, and the corpus only grew when
// an agent ran `tab read`. So: watch which tab is actually focused, and once
// you've stayed on it long enough to have really read it, extract the readable
// text and hand it to the server. Same FTS5 index, no extra effort.
//
// The policy (dwell bar, exclusions) is owned by capture.py and pulled from
// /ext/policy. It is applied HERE, before any text is extracted — so an excluded
// domain's content is never read out of the page at all, let alone sent. The
// server re-checks on ingest; both sides have to agree before anything is stored.

let POLICY = null;         // null until first fetch → capture stays off
let POLICY_AT = 0;
const FOCUS = { tabId: null, url: "", since: 0, captured: false };

async function fetchPolicy() {
  if (Date.now() - POLICY_AT < 60000) return POLICY;
  try {
    const r = await fetch(`${HANDLE}/ext/policy`);
    POLICY = await r.json();
    POLICY_AT = Date.now();
  } catch (e) { /* server down → POLICY stays as-is; capture just pauses */ }
  return POLICY;
}

// Mirrors capture.excluded_by() exactly: a term with no slash matches the HOST,
// a term with one matches host+path. Keep the two in step — the server re-checks,
// so a mismatch shows up as text extracted and then thrown away.
function capExcluded(url) {
  if (!POLICY || !POLICY.enabled) return true;
  const u = (url || "").toLowerCase();
  if (!u.startsWith("http")) return true;
  let host, hostpath;
  try {
    const p = new URL(u);
    host = p.hostname;
    hostpath = p.hostname + p.pathname;
  } catch (e) { return true; }
  const hit = (POLICY.exclude || []).some((t) => {
    const term = String(t).toLowerCase();
    if (!term) return false;
    return (term.includes("/") ? hostpath : host).includes(term);
  });
  if (hit) return true;
  // Junk kinds: pages with plenty of text but no content (search results, video,
  // infinite feeds, shopping). They'd clear any character floor and then degrade
  // every query. Patterns come from capture.py so both sides skip the same pages.
  const junk = (POLICY && POLICY.junk) || {};
  for (const kind of Object.keys(junk)) {
    for (const pat of junk[kind]) {
      try { if (new RegExp(pat).test(u)) return true; } catch (e) {}
    }
  }
  return false;
}

// Readability in the page: strip the chrome, score containers by how much real
// paragraph text they hold, return the winner's innerText. Cheap enough to run
// on a page you're already looking at (it clones once and only walks blocks).
async function extractReadable(tabId) {
  const [res] = await chrome.scripting.executeScript({
    target: { tabId },
    func: () => {
      if (!document.body || document.visibilityState === "hidden") return null;
      const body = document.body.cloneNode(true);
      body.querySelectorAll(
        "script,style,noscript,svg,iframe,form,nav,header,footer,aside," +
        "[role=navigation],[role=banner],[role=complementary],[aria-hidden=true]"
      ).forEach((el) => el.remove());
      let best = null, bestScore = 0;
      const blocks = body.querySelectorAll("main,article,[role=main],div,section");
      for (const el of blocks) {
        let score = 0;
        for (const p of el.querySelectorAll(":scope > p, :scope > li, :scope > h1, :scope > h2, :scope > h3")) {
          const n = (p.innerText || "").trim().length;
          if (n > 40) score += n;
        }
        if (el.tagName === "MAIN" || el.tagName === "ARTICLE") score *= 2;
        if (score > bestScore) { bestScore = score; best = el; }
      }
      const target = bestScore > 400 ? best : body;
      const text = (target.innerText || "").replace(/\n{3,}/g, "\n\n").trim();
      return { title: document.title, url: location.href, text: text.slice(0, 200000) };
    },
  });
  return res && res.result;
}

// Capture the tab we're leaving (or the one we've sat on long enough), if it
// cleared the dwell bar and we haven't already stored it this visit.
async function maybeCapture(rec) {
  if (!rec || !rec.tabId || rec.captured || !rec.since) return;
  const policy = await fetchPolicy();
  if (!policy || !policy.enabled) return;
  const dwell = Date.now() - rec.since;
  if (dwell < (policy.min_dwell_ms || 20000)) return;
  if (capExcluded(rec.url)) return;
  let tab;
  try { tab = await chrome.tabs.get(rec.tabId); } catch (e) { return; }
  if (tab.incognito) return;              // incognito is never indexed
  if (tab.url !== rec.url) return;        // navigated away mid-flight; next visit gets it

  rec.captured = true;  // set before the await so a concurrent tick can't double-send
  let page;
  try { page = await withTimeout(extractReadable(rec.tabId), 8000, "capture"); }
  catch (e) { rec.captured = false; return; }
  if (!page || !page.text || page.text.length < (policy.min_chars || 500)) return;
  if (capExcluded(page.url)) return;      // re-check the URL the page reports

  try {
    await fetch(`${HANDLE}/ext/index`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        url: page.url, title: page.title, text: page.text, dwell_ms: dwell,
      }),
    });
  } catch (e) { /* server down — the page gets another chance next visit */ }
}

// Move the focus pointer to a new (tab, url). Flushes the one we're leaving.
async function focusOn(tabId, url) {
  if (FOCUS.tabId === tabId && FOCUS.url === url) {
    // Same page we were already on. If the clock was stopped (Chrome had lost
    // focus), restart it — otherwise alt-tabbing away and back to a tab you
    // never actually left would leave `since` at 0, and that page could never
    // accrue dwell again.
    if (!FOCUS.since && !FOCUS.captured) FOCUS.since = Date.now();
    return;
  }
  const leaving = { ...FOCUS };
  FOCUS.tabId = tabId; FOCUS.url = url || ""; FOCUS.since = tabId ? Date.now() : 0;
  FOCUS.captured = false;
  maybeCapture(leaving);
}

async function focusCurrent() {
  try {
    const [t] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
    if (t && t.url) focusOn(t.id, t.url);
  } catch (e) {}
}

// The periodic tick: catches the long read you never navigate away from.
function captureTick() {
  fetchPolicy();
  maybeCapture(FOCUS);
}

// ---- long-poll loop (self-healing) ----------------------------------------
// MV3 suspends idle service workers. An in-flight fetch keeps the worker warm,
// so we keep a poll request parked at almost all times and heal fast: short
// error backoff (no long dead-air gaps), a stall check that restarts a wedged
// loop, and ensurePolling() called from every wake (alarm + tab events).
let lastPollAt = 0;
let looping = false;

async function handleCommand(cmd) {
  let result;
  try { result = await dispatch(cmd); }
  catch (e) { result = { error: String(e) }; }
  try {
    await fetch(`${HANDLE}/ext/result`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: cmd.id, result }),
    });
  } catch (e) {}
}

async function pollLoop() {
  if (looping) return;
  looping = true;
  try {
    while (looping) {
      let cmd = null;
      try {
        const r = await fetch(`${HANDLE}/ext/poll`);
        lastPollAt = Date.now();
        cmd = await r.json();
      } catch (e) {
        await new Promise((res) => setTimeout(res, 1000));
        continue;
      }
      // Dispatch WITHOUT awaiting: the loop re-polls immediately so a fetch is
      // always in flight. That pending request keeps the MV3 worker warm
      // through a slow debugger op (a bare await would let it suspend mid-op).
      if (cmd && cmd.id) handleCommand(cmd);
    }
  } finally {
    looping = false;
  }
}

function ensurePolling() {
  // start if not running, or if a prior loop looks stalled / wedged
  if (!looping || Date.now() - lastPollAt > 35000) {
    looping = false;
    pollLoop();
  }
}

// ---- lifecycle ------------------------------------------------------------
function kick() { syncTabs(); ensurePolling(); recRestore(); focusCurrent(); }

chrome.runtime.onStartup.addListener(kick);
chrome.runtime.onInstalled.addListener(kick);

// 30s alarm is the backstop that revives a suspended worker — and the tick that
// captures a page you're still sitting on and never navigate away from
chrome.alarms.create("handle-keepalive", { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener(() => { kick(); captureTick(); });

// Leaving the browser entirely still counts as finishing a read — flush first.
chrome.windows.onFocusChanged.addListener((winId) => {
  if (winId === chrome.windows.WINDOW_ID_NONE) {
    maybeCapture(FOCUS);
    FOCUS.since = 0;              // stop the clock while Chrome is in the background
  } else {
    focusCurrent();
  }
});

// CDP Network events (recording enrichment) — registered at top level so it
// survives a service-worker wake; gated by REC.on inside the handler.
chrome.debugger.onEvent.addListener(recOnEvent);

// any tab activity both re-syncs AND revives the poll loop
const reSync = () => { syncTabs(); ensurePolling(); };
chrome.tabs.onCreated.addListener(reSync);
chrome.tabs.onRemoved.addListener((id) => {
  reSync();
  // can't extract from a tab that's gone; a long read was already caught by the tick
  if (FOCUS.tabId === id) { FOCUS.tabId = null; FOCUS.since = 0; }
});
chrome.tabs.onUpdated.addListener((id, info) => {
  if (info.status === "complete" || info.title || info.url) reSync();
  // recording skeleton: a real URL change is a navigation (full + SPA pushState)
  if (info.url) recNav(id, info.url);
  // navigating the focused tab ends one read and starts another
  if (info.url && id === FOCUS.tabId) focusOn(id, info.url);
});
chrome.tabs.onActivated.addListener(reSync);
// recording skeleton: follow the user across tabs (records the switch + enriches)
chrome.tabs.onActivated.addListener((info) => { recTabSwitch(info.tabId); });
// passive capture: the tab you switch TO starts a dwell clock; the one you left gets flushed
chrome.tabs.onActivated.addListener(async (info) => {
  try {
    const t = await chrome.tabs.get(info.tabId);
    if (t && t.url) focusOn(t.id, t.url);
  } catch (e) {}
});
if (chrome.tabGroups) {
  chrome.tabGroups.onUpdated.addListener(reSync);
  chrome.tabGroups.onRemoved.addListener(reSync);
}

recRestore();  // in case the worker woke without an onStartup/alarm

kick();
