#!/usr/bin/env node
'use strict';

/*
 * handle-mcp — reference and read YOUR open Chrome tabs from inside an agent.
 * Zero dependencies. Speaks MCP over stdio (newline-delimited JSON-RPC 2.0).
 *
 * It runs locally and reads the browser on THIS machine: it drives your real,
 * logged-in Chrome, so it reads pages past login walls — and it never exposes
 * anyone else's tabs. A thin wrapper over Handle's Python CLI (tab.py); each
 * tool shells out to `python3 tab.py <cmd> --json`. Screenshots come back as
 * inline images the model reads with vision.
 *
 * Tools: list_tabs, find_tab, grep_tabs, read_tab, screenshot_tab, active_tab,
 *        open_tab, close_tab, note_tab, group_tab, pin_tab, refresh_tabs
 * Requires: macOS + Google Chrome, Python 3 on PATH. For read/screenshot,
 *   Chrome → View → Developer → "Allow JavaScript from Apple Events", and
 *   Screen Recording permission for screenshots.
 */

const fs = require('fs');
const path = require('path');
const { execFileSync } = require('child_process');

const SERVER_NAME = 'handle';
const SERVER_VERSION = '1.0.0';
// tab.py lives one level up from this mcp/ dir; paths inside it are self-relative.
const TAB_PY = path.join(__dirname, '..', 'tab.py');
const PYTHON = process.env.HANDLE_PYTHON || 'python3';
const MAX_IMAGES = 8; // safety cap on a single read/screenshot call

const log = (...a) => process.stderr.write('[handle-mcp] ' + a.join(' ') + '\n');

// ---------- run the CLI ----------
function runTab(args, { json = false } = {}) {
  let out;
  try {
    out = execFileSync(PYTHON, [TAB_PY, ...args], {
      encoding: 'utf8', maxBuffer: 32 * 1024 * 1024, timeout: 60000,
    });
  } catch (e) {
    // tab.py exits non-zero on ambiguous/unknown refs and prints the candidates.
    const msg = (e.stdout || '') + (e.stderr || '') || e.message;
    throw new Error(String(msg).trim());
  }
  if (!json) return out.trim();
  try { return JSON.parse(out); } catch { return out.trim(); }
}

function asArray(x) { return Array.isArray(x) ? x : [x]; }

function imageContent(absPath) {
  const data = fs.readFileSync(absPath).toString('base64');
  return { type: 'image', data, mimeType: 'image/png' };
}

// ---------- tools ----------
function toolListTabs(a) {
  const args = ['list'];
  if (a.group) args.push('--group', String(a.group));
  if (a.stale) args.push('--stale');
  if (a.window != null) args.push('--window', String(a.window));
  if (a.refresh) args.push('--refresh');
  return [{ type: 'text', text: runTab(args) }];
}

function toolFindTab(a) {
  if (!a.query) throw new Error('query is required');
  return [{ type: 'text', text: runTab(['find', String(a.query)]) }];
}

function toolGrepTabs(a) {
  if (!a.query) throw new Error('query is required');
  return [{ type: 'text', text: runTab(['grep', String(a.query)]) }];
}

function toolAskTabs(a) {
  if (!a.question) throw new Error('question is required');
  const args = ['ask', String(a.question)];
  if (a.tabs != null) args.push('--tabs', String(a.tabs));
  if (a.chars != null) args.push('--chars', String(a.chars));
  if (a.saved) args.push('--saved');
  return [{ type: 'text', text: runTab(args) }];
}

function toolSaveTabs(a) {
  const args = ['save'];
  if (Array.isArray(a.refs)) args.push(...a.refs.map(String));
  else if (a.refs) args.push(String(a.refs));
  if (a.group) args.push('--group', String(a.group));
  if (a.all) args.push('--all');
  if (a.as) args.push('--as', String(a.as));
  return [{ type: 'text', text: runTab(args) }];
}
function toolListBundles() { return [{ type: 'text', text: runTab(['bundles']) }]; }
function toolRecallBundle(a) {
  if (!a.name) throw new Error('name is required');
  const args = ['recall', String(a.name)];
  if (a.chars != null) args.push('--chars', String(a.chars));
  return [{ type: 'text', text: runTab(args) }];
}

