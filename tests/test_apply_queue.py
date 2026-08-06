"""Tests for utils/apply_queue.py and the Step 2 DB additions.

Run:  python -m unittest tests.test_apply_queue -v
"""

import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import os  # noqa: E402
from unittest import mock  # noqa: E402

from utils.apply_queue import (  # noqa: E402
    DedupContext,
    build_queue,
    dedup_gate,
    is_addressable,
    is_staffing_employer,
    normalize_company,
    title_excluded,
    title_overreaches,
    topup_budget,
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
             fetched_days_ago=1, jd_hash=None, source="heise", apply_url=None,
             applied_at=None, title=None, form_verdict=None):
    conn.execute(
        "INSERT INTO jobs (id, company, title, url, source, raw_jd_text, fetched_at, "
        "  location, fit_grade, match_score, status, ats, ats_checked_at, jd_hash, apply_url, applied_at, form_verdict) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            job_id, company, title or f"Title {job_id}", f"https://example.com/{job_id}", source,
            "x" * 600, _iso(NOW - timedelta(days=fetched_days_ago)),
            location, grade, score, status, ats,
            _iso(NOW - timedelta(days=checked_days_ago)) if checked_days_ago is not None else None,
            jd_hash, apply_url, applied_at, form_verdict,
        ),
    )
    conn.commit()


class NormalizeCompanyTest(unittest.TestCase):
    def test_strips_german_legal_suffixes(self):
        self.assertEqual(normalize_company("Aleph Alpha GmbH"), "alephalpha")
        self.assertEqual(normalize_company("Allianz SE"), "allianz")
        self.assertEqual(normalize_company("Siemens AG"), "siemens")
        self.assertEqual(normalize_company("Otto GmbH & Co. KG"), "otto")
        self.assertEqual(normalize_company("Beispiel UG (haftungsbeschränkt)"), "beispiel")
        self.assertEqual(normalize_company("Merck KGaA"), "merck")

    def test_strips_international_suffixes(self):
        self.assertEqual(normalize_company("Foo Inc."), "foo")
        self.assertEqual(normalize_company("Bar Ltd"), "bar")
        self.assertEqual(normalize_company("Sopra Steria Limited"), "soprasteria")
        self.assertEqual(normalize_company("Hays plc"), "hays")

    def test_suffix_only_as_trailing_token(self):
        # "ag"/"se" inside a word must survive
        self.assertEqual(normalize_company("Montag Media"), "montagmedia")
        self.assertEqual(normalize_company("Hanse Digital"), "hansedigital")

    def test_squash_matches_observed_double_apply_variants(self):
        # every pair below produced a real duplicate application before the
        # squash step (2026-07 alias scan)
        self.assertEqual(normalize_company("1KOMMA5˚"),  # U+02DA ring above
                         normalize_company("1KOMMA5° GmbH"))  # U+00B0 degree
        self.assertEqual(normalize_company("Deutsches Klimarechenzentrum GmbH"),
                         normalize_company("Deutsches      Klima rechenzentrum GmbH"))
        self.assertEqual(normalize_company("AlephAlpha"),
                         normalize_company("Aleph Alpha GmbH"))

    def test_squash_keeps_distinct_companies_distinct(self):
        self.assertNotEqual(normalize_company("H&S"), normalize_company("H&Z"))
        self.assertNotEqual(normalize_company("Sopra Steria"),
                            normalize_company("Sopra"))


class IsStaffingEmployerTest(unittest.TestCase):
    def test_recruitment_and_staffing_names_match(self):
        # generic descriptor terms only — German staffing/consultancy firms
        # almost always carry one in their legal name. Bare brand names with no
        # descriptor (e.g. "Randstad Deutschland") are deliberately NOT hardcoded
        # (brittle); their different-role case is still handled by the
        # different-title→warn path, only same-title-different-client slips through.
        for name in ("ABALON Recruitment GmbH", "Hays Professional Solutions",
                     "XY Personaldienstleistung GmbH",
                     "Muster Zeitarbeit GmbH", "Foo Personalberatung",
                     "Bar Personalvermittlung", "Acme IT-Beratung",
                     "Some Unternehmensberatung AG", "Global Consulting Group",
                     "Nimbus Consultancy"):
            self.assertTrue(is_staffing_employer(name), name)

    def test_product_companies_do_not_match(self):
        # a product company that merely "consults stakeholders" must not match —
        # the bare word "consult" is never used alone
        for name in ("Zalando SE", "Celonis", "Mistral AI", "HelloFresh",
                     "N26", "Trade Republic", "adesso SE", "Consultstakeholders"):
            self.assertFalse(is_staffing_employer(name), name)

    def test_empty_is_false(self):
        self.assertFalse(is_staffing_employer(None))
        self.assertFalse(is_staffing_employer(""))


