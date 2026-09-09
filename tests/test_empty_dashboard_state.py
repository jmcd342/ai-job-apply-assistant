from __future__ import annotations

import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.application_tracker import ApplicationTracker
from src.database import Database


class EmptyDashboardStateTests(unittest.TestCase):
    def test_empty_jobs_dataframe_has_expected_columns_after_reset_like_state(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            root = Path(temp_dir)
            data_dir = root / "data"
            data_dir.mkdir(parents=True, exist_ok=True)
            applications_dir = root / "applications"
            applications_dir.mkdir(parents=True, exist_ok=True)

            database = Database(data_dir / "job_assistant.db")
            tracker = ApplicationTracker(database, root)

            with closing(database.connect()) as connection:
                connection.execute("DELETE FROM generated_documents")
                connection.execute("DELETE FROM applications")
                connection.execute("DELETE FROM jobs")
                connection.execute("DELETE FROM processed_email_messages")
                connection.commit()

            jobs = tracker.search_jobs(status="All", min_fit_score=0)
            self.assertTrue(jobs.empty)
            for column in [
                "title",
                "company",
                "source",
                "location",
                "fit_score",
                "reason_for_rating",
                "status",
                "job_url",
                "imported_at",
            ]:
                self.assertIn(column, jobs.columns)

            filtered = jobs[jobs["status"].isin(["discovered", "drafted", "needs_user_action"])]
            self.assertTrue(filtered.empty)


if __name__ == "__main__":
    unittest.main()
