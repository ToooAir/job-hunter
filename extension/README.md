# job-hunter autofill (MV3 extension)

Fills a structured-ATS application form from the **reviewed draft** served by the
local `apply_api` sidecar. The fill engine (selector replay + label-match
fallback, React-safe native setter) is the one the spike proved at 100% on
Greenhouse/Personio/Ashby; Phase 1 (see [PHASE1_PLAN.md](./PHASE1_PLAN.md))
swapped the clipboard for the sidecar so it can also pull the CV and book the
application submitted. The spike's clipboard harness lives in git history
(commit `077c1c8`).

## Setup

1. **Start the sidecar** and give it a token (host shell, once):
   ```
   echo "APPLY_API_TOKEN=$(openssl rand -hex 24)" >> .env
   docker compose up -d apply_api          # publishes 127.0.0.1:8531 only
   ```
2. **Load the extension**: Chrome → `chrome://extensions` → **Developer mode** →
   **Load unpacked** → pick this `extension/` folder. (After any code change here,
   hit the extension's **Reload** ↻.)
3. **Configure it**: open the extension's **Options**, set the token (the
   `APPLY_API_TOKEN` value from `.env`); base defaults to `http://127.0.0.1:8531`.
   Click **Test connection** → expect "OK — N pending drafts".

## Run loop

1. Open a job's apply page **logged in** (one of the structured ATS the content
   script runs on: greenhouse / ashby / personio / lever / workable).
2. The panel (top-right) auto-detects the draft for this page and shows
   **Fill — {company}**. Click it.
3. Read the per-field result table (selector% / label% / filled%). Review the
   form, then submit it yourself. *(CV upload, auto-mark-submitted and gated
   auto-submit arrive in tasks 3–5.)*

## Architecture

- `manifest.json` — MV3; runs on the structured-ATS hosts, top frame only
  (iframe traversal deferred). `host_permissions` covers `127.0.0.1:8531`.
- `background.js` — service worker; holds base+token in `chrome.storage`, runs
  all fetches to the sidecar (so the content script never hits page CORS).
- `content.js` — panel, host→draft match, the fill engine, result table.
- `options.html` / `options.js` — token + base, with a connection test.

## Security

The sidecar serves the CV + personal data: it binds **127.0.0.1 only** and
requires the bearer token. Never publish it on `0.0.0.0`. The token lives in
`.env` (gitignored) and `chrome.storage` (not synced).
