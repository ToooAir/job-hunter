# Phase 1 Plan: production autofill — port transport + CV upload + bookkeeping

> Status: **planned, not started** (2026-06-26). Builds directly on the spike,
> whose result was decisive (see SPIKE_PLAN.md §"Spike result"): selector replay
> hit 100% on Greenhouse/Personio/Ashby incl. real React inputs; the only fill
> gap was the file upload (out of the spike's scope). Phase 1 closes that gap and
> turns the throwaway harness into a real tool.

## 0. Goal

Make applying to a **structured-ATS** posting a one-click (and, opt-in, zero-click)
act, while keeping the red line: the human reviewed the content in the dashboard
first, and nothing free-text is submitted unseen.

Concretely, Phase 1 delivers **goal 2 in full** (extension fills everything incl.
the CV, the human presses submit) plus **auto-bookkeeping** (confirmation →
marked submitted), and makes **goal 1** (zero-intervention) reachable as a
tightly-gated opt-in. See SPIKE_PLAN.md §"Context" for the goal tiers.

## 1. Why a port now (not the clipboard)

The clipboard proved the mechanism but is a dead end for the real thing:
- it can't carry the **CV bytes** (the one fill gap the spike found), and
- it can't push **"submitted"** back (the bookkeeping the project keeps losing).

So Phase 1 introduces the production transport the SPIKE_PLAN reserved: a small
container-side HTTP sidecar on host-loopback, fetched by the extension's
background worker (which bypasses page CORS via `host_permissions`).

## 2. Architecture

```
container (new service)            host browser (extension)
  apply_api.py  (FastAPI)            background.js  (service worker)
   127.0.0.1:8531, token-gated  <--   - holds token + API base
   reads jobs.db via snapshot_io      - does all fetches (no page CORS)
                                       - messaged by content.js
                                     content.js (on ATS pages)
                                       - match page URL -> pending snapshot
                                       - fill (selector||label, proven)
                                       - set CV file via DataTransfer
                                       - detect confirmation -> POST submitted
                                     options.html  (token + API base)
```

### 2a. Sidecar `apply_api.py` (FastAPI, new compose service)
- Bind **127.0.0.1 only**; publish `127.0.0.1:8531:8531` (host-loopback, never
  0.0.0.0). Bearer token from `.env` (`APPLY_API_TOKEN`), checked on every route.
- Reuses `utils/snapshot_io` + `utils/db` — no new data model.
- Endpoints:
  - `GET  /pending` → `[{snapshot_id, job_id, company, apply_url, host, tier, ats}]`
    for `status='draft'` snapshots (the review queue). Lets the extension match
    the current tab's URL to the right snapshot — no manual copy.
  - `GET  /snapshot/{id}` → full `form_payload`, `custom_qa`, `cover_letter`.
  - `GET  /snapshot/{id}/cv` → the CV bytes (`application/pdf`) from the profile.
  - `POST /snapshot/{id}/submitted` → `mark_submitted(...)` (the existing
    draft→submitted + job applied + same-company abandon).
- Shares the image (like dashboard); command `uvicorn apply_api:app`.

### 2b. Extension upgrade (MV3)
- `background.js` (service worker): stores token/base in `chrome.storage`; runs
  all `fetch`es to `127.0.0.1:8531` (host_permissions cover it → no page CORS).
- `content.js`: on an ATS page, ask background for a pending snapshot whose host
  matches the tab → show **"Fill — {company}"**. On click: pull payload + CV,
  fill (the spike engine, unchanged), set the file input, render the result
  table. On confirmation detection → tell background to POST submitted.
- `options.html`: token + API base.
- manifest: add `background`, `host_permissions: http://127.0.0.1:8531/*`,
  `permissions: [storage]`; **drop** `clipboardRead`.

### 2c. CV upload (DataTransfer)
- background `GET /snapshot/{id}/cv` → ArrayBuffer → content builds a `File`,
  puts it in a `DataTransfer`, sets `input.files`, dispatches `change`.
- Works on real `<input type=file>` (the spike confirmed Personio/Ashby expose
  one). Custom drag-drop widgets that aren't real inputs → detect and fall back
  to "attach manually" (a v2 long-tail, same family as the phone widget).

### 2d. Auto-bookkeeping (kills "forgot to mark submitted")
- Confirmation detection = URL change + thank-you patterns
  (`Danke|Vielen Dank|thank you|received|eingegangen|bewerbung.*eingegangen`),
  ported from the deleted apply_session watch (git history).
- On detect → `POST /snapshot/{id}/submitted`. This is the *only* state write the
  extension makes.

## 3. Submit policy — the careful gradient (red line intact)

- **Default = goal 2**: extension fills everything (incl. CV); the **human
  presses submit**. Already the whole win — copy-paste and CV upload gone.
- **Opt-in = goal 1**, gated by ALL of: snapshot verifier `pass` + zero
  `fabrication` flags; `ats ∈ {ashby, greenhouse, lever, workable}`; **no captcha
  detected on the page**; an explicit per-session flag. Even then, a **3-2-1
  countdown with Cancel** before the click (near-0, not blind). Truly unattended
  submit stays a later, separate toggle.
- This mirrors the project's prepare→submit progression and `auto_approve_tier1`
  kill-switch style. The seam already noted at `apply_verifier.py:151` is where
  the verifier-clean condition is computed.

## 4. Scope

**IN:** sidecar + 4 endpoints + token; extension background/options/content
upgrade; URL→snapshot matching; CV upload; auto-mark-submitted; gated auto-submit
with countdown.

**OUT (later phases):** the phone/custom-widget special-casing; multi-step wizard
page-by-page beyond known data; account-wall ATS (softgarden/join/indeed — stays
manual answer sheet); iframe-embedded forms; truly unattended (no-countdown)
auto-submit.

## 5. Security

- 127.0.0.1 bind + bearer token; serves CV + PII so never expose to 0.0.0.0.
- Token in `.env` (gitignored) and `chrome.storage` (not synced).
- Read-only except the single `submitted` POST, which only advances a known
  lifecycle transition via `snapshot_io`.

## 6. Tasks

1. `apply_api.py` FastAPI sidecar + compose service (127.0.0.1:8531 + token) +
   the 4 endpoints over `snapshot_io`/`db`; unittest the endpoints + auth in the
   container (pending list, snapshot fetch, cv bytes, submitted transition).
2. Extension: `background.js` worker + `options.html` (token/base) + content
   script pulls snapshot by URL match (replaces clipboard); manifest perms.
3. CV upload via DataTransfer (background fetches bytes → File → input.files).
4. Auto-mark-submitted on confirmation detection (port thank-you patterns).
5. Gated auto-submit (verifier-clean + structured ATS + no captcha + opt-in +
   3-2-1 countdown/Cancel).
6. README run loop + real validation on the 3 reachable ATS (Greenhouse,
   Personio, Ashby); confirm CV lands and bookkeeping fires.

## 7. Footprint & effort

- New `apply_api.py` + one compose service; extension grows a background worker
  and options page. Pipeline/generation untouched; the one DB write reuses the
  existing `mark_submitted`. Rough effort ~3-4 focused days.

## 8. Risks

- Custom file widgets (not real inputs) reject DataTransfer → fall back to manual.
- ATS that re-validate phone/format client-side (Greenhouse phone) → known v2.
- Confirmation pattern misses on some sites → fall back to the dashboard
  "mark submitted" button (kept as backstop).
- Token/port hygiene → 127.0.0.1 + token, documented in README.
