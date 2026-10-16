---
name: career-log
description: Append a dated win to candidate_kb/career_log.md or a STAR story to candidate_kb/stories.md, then rebuild the KB. Use when the user says "log this", "記一下", "加進 career log", or wants to write/practise an interview story.
---

# Career log

Two files feed the KB (both gitignored — real personal data, never `git add`):

- `candidate_kb/career_log.md` — dated one-liners, written the day it happens.
  Format: `candidate_kb/career_log.md.example`.
- `candidate_kb/stories.md` — STAR stories for behavioural questions; the
  interview brief maps likely questions to their titles.
  Format: `candidate_kb/stories.md.example`.

If a file does not exist yet, create it from its `.example` minus the
placeholder blocks.

## Log a win

1. Get the facts from the user, not from your imagination: what was done, the
   concrete outcome, a number if one exists. If the user gave only a vague
   line ("fixed a bug"), ask ONE question for the outcome. Never invent a
   metric — an entry without a number is fine.
2. Append `- YYYY-MM-DD: <what> — <outcome>` under the `## YYYY-MM | <project>`
   block for this month and project; create the block (newest first) if
   missing. No blank line inside a block: kb_loader splits on blank lines.
3. Write the entry in English — it is cover-letter material.
4. Rebuild the KB (step below).

## Write a story

Interview the user one question at a time: situation, their own task, what
they did (including dead ends), the result. Push for specifics they actually
remember; leave out anything they cannot back. Then write the `##` block with
a `Question types:` line, show it, and save after they confirm. Remind them to
practise it out loud — a typed story is not a rehearsed one.

## Rebuild the KB

```bash
docker exec job-hunter-pipeline-1 python -m utils.kb_loader
```

Before running, check that no scorer run holds the Qdrant lock (`ls
/proc/[0-9]*/cmdline` via docker exec, look for phase2_scorer / apply_stage1);
if one does, tell the user and rebuild after it finishes. Report the chunk
count from the log.
