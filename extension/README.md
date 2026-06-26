# autofill spike (throwaway)

A Manifest V3 content script that fills a live application form from a
`form_payload` placed on the clipboard, and reports — per field — whether the
**selector** (B) or the **label** (A) strategy found the target. This answers the
one question in [SPIKE_PLAN.md](./SPIKE_PLAN.md) before any real build.

It touches no network and submits nothing. See SPIKE_PLAN.md §1 for scope.

## Load it

1. Chrome → `chrome://extensions` → enable **Developer mode**.
2. **Load unpacked** → pick this `extension/` folder.
3. (For the offline fixture via `file://`) open the extension's details and turn
   on **Allow access to file URLs**. Or serve over http (below).

## Offline sanity check (no login needed)

```
python3 -m http.server -d extension 8000
# open http://localhost:8000/fixture.html
```

Click **Copy payload** on the page, then click **Fill from clipboard** in the
panel (top-right). Expect: all fields ✓, `#nope` filled via *label* (selector
misses on purpose), the digit-id phone filled via the `[id="..."]` fallback, and
the controlled First Name staying filled (native-setter path).

## Real run loop (SPIKE_PLAN.md §6)

For each test job (Greenhouse Solaris / Ashby Payrails / Personio Peter Park /
softgarden Wackler):

1. In the dashboard review card, expand **🧪 Spike: payload JSON** and copy it.
2. Open the job's apply page **logged in** (for softgarden, start a fresh
   session — the stored URL is one-time).
3. Click **Fill from clipboard** in the panel.
4. Read the result table (selector% / label% / filled%) and screenshot it.

Collect the four tables → apply the decision matrix in SPIKE_PLAN.md §5.

## Files

- `manifest.json` — MV3; runs on the four test hosts + localhost/file fixture;
  top frame only (iframe traversal deferred).
- `content.js` — panel, clipboard read, both strategies, fill primitives
  (React-safe native setter), result table.
- `fixture.html` — offline form to validate the primitives.
