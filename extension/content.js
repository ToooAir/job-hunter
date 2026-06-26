/* job-hunter autofill — content script.
 *
 * Phase 1 (PHASE1_PLAN.md): on a structured-ATS apply page, find the reviewed
 * draft for this page (via the background worker → apply_api /pending, matched
 * by host), and fill the form with it. The fill engine (selector replay with a
 * label-match fallback, React-safe native setter) is the spike's, unchanged —
 * only the data source moved from the clipboard to the sidecar.
 *
 * Out of scope here (later tasks): CV upload (task 3), auto-mark-submitted
 * (task 4), gated auto-submit (task 5), iframe traversal (top frame only).
 */

"use strict";

let MATCH = null; // the pending snapshot for this page, if any

if (window.top === window.self) {
  injectPanel();
  findPending();
}

function bg(msg) {
  return new Promise((resolve) => chrome.runtime.sendMessage(msg, resolve));
}

// ── panel ────────────────────────────────────────────────────────────────────
function injectPanel() {
  if (document.getElementById("jh-spike-panel")) return;
  const panel = document.createElement("div");
  panel.id = "jh-spike-panel";
  panel.style.cssText =
    "position:fixed;top:12px;right:12px;z-index:2147483647;width:380px;" +
    "max-height:82vh;overflow:auto;background:#111;color:#eee;" +
    "font:12px/1.45 ui-monospace,Menlo,monospace;border:1px solid #444;" +
    "border-radius:8px;padding:10px;box-shadow:0 6px 22px rgba(0,0,0,.55)";
  const btn =
    "cursor:pointer;border:1px solid #555;background:#1e1e1e;color:#eee;" +
    "border-radius:5px;padding:5px 9px;font:inherit";
  panel.innerHTML =
    '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">' +
    "<b>job-hunter autofill</b>" +
    '<button id="jh-close" style="' + btn + '">×</button></div>' +
    '<button id="jh-fill" style="' + btn + ';width:100%" disabled>checking…</button>' +
    '<div id="jh-status" style="margin-top:6px;color:#888"></div>' +
    '<div id="jh-out" style="margin-top:8px;color:#aaa"></div>';
  document.documentElement.appendChild(panel);
  panel.querySelector("#jh-fill").addEventListener("click", run);
  panel.querySelector("#jh-close").addEventListener("click", () => panel.remove());
}

// ── find the draft for this page ───────────────────────────────────────────────
async function findPending() {
  const status = document.getElementById("jh-status");
  const fill = document.getElementById("jh-fill");
  const res = await bg({ type: "pending" });
  if (!res || !res.ok) {
    fill.textContent = "not connected";
    status.innerHTML = red((res && res.error) || "no response") + " — check options.";
    return;
  }
  const host = location.host.replace(/^www\./, "");
  MATCH = res.data.find((s) => s.host && hostMatch(host, s.host));
  if (MATCH) {
    fill.textContent = "Fill — " + MATCH.company;
    fill.disabled = false;
    status.textContent =
      "snapshot #" + MATCH.snapshot_id + " · " + (MATCH.ats || "?") + " · T" + MATCH.tier;
  } else {
    fill.textContent = "No draft for this page";
    fill.disabled = true;
    status.textContent = res.data.length + " pending on other pages";
  }
}

function hostMatch(a, b) {
  return a === b || a.endsWith("." + b) || b.endsWith("." + a);
}

// ── fill ───────────────────────────────────────────────────────────────────────
async function run() {
  if (!MATCH) return;
  const out = document.getElementById("jh-out");
  out.textContent = "fetching draft…";
  const res = await bg({ type: "snapshot", id: MATCH.snapshot_id });
  if (!res || !res.ok) {
    out.innerHTML = red("draft fetch failed: " + ((res && res.error) || "?"));
    return;
  }
  const actions = ((res.data.form_payload || {}).actions) || [];
  if (!actions.length) {
    out.innerHTML = red("no actions in this draft");
    return;
  }
  fillActions(out, actions);
}

function fillActions(out, actions) {
  // Pass 1 — measure (no fill): selector target vs label target, per field.
  const rows = actions.map((a) => {
    const selEl = a.selector ? safeQuery(a.selector) : null;
    const labEl = a.label ? resolveByLabel(a.label) : null;
    return { a, selEl, labEl, selFound: !!selEl, labFound: !!labEl, agree: !!selEl && selEl === labEl };
  });
  // Pass 2 — fill via selector, falling back to label.
  for (const r of rows) {
    const target = r.selEl || r.labEl;
    r.filledBy = r.selEl ? "selector" : r.labEl ? "label" : null;
    r.detected = !!target;
    if ((r.a.kind || "").toLowerCase() === "file") {
      r.filled = false; // CV upload is task 3
      r.note = "file (task 3)";
    } else {
      r.filled = target ? fillField(target, r.a) : false;
    }
  }
  out.innerHTML = renderTable(rows);
  // eslint-disable-next-line no-console
  console.table(
    rows.map((r) => ({
      label: r.a.label, kind: r.a.kind, selector: r.selFound,
      label_match: r.labFound, agree: r.agree, filledBy: r.filledBy, filled: r.filled,
    }))
  );
}

