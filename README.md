# Handle

**[🔗 Live site →](https://abhitsian.github.io/handle/)**

**Let your AI coding agent read the Chrome you're already signed into.** Handle
gives your agent your real, logged-in browser as **read-only context** — the
tabs you have open (past the login wall), and the trail behind them (history,
closed tabs, bookmarks, downloads). The other tools that touch the web
(browser-use, Browserbase, ChatGPT's agent) spin up a *fresh, signed-out*
browser; Handle reads the one you actually work in.

Every open tab gets a **stable handle** (`t1`, `t2`…) you reference from a Claude
Code session — read its live content, search across what's open, ask a question
across all of it (cited), save research for later. There's also a board: tabs
grouped into the tasks they belong to, pinned, and flagged when they go
**stale**. The same handle (`t49`) names the tab on the board and in your session.

Ships three ways: a **CLI** (`tab`), a **board** (localhost:4910), and an **MCP
server** ([`mcp/`](mcp/), 25 tools) so any agent uses it as first-class tools —
`claude mcp add handle -- node ~/claude-apps/handle/mcp/server.js`. Local,
read-only, no API key, open source. It reads *your* Chrome — it never drives the
page, and never exposes anyone else's tabs.

## From Claude Code

The `tab` CLI (`tab.py`) is the agent surface; the `/tabs` skill drives it from
any session, so you can just say "summarize the figma tab" or "what am I
looking at."

    tab list                 # every open tab: handle · group · staleness
    tab find figma           # resolve a fuzzy query to handle(s) by title/url
    tab grep "fable"         # search the on-page content of every tab
    tab ask "what do my tabs say about X"   # rank → read relevant tabs → answer, cited
    tab save t29 t30 --as ai-landscape      # capture tab content into a dated bundle for later
    tab bundles                             # list saved research bundles
    tab recall ai-landscape                 # load a saved bundle as context (tabs long closed is fine)
    tab ask "…" --saved                     # ask across open tabs AND saved bundles
    tab read t49             # page text — cached & instant
    tab read t29 t30 t4      # several tabs at once
    tab read t49 --live      # fresh full page, main content (nav stripped)
    tab read t49 --md        # convert the page to Markdown (headings/lists/links/tables)
    tab read t49 --clipboard # read what YOU copied (⌘A ⌘C) — the reliable floor
    tab grab                 # same: read the clipboard
    tab shot t49 [--full]    # screenshot the tab → PNG path(s) the agent reads with vision
    tab active               # the frontmost tab ("what I'm looking at")
    tab open / close / note / group / pin / refresh

    # beyond your open tabs — Chrome's own data (titles + links, never content)
    tab history "datacenter" # query Chrome history; filter the results in plain language
    tab history "x" --searches  # the omnibox search terms you typed
    tab closed               # recently-closed tabs, real titles → tab open <url>
    tab bookmarks "spec"     # search your bookmarks
    tab downloads "report"   # recent downloads → the local file path (then read the file)
    tab journeys "evals"     # Chrome's own topical clustering of your browsing
    tab top                  # most-visited pages

    # sturdier backends (optional)
    tab ext                  # Chrome-extension bridge status (preferred when connected)
    tab console t49          # console logs + errors for a tab (via the extension)
    tab cdp / read --cdp     # opt-in DevTools Protocol read path

`read` is the headline: it pulls a tab's **live, logged-in** content via Chrome
— past auth walls and JS shells that defeat a plain fetch. Default is the
cached snapshot (instant); `--live` reads the full page fresh and extracts the
main article, dropping nav/footer chrome.

`read` is **format-aware** — it routes by what the tab is, and the payload
carries a `kind`:

| Tab | How it's read |
|-----|---------------|
| **HTML** page | main-content text (readability) |
| **Google Doc / Sheet / Slides** | authenticated **export, Markdown-first** (Doc → md, Sheet → md table), past your login |
| **Office Word (SharePoint)** | the real **`.docx` → Markdown** (downloaded + unzipped past the login — headings/lists preserved) |
| **Figma** | **screenshot** → vision (also returns `file_key`/`node_id` for the Figma MCP) |
| **PDF / Office Excel·PPT** | **screenshot** → vision; works for authenticated/internal files (`--full` multi-page) |

`read` is a **cascade**: it tries the best text route for the content
(Markdown-first), falls through when a route returns nothing or just the
editor's chrome, then to a **screenshot** (vision), then to a precise `note` —
never a silent blank. `source` reports which route won. As a reliable floor,
**copy anything (⌘A ⌘C) and `tab read <ref> --clipboard`** (or `tab grab`) — you
copy, Handle reads `pbpaste`. Screenshots need macOS Screen Recording (one-time).

When content comes back empty, the payload's `note` says exactly why (export
blocked, not signed in, JavaScript-from-Apple-Events off, …).

## Beyond your open tabs — Chrome's own data

Handle also reads Chrome's other local stores, so the agent can reach what
you're *no longer* looking at. These return **the pointer, never page content**
— titles, links, file paths, search terms; to get content you `open` the URL
(or read the local file). All read-only off a copy of Chrome's databases; none
touch Login Data, Cookies, or Web Data.

| Command | What it surfaces |
|---|---|
| `tab history "<q>"` | history — narrow with a keyword + `--days`, then filter the rows in plain language. `--closed` drops still-open tabs; `--searches` switches to omnibox search terms |
| `tab closed` | recently-closed tabs reconstructed from the session file, with **real titles** — the precise "reopen the tab I just closed" (`tab open <url>`) |
| `tab bookmarks "<term>"` | your bookmarks |
| `tab downloads "<term>"` | recent downloads resolved to **local file paths** (with a `moved/deleted` flag) — closes the download → reference loop |
| `tab journeys "<term>"` | Chrome's own ML clustering of your browsing into topical sessions |
| `tab top` | most-visited pages, by Chrome's own ranking |

`tab open` now accepts a raw `http(s)://`/`file://` URL too, so anything these
surface can be reopened as a new tab.

## Sturdier backends — the extension & CDP

The default read path is **AppleScript** (zero install). Two optional backends
make it sturdier; both are read-only and Handle falls back automatically when
they aren't there.

**Chrome extension** ([`extension/`](extension/)) — the recommended upgrade.
Install once (no Chrome relaunch, no open debug port — it connects *outbound* to
the board on `127.0.0.1:4910`). When `tab ext` says connected, Handle prefers it
and these get sturdier with no change in how you call them:

- **scan / `list` / `refresh`** via `chrome.tabs` — no Automation permission,
  faster, and it carries your **real native Chrome tab groups** (colored/named).
- **`read --live`** via `chrome.scripting` — no "Allow JavaScript from Apple
  Events" toggle.
- **screenshots** via `chrome.debugger` — **background tabs, no focus race, no
  Screen Recording permission**.
- **`tab console <ref>`** — console logs + errors, which AppleScript can't reach.

Setup: run `python3 app.py`, then `chrome://extensions` → Developer mode → Load
unpacked → the `extension/` folder. See [`extension/README.md`](extension/README.md).

**DevTools Protocol** (`tab cdp`, `tab read --cdp`) — for anyone who already
launches Chrome with `--remote-debugging-port`. Same powers as the extension via
an open port; the extension is the no-friction route to the same thing.

## What the board does

- **Refresh** — reads every open Chrome tab (via the extension when connected,
  else AppleScript), plus when you last visited each URL (from Chrome's own
  history database).
- **Deduce** — in Claude Code, say `deduce my tabs`. Claude groups the tabs into
  named tasks and writes a one-line summary for each. No API key needed — the
  deduction runs through your Claude Code session.
- **Group by Task or Window** — toggle in the header. Task view uses the
  deduced/your groups; Window view groups tabs by which browser window they're
  in (one window = one work context).
- **Pin** — pin a tab so it sticks to the top of its group, gets a 📌 marker,
  and is never flagged stale.
- **Move** — the group menu under any tab moves it to another group, or creates
  a new group on the spot.
- **Annotate** — add your own note to any tab. Your note wins over Claude's
  guess; without one, the deduction stands.
- **Close** — the ✕ button closes the tab in Chrome for real.
- **Queue actions** — the *Run action…* menu under any tab, or the buttons on a
  cluster, queue a job for Claude Code: **Summarize**, **→ Notion task**,
  **Ingest**, or **Synthesize** a whole cluster. Say `run my tab actions` in
  Claude Code to execute them; results show as chips/summaries on the board.
  This is the part a browser extension can't do — the tabs become an agent
  work queue with access to your skills, MCP tools, and logged-in page content.

Tabs you haven't visited in 3+ days are flagged `stale`. A task floats to the
top of the board when at least half its tabs have gone stale.

## Run

    python3 app.py
    # open http://localhost:4910

No dependencies — it runs on the Python standard library alone.

## One-time macOS setup

- **Automation permission** — the first **Refresh** asks permission to control
  Chrome. Allow it (or System Settings → Privacy & Security → Automation).
- **Page content (optional but recommended)** — to let Claude read each tab's
  *content* for sharper deductions, enable Chrome's
  **View → Developer → Allow JavaScript from Apple Events**. Without it,
  deductions fall back to the tab title and URL.

## The deduce loop

Handle does not call any AI API itself. When you want your tabs sorted:

1. Refresh the board (or run `python3 collect.py`).
2. In Claude Code, say **`deduce my tabs`**.
3. Claude reads `state.json`, clusters the tabs, and writes `deductions.json`.
4. Reload the board — tasks appear.

## Files

| File              | Owner        | Holds                                       |
|-------------------|--------------|---------------------------------------------|
| `state.json`      | the app      | tabs, notes, pins, group overrides, timestamps |
| `deductions.json` | Claude Code  | task names, summaries, per-tab one-liners   |
| `actions.json`    | app + Claude Code | queued actions and their results       |
| `collect.py`      | —            | the AppleScript + history collector + extension merge |
| `chrome_data.py`  | —            | read-only views into Chrome's own data (bookmarks, downloads, searches, journeys, most-visited, sessions) |
| `cdp.py`          | —            | stdlib DevTools Protocol client (opt-in)    |
| `extbridge.py`    | —            | in-memory bridge between the board and the extension |
| `extension/`      | —            | the Chrome companion extension (preferred backend) |
