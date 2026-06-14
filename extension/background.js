// Handle companion extension — connects OUTBOUND to Handle's local server.
//
// Two jobs:
//   1. push the current tabs + native tab groups to /ext/sync (replaces the
//      AppleScript scan: no Automation permission, includes real groups)
//   2. long-poll /ext/poll for live commands (read / screenshot / console /
//      eval) and return each result to /ext/result
//
// Read-only: it reads tab content and captures screenshots/console; it never
// clicks, types, or navigates. It talks to nothing but 127.0.0.1:4910.

const HANDLE = "http://127.0.0.1:4910";
const VERSION = "1.0.1";

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
async function readTab(tabId) {
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
