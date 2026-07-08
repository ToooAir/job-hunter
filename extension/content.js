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
let HOST = null;  // light-DOM host element carrying the panel's shadow root
let PANEL = null; // the shadow root — the UI lives here, isolated from page CSS

// Structured-ATS hosts (keep in sync with manifest matches): a subframe on one
// of these is the disguised-embed case (gh_jid iframe on a company careers
// page) — the form lives there, so the panel must too. Other subframes stay
// silent; in the top frame the panel always runs.
const ATS_FRAME_RE = /(^|\.)(greenhouse\.io|ashbyhq\.com|jobs\.personio\.de|lever\.co|workable\.com)$/i;

if (window.top === window.self || ATS_FRAME_RE.test(location.hostname)) {
  injectPanel();
  init();
}

// Toolbar icon click (background → here): re-summon or hide the panel, so it can
// always be brought back if it was dismissed or clobbered by the page.
chrome.runtime.onMessage.addListener((msg) => {
  if (msg && msg.type === "toggle-panel") togglePanel();
});

async function init() {
  await findPending();
  await maybeWatchConfirmation(); // resume a watch across the post-submit nav
}

// Query inside the panel's shadow root (page getElementById cannot see it).
function $(sel) {
  return PANEL ? PANEL.querySelector(sel) : null;
}

function bg(msg) {
  return new Promise((resolve) => chrome.runtime.sendMessage(msg, resolve));
}

function pageHost() {
  return location.host.replace(/^www\./, "");
}

// ── panel ────────────────────────────────────────────────────────────────────
// The panel lives in a closed shadow root, not the page DOM: the page's and
// other extensions' CSS cannot reach in to resize, restyle, or collapse it.
const BTN_STYLE =
  "cursor:pointer;border:1px solid #555;background:#1e1e1e;color:#eee;" +
  "border-radius:5px;padding:5px 9px;font:inherit";

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
  // Works on ANY page, no snapshot needed — fills only profile facts.
  $("#jh-fill-profile").addEventListener("click", runProfileFill);
  $("#jh-answer").addEventListener("click", () => runAnswer());
  $("#jh-salary").addEventListener("click", () => runAnswer(SALARY_QUESTION));
  $("#jh-cl").addEventListener("click", runCoverLetter);
  $("#jh-copy").addEventListener("click", copyAnswer);
  $("#jh-email-match").addEventListener("click", runEmailMatch);
  // The authoritative bookkeeping signal: the human, who just submitted, says so.
  $("#jh-submitted").addEventListener("click", () => MATCH && bookSubmitted(MATCH.snapshot_id));
  $("#jh-close").addEventListener("click", closePanel);
}

function closePanel() {
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
  MATCH = res.data.find((s) => s.host && hostMatch(pageHost(), s.host));
  let via = "";
  if (!MATCH) {
    // Aggregator redirect (a de.indeed.com draft's apply flow lands on
    // smartapply.indeed.com) defeats host matching — fall back to the
    // dashboard 🎯 focus. The status names the company + basis, so the human
    // verifies it is the right application before booking.
    const foc = await bg({ type: "focus" });
    const sid = foc && foc.ok && foc.data && foc.data.snapshot_id;
    if (sid) MATCH = res.data.find((s) => s.snapshot_id === sid) || null;
    if (MATCH) via = " · via 🎯 focus";
  }
  if (MATCH) {
    $("#jh-submitted").style.display = "block";
    status.textContent = MATCH.company + " · snapshot #" + MATCH.snapshot_id +
      " · " + (MATCH.ats || "?") + " · T" + MATCH.tier + via;
  } else {
    status.textContent = res.data.length + " pending draft(s), none for this page";
  }
}

function hostMatch(a, b) {
  return a === b || a.endsWith("." + b) || b.endsWith("." + a);
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
    b.addEventListener("click", () => bookEmailStatus(b));
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
  "bewerbung[^.!?]{0,40}(eingegangen|erhalten|erfolgreich (gesendet|übermittelt))",
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
function fillableFields() {
  return [...document.querySelectorAll("input, select, textarea")].filter((el) => {
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
  if (el.id) {
    const lab = document.querySelector('label[for="' + cssEscape(el.id) + '"]');
    if (lab && lab.textContent.trim()) return lab.textContent;
  }
  const wrap = el.closest("label");
  if (wrap && wrap.textContent.trim()) return wrap.textContent;
  const aria = el.getAttribute("aria-label");
  if (aria) return aria;
  const labelledby = el.getAttribute("aria-labelledby");
  if (labelledby) {
    const ref = document.getElementById(labelledby);
    if (ref && ref.textContent.trim()) return ref.textContent;
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
  if (desc && desc.set) desc.set.call(el, value);
  else el.value = value;
  el.dispatchEvent(new Event("input", { bubbles: true }));
  el.dispatchEvent(new Event("change", { bubbles: true }));
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
