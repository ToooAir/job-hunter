/* job-hunter autofill — content script.
 *
 * One fill mode (snapshot replay retired 2026-07-02 with Stage 1's mapping
 * chain): "Fill facts from profile" (runProfileFill) live-extracts the
 * fields on ANY page, asks the sidecar (POST /fill-plan) which map to a
 * profile fact, and fills those (incl. the CV into resume file inputs) via
 * the React-safe native setter. Open questions stay blank — the human
 * answers them (cover letter in the dashboard is the raw material).
 * Reaches any page via toolbar-click injection (background.js), and ATS
 * iframes on disguised career pages via all_frames.
 *
 * The pending-draft match (findPending) remains for bookkeeping: it shows
 * which snapshot this page belongs to and carries the "I submitted it"
 * button + confirmation watch that book the application.
 *
 * Out of scope here: gated auto-submit, custom-JS widgets / dropzones.
 */

"use strict";

let MATCH = null; // the pending snapshot for this page, if any
let FOCUS_JOB = null; // draft-less 🎯 focus (job_id/company) — no snapshot to
//                       bind, booked applied at the job level instead
let HOST = null;  // light-DOM host element carrying the panel's shadow root
let PANEL = null; // the shadow root — the UI lives here, isolated from page CSS

// Structured-ATS hosts (keep in sync with manifest matches): a subframe on one
// of these is the disguised-embed case (gh_jid iframe on a company careers
// page) — the form lives there, so the panel must too. Other subframes stay
// silent; in the top frame the panel always runs.
const ATS_FRAME_RE = /(^|\.)(greenhouse\.io|ashbyhq\.com|jobs\.personio\.de|lever\.co|workable\.com|smartrecruiters\.com)$/i;

// One host, many employers (SuccessFactors regional instances, Workday,
// path-routed ATS boards): a lone host hit proves nothing there — the Audatic
// draft bound a KHS application page on career5.successfactors.eu
// (2026-07-10). On these hosts binding needs an exact page match or 🎯 focus.
const MULTI_TENANT_RE =
  /successfactors|myworkdayjobs|greenhouse\.io|ashbyhq\.com|lever\.co|workable\.com|smartrecruiters\.com|join\.com|softgarden|icims\.com|taleo/i;

// Declared BEFORE the boot block below: injectPanel() runs immediately, and a
// `const` referenced across that call must already be initialized (TDZ) —
// 0.7.0 had it after the block, which killed the whole script on injection.
const BTN_STYLE =
  "cursor:pointer;border:1px solid #555;background:#1e1e1e;color:#eee;" +
  "border-radius:5px;padding:5px 9px;font:inherit";
const CLICK_ACTIONS = new WeakMap(); // panel button → click handler (armor)
let ATTR_GUARD = null;   // MutationObserver reverting site edits to HOST attrs
let HOSTILE = false;     // this page kills events addressed to our panel
let PROBE = null;        // pointerdown token awaiting delivery proof at HOST

if (window.top === window.self || ATS_FRAME_RE.test(location.hostname)) {
  injectPanel();
  init();
}

// Toolbar icon click (background → here): re-summon or hide the panel, so it can
// always be brought back if it was dismissed or clobbered by the page.
chrome.runtime.onMessage.addListener((msg) => {
  if (msg && msg.type === "toggle-panel") togglePanel();
});

// A 🎯 focus set AFTER this panel loaded (the smartapply case: the redirect
// page matches no host, so the user goes to the dashboard to press 🎯 and
// comes back) — findPending ran once at injection and would stay stale
// forever. Re-check whenever the tab regains the user's attention, but only
// while unmatched: an established match must not be re-resolved mid-apply.
window.addEventListener("focus", refindIfUnmatched);
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") refindIfUnmatched();
});

function refindIfUnmatched() {
  if (!MATCH && HOST && HOST.isConnected) findPending();
}

async function init() {
  await findPending();
  await maybeWatchConfirmation(); // resume a watch across the post-submit nav
}

// Query inside the panel's shadow root (page getElementById cannot see it).
function $(sel) {
  return PANEL ? PANEL.querySelector(sel) : null;
}

function bg(msg) {
  // An orphaned script (extension reloaded, tab not) throws "Extension
  // context invalidated" from sendMessage — degrade to the {ok:false} shape
  // every caller already renders instead of an uncaught rejection.
  return new Promise((resolve) => {
    try {
      chrome.runtime.sendMessage(msg, (res) => {
        if (chrome.runtime.lastError) {
          resolve({ ok: false, error: chrome.runtime.lastError.message });
        } else {
          resolve(res);
        }
      });
    } catch (_e) {
      resolve({ ok: false, error: "extension reloaded — refresh this page" });
    }
  });
}

function pageHost() {
  return location.host.replace(/^www\./, "");
}