class TopupBudgetTest(unittest.TestCase):
    def test_topup_math(self):
        self.assertEqual(topup_budget(0, 40), 40)
        self.assertEqual(topup_budget(25, 40), 15)
        self.assertEqual(topup_budget(40, 40), 0)
        self.assertEqual(topup_budget(50, 40), 0)  # over target → never negative


class IncludeStaleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = init_db(str(Path(self.tmp.name) / "t.db"))
        make_job(self.conn, "fresh", company="Acme", checked_days_ago=1)
        make_job(self.conn, "stale", company="Other", checked_days_ago=10)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_stale_goes_to_recheck_by_default(self):
        r = build_queue(self.conn, budget=10, now=NOW)
        self.assertEqual({j["id"] for j in r["queue"]}, {"fresh"})
        self.assertIn("stale", {j["id"] for j in r["needs_recheck"]})

    def test_include_stale_puts_it_in_queue(self):
        r = build_queue(self.conn, budget=10, now=NOW, include_stale=True)
        self.assertEqual({j["id"] for j in r["queue"]}, {"fresh", "stale"})
        self.assertEqual(r["needs_recheck"], [])

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


class TitleExcludedTest(unittest.TestCase):
    def test_student_roles_excluded(self):
        # snapshot 159 (DLR) reached a Tier-3 draft before this veto existed
        self.assertTrue(title_excluded(
            "Internship/Master Thesis (f/m/x) - Multimodal Models"))
        self.assertTrue(title_excluded("Werkstudent Softwareentwicklung"))
        self.assertTrue(title_excluded("Praktikant (m/w/d) Data Science"))
        self.assertTrue(title_excluded("Working Student Backend"))
        self.assertTrue(title_excluded("Ausbildung Fachinformatiker"))

    def test_intern_does_not_match_international(self):
        self.assertFalse(title_excluded("International Sales Engineer"))
        self.assertFalse(title_excluded("Senior Software Engineer (Internal Tools)"))
        self.assertFalse(title_excluded("Software Engineer"))
        self.assertFalse(title_excluded(None))


class SeniorityRankBackTest(QueueTestBase):
    """② Overreach titles are kept but ranked behind on-target roles — never
    dropped, and the demotion beats even a higher match_score."""

    def test_title_overreaches_matches_leadership_not_ic(self):
        for t in ("Staff Engineer", "Principal Software Engineer",
                  "Head of Engineering", "Solutions Architect",
                  "(Senior) Manager – AI", "Engineering Manager",
                  "VP of Engineering", "Director of Data"):
            self.assertTrue(title_overreaches(t), t)
        for t in ("Software Engineer", "Senior Backend Engineer",
                  "Full Stack Developer", None):
            self.assertFalse(title_overreaches(t), t)

    def test_overreach_demoted_below_on_target_despite_higher_score(self):
        make_job(self.conn, "ic", company="C1", grade="A", score=70,
                 title="Software Engineer")
        make_job(self.conn, "staff", company="C2", grade="A", score=90,
                 title="Staff Engineer")
        result = build_queue(self.conn, budget=1, now=NOW)
        self.assertEqual(self.queue_ids(result), ["ic"])          # in-budget
        self.assertEqual([j["id"] for j in result["over_budget"]], ["staff"])
        # kept & flagged, not blocked/dropped
        self.assertEqual([j["id"] for j in result["blocked"]], [])
        self.assertIn("seniority-reach", result["over_budget"][0]["demote_reasons"])


