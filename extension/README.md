# job-hunter autofill (MV3 extension)

Fills a structured-ATS application form from the **reviewed draft** served by the
local `apply_api` sidecar — fields, the CV, and bookkeeping — so applying is a
review-and-submit, not a copy-paste. The fill engine (selector replay with a
label-match fallback, React-safe native setter) is the one the spike proved at
100 % on Greenhouse/Personio/Ashby. See [PHASE1_PLAN.md](./PHASE1_PLAN.md); the
spike's clipboard harness lives in git history (commit `077c1c8`).

The extension fills and books; **the human reviews and presses the ATS's submit
button.** Hands-off auto-submit is deliberately not built (PHASE1_PLAN task 5,
deferred — the review is the safety, and the saving is marginal).

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

## Run loop

1. Open a job's apply page **logged in** (a structured ATS the content script
   runs on: greenhouse / ashby / personio / lever / workable).
2. The panel (top-right) auto-detects the draft and shows **Fill — {company}**.
   Click it → fields fill, the CV uploads, a per-field result table appears.
3. Review the form, then **submit it yourself** on the ATS.
4. Bookkeeping: the confirmation page usually auto-books it (panel shows
   "✓ marked submitted"). If it doesn't, click **✓ I submitted it** in the panel
   (the authority), or the dashboard's "mark submitted" (backstop).

- **Toolbar icon** toggles the panel — use it to bring the panel back if you
  closed it with ×.

## Architecture

- `manifest.json` — MV3; runs on the structured-ATS hosts, top frame only
  (iframe traversal deferred). `host_permissions` covers `127.0.0.1:8531`; an
  `action` (toolbar icon) toggles the panel.
- `background.js` — service worker; holds base+token in `chrome.storage`, runs
  all fetches to the sidecar (so the content script never hits page CORS), and
  routes the toolbar click to the content script.
- `content.js` — the panel (in a **closed shadow root**, isolated from page /
  other-extension CSS), host→draft match, the fill engine, CV upload via
  DataTransfer, the result table, the authoritative "I submitted it" button, and
  a best-effort confirmation watch.
- `options.html` / `options.js` — token + base, with a connection test.

## Bookkeeping design

Marking a job *submitted* is **ground truth from the human** (the "I submitted
it" button), not detection. The confirmation-text watch is best-effort only —
when it fires it saves the click; a miss is harmless. Network-level detection
(`webRequest`) was rejected: it can't cleanly tell the submit POST from
autosave/analytics noise, MV3 hides response bodies, and it needs a broad
permission. See PHASE1_PLAN.md.

## Security

The sidecar serves the CV + personal data: it binds **127.0.0.1 only** and
requires the bearer token. Never publish it on `0.0.0.0`. The token lives in
`.env` (gitignored) and `chrome.storage` (not synced).

## Validated end-to-end (2026-06-26)

- **Personio** (Peter Park): fields + 3 selects + **CV** all filled; real submit
  → booked submitted, job → applied.
- **Greenhouse** (Solaris): 12/12 selector, 11/12 filled (the one miss is an
  intl-tel custom phone widget — known v2 long-tail).
- **Ashby** (Payrails): fill verified in the spike (the posting is now expired).
- **softgarden** (Wackler): account wall — out of scope, stays manual.
