"""The interview brief reads the whole KB, not a ranked slice (phase2_scorer).

A relevance floor that protects batch scoring from noise was silently dropping
the chunks the brief needed most — on a real JD the two evaluation chunks
scored 0.266 and 0.313 against a 0.35 floor. This pins the contract: every
stored chunk reaches the brief, in a stable order, whatever its similarity.
Fictional Max Mustermann data only.
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from phase2_scorer import COLLECTION, kb_context_all  # noqa: E402

CHUNKS = [
    (1, "projects.md", 0, "[Projects: Eval] Ragas faithfulness 0.54 -> 0.796."),
    (2, "projects.md", 1, "[Projects: Privacy] Query text replaced by a fingerprint."),
    (3, "resume_bullets.md", 0, "[Resume Bullets: Axiom] Shipped the MVP in 4 weeks."),
]


class KbContextAllTest(unittest.TestCase):
    def setUp(self):
        from qdrant_client import QdrantClient
        from qdrant_client.models import Distance, PointStruct, VectorParams
        self.tmp = tempfile.TemporaryDirectory()
        self.path = self.tmp.name
        client = QdrantClient(path=self.path)
        client.create_collection(
            collection_name=COLLECTION,
            vectors_config=VectorParams(size=3, distance=Distance.COSINE))
        # deliberately reversed insert order, and vector 1 is orthogonal to the
        # others: neither insertion order nor similarity may decide what is read
        client.upsert(collection_name=COLLECTION, points=[
            PointStruct(id=pid, vector=[0.0, 0.0, 1.0] if pid == 1 else [1.0, 0.0, 0.0],
                        payload={"source": src, "chunk_index": idx, "text": text})
            for pid, src, idx, text in reversed(CHUNKS)])
        client.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_every_chunk_reaches_the_brief_in_a_stable_order(self):
        ctx = kb_context_all(self.path)
        for _, src, _, text in CHUNKS:
            self.assertIn(text, ctx)
            self.assertIn(f"[來源: {src}]", ctx)
        self.assertLess(ctx.index("Ragas"), ctx.index("fingerprint"))
        self.assertLess(ctx.index("fingerprint"), ctx.index("4 weeks"))


if __name__ == "__main__":
    unittest.main()
