"""
benchmark_threshold.py — Spot-check Qdrant KB similarity threshold.

Usage:
    python benchmark_threshold.py [--n 8] [--thresholds 0.50,0.60,0.70]

Picks N jobs from the DB (mix of A/B/C grades if available), embeds each JD,
queries Qdrant at each threshold, and prints a comparison table.
"""

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, str(Path(__file__).parent))

import sqlite3
from utils.llm import make_client, emb_model, rate_limit

DB_PATH     = os.getenv("DB_PATH", "./data/jobs.db")
QDRANT_PATH = os.getenv("QDRANT_PATH", "./qdrant_data")
COLLECTION  = "candidate_kb"


def get_sample_jobs(db_path: str, n: int) -> list[dict]:
    """Fetch N jobs with JD text, balanced across fit grades if possible."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    jobs: list[dict] = []
    # Try to get a mix: 2-3 from each grade
    for grade in ("A", "B", "C", "D"):
        cur.execute(
            """
            SELECT id, title, company, fit_grade, raw_jd_text, translated_jd_text
            FROM jobs
            WHERE (raw_jd_text IS NOT NULL AND length(raw_jd_text) > 200)
              AND fit_grade = ?
            ORDER BY RANDOM()
            LIMIT ?
            """,
            (grade, max(1, n // 4)),
        )
        rows = cur.fetchall()
        jobs.extend(dict(r) for r in rows)
        if len(jobs) >= n:
            break

    # Fill remainder with any scored jobs if not enough
    if len(jobs) < n:
        ids = [j["id"] for j in jobs]
        placeholders = ",".join("?" * len(ids)) if ids else "''"
        cur.execute(
            f"""
            SELECT id, title, company, fit_grade, raw_jd_text, translated_jd_text
            FROM jobs
            WHERE (raw_jd_text IS NOT NULL AND length(raw_jd_text) > 200)
              AND id NOT IN ({placeholders})
            ORDER BY RANDOM()
            LIMIT ?
            """,
            (*ids, n - len(jobs)),
        )
        jobs.extend(dict(r) for r in cur.fetchall())

    conn.close()
    return jobs[:n]


def embed_one(client, text: str, retries: int = 3) -> list[float]:
    """Embed a single text with retry on transient errors."""
    import time
    for attempt in range(retries):
        rate_limit()
        try:
            resp = client.embeddings.create(model=emb_model(), input=[text[:1500]])
            return resp.data[0].embedding
        except Exception as exc:
            if attempt < retries - 1:
                wait = 10 * (attempt + 1)
                print(f"    embed failed ({exc}), retrying in {wait}s…")
                time.sleep(wait)
            else:
                raise


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed texts one at a time to stay within Mistral API limits."""
    client = make_client()
    vectors = []
    for i, text in enumerate(texts):
        vectors.append(embed_one(client, text))
        if (i + 1) % 4 == 0:
            print(f"    embedded {i+1}/{len(texts)}…")
    return vectors


def query_at_threshold(qdrant, vector: list[float], threshold: float, top_k: int = 5) -> list[dict]:
    result = qdrant.query_points(collection_name=COLLECTION, query=vector, limit=top_k)
    return [
        {"score": round(h.score, 4), "section": h.payload.get("section", ""), "text": h.payload.get("text", "")[:80]}
        for h in result.points
        if (h.score or 0) >= threshold
    ]


