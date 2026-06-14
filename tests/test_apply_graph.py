"""Tests for the Stage 1 per-job graph (utils/apply_graph.py).

Topology tests inject recorder nodes via build_graph(overrides=...);
the end-to-end test runs the real node bodies with a fake LLM client and
dry_run config (no DB, no network). Fictional Max Mustermann data only.
"""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.test_apply_llm import FakeClient  # noqa: E402
from utils.profile_loader import CandidateProfile  # noqa: E402

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

PROFILE = CandidateProfile({
    "meta": {"cv_path": "candidate_kb/cv/cv.pdf"},
    "fields": {
        "first_name": {"value": "Max", "aliases": ["vorname"]},
    },
})


def config_for(client):
    return {"configurable": {"profile": PROFILE, "dry_run": True,
                             "client": client, "model": "m"}}


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

    def test_unknown_override_rejected(self):
        with self.assertRaises(ValueError):
            build_graph(overrides={"not_a_node": lambda s: {}})


@unittest.skipUnless(HAS_LANGGRAPH, "langgraph not installed (container-only dep)")
class TestEndToEndDryRun(unittest.TestCase):
    """Real node bodies, fake LLM, dry_run — the full Pass B for one job."""

    JOB = {"id": "j9", "title": "Backend Engineer", "company": "Mustermann GmbH",
           "ats": "unknown", "dedup": "ok",
           "cover_letter_draft": "I build backends."}

    FIELDS = [
        {"selector": "#vn", "kind": "text", "label": "Vorname *", "required": True},
        {"selector": "#why", "kind": "textarea", "label": "Warum wir?"},
    ]

    def test_full_pipeline_produces_reviewed_tier2_draft(self):
        client = FakeClient([
            json.dumps({"fields": [{"index": 0, "decision": "open_question"}]}),
            json.dumps({"answers": [{"index": 0, "answer": "Because backends."}]}),
            json.dumps({"pass": True, "issues": []}),
        ])
        app = build_graph()
        out = app.invoke(
            {"job": self.JOB, "verdict": "ok", "fields": self.FIELDS,
             "apply_url": "https://example.com/apply"},
            config=config_for(client),
        )
        values = {a["selector"]: a["value"] for a in out["actions"]}
        self.assertEqual(values, {"#vn": "Max", "#why": "Because backends."})
        self.assertEqual(out["tier"], 2)  # CL + LLM answer → review floor
        self.assertEqual(out["cover_letter"], "I build backends.")
        self.assertEqual(out["custom_qa"][0]["answer"], "Because backends.")
        self.assertTrue(out["verifier_report"]["pass"])
        self.assertNotIn("snapshot_id", out)  # dry run wrote nothing
        self.assertEqual(out["apply_url"], "https://example.com/apply")

    def test_weak_form_fields_skip_mapping_chain(self):
        client = FakeClient([])  # junk field table must not reach the LLM
        app = build_graph()
        out = app.invoke(
            {"job": {**self.JOB, "id": "j11"}, "verdict": "weak-form",
             "fields": [{"selector": "#q", "kind": "text", "label": "Find a role"}]},
            config=config_for(client),
        )
        self.assertEqual(out["tier"], 3)
        self.assertEqual(client.calls, [])

    def test_no_fields_job_gets_tier3_without_llm_calls(self):
        client = FakeClient([])  # raises if any LLM call happens
        app = build_graph()
        out = app.invoke(
            {"job": {**self.JOB, "id": "j10"}, "verdict": "external-board",
             "fields": []},
            config=config_for(client),
        )
        self.assertEqual(out["tier"], 3)
        self.assertEqual(client.calls, [])