// ── panel ────────────────────────────────────────────────────────────────────
// The panel lives in a closed shadow root, not the page DOM: the page's and
// other extensions' CSS cannot reach in to resize, restyle, or collapse it.
function injectPanel() {
  if (HOST && HOST.isConnected) return;
  HOST = document.createElement("div");
  HOST.id = "jh-autofill-host";
  PANEL = HOST.attachShadow({ mode: "closed" });
  const btn = BTN_STYLE;
  PANEL.innerHTML =
    '<div style="position:fixed;top:12px;right:12px;z-index:2147483647;width:380px;' +
    "max-height:82vh;overflow:auto;background:#111;color:#eee;" +
    "font:12px/1.45 ui-monospace,Menlo,monospace;border:1px solid #444;" +
    'border-radius:8px;padding:10px;box-shadow:0 6px 22px rgba(0,0,0,.55)">' +
    '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">' +
    "<b>job-hunter autofill</b>" +
    '<button id="jh-close" style="' + btn + '">×</button></div>' +
    '<button id="jh-fill-profile" style="' + btn +
    ';width:100%">Fill facts from profile</button>' +
    '<button id="jh-submitted" style="' + btn +
    ';width:100%;margin-top:6px;display:none">✓ I submitted it</button>' +
    '<div id="jh-status" style="margin-top:6px;color:#888"></div>' +
    '<div id="jh-out" style="margin-top:8px;color:#aaa"></div>' +
    // answer panel (ANSWER_PANEL_PLAN.md): paste a question, get a grounded
    // answer + Copy button. The answer is NEVER filled into the page —
    // reading-before-pasting is the review gate.
    '<div style="margin-top:10px;border-top:1px solid #333;padding-top:8px">' +
    '<textarea id="jh-q" rows="3" placeholder="Paste the form&#39;s question…" ' +
    'style="width:100%;box-sizing:border-box;background:#1e1e1e;color:#eee;' +
    'border:1px solid #555;border-radius:5px;padding:5px;font:inherit;resize:vertical"></textarea>' +
    '<button id="jh-answer" style="' + btn + ';width:100%;margin-top:4px">' +
    "Answer from my background</button>" +
    // one-click for the most frequent form question: estimator-backed figure
    // (server generates + caches the job's salary estimate on first ask)
    '<button id="jh-salary" style="' + btn + ';width:100%;margin-top:4px">' +
    "💰 Salary expectation</button>" +
    // the reviewed letter for the focused job — kills the last dashboard
    // round-trip (fetch → read → Copy, same review gate as answers)
    '<button id="jh-cl" style="' + btn + ';width:100%;margin-top:4px">' +
    "📄 Cover letter</button>" +
    '<div id="jh-ans-warn" style="margin-top:4px;color:#e0a000"></div>' +
    '<div id="jh-ans" style="display:none;margin-top:6px;padding:6px;' +
    'background:#1a1a1a;border:1px solid #333;border-radius:5px;' +
    'white-space:pre-wrap;color:#ddd"></div>' +
    '<div id="jh-ans-ground" style="margin-top:4px;color:#888"></div>' +
    '<button id="jh-copy" style="' + btn +
    ';width:100%;margin-top:4px;display:none">Copy answer</button>' +
    "</div>" +
    // ✉️ decision-email booking: paste a whole email (works on Gmail — the
    // toolbar click injects the panel on any page), the server classifies it
    // and nominates from the closed active-application list, and the button
    // offered is derived from the INTENT — a pasted interview invite cannot
    // present a "mark rejected" button. Booking = the human's click.
    '<div style="margin-top:10px;border-top:1px solid #333;padding-top:8px">' +
    '<textarea id="jh-email" rows="3" placeholder="Paste a decision email ' +
    '(rejection / interview invite)…" ' +
    'style="width:100%;box-sizing:border-box;background:#1e1e1e;color:#eee;' +
    'border:1px solid #555;border-radius:5px;padding:5px;font:inherit;resize:vertical"></textarea>' +
    '<button id="jh-email-match" style="' + btn + ';width:100%;margin-top:4px">' +
    "✉️ Match email to application</button>" +
    '<div id="jh-email-out" style="margin-top:6px"></div>' +
    "</div>" +
    "</div>";
  document.documentElement.appendChild(HOST);
  promoteToTopLayer();
  guardHostAttributes();
  // Armor probe target: an event that reaches HOST was not killed en route.
  HOST.addEventListener("pointerdown", markProbeDelivered, true);
  // Works on ANY page, no snapshot needed — fills only profile facts.
  // (onClick, not bare addEventListener: hostile-modal armor needs the
  // handler for direct invocation when the event chain is killed.)
  onClick($("#jh-fill-profile"), runProfileFill);
  onClick($("#jh-answer"), () => runAnswer());
  onClick($("#jh-salary"), () => runAnswer(SALARY_QUESTION));
  onClick($("#jh-cl"), runCoverLetter);
  onClick($("#jh-copy"), copyAnswer);
  onClick($("#jh-email-match"), runEmailMatch);
  // The authoritative bookkeeping signal: the human, who just submitted, says
  // so. A matched snapshot advances its lifecycle; a draft-less 🎯 focus books
  // the job applied directly (same as the dashboard's ✅ button).
  onClick($("#jh-submitted"), () => {
    if (MATCH) bookSubmitted(MATCH.snapshot_id);
    else if (FOCUS_JOB) bookFocusSubmitted();
  });
  onClick($("#jh-close"), closePanel);
}

// Max z-index is not enough: site modals use the same value and paint above
// (later DOM order wins ties), and <dialog>.showModal() sits in the top layer,
// above ANY z-index. So the host goes into the top layer too, as a manual
// popover (no light dismiss, no ESC). Within the top layer the last-shown
// element paints on top, so when the SITE opens a dialog/popover after us we
// re-show ours to jump back above it (the capture listener sees non-bubbling
// `toggle` events from any element; ours is guarded out to avoid a loop).
function promoteToTopLayer() {
  if (!HOST || typeof HOST.showPopover !== "function") return; // old Chrome: plain div
  HOST.setAttribute("popover", "manual");
  // Neutralize the UA popover box (position:fixed inset:0 margin:auto border
  // background): the shadow panel does its own fixed positioning.
  // pointer-events:auto re-opts-in when a site whitelists its modal by
  // switching everything else off.
  HOST.style.cssText =
    "position:fixed;inset:auto;width:0;height:0;border:0;margin:0;" +
    "padding:0;background:transparent;overflow:visible;pointer-events:auto";
  try { HOST.showPopover(); } catch (_e) {}
}

function repromoteOnSiteTopLayer(e) {
  if (!HOST || !HOST.isConnected || e.target === HOST) return;
  if (e.newState !== "open") return;
  try { HOST.hidePopover(); } catch (_e) {}
  try { HOST.showPopover(); } catch (_e) {}
}
document.addEventListener("toggle", repromoteOnSiteTopLayer, true);

// Hide-outside libraries (react-aria style) don't mark the page once — a
// MutationObserver RE-applies inert/aria-hidden to anything outside their
// modal, so stripping it in a pointerdown handler lasts one microtask and the
// click's hit test already misses again (EPAM, 2026-07-09 second retest).
// Fight observer with observer: attribute changes on HOST are reverted the
// instant they land — our callback always runs after their mutation, so the
// last word is ours. (Their observers watch for added nodes, not for
// attribute removals on known ones, so this settles instead of ping-ponging.)
function guardHostAttributes() {
  if (ATTR_GUARD) ATTR_GUARD.disconnect();
  ATTR_GUARD = new MutationObserver(() => {
    if (!HOST) return;
    if (HOST.hasAttribute("inert")) HOST.removeAttribute("inert");
    if (HOST.hasAttribute("aria-hidden")) HOST.removeAttribute("aria-hidden");
    if (HOST.style.pointerEvents !== "auto") HOST.style.pointerEvents = "auto";
  });
  ATTR_GUARD.observe(HOST, {
    attributes: true, attributeFilter: ["inert", "aria-hidden", "style"],
  });
}