function readPayloadsToContent(payloads) {
  const content = [];
  let images = 0;
  for (const p of payloads) {
    const title = `# ${p.title}${p.kind && p.kind !== 'html' ? '  ·  ' + p.kind : ''}\n${p.url}`;
    if (p.source === 'screenshot' && Array.isArray(p.shots) && p.shots.length) {
      const note = p.kind === 'figma' && p.file_key
        ? `\n(Figma file ${p.file_key}${p.node_id ? ', node ' + p.node_id : ''} — or use the Figma MCP)` : '';
      content.push({ type: 'text', text: title + note + `\n\n(${p.shots.length} screenshot(s) below)` });
      for (const s of p.shots) {
        if (images >= MAX_IMAGES) break;
        try { content.push(imageContent(s)); images++; } catch (e) { content.push({ type: 'text', text: `(could not read ${s})` }); }
      }
    } else if (p.content) {
      content.push({ type: 'text', text: `${title}\n\n${p.content}` });
    } else {
      content.push({ type: 'text', text: `${title}\n\n(${p.note || 'no content'})` });
    }
  }
  return content;
}

function toolReadTab(a) {
  const refs = a.ref == null ? [] : asArray(a.ref).map(String);
  if (!refs.length) throw new Error('ref is required (a handle like "t7", a number, "active", or a fuzzy name)');
  const args = ['read', ...refs, '--json'];
  if (a.live) args.push('--live');
  if (a.md) args.push('--md');
  if (a.shot) args.push('--shot');
  if (a.full) args.push('--full');
  if (a.clipboard) args.push('--clipboard');
  if (a.cdp) args.push('--cdp');
  if (a.chars != null) args.push('--chars', String(a.chars));
  return readPayloadsToContent(asArray(runTab(args, { json: true })));
}

function toolGrab() {
  return [{ type: 'text', text: runTab(['grab']) }];
}

function toolScreenshotTab(a) {
  if (!a.ref) throw new Error('ref is required');
  const args = ['shot', String(a.ref), '--json'];
  if (a.full) args.push('--full');
  const r = runTab(args, { json: true });
  const shots = (r && r.shots) || [];
  if (!shots.length) return [{ type: 'text', text: `Could not screenshot: ${(r && r.error) || 'unknown error'}` }];
  const content = [{ type: 'text', text: `# ${r.title}\n${r.url}\n\n${shots.length} screenshot(s):` }];
  shots.slice(0, MAX_IMAGES).forEach((s) => { try { content.push(imageContent(s)); } catch {} });
  return content;
}

function toolActiveTab() {
  return [{ type: 'text', text: runTab(['active']) }];
}

function toolOpenTab(a) { if (!a.ref) throw new Error('ref is required'); return [{ type: 'text', text: runTab(['open', String(a.ref)]) }]; }
function toolCloseTab(a) {
  const refs = a.ref == null ? [] : asArray(a.ref).map(String);
  if (!refs.length) throw new Error('ref is required');
  return [{ type: 'text', text: runTab(['close', ...refs]) }];
}
function toolNoteTab(a) { if (!a.ref || !a.text) throw new Error('ref and text are required'); return [{ type: 'text', text: runTab(['note', String(a.ref), String(a.text)]) }]; }
function toolGroupTab(a) { if (!a.ref || !a.name) throw new Error('ref and name are required'); return [{ type: 'text', text: runTab(['group', String(a.ref), String(a.name)]) }]; }
function toolPinTab(a) { if (!a.ref) throw new Error('ref is required'); return [{ type: 'text', text: runTab([a.pin === false ? 'unpin' : 'pin', String(a.ref)]) }]; }
function toolRefreshTabs() { return [{ type: 'text', text: runTab(['refresh']) }]; }

