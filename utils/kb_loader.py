"""
utils/kb_loader.py — Build and persist the candidate knowledge base in Qdrant.
"""

import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

# Ensure project root is on sys.path when running this file directly
sys.path.insert(0, str(Path(__file__).parent.parent))

log = logging.getLogger(__name__)


def build_kb(
    kb_dir: str = "./candidate_kb",
    qdrant_path: str = "./qdrant_data",
) -> None:
    """
    Chunk all *.md files in kb_dir, embed them with text-embedding-3-small,
    and store the vectors in a local Qdrant collection called 'candidate_kb'.
    Safe to call repeatedly — recreates the collection each time.
    """
    from qdrant_client import QdrantClient
    from qdrant_client.models import Distance, VectorParams, PointStruct
    from dotenv import load_dotenv
    load_dotenv()
    from utils.llm import make_client, emb_model

    openai_client = make_client()
    qdrant = QdrantClient(path=qdrant_path)

    # ── 1. Read and chunk all .md files ───────────────────────────────────────
    chunks: list[dict] = []
    for md_file in sorted(Path(kb_dir).glob("*.md")):
        text = md_file.read_text(encoding="utf-8")
        h1 = ""
        h2 = ""
        for i, segment in enumerate(text.split("\n\n")):
            segment = segment.strip()
            if not segment:
                continue
            # A block may start with a heading followed by content on the same block
            # (e.g. "## Company | Role\n- bullet1\n- bullet2").
            # Split off the heading line and treat the rest as content.
            lines = segment.splitlines()
            if lines[0].startswith("# ") and not lines[0].startswith("## "):
                h1 = lines[0].lstrip("# ").strip()
                h2 = ""
                content = "\n".join(lines[1:]).strip()
            elif lines[0].startswith("## "):
                h2 = lines[0].lstrip("# ").strip()
                content = "\n".join(lines[1:]).strip()
            else:
                content = segment
            if len(content) < 20:
                continue
            prefix = f"[{h1}: {h2}] " if h2 else (f"[{h1}] " if h1 else "")
            chunks.append(
                {
                    "text": prefix + content,
                    "metadata": {"source": md_file.name, "chunk_index": i, "section": h2 or h1},
                }
            )

    if not chunks:
        log.warning("build_kb: no chunks found in %s — knowledge base is empty", kb_dir)
        return

    log.info("build_kb: %d chunks from %s", len(chunks), kb_dir)

    # ── 2. Embed in batches of 50 ──────────────────────────────────────────────
    BATCH = 50
    vectors: list[list[float]] = []
    for start in range(0, len(chunks), BATCH):
        batch_texts = [c["text"] for c in chunks[start : start + BATCH]]
        resp = openai_client.embeddings.create(
            model=emb_model(),
            input=batch_texts,
        )
        vectors.extend([e.embedding for e in resp.data])
        log.info("  embedded %d/%d chunks", min(start + BATCH, len(chunks)), len(chunks))

    vector_size = len(vectors[0])
    log.info("build_kb: embedding dimension = %d", vector_size)

    # ── 3. (Re)create Qdrant collection ───────────────────────────────────────
    COLLECTION = "candidate_kb"
    existing = {c.name for c in qdrant.get_collections().collections}
    if COLLECTION in existing:
        qdrant.delete_collection(COLLECTION)

    qdrant.create_collection(
        collection_name=COLLECTION,
        vectors_config=VectorParams(size=vector_size, distance=Distance.COSINE),
    )

    # ── 4. Upsert points ───────────────────────────────────────────────────────
    points = [
        PointStruct(
            id=i,
            vector=vectors[i],
            payload={"text": chunks[i]["text"], **chunks[i]["metadata"]},
        )
        for i in range(len(chunks))
    ]
    qdrant.upsert(collection_name=COLLECTION, points=points)
    log.info("build_kb: upserted %d vectors into '%s'", len(points), COLLECTION)

    # Write build timestamp so Phase 2 can detect stale KB
    ts_file = Path(qdrant_path) / ".kb_built_at"
    ts_file.write_text(
        datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
        encoding="utf-8",
    )
    log.info("build_kb: wrote timestamp → %s", ts_file)


if __name__ == "__main__":
    import logging
    from dotenv import load_dotenv

    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    build_kb()
