# Answer Panel — on-demand grounded answers, copy-paste interface

Status: BUILT (2026-07-02) — the one sanctioned exception to the feature
freeze (the user confirmed open questions are met often). Verified
end-to-end against the live profile: focus-grounded answer, mismatch
warning, custom_qa trail all behaved as designed. Standing decisions:
answers in English (2026-06-10 rule), no trail for hand-found jobs (v1),
150-word cap.

## Problem

Open/job-specific questions ("Why us?", "Describe a project…") are the most
expensive manual step of an application — minutes each, on every form. The
retired Stage-1 answerer pre-generated them per form, but 0 of 74 historical
snapshots' submissions ever used one: it answered questions the human never
met. This flips the trigger: answer the question the human is looking at,
when they meet it.

## Shape: LLM as writer, human as actuator

```
 dashboard (Apply Review) ── "🎯 applying to this one" ──► app_state focus
        │                                                       │
        │ human opens the apply link                            │
        ▼                                                       ▼
 application form (any page, any widget, any captcha)     apply_api sidecar
        │  human copies the question                            │
        ▼                                                       │
 extension panel ──POST /answer {question, job_id?}────────────►│
        ▲                                                       │ resolve job
        │  answer + grounding label + [Copy]                    │ (focus first)
        │  human READS, then copies,                            │ + grounding
        │  then pastes into the form                            │ 1 Mistral call
        └───────────────────────────────────────────────────────┘ + custom_qa
```

Copy-paste is the whole trick. Every hard problem of form automation —
extraction, custom widgets, isTrusted, cross-origin iframes, captchas —
disappears because the extension never touches the form. The human physically
must read the answer to paste it, so review is enforced by construction, not
by policy.

## API: `POST /answer` (apply_api, token-gated)

Request:
```json
{"question": "<pasted text, truncated server-side to ~1000 chars>",
 "job_id": "<optional — reserved for a later panel-side override; v1 panels
             send question only and let the server resolve>",
 "page_host": "<optional — the panel's location.host, used only for the
                unambiguous-host fallback and the mismatch warning>"}
```

Response:
```json
{"answer": "<text>",
 "grounding": {"kind": "job+profile" | "profile-only",
               "job_id": "…", "company": "…", "title": "…",
               "via": "focus" | "host" | null},
 "warnings": ["focus is Mistral AI but this page is greenhouse.io — check", ...],
 "notes": ["cover letter missing for this job", ...]}
```

### Job resolution — dashboard focus first, never host-guessing

Host matching is NOT a reliable job identifier for answering: two drafts on
the same ATS host collide (the live cohort has Mistral ×3, all on
jobs.lever.co — hostMatch returns an arbitrary one), and redirect/iframe
disguises break it entirely (the Workato form lives on job-boards.
greenhouse.io). A silently WRONG job context grounds the answer in the wrong
JD — worse than no context. So the human's explicit intent is the primary
signal:

- **Dashboard focus**: each draft card in Apply Review gets a
  "🎯 我要投這筆" button → `set_focus(conn, snapshot_id)` writes one row to
  a tiny `app_state` K/V table (dashboard already writes the DB directly).
  Clicking it is the natural first step of applying anyway.
- Resolution order in `/answer`:
  1. explicit `job_id` in the request (panel override, later)
  2. dashboard focus, if fresh (TTL 2 h) — cleared/superseded by the next
     focus click and by `mark_submitted`
  3. `MATCH` host (fallback, only when exactly ONE pending draft matches
     the host — ambiguity means no match, never a guess)
  4. none → profile-only grounding
- The panel always shows WHICH job grounded the answer
  ("grounded: Mistral AI · Applied AI Lead · via dashboard focus") — the
  human-visible label is the last-line defence against a stale focus.

Server flow:
1. Resolve the job (above) → load `title, company, COALESCE(translated_jd_text,
   raw_jd_text) AS description, cover_letter_draft` from jobs.
2. Assemble grounding context:
   - candidate profile facts (`apply_llm.build_profile_facts` — exists)
   - the job's `cover_letter_draft` — already tailored, RAG-derived at
     scoring time; the highest-signal background for THIS job
   - JD excerpt (first ~4000 chars — token budget discipline)