class FormReachabilityTest(QueueTestBase):
    """③ A persisted stage1 verdict of no-form/captcha ranks a job behind
    reachable roles; non-A dead-ends drop to over_budget, but A-grade is always
    kept in the queue (constraint: A weak-form still gets a manual look)."""

    def test_b_unreachable_demoted_below_b_unknown(self):
        make_job(self.conn, "b-open", company="C1", grade="B", score=70,
                 form_verdict=None)
        make_job(self.conn, "b-dead", company="C2", grade="B", score=70,
                 form_verdict="no-form")
        result = build_queue(self.conn, budget=1, now=NOW)
        self.assertEqual(self.queue_ids(result), ["b-open"])
        self.assertEqual([j["id"] for j in result["over_budget"]], ["b-dead"])
        self.assertEqual(result["over_budget"][0]["demote_reasons"],
                         ["form-unreachable"])

    def test_a_unreachable_stays_in_queue_above_addressable_b(self):
        # constraint #1: an A weak/no-form role outranks a fully-addressable B
        # and is never demoted
        make_job(self.conn, "a-dead", company="C1", grade="A", score=60,
                 form_verdict="captcha")
        make_job(self.conn, "b-fill", company="C2", grade="B", score=95,
                 ats="greenhouse")
        result = build_queue(self.conn, budget=1, now=NOW)
        self.assertEqual(self.queue_ids(result), ["a-dead"])
        self.assertEqual([j["id"] for j in result["over_budget"]], ["b-fill"])
        self.assertEqual(result["queue"][0]["demote_reasons"], [])


class AGradeLivenessWindowTest(QueueTestBase):
    """⑥ A-grade gets a tighter liveness window (3d) than B (7d), so an A is
    re-verified sooner and less likely to surface already-expired."""

    def test_a_recheck_at_4d_but_b_still_live(self):
        make_job(self.conn, "a-mid", company="C1", grade="A", score=80,
                 checked_days_ago=4)   # >3d → A is stale
        make_job(self.conn, "b-mid", company="C2", grade="B", score=70,
                 checked_days_ago=4)   # <7d → B still live
        result = build_queue(self.conn, now=NOW)  # include_stale=False (default)
        self.assertEqual(self.queue_ids(result), ["b-mid"])
        self.assertIn("a-mid", {j["id"] for j in result["needs_recheck"]})

    def test_include_stale_lets_a_through_for_pass_a(self):
        make_job(self.conn, "a-mid", company="C1", grade="A", score=80,
                 checked_days_ago=4)
        result = build_queue(self.conn, now=NOW, include_stale=True)
        self.assertEqual(self.queue_ids(result), ["a-mid"])  # Pass A re-verifies


class SameCompanyBatchTest(QueueTestBase):
    """④ A 2nd+ role at the same company in one batch is ranked back (not
    blocked): the best-ranked one keeps the in-budget slot, the rest go to
    over_budget so one company can't monopolize the review queue."""

    def test_second_role_same_company_demoted_not_blocked(self):
        make_job(self.conn, "freenet-sr", company="freenet AG", grade="A",
                 score=90, title="Senior AI Engineer")
        make_job(self.conn, "freenet-jr", company="freenet AG", grade="A",
                 score=85, title="Junior AI Engineer")  # different title → not batch-blocked
        result = build_queue(self.conn, budget=1, now=NOW)
        self.assertEqual(self.queue_ids(result), ["freenet-sr"])
        self.assertEqual([j["id"] for j in result["over_budget"]], ["freenet-jr"])
        self.assertEqual([j["id"] for j in result["blocked"]], [])   # kept, not blocked
        self.assertIn("same-company-in-batch",
                      result["over_budget"][0]["demote_reasons"])

    def test_distinct_companies_not_demoted(self):
        make_job(self.conn, "a", company="Acme", grade="A", score=80)
        make_job(self.conn, "b", company="Globex", grade="A", score=75)
        result = build_queue(self.conn, budget=2, now=NOW)
        self.assertEqual(set(self.queue_ids(result)), {"a", "b"})
        self.assertTrue(all(j["demote_reasons"] == [] for j in result["queue"]))


