/* background service worker — the extension's API client.
 *
 * Holds the API base + token in chrome.storage and runs all fetches to the
 * sidecar (apply_api.py on 127.0.0.1:8531). Because the fetch happens here,
 * with the URL declared in host_permissions, it is not subject to the page's
 * CORS — the content script just messages this worker.
 */

"use strict";

const DEFAULTS = { base: "http://127.0.0.1:8531", token: "" };

async function cfg() {
  const c = await chrome.storage.local.get(DEFAULTS);
  return { base: (c.base || DEFAULTS.base).replace(/\/$/, ""), token: c.token || "" };
}

// Returns the Response on success, or {ok:false,error} on a config/transport/HTTP error.
async function api(path, opts) {
  const { base, token } = await cfg();
  if (!token) return { ok: false, error: "no token — open the extension options" };
  try {
    const r = await fetch(base + path, {
      ...opts,
      headers: { Authorization: "Bearer " + token, ...(opts && opts.headers) },
    });
    if (!r.ok) {
      let detail = "";
      try { detail = (await r.json()).detail || ""; } catch (_e) { /* not JSON */ }
      return { ok: false, error: "HTTP " + r.status + (detail ? " — " + detail : "") };
    }
    return r;
  } catch (e) {
    return { ok: false, error: e.message };
  }
}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  (async () => {
    if (msg.type === "pending") {
      const r = await api("/pending");
      sendResponse(r.ok === false ? r : { ok: true, data: await r.json() });
    } else if (msg.type === "profile-cv") {
      // snapshot-free: one CV per candidate, served straight from the profile
      const r = await api("/cv");
      if (r.ok === false) return sendResponse(r);
      const buf = await r.arrayBuffer();
      // bytes as a plain array — structured-clone over the message boundary
      sendResponse({ ok: true, bytes: Array.from(new Uint8Array(buf)), name: filenameFrom(r) });
    } else if (msg.type === "answer") {
      // answer panel: one grounded answer; the server resolves the job
      // (dashboard focus > unambiguous host > profile-only)
      const r = await api("/answer", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question: msg.question, page_host: msg.page_host }),
      });
      sendResponse(r.ok === false ? r : { ok: true, data: await r.json() });
    } else if (msg.type === "cover-letter") {
      // the letter for the job being applied to (focus-resolved, like /answer)
      const r = await api("/cover-letter", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ page_host: msg.page_host }),
      });
      sendResponse(r.ok === false ? r : { ok: true, data: await r.json() });
    } else if (msg.type === "focus") {
      // dashboard 🎯 focus — the fallback binding for "I submitted it" when
      // the page host matches no pending draft (aggregator redirects)
      const r = await api("/focus");
      sendResponse(r.ok === false ? r : { ok: true, data: await r.json() });
    } else if (msg.type === "email-match") {
      // ✉️ flow: whole pasted email → intent + nominated application(s);
      // the server's LLM picks only from the closed active-application list
      const r = await api("/email-match", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email_text: msg.email_text }),
      });
      sendResponse(r.ok === false ? r : { ok: true, data: await r.json() });
    } else if (msg.type === "email-book") {
      // the human clicked the intent-matched button on a named application
      const r = await api("/email-status", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ job_id: msg.job_id, status: msg.status }),
      });
      sendResponse(r.ok === false ? r : { ok: true, data: await r.json() });
    } else if (msg.type === "submitted") {
      const r = await api("/snapshot/" + msg.id + "/submitted", { method: "POST" });
      sendResponse(r.ok === false ? r : { ok: true, data: await r.json() });
    } else if (msg.type === "focus-submitted") {
      // draft-less 🎯 job: book the focused job applied at the job level
      // (the dashboard's ✅ button has no snapshot to advance either)
      const r = await api("/focus/submitted", { method: "POST" });
      sendResponse(r.ok === false ? r : { ok: true, data: await r.json() });
    } else if (msg.type === "sweep") {
      // options page "Sweep now" — runs on after the reply; progress is in storage
      if (sweeping) return sendResponse({ ok: false, error: "a sweep is already running" });
      runSweep(!!msg.dry_run);
      sendResponse({ ok: true });
    } else if (msg.type === "fill-plan") {
      // snapshot-free: send live-extracted fields, get back which map to a fact
      const r = await api("/fill-plan", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ fields: msg.fields, page_host: msg.page_host || "" }),
      });
      sendResponse(r.ok === false ? r : { ok: true, data: await r.json() });
    } else {
      sendResponse({ ok: false, error: "unknown message type: " + msg.type });
    }
  })();
  return true; // keep the message channel open for the async sendResponse
});

function filenameFrom(r) {
  const m = (r.headers.get("Content-Disposition") || "").match(/filename="?([^"]+)"?/);
  return m ? m[1] : "cv.pdf";
}