3. One `_chat_json` call with the fabrication-guarded answer prompt
   (resurrect `_ANSWER_SYSTEM` from git, deleted in 6d4c205: "never add a
   metric/number/tool the background does not state", ~150-word cap).
4. If the job has a draft/submitted snapshot → append `{question, answer,
   source: "on-demand"}` to its `custom_qa` via `snapshot_io.edit_snapshot`
   — the evidence trail for interview prep, restored at the right moment.
5. No `job_id` → profile-facts-only grounding, `grounding: "profile-only"`
   so the panel shows a visible "no job context" banner.

### Focus: schema & lifecycle

```sql
CREATE TABLE IF NOT EXISTS app_state (      -- generic K/V, one row per key
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,               -- JSON
    updated_at TEXT NOT NULL
);
-- key='apply_focus', value={"snapshot_id": 74, "job_id": "…"}
```

- `set_focus(conn, snapshot_id)` — upsert; called by the dashboard button.
- `get_focus(conn, max_age_h=2)` — returns None when missing or stale;
  **staleness is enforced read-side** (no cleaner daemon). Rationale for
  2 h: same window as the extension's applying-slot; long enough for one
  sitting, short enough that tomorrow's session can't inherit it.
- `clear_focus(conn)` — called inside `mark_submitted` when the submitted
  snapshot IS the focused one (submitting job A must not clear a focus the
  user already moved to job B).
- Single-row by design: the human applies to one job at a time; parallel
  applying stays on the host-match fallback + warning label.

### Dashboard hook (pages/1_Apply_Review.py)

- Each draft card gets one button: **「🎯 我要投這筆」** → `set_focus` +
  `st.toast` confirmation. It sits next to the apply-URL link, so clicking
  it is part of the natural "open the form" motion, not a new habit.
- The page header shows the current focus ("🎯 investing: Mistral AI ·
  Applied AI Lead · 12 min ago") so a stale focus is visible before it
  misgrounds anything.
- No other dashboard changes; the cheat-sheet card stays as is.

### Prompt assembly (server-side, one call)

```
system: _ANSWER_SYSTEM (resurrected from git 6d4c205^)
        — ground every claim in the background; NEVER add a metric/number/
          tool the background does not state; ≤150 words; JSON out.

user:   Candidate facts:            build_profile_facts(profile)   (~300 tok)
        Tailored cover letter:      jobs.cover_letter_draft        (~250 tok)
        Job: {title} at {company}
        Job description (excerpt):  description[:4000]             (~1000 tok)
        Question:                   _sanitize(question)[:1000]     (~250 tok)
```

- Budget ≈ 2k tokens in, 400 out — one cheap Mistral call, latency 2–8 s.
- The pasted question is DATA: sanitized, placed last, never allowed to
  restate instructions (system prompt stays authoritative).
- profile-only mode simply omits the CL + JD blocks and keeps the same
  fabrication rules — fewer facts available means shorter answers, never
  invented ones.

## Failure & edge matrix

| Case | Behaviour |
|---|---|
| LLM error / unparseable ×2 | panel shows the error string; no fallback text |
| focus stale (>2 h) | ignored → host fallback → profile-only + banner |
| focus set, but page host ≠ focused job's apply host | answer still grounds on focus, plus a `warnings` entry the panel renders in orange |
| ≥2 pending drafts match the page host, no focus | no guess: profile-only + "set focus in the dashboard" hint |
| resolved job has empty JD and empty CL | grounding degrades to profile-only with a note (never half-grounded silently) |
| question > 1000 chars | truncated server-side, `notes` says so |
| snapshot already submitted when the trail write lands | trail appends anyway (edit_snapshot on a terminal row is a data update, not a transition) |
| clipboard write rejected | panel falls back to selecting the answer text for manual ⌘C |

## Walkthrough (the case that motivated the design)

Mistral has 3 pending drafts, all on jobs.lever.co. The user opens the
dashboard, clicks 🎯 on **Applied AI Lead**, opens its apply link, and hits
"Why Mistral?" on the lever form. They paste it into the panel → server
resolves via focus (host match would have been ambiguous — 3 candidates) →
grounds on THAT role's JD + its tailored CL → answer comes back labelled
"Mistral AI · Applied AI Lead · via focus". The user reads it, copies,
pastes, submits, clicks "I submitted it" → `mark_submitted` books the
snapshot, abandons the two sibling drafts, and clears the focus. The Q&A
pair is now in that snapshot's `custom_qa` for interview prep.

### Why no qdrant RAG in v1

The apply_api container does not mount `qdrant_data`, and qdrant local mode
takes a file lock — a second process opening it while phase2 scores would
collide. The tailored cover letter is itself the distilled output of that
RAG pass for this exact job, so v1 rides on it. Upgrade path if answers feel
thin: mount the volume read-only and degrade gracefully on lock failure
(same contract as the old `_retrieve_kb`).

## Extension panel

New section under the existing buttons:

```
[ paste the form's question here…          ]   ← <textarea>
[ Answer from my background ]                  ← disabled while pending
┌─────────────────────────────────────────┐
│ <answer text>                           │
│ grounded: Workato · Staff AI Engineer   │   ← or ⚠ no job context
└─────────────────────────────────────────┘
[ Copy answer ]
```

- The panel does not resolve the job itself — it sends `{question,
  page_host}` and the server resolves (focus > unambiguous host match >
  none). The grounding label is rendered prominently; any `warnings`
  (focus/page mismatch) render in orange above the answer.
- background.js: one new message type `answer` → `POST /answer`.
- Copy: `navigator.clipboard.writeText` on the button's click (user
  gesture); add `clipboardWrite` permission for safety.
- The answer is NEVER filled into the page. No selector, no setter. If the
  user wants it in the form, they paste it — that's the review gate.

## Fact short-circuit (2026-07-03, field-test fix)

The first real salary question came back as LLM prose padded with visa
trivia. Root cause: fact questions must never reach the LLM. A question
≤120 chars that `match_field`s a profile fact returns the fact
deterministically (`grounding.kind: "profile-fact"`, no LLM call, trail
source `profile-fact`); longer questions that merely mention a fact term
("describe your salary negotiation…") stay on the LLM path.

Salary is special-cased: the per-job salary_estimator report carries a
"Gehaltsvorstellung — Application Form" section with a suggested figure —
`/answer` parses it (en/zh anchors, 185/195 existing reports parse) and
answers `max(figure, profile floor)` so high-paying jobs are not undersold
and the floor is never broken. The basis (figure vs floor) is shown in the
panel notes. No estimate → profile value + a "generate one in the
dashboard" hint. NOTE: estimates are generated per-job in the dashboard —
run it for a cohort job before applying if you want the tailored figure.

## Guardrails

- fabrication prompt is the tested one (momox/verifier lineage), ~150-word
  cap, English answers (2026-06-10 decision — reconfirm, see open items).
- Question text is defanged (`_sanitize` lineage) before entering the
  prompt; it comes from an arbitrary web page (prompt-injection surface:
  treat the pasted text as data, keep the system prompt authoritative).
- No verifier second-pass in v1: the human reads every answer by
  construction. Add `verify_draft`'s fabrication gate later only if pasted
  answers turn out to be trusted blindly.
- LLM/API failure → error string in the panel, nothing else; never a
  fabricated fallback.
- Loopback + bearer token, same as every other route.

## Out of scope (stays frozen)

- Auto-filling the generated answer into the form (fill risk returns).
- Answer bank / dedup of repeated questions (build only if the same
  question keeps recurring — the custom_qa trail will show it).
- Panel-side job picker (dropdown of pending drafts) — only if the focus
  button turns out to be forgotten in practice.
- Multi-question batch answering.

## Effort & test plan

- `app_state` K/V table + `set_focus`/`get_focus` in utils/db: ~30 lines.
- dashboard focus button on draft cards: ~15 lines.
- apply_api `/answer` + resolution order + custom_qa trail: ~90 lines +
  tests (FakeClient, no network; focus precedence and the ambiguous-host
  no-guess rule both asserted).
- extension panel section + bg message: ~60 lines; manual test.
- Real-world acceptance: the open questions collected while submitting the
  7-draft cohort — if fewer than ~3 real questions were met, revisit
  whether this ships at all.

## Open decisions (user)

1. **Answer language**: old rule was English-always. German forms may read
   better with German answers — flag per request? auto-detect?
2. **Trail on hand-found jobs**: no snapshot to attach custom_qa to —
   acceptable to lose the trail, or create a minimal snapshot row?
3. Cap at 150 words per answer still right?