// ---- Chrome's own data (pointer, not page content) ----
function toolHistory(a) {
  const args = ['history'];
  if (a.query) args.push(String(a.query));
  if (a.days != null) args.push('--days', String(a.days));
  if (a.hours != null) args.push('--hours', String(a.hours));
  if (a.limit != null) args.push('--limit', String(a.limit));
  if (a.closed) args.push('--closed');
  if (a.searches) args.push('--searches');
  return [{ type: 'text', text: runTab(args) }];
}
function toolBookmarks(a) {
  const args = ['bookmarks'];
  if (a.query) args.push(String(a.query));
  if (a.limit != null) args.push('--limit', String(a.limit));
  return [{ type: 'text', text: runTab(args) }];
}
function toolDownloads(a) {
  const args = ['downloads'];
  if (a.query) args.push(String(a.query));
  if (a.days != null) args.push('--days', String(a.days));
  if (a.limit != null) args.push('--limit', String(a.limit));
  return [{ type: 'text', text: runTab(args) }];
}
function toolJourneys(a) {
  const args = ['journeys'];
  if (a.query) args.push(String(a.query));
  if (a.days != null) args.push('--days', String(a.days));
  if (a.limit != null) args.push('--limit', String(a.limit));
  return [{ type: 'text', text: runTab(args) }];
}
function toolTop(a) {
  const args = ['top'];
  if (a.limit != null) args.push('--limit', String(a.limit));
  return [{ type: 'text', text: runTab(args) }];
}
function toolClosed(a) {
  const args = ['closed'];
  if (a.limit != null) args.push('--limit', String(a.limit));
  return [{ type: 'text', text: runTab(args) }];
}
function toolConsole(a) {
  if (!a.ref) throw new Error('ref is required');
  const args = ['console', String(a.ref)];
  if (a.ms != null) args.push('--ms', String(a.ms));
  return [{ type: 'text', text: runTab(args) }];
}
function toolExtStatus() { return [{ type: 'text', text: runTab(['ext']) }]; }

const REF_DESC = 'A tab reference: a handle ("t7"), a bare number ("7"), "active" (the frontmost tab — what the user is looking at), or a fuzzy term matched against title/url/group/note ("figma", "the spec doc").';