// Paint order is not click order: modal layers make everything OUTSIDE them
// unclickable even when it paints above — <dialog>.showModal() marks the rest
// of the page inert (hit testing skips inert nodes), focus-trap libraries kill
// events outside their overlay at document capture, Radix-style CSS whitelists
// via body{pointer-events:none}. The panel then looks fine but a click inside
// it lands on the site's form (EPAM, 2026-07-09). Window-capture runs before
// any site document listener, so watch pointerdown: a press inside the panel's
// box whose target is NOT our host means we are blocked — re-parent the host
// INTO the blocking overlay's top-level container, joining its inert/whitelist
// scope. Hit testing re-runs per event, so the same click's pointerup/click
// already reaches the button.
//
// The re-parent must NOT give up the top layer (first EPAM retest: the moved
// panel painted BELOW the overlay, losing on z-index inside its stacking
// context). Tree position buys clickability, the popover buys paint order —
// they are independent, so keep both: moving a shown popover auto-hides it,
// re-promote right after.
function rescueClickThrough(e) {
  if (HOST && !HOST.isConnected) {
    // the overlay we moved into was torn down and took us with it
    HOST = null;
    PANEL = null;
    injectPanel();
    init();
    return;
  }
  if (!HOST || !PANEL || e.target === HOST) return;
  const panel = PANEL.firstElementChild;
  const r = panel && panel.getBoundingClientRect();
  if (!r || e.clientX < r.left || e.clientX > r.right ||
      e.clientY < r.top || e.clientY > r.bottom) return;
  // Field evidence for the next hostile-modal variant: what stole the click
  // and what state our host was in (the panel's own buttons never get here —
  // their events target HOST and returned above).
  console.info("[jh-autofill] click-through rescue:", {
    blocker: e.target.tagName + "." + (e.target.className || ""),
    hostInert: HOST.hasAttribute("inert"),
    hostAriaHidden: HOST.getAttribute("aria-hidden"),
    hostPointerEvents: getComputedStyle(HOST).pointerEvents,
    hostParent: HOST.parentElement && HOST.parentElement.tagName,
    hostPopoverOpen: HOST.matches(":popover-open"),
    modalDialogOpen: !!document.querySelector("dialog:modal"),
  });
  if (HOST.hasAttribute("inert") || HOST.getAttribute("aria-hidden") === "true") {
    // the site marked US inert (react-aria hide-outside style): strip it and
    // stay home — the attribute guard keeps it stripped from here on
    HOST.removeAttribute("inert");
    HOST.removeAttribute("aria-hidden");
    promoteToTopLayer();
    return;
  }
  // Native <dialog>.showModal() (EPAM's Modal component — confirmed in their
  // bundle) makes everything outside the dialog IMPLICITLY inert: no attribute
  // ever appears on HOST (field log: hostInert:false, popover open, hit test
  // still lands on the modal), so attribute stripping and the guard both have
  // nothing to act on. The only exit is to become a descendant of the dialog —
  // modal inertness spares the dialog's own subtree. Target the dialog
  // ELEMENT, not its top-level wrapper: a sibling inside the wrapper is still
  // outside the dialog and stays inert.
  const dlg = (e.target.closest && e.target.closest("dialog:modal")) ||
    document.querySelector("dialog:modal");
  if (dlg && !dlg.contains(HOST)) {
    dlg.appendChild(HOST); // auto-hides the shown popover…
    promoteToTopLayer();   // …so re-show it from the new position
    // A closed dialog usually stays in the DOM as display:none, which stops
    // rendering its whole subtree — us included. Move back out on close.
    dlg.addEventListener("close", () => {
      if (HOST && HOST.isConnected) {
        document.documentElement.appendChild(HOST);
        promoteToTopLayer();
      }
    }, { once: true });
    return;
  }
  let root = e.target;
  if (root === document.documentElement || root === document.body) return;
  while (root.parentElement && root.parentElement !== document.body &&
         root.parentElement !== document.documentElement) {
    root = root.parentElement;
  }
  root.appendChild(HOST); // auto-hides the shown popover…
  promoteToTopLayer();    // …so re-show it from the new position
}
window.addEventListener("pointerdown", rescueClickThrough, true);

// ── hostile-modal armor: direct dispatch when the site kills our events ─────
// Third EPAM state (2026-07-09): hit testing reaches the panel (target=HOST,
// so the rescue above stays silent) but the site's outside-click guard runs
// at document capture — between window and our shadow — and
// stopPropagation()s the event, so the button's listener never fires. We
// can't outrank a document-capture listener from inside the shadow; what we
// CAN do is run first (window capture precedes document) and deliver the
// action ourselves. Detection is a probe: a pointerdown targeting HOST that
// never arrives at HOST's own capture listener was killed mid-path — arm the
// armor. From then on, events targeting HOST are stopped at window (the
// site's guard never sees them) and clicks are dispatched by direct handler
// invocation (CLICK_ACTIONS); text inputs need no listener — native defaults
// (focus, typing) are not affected by stopPropagation.
// (CLICK_ACTIONS / HOSTILE / PROBE state lives at the top of the file —
// injectPanel runs from the boot block before this point is reached.)
function onClick(el, fn) {
  el.addEventListener("click", fn);
  CLICK_ACTIONS.set(el, fn);
}

function markProbeDelivered() {
  if (PROBE) PROBE.delivered = true;
}

function armorPointerdown(e) {
  if (!HOST || e.target !== HOST) return;
  if (HOSTILE) {
    e.stopImmediatePropagation();
    return;
  }
  const probe = { delivered: false };
  PROBE = probe;
  setTimeout(() => {
    if (PROBE === probe) PROBE = null;
    if (!probe.delivered && HOST && HOST.isConnected) {
      HOSTILE = true;
      console.info("[jh-autofill] site kills events outside its modal — " +
                    "direct dispatch armed (this first click was lost; " +
                    "click again)");
    }
  }, 0);
}
window.addEventListener("pointerdown", armorPointerdown, true);

function armorClick(e) {
  if (!HOSTILE || !HOST || !PANEL || e.target !== HOST) return;
  e.stopImmediatePropagation();
  e.preventDefault();
  const inner = PANEL.elementFromPoint(e.clientX, e.clientY);
  const btn = inner && inner.closest && inner.closest("button");
  const fn = btn && CLICK_ACTIONS.get(btn);
  if (fn && !btn.disabled) fn();
  else if (inner && /^(TEXTAREA|INPUT|SELECT)$/.test(inner.tagName)) inner.focus();
}
window.addEventListener("click", armorClick, true);

// Under armor, shield the rest of the panel's event traffic from the site's
// guards too (focus containment yanking focus back, key handlers): stopping
// propagation does not cancel default actions, so typing/paste still work.
for (const t of ["pointerup", "mousedown", "mouseup", "focusin",
                 "keydown", "keyup", "keypress", "paste"]) {
  window.addEventListener(t, (e) => {
    if (HOSTILE && HOST && e.target === HOST) e.stopImmediatePropagation();
  }, true);
}