class EligibilityTest(QueueTestBase):
    def test_student_title_excluded_from_queue(self):
        make_job(self.conn, "eng", company="C1")
        make_job(self.conn, "intern", company="C2",
                 title="Internship/Master Thesis (f/m/x)")
        result = build_queue(self.conn, now=NOW)
        self.assertEqual(self.queue_ids(result), ["eng"])

    def test_only_scored_status(self):
        for i, status in enumerate(
            ["scored", "applied", "skipped", "expired", "un-scored", "error"]
        ):
            make_job(self.conn, f"j{i}", company=f"C{i}", status=status)
        result = build_queue(self.conn, now=NOW)
        self.assertEqual(self.queue_ids(result), ["j0"])

    def test_grade_threshold(self):
        # MIN_B_SCORE gate: A always; B at/above 65 (the LLM's borderline bucket)
        make_job(self.conn, "a", company="C1", grade="A", score=60)
        make_job(self.conn, "b-at", company="C2", grade="B", score=65)
        make_job(self.conn, "b-below", company="C3", grade="B", score=64)
        make_job(self.conn, "c", company="C4", grade="C", score=95)
        result = build_queue(self.conn, now=NOW)
        self.assertEqual(set(self.queue_ids(result)), {"a", "b-at"})

    def test_germany_only(self):
        make_job(self.conn, "de", company="C1", location="Berlin, Germany")
        make_job(self.conn, "at", company="C2", location="Vienna, Austria")
        make_job(self.conn, "remote", company="C3", location="Remote")
        result = build_queue(self.conn, now=NOW)
        self.assertEqual(self.queue_ids(result), ["de"])

    def test_remote_eu_admitted_other_remote_excluded(self):
        # Chancenkarte → EU-remote is workable; the other triage labels are not.
        make_job(self.conn, "eu", company="C1", location="Remote — EU")
        make_job(self.conn, "non-eu", company="C2", location="Remote — non-EU")
        make_job(self.conn, "bare", company="C3", location="Remote")
        make_job(self.conn, "unclear", company="C4", location="Remote — unclear")
        result = build_queue(self.conn, now=NOW)
        self.assertEqual(self.queue_ids(result), ["eu"])

    def test_dead_ats_excluded(self):
        make_job(self.conn, "alive", company="C1", ats="ashby")
        make_job(self.conn, "gone", company="C2", ats="gone")
        make_job(self.conn, "err", company="C3", ats="fetch-error")
        result = build_queue(self.conn, now=NOW)
        self.assertEqual(self.queue_ids(result), ["alive"])
        self.assertEqual({j["id"] for j in result["dead"]}, {"gone", "err"})

    def test_stale_or_missing_liveness_goes_to_recheck(self):
        # grade B → the general 7-day window (A has a tighter 3-day one, tested
        # separately in AGradeLivenessWindowTest)
        make_job(self.conn, "fresh", company="C1", grade="B", score=70, checked_days_ago=6)
        make_job(self.conn, "stale", company="C2", grade="B", score=70, checked_days_ago=8)
        make_job(self.conn, "never", company="C3", grade="B", score=70, checked_days_ago=None)
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
    def test_same_title_variant_in_batch_blocked(self):
        # multi-city variants of one posting (Breuninger ×3): one draft only,
        # the other variants blocked — not merely warned — within the batch
        make_job(self.conn, "v1", company="Breuninger GmbH", score=90,
                 title="AI Engineer (m/w/d)")
        make_job(self.conn, "v2", company="Breuninger", score=85,
                 title="AI Engineer  (m/w/d)")   # whitespace variant
        result = build_queue(self.conn, now=NOW)
        self.assertEqual(self.queue_ids(result), ["v1"])
        self.assertEqual(result["blocked"][0]["id"], "v2")
        self.assertIn("same-title variant", result["blocked"][0]["dedup_reason"])

    def test_different_role_same_company_still_warns(self):
        make_job(self.conn, "r1", company="Acme", score=90, title="Backend Engineer")
        make_job(self.conn, "r2", company="Acme", score=85, title="Data Engineer")
        result = build_queue(self.conn, now=NOW)
        self.assertEqual(self.queue_ids(result), ["r1", "r2"])
        second = next(j for j in result["queue"] if j["id"] == "r2")
        self.assertEqual(second["dedup"], "warn")

    def test_block_company_in_pipeline(self):
        # same role, suffix-normalized company name must still match → block
        make_job(self.conn, "applied", company="Acme GmbH", status="applied",
                 title="Backend Engineer")
        make_job(self.conn, "new", company="Acme", title="Backend Engineer")
        result = build_queue(self.conn, now=NOW)
        self.assertEqual(self.queue_ids(result), [])
        self.assertEqual(result["blocked"][0]["id"], "new")
        self.assertIn("pipeline", result["blocked"][0]["dedup_reason"])

    def test_applied_company_different_role_warns_not_blocks(self):
        # supply-yield relaxation: a genuinely different role at a company we
        # applied to is a human decision (warn), not a hard block
        make_job(self.conn, "applied", company="Zalando SE", status="applied",
                 title="Backend Engineer")
        make_job(self.conn, "new", company="Zalando", title="ML Platform Engineer")
        result = build_queue(self.conn, now=NOW)
        self.assertEqual(self.queue_ids(result), ["new"])
        self.assertEqual(result["queue"][0]["dedup"], "warn")
        self.assertIn("different role", result["queue"][0]["dedup_reason"])

    def test_applied_company_same_role_still_blocks(self):
        make_job(self.conn, "applied", company="Zalando SE", status="applied",
                 title="Backend Engineer")
        make_job(self.conn, "repost", company="Zalando", title="Backend  Engineer")
        result = build_queue(self.conn, now=NOW)
        self.assertEqual(self.queue_ids(result), [])
        self.assertEqual(result["blocked"][0]["id"], "repost")
        self.assertIn("pipeline", result["blocked"][0]["dedup_reason"])

    def test_staffing_company_in_pipeline_warns_even_same_title(self):
        # a recruitment agency fronts many end clients — a second posting there
        # (even same title = different client) is a warn, never a company block
        make_job(self.conn, "a1", company="ABALON Recruitment GmbH",
                 status="applied", title="Backend Developer")
        make_job(self.conn, "a2", company="ABALON Recruitment",
                 title="Backend Developer")
        result = build_queue(self.conn, now=NOW)
        self.assertEqual(self.queue_ids(result), ["a2"])
        self.assertEqual(result["queue"][0]["dedup"], "warn")
        self.assertIn("staffing", result["queue"][0]["dedup_reason"])

    def test_staffing_same_title_rejected_still_blocks(self):
        # a rejection is a rejection even for a staffing firm: the exact role
        # that said no stays out
        make_job(self.conn, "rej", company="Hays Professional Solutions GmbH",
                 status="rejected", title="Data Engineer",
                 applied_at=_iso(NOW - timedelta(days=5)))
        make_job(self.conn, "repost", company="Hays Professional Solutions",
                 title="Data Engineer")
        result = build_queue(self.conn, now=NOW)
        self.assertEqual(self.queue_ids(result), [])
        self.assertIn("same title previously rejected",
                      result["blocked"][0]["dedup_reason"])

    def test_ghosted_company_within_cooldown_still_blocks(self):
        make_job(self.conn, "ghost", company="GhostCo", status="ghosted",
                 applied_at=_iso(NOW - timedelta(days=30)))
        make_job(self.conn, "new", company="GhostCo")
        result = build_queue(self.conn, now=NOW)
        self.assertEqual(self.queue_ids(result), [])
        self.assertEqual(result["blocked"][0]["id"], "new")

    def test_ghosted_company_past_cooldown_released_with_warn(self):
        # released into the queue, but visibly: the reviewer must see "we
        # applied here before" (DKRZ case — silent release hid a re-apply)
        make_job(self.conn, "ghost", company="GhostCo", status="ghosted",
                 applied_at=_iso(NOW - timedelta(days=90)))
        make_job(self.conn, "new", company="GhostCo")
        result = build_queue(self.conn, now=NOW)
        self.assertEqual(self.queue_ids(result), ["new"])
        self.assertEqual(result["queue"][0]["dedup"], "warn")
        self.assertIn("no reply", result["queue"][0]["dedup_reason"])

    def test_ghosted_missing_applied_at_fails_closed(self):
        make_job(self.conn, "ghost", company="GhostCo", status="ghosted")  # no applied_at
        make_job(self.conn, "new", company="GhostCo")
        result = build_queue(self.conn, now=NOW)
        self.assertEqual(self.queue_ids(result), [])
        self.assertIn("pipeline", result["blocked"][0]["dedup_reason"])

    def test_ghosted_past_cooldown_but_rejected_keeps_block(self):
        # a stronger terminal status on the same company wins over the cooled ghost
        make_job(self.conn, "ghost", company="GhostCo", status="ghosted",
                 applied_at=_iso(NOW - timedelta(days=90)))
        make_job(self.conn, "rej", company="GhostCo GmbH", status="rejected")
        make_job(self.conn, "new", company="GhostCo")
        result = build_queue(self.conn, now=NOW)
        self.assertEqual(self.queue_ids(result), [])

    def test_rejected_company_within_cooldown_still_blocks(self):
        make_job(self.conn, "rej", company="NoCo", status="rejected",
                 applied_at=_iso(NOW - timedelta(days=10)))
        make_job(self.conn, "new", company="NoCo", title="Data Engineer")
        result = build_queue(self.conn, now=NOW)
        self.assertEqual(self.queue_ids(result), [])
        self.assertEqual(result["blocked"][0]["id"], "new")

    def test_rejected_company_past_cooldown_warns_on_different_role(self):
        make_job(self.conn, "rej", company="NoCo", status="rejected",
                 title="Backend Engineer", applied_at=_iso(NOW - timedelta(days=45)))
        make_job(self.conn, "new", company="NoCo GmbH", title="Data Engineer")
        result = build_queue(self.conn, now=NOW)
        self.assertEqual(self.queue_ids(result), ["new"])
        self.assertEqual(result["queue"][0]["dedup"], "warn")
        self.assertIn("rejected", result["queue"][0]["dedup_reason"])

    def test_rejected_same_title_blocked_even_past_cooldown(self):
        make_job(self.conn, "rej", company="NoCo", status="rejected",
                 title="Backend  Engineer", applied_at=_iso(NOW - timedelta(days=45)))
        make_job(self.conn, "repost", company="NoCo GmbH", title="Backend Engineer")
        result = build_queue(self.conn, now=NOW)
        self.assertEqual(self.queue_ids(result), [])
        self.assertIn("same title previously rejected",
                      result["blocked"][0]["dedup_reason"])

    def test_rejected_missing_applied_at_fails_closed(self):
        make_job(self.conn, "rej", company="NoCo", status="rejected")  # no applied_at
        make_job(self.conn, "new", company="NoCo", title="Data Engineer")
        result = build_queue(self.conn, now=NOW)
        self.assertEqual(self.queue_ids(result), [])
        self.assertIn("pipeline", result["blocked"][0]["dedup_reason"])

    def test_block_company_with_in_flight_snapshot(self):
        # same role, in-flight draft at the suffix-normalized company → block
        make_job(self.conn, "drafted", company="Beispiel UG (haftungsbeschränkt)",
                 title="Backend Engineer")
        make_job(self.conn, "second", company="Beispiel", title="Backend Engineer")
        create_application_snapshot(self.conn, "drafted", status="draft")
        result = build_queue(self.conn, now=NOW)
        self.assertEqual(self.queue_ids(result), [])  # drafted skipped, second blocked
        self.assertEqual(result["blocked"][0]["id"], "second")

    def test_in_flight_different_role_warns_not_blocks(self):
        # a draft in progress for one role must not hide a genuinely different
        # role at the same company — surfaced as a warn, human decides
        make_job(self.conn, "drafted", company="Beispiel UG (haftungsbeschränkt)",
                 title="Backend Engineer")
        make_job(self.conn, "other", company="Beispiel", title="Data Engineer")
        create_application_snapshot(self.conn, "drafted", status="draft")
        result = build_queue(self.conn, now=NOW)
        self.assertEqual(self.queue_ids(result), ["other"])
        self.assertEqual(result["queue"][0]["dedup"], "warn")
        self.assertIn("different role", result["queue"][0]["dedup_reason"])

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


