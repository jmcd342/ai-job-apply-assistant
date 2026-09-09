from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from email.message import EmailMessage
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.database import Database
from src.document_generator import DocumentGenerator
from src.email_alert_ingestor import EmailAlertIngestor
from src.email_job_parser import parse_email_html_jobs
from src.job_importer import JobImporter


LINKEDIN_HTML = """
<html>
  <body>
    <h1>Your job alert has been created</h1>
    <p>Your job alert for data engineer roles is now active.</p>
    <table>
      <tr>
        <td>
          <a href="https://www.linkedin.com/comm/jobs/view/1111111111/?trk=jobcard_body_1111111111">Intermediate Data Engineer</a>
          <a href="https://www.linkedin.com/comm/jobs/view/1111111111/?trk=company_logo_1111111111">ANZ logo</a>
          <p>ANZ · Auckland, Auckland, New Zealand</p>
        </td>
      </tr>
      <tr>
        <td>
          <a href="https://www.linkedin.com/comm/jobs/view/2222222222/?trk=jobcard_body_2222222222">Senior Data Engineer</a>
          <p>ANZ · Auckland, New Zealand</p>
        </td>
      </tr>
      <tr>
        <td>
          <a href="https://www.linkedin.com/comm/jobs/view/3333333333/?trk=jobcard_body_3333333333">Data Engineer - Databricks</a>
          <p>EY · Auckland, New Zealand</p>
        </td>
      </tr>
    </table>
  </body>
</html>
"""


class LinkedInEmailParsingTests(unittest.TestCase):
    def test_linkedin_alert_created_email_imports_job_cards(self) -> None:
        parsed = parse_email_html_jobs(LINKEDIN_HTML, subject="Your job alert has been created", message_id="li-1")
        self.assertEqual(3, len(parsed.jobs))
        self.assertEqual(3, parsed.card_counts["linkedin_jobcard_body_links_found"])
        self.assertEqual(["1111111111", "2222222222", "3333333333"], [job["source_job_id"] for job in parsed.jobs])

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            root = Path(temp_dir)
            data_dir = root / "data"
            applications_dir = root / "applications"
            data_dir.mkdir(parents=True, exist_ok=True)
            applications_dir.mkdir(parents=True, exist_ok=True)
            for file_name in ["profile.md", "base_cv.txt", "personal_details.json"]:
                source = PROJECT_ROOT / "data" / file_name
                if source.exists():
                    shutil.copy2(source, data_dir / file_name)

            database = Database(data_dir / "job_assistant.db")
            importer = JobImporter(database)
            generator = DocumentGenerator(database, root)
            ingestor = EmailAlertIngestor(database, importer, generator, data_dir)

            message = EmailMessage()
            message["Subject"] = "Your job alert has been created"
            message["From"] = "jobs-noreply@linkedin.com"
            message["To"] = "candidate@example.com"
            message["Date"] = "Wed, 07 May 2026 10:00:00 +1200"
            message.set_content("Your job alert has been created. See HTML for jobs.")
            message.add_alternative(LINKEDIN_HTML, subtype="html")

            result = ingestor.ingest_uploaded_file("linkedin-alert.eml", message.as_bytes())
            self.assertEqual(3, result["created"])

            jobs = database.search_jobs(status="All", min_fit_score=0)
            titles = set(jobs["title"].tolist())
            self.assertIn("Intermediate Data Engineer", titles)
            self.assertIn("Senior Data Engineer", titles)
            self.assertIn("Data Engineer - Databricks", titles)


if __name__ == "__main__":
    unittest.main()
