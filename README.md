# Tab Tasks

An on-demand board over your open Chrome tabs. Pull your current tabs whenever
you want, see them grouped into the tasks they belong to, pin the ones that
matter, move tabs between groups, close tabs you're done with, and spot the
tasks that have gone **stale** — so you remember what you started and never
came back to.

## What you can do

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

Tab Tasks does not call any AI API itself. When you want your tabs sorted:

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