def run_benchmark(n: int, thresholds: list[float], runs: int = 1) -> None:
    from qdrant_client import QdrantClient

    qdrant = QdrantClient(path=QDRANT_PATH)
    existing = {c.name for c in qdrant.get_collections().collections}
    if COLLECTION not in existing:
        print(f"ERROR: Qdrant collection '{COLLECTION}' not found. Run: python -m utils.kb_loader")
        sys.exit(1)

    info = qdrant.get_collection(COLLECTION)
    total_chunks = info.points_count or 0
    sep = "─" * 100
    print(f"\nKB: {total_chunks} chunks in '{COLLECTION}'")
    print(f"Embedding model: {emb_model()}")
    print(f"Thresholds to test: {thresholds}")
    print(f"Sample jobs: {n} × {runs} run(s) = {n * runs} total\n")

    all_raw_scores: list[float] = []
    # hits_per_thr[thr] = list of hit counts (one per job per run)
    hits_per_thr: dict[float, list[int]] = {t: [] for t in thresholds}

    for run_idx in range(runs):
        if run_idx > 0:
            import time
            print(f"\n  (pausing 15s between runs to avoid rate limit…)")
            time.sleep(15)
        print(f"{sep}")
        print(f"Run {run_idx + 1}/{runs}")

        jobs = get_sample_jobs(DB_PATH, n)
        if not jobs:
            print("ERROR: No jobs with JD text found in DB.")
            sys.exit(1)

        texts = [(j.get("translated_jd_text") or j["raw_jd_text"])[:3000] for j in jobs]
        print(f"Embedding {len(jobs)} JDs…")
        vectors = embed_texts(texts)

        for job, vec in zip(jobs, vectors):
            label = f"  [{job['fit_grade'] or '?'}] {job['title'][:45]:45s} @ {job['company'][:20]}"
            print(f"\n{label}")

            raw_result = qdrant.query_points(collection_name=COLLECTION, query=vec, limit=10)
            raw_scores = [round(h.score, 4) for h in raw_result.points]
            all_raw_scores.extend(raw_scores)
            print(f"    scores (top-10): {raw_scores}")

            for thr in thresholds:
                hits = query_at_threshold(qdrant, vec, thr)
                hits_per_thr[thr].append(len(hits))
                chunks_preview = " | ".join(f"{h['score']} «{h['section']}»" for h in hits) or "(none)"
                print(f"    thr={thr:.2f} → {len(hits):2d} hit(s): {chunks_preview[:80]}")

    # ── Aggregate summary ─────────────────────────────────────────────────────
    total_jobs = n * runs
    print(f"\n{sep}")
    print(f"AGGREGATE SUMMARY  ({total_jobs} job queries across {runs} run(s))")
    print(f"{sep}")

    # Score distribution
    all_raw_scores.sort(reverse=True)
    print(f"\nScore distribution (top-10 per job, {len(all_raw_scores)} total scores):")
    print(f"  Max:    {max(all_raw_scores):.4f}")
    print(f"  Mean:   {sum(all_raw_scores)/len(all_raw_scores):.4f}")
    print(f"  Median: {sorted(all_raw_scores)[len(all_raw_scores)//2]:.4f}")
    print(f"  Min:    {min(all_raw_scores):.4f}")

    brackets = {"≥0.70": 0, "0.60–0.69": 0, "0.50–0.59": 0, "<0.50": 0}
    for s in all_raw_scores:
        if s >= 0.70:
            brackets["≥0.70"] += 1
        elif s >= 0.60:
            brackets["0.60–0.69"] += 1
        elif s >= 0.50:
            brackets["0.50–0.59"] += 1
        else:
            brackets["<0.50"] += 1

    total_scores = len(all_raw_scores)
    print("  Bracket counts (% of all top-10 hits):")
    for k, v in brackets.items():
        pct = v / total_scores * 100
        bar = "█" * v
        print(f"    {k:12s}: {v:4d}  ({pct:5.1f}%)  {bar}")

    # Average hits per threshold
    print(f"\nAverage chunks returned per job at each threshold (out of top_k=5):")
    for thr in thresholds:
        counts = hits_per_thr[thr]
        avg = sum(counts) / len(counts)
        mn, mx = min(counts), max(counts)
        print(f"  thr={thr:.2f} → avg {avg:.2f} hits/job  (min={mn}, max={mx})")

    print(f"\n{sep}")
    print("Done. Use the distribution above to confirm or revise _KB_SCORE_THRESHOLD in phase2_scorer.py:303")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Spot-check Qdrant similarity threshold")
    parser.add_argument("--n", type=int, default=8, help="Number of jobs to sample (default: 8)")
    parser.add_argument(
        "--thresholds",
        type=str,
        default="0.50,0.60,0.70",
        help="Comma-separated thresholds to compare (default: 0.50,0.60,0.70)",
    )
    parser.add_argument("--runs", type=int, default=3, help="Number of independent random samples (default: 3)")
    args = parser.parse_args()
    thresholds = [float(t.strip()) for t in args.thresholds.split(",")]
    run_benchmark(args.n, thresholds, args.runs)
