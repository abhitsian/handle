const $ = (id) => document.getElementById(id);
const send = (panel, extra = {}) => chrome.runtime.sendMessage({ panel, ...extra }).catch(() => {});
let state = {};
const STATUS = { playing: "PLAYING", paused: "PAUSED", ready: "READY", done: "DONE", error: "STOPPED", none: "NO FLOW" };

function render() {
  const s = state;
  $("pill").className = "pill " + (s.status || "none");
  $("pill").textContent = STATUS[s.status] || "—";
  $("title").textContent = s.title || "—";
  $("group").textContent = s.group || "";
  const n = s.n || 0, i = s.i || 0;
  if (!s.flow) { $("k").textContent = "No flow for this page"; $("t").textContent = "Drive it by hand, or switch tab"; }
  else if (i >= n) { $("k").textContent = `All ${n} steps done`; $("t").textContent = "Next tab ▶ when you're ready"; }
  else { $("k").textContent = `Step ${i + 1} of ${n}${s.status === "playing" ? "" : " · next up"}`; $("t").textContent = s.steps[i]; }
  $("err").textContent = s.error ? `Stopped: ${s.error}. Do it by hand, then Next or Play.` : "";
  const playing = s.status === "playing";
  $("play").textContent = playing ? "⏸ Pause" : "▶ Play";
  $("play").classList.toggle("pause", playing);
  $("steps").innerHTML = "";
  (s.steps || []).forEach((t, k) => {
    const li = document.createElement("li");
    li.className = k < i ? "done" : k === i ? "cur" : "";
    li.innerHTML = `<span class="n">${k + 1}</span><span></span>`;
    li.lastChild.textContent = t;
    li.onclick = () => send("jump", { i: k });
    $("steps").appendChild(li);
  });
  $("tabs").innerHTML = "";
  (s.tabs || []).forEach((t, k) => {
    const li = document.createElement("li");
    li.className = "tab" + (t.active ? " active" : "") + (t.flow ? "" : " noflow");
    li.innerHTML = `<span class="n">${k + 1}</span><span></span><span class="g"></span>`;
    li.children[1].textContent = t.title.replace(/ - ServiceNow.*$| \| ServiceNow.*$/, "");
    li.children[2].textContent = t.group || "";
    li.onclick = () => send("tab", { tabId: t.id });
    $("tabs").appendChild(li);
  });
}

function tick() {
  const s = state;
  const w = s.dwellEnd && s.dwellMs ? Math.max(0, Math.min(1, 1 - (s.dwellEnd - Date.now()) / s.dwellMs)) : 0;
  $("bar").style.width = s.status === "playing" ? `${w * 100}%` : "0";
  requestAnimationFrame(tick);
}

$("play").onclick = () => send(state.status === "playing" ? "pause" : "play");
$("next").onclick = () => send("next");
$("prev").onclick = () => send("prev");
$("stop").onclick = () => send("stop");
$("tprev").onclick = () => send("tab", { delta: -1 });
$("tnext").onclick = () => send("tab", { delta: 1 });
addEventListener("keydown", (e) => {
  const k = { ArrowRight: () => send("next"), ArrowLeft: () => send("prev"), r: () => send("stop"), "[": () => send("tab", { delta: -1 }), "]": () => send("tab", { delta: 1 }) }[e.key];
  if (k) { e.preventDefault(); k(); }
});
chrome.storage.onChanged.addListener((ch, area) => { if (area === "session" && ch.panel) { state = ch.panel.newValue || {}; render(); } });
chrome.storage.session.get("panel").then((r) => { state = r.panel || {}; render(); send("refresh"); });
tick();