const TOOLS = [
  {
    name: 'list_tabs',
    description:
      'List the user\'s open Chrome tabs, each with a stable handle (t1, t2…), its group, and a stale flag. ' +
      'Use to see what the user has open before reading or acting. Reads cached state (fast); pass refresh:true ' +
      'if tabs may have changed since the last scan.',
    inputSchema: { type: 'object', properties: {
      group: { type: 'string', description: 'Only tabs whose group matches.' },
      stale: { type: 'boolean', description: 'Only tabs idle 3+ days.' },
      window: { type: 'number', description: 'Only tabs in this browser window.' },
      refresh: { type: 'boolean', description: 'Re-scan Chrome before listing.' },
    } },
  },
  {
    name: 'find_tab',
    description:
      'Resolve a fuzzy description to tab handle(s) by title/url/group/note. Use when the user names a tab ' +
      '("the figma tab", "the stratechery one") and you need its handle before reading. Returns matches; if ' +
      'several match, show them and ask which.',
    inputSchema: { type: 'object', properties: { query: { type: 'string', description: 'Fuzzy term to match.' } }, required: ['query'] },
  },
  {
    name: 'grep_tabs',
    description:
      'Search the actual ON-PAGE content of every open tab (not just titles). Use for "which tab mentions X", ' +
      '"where was I reading about Y". Returns matching tabs with excerpts.',
    inputSchema: { type: 'object', properties: { query: { type: 'string', description: 'Text to search for across tab contents.' } }, required: ['query'] },
  },
  {
    name: 'ask_tabs',
    description:
      'Answer a question from the user\'s OPEN TABS. Ranks every open tab by the question\'s terms, reads the ' +
      'most relevant ones as clean content, and returns a cited bundle (each source tagged with its handle). ' +
      'Use for "answer X from my open tabs", "what do my tabs say about Y", "across what I have open, …". Then ' +
      'write the answer and CITE the tabs by handle (t3, t7); discount weak/irrelevant matches. Cross-tab and ' +
      'low-token — the thing single-tab browser tools can\'t do.',
    inputSchema: { type: 'object', properties: {
      question: { type: 'string', description: 'The question, in plain words.' },
      tabs: { type: 'number', description: 'Max tabs to pull (default 5).' },
      chars: { type: 'number', description: 'Max characters per source (default 4000).' },
      saved: { type: 'boolean', description: 'Also search saved research bundles (capture→recall→ask), not just open tabs.' },
    }, required: ['question'] },
  },
  {
    name: 'save_tabs',
    description:
      'Capture the content of open tabs into a saved, dated research bundle, so it survives the tabs being ' +
      'closed and the session ending. Use when the user says "save these tabs", "keep this research", "store ' +
      'this for later". Give refs (or group/all) and a label. Later: recall_bundle, or ask_tabs with saved:true.',
    inputSchema: { type: 'object', properties: {
      refs: { description: 'Tab references to save. May be an array.', anyOf: [{ type: 'string' }, { type: 'array', items: { type: 'string' } }] },
      group: { type: 'string', description: 'Save all tabs in this group instead.' },
      all: { type: 'boolean', description: 'Save every open tab.' },
      as: { type: 'string', description: 'Label for the bundle, e.g. "vendor-research".' },
    } },
  },
  {
    name: 'list_bundles',
    description: 'List saved research bundles (label, tab count, date). Use to see what past research is stored.',
    inputSchema: { type: 'object', properties: {} },
  },
  {
    name: 'recall_bundle',
    description: 'Load a saved research bundle\'s captured content as context — even after the tabs are long closed. Match by label or a substring.',
    inputSchema: { type: 'object', properties: {
      name: { type: 'string', description: 'Bundle label or a substring of it.' },
      chars: { type: 'number', description: 'Max characters per source (0 = full).' },
    }, required: ['name'] },
  },
  {
    name: 'read_tab',
    description:
      'Read the live, logged-in content of the user\'s open tab(s) — past login walls and JS that defeat a plain ' +
      'fetch. THE primary tool when the user refers to something in their browser. Format-aware: HTML returns ' +
      'text (md:true for structured Markdown); Google Docs/Sheets/Slides return their authenticated export; ' +
      'Figma/PDF/Office (and shot:true on anything) return screenshot image(s) you read with vision. Pass an ' +
      'array of refs to read several tabs at once.',
    inputSchema: { type: 'object', properties: {
      ref: { description: REF_DESC + ' May be an array to read multiple tabs.', anyOf: [{ type: 'string' }, { type: 'array', items: { type: 'string' } }] },
      live: { type: 'boolean', description: 'Read the live page now instead of the cached snapshot.' },
      md: { type: 'boolean', description: 'Return Markdown (headings/lists/links/tables) — best for text-heavy HTML.' },
      shot: { type: 'boolean', description: 'Screenshot the tab instead of reading text (returns images).' },
      full: { type: 'boolean', description: 'With a screenshot: scroll and capture the whole page.' },
      clipboard: { type: 'boolean', description: 'Read the macOS clipboard instead — the user copies (⌘A ⌘C), you grab it. The reliable floor when in-page reads fail.' },
      cdp: { type: 'boolean', description: 'Read via the opt-in DevTools Protocol (needs an existing --remote-debugging-port). The extension is the no-friction alternative.' },
      chars: { type: 'number', description: 'Max characters of text per tab.' },
    }, required: ['ref'] },
  },
  {
    name: 'grab_clipboard',
    description:
      'Read the macOS clipboard — the human-assisted read rung. When an in-page read can\'t get clean content ' +
      '(a stubborn editor, a format that won\'t extract), have the user copy it (⌘A then ⌘C — no focus race) and ' +
      'call this to grab it. Returns whatever they copied.',
    inputSchema: { type: 'object', properties: {} },
  },
  {
    name: 'screenshot_tab',
    description:
      'Capture the tab as image(s) for you to read with vision — the catch-all for anything the DOM can\'t give ' +
      '(Figma, PDFs, Office viewers, charts, dashboards). Works on authenticated/internal pages since it\'s the ' +
      'on-screen tab. full:true scrolls and captures the whole page. Needs macOS Screen Recording permission.',
    inputSchema: { type: 'object', properties: {
      ref: { type: 'string', description: REF_DESC },
      full: { type: 'boolean', description: 'Scroll and capture the whole page across several images.' },
    }, required: ['ref'] },
  },
  {
    name: 'active_tab',
    description: 'The frontmost tab — "what the user is looking at right now". Returns its handle, title, and url.',
    inputSchema: { type: 'object', properties: {} },
  },
  {
    name: 'open_tab',
    description: 'Bring a tab to the front in Chrome — or open a raw http(s)/file URL as a new tab (e.g. a url from history/closed/bookmarks/downloads).',
    inputSchema: { type: 'object', properties: { ref: { type: 'string', description: REF_DESC + ' Or a raw http(s)://file:// URL to open as a new tab.' } }, required: ['ref'] },
  },
  {
    name: 'close_tab',
    description: 'Close tab(s) in Chrome for real. Confirm with the user before closing more than a couple.',
    inputSchema: { type: 'object', properties: { ref: { description: REF_DESC + ' May be an array.', anyOf: [{ type: 'string' }, { type: 'array', items: { type: 'string' } }] } }, required: ['ref'] },
  },
  {
    name: 'note_tab',
    description: 'Attach a note to a tab (wins over the auto-deduced label).',
    inputSchema: { type: 'object', properties: { ref: { type: 'string', description: REF_DESC }, text: { type: 'string', description: 'The note.' } }, required: ['ref', 'text'] },
  },
  {
    name: 'group_tab',
    description: 'Move a tab into a named group.',
    inputSchema: { type: 'object', properties: { ref: { type: 'string', description: REF_DESC }, name: { type: 'string', description: 'Group name.' } }, required: ['ref', 'name'] },
  },
  {
    name: 'pin_tab',
    description: 'Pin (or unpin) a tab so it stays at the top of its group and is never flagged stale.',
    inputSchema: { type: 'object', properties: { ref: { type: 'string', description: REF_DESC }, pin: { type: 'boolean', description: 'true to pin (default), false to unpin.' } }, required: ['ref'] },
  },
  {
    name: 'refresh_tabs',
    description: 'Re-scan Chrome into Handle\'s state (run if the user opened/closed tabs since the last scan).',
    inputSchema: { type: 'object', properties: {} },
  },
  {
    name: 'history',
    description:
      'Query the user\'s Chrome history — titles + links, NEVER page content. The pointer to where they\'ve ' +
      'been, so you can find a page they no longer have open. Give a keyword to prefilter and a wider --days ' +
      'window, then filter the returned rows yourself by MEANING (the keyword just narrows the haystack). To ' +
      'read a hit, open_tab its url then read_tab. closed:true drops still-open tabs ("find the tab I closed"); ' +
      'searches:true returns omnibox search terms the user typed instead of pages.',
    inputSchema: { type: 'object', properties: {
      query: { type: 'string', description: 'Keyword prefilter (every word must appear in title/url). Keep it broad.' },
      days: { type: 'number', description: 'How far back to look (default 7).' },
      hours: { type: 'number', description: 'Window in hours (overrides days).' },
      limit: { type: 'number', description: 'Max rows (default 40).' },
      closed: { type: 'boolean', description: 'Exclude currently-open tabs.' },
      searches: { type: 'boolean', description: 'Return omnibox search terms instead of visited pages.' },
    } },
  },
  {
    name: 'closed',
    description:
      'Recently-closed tabs reconstructed from Chrome\'s session file, with their real titles — the precise ' +
      '"reopen the tab I just closed". Returns titles + urls (not content); open_tab a url to bring it back.',
    inputSchema: { type: 'object', properties: { limit: { type: 'number', description: 'Max rows (default 40).' } } },
  },
  {
    name: 'bookmarks',
    description: 'Search the user\'s Chrome bookmarks (titles + links). Use for "open my bookmarked X" → then open_tab the url.',
    inputSchema: { type: 'object', properties: {
      query: { type: 'string', description: 'Filter by title/url/folder.' },
      limit: { type: 'number', description: 'Max rows (default 200).' },
    } },
  },
  {
    name: 'downloads',
    description:
      'Recent downloads resolved to LOCAL FILE PATHS (with a moved/deleted flag). Closes the download→reference ' +
      'loop: "read the file I just downloaded" → find it here → read the returned path with your file reader.',
    inputSchema: { type: 'object', properties: {
      query: { type: 'string', description: 'Filter by filename/source url.' },
      days: { type: 'number', description: 'How far back (default 30).' },
      limit: { type: 'number', description: 'Max rows (default 40).' },
    } },
  },
  {
    name: 'journeys',
    description:
      'Chrome\'s own ML clustering of the user\'s browsing into topical sessions (a ready-made research trail). ' +
      'Each journey is a label + the pages in it. Use for "pull up my research on X" without re-deriving clusters.',
    inputSchema: { type: 'object', properties: {
      query: { type: 'string', description: 'Filter by keyword/url.' },
      days: { type: 'number', description: 'How far back (default 30).' },
      limit: { type: 'number', description: 'Max journeys (default 25).' },
    } },
  },
  {
    name: 'most_visited',
    description: 'The user\'s most-visited pages by Chrome\'s own ranking — "what sites do I live in".',
    inputSchema: { type: 'object', properties: { limit: { type: 'number', description: 'Max rows (default 25).' } } },
  },
  {
    name: 'console',
    description:
      'Console logs + errors for a tab — "what\'s broken on this page". Needs the Handle Chrome extension ' +
      'connected (it uses chrome.debugger); if it isn\'t, the result says how to set it up. Captures output ' +
      'AFTER attach, so to catch load-time errors the user should reload the tab first.',
    inputSchema: { type: 'object', properties: {
      ref: { type: 'string', description: REF_DESC },
      ms: { type: 'number', description: 'Capture window in ms (default 1800).' },
    }, required: ['ref'] },
  },
  {
    name: 'ext_status',
    description:
      'Status of the Handle Chrome-extension bridge — the sturdier backend (permission-free scan, native tab ' +
      'groups, race-free screenshots, console). Reports connected/not, and if not, how to install it. Reads/' +
      'scans/screenshots prefer the extension automatically when connected and fall back to AppleScript otherwise.',
    inputSchema: { type: 'object', properties: {} },
  },
];