// ── B: selector replay ─────────────────────────────────────────────────────────
function safeQuery(sel) {
  try {
    const el = document.querySelector(sel);
    if (el) return el;
  } catch (_e) {
    /* invalid CSS (e.g. digit-leading id) — fall through */
  }
  // Personio & co. use digit-prefixed UUID ids; #123 is invalid CSS.
  const m = sel.match(/^#(.+)$/);
  if (m) {
    try {
      return document.querySelector('[id="' + cssEscape(m[1]) + '"]');
    } catch (_e) {
      return null;
    }
  }
  return null;
}

// ── A: label match ──────────────────────────────────────────────────────────
// A tiny German/English alias set — enough to test whether label matching can
// carry the long tail. Each group's first token is the canonical concept.
const ALIAS_GROUPS = [
  ["first name", "first", "vorname", "given name"],
  ["last name", "last", "nachname", "surname", "family name"],
  ["email", "e-mail", "email address", "e-mail-adresse"],
  ["phone", "telephone", "telefon", "phone number", "mobile", "mobil"],
  ["salutation", "anrede", "gender", "geschlecht", "title", "titel"],
  ["linkedin", "linkedin url", "linkedin profile"],
  ["available from", "earliest start date", "verfügbar ab", "eintrittstermin", "start date"],
  ["country", "land"],
  ["city", "stadt", "ort"],
  ["cover letter", "anschreiben", "motivation"],
  ["resume", "cv", "lebenslauf"],
  ["salary", "gehalt", "gehaltsvorstellung", "salary expectation"],
];

function resolveByLabel(rawLabel) {
  const want = normalize(rawLabel);
  if (!want) return null;
  const wantAlias = aliasKey(want);
  const fields = fillableFields();

  const scored = [];
  for (const el of fields) {
    const have = normalize(fieldLabel(el));
    if (!have) continue;
    let score = 0;
    if (have === want) score = 100;
    else if (have.includes(want) || want.includes(have)) score = 70;
    else if (wantAlias && wantAlias === aliasKey(have)) score = 60;
    if (score) scored.push({ el, score });
  }
  if (!scored.length) return null;
  scored.sort((a, b) => b.score - a.score);
  // ambiguity guard (drift_recovery discipline): a tie at the top = don't guess.
  if (scored.length > 1 && scored[1].score === scored[0].score) return null;
  return scored[0].el;
}

function fillableFields() {
  return [...document.querySelectorAll("input, select, textarea")].filter((el) => {
    const t = (el.type || "").toLowerCase();
    if (["hidden", "submit", "button", "reset", "image"].includes(t)) return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0; // visible only
  });
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
function fillField(el, a) {
  try {
    if (el.tagName === "SELECT") return fillSelect(el, a.value);
    const t = (el.type || "").toLowerCase();
    if (t === "checkbox" || t === "radio") {
      setNativeChecked(el, true);
      return el.checked === true;
    }
    setNativeValue(el, a.value == null ? "" : String(a.value));
    return el.value === (a.value == null ? "" : String(a.value));
  } catch (_e) {
    return false;
  }
}

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

// ── reporting ────────────────────────────────────────────────────────────────
function renderTable(rows) {
  const n = rows.length;
  const sel = rows.filter((r) => r.selFound).length;
  const lab = rows.filter((r) => r.labFound).length;
  const filled = rows.filter((r) => r.filled).length;
  const cell = "padding:2px 4px;border-bottom:1px solid #2a2a2a;vertical-align:top";
  const head =
    '<div style="margin:6px 0;color:#ddd">selector ' + pct(sel, n) +
    " · label " + pct(lab, n) + " · filled " + pct(filled, n) + "</div>";
  const body = rows
    .map((r) => {
      const lbl = (r.a.label || r.a.selector || "?").slice(0, 28);
      return (
        '<tr><td style="' + cell + '">' + esc(lbl) + "</td>" +
        '<td style="' + cell + '">' + esc(r.a.kind || "") + "</td>" +
        '<td style="' + cell + ';text-align:center">' + mark(r.selFound) + "</td>" +
        '<td style="' + cell + ';text-align:center">' + mark(r.labFound) + "</td>" +
        '<td style="' + cell + ';text-align:center">' + (r.agree ? "=" : "") + "</td>" +
        '<td style="' + cell + '">' + esc(r.filledBy || r.note || "—") + "</td>" +
        '<td style="' + cell + ';text-align:center">' + mark(r.filled) + "</td></tr>"
      );
    })
    .join("");
  return (
    head +
    '<table style="width:100%;border-collapse:collapse">' +
    '<thead><tr style="color:#888">' +
    ["field", "kind", "sel", "lbl", "=", "by", "ok"]
      .map((h) => '<th style="' + cell + ';text-align:left">' + h + "</th>")
      .join("") +
    "</tr></thead><tbody>" + body + "</tbody></table>"
  );
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

function aliasKey(norm) {
  for (const group of ALIAS_GROUPS) {
    if (group.some((tok) => norm === tok || norm.includes(tok))) return group[0];
  }
  return null;
}

function cssEscape(s) {
  return window.CSS && CSS.escape ? CSS.escape(s) : s.replace(/["\\]/g, "\\$&");
}

function mark(ok) {
  return ok ? '<span style="color:#5fd35f">✓</span>' : '<span style="color:#e06666">✗</span>';
}

function pct(k, n) {
  return k + "/" + n + " (" + (n ? Math.round((100 * k) / n) : 0) + "%)";
}

function red(msg) {
  return '<span style="color:#e06666">' + esc(msg) + "</span>";
}

function esc(s) {
  return String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}