function closePanel() {
  if (ATTR_GUARD) ATTR_GUARD.disconnect();
  ATTR_GUARD = null;
  if (HOST) HOST.remove();
  HOST = null;
  PANEL = null;
}

// Toolbar click: hide if shown, otherwise re-inject and re-detect.
function togglePanel() {
  if (HOST && HOST.isConnected) {
    closePanel();
    return;
  }
  injectPanel();
  init();
}

// ── find the draft for this page ───────────────────────────────────────────────
async function findPending() {
  const status = $("#jh-status");
  if (!status) return; // panel hidden
  const res = await bg({ type: "pending" });
  if (!res || !res.ok) {
    status.innerHTML = red((res && res.error) || "no response") + " — check options.";
    return;
  }
  const hostHits = res.data.filter((s) => s.host && hostMatch(pageHost(), s.host));
  // Aggregator boards (arbeitnow.com) list every job on one host, so a host
  // hit alone can be another job's draft — and MATCH arms the confirmation
  // watch, so a wrong bind books the wrong application. Exact page-URL match
  // first; a lone host hit stays trustworthy; anything else needs the human's
  // 🎯 focus (which also covers the smartapply.indeed.com redirect, where the
  // draft's host matches nothing).
  MATCH = hostHits.find((s) => samePage(s.apply_url)) || null;
  if (!MATCH && hostHits.length === 1 && !MULTI_TENANT_RE.test(pageHost())) {
    MATCH = hostHits[0]; // company-specific host: the lone draft IS this page's
  }
  let via = "";
  FOCUS_JOB = null;
  let focusInfo = null;
  if (!MATCH) {
    const foc = await bg({ type: "focus" });
    if (foc && foc.ok && foc.data && foc.data.job_id) {
      focusInfo = foc.data;
      // A focus with a draft snapshot binds the submit tracking (the
      // smartapply redirect case). A focus on a plain scored job carries no
      // snapshot_id — the answer panel (💰/📄) still grounds on it server-
      // side, and "I submitted it" books it applied at the job level.
      if (focusInfo.snapshot_id)
        MATCH = res.data.find((s) => s.snapshot_id === focusInfo.snapshot_id) || null;
      if (MATCH) via = " · via 🎯 focus";
    }
  }
  if (MATCH) {
    $("#jh-submitted").style.display = "block";
    status.textContent = MATCH.company + " · snapshot #" + MATCH.snapshot_id +
      " · " + (MATCH.ats || "?") + " · T" + MATCH.tier + via;
  } else if (focusInfo) {
    // 🎯 set on a draft-less job: make the linkage visible (the panel used to
    // fall through to "none for this page" and look disconnected) and arm the
    // job-level booking, mirroring the dashboard's ✅ button (no snapshot to
    // advance, but the human's click is the same authority).
    FOCUS_JOB = focusInfo;
    $("#jh-submitted").style.display = "block";
    status.textContent = "🎯 " + (focusInfo.company || "focused job") +
      " · no draft — press ✓ when submitted to book it applied";
  } else if (hostHits.length) {
    // >1 drafts, or a lone draft on a multi-tenant host (not bindable)
    $("#jh-submitted").style.display = "none";
    status.innerHTML = red(hostHits.length + " draft(s) share this host") +
      " — press 🎯 on the right one in the dashboard, then come back.";
  } else {
    status.textContent = res.data.length + " pending draft(s), none for this page";
  }
}

function hostMatch(a, b) {
  return a === b || a.endsWith("." + b) || b.endsWith("." + a);
}

// True when a draft's apply_url IS the page being viewed (host + path) —
// the only per-job signal on multi-job hosts. Query string and hash are
// mostly tracking noise and ignored — EXCEPT the identifying params of
// query-routed ATS (successfactors: ?company=X&jobId=N shares one path
// across every employer): those the draft URL carries must match the page.
const IDENTITY_PARAMS = ["company", "jobId", "gh_jid", "job", "id"];

function samePage(applyUrl) {
  if (!applyUrl) return false;
  try {
    const u = new URL(applyUrl);
    if (!hostMatch(pageHost(), u.host.replace(/^www\./, "")) ||
        normPath(u.pathname) !== normPath(location.pathname)) return false;
    const here = new URLSearchParams(location.search);
    return IDENTITY_PARAMS.every((k) => {
      const want = u.searchParams.get(k);
      return !want || here.get(k) === want;
    });
  } catch (_e) {
    return false;
  }
}

function normPath(p) {
  return (p || "/").replace(/\/+$/, "").toLowerCase() || "/";
}

// ── fill facts from the profile on ANY page ───────────────────────────────────
// No snapshot/job_id needed — facts (name/email/visa/salary…) are the same for
// every application. Live-extract the fields, ask the sidecar which map to a
// fact, fill those; open/job-specific questions come back unmatched and stay
// blank (never invented). The human answers those, then checks & submits.
// ── answer panel: grounded answers on demand, copy-paste interface ────────────
// The canonical salary question — hits the server's fact short-circuit, which
// generates + caches the job's salary estimate on first ask (~20 s).
const SALARY_QUESTION = "What is your expected compensation?";

async function runAnswer(presetQ) {
  const preset = typeof presetQ === "string" ? presetQ : "";
  const q = preset || ($("#jh-q").value || "").trim();
  const askBtn = $("#jh-answer");
  const salBtn = $("#jh-salary");
  const clBtn = $("#jh-cl");
  const active = preset ? salBtn : askBtn;
  const warn = $("#jh-ans-warn");
  const box = $("#jh-ans");
  const ground = $("#jh-ans-ground");
  const copy = $("#jh-copy");
  if (!q || !active) return;
  askBtn.disabled = salBtn.disabled = clBtn.disabled = true;
  const origText = active.textContent;
  active.textContent = preset ? "estimating… (first ask ~20 s)" : "answering…";
  warn.textContent = "";
  box.style.display = "none";
  copy.style.display = "none";
  ground.textContent = "";

  const res = await bg({ type: "answer", question: q, page_host: pageHost() });
  askBtn.disabled = salBtn.disabled = clBtn.disabled = false;
  active.textContent = origText;
  if (!res || !res.ok) {
    warn.innerHTML = red("answer failed: " + ((res && res.error) || "?"));
    return;
  }
  const d = res.data;
  for (const w of d.warnings || []) {
    warn.innerHTML += "⚠ " + esc(w) + "<br>";
  }
  box.textContent = d.answer;
  box.style.display = "block";
  const g = d.grounding || {};
  ground.innerHTML =
    g.kind === "job+profile"
      ? "grounded: " + esc(g.company || "?") + " · " + esc(g.title || "?") +
        " · via " + esc(g.via || "?")
      : g.kind === "profile-fact"
      ? "profile fact: " + esc(g.fact || "?") +
        (g.company ? " · " + esc(g.company) : "") + " (deterministic, no LLM)"
      : '<span style="color:#e0a000">⚠ no job context — profile facts only.' +
        " Set the focus (🎯) in the dashboard for a grounded answer.</span>";
  for (const n of d.notes || []) {
    ground.innerHTML += "<br>· " + esc(n);
  }
  copy.style.display = "block";
  copy.textContent = "Copy answer";
}

