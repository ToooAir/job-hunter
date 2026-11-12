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
// Runs here, not in the service worker: MV3 kills an idle worker after ~30 s,
// and this loop spends most of its time waiting. The extension only renders
// and reports; the verdict is the server's (utils/draft_liveness.py).
const HOST_GAP_MS = 8000;  // per-site pacing: a human opening tabs, not a crawler
const LOAD_TIMEOUT_MS = 25000;
const RENDER_WAIT_MS = 4000;  // SPAs paint the "expired" notice after load

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const hostOf = (url) => { try { return new URL(url).host; } catch (_e) { return ""; } };

function tabLoaded(tabId) {
  return new Promise((resolve) => {
    const done = () => {
      chrome.tabs.onUpdated.removeListener(onUpd);
      resolve();
    };
    const onUpd = (id, info) => { if (id === tabId && info.status === "complete") done(); };
    chrome.tabs.onUpdated.addListener(onUpd);
    setTimeout(done, LOAD_TIMEOUT_MS);
  });
}

// What the page looks like in this browser. A tab we cannot read (error page,
// blocked scheme) reports empty — the server flags that, never kills on it.
async function observe(url) {
  const tab = await chrome.tabs.create({ url, active: false });
  try {
    await tabLoaded(tab.id);
    await sleep(RENDER_WAIT_MS);
    const [res] = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      func: () => ({
        status: (performance.getEntriesByType("navigation")[0] || {}).responseStatus || null,
        final_url: location.href,
        title: document.title,
        text: (document.body ? document.body.innerText : "").slice(0, 20000),
      }),
    });
    return res.result;
  } catch (_e) {
    return { status: null, final_url: "", title: "", text: "" };
  } finally {
    chrome.tabs.remove(tab.id).catch(() => {});
  }
}

$("sweep").addEventListener("click", async () => {
  const out = $("sweep-out");
  const dry_run = $("dry").checked;
  const log = (line) => { out.textContent += line + "\n"; };
  out.textContent = "";
  $("sweep").disabled = true;
  try {
    const q = await chrome.runtime.sendMessage({ type: "liveness-queue" });
    if (!q || !q.ok) return log("error: " + (q && q.error));
    const queue = q.data;
    log(queue.length + " drafts to check" + (dry_run ? " (dry run)" : ""));
    const nextOk = {};  // host → earliest time we may hit it again
    const tally = {};
    while (queue.length) {
      // take the draft whose site has rested longest, so sites interleave
      const at = (d) => nextOk[hostOf(d.apply_url)] || 0;
      const d = queue.reduce((a, b) => (at(b) < at(a) ? b : a));
      queue.splice(queue.indexOf(d), 1);
      await sleep(Math.max(0, at(d) - Date.now()));
      nextOk[hostOf(d.apply_url)] = Date.now() + HOST_GAP_MS;
      const seen = await observe(d.apply_url);
      const r = await chrome.runtime.sendMessage({
        type: "liveness-report",
        report: { snapshot_id: d.snapshot_id, dry_run, ...seen },
      });
      const verdict = r && r.ok ? r.data.liveness : "error";
      tally[verdict] = (tally[verdict] || 0) + 1;
      log(`#${d.snapshot_id} ${d.company} — ${verdict}: ` +
          (r && r.ok ? r.data.note : r && r.error));
    }
    log("done: " + JSON.stringify(tally));
  } finally {
    $("sweep").disabled = false;
  }
});
