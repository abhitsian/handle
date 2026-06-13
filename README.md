# Handle

**[🔗 Live site →](https://abhitsian.github.io/handle/)**

Give every open Chrome tab a **stable handle** (`t1`, `t2`…) you can reference
from a Claude Code session — then read its live content, search across what's
open, and act on it. There's also a board: see tabs grouped into the tasks they
belong to, pin the ones that matter, and spot the ones that have gone **stale**.

The same handle (`t49`) names the tab on the board and in your session.

Ships three ways: a **CLI** (`tab`), a **board** (localhost:4910), and an **MCP
server** ([`mcp/`](mcp/)) so any agent can use it as first-class tools —
`claude mcp add handle -- node ~/claude-apps/handle/mcp/server.js`. It runs
locally and reads *your* Chrome; it never exposes anyone else's tabs.

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

## What the board does

- **Refresh** — reads every open Chrome tab via AppleScript, plus when you last
  visited each URL (from Chrome's own history database).
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
| `collect.py`      | —            | the AppleScript + history collector         |
