// Options: link this browser to another computer's Handle.
// Relay (default): paste the pairing code from `tab devices pair`. Direct: a
// tailnet server address + token from `tab devices pair --tailnet`.
// Stored in chrome.storage.local.remote; background.js picks it up on change.
const $ = (id) => document.getElementById(id);
const say = (msg, ok) => { $("status").textContent = msg; $("status").className = ok ? "ok" : "bad"; };
const NAME_RE = /^[a-z0-9][a-z0-9-]{0,31}$/;

function parseCode(code) {
  const m = code.trim().match(/^handle1:([A-Za-z0-9_-]+)$/);
  if (!m) return null;
  try {
    const j = JSON.parse(new TextDecoder().decode(E2E.unb64u(m[1])));
    return j.u && j.r && j.t && j.k ? { url: j.u, room: j.r, token: j.t, key: j.k } : null;
  } catch (e) { return null; }
}

async function load() {
  const { remote } = await chrome.storage.local.get("remote");
  if (!remote) return;
  $("device").value = remote.device || "";
  $("local").checked = remote.localServer !== false;
  if (remote.mode !== "relay") { $("url").value = remote.url || ""; $("token").value = remote.token || ""; }
  say(`Linked as "${remote.device}"` + (remote.mode === "relay" ? " through the relay." : ` to ${remote.url}.`), true);
}

async function testRelay(r, device) {
  try {
    const sealed = await E2E.seal(r.key, JSON.stringify({ device, kind: "hello", body: {} }));
    const res = await fetch(`${r.url.replace(/\/+$/, "")}/r/${r.room}/up`, {
      method: "POST", headers: { Authorization: "Bearer " + r.token, "Content-Type": "text/plain" }, body: sealed,
    });
    if (res.status === 403 || res.status === 401) return "The relay refused this code. Run `tab devices pair` again for a fresh one.";
    return res.ok ? "" : `The relay answered ${res.status}.`;
  } catch (e) { return "Could not reach the relay. Check this computer's internet connection."; }
}

async function testDirect(r) {
  try {
    const res = await fetch(r.url.replace(/\/+$/, "") + "/ext/hello", {
      headers: { "X-Handle-Token": r.token, "X-Handle-Device": r.device },
    });
    if (res.status === 403) return "The server refused the token or device name.";
    return res.ok ? "" : `The server answered ${res.status}.`;
  } catch (e) { return "Could not reach the server. Is Tailscale up on both computers?"; }
}

$("save").addEventListener("click", async () => {
  const device = $("device").value.trim().toLowerCase();
  if (!NAME_RE.test(device)) return say("Device name: lowercase letters, digits and dashes.", false);
  const localServer = $("local").checked;
  let remote;
  if ($("code").value.trim()) {
    const r = parseCode($("code").value);
    if (!r) return say("That doesn't look like a pairing code (it starts with handle1:).", false);
    remote = { mode: "relay", ...r, device, localServer };
    say("Testing…", true);
    const err = await testRelay(remote, device);
    if (err) return say(err, false);
  } else {
    remote = { url: $("url").value.trim(), token: $("token").value.trim(), device, localServer };
    if (!/^https?:\/\/[^/]+/.test(remote.url) || !remote.token) return say("Paste a pairing code, or a server address and token.", false);
    say("Testing…", true);
    const err = await testDirect(remote);
    if (err) return say(err, false);
  }
  await chrome.storage.local.set({ remote });
  $("code").value = "";
  say(`Linked as "${device}". On the other computer: tab --device ${device} list`, true);
});

$("unlink").addEventListener("click", async () => {
  await chrome.storage.local.remove("remote");
  ["code", "url", "token", "device"].forEach((id) => { $(id).value = ""; });
  say("Unlinked. This browser reports only to its own computer.", true);
});

load();
