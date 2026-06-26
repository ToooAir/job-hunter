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
    if (!r.ok) return { ok: false, error: "HTTP " + r.status };
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
    } else if (msg.type === "snapshot") {
      const r = await api("/snapshot/" + msg.id);
      sendResponse(r.ok === false ? r : { ok: true, data: await r.json() });
    } else if (msg.type === "cv") {
      const r = await api("/snapshot/" + msg.id + "/cv");
      if (r.ok === false) return sendResponse(r);
      const buf = await r.arrayBuffer();
      // bytes as a plain array — structured-clone over the message boundary
      sendResponse({ ok: true, bytes: Array.from(new Uint8Array(buf)), name: filenameFrom(r) });
    } else if (msg.type === "submitted") {
      const r = await api("/snapshot/" + msg.id + "/submitted", { method: "POST" });
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
