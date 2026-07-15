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
