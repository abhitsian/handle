# Handle

Give every open Chrome tab a **stable handle** (`t1`, `t2`…) you can reference
from a Claude Code session — then read its live content, search across what's
open, and act on it. There's also a board: see tabs grouped into the tasks they
belong to, pin the ones that matter, and spot the ones that have gone **stale**.

The same handle (`t49`) names the tab on the board and in your session.

## From Claude Code

The `tab` CLI (`tab.py`) is the agent surface; the `/tabs` skill drives it from
any session, so you can just say "summarize the figma tab" or "what am I
looking at."

    tab list                 # every open tab: handle · group · staleness
    tab find figma           # resolve a fuzzy query to handle(s) by title/url
    tab grep "fable"         # search the on-page content of every tab
    tab read t49             # page text — cached & instant
    tab read t29 t30 t4      # several tabs at once
    tab read t49 --live      # fresh full page, main content (nav stripped)
    tab read t49 --md        # convert the page to Markdown (headings/lists/links/tables)
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
| **Google Doc / Sheet / Slides** | the authenticated **export** (txt / CSV / txt), pulled fresh past your login via an in-tab request |
| **Figma** | **screenshot** → read with vision (also returns `file_key`/`node_id` for the Figma MCP if you want structured design data) |
| **PDF** | **screenshot** → read with vision; works for authenticated/internal PDFs (`--full` for multi-page) |
| **Office / SharePoint** | **screenshot** → read with vision |

Anything with no DOM text (Figma/PDF/Office) — or any tab with `read --shot` —
is captured as image(s) the agent reads with vision (`tab shot <ref>`, `--full`
to scroll the whole page). Needs macOS Screen Recording permission (one-time).

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