// The reviewed cover letter for the focus-resolved job, into the same
// box + Copy flow as answers — fetched and displayed, never page-filled.
async function runCoverLetter() {
  const askBtn = $("#jh-answer");
  const salBtn = $("#jh-salary");
  const clBtn = $("#jh-cl");
  const warn = $("#jh-ans-warn");
  const box = $("#jh-ans");
  const ground = $("#jh-ans-ground");
  const copy = $("#jh-copy");
  askBtn.disabled = salBtn.disabled = clBtn.disabled = true;
  const origText = clBtn.textContent;
  clBtn.textContent = "fetching…";
  warn.textContent = "";
  box.style.display = "none";
  copy.style.display = "none";
  ground.textContent = "";

  const res = await bg({ type: "cover-letter", page_host: pageHost() });
  askBtn.disabled = salBtn.disabled = clBtn.disabled = false;
  clBtn.textContent = origText;
  if (!res || !res.ok) {
    warn.innerHTML = red("cover letter failed: " + ((res && res.error) || "?"));
    return;
  }
  const d = res.data;
  for (const w of d.warnings || []) {
    warn.innerHTML += "⚠ " + esc(w) + "<br>";
  }
  box.textContent = d.cover_letter;
  box.style.display = "block";
  const g = d.grounding || {};
  ground.innerHTML = "cover letter: " + esc(g.company || "?") + " · " +
    esc(g.title || "?") + " · via " + esc(g.via || "?");
  for (const n of d.notes || []) {
    ground.innerHTML += "<br>· " + esc(n);
  }
  copy.style.display = "block";
  copy.textContent = "Copy cover letter";
}

async function copyAnswer() {
  const box = $("#jh-ans");
  const copy = $("#jh-copy");
  if (!box || !box.textContent) return;
  try {
    await navigator.clipboard.writeText(box.textContent);
    copy.textContent = "✓ copied — paste it into the form";
  } catch (_e) {
    copy.textContent = "clipboard blocked — select the text manually";
  }
}

// ── ✉️ decision-email booking ─────────────────────────────────────────────────
const INTENT_LINE = {
  rejection: '<span style="color:#e06666">✖ rejection</span>',
  interview_invite: '<span style="color:#5fd35f">📅 interview invite</span>',
  received_confirmation:
    '<span style="color:#888">📨 received confirmation — no decision, nothing to book</span>',
  other: '<span style="color:#888">— not a decision email, nothing to book</span>',
};

async function runEmailMatch() {
  const btn = $("#jh-email-match");
  const out = $("#jh-email-out");
  const text = (($("#jh-email") || {}).value || "").trim();
  if (!btn || !out) return;
  if (!text) {
    out.innerHTML = red("paste the email first");
    return;
  }
  btn.disabled = true;
  const orig = btn.textContent;
  btn.textContent = "matching…";
  out.textContent = "";
  const res = await bg({ type: "email-match", email_text: text });
  btn.disabled = false;
  btn.textContent = orig;
  if (!res || !res.ok) {
    out.innerHTML = red("email match failed: " + ((res && res.error) || "?"));
    return;
  }
  renderEmailResult(out, res.data);
}

function renderEmailResult(out, d) {
  let html = '<div style="color:#ddd">' +
    (INTENT_LINE[d.intent] || esc(d.intent || "?")) + "</div>";
  if (d.evidence) {
    // the sentence the classification stands on — verify THIS, not the whole email
    html += '<div style="color:#888;font-style:italic;margin-top:2px">“' +
      esc(d.evidence) + "”</div>";
  }
  for (const w of d.warnings || []) {
    html += '<div style="color:#e0a000;margin-top:2px">⚠ ' + esc(w) + "</div>";
  }
  const label = d.book_as === "rejected" ? "Mark rejected"
    : d.book_as === "interview_1" ? "Mark interview" : "";
  const color = d.book_as === "rejected" ? "#e06666" : "#5fd35f";
  if (!(d.matches || []).length) {
    html += '<div style="color:#888;margin-top:4px">no active application' +
      " matched — book it in the dashboard (⚡ Quick Reject)</div>";
  }
  for (const m of d.matches || []) {
    html += '<div style="margin-top:6px;padding:6px;background:#1a1a1a;' +
      'border:1px solid #333;border-radius:5px">' +
      "<b>" + esc(m.company) + "</b> — " + esc(m.title) +
      '<div style="color:#888">applied ' + esc((m.applied_at || "?").slice(0, 10)) +
      " · " + esc(m.status) + "</div>" +
      (m.company_in_email ? "" :
        '<div style="color:#e0a000">⚠ company name not found in the email —' +
        " check this is the right one</div>") +
      (label
        ? '<button data-jh-book data-job="' + esc(m.id) + '" data-status="' +
          esc(d.book_as) + '" style="' + BTN_STYLE + ";color:" + color +
          ';width:100%;margin-top:4px">' + label + "</button>"
        : "") +
      "</div>";
  }
  out.innerHTML = html;
  out.querySelectorAll("button[data-jh-book]").forEach((b) => {
    onClick(b, () => bookEmailStatus(b));
  });
}

async function bookEmailStatus(b) {
  b.disabled = true;
  b.textContent = "booking…";
  const res = await bg({ type: "email-book",
                         job_id: b.dataset.job, status: b.dataset.status });
  if (!res || !res.ok) {
    b.disabled = false;
    b.textContent = "failed — retry (" + ((res && res.error) || "?") + ")";
    return;
  }
  const d = res.data;
  b.outerHTML = '<div style="color:#5fd35f;margin-top:4px">✓ booked ' +
    esc(d.status) + " — " + esc(d.company) + "</div>";
}

const CV_LABEL_RE = /resume|\bcv\b|lebenslauf/i;

