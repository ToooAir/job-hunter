#!/usr/bin/env python3
"""Step A of worklist item 2: can the JD translation step be retired?

Read-only. For 40 German JDs that already have a stored translation:
  1. embed the German original and the English translation, query the KB with
     each, and compare the top-5 hit sets and top-1 scores (does the 0.35
     cosine floor still return context for a German query?)
  2. score each job twice with the production prompt — once on the German
     original, once on the translation — and compare grade, score and
     jd_language_req.

Pass criteria (worklist): median top-5 overlap >= 4/5, grade agreement >= 36/40,
and the German arm's jd_language_req must be no worse than the English arm's.

RESULT (2026-09-05, 40 jobs, gpt-5.6-luna, $0.10): all three criteria FAILED —
top-5 overlap median 3/5 (never above 3), grade agreement 34/40 and only 14/20
in the A/B band that decides what gets applied to, with 5 of 6 flips scoring the
German arm LOWER, and the German arm both hallucinating one de_required (JD has
zero language mentions) and missing one ("sicheres Deutsch in Wort und Schrift").
The translation step stays. Kept in-tree so the call can be re-run against a
future model instead of re-argued.

Run inside the container (read-only; qdrant is copied so the live stack keeps
its lock):
    docker exec job-hunter-pipeline-1 python3 scripts/experiment_translation_retirement.py
"""

import json
import os
import re
import shutil
import sqlite3
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import phase2_scorer as ps  # noqa: E402
from utils.llm import make_client  # noqa: E402

N = int(os.getenv("STEP_A_N", "40"))
# data/ is gitignored — results stay out of the repo
OUT = Path(os.getenv("STEP_A_OUT", str(ROOT / "data" / "translation_experiment.json")))
QDRANT_SRC = os.getenv("QDRANT_PATH", "./qdrant_data")
QDRANT_TMP = "/tmp/qdrant_stepa"


def _spread(rows: list[dict], k: int) -> list[dict]:
    """Deterministic evenly-spaced pick — reproducible, unlike ORDER BY random()."""
    if len(rows) <= k:
        return rows
    step = len(rows) / k
    return [rows[int(i * step)] for i in range(k)]


