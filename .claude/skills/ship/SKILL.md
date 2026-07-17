---
name: ship
description: Test, commit, push, and deploy the current changes — the repo's full implement-to-deploy loop (Python/unittest/Docker, not npm).
---

# Ship

Run the full delivery loop for the job-hunter repo. Stop at the first failing
step and report it — never commit failing code, never deploy an unbuilt image.

## Steps

1. **Test.** Run the full suite inside the container image with the repo
   mounted (host Python lacks langgraph/httpx/streamlit):

   ```bash
   docker run --rm -v "$PWD":/app -w /app -v "$PWD"/config:/app/config:ro \
     job-hunter-app:latest python3 -m unittest discover tests -q
   ```

   All tests must pass. There is no separate CI — this container run is the CI.

2. **Commit.** English conventional message (`feat(scope): ...`, `fix(scope): ...`)
   describing why, not just what. Never `git add` gitignored config
   (`config/search_targets.yaml`, `config/grading_rules.md`,
   `candidate_kb/*.yaml` — real personal data lives only outside git).
   End the message with:

   ```
   Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
   ```

   Multi-commit features go on a `feat/...` or `fix/...` branch merged
   `--no-ff` into main; a single small commit may land on main directly.

3. **Push.** `git push` and confirm the ref moved.

4. **Deploy.** Code is baked into the image — a push alone deploys nothing:

   ```bash
   docker compose build && docker compose up -d
   ```

   Verify all three containers (`pipeline`, `dashboard`, `apply_api`) show
   fresh `Up` status, and spot-check the new code is really inside:
   `docker exec job-hunter-pipeline-1 grep <new-string> /app/<changed-file>`.

   ⚠ Before rebuilding, check no long `docker exec` task is running inside
   (`ls /proc/[0-9]*/cmdline` via docker exec — the container has no pgrep);
   a rebuild kills such tasks silently.

5. **Report.** State: tests passed (count), commit hash + message, push result,
   container status. If anything was skipped or failed, say so plainly.
