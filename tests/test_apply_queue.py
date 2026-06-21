"""Tests for utils/apply_queue.py and the Step 2 DB additions.

Run:  python -m unittest tests.test_apply_queue -v
"""

import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utils.apply_queue import (  # noqa: E402
    DedupContext,
    build_queue,
    dedup_gate,
    normalize_company,
)
from utils.db import (  # noqa: E402
    create_application_snapshot,
    get_application_snapshots,
    get_in_flight_snapshots,
    init_db,
    update_application_snapshot,
)

NOW = datetime(2026, 6, 11, 12, 0, 0)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def make_job(conn, job_id, *, company="Acme", grade="A", score=80, status="scored",
             location="Hamburg, Germany", ats="unknown", checked_days_ago=1,
             fetched_days_ago=1, jd_hash=None, source="heise"):
    conn.execute(
        "INSERT INTO jobs (id, company, title, url, source, raw_jd_text, fetched_at, "
        "  location, fit_grade, match_score, status, ats, ats_checked_at, jd_hash) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            job_id, company, f"Title {job_id}", f"https://example.com/{job_id}", source,
            "x" * 600, _iso(NOW - timedelta(days=fetched_days_ago)),
            location, grade, score, status, ats,
            _iso(NOW - timedelta(days=checked_days_ago)) if checked_days_ago is not None else None,
            jd_hash,
        ),
    )
    conn.commit()


class NormalizeCompanyTest(unittest.TestCase):
    def test_strips_german_legal_suffixes(self):
        self.assertEqual(normalize_company("Aleph Alpha GmbH"), "aleph alpha")
        self.assertEqual(normalize_company("Allianz SE"), "allianz")
        self.assertEqual(normalize_company("Siemens AG"), "siemens")
        self.assertEqual(normalize_company("Otto GmbH & Co. KG"), "otto")
        self.assertEqual(normalize_company("Beispiel UG (haftungsbeschränkt)"), "beispiel")

    def test_strips_international_suffixes(self):
        self.assertEqual(normalize_company("Foo Inc."), "foo")
        self.assertEqual(normalize_company("Bar Ltd"), "bar")

    def test_suffix_only_as_trailing_token(self):
        # "ag"/"se" inside a word must survive
        self.assertEqual(normalize_company("Montag Media"), "montag media")
        self.assertEqual(normalize_company("Hanse Digital"), "hanse digital")

    def test_case_and_whitespace_insensitive(self):
        self.assertEqual(normalize_company("  ACME   gmbh "), normalize_company("Acme GmbH"))

    def test_plain_name_unchanged(self):
        self.assertEqual(normalize_company("Zalando"), "zalando")


class QueueTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = init_db(str(Path(self.tmp.name) / "jobs.db"))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def queue_ids(self, result):
        return [j["id"] for j in result["queue"]]


