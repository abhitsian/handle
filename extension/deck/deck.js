const img = document.getElementById("s");
function show() {
  const n = parseInt(location.hash.slice(1), 10) || 1;
  img.src = `slide-${String(n).padStart(2, "0")}.jpg`;
  document.title = `Slide ${n}`;
}
addEventListener("hashchange", show);
show();
