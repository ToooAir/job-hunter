"""Routing/wiring tests for the Stage 1 per-job graph (utils/apply_graph.py).

These pin the graph topology, not node behaviour: recorder nodes are
injected via build_graph(overrides=...) so the tests stay green while the
real node bodies land in sub-tasks 4.1-4.4.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    import langgraph  # noqa: F401
    HAS_LANGGRAPH = True
except ImportError:
    HAS_LANGGRAPH = False

if HAS_LANGGRAPH:
    from utils.apply_graph import NODE_ORDER, ApplyState, build_graph


FIELDS_SAMPLE = [
    {"selector": '[name="first_name"]', "kind": "text", "label": "Vorname *"},
    {"selector": '[name="cv"]', "kind": "file", "label": "Lebenslauf"},
]


def _recorders(seen: list):
    """One recorder per node; save_draft keeps its (state, config) arity."""

    def rec(name):
        def node(state: ApplyState) -> dict:
            seen.append(name)
            return {}
        return node

    def rec_save(state: ApplyState, config) -> dict:
        seen.append("save_draft")
        return {}

    impl = {name: rec(name) for name in NODE_ORDER if name != "save_draft"}
    impl["save_draft"] = rec_save
    return impl


@unittest.skipUnless(HAS_LANGGRAPH, "langgraph not installed (container-only dep)")
class TestGraphWiring(unittest.TestCase):
    def test_graph_compiles_with_default_nodes(self):
        app = build_graph()
        self.assertIsNotNone(app)

    def test_fields_present_visits_full_chain_in_order(self):
        seen = []
        app = build_graph(overrides=_recorders(seen))
        app.invoke({"job": {"id": "j1"}, "verdict": "ok", "fields": FIELDS_SAMPLE})
        self.assertEqual(seen, list(NODE_ORDER))

    def test_no_fields_skips_mapping_chain(self):
        seen = []
        app = build_graph(overrides=_recorders(seen))
        app.invoke({"job": {"id": "j2"}, "verdict": "external-board", "fields": []})
        self.assertEqual(seen, ["assign_tier", "save_draft"])

    def test_captcha_with_fields_still_gets_full_payload_chain(self):
        seen = []
        app = build_graph(overrides=_recorders(seen))
        app.invoke({"job": {"id": "j3"}, "verdict": "captcha", "fields": FIELDS_SAMPLE})
        self.assertEqual(seen, list(NODE_ORDER))

    def test_notes_accumulate_through_stub_nodes(self):
        app = build_graph()
        out = app.invoke({"job": {"id": "j4"}, "verdict": "ok", "fields": FIELDS_SAMPLE})
        self.assertEqual(len(out["notes"]), len(NODE_ORDER))
        self.assertTrue(all("stub" in n for n in out["notes"]))

    def test_unknown_override_rejected(self):
        with self.assertRaises(ValueError):
            build_graph(overrides={"not_a_node": lambda s: {}})

    def test_state_keys_survive_invoke(self):
        app = build_graph()
        out = app.invoke({
            "job": {"id": "j5"}, "verdict": "ok", "fields": FIELDS_SAMPLE,
            "apply_url": "https://example.com/apply",
        })
        self.assertEqual(out["apply_url"], "https://example.com/apply")
        self.assertEqual(out["job"]["id"], "j5")


if __name__ == "__main__":
    unittest.main()
