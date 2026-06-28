# Spike Plan: Browser-Extension Autofill — field-matching de-risk

> Status: **planned, not started** (2026-06-26). This spike changes no existing
> behaviour. It exists to answer one question before any real build.

## 0. The one question this spike answers

Can a content script, running on the **live apply page in the user's real
(logged-in) browser**, put the right values into the right fields — and which
strategy wins: **B = selector replay** vs **A = label match**?

Everything else (transport layer, CV upload, submit click, captcha detection,
bookkeeping) is secondary plumbing and is explicitly **out of this spike**.

### Why an extension at all
After Stage 2 (host-CDP auto-submit) was removed, the human copies the answer
sheet onto the real form by hand, one value at a time. An extension running
*inside* the browser the human already uses dodges the exact thing that killed
Stage 2 (a container cannot drive the host's Chrome) and collapses the copy-paste
dance into one click — while the human still reviews and presses submit (red line
intact). It also fills multi-step wizards page-by-page as the user navigates,
which Stage 1's headless pre-extraction never could.

## 1. Scope

**IN:**
- `extension/` — a standalone MV3 extension; a floating "Fill" button on the page.
- Read one `form_payload` from the **clipboard** (the clipboard-as-transport idea,
  zero network — perfect throwaway harness for the spike).
- For each field, run **both A and B**, record per-field hit/miss.
- A **hit-rate table** (per page × per strategy).
- A "Copy payload (spike)" button on the dashboard review card that puts the
  snapshot's `form_payload` JSON on the clipboard.

**OUT (deferred until the core is proven):**
- Transport layer (a container port / local endpoint). Spike hand-feeds via clipboard.
- Actual CV/file **upload execution** (the field is detected, not filled).
- Submit click, captcha detection, auto-mark-submitted.
- iframe / `frame_path` traversal (flagged v2 risk; test set picks top-level forms).
- Generated content for later wizard pages (only known-data fields are filled).

## 2. Architecture (minimal)

```
extension/
  manifest.json   # MV3; content_scripts on the 4 test hosts; all_frames:true;
                  # permissions: clipboardRead, activeTab
  content.js      # floating panel + two strategies + result table;
                  # NO background worker (spike does not touch the network)
```

`content.js` flow: inject floating panel → click "Fill" (this click is the user
gesture clipboard read needs) → `navigator.clipboard.readText()` → `JSON.parse`
→ run both strategies → render result table in the panel + console.

## 3. The two strategies — measure first, then fill

**Pass 1 (dry, no fill — pure measurement):** for each action compute
- `selectorTarget = querySelector(action.selector)`
- `labelTarget = resolveByLabel(action.label)` (normalize + a small German alias
  set: Anrede / Vorname / Nachname / E-Mail / Telefon …)

Record per field: `selector_found? / label_found? / same element?`

**Pass 2 (real fill — visibly verifiable):** fill via `selectorTarget ||
labelTarget` by `kind`:
- text/email/tel/textarea → **native value setter + dispatch `input`/`change`**.
  ⚠️ Greenhouse/Ashby are React; assigning `.value` directly is ignored by React
  yet *looks* filled — the spike MUST use the native-setter trick from the start
  or the result table lies.
- select → match options by value/text.
- checkbox → set checked + dispatch.
- file → **detected and counted only, NOT uploaded.**

## 4. Test set (real jobs from the queue)

| # | Engine | Company (sid) | Stresses | Sample labels | apply_url |
|---|---|---|---|---|---|
| 1 | **Greenhouse** | Solaris (47) | selector replay **best case**; text×10+tel+textarea | First Name * / Email * / Phone * | `job-boards.greenhouse.io/solarisbank/jobs/8419465002` |
| 2 | **Ashby** | Payrails (57) ⚠️ | second structured engine (different selector shape) | Name / Resume / Earliest Start Date | `jobs.ashbyhq.com/payrails/d5a82e6a-2adc-4a82-8e78-730199a68639/application` |
| 3 | **Personio** | Peter Park (30) | digit-prefixed ids; select×3 + file | First / Available from / LinkedIn | `peter-park.jobs.personio.de/job/2655559?apply` |
| 4 | **softgarden (multi-step wizard)** | Wackler (54) | page-by-page fill + German labels + session-bound URL | Anrede / Vorname * / E-Mail * | `wackler-group.softgarden.io/applications/ea22dbf2-1219-42e8-9137-a0258c717e53?...&isNew=true` |

**Caveats on the test set:**
- **#2 Ashby (Payrails) job is `expired`** → the posting may be gone. Confirm the
  form loads before spiking; if not, regenerate a fresh Ashby draft
  (`apply_stage1.py --source …`) and swap it in.
- **#4 softgarden URL is session-bound** (`isNew=true` + uuid) → do NOT open the
  stale link; enter a fresh session from the job entry and test the live form
  (which also proves the "match the live page, not the stored URL" premise).
- A pure "messy self-built" case is thin in the current queue (most resolve to
  board/Tier 3). #3's weird ids + #4's German labels already stress the label
  fallback; regenerate a self-built draft later if a pure case is wanted.

## 5. Decision matrix (the spike output → the call it makes)

| Observation | Conclusion → how the real build goes |
|---|---|
| B ≥ ~90% on Greenhouse/Ashby | selector replay is the **backbone** for structured ATS (fast, precise) |
| B collapses on Personio/softgarden but A recovers the majority | label match is a long-tail **necessity**; also confirms wizard page-by-page fill works |
| both weak everywhere | Stage 1's extracted field data is **not portable** → reinforce live extraction, or drop the extension and go API-only (B1 structured ATS) |

"Hit" = the value ended up in the **intended** field (panel marks ✓/✗ per field, screenshot as evidence).

## 6. Run loop

1. dashboard: add "Copy payload (spike)" button on the review card (tiny, additive).
2. Load the unpacked extension in Chrome (`chrome://extensions` → Load unpacked).
3. Per test job: open its apply_url (logged in) → copy that snapshot's payload in
   the dashboard → back on the form, click "Fill" → read the result table → screenshot.
4. Collect the 4 tables → apply the decision matrix.

## 7. Effort & footprint

- **~1–2 days.** New `extension/` dir + one dashboard button.
- **Touches nothing** in the pipeline / generation chain / apply flow / state
  machine. Fully disposable.

## 8. Risk list

- React controlled inputs → native setter (built into §3).
- `clipboardRead` permission + user gesture → the Fill click provides the gesture.
- iframe forms → out of this spike, flagged v2.
- session-bound URLs (softgarden) → enter a fresh session.
- some sites' CSP / custom upload widgets → only affects upload (already OUT).

---

## Context this sits in (why the extension, not auto-submit)

The "back half" (manual submission) is two different kinds of labour:
mechanical copy-paste/upload (highly automatable) vs captcha/account-walls
(genuine human floor). Rough split of the live pool: ~1/4 structured ATS (truly
hands-off via API — route **B1**), ~1/3 no-captcha self-built (one-click via this
extension — route **A1**), ~1/3 captcha/account/board (stays manual — correct).

Goal tiers the user set:
1. **Full 0-intervention** — still wanted. Only safely reachable on the
   structured-ATS subset (stable schema, no captcha) and needs CV bytes →
   requires the **port transport**, not the clipboard. Clipboard can't carry a file.
2. **Near 0-intervention** (extension fills, human presses submit) — good idea,
   parked for later. Clipboard transport is enough here.
3. **Must intervene** (captcha/account) — accepted as a hard floor.

Transport decision: **clipboard** serves goal 2 and is the spike's harness; a
**container port + tiny FastAPI sidecar** (background worker fetches localhost,
bypassing page CORS; bind 127.0.0.1 + token) is the production transport that
unlocks goal 1 (CV bytes + auto-mark-submitted). The spike deliberately does NOT
build the port — field matching is the high-risk unknown; transport is low-risk
known plumbing, validated separately.
