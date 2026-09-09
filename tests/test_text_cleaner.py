from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.database import Database
from src.job_importer import JobImporter
from src.text_cleaner import clean_job_description


class TextCleanerTests(unittest.TestCase):
    def test_linkedin_email_text_is_cleaned_and_url_is_extracted(self) -> None:
        noisy = (
            "Auckland 220 school alumni View job: "
            "https://www.linkedin.com/comm/jobs/view/1234567890/?trackingId=abc1234567890xyz987654321&refId=longtoken "
            "actively hiring promoted viewed job reminder job alert digest "
            "Power BI, SQL, stakeholder reporting, and dashboard delivery."
        )
        cleaned = clean_job_description(
            noisy,
            title="BI Analyst",
            company="Example Co",
            location="Auckland",
            source="linkedin",
        )
        self.assertIn("linkedin.com/comm/jobs/view/1234567890/", cleaned["job_url"])
        self.assertNotIn("trackingId", cleaned["clean_description"])
        self.assertNotIn("school alumni", cleaned["clean_description"].lower())
        self.assertNotIn("View job", cleaned["clean_description"])
        self.assertNotIn("https://", cleaned["clean_description"])

    def test_normal_description_is_mostly_unchanged(self) -> None:
        description = (
            "Build Power BI dashboards, write SQL queries, support stakeholder reporting, "
            "and improve operational analytics for Auckland teams."
        )
        cleaned = clean_job_description(
            description,
            title="Data Analyst",
            company="Example Co",
            location="Auckland",
            source="manual",
        )
        self.assertEqual(description, cleaned["clean_description"])
        self.assertEqual("", cleaned["job_url"])

    def test_importer_stores_clean_description_and_separate_url(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            root = Path(temp_dir)
            data_dir = root / "data"
            data_dir.mkdir(parents=True, exist_ok=True)
            for file_name in ["profile.md", "base_cv.txt", "personal_details.json"]:
                source = PROJECT_ROOT / "data" / file_name
                if source.exists():
                    shutil.copy2(source, data_dir / file_name)

            database = Database(data_dir / "job_assistant.db")
            importer = JobImporter(database)
            text = (
                "BI Analyst\n"
                "Example Co\n"
                "Auckland\n"
                "220 school alumni\n"
                "View job: https://www.linkedin.com/comm/jobs/view/1234567890/?trackingId=abc1234567890xyz987654321\n"
                "Actively hiring promoted viewed job reminder job alert digest\n"
                "Power BI, SQL, stakeholder reporting, and dashboard delivery.\n"
            )
            result = importer.import_email_alert_text(text, source_hint="linkedin")
            self.assertEqual(1, result["created_count"])

            details = database.get_job_details(1)
            self.assertIsNotNone(details)
            assert details is not None
            self.assertIn("linkedin.com/comm/jobs/view/1234567890/", details["url"])
            self.assertNotIn("https://", details["description"])
            self.assertNotIn("trackingId", details["description"])
            self.assertNotIn("school alumni", details["description"].lower())

    def test_importer_skips_unusable_url(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            root = Path(temp_dir)
            data_dir = root / "data"
            data_dir.mkdir(parents=True, exist_ok=True)
            database = Database(data_dir / "job_assistant.db")
            importer = JobImporter(database)
            result = importer.import_email_alert_text(
                "Data Analyst\nExample Co\nAuckland\nView job: not-a-real-url\nSQL and reporting support",
                source_hint="linkedin",
            )
            self.assertEqual(0, result["created_count"])
            self.assertEqual(1, len(result["errors"]))


if __name__ == "__main__":
    unittest.main()