class IsAddressableTest(unittest.TestCase):
    def test_structured_ats_by_column(self):
        for ats in ("greenhouse", "lever", "ashby", "workable", "personio"):
            self.assertTrue(is_addressable({"ats": ats, "apply_url": ""}), ats)

    def test_case_insensitive(self):
        self.assertTrue(is_addressable({"ats": "Greenhouse", "apply_url": ""}))

    def test_disguise_pierced_via_url(self):
        # ats column reads unknown, but the apply_url reveals the real ATS.
        cases = [
            "https://www.workato.com/careers/x?gh_jid=8579922002",   # greenhouse embed
            "https://boards.greenhouse.io/acme/jobs/1",
            "https://jobs.lever.co/acme/123",
            "https://acme.ashbyhq.com/x",
            "https://www.personio.com/careers/abc",
        ]
        for url in cases:
            self.assertTrue(is_addressable({"ats": "unknown", "apply_url": url}), url)

    def test_human_floor_not_addressable(self):
        for url in ("https://de.indeed.com/viewjob?jk=x",
                    "https://join.com/companies/x/apply",
                    "https://virtualq.softgarden.io/x"):
            self.assertFalse(is_addressable({"ats": "unknown", "apply_url": url}), url)

    def test_missing_fields_safe(self):
        self.assertFalse(is_addressable({}))
        self.assertFalse(is_addressable({"ats": None, "apply_url": None}))


