# Handle

**[🔗 Live site →](https://abhitsian.github.io/handle/)**

**Your whole Chrome, one question away — for your AI agent.** Handle makes your
browser queryable in plain language: the signed-in tabs you have open, **and
everything behind them** — your history, the pages you closed, bookmarks,
downloads — searched by meaning, not keywords. Two things nothing else does:

1. **Live & signed-in.** It reads the real, logged-in pages you work in — the
   Google Doc, the Notion page, the dashboard. The other tools that touch the
   web (browser-use, Browserbase, ChatGPT's agent) spin up a *fresh, signed-out*
   browser; Handle reads the one you actually use.
2. **Historical & whole-Chrome.** Your browsing becomes agent-queryable: *"pull
   up that Stratechery piece I read last week,"* *"what did I work on today?"*,
   *"reopen the doc I closed this morning."* Chrome's own history can't search by
   meaning; agentic-browser tools don't touch your history at all.

And it doesn't only read — when you ask, Handle **acts** on a tab too:
**click, type, and watch**, for monitor-and-drive flows. Acting runs a real
injected function in the page's *isolated world*, so it survives a strict
Content-Security-Policy (Okta, Teams, banks) that blocks ordinary scripting.
Local, no API key, open source.

A quick taste (you say it in plain English; the agent runs the tools):

| You say | What happens |
|---|---|
| "Summarize the Google Doc I'm looking at" | reads your signed-in page, past the login |
| "What did I work on today?" | reads your history back as themes |
| "Pull up that TechCrunch article I read last week" | finds it in history by meaning → reopens → reads |
| "Answer this from the six tabs I have open" | reads all six, answers cited by tab |
| "Reopen the doc I closed this morning" | recovers it from the session, reopens it |
| "What errors are on this page?" | console logs/errors (via the extension) |
| "Click into the Activity feed and screenshot it" | clicks the element, even behind a strict CSP |
| "Watch this PR and ping me when checks pass" | polls the live page, fires when your condition is met |

Every open tab also gets a **stable handle** (`t1`, `t2`…) you reference from a
Claude Code session, and there's a board (localhost:4910) that shows the same
handles, grouped into tasks and flagged when **stale**.

Ships three ways: a **CLI** (`tab`), a **board**, and an **MCP server**
([`mcp/`](mcp/), 25 tools) so any agent uses it as first-class tools —
`claude mcp add handle -- node ~/claude-apps/handle/mcp/server.js`. It works on
*your* Chrome — reads and acts only on the tabs you point it at, and never
exposes anyone else's.

## From Claude Code

> **The MCP server is the install** — `claude mcp add handle …` above gives any
> agent the 25 tools as first-class functions; just talk to it. The `tab` CLI
> and `/tabs` skill below are the *optional* local driving surface (handy if you
> want to invoke it yourself, or wire it into your own skills). You don't need
> the skill for the MCP path to work.

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

    # act on a tab — not just read (CSP-safe via an isolated-world injected fn)
    tab click t49 "button.submit"     # click an element (or --text "Save")
    tab type t49 "input#q" "hello"     # type into a field
    tab eval t49 "document.title"      # run JS in the page (MAIN world; a hard CSP may block)
    tab watch t49 --check "<js>" --every 30 --timeout 600 [--click <sel> --say "<msg>"]  # poll → act/notify when it fires

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

## Acting on a tab

Reading is the default, but Handle can also **drive** a tab when you ask — for
monitoring and automation, not just observation:

- **`tab click <ref> "<sel>"`** (or `--text "Save"`) — click an element.
- **`tab type <ref> "<sel>" "<text>"`** — fill a field.
- **`tab watch <ref> --check "<js>" --every N --timeout M`** — poll the live page
  and **act** (`--click`, `--do`) or **notify** (`--say`) the moment your
  condition fires. A self-contained monitor with no API to poll.
- **`tab eval <ref> "<js>"`** — run JS in the page (MAIN world).

`click`/`type` run a **real injected function in the page's isolated world**, so
they survive a strict Content-Security-Policy (Okta, Teams, banks) that blocks
string-`eval`. That's what lets a `/loop` open the Teams Activity feed and
screenshot it on a page where ordinary scripting is refused. Acting goes through
the **extension** (the isolated-world injection lives there); `eval` is MAIN-world,
so a hard CSP can still refuse it. Handle still acts only on the tabs you point
it at, on your own logged-in Chrome.

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

## Under the hood — why it keeps working

One idea runs through the whole design: **read your real, logged-in Chrome — and
always have a backup way in.**

Programs are walled off from your browser by design, so Handle keeps **three
different doors** into Chrome and tries them best-first, degrading the moment one
isn't available:

1. **The extension** *(preferred)* — a helper inside Chrome itself. Permission-free,
   sees your real tabs and groups, takes race-free screenshots, even wakes a
   *discarded* tab before reading it.
2. **AppleScript** *(fallback floor)* — asks macOS to relay to Chrome. Always
   present, zero install; clumsier (toggle-gated JS, needs the tab frontmost).
3. **DevTools / CDP** *(opt-in)* — a debug port you open deliberately; most
   precise, race-free.

Because every live read is this cascade, a read almost never just fails — if the
best door is locked, it walks to the next. (That's exactly why Word docs read
fine even with the "Allow JavaScript from Apple Events" toggle off.)

Once it's in, Handle matches the method to the content — clean article text for
web pages, the document's own file for Google/Office docs, and a **screenshot
read with vision** for designs, PDFs, and slides that have no text to grab.
Separately, it reads **copies** of Chrome's own history/downloads databases —
strictly read-only, pointers only. (Driving a live tab — click/type/watch — is a
separate, explicit capability; see *Acting on a tab* above.)

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
