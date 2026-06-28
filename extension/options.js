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
