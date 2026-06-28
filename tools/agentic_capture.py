#!/usr/bin/env python3
"""agentic_capture.py — snapshot a real apply form for offline agentic probing.

Stage 1's browser layer (headless) is the most faithful source of the field
node table the pipeline actually sees, so this reuses it verbatim: navigate to
a job's apply form, extract the field table, and persist BOTH the table (the
input the agentic mapper consumes) and the raw/pruned HTML (for debugging) to a
gitignored dir. No LLM, no submit — pure capture.

Runs inside the pipeline container (needs Playwright/Chromium):

    docker exec job-hunter-pipeline-1 python tools/agentic_capture.py \
        --job-id <id> [--out-dir data/agentic] [--name rosen]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utils.apply_queue import DEFAULT_DB_PATH  # noqa: E402
from utils.db import init_db  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description="Capture an apply form for agentic probing.")
    ap.add_argument("--job-id", required=True)
    ap.add_argument("--db", default=DEFAULT_DB_PATH)
    ap.add_argument("--out-dir", default="data/agentic")
    ap.add_argument("--name", default=None, help="basename for the output files")
    args = ap.parse_args()

    conn = init_db(args.db)
    row = conn.execute(
        "SELECT id, company, title, source, "
        "       COALESCE(apply_url, url) AS target, url "
        "FROM jobs WHERE id = ?",
        (args.job_id,),
    ).fetchone()
    conn.close()
    if row is None:
        sys.exit(f"job {args.job_id} not found")
    job = dict(row)
    name = args.name or job["id"]
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    from utils.browser import extract_form_tree, goto_apply_page, headless_session

    target = job["target"]
    print(f"capturing {job['company']} — {job['title']}\n  target: {target}", flush=True)

    with headless_session() as context:
        page = context.new_page()
        report = goto_apply_page(page, target, title=job.get("title"))
        active = report.pop("page", page)
        tree = extract_form_tree(active) if report["form_found"] else None
        raw_html = active.content()
        final_url = active.url

    fields = [f.to_dict() for f in tree["fields"]] if tree else []
    payload = {
        "job": job,
        "final_url": final_url,
        "form_found": report["form_found"],
        "captcha": report["captcha"],
        "controls": report["controls"],
        "cookie_clicked": report.get("cookie_clicked"),
        "clicked_apply": report.get("clicked_apply"),
        "n_fields": len(fields),
        "fields": fields,
    }
    (out_dir / f"{name}_capture.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1))
    (out_dir / f"{name}.html").write_text(raw_html)
    if tree:
        (out_dir / f"{name}_pruned.html").write_text(
            "\n\n<!-- ===== frame ===== -->\n\n".join(tree["pruned"].values()))

    print(f"\n  final_url : {final_url}")
    print(f"  form_found: {report['form_found']}  captcha: {report['captcha']}")
    print(f"  fields    : {len(fields)}")
    print(f"  written   : {out_dir}/{name}_capture.json (+ .html)")


if __name__ == "__main__":
    main()