async function runProfileFill() {
  const out = $("#jh-out");
  if (!out) return;
  const els = fillableFields();
  if (!els.length) {
    out.innerHTML = red("no fillable fields on this page");
    return;
  }
  // File inputs never go to /fill-plan (no fact can be a file) — they're
  // handled below: resume-labelled ones get the CV, the rest stay manual.
  const fileEls = els.filter((el) => (el.type || "").toLowerCase() === "file");
  const textEls = els.filter((el) => !fileEls.includes(el));

  // Send id/label/name/type/options; the server echoes `id` back so the plan
  // maps to the exact element — `name` alone is ambiguous (radio groups share
  // one name across options).
  const byId = new Map();
  const fields = textEls.map((el, i) => {
    const id = "jh-" + i;
    byId.set(id, el);
    const isRadio = (el.type || "").toLowerCase() === "radio";
    return {
      id,
      // a radio's own label is its OPTION ("Male") — the matchable question
      // ("What gender do you identify as?") lives on the group
      label: collapse((isRadio && radioGroupLabel(el)) || fieldLabel(el)).slice(0, 140),
      name: el.name || el.id || "",
      type: el.tagName === "SELECT" ? "select" : (el.type || "text").toLowerCase(),
      placeholder: (el.placeholder || "").slice(0, 40), // date-format mask hint

      options:
        el.tagName === "SELECT"
          ? [...el.options].map((o) => o.textContent.trim()).filter(Boolean).slice(0, 25)
          : undefined,
    };
  });

  out.textContent = "matching " + fields.length + " fields to profile…";
  const res = await bg({ type: "fill-plan", fields, page_host: pageHost() });
  if (!res || !res.ok) {
    out.innerHTML = red("fill-plan failed: " + ((res && res.error) || "?"));
    return;
  }

  const plan = res.data;
  let filled = 0;
  const reviewNotes = [];
  for (const f of plan.fills) {
    const el = byId.get(f.id);
    if (!el) continue;
    let ok;
    if (f.action === "check") {
      setNativeChecked(el, true);
      ok = el.checked === true;
    } else if (f.action === "select_option") {
      ok = fillSelect(el, f.value);
      if (!ok) {
        // a silent select failure looked like "the extension skipped it" —
        // always leave a visible trace instead
        reviewNotes.push((f.label || f.name) + " → couldn't set the dropdown, pick it manually");
      } else if (!isVisible(el)) {
        // skinned select: the submit value is set, but the widget on screen
        // still shows the old text — the human must eyeball it
        reviewNotes.push((f.label || f.name) + " → filled the hidden select; its on-screen widget may still show the old text");
      }
    } else if ((el.type || "").toLowerCase() === "radio") {
      // A fact value for a radio option: tick it only when THIS option is the
      // value or one of its synonyms (word-boundary match on its label).
      // Never setNativeValue on a radio — that rewrites its submit value. No
      // match → stays blank (a wrongly ticked radio is worse than an empty one).
      const wants = [f.value, ...(f.synonyms || [])]
        .map((v) => normalize(String(v == null ? "" : v)))
        .filter(Boolean);
      const have = normalize(collapse(fieldLabel(el)) + " " + (el.value || ""));
      ok = wants.some((w) => new RegExp("(^| )" + escapeRe(w) + "( |$)").test(have));
      if (ok) setNativeChecked(el, true);
    } else {
      setNativeValue(el, f.value == null ? "" : String(f.value));
      ok = true;
    }
    if (ok) filled++;
    if (f.needs_review) reviewNotes.push((f.label || f.name) + " → confirm the dropdown");
  }

  // CV upload: only into inputs whose label says resume/CV — a cover-letter or
  // certificates upload must not silently get the CV.
  const cvNotes = [];
  const cvTargets = fileEls.filter((el) => CV_LABEL_RE.test(fieldLabel(el)));
  if (cvTargets.length) {
    out.innerHTML = renderProfileResult(fields.length, filled, plan, reviewNotes, cvNotes) +
      '<div style="margin-top:6px;color:#aaa">uploading CV…</div>';
    const cv = await bg({ type: "profile-cv" });
    for (const el of cvTargets) {
      if (!cv || !cv.ok) cvNotes.push("CV fetch failed: " + ((cv && cv.error) || "?"));
      else if (setFile(el, cv)) { cvNotes.push("CV → " + collapse(fieldLabel(el)).slice(0, 40)); filled++; }
      else cvNotes.push("not a real file input — attach the CV manually");
    }
  }
  for (const el of fileEls.filter((e) => !cvTargets.includes(e))) {
    cvNotes.push("file field “" + collapse(fieldLabel(el)).slice(0, 40) + "” — attach manually");
  }
  out.innerHTML = renderProfileResult(fields.length + cvTargets.length, filled, plan, reviewNotes, cvNotes);

  // Filling on a page that has a pending draft = we are applying here: arm
  // the confirmation watch so a thank-you page books it submitted. Keyed by
  // host so two applications in two tabs never clobber each other's watch.
  if (MATCH) {
    const { applyingByHost } = await chrome.storage.local.get({ applyingByHost: {} });
    applyingByHost[pageHost()] = { id: MATCH.snapshot_id, ts: Date.now() };
    await chrome.storage.local.set({ applyingByHost });
    startConfirmWatch(MATCH.snapshot_id);
  }
}

function renderProfileResult(total, filled, plan, reviewNotes, cvNotes) {
  const summary =
    '<div style="margin:6px 0;color:#ddd">filled <b>' + filled + "</b>/" + total +
    " · unmatched " + plan.unmatched.length +
    " · never-fill " + plan.skipped_never_fill.length +
    (reviewNotes.length ? ' · <span style="color:#e0a000">review ' + reviewNotes.length + "</span>" : "") +
    "</div>";
  const hint =
    '<div style="color:#888">unmatched = open / job-specific questions — left ' +
    "blank on purpose. Answer them yourself, then check &amp; submit.</div>";
  const review = reviewNotes.length
    ? '<div style="margin-top:4px;color:#e0a000">' +
      reviewNotes.map((n) => "⚠ " + esc(n)).join("<br>") + "</div>"
    : "";
  const cv = (cvNotes && cvNotes.length)
    ? '<div style="margin-top:4px;color:#aaa">' +
      cvNotes.map((n) => esc(n)).join("<br>") + "</div>"
    : "";
  return summary + hint + review + cv;
}

function collapse(s) {
  return (s || "").replace(/\s+/g, " ").trim();
}

