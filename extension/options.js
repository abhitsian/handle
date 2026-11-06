// Options: link this browser to another computer's Handle (a "hub").
// Stored in chrome.storage.local.remote; background.js picks it up on change.
const $ = (id) => document.getElementById(id);
const say = (msg, ok) => { $("status").textContent = msg; $("status").className = ok ? "ok" : "bad"; };

async function load() {
  const { remote } = await chrome.storage.local.get("remote");
  if (!remote) return;
  $("url").value = remote.url || "";
  $("token").value = remote.token || "";
  $("device").value = remote.device || "";
  $("local").checked = remote.localServer !== false;
  say(`Linked as "${remote.device}" to ${remote.url}.`, true);
}

async function test(remote) {
  try {
    const r = await fetch(remote.url.replace(/\/+$/, "") + "/ext/hello", {
      headers: { "X-Handle-Token": remote.token, "X-Handle-Device": remote.device },
    });
    if (r.status === 403) return "The server refused the token or device name.";
    if (!r.ok) return `The server answered ${r.status}.`;
    return "";
  } catch (e) {
    return "Could not reach the server. Is Tailscale up on both computers, and was `tab devices pair` run there?";
  }
}

$("save").addEventListener("click", async () => {
  const remote = {
    url: $("url").value.trim(),
    token: $("token").value.trim(),
    device: $("device").value.trim().toLowerCase(),
    localServer: $("local").checked,
  };
  if (!/^https?:\/\/[^/]+/.test(remote.url)) return say("Server must look like http://100.x.y.z:4910", false);
  if (!/^[a-z0-9][a-z0-9-]{0,31}$/.test(remote.device)) return say("Device name: lowercase letters, digits and dashes.", false);
  if (!remote.token) return say("Paste the token from `tab devices pair`.", false);
  say("Testing…", true);
  const err = await test(remote);
  if (err) return say(err, false);
  await chrome.storage.local.set({ remote });
  say(`Linked as "${remote.device}". On the other computer: tab --device ${remote.device} list`, true);
});

$("unlink").addEventListener("click", async () => {
  await chrome.storage.local.remove("remote");
  ["url", "token", "device"].forEach((id) => { $(id).value = ""; });
  say("Unlinked. This browser reports only to its own computer.", true);
});

load();
