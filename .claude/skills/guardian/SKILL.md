---
name: guardian
description: Queue-health guardian pass — classify recent abandons, split live holes from old-stock residue, fix with regression tests, repair data. Run when an abandon-tally bucket gets fat or after a big pipeline change.
---

# Guardian

Root-cause pass over the apply review queue's recent abandons. The daily
`放棄近7天` tally line (stage1 accounting, `utils/snapshot_io.abandon_tally`)
is the dial; this skill is the technician. Produce the prioritized cause
list BEFORE touching any code.

## Ground rules

- DB access: always `docker exec job-hunter-pipeline-1 python3 -c ...`
  (never from macOS host — WAL corruption; container has no sqlite3 CLI,
  no pgrep/ps). Read-only queries use
  `sqlite3.connect('file:/app/data/jobs.db?mode=ro', uri=True)`.
- Known-unfixable floors — do NOT re-litigate, just count them:
  - zombie boards: wearedevelopers/indeed pages render full JD + Apply
    after the external target dies (byte-identical to live) → `expired`
    on arrival is expected background noise
  - `not-qualified` / `heavy-form`: irreducible human judgment
- Auto-abandons (`liveness-sweep`, `variant-revoked`) are the system
  working, not reviewer labor — exclude them from "what hurts".

## Steps

1. **Collect.** Recent abandons with reasons (7-14d window):

   ```sql
   SELECT s.id, substr(s.created_at,1,10), j.source, j.company, j.title,
          j.location, s.liveness, s.notes
   FROM application_snapshots s JOIN jobs j ON j.id=s.job_id
   WHERE s.status='abandoned' ORDER BY s.id DESC LIMIT 40
   ```

   Plus `abandon_tally(conn, days=7)` for the bucket view. Also check
   stalled states: student titles on the wall
   (`title_excluded` over current drafts), needs_recheck backlog,
   stage1 對帳行的不可投 share.

2. **Classify each suspicious sample** into exactly one of:
   - **live hole** — reproduce the misjudgment with CURRENT code
     (`classify_rules`, `is_germany_location`, `title_excluded`,
     `redirect_off_posting` …). Only a current-code reproduction counts;
     a mislabel whose cause was already fixed is residue, not a hole.
   - **old-stock residue** — cause already fixed, draft predates the fix;
     no action beyond letting the reviewer consume it.
   - **unfixable floor** — see ground rules.
   - **LLM noise** — single-sample LLM verdicts (e.g. one bad
     `Remote — EU`); monitor, never patch rules for n=1.

3. **Size every live hole** before fixing: query how many current
   queue-eligible rows share the failure fingerprint. A hole worth fixing
   has population > a handful, or sits directly on the queue path.

4. **Report the prioritized list** (cause / samples / confidence / status)
   — this is the deliverable even if nothing is fixable.

5. **Fix** (only live holes, on a `fix/...` branch):
   - regression test first, reproducing the real sample (use the actual
     JD phrasing, fictionalized company if needed)
   - prefer demoting a wrong automatic verdict to `unclear`/human over
     enumerating counter-patterns (the LLM gate = queue floor since
     `58b2975`, so `unclear` still gets classified)
   - full suite in container:
     `docker run --rm -v "$PWD":/app -w /app -v "$PWD"/config:/app/config:ro
     job-hunter-app:latest python3 -m unittest discover tests -q`

6. **Repair data** idempotently: reset mislabeled rows so the next daily
   run re-classifies them (chain order triage→scorer→stage1 guarantees
   relabeling happens before queue build). Always exclude jobs with
   in-flight snapshots. Write via docker exec on the live container.

7. **Ship** per /ship conventions (merge `--no-ff`, push, rebuild+`up -d`
   — first verify no long docker exec task is running inside; a rebuild
   kills it silently and batch scripts commit once at the end).

8. **Verify next morning** (07:00 anchor run): the repaired rows got
   re-classified, and the fat bucket deflates over the following days.