// Drop the CV bytes into a real <input type=file> via DataTransfer. Custom
// drag-drop widgets that aren't a real file input reject this → caller flags
// "attach manually" (a known v2 long-tail).
function setFile(input, cv) {
  if (!(input.tagName === "INPUT" && (input.type || "").toLowerCase() === "file")) {
    return false;
  }
  try {
    const file = new File([new Uint8Array(cv.bytes)], cv.name || "cv.pdf",
                          { type: "application/pdf" });
    const dt = new DataTransfer();
    dt.items.add(file);
    input.files = dt.files;
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.dispatchEvent(new Event("change", { bubbles: true }));
    return input.files.length === 1;
  } catch (_e) {
    return false;
  }
}

// ── auto-mark-submitted on confirmation ───────────────────────────────────────
// Post-submission phrasing only (ported from the deleted apply_session watch).
// Deliberately excludes "thank you for your interest" — that's listing/apply-
// page boilerplate, not a confirmation — and requires received/submitted/sent
// so it doesn't fire before the form is actually sent. Dashboard "mark
// submitted" stays as the backstop.
const CONFIRM_RE = new RegExp([
  "(vielen dank|danke)[^.!?]{0,20}für (ihre|deine) bewerbung",
  // "wurde/ist gesendet" = past tense, can only be true AFTER submit (indeed's
  // "Ihre Bewerbung wurde gesendet", 2026-07-09); the infinitive "Bewerbung
  // senden" (the submit button itself) stays excluded
  "bewerbung[^.!?]{0,40}(eingegangen|erhalten|" +
    "(wurde|ist) (gesendet|übermittelt|verschickt|versandt)|" +
    "erfolgreich (gesendet|übermittelt))",
  "thank you[^.!?]{0,20}for (applying|your application)",
  "your application[^.!?]{0,30}(has been |was )?(received|submitted|sent)",
  "we[^.!?]{0,20}received your application",
  "application (received|submitted|sent)",
  "successfully (applied|submitted)",
  "erfolgreich (beworben|übermittelt)",
].join("|"), "i");

// On load, resume a watch if we were applying on this host (set when Fill ran).
// Stale entries (>2h) are pruned; other hosts' entries are left alone.
async function maybeWatchConfirmation() {
  const { applyingByHost } = await chrome.storage.local.get({ applyingByHost: {} });
  let dirty = false;
  let mine = null;
  for (const [host, entry] of Object.entries(applyingByHost)) {
    if (Date.now() - entry.ts > 2 * 3600 * 1000) {
      delete applyingByHost[host];
      dirty = true;
    } else if (hostMatch(pageHost(), host)) {
      mine = entry;
    }
  }
  if (dirty) await chrome.storage.local.set({ applyingByHost });
  if (mine) startConfirmWatch(mine.id);
}

// The single booking path, used by the authoritative "I submitted it" button
// and by the best-effort confirmation watch. Idempotent: a 409 (already
// submitted) is shown as success.
async function bookSubmitted(id) {
  const { applyingByHost } = await chrome.storage.local.get({ applyingByHost: {} });
  for (const [host, entry] of Object.entries(applyingByHost)) {
    if (entry.id === id) delete applyingByHost[host];
  }
  await chrome.storage.local.set({ applyingByHost });
  const res = await bg({ type: "submitted", id });
  if (res && res.ok) return onBooked(id, false);
  if (res && /409/.test(res.error || "")) return onBooked(id, true);
  const s = $("#jh-status");
  if (s) {
    s.innerHTML = red("book failed: " + ((res && res.error) || "?") +
                      " — try the dashboard button");
  }
}

function onBooked(id, already) {
  window.__jhBooked = true;
  const s = $("#jh-status");
  if (s) {
    s.innerHTML = '<span style="color:#5fd35f">✓ ' +
      (already ? "already submitted" : "marked submitted") + " (#" + id + ")</span>";
  }
  const sub = $("#jh-submitted");
  if (sub) sub.style.display = "none";
}

// Job-level booking for a draft-less 🎯 focus: the server books the focused
// job applied (no snapshot to advance). Mirrors bookSubmitted's UX; the
// dashboard stays the fallback on failure.
async function bookFocusSubmitted() {
  const res = await bg({ type: "focus-submitted" });
  const s = $("#jh-status");
  if (res && res.ok) {
    window.__jhBooked = true;
    if (s) {
      s.innerHTML = '<span style="color:#5fd35f">✓ marked applied — ' +
        ((FOCUS_JOB && FOCUS_JOB.company) || "focused job") + "</span>";
    }
    const sub = $("#jh-submitted");
    if (sub) sub.style.display = "none";
    FOCUS_JOB = null;
    return;
  }
  if (s) {
    s.innerHTML = red("book failed: " + ((res && res.error) || "?") +
                      " — try the dashboard button");
  }
}

// Best-effort only: a correct confirmation saves the human even the one button
// click. A miss is harmless — the "I submitted it" button (and the dashboard)
// are the authority. Precision-biased so it never books the wrong thing.
function startConfirmWatch(id) {
  if (window.__jhWatching) return;
  window.__jhWatching = true;
  const check = async () => {
    if (window.__jhBooked) return;
    const text = (document.body && document.body.innerText) || "";
    if (!CONFIRM_RE.test(text)) return;
    window.__jhBooked = true;
    obs.disconnect();
    clearInterval(iv);
    await bookSubmitted(id);
  };
  const obs = new MutationObserver(check);
  obs.observe(document.documentElement, { childList: true, subtree: true });
  const iv = setInterval(check, 1500);
  check(); // navigation case: the confirmation page is already here
}

// ── live field extraction ─────────────────────────────────────────────────────
// Pierce open shadow roots: SmartRecruiters OneClick is an Angular ShadowDom app
// (<oc-oneclick-form-root> et al.), so its inputs live inside custom-element
// shadow trees a light-DOM querySelectorAll can't see. Angular always attaches
// OPEN roots; closed ones stay opaque (nothing we can do — reads as no-form).
function queryAllDeep(selector, root = document) {
  const out = [...root.querySelectorAll(selector)];
  for (const host of root.querySelectorAll("*")) {
    if (host.shadowRoot) out.push(...queryAllDeep(selector, host.shadowRoot));
  }
  return out;
}

function fillableFields() {
  return queryAllDeep("input, select, textarea").filter((el) => {
    const t = (el.type || "").toLowerCase();
    if (["hidden", "submit", "button", "reset", "image"].includes(t)) return false;
    if (isVisible(el)) return true;
    // Skinned native select (jQuery UI selectmenu / select2 / chosen …): the
    // real <select> is hidden behind a widget but still carries the submit
    // value (the rexx-ATS Geschlecht case) — fill it, flag it for eyes.
    return el.tagName === "SELECT" && hasSelectSkin(el);
  });
}

