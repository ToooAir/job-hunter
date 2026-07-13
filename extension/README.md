# job-hunter autofill (MV3 extension)

The browser half of the semi-automatic apply flow. It talks only to the local
`apply_api` sidecar (`127.0.0.1:8531`) and does two things:

1. **Fill profile facts into any form** — click **Fill facts from profile** and
   the content script live-extracts the page's fields, asks the sidecar
   (`POST /fill-plan`) which map to a profile fact, and fills those (including
   the **CV** into resume file inputs) via a React-safe native setter. Facts are
   job-independent, so no draft/snapshot is needed; **open questions have no
   fact and stay blank — never invented.**
2. **Answer the open questions + book the result** — an in-page panel gives
   grounded, copy-paste answers (salary, cover letter, free-text), matches
   decision emails to an application, and records the submission back into the
   tracker.

**The human always reviews and presses the ATS's own Submit button.** Hands-off
auto-submit is deliberately not built (PHASE1_PLAN task 5, deferred — the review
is the safety, and the saving is marginal).

> The old selector-replay engine that filled from a pre-generated snapshot was
> **retired 2026-07-02**; filling is now snapshot-free (`/fill-plan`), so the
> panel works on **any** page — disguised ATS on career sites and hand-found
> jobs included — not just pages a Stage-1 draft was generated for.

## Setup

1. **Start the sidecar** with a token (host shell, once):
   ```
   echo "APPLY_API_TOKEN=$(openssl rand -hex 24)" >> .env
   docker compose up -d apply_api          # publishes 127.0.0.1:8531 only
   ```
2. **Load the extension**: Chrome → `chrome://extensions` → **Developer mode** →
   **Load unpacked** → pick this `extension/` folder. (After any change here, hit
   the extension's **Reload** ↻.)
3. **Configure it**: open the extension **Options**, paste the `APPLY_API_TOKEN`
   from `.env` (base defaults to `http://127.0.0.1:8531`). **Test connection** →
   expect "OK — N pending drafts".

## The panel

Click the **toolbar icon** to summon (or hide) the panel on **any** page — it is
injected on demand, so it works beyond the declared ATS hosts. It lives in a
closed shadow root, isolated from the page's CSS.

| Button | What it does |
|---|---|
| **Fill facts from profile** | Fill matched profile fields + upload the CV (see above). |
| **Answer from my background** | Paste a form's open question → one grounded, human-reviewed answer + **Copy**. Never typed into the page — reading before pasting is the review gate. |
| **💰 Salary expectation** | One-click salary answer, backed by the job's salary estimate (server-cached). |
| **📄 Cover letter** | The reviewed cover letter for the job being applied to, ready to copy. |
| **✉️ Match email to application** | Paste a decision email (rejection / interview invite); the server classifies it and nominates from your closed active-application list, and the offered button is derived from the email's intent. Booking is your click. |
| **✓ I submitted it** | The authoritative "I applied" signal (see Bookkeeping). |

### 🎯 Focus — which job is this?

Answers and the cover letter must be grounded in the *right* job. Host matching
alone can't tell (aggregator boards put every job on one host; multi-tenant ATS
like SuccessFactors/Workday share a host across employers). So the dashboard's
**🎯 (I'm applying to this one)** button is the primary signal: press it on the
job, and the panel grounds `/answer` and `/cover-letter` on it (TTL-guarded).

A 🎯 on a job that has **no draft** still works: the panel shows the focused
company and **✓ I submitted it** books it applied at the job level (the same as
the dashboard's ✅ button — there is just no snapshot to advance).

## Run loop

1. Open a job's apply page **logged in**.
2. If the job isn't unambiguously identified by the page (aggregator / redirect /
   multi-tenant ATS), press **🎯** on it in the dashboard first.
3. **Fill facts from profile** → review the filled fields and the per-field
   result table; answer any open questions with the panel's buttons.
4. **Submit it yourself** on the ATS.
5. Bookkeeping: the confirmation page usually auto-books it (panel shows
   "✓ marked submitted"). If it doesn't, click **✓ I submitted it** (the
   authority), or the dashboard's "mark submitted" (backstop).

## Architecture

- `manifest.json` — MV3. Declarative `content_scripts` run on the structured-ATS
  hosts (greenhouse / ashby / personio / lever / workable) with `all_frames` so
  disguised ATS iframes get the panel; `host_permissions` covers
  `127.0.0.1:8531`; the toolbar `action` toggles/injects the panel on any page.
  Toolbar/store icons live in `icons/` (`icon.png` is the master).
- `background.js` — service worker + API client: holds base+token in
  `chrome.storage`, runs every fetch to the sidecar (so the content script never
  hits page CORS), and on the toolbar click enumerates the tab's frames and
  `executeScript`s `content.js` into the top frame + any ATS subframe.
- `content.js` — the panel (closed shadow root), the snapshot-free fill engine
  (`/fill-plan` + React-safe native setter, CV upload via DataTransfer), the
  answer/cover-letter/email panel, host→draft match with 🎯 focus fallback, the
  authoritative "I submitted it" booking, and a best-effort confirmation watch.
- `options.html` / `options.js` — token + base, with a connection test.

See [PHASE1_PLAN.md](./PHASE1_PLAN.md) and
[ANSWER_PANEL_PLAN.md](./ANSWER_PANEL_PLAN.md) for the design rationale.

## Bookkeeping design

Marking a job *submitted* is **ground truth from the human** (the "I submitted
it" button), not detection. With a draft snapshot it advances the snapshot
lifecycle and abandons sibling drafts for the same company; on a draft-less 🎯
focus it books the job applied directly. The confirmation-text watch is
best-effort only — when it fires it saves the click; a miss is harmless.
Network-level detection (`webRequest`) was rejected: it can't cleanly tell the
submit POST from autosave/analytics noise, MV3 hides response bodies, and it
needs a broad permission. See PHASE1_PLAN.md.

## Security

The sidecar serves the CV + personal data: it binds **127.0.0.1 only** and
requires the bearer token. Never publish it on `0.0.0.0`. The token lives in
`.env` (gitignored) and `chrome.storage` (not synced).

## History

The fill engine (selector replay with a label-match fallback) was proven at
100 % on Greenhouse/Personio/Ashby in the spike (clipboard harness in git
history, commit `077c1c8`), and validated end-to-end on 2026-06-26 (Personio /
Greenhouse real submits → booked applied). That snapshot-bound engine was
retired on 2026-07-02 in favour of the snapshot-free `/fill-plan` model
described above; the selector-replay validation is what it inherits its
confidence from.
