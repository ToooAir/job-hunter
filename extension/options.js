"use strict";

const DEFAULTS = { base: "http://127.0.0.1:8531", token: "" };
const $ = (id) => document.getElementById(id);

chrome.storage.local.get(DEFAULTS).then((c) => {
  $("base").value = c.base;
  $("token").value = c.token;
});

async function save() {
  await chrome.storage.local.set({
    base: $("base").value.trim(),
    token: $("token").value.trim(),
  });
}

$("save").addEventListener("click", async () => {
  await save();
  $("out").textContent = "saved.";
});

$("test").addEventListener("click", async () => {
  await save();
  $("out").textContent = "testing…";
  const r = await chrome.runtime.sendMessage({ type: "pending" });
  $("out").textContent =
    r && r.ok ? "OK — " + r.data.length + " pending drafts" : "error: " + (r && r.error);
});

// ── draft liveness sweep ─────────────────────────────────────────────────────
// The sweep itself runs in the background worker (daily, or on this button);
// this page only starts one and mirrors its log from storage.
function renderSweep(run) {
  if (!run) return;
  $("sweep-out").textContent =
    `${run.started}${run.dry_run ? " (dry run)" : ""}\n` + run.lines.join("\n");
}

chrome.storage.local.get("sweepRun").then((c) => renderSweep(c.sweepRun));
chrome.storage.onChanged.addListener((ch) => {
  if (ch.sweepRun) renderSweep(ch.sweepRun.newValue);
});

$("sweep").addEventListener("click", async () => {
  const r = await chrome.runtime.sendMessage({ type: "sweep", dry_run: $("dry").checked });
  if (!r || !r.ok) $("sweep-out").textContent = "error: " + (r && r.error);
});