const HANDLERS = {
  list_tabs: toolListTabs, find_tab: toolFindTab, grep_tabs: toolGrepTabs, ask_tabs: toolAskTabs,
  save_tabs: toolSaveTabs, list_bundles: toolListBundles, recall_bundle: toolRecallBundle,
  grab_clipboard: toolGrab,
  read_tab: toolReadTab, screenshot_tab: toolScreenshotTab, active_tab: toolActiveTab,
  open_tab: toolOpenTab, close_tab: toolCloseTab, note_tab: toolNoteTab,
  group_tab: toolGroupTab, pin_tab: toolPinTab, refresh_tabs: toolRefreshTabs,
  history: toolHistory, closed: toolClosed, bookmarks: toolBookmarks, downloads: toolDownloads,
  journeys: toolJourneys, most_visited: toolTop, console: toolConsole, ext_status: toolExtStatus,
};

// ---------- JSON-RPC / MCP stdio loop ----------
function send(obj) { process.stdout.write(JSON.stringify(obj) + '\n'); }

function handle(msg) {
  const { id, method, params } = msg;
  if (method === 'initialize') {
    send({ jsonrpc: '2.0', id, result: {
      protocolVersion: (params && params.protocolVersion) || '2025-06-18',
      capabilities: { tools: {} },
      serverInfo: { name: SERVER_NAME, version: SERVER_VERSION },
    } });
    return;
  }
  if (method && method.startsWith('notifications/')) return;
  if (method === 'ping') { send({ jsonrpc: '2.0', id, result: {} }); return; }
  if (method === 'tools/list') { send({ jsonrpc: '2.0', id, result: { tools: TOOLS } }); return; }

  if (method === 'tools/call') {
    const name = params && params.name;
    const args = (params && params.arguments) || {};
    const fn = HANDLERS[name];
    if (!fn) { send({ jsonrpc: '2.0', id, error: { code: -32602, message: 'Unknown tool: ' + name } }); return; }
    try {
      const content = fn(args);
      send({ jsonrpc: '2.0', id, result: { content } });
    } catch (e) {
      send({ jsonrpc: '2.0', id, result: { content: [{ type: 'text', text: 'Error: ' + e.message }], isError: true } });
    }
    return;
  }

  if (id !== undefined) send({ jsonrpc: '2.0', id, error: { code: -32601, message: 'Method not found: ' + method } });
}

let buf = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', (d) => {
  buf += d;
  let nl;
  while ((nl = buf.indexOf('\n')) !== -1) {
    const line = buf.slice(0, nl);
    buf = buf.slice(nl + 1);
    if (!line.trim()) continue;
    let msg;
    try { msg = JSON.parse(line); } catch { continue; }
    try { handle(msg); } catch (e) { log('handler error:', e.message); }
  }
});
process.stdin.on('end', () => process.exit(0));

log('ready (v' + SERVER_VERSION + ') — tab.py: ' + TAB_PY);