class EligibilityTest(QueueTestBase):
    def test_only_scored_status(self):
        for i, status in enumerate(
            ["scored", "applied", "skipped", "expired", "un-scored", "error"]
        ):
            make_job(self.conn, f"j{i}", company=f"C{i}", status=status)
        result = build_queue(self.conn, now=NOW)
        self.assertEqual(self.queue_ids(result), ["j0"])

    def test_grade_threshold(self):
        make_job(self.conn, "a", company="C1", grade="A", score=60)
        make_job(self.conn, "b-high", company="C2", grade="B", score=70)
        make_job(self.conn, "b-low", company="C3", grade="B", score=69)
        make_job(self.conn, "c", company="C4", grade="C", score=95)
        result = build_queue(self.conn, now=NOW)
        self.assertEqual(set(self.queue_ids(result)), {"a", "b-high"})

    def test_germany_only(self):
        make_job(self.conn, "de", company="C1", location="Berlin, Germany")
        make_job(self.conn, "at", company="C2", location="Vienna, Austria")
        make_job(self.conn, "remote", company="C3", location="Remote")
        result = build_queue(self.conn, now=NOW)
        self.assertEqual(self.queue_ids(result), ["de"])

    def test_dead_ats_excluded(self):
        make_job(self.conn, "alive", company="C1", ats="ashby")
        make_job(self.conn, "gone", company="C2", ats="gone")
        make_job(self.conn, "err", company="C3", ats="fetch-error")
        result = build_queue(self.conn, now=NOW)
        self.assertEqual(self.queue_ids(result), ["alive"])
        self.assertEqual({j["id"] for j in result["dead"]}, {"gone", "err"})

    def test_stale_or_missing_liveness_goes_to_recheck(self):
        make_job(self.conn, "fresh", company="C1", checked_days_ago=6)
        make_job(self.conn, "stale", company="C2", checked_days_ago=8)
        make_job(self.conn, "never", company="C3", checked_days_ago=None)
        result = build_queue(self.conn, now=NOW)
        self.assertEqual(self.queue_ids(result), ["fresh"])
        self.assertEqual({j["id"] for j in result["needs_recheck"]}, {"stale", "never"})

    def test_job_with_in_flight_snapshot_not_requeued(self):
        make_job(self.conn, "drafted", company="C1")
        make_job(self.conn, "clean", company="C2")
        create_application_snapshot(self.conn, "drafted", status="draft")
        result = build_queue(self.conn, now=NOW)
        self.assertEqual(self.queue_ids(result), ["clean"])

    def test_abandoned_snapshot_allows_requeue(self):
        make_job(self.conn, "retry", company="C1")
        sid = create_application_snapshot(self.conn, "retry", status="draft")
        update_application_snapshot(self.conn, sid, status="abandoned")
        result = build_queue(self.conn, now=NOW)
        self.assertEqual(self.queue_ids(result), ["retry"])


class RankingTest(QueueTestBase):
    def test_fresh_bucket_beats_grade(self):
        make_job(self.conn, "old-a", company="C1", grade="A", score=95, fetched_days_ago=10)
        make_job(self.conn, "fresh-b", company="C2", grade="B", score=70, fetched_days_ago=2)
        result = build_queue(self.conn, now=NOW)
        self.assertEqual(self.queue_ids(result), ["fresh-b", "old-a"])

    def test_grade_beats_score_within_bucket(self):
        make_job(self.conn, "a-low", company="C1", grade="A", score=72, fetched_days_ago=1)
        make_job(self.conn, "b-high", company="C2", grade="B", score=90, fetched_days_ago=1)
        result = build_queue(self.conn, now=NOW)
        self.assertEqual(self.queue_ids(result), ["a-low", "b-high"])

    def test_score_desc_within_grade(self):
        make_job(self.conn, "lo", company="C1", score=75, fetched_days_ago=1)
        make_job(self.conn, "hi", company="C2", score=88, fetched_days_ago=1)
        result = build_queue(self.conn, now=NOW)
        self.assertEqual(self.queue_ids(result), ["hi", "lo"])

    def test_fetched_at_tiebreak_newest_first(self):
        make_job(self.conn, "older", company="C1", score=80, fetched_days_ago=2)
        make_job(self.conn, "newer", company="C2", score=80, fetched_days_ago=1)
        result = build_queue(self.conn, now=NOW)
        self.assertEqual(self.queue_ids(result), ["newer", "older"])

    def test_budget_truncation(self):
        for i in range(30):
            make_job(self.conn, f"j{i:02d}", company=f"C{i}", score=99 - i)
        result = build_queue(self.conn, budget=25, now=NOW)
        self.assertEqual(len(result["queue"]), 25)
        self.assertEqual(len(result["over_budget"]), 5)
        # ranks continue across the cut
        self.assertEqual(result["over_budget"][0]["rank"], 26)


