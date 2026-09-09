from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app import _effective_status_series
from src.application_tracker import ApplicationTracker
from src.database import Database
from src.job_importer import JobImporter


class DashboardJobVisibilityTests(unittest.TestCase):
    def test_imported_jobs_remain_visible_when_legacy_application_status_is_null(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            root = Path(temp_dir)
            data_dir = root / "data"
            data_dir.mkdir(parents=True, exist_ok=True)

            database = Database(data_dir / "job_assistant.db")
            importer = JobImporter(database)
            tracker = ApplicationTracker(database, root)

            result = importer.import_manual_job(
                source="seek",
                url="https://www.seek.co.nz/job/12345678",
                title="Data Analyst",
                company="Example Co",
                location="Auckland",
                salary="$100,000",
                description="SQL, Power BI, reporting, stakeholder communication, and analytics delivery.",
                import_channel="test",
            )
            self.assertTrue(result["created"])

            database.execute("UPDATE jobs SET application_status = NULL WHERE id = ?", (result["job_id"],))
            database.update_application_status(result["job_id"], "drafted", "Legacy drafted row")

            reloaded = Database(data_dir / "job_assistant.db")
            reloaded_tracker = ApplicationTracker(reloaded, root)
            jobs = reloaded_tracker.search_jobs(status="All", min_fit_score=0)

            self.assertEqual(len(jobs), 1)
            self.assertIn("application_status", jobs.columns)
            self.assertEqual(jobs.iloc[0]["application_status"], "discovered")

            filtered = jobs[_effective_status_series(jobs).isin(["discovered", "drafted", "needs_user_action"])]
            self.assertEqual(len(filtered), 1)

            snapshot = reloaded.get_jobs_debug_snapshot()
            self.assertEqual(snapshot["total_jobs"], 1)
            self.assertTrue(any(item["status"] == "discovered" for item in snapshot["application_status_counts"]))


if __name__ == "__main__":
    unittest.main()
