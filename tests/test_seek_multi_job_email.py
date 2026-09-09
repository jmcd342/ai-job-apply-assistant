from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.database import Database
from src.email_job_parser import parse_email_html_jobs
from src.job_importer import JobImporter


SEEK_MULTI_HTML = """
<html>
  <body>
    <h1>12 new jobs for data</h1>
    <a
      style="display:block"
      href="https://email.s.seek.co.nz/uni/ss/c/abc123"
      data-saferedirecturl="https://www.google.com/url?q=https://email.s.seek.co.nz/uni/ss/c/abc123&amp;source=gmail"
    >
      <div style="text-decoration:underline">Digital Data &amp; Innovation Analyst</div>
      <div>Barker Fruit Processors</div>
      <div>Geraldine, Canterbury</div>
      <div>Salary + Health Insurance</div>
    </a>
    <a
      style="display:block"
      href="https://email.s.seek.co.nz/uni/ss/c/xyz789"
      data-saferedirecturl="https://www.google.com/url?q=https://email.s.seek.co.nz/uni/ss/c/xyz789&amp;source=gmail"
    >
      <div style="text-decoration:underline">Senior Data Analyst</div>
      <div>Insight Partners</div>
      <div>Auckland, New Zealand</div>
      <div>$110,000 - $130,000</div>
    </a>
    <a
      style="display:block"
      href="https://email.s.seek.co.nz/uni/ss/c/xyz789"
      data-saferedirecturl="https://www.google.com/url?q=https://email.s.seek.co.nz/uni/ss/c/xyz789&amp;source=gmail"
    >
      <div style="text-decoration:underline">Senior Data Analyst</div>
      <div>Insight Partners</div>
      <div>Auckland, New Zealand</div>
      <div>$110,000 - $130,000</div>
    </a>
  </body>
</html>
"""


class SeekMultiJobEmailTests(unittest.TestCase):
    def test_multi_job_seek_email_imports_all_unique_cards(self) -> None:
        parsed = parse_email_html_jobs(SEEK_MULTI_HTML, subject="12 new jobs for data", message_id="seek-multi-1")
        self.assertEqual(2, len(parsed.jobs))
        self.assertEqual(3, parsed.card_counts["seek_cards_found"])

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            database = Database(Path(temp_dir) / "job_assistant.db")
            importer = JobImporter(database)
            result = importer.import_email_alert_text(
                "ignored",
                parsed_jobs=parsed.jobs,
                source_hint="seek",
                auckland_only=False,
                skip_clearly_irrelevant_locations=True,
                log_filtered_out=False,
            )
            jobs = database.search_jobs(status="All", min_fit_score=0)

            self.assertEqual(2, result["created_count"])
            self.assertEqual(0, result["duplicate_count"])
            self.assertEqual(
                sorted(result["imported_job_titles"]),
                ["Digital Data & Innovation Analyst", "Senior Data Analyst"],
            )
            self.assertEqual(2, len(jobs))
            self.assertEqual(
                sorted(jobs["title"].tolist()),
                ["Digital Data & Innovation Analyst", "Senior Data Analyst"],
            )
            self.assertEqual(len(set(jobs["title"].tolist())), 2)

            result_again = importer.import_email_alert_text(
                "ignored",
                parsed_jobs=parsed.jobs,
                source_hint="seek",
                auckland_only=False,
                skip_clearly_irrelevant_locations=True,
                log_filtered_out=False,
            )
            jobs_again = database.search_jobs(status="All", min_fit_score=0)

            self.assertEqual(0, result_again["created_count"])
            self.assertEqual(2, result_again["duplicate_count"])
            self.assertEqual(
                sorted(result_again["duplicate_job_titles"]),
                ["Digital Data & Innovation Analyst", "Senior Data Analyst"],
            )
            self.assertEqual(2, len(jobs_again))


if __name__ == "__main__":
    unittest.main()
