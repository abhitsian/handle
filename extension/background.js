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
const VERSION = "1.2.1";

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
async function clickIn(tabId, sel, text) {
  await wakeTab(tabId);
  const [res] = await chrome.scripting.executeScript({
    target: { tabId },
    func: (sel, text) => {
      const deep = (root, acc) => {
        let kids; try { kids = root.querySelectorAll("*"); } catch (e) { return acc; }
        for (const el of kids) { acc.push(el); if (el.shadowRoot) deep(el.shadowRoot, acc); }
        return acc;
      };
      const all = deep(document, []);
      let el;
      if (text) {
        const q = (text + "").toLowerCase();
        const ROLES = ["button", "option", "menuitem", "menuitemradio", "menuitemcheckbox", "link", "tab", "treeitem", "listitem"];
        const clickable = all.filter((e) => /^(BUTTON|A)$/.test(e.tagName) || (e.getAttribute && (ROLES.includes(e.getAttribute("role")) || e.hasAttribute("onclick") || e.hasAttribute("tabindex"))) || /^(submit|button)$/.test(e.type || ""));
        const hit = (e) => ((e.innerText || e.value || e.getAttribute("aria-label") || "") + "").trim().toLowerCase().indexOf(q) >= 0;
        // prefer the smallest (most specific) matching node, not a huge container
        el = clickable.filter(hit).sort((a, b) => (a.innerText || "").length - (b.innerText || "").length)[0];
      } else {
        el = document.querySelector(sel) || all.find((e) => { try { return e.matches(sel); } catch (x) { return false; } });
      }
      if (!el) return "NOT_FOUND";
      el.scrollIntoView({ block: "center" });
      el.click();
      return "clicked: " + ((el.innerText || el.value || el.getAttribute("aria-label") || el.tagName) + "").trim().slice(0, 60);
    },
    args: [sel || null, text || null],
  });
  return { value: res.result };
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

async function dispatch(cmd) {
  const { method, params = {} } = cmd;
  const tabId = params.tab_id;
  switch (method) {
    case "read": return await readTab(tabId);
    case "eval": return await evalIn(tabId, params.expression || "");
    case "click": return await clickIn(tabId, params.sel, params.text);
    case "type": return await typeIn(tabId, params.sel, params.text, params.submit);
    case "screenshot": return await withTimeout(screenshot(tabId), 8000, "screenshot");
    case "console": return await withTimeout(consoleLogs(tabId, params.ms || 1800), (params.ms || 1800) + 6000, "console");
    case "ping": return { pong: true, version: VERSION };
    default: return { error: "unknown method: " + method };
  }
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
function kick() { syncTabs(); ensurePolling(); }

chrome.runtime.onStartup.addListener(kick);
chrome.runtime.onInstalled.addListener(kick);

// 30s alarm is the backstop that revives a suspended worker
chrome.alarms.create("handle-keepalive", { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener(kick);

// any tab activity both re-syncs AND revives the poll loop
const reSync = () => { syncTabs(); ensurePolling(); };
chrome.tabs.onCreated.addListener(reSync);
chrome.tabs.onRemoved.addListener(reSync);
chrome.tabs.onUpdated.addListener((id, info) => { if (info.status === "complete" || info.title || info.url) reSync(); });
chrome.tabs.onActivated.addListener(reSync);
if (chrome.tabGroups) {
  chrome.tabGroups.onUpdated.addListener(reSync);
  chrome.tabGroups.onRemoved.addListener(reSync);
}

kick();