class AddressableRankingTest(QueueTestBase):
    def test_addressable_floats_up_within_grade(self):
        # Same freshness + same grade + same score: addressable wins.
        make_job(self.conn, "plain", company="C1", grade="A", score=88, ats="unknown")
        make_job(self.conn, "fill", company="C2", grade="A", score=88, ats="greenhouse")
        with mock.patch.dict(os.environ, {"APPLY_PREFER_ADDRESSABLE": "1"}):
            result = build_queue(self.conn, now=NOW)
        self.assertEqual(self.queue_ids(result), ["fill", "plain"])

    def test_bias_never_lets_b_jump_a(self):
        make_job(self.conn, "a-plain", company="C1", grade="A", score=80, ats="unknown")
        make_job(self.conn, "b-fill", company="C2", grade="B", score=95, ats="lever")
        with mock.patch.dict(os.environ, {"APPLY_PREFER_ADDRESSABLE": "1"}):
            result = build_queue(self.conn, now=NOW)
        self.assertEqual(self.queue_ids(result), ["a-plain", "b-fill"])

    def test_bias_off_keeps_score_order(self):
        make_job(self.conn, "plain-hi", company="C1", grade="A", score=90, ats="unknown")
        make_job(self.conn, "fill-lo", company="C2", grade="A", score=80, ats="greenhouse")
        with mock.patch.dict(os.environ, {"APPLY_PREFER_ADDRESSABLE": "0"}):
            result = build_queue(self.conn, now=NOW)
        self.assertEqual(self.queue_ids(result), ["plain-hi", "fill-lo"])

    def test_addressable_flag_on_queued_jobs(self):
        make_job(self.conn, "fill", company="C1", ats="ashby")
        result = build_queue(self.conn, now=NOW)
        self.assertTrue(result["queue"][0]["addressable"])


