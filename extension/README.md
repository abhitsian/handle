# Handle companion extension

Makes Handle's view of your tabs sturdier — **without relaunching Chrome and without an open debug port.** It connects *outbound* to Handle's local server (`127.0.0.1:4910`) and does two things:

1. **Pushes your open tabs + native tab groups** to Handle (replaces the AppleScript scan: no Automation permission, faster, and it carries your real colored/named Chrome tab groups).
2. **Answers live commands** — read a tab's text, screenshot it (race-free, even in the background), capture its console logs/errors — using `chrome.scripting` and `chrome.debugger`.

It is **read-only**: it reads content and captures screenshots/console; it never clicks, types, or navigates. It talks to nothing but `127.0.0.1:4910`.

## Why an extension instead of `--remote-debugging-port`

`chrome.debugger` gives the same DevTools Protocol powers as an open debug port, but:

- **no Chrome relaunch** — install once, works forever
- **no open TCP port** any local process could attach to — the extension connects outward
- Chrome shows its own "Handle is debugging this tab" banner while a debugger command runs — a built-in transparency cue

## Install (one time)

1. Start the board so the server is listening:
   ```
   python3 app.py        # serves http://127.0.0.1:4910
   ```
2. Open `chrome://extensions`, turn on **Developer mode** (top right).
3. **Load unpacked** → select this `extension/` folder.
4. Back in a terminal: `python3 tab.py ext` should now say **connected**.

That's it. Handle now prefers the extension for scans, reads, and screenshots, and falls back to AppleScript automatically whenever the extension or board isn't running — nothing breaks either way.

## What it unlocks

| Command | Powered by |
|---|---|
| faster `tab list` / `tab refresh`, **native tab groups** | `chrome.tabs` + `chrome.tabGroups` (no Automation permission) |
| `tab read <ref> --live` | `chrome.scripting` (no "Allow JS from Apple Events" toggle) |
| `tab shot` / Figma·PDF·Office screenshots | `chrome.debugger` `Page.captureScreenshot` (background tabs, no focus race) |
| `tab console <ref>` | `chrome.debugger` Runtime/Log (console logs + errors) |

## Permissions, and why

- `tabs`, `tabGroups` — read the tab list + your native groups
- `scripting` + `<all_urls>` — read a tab's text on request
- `debugger` — screenshots + console (this is what triggers Chrome's banner)
- `http://127.0.0.1:4910/*` — talk to Handle's local server, and nothing else
