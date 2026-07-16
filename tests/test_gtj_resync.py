"""Tests for phase1_ingestor._gtj_resync_urls — germantechjobs slug rotation.

The site rotates slugs while the posting lives on (multi-city variants
collapse into a base slug; company renames re-slug entirely); the stale slug
200-redirects onto a category listing and read as "gone" downstream.

Run:  python -m unittest tests.test_gtj_resync -v   (container: needs bs4/yaml)
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:  # phase1_ingestor needs requests/bs4/yaml — container, not the host
    from phase1_ingestor import GTJ_BASE, _gtj_resync_urls, _url_in_db, make_id
    HAS_DEPS = True
except ModuleNotFoundError:
    HAS_DEPS = False

from utils.db import init_db  # noqa: E402


def gtj_url(slug: str) -> str:
    return f"https://germantechjobs.de/jobs/{slug}"


@unittest.skipUnless(HAS_DEPS, "phase1_ingestor deps not installed on this host")
class GtjResyncTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = init_db(str(Path(self.tmp.name) / "jobs.db"))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def add_job(self, slug, *, title="Engineer (m/w/d)", company="Acme GmbH",
                location="Stuttgart", status="scored", ats="unknown"):
        url = gtj_url(slug)
        self.conn.execute(
            "INSERT INTO jobs (id, company, title, url, source, raw_jd_text,"
            " fetched_at, status, location, ats, ats_checked_at, apply_url)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (make_id(url), company, title, url, "germantechjobs", "jd",
             "2026-07-01T08:00:00", status, location, ats,
             "2026-07-10T08:00:00", url))
        self.conn.commit()
        return make_id(url)

    def job(self, jid):
        return self.conn.execute(
            "SELECT url, ats, ats_checked_at, apply_url FROM jobs WHERE id=?",
            (jid,)).fetchone()

    def api(self, *entries):
        return [{"jobUrl": slug, "name": name, "company": company,
                 "cityCategory": city}
                for slug, name, company, city in entries]

    def test_valid_slug_untouched(self):
        jid = self.add_job("Acme-GmbH-Engineer-mwd")
        n = _gtj_resync_urls(self.conn, self.api(
            ("Acme-GmbH-Engineer-mwd", "Engineer (m/w/d)", "Acme GmbH", "Stuttgart")))
        self.assertEqual(n, 0)
        self.assertEqual(self.job(jid)["ats"], "unknown")  # scan info kept

    def test_city_suffix_variant_repaired(self):
        # multi-city variants collapsed into the base slug (LBBW/Proemion case)
        jid = self.add_job("Acme-GmbH-Engineer-mwd---Stuttgart",
                           title="Engineer (m/w/d) - Stuttgart")
        n = _gtj_resync_urls(self.conn, self.api(
            ("Acme-GmbH-Engineer-mwd", "Engineer (m/w/d)", "Acme GmbH", "Stuttgart")))
        self.assertEqual(n, 1)
        row = self.job(jid)
        self.assertEqual(row["url"], gtj_url("Acme-GmbH-Engineer-mwd"))
        # the old scan judged the old URL — must be redone on the new one
        self.assertIsNone(row["ats"])
        self.assertIsNone(row["ats_checked_at"])
        self.assertIsNone(row["apply_url"])

    def test_company_rename_repaired_via_title(self):
        # "LBBW" became "LBBW Landesbank Baden-Württemberg" — whole slug rotated
        jid = self.add_job("LBBW-Senior-Analyst-mwd", title="Senior Analyst (m/w/d)",
                           company="LBBW")
        n = _gtj_resync_urls(self.conn, self.api(
            ("LBBW-Landesbank-Baden-Wrttemberg-Senior-Analyst-mwd",
             "Senior Analyst (m/w/d)", "LBBW Landesbank Baden-Württemberg",
             "Stuttgart")))
        self.assertEqual(n, 1)
        self.assertEqual(
            self.job(jid)["url"],
            gtj_url("LBBW-Landesbank-Baden-Wrttemberg-Senior-Analyst-mwd"))

    def test_ambiguous_title_match_skipped(self):
        # two current postings share the stripped title and company; the DB
        # row's city matches neither → leave it alone (truly delisted variant)
        jid = self.add_job("Acme-GmbH-Dev-mwd---Munich",
                           title="Dev (m/w/d) - Munich", location="Munich")
        n = _gtj_resync_urls(self.conn, self.api(
            ("Acme-GmbH-Dev-mwd---Bautzen", "Dev (m/w/d) - Bautzen", "Acme GmbH", "Bautzen"),
            ("Acme-GmbH-Dev-mwd---Grlitz", "Dev (m/w/d) - Görlitz", "Acme GmbH", "Görlitz")))
        self.assertEqual(n, 0)
        self.assertEqual(self.job(jid)["url"], gtj_url("Acme-GmbH-Dev-mwd---Munich"))

    def test_variant_of_existing_canonical_row_is_left_as_dup(self):
        # base row already lives in the DB — repairing the variant onto the
        # same URL would create two rows for one posting
        self.add_job("Acme-GmbH-Engineer-mwd")
        jid_var = self.add_job("Acme-GmbH-Engineer-mwd---Cologne",
                               title="Engineer (m/w/d) - Cologne", location="Cologne")
        n = _gtj_resync_urls(self.conn, self.api(
            ("Acme-GmbH-Engineer-mwd", "Engineer (m/w/d)", "Acme GmbH", "Stuttgart")))
        self.assertEqual(n, 0)
        self.assertEqual(self.job(jid_var)["url"],
                         gtj_url("Acme-GmbH-Engineer-mwd---Cologne"))

    def test_two_variants_only_first_claims_the_base(self):
        a = self.add_job("Acme-GmbH-Engineer-mwd---Cologne",
                         title="Engineer (m/w/d) - Cologne", location="Cologne")
        b = self.add_job("Acme-GmbH-Engineer-mwd---Hamburg",
                         title="Engineer (m/w/d) - Hamburg", location="Hamburg")
        n = _gtj_resync_urls(self.conn, self.api(
            ("Acme-GmbH-Engineer-mwd", "Engineer (m/w/d)", "Acme GmbH", "Stuttgart")))
        self.assertEqual(n, 1)
        urls = {self.job(a)["url"], self.job(b)["url"]}
        self.assertIn(gtj_url("Acme-GmbH-Engineer-mwd"), urls)  # one repaired
        self.assertEqual(len(urls), 2)                          # the other kept

    def test_title_match_never_repoints_to_another_city_variant(self):
        # real miss (preview run): the Munich variant was delisted while the
        # Bielefeld variant lives on — same title, same company, one candidate,
        # but it is a DIFFERENT city's posting
        jid = self.add_job("eWolff-GmbH-MarTech-Engineer-mwd---Munich",
                           title="MarTech Engineer (m/w/d) - Munich",
                           company="eWolff GmbH", location="Munich")
        n = _gtj_resync_urls(self.conn, self.api(
            ("eWolff-GmbH-MarTech-Engineer-mwd---Bielefeld",
             "MarTech Engineer (m/w/d) - Bielefeld", "eWolff GmbH", "Bielefeld")))
        self.assertEqual(n, 0)
        self.assertEqual(self.job(jid)["url"],
                         gtj_url("eWolff-GmbH-MarTech-Engineer-mwd---Munich"))

    def test_delisted_job_untouched(self):
        jid = self.add_job("Gone-GmbH-Old-Role-mwd", title="Old Role (m/w/d)",
                           company="Gone GmbH")
        n = _gtj_resync_urls(self.conn, self.api(
            ("Other-AG-Something-mwd", "Something (m/w/d)", "Other AG", "Berlin")))
        self.assertEqual(n, 0)
        self.assertEqual(self.job(jid)["url"], gtj_url("Gone-GmbH-Old-Role-mwd"))

    def test_url_in_db_sees_resynced_row(self):
        # after a repair the row's id is no longer hash(url) — the scraper's
        # pre-fetch dedup must still find it by url, or it would re-insert
        self.add_job("Acme-GmbH-Engineer-mwd---Stuttgart",
                     title="Engineer (m/w/d) - Stuttgart")
        _gtj_resync_urls(self.conn, self.api(
            ("Acme-GmbH-Engineer-mwd", "Engineer (m/w/d)", "Acme GmbH", "Stuttgart")))
        self.assertTrue(_url_in_db(self.conn, gtj_url("Acme-GmbH-Engineer-mwd")))


if __name__ == "__main__":
    unittest.main()