class ExperimentGateTest(QueueTestBase):
    def test_min_score_gate(self):
        make_job(self.conn, "hi", company="C1", grade="A", score=85)
        make_job(self.conn, "lo", company="C2", grade="A", score=84)
        with mock.patch.dict(os.environ, {"APPLY_MIN_SCORE": "85"}):
            result = build_queue(self.conn, now=NOW)
        self.assertEqual(self.queue_ids(result), ["hi"])

    def test_addressable_only_gate(self):
        make_job(self.conn, "fill", company="C1", ats="greenhouse")
        make_job(self.conn, "disguised", company="C2", ats="unknown",
                 apply_url="https://x.com/a?gh_jid=1")
        make_job(self.conn, "drop", company="C3", ats="indeed")
        with mock.patch.dict(os.environ, {"APPLY_ADDRESSABLE_ONLY": "1"}):
            result = build_queue(self.conn, now=NOW)
        self.assertEqual(set(self.queue_ids(result)), {"fill", "disguised"})

    def test_gates_combine_for_cohort(self):
        make_job(self.conn, "cohort", company="C1", grade="A", score=88, ats="lever")
        make_job(self.conn, "low-score", company="C2", grade="A", score=70, ats="lever")
        make_job(self.conn, "not-fill", company="C3", grade="A", score=88, ats="indeed")
        with mock.patch.dict(os.environ,
                             {"APPLY_ADDRESSABLE_ONLY": "1", "APPLY_MIN_SCORE": "85"}):
            result = build_queue(self.conn, now=NOW)
        self.assertEqual(self.queue_ids(result), ["cohort"])


if __name__ == "__main__":
    unittest.main()
