# Cowork Pilot — y-bucket (unknown-ATS) batch #1

2026-07-06. Instructions doc: `docs/cowork_sonnet5_apply.md` (paste as task
instructions). Working folder needs `answers.yaml` (from
`docs/cowork_answers.yaml.example`) + `cv.pdf` + optional `cover_letter.txt`.

**Two questions this pilot answers** (fill the tracking table as you go):

1. **Agent stability** — does Sonnet fill the right values into the right
   fields and stop at the review point, per the instructions?
2. **y-bucket yield** — of the forms the static scanner couldn't classify,
   how many are actually fillable (vs captcha / login walls / dead ends)?

Decision hint after the batch: mostly READY-FOR-REVIEW with zero wrong
fields → the supervised-agent path is viable for the y bucket; mostly
STOPPED on walls → the y bucket is dead weight, deprioritize it.

## Batch (dedup verified 2026-07-06 — no pipeline-company conflicts)

Five jobs already have a dashboard draft (review the draft first; its cover
letter and answers are your pre-approved content). The other three have no
draft — generate/review a cover letter via the dashboard 📄 button if the
form wants one.

| # | Company — Title | Score | job_id | Draft? | Note |
|---|---|---|---|---|---|
| 1 | Workato — Staff AI Engineer | 90 | c1ba2fd8 | ✓ | suspected Greenhouse behind iframe (`gh_jid`) |
| 2 | ING Deutschland — Senior AI Engineer (Frankfurt) | 88 | bfd20de5 | ✓ | js-page; big-corp ATS likely behind it |
| 3 | CompuSafe Data Systems — Python Entwickler AI & APIs | 88 | 9333ff1f | — | German form likely |
| 4 | Dorsch Service — AI & Data Engineer | 88 | 57bf0b89 | ✓ | via heise listing |
| 5 | GWQ ServicePlus — Full Stack Software Engineer | 88 | 3807dbba | ✓ | via heise listing |
| 6 | Riverty Group — Software Engineer RAG & Knowledge Graphs | 88 | df7e53a8 | ✓ | via get-in-it listing |
| 7 | Yatta Solutions — Software Engineer | 80 | 27471a56 | — | |
| 8 | CompuGroup Medical — Senior AI Software Engineer | 78 | 52be5bd7 | — | |

Get each job's URL from the dashboard (or `data/ats_scan.csv` by job_id).

Excluded on purpose: Hays / FERCHAU / Akkodis / ALTEN / Guldberg /
Getspecialfasteners (staffing agencies — low yield), Sharpist (draft already
in extension flow), MAIA / Yareto / Scopevisio (company previously rejected —
re-applying needs your explicit decision, not an agent's).

## Per-task message template

```
Job URL: <url>
Company: <company>
Job title: <title>
Working folder: <path>
Follow the instructions exactly. Fill, verify, stop before submit, report.
```

## After each task

1. Read the agent's report table; fix empty/flagged fields yourself.
2. Solve any CAPTCHA, click Submit yourself.
3. Mark the job applied in the dashboard (same discipline as extension flow —
   this is what prevents double applications).
4. Fill the tracking row below.

## Tracking

| # | Result (ready/stopped-why) | Fields ok/wrong/blank | CAPTCHA? | Minutes | Submitted? |
|---|---|---|---|---|---|
| 1 | | | | | |
| 2 | | | | | |
| 3 | | | | | |
| 4 | | | | | |
| 5 | | | | | |
| 6 | | | | | |
| 7 | | | | | |
| 8 | | | | | |
