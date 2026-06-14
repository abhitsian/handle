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
const VERSION = "1.0.0";
let polling = false;

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

async function screenshot(tabId) {
  await chrome.debugger.attach({ tabId }, "1.3");
  try {
    const { data } = await chrome.debugger.sendCommand(
      { tabId }, "Page.captureScreenshot", { format: "png", captureBeyondViewport: true });
    return { data }; // base64 PNG
  } finally {
    try { await chrome.debugger.detach({ tabId }); } catch (e) {}
  }
}

async function consoleLogs(tabId, ms = 1800) {
  await chrome.debugger.attach({ tabId }, "1.3");
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
    case "screenshot": return await screenshot(tabId);
    case "console": return await consoleLogs(tabId, params.ms || 1800);
    case "ping": return { pong: true, version: VERSION };
    default: return { error: "unknown method: " + method };
  }
}

// ---- long-poll loop -------------------------------------------------------
async function pollLoop() {
  if (polling) return;
  polling = true;
  while (polling) {
    let cmd = null;
    try {
      const r = await fetch(`${HANDLE}/ext/poll`);
      cmd = await r.json();
    } catch (e) {
      // server down — back off, then retry
      await new Promise((res) => setTimeout(res, 3000));
      continue;
    }
    if (cmd && cmd.id) {
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
  }
}

// ---- lifecycle ------------------------------------------------------------
function kick() { syncTabs(); pollLoop(); }

chrome.runtime.onStartup.addListener(kick);
chrome.runtime.onInstalled.addListener(kick);

// keep the service worker alive + the poll loop running
chrome.alarms.create("handle-keepalive", { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener(() => { syncTabs(); pollLoop(); });

// re-sync whenever the tab set changes
const reSync = () => syncTabs();
chrome.tabs.onCreated.addListener(reSync);
chrome.tabs.onRemoved.addListener(reSync);
chrome.tabs.onUpdated.addListener((id, info) => { if (info.status === "complete" || info.title || info.url) reSync(); });
chrome.tabs.onActivated.addListener(reSync);
if (chrome.tabGroups) {
  chrome.tabGroups.onUpdated.addListener(reSync);
  chrome.tabGroups.onRemoved.addListener(reSync);
}

kick();