function isVisible(el) {
  const r = el.getBoundingClientRect();
  return r.width > 0 && r.height > 0;
}

const SKIN_RE = /ui-selectmenu|select2|chosen-container|selectric|nice-select|selectboxit/i;

function hasSelectSkin(el) {
  const sib = el.nextElementSibling;
  if (sib && SKIN_RE.test(sib.className || "")) return true;
  const p = el.parentElement;
  return !!(p && [...p.children].some((c) => SKIN_RE.test(c.className || "")));
}

// The question a radio group answers. Semantic containers first (fieldset
// legend, role=radiogroup); generic fallback for div-soup forms (lever): climb
// to the ancestor holding every same-name radio, take its first text block
// that is neither an option label nor a widget wrapper.
function radioGroupLabel(el) {
  const fs = el.closest("fieldset");
  if (fs) {
    const leg = fs.querySelector("legend");
    if (leg && leg.textContent.trim()) return leg.textContent;
  }
  const grp = el.closest('[role="radiogroup"]');
  if (grp) {
    const aria = grp.getAttribute("aria-label");
    if (aria) return aria;
    const ref = grp.getAttribute("aria-labelledby");
    if (ref) {
      const n = document.getElementById(ref);
      if (n && n.textContent.trim()) return n.textContent;
    }
  }
  if (!el.name) return "";
  const peers = [...document.querySelectorAll('input[type="radio"]')].filter((r) => r.name === el.name);
  if (peers.length < 2) return "";
  let node = el.parentElement;
  while (node && node !== document.body && !peers.every((p) => node.contains(p))) node = node.parentElement;
  if (!node || node === document.body) return "";
  for (const cand of node.querySelectorAll("div, span, p, legend, h1, h2, h3, h4")) {
    if (cand.closest("label")) continue; // option text, not the question
    if (cand.querySelector("input, select, textarea")) continue; // wrapper
    const txt = cand.textContent.trim();
    if (txt) return txt;
  }
  return "";
}

function fieldLabel(el) {
  // Resolve within the element's own root (its shadow tree, or the document) so
  // for-labels and labelledby refs work for shadow-DOM forms (SmartRecruiters).
  const root = el.getRootNode();
  if (el.id) {
    const lab = root.querySelector('label[for="' + cssEscape(el.id) + '"]');
    if (lab && lab.textContent.trim()) return lab.textContent;
  }
  const wrap = el.closest("label");
  if (wrap && wrap.textContent.trim()) return wrap.textContent;
  const aria = el.getAttribute("aria-label");
  if (aria) return aria;
  const labelledby = el.getAttribute("aria-labelledby");
  if (labelledby) {
    const ref = root.getElementById ? root.getElementById(labelledby) : document.getElementById(labelledby);
    if (ref && ref.textContent.trim()) return ref.textContent;
  }
  // for-less sibling label (team-beverage, 2026-07-10: <label>Vorname</label>
  // next to <input name='1-78'> in one .form-group): climb while the wrapper
  // holds ONLY this control — a wrapper spanning other fields is too far, its
  // label would be someone else's. Ranked above placeholder: required-field
  // placeholders there say "Pflichtfeld"/"Mandatory", noise not a question.
  let node = el.parentElement;
  for (let depth = 0; node && depth < 4 && node.tagName !== "FORM"; depth++) {
    if (node.querySelectorAll("input, select, textarea").length > 1) break;
    const lab = node.querySelector("label");
    if (lab && lab.textContent.trim()) return lab.textContent;
    node = node.parentElement;
  }
  if (el.placeholder) return el.placeholder;
  return el.name || "";
}

// ── fill primitives ─────────────────────────────────────────────────────────
function fillSelect(el, value) {
  const want = normalize(String(value));
  let opt = [...el.options].find((o) => normalize(o.value) === want || normalize(o.textContent) === want);
  if (!opt) opt = [...el.options].find((o) => normalize(o.textContent).includes(want) && want);
  if (!opt) return false;
  setNativeValue(el, opt.value);
  return el.value === opt.value;
}

// React/controlled-input gotcha: assigning .value directly is ignored by React.
// Go through the native prototype setter, then dispatch input+change.
function setNativeValue(el, value) {
  const proto =
    el.tagName === "TEXTAREA"
      ? window.HTMLTextAreaElement.prototype
      : el.tagName === "SELECT"
      ? window.HTMLSelectElement.prototype
      : window.HTMLInputElement.prototype;
  const desc = Object.getOwnPropertyDescriptor(proto, "value");
  // Bracket the write with a full focus→edit→blur lifecycle. Validation-on-blur
  // forms (Ashby, react-hook-form, Formik, Personio) keep flagging a filled
  // field as empty until a *real* focus-out — which is why a manual click-in/
  // click-out "fixes" it. Some also ignore an orphan blur that had no matching
  // focus, so we open with focus/focusin too. Synthetic events don't move
  // keyboard focus or scroll, so bracketing every field is safe.
  // React delegates onFocus/onBlur off bubbling focusin/focusout; Vue/native
  // @focus/@blur listen on the element itself.
  el.dispatchEvent(new Event("focus"));
  el.dispatchEvent(new Event("focusin", { bubbles: true }));
  if (desc && desc.set) desc.set.call(el, value);
  else el.value = value;
  el.dispatchEvent(new Event("input", { bubbles: true }));
  el.dispatchEvent(new Event("change", { bubbles: true }));
  el.dispatchEvent(new Event("blur"));
  el.dispatchEvent(new Event("focusout", { bubbles: true }));
}

function setNativeChecked(el, checked) {
  const desc = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "checked");
  if (desc && desc.set) desc.set.call(el, checked);
  else el.checked = checked;
  el.dispatchEvent(new Event("click", { bubbles: true }));
  el.dispatchEvent(new Event("change", { bubbles: true }));
}

// ── small utils ──────────────────────────────────────────────────────────────
function normalize(s) {
  return (s || "")
    .toLowerCase()
    .replace(/\*|\(required\)|\(optional\)|pflichtfeld/g, "")
    .replace(/[^a-z0-9äöüß+\- ]/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function cssEscape(s) {
  return window.CSS && CSS.escape ? CSS.escape(s) : s.replace(/["\\]/g, "\\$&");
}

function escapeRe(s) {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function red(msg) {
  return '<span style="color:#e06666">' + esc(msg) + "</span>";
}

function esc(s) {
  return String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}