// Frames worth a panel: the top frame (any page — activeTab covers it on the
// click) and structured-ATS subframes (host_permissions cover those). Keep in
// sync with content.js ATS_FRAME_RE / the manifest matches.
const ATS_URL_RE =
  /^https:\/\/([^/]+\.)?(greenhouse\.io|ashbyhq\.com|jobs\.personio\.de|lever\.co|workable\.com|smartrecruiters\.com)\//i;

// Toolbar icon → toggle the in-page panel (re-summon it if dismissed/clobbered).
// Injection is explicit per frame, not declarative-only: manifest all_frames
// injection proved unreliable for cross-origin ATS iframes (the Workato
// gh_jid embed got no script while a top-level greenhouse tab did), so we
// enumerate the tab's frames and executeScript into any target frame whose
// toggle message finds no receiver. Newly injected frames show their panel on
// their own init. This is also what makes "Fill facts from profile" work on
// ANY page — disguised ATS on career sites and hand-found jobs included.
chrome.action.onClicked.addListener(async (tab) => {
  if (!tab || tab.id == null) return;
  let frames;
  try {
    frames = (await chrome.webNavigation.getAllFrames({ tabId: tab.id })) || [];
  } catch (_e) {
    frames = [{ frameId: 0, url: tab.url || "" }];
  }
  const targets = frames.filter((f) => f.frameId === 0 || ATS_URL_RE.test(f.url || ""));
  for (const f of targets) {
    try {
      await chrome.tabs.sendMessage(tab.id, { type: "toggle-panel" }, { frameId: f.frameId });
    } catch (_e) {
      // no receiver in this frame → inject; its own init shows the panel
      try {
        await chrome.scripting.executeScript({
          target: { tabId: tab.id, frameIds: [f.frameId] },
          files: ["content.js"],
        });
      } catch (_e2) {
        /* chrome:// pages, web store, frames without access — nothing we can do */
      }
    }
  }
});

// ── draft liveness sweep ─────────────────────────────────────────────────────
// Opens each pending draft the server could not verify in a background tab of
// this browser (the human's logins, real rendering) and reports what it sees.
// The extension only renders and reports; the verdict is the server's
// (utils/draft_liveness.py).
const HOST_GAP_MS = 8000;  // per-site pacing: a human opening tabs, not a crawler
const LOAD_TIMEOUT_MS = 25000;
const RENDER_WAIT_MS = 4000;  // SPAs paint the "expired" notice after load
const SWEEP_EVERY_MS = 20 * 3600 * 1000;  // "daily", tolerant of when Chrome is open
const SWEEP_ALARM = "liveness-sweep";

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

// ponytail: the worker can still be recycled mid-sweep (the waits here are all
// under its 30 s idle limit, but Chrome gives no guarantee). Every draft is
// reported as soon as it is seen, so a cut-off run loses nothing — the rest is
// picked up by the next run. Move to an offscreen document if that bites.
let sweeping = false;

async function runSweep(dry_run) {
  if (sweeping) return;
  sweeping = true;
  const run = { started: new Date().toLocaleString(), dry_run, lines: [], done: false };
  const log = (line) => {
    run.lines.push(line);
    return chrome.storage.local.set({ sweepRun: run });
  };
  try {
    const q = await api("/liveness-queue");
    if (q.ok === false) return await log("error: " + q.error);
    const queue = await q.json();
    await log(queue.length + " drafts to check");
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
      const r = await api("/liveness-report", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ snapshot_id: d.snapshot_id, dry_run, ...seen }),
      });
      const res = r.ok === false ? { liveness: "error", note: r.error } : await r.json();
      tally[res.liveness] = (tally[res.liveness] || 0) + 1;
      await log(`#${d.snapshot_id} ${d.company} — ${res.liveness}: ${res.note}`);
    }
    // a dry run or an unreachable sidecar does not count as today's sweep
    if (!dry_run) {
      await chrome.storage.local.set({ sweepLastRun: Date.now() });
      // heartbeat for the review page's "sweep has not run" warning
      const hb = await api("/liveness-swept", { method: "POST" });
      if (hb.ok === false) await log("heartbeat failed: " + hb.error);
    }
    await log("done: " + JSON.stringify(tally));
  } finally {
    sweeping = false;
    run.done = true;
    await chrome.storage.local.set({ sweepRun: run });
  }
}

// Hourly tick, daily sweep: an alarm set for "24 h from now" would never fire
// in a browser that is restarted every day; the timestamp survives restarts.
chrome.alarms.get(SWEEP_ALARM).then((a) => {
  if (!a) chrome.alarms.create(SWEEP_ALARM, { delayInMinutes: 5, periodInMinutes: 60 });
});
chrome.alarms.onAlarm.addListener(async (alarm) => {
  if (alarm.name !== SWEEP_ALARM) return;
  const { sweepLastRun = 0 } = await chrome.storage.local.get("sweepLastRun");
  if (Date.now() - sweepLastRun >= SWEEP_EVERY_MS) await runSweep(false);
});
