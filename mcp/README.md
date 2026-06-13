# handle-mcp

An MCP server that lets an agent reference and read **your own** open Chrome
tabs — the tab you're looking at, a tab you name, a tab by what's on the page —
and pull its live, logged-in content straight into the conversation.

It runs **locally** and reads the browser on **this machine**: it drives your
real Chrome, so it reads pages past login walls and JavaScript that defeat a
plain fetch. It never exposes anyone else's tabs — each person runs it against
their own browser.

Zero npm dependencies. A thin Node wrapper over Handle's Python CLI
(`../tab.py`); screenshots are returned as inline images the model reads with
vision.

## Tools

| Tool | What it does |
|------|--------------|
| `list_tabs` | open tabs with stable handles (t1, t2…), groups, stale flags |
| `find_tab` | resolve a fuzzy name → handle (title/url/group/note) |
| `grep_tabs` | search the on-page content of every tab |
| `ask_tabs` | answer a question across open tabs (and saved bundles) — ranks, reads, returns a cited bundle |
| `save_tabs` | capture tab content into a dated research bundle for later |
| `list_bundles` / `recall_bundle` | list saved bundles · load one back as context |
| `read_tab` | read tab(s): HTML text / Markdown (`md`) / Google export / Figma·PDF·Office screenshot |
| `screenshot_tab` | capture a tab as image(s) to read with vision (`full` for whole page) |
| `active_tab` | the frontmost tab — "what I'm looking at" |
| `open_tab` / `close_tab` / `note_tab` / `group_tab` / `pin_tab` / `refresh_tabs` | act on tabs |

`read_tab` is format-aware: text where text exists (lossless, cheap), pixels
where it doesn't (Figma, PDFs, Office — read with vision).

## Requirements

- **macOS** + **Google Chrome**
- **Node ≥ 18** and **Python 3** on PATH
- For reading page content: Chrome → **View → Developer → Allow JavaScript from
  Apple Events**
- For screenshots: **Screen Recording** permission for the terminal/app running
  the agent (System Settings → Privacy & Security → Screen Recording)

## Install

```bash
git clone https://github.com/abhitsian/handle.git ~/claude-apps/handle
```

Register the server (Claude Code):

```bash
claude mcp add handle -- node ~/claude-apps/handle/mcp/server.js
```

Or add it to your MCP client config:

```json
{
  "mcpServers": {
    "handle": { "command": "node", "args": ["/absolute/path/to/handle/mcp/server.js"] }
  }
}
```

Then just talk to your tabs: *"what am I looking at?"*, *"summarize the figma
tab"*, *"which tab mentions the migration — read it"*, *"pull in these three
tabs as markdown."*

## Notes

- The CLI it wraps (`tab.py`) is pure Python stdlib — no API key, no install.
- Set `HANDLE_PYTHON` to use a specific Python interpreter.
- There's also a board UI (`python3 ../app.py`, localhost:4910) showing the same
  handles. See the [main README](../README.md).
