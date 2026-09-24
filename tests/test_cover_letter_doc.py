"""Tests for utils.cover_letter_doc — the downloadable letter.

The letter is downloaded to be ATTACHED, next to a CV named after the
candidate. Without a sender block and a matching filename the attachment says
nothing about who sent it, which only an emailed letter gets away with (the
account signs it). Fictional Max Mustermann data only.
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utils import cover_letter_doc as doc  # noqa: E402

MAX = {"full_name": "Max Mustermann", "email": "max@example.com",
       "phone": "+49 30 000000", "city": "Hamburg"}


class SenderTest(unittest.TestCase):
    def test_name_leads_the_filename_like_the_cv(self):
        with mock.patch.object(doc, "_candidate", return_value=MAX):
            self.assertEqual(doc.file_stem("Acme GmbH", "Backend Engineer"),
                             "Max_Mustermann_cover_letter_Acme_GmbH_Backend_Engineer")

    def test_contact_line_joins_everything_but_the_name(self):
        with mock.patch.object(doc, "_candidate", return_value=MAX):
            self.assertEqual(doc._sender_lines(),
                             ["Max Mustermann",
                              "max@example.com · +49 30 000000 · Hamburg"])

    def test_no_profile_still_builds_a_letter(self):
        # a checkout without candidate_kb (CI, a fresh clone) must not lose the
        # download button — it just has no name to put on it
        with mock.patch.object(doc, "_candidate", return_value={}):
            self.assertEqual(doc.file_stem("Acme", "Dev"), "cover_letter_Acme_Dev")
            self.assertEqual(doc._sender_lines(), [])
            self.assertTrue(doc.build_pdf("Dear team", "Dev", "Acme").startswith(b"%PDF"))

    def test_pdf_and_docx_are_built_with_the_sender(self):
        with mock.patch.object(doc, "_candidate", return_value=MAX):
            self.assertTrue(doc.build_pdf("Dear team", "Dev", "Acme").startswith(b"%PDF"))
            self.assertTrue(doc.build_docx("Dear team", "Dev", "Acme"))


if __name__ == "__main__":
    unittest.main()