class DedupGateTest(QueueTestBase):
    def test_block_company_in_pipeline(self):
        make_job(self.conn, "applied", company="Acme GmbH", status="applied")
        make_job(self.conn, "new", company="Acme")  # suffix differs, must still match
        result = build_queue(self.conn, now=NOW)
        self.assertEqual(self.queue_ids(result), [])
        self.assertEqual(result["blocked"][0]["id"], "new")
        self.assertIn("pipeline", result["blocked"][0]["dedup_reason"])

    def test_block_company_with_in_flight_snapshot(self):
        make_job(self.conn, "drafted", company="Beispiel UG (haftungsbeschränkt)")
        make_job(self.conn, "second", company="Beispiel")
        create_application_snapshot(self.conn, "drafted", status="draft")
        result = build_queue(self.conn, now=NOW)
        self.assertEqual(self.queue_ids(result), [])  # drafted skipped, second blocked
        self.assertEqual(result["blocked"][0]["id"], "second")

    def test_warn_jd_hash_applied_elsewhere(self):
        make_job(self.conn, "via-board", company="Recruiter AG", status="applied",
                 jd_hash="deadbeef")
        make_job(self.conn, "direct", company="Original", jd_hash="deadbeef")
        result = build_queue(self.conn, now=NOW)
        self.assertEqual(self.queue_ids(result), ["direct"])
        self.assertEqual(result["queue"][0]["dedup"], "warn")
        self.assertIn("same JD", result["queue"][0]["dedup_reason"])

    def test_warn_second_company_job_in_batch(self):
        make_job(self.conn, "first", company="Dupli GmbH", score=90)
        make_job(self.conn, "second", company="Dupli", score=80)
        result = build_queue(self.conn, now=NOW)
        self.assertEqual(self.queue_ids(result), ["first", "second"])
        self.assertEqual(result["queue"][0]["dedup"], "ok")
        self.assertEqual(result["queue"][1]["dedup"], "warn")
        self.assertIn("batch", result["queue"][1]["dedup_reason"])

    def test_gate_unit_block_beats_warn(self):
        ctx = DedupContext({"acme": "applied"}, {}, {"h1": "other"})
        verdict, _ = dedup_gate(
            {"id": "x", "company": "Acme GmbH", "jd_hash": "h1"}, ctx
        )
        self.assertEqual(verdict, "block")


class SnapshotCrudTest(QueueTestBase):
    def test_create_and_read_roundtrip(self):
        make_job(self.conn, "j1")
        sid = create_application_snapshot(
            self.conn, "j1", status="draft", tier=2, channel="generic-form",
            form_payload={"Vorname": "Max"}, custom_qa={"Why us?": "Because."},
        )
        snaps = get_application_snapshots(self.conn, "j1")
        self.assertEqual(len(snaps), 1)
        self.assertEqual(snaps[0]["id"], sid)
        self.assertEqual(snaps[0]["status"], "draft")
        self.assertEqual(snaps[0]["form_payload"], '{"Vorname": "Max"}')
        self.assertTrue(snaps[0]["created_at"])

    def test_update_and_in_flight_listing(self):
        make_job(self.conn, "j1")
        sid = create_application_snapshot(self.conn, "j1", status="draft")
        update_application_snapshot(self.conn, sid, status="submitted",
                                    submitted_by="human")
        self.assertEqual([s["id"] for s in get_in_flight_snapshots(self.conn)], [sid])
        update_application_snapshot(self.conn, sid, status="abandoned")
        self.assertEqual(get_in_flight_snapshots(self.conn), [])

    def test_invalid_status_rejected(self):
        make_job(self.conn, "j1")
        with self.assertRaises(ValueError):
            create_application_snapshot(self.conn, "j1", status="pending")
        with self.assertRaises(ValueError):
            create_application_snapshot(self.conn, "j1", bogus_field=1)


class BuildQueueIsReadOnlyTest(QueueTestBase):
    def test_no_status_writes(self):
        make_job(self.conn, "j1", company="C1")
        make_job(self.conn, "j2", company="C2", ats="gone")
        make_job(self.conn, "j3", company="C3", checked_days_ago=None)
        before = self.conn.execute(
            "SELECT id, status, ats, ats_checked_at FROM jobs ORDER BY id"
        ).fetchall()
        build_queue(self.conn, now=NOW)
        after = self.conn.execute(
            "SELECT id, status, ats, ats_checked_at FROM jobs ORDER BY id"
        ).fetchall()
        self.assertEqual([tuple(r) for r in before], [tuple(r) for r in after])
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM application_snapshots").fetchone()[0], 0
        )


if __name__ == "__main__":
    unittest.main()