def pick_jobs(conn) -> list[dict]:
    """Stratified sample of the population the scorer actually LLM-scores:
    German JDs with a stored translation that the regex gate does NOT catch
    (rule-gated ones never reach the LLM, so they cannot inform this).

    Half A/B, half C. The pool is 86% C, and an unstratified draw would make
    "grades agree" trivially true — both arms say C — while telling us nothing
    about the band where a grade flip actually changes what gets applied to.
    """
    rows = conn.execute("""
        SELECT id, company, title, location, raw_jd_text, translated_jd_text,
               fit_grade, match_score, jd_language_req, source
        FROM jobs
        WHERE translated_jd_text IS NOT NULL
          AND length(raw_jd_text) > 800
          AND status IN ('scored', 'applied', 'skipped')
        ORDER BY id
    """).fetchall()
    cands = [dict(r) for r in rows
             if not ps.german_required(r["raw_jd_text"])
             and ps._detect_german(r["raw_jd_text"], job_id=r["id"])]
    ab = [c for c in cands if c["fit_grade"] in ("A", "B")]
    c_ = [c for c in cands if c["fit_grade"] == "C"]
    picked = _spread(ab, N // 2) + _spread(c_, N - N // 2)
    for p in picked:
        p["stratum"] = "A/B" if p["fit_grade"] in ("A", "B") else "C"
    return picked


_LANG_SPAN = re.compile(r"[^.\n]{0,90}(deutsch|german)[^.\n]{0,90}", re.I)


def lang_evidence(jd: str) -> list[str]:
    """The German original's own sentences about language — the adjudication
    material. 2026-09-04 lesson: judge against the JD, not against whichever
    model is incumbent."""
    return [" ".join(m.group(0).split())[:180] for m in _LANG_SPAN.finditer(jd or "")][:4]


def main() -> None:
    from qdrant_client import QdrantClient

    if not Path(QDRANT_TMP).exists():
        shutil.copytree(QDRANT_SRC, QDRANT_TMP)
    ps.check_kb_model(QDRANT_TMP)

    conn = sqlite3.connect(f"file:{os.getenv('DB_PATH', './data/jobs.db')}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    jobs = pick_jobs(conn)
    conn.close()
    print(f"sample: {len(jobs)} German JDs with a stored translation\n", flush=True)

    client = make_client()
    qdrant = QdrantClient(path=QDRANT_TMP)
    rules = (ROOT / "config" / "grading_rules.md").read_text(encoding="utf-8")

    # ── 1. retrieval comparison ────────────────────────────────────────────
    de_texts = [j["raw_jd_text"][:3000] for j in jobs]
    en_texts = [j["translated_jd_text"][:3000] for j in jobs]
    de_vecs = ps._batch_embed(de_texts, client)
    en_vecs = ps._batch_embed(en_texts, client)

    def hits(vec):
        r = qdrant.query_points(collection_name=ps.COLLECTION, query=vec, limit=5)
        return [(h.payload.get("source", "?"), round(h.score, 4)) for h in r.points]

    results = []
    for j, dv, ev in zip(jobs, de_vecs, en_vecs):
        dh, eh = hits(dv), hits(ev)
        d_ids = [h[0] for h in dh]
        e_ids = [h[0] for h in eh]
        overlap = len(set(d_ids) & set(e_ids))
        results.append({
            "id": j["id"], "company": j["company"], "title": j["title"],
            "stratum": j["stratum"], "source": j["source"],
            "stored_grade": j["fit_grade"], "stored_lang": j["jd_language_req"],
            "lang_evidence": lang_evidence(j["raw_jd_text"]),
            "overlap": overlap,
            "de_top1": dh[0][1] if dh else None,
            "en_top1": eh[0][1] if eh else None,
            "de_above_floor": sum(1 for h in dh if h[1] >= ps._KB_SCORE_THRESHOLD),
            "en_above_floor": sum(1 for h in eh if h[1] >= ps._KB_SCORE_THRESHOLD),
            "de_hits": dh, "en_hits": eh,
        })

    # ── 2. scoring comparison ──────────────────────────────────────────────
    for i, (j, rec, dv, ev) in enumerate(zip(jobs, results, de_vecs, en_vecs), 1):
        for arm, text in (("de", j["raw_jd_text"]), ("en", j["translated_jd_text"])):
            ctx = ps._qdrant_query(qdrant, dv if arm == "de" else ev, top_k=5)
            sp, up = ps.build_prompt(
                jd_text=text[:6000], company=j["company"], title=j["title"],
                location=j["location"] or "", context=ctx, grading_rules=rules)
            try:
                r = ps._call_llm(client, sp, up)
                rec[f"{arm}_grade"] = r.fit_grade
                rec[f"{arm}_score"] = r.match_score
                rec[f"{arm}_lang"] = r.jd_language_req
                rec[f"{arm}_reasons"] = r.top_3_reasons
            except Exception as exc:
                rec[f"{arm}_error"] = str(exc)[:200]
        print(f"  [{i}/{len(jobs)}] {rec.get('de_grade')}/{rec.get('de_score')}"
              f" {rec.get('de_lang')}  vs  {rec.get('en_grade')}/{rec.get('en_score')}"
              f" {rec.get('en_lang')}   {j['title'][:44]}", flush=True)

    OUT.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")

    # ── report ─────────────────────────────────────────────────────────────
    ok = [r for r in results if "de_grade" in r and "en_grade" in r]
    ov = [r["overlap"] for r in results]
    print("\n" + "=" * 68)
    print(f"1) KB retrieval — top-5 overlap (German query vs English query)")
    print(f"   median {statistics.median(ov)}/5 | mean {statistics.mean(ov):.2f}"
          f" | distribution {{n: count}} "
          f"{ {k: ov.count(k) for k in sorted(set(ov))} }")
    de1 = [r["de_top1"] for r in results if r["de_top1"] is not None]
    en1 = [r["en_top1"] for r in results if r["en_top1"] is not None]
    print(f"   top-1 score: German median {statistics.median(de1):.3f} "
          f"(min {min(de1):.3f}) | English median {statistics.median(en1):.3f} "
          f"(min {min(en1):.3f}) | floor {ps._KB_SCORE_THRESHOLD}")
    empty_de = sum(1 for r in results if r["de_above_floor"] == 0)
    empty_en = sum(1 for r in results if r["en_above_floor"] == 0)
    print(f"   JDs whose context would be EMPTY at the floor: German {empty_de},"
          f" English {empty_en}")

    same_grade = sum(1 for r in ok if r["de_grade"] == r["en_grade"])
    print(f"\n2) Scoring — grade agreement {same_grade}/{len(ok)}")
    for st in ("A/B", "C"):
        sub = [r for r in ok if r["stratum"] == st]
        if sub:
            agree = sum(1 for r in sub if r["de_grade"] == r["en_grade"])
            print(f"   stratum {st:3s}: {agree}/{len(sub)}")
    diffs = [abs(r["de_score"] - r["en_score"]) for r in ok]
    print(f"   |score delta| median {statistics.median(diffs)} "
          f"mean {statistics.mean(diffs):.1f} max {max(diffs)}")
    print("   grade disagreements:")
    for r in ok:
        if r["de_grade"] != r["en_grade"]:
            print(f"     {r['de_grade']}{r['de_score']:>3} (de) vs "
                  f"{r['en_grade']}{r['en_score']:>3} (en)  {r['company'][:22]} | {r['title'][:40]}")

    same_lang = sum(1 for r in ok if r["de_lang"] == r["en_lang"])
    print(f"\n3) jd_language_req agreement {same_lang}/{len(ok)}")
    print("   disagreements (adjudicate against the German original):")
    for r in ok:
        if r["de_lang"] != r["en_lang"]:
            print(f"     de={r['de_lang']:12s} en={r['en_lang']:12s} "
                  f"stored={r['stored_lang']:12s} {r['company'][:22]} | {r['title'][:40]}")
            print(f"       id={r['id']}")
            for ev in r["lang_evidence"]:
                print(f"       JD: {ev}")
    errs = [r for r in results if "de_error" in r or "en_error" in r]
    if errs:
        print(f"\n!! {len(errs)} job(s) errored:")
        for r in errs:
            print("   ", r["id"], r.get("de_error"), r.get("en_error"))
    print(f"\nfull results → {OUT}")


if __name__ == "__main__":
    main()
