from __future__ import annotations

import gc
import sqlite3
import sys
import tempfile
import unittest
import warnings
from contextlib import closing
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.database import Database


class DatabaseApplicationStatusTests(unittest.TestCase):
    def test_database_owned_connections_are_closed(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            db_path = Path(temp_dir) / "job_assistant.db"

            with warnings.catch_warnings(record=True) as captured:
                warnings.simplefilter("always", ResourceWarning)
                database = Database(db_path)
                database.execute("CREATE TABLE IF NOT EXISTS lifecycle_probe (id INTEGER)")
                database.fetch_one("SELECT COUNT(*) FROM lifecycle_probe")
                gc.collect()

            unclosed_database_warnings = [
                warning
                for warning in captured
                if issubclass(warning.category, ResourceWarning)
                and "unclosed database" in str(warning.message)
            ]
            self.assertEqual(unclosed_database_warnings, [])

    def test_update_application_state_clears_ready_flag_when_job_is_applied(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            db_path = Path(temp_dir) / "job_assistant.db"
            database = Database(db_path)

            with closing(sqlite3.connect(db_path)) as connection:
                connection.execute(
                    """
                    INSERT INTO jobs (
                        source,
                        url,
                        title,
                        company,
                        description,
                        date_found
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "seek",
                        "https://www.seek.co.nz/job/12345678",
                        "Data Analyst",
                        "Example Co",
                        "SQL and reporting role",
                        "2026-07-22",
                    ),
                )
                job_id = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
                connection.execute(
                    """
                    INSERT INTO applications (
                        job_id,
                        status,
                        ready_to_apply,
                        notes
                    )
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        job_id,
                        "ready_to_apply",
                        1,
                        "Prepared to SEEK review page and awaiting final submit.",
                    ),
                )
                connection.commit()

            database.update_application_state(
                job_id,
                "ready_to_apply",
                note="Prepared to SEEK review page and awaiting final submit.",
            )

            with closing(sqlite3.connect(db_path)) as connection:
                before_row = connection.execute(
                    """
                    SELECT jobs.application_status, applications.ready_to_apply
                    FROM jobs
                    JOIN applications ON applications.job_id = jobs.id
                    WHERE jobs.id = ?
                    """,
                    (job_id,),
                ).fetchone()

            self.assertIsNotNone(before_row)
            assert before_row is not None
            self.assertEqual(before_row[0], "ready_to_apply")
            self.assertEqual(int(before_row[1]), 1)

            database.update_application_state(
                job_id,
                "applied",
                note="Submitted via replayed SEEK flow.",
            )

            with closing(sqlite3.connect(db_path)) as connection:
                after_row = connection.execute(
                    """
                    SELECT
                        jobs.application_status,
                        applications.status,
                        applications.ready_to_apply
                    FROM jobs
                    JOIN applications ON applications.job_id = jobs.id
                    WHERE jobs.id = ?
                    """,
                    (job_id,),
                ).fetchone()

            self.assertIsNotNone(after_row)
            assert after_row is not None
            self.assertEqual(after_row[0], "applied")
            self.assertEqual(after_row[1], "applied")
            self.assertEqual(int(after_row[2]), 0)


if __name__ == "__main__":
    unittest.main()