@unittest.skipUnless(HAS_LANGGRAPH, "langgraph not installed (container-only dep)")
class TestMapAgentic(unittest.TestCase):
    """Hybrid fallback: agentic fills only the gaps, never overrides the
    deterministic/LLM passes, and every adopted action is needs_review."""

    FIELDS = [
        {"selector": "#vn", "kind": "text", "label": "Vorname"},
        {"selector": "#q", "kind": "textarea", "label": "cards[x][f0]",
         "context_hint": "What languages do you speak?"},
    ]

    def _state(self):
        # deterministic already filled #vn; #q was left unfilled (opaque label)
        return {"job": {"id": "j", "title": "T", "company": "C"},
                "fields": self.FIELDS,
                "actions": [{"selector": "#vn", "action": "fill", "value": "Max",
                             "source": "profile:first_name", "needs_review": False}],
                "unfilled": [{"selector": "#q", "label": "cards[x][f0]",
                              "reason": "llm-needs-human", "required": False}],
                "custom_qa": []}

    def test_adopts_gap_only_and_keeps_deterministic(self):
        from utils.apply_graph import map_agentic
        # agentic answers both ids; #vn is already actioned so it must be dropped
        client = FakeClient([json.dumps({"fields": [
            {"id": 0, "action": "fill", "value": "WRONG — must not override"},
            {"id": 1, "action": "fill", "value": "German, English"},
        ]})])
        out = map_agentic(self._state(), config_for(client))
        by_sel = {a["selector"]: a for a in out["actions"]}
        self.assertEqual(by_sel["#vn"]["value"], "Max")            # deterministic kept
        self.assertEqual(by_sel["#q"]["value"], "German, English")  # gap filled
        self.assertEqual(by_sel["#q"]["source"], "agentic")
        self.assertTrue(by_sel["#q"]["needs_review"])               # → Tier 2 only
        self.assertFalse(any(u["selector"] == "#q" for u in out["unfilled"]))

    def test_kill_switch_skips_without_calling_llm(self):
        from utils.apply_graph import map_agentic
        client = FakeClient([])  # raises if called
        cfg = {"configurable": {"profile": PROFILE, "client": client, "model": "m",
                                "enable_agentic_fallback": False}}
        self.assertEqual(map_agentic(self._state(), cfg), {})
        self.assertEqual(client.calls, [])

    def test_no_gap_makes_no_llm_call(self):
        from utils.apply_graph import map_agentic
        client = FakeClient([])  # raises if called
        state = self._state()
        state["actions"].append({"selector": "#q", "action": "fill", "value": "x",
                                 "source": "llm", "needs_review": True})
        self.assertEqual(map_agentic(state, config_for(client)), {})
        self.assertEqual(client.calls, [])


@unittest.skipUnless(HAS_LANGGRAPH, "langgraph not installed (container-only dep)")
class TestSaveDraftAutoApprove(unittest.TestCase):
    """Tier 1 drafts auto-approve so a --submit run needs no dashboard click."""

    def setUp(self):
        import tempfile
        from utils.db import init_db
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "t.db")
        self.conn = init_db(self.db_path)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _save(self, tier, **cfg):
        from utils.apply_graph import save_draft
        state = {"job": {"id": "j1", "url": "https://x/apply"}, "tier": tier,
                 "verdict": "ok", "actions": [{"selector": "#a", "value": "Max"}],
                 "unfilled": [], "never_fill_skipped": []}
        out = save_draft(state, {"configurable": {"db_path": self.db_path, **cfg}})
        return self.conn.execute(
            "SELECT status, approved_at, notes FROM application_snapshots WHERE id=?",
            (out["snapshot_id"],)).fetchone()

    def test_tier1_is_auto_approved(self):
        row = self._save(1)
        self.assertEqual(row["status"], "approved")
        self.assertTrue(row["approved_at"])
        self.assertIn("auto-approved", row["notes"])

    def test_tier2_stays_draft(self):
        row = self._save(2)
        self.assertEqual(row["status"], "draft")
        self.assertIsNone(row["approved_at"])

    def test_kill_switch_keeps_tier1_in_draft(self):
        row = self._save(1, auto_approve_tier1=False)
        self.assertEqual(row["status"], "draft")
        self.assertIsNone(row["approved_at"])


if __name__ == "__main__":
    unittest.main()
