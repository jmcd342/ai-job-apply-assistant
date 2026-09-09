from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.database import Database
from src.duplicate_checker import locations_are_compatible, normalise_location
from src.job_importer import JobImporter


DESCRIPTION = (
    "Build trusted Power BI reporting and SQL analytics for operational teams, "
    "partner with stakeholders, validate data quality, and improve business decisions."
)


class DuplicateCheckerTests(unittest.TestCase):
    def test_location_work_arrangement_suffixes_are_ignored(self) -> None:
        self.assertEqual("mangere auckland", normalise_location("Mangere, Auckland (Hybrid)"))
        self.assertTrue(locations_are_compatible("Queenstown, Otago", "Queenstown, Otago (Remote)"))
        self.assertTrue(locations_are_compatible("Auckland", "Auckland CBD, Auckland (Hybrid)"))
        self.assertFalse(locations_are_compatible("Auckland", "Wellington"))
        self.assertFalse(locations_are_compatible("Auckland, New Zealand", "Wellington, New Zealand"))

    def test_different_seek_tracking_urls_do_not_create_duplicate_role(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            database = Database(Path(temp_dir) / "job_assistant.db")
            importer = JobImporter(database)
            first = importer.import_manual_job(
                source="seek",
                url="https://email.s.seek.co.nz/uni/ss/c/first-tracking-token",
                title="Data Analyst",
                company="JUCY",
                location="Mangere, Auckland (Hybrid)",
                salary="",
                description=DESCRIPTION,
            )
            second = importer.import_manual_job(
                source="seek",
                url="https://email.s.seek.co.nz/uni/ss/c/second-tracking-token",
                title="Data Analyst",
                company="JUCY",
                location="Mangere, Auckland",
                salary="",
                description=DESCRIPTION,
            )
            self.assertTrue(first["created"])
            self.assertFalse(second["created"])
            self.assertEqual(first["job_id"], second["job_id"])

    def test_same_company_and_title_in_different_regions_remain_distinct(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            database = Database(Path(temp_dir) / "job_assistant.db")
            importer = JobImporter(database)
            first = importer.import_manual_job(
                source="seek",
                url="https://email.s.seek.co.nz/uni/ss/c/auckland-role",
                title="Data Analyst",
                company="Example Co",
                location="Auckland",
                salary="",
                description=DESCRIPTION,
            )
            second = importer.import_manual_job(
                source="seek",
                url="https://email.s.seek.co.nz/uni/ss/c/wellington-role",
                title="Data Analyst",
                company="Example Co",
                location="Wellington",
                salary="",
                description=DESCRIPTION,
            )
            self.assertTrue(first["created"])
            self.assertTrue(second["created"])
            self.assertNotEqual(first["job_id"], second["job_id"])


if __name__ == "__main__":
    unittest.main()
