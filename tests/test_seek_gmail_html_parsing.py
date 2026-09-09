from __future__ import annotations

import base64
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.database import Database
from src.document_generator import DocumentGenerator
from src.email_job_parser import parse_email_html_jobs
from src.gmail_ingestor import GmailIngestor, PROCESSED_LABEL_NAME
from src.job_importer import JobImporter


def _encode_body(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("utf-8").rstrip("=")


def _current_internal_date_ms() -> str:
    return str(int(datetime.now(timezone.utc).timestamp() * 1000))


SEEK_HTML = """
<html>
  <body>
    <h1>20 new jobs for data</h1>
    <table>
      <tr>
        <td>
          <a href="https://www.seek.co.nz/job/12345678?tracking=TMC-SAU-eDM-SharedJob-13248">Junior Data Scientist - Fleet Operations</a>
          <div>Halter</div>
          <div>Auckland CBD, Auckland</div>
        </td>
      </tr>
      <tr>
        <td>
          <a href="https://www.seek.co.nz/job/87654321?tracking=TMC-SAU-eDM-SharedJob-13249">Data Analyst</a>
          <div>Example Co</div>
          <div>Wellington, New Zealand</div>
        </td>
      </tr>
      <tr>
        <td>
          <a href="https://www.seek.co.nz/job/11223344?tracking=TMC-SAU-eDM-SharedJob-13250">Reporting Analyst</a>
          <div>Analytics Group</div>
          <div>Auckland, New Zealand</div>
        </td>
      </tr>
      <tr>
        <td>
          <a href="https://www.seek.co.nz/job/55667788?tracking=TMC-SAU-eDM-SharedJob-13251">Business Intelligence Analyst</a>
          <div>Global Data Co</div>
          <div>Sydney, Australia</div>
        </td>
      </tr>
    </table>
  </body>
</html>
"""

SEEK_TRACKING_HTML = """
<html>
  <body>
    <h1>20 new jobs for data</h1>
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
  </body>
</html>
"""


class FakeExecute:
    def __init__(self, payload):
        self.payload = payload

    def execute(self):
        return self.payload


class FakeMessages:
    def __init__(self):
        self.modified = []

    def list(self, **kwargs):
        return FakeExecute({"messages": [{"id": "seek-msg-1"}]})

    def get(self, **kwargs):
        return FakeExecute(
            {
                "id": "seek-msg-1",
                "internalDate": _current_internal_date_ms(),
                "payload": {
                    "headers": [
                        {"name": "Subject", "value": "20 new jobs for data"},
                        {"name": "From", "value": "jobmail@seek.co.nz"},
                    ],
                    "mimeType": "multipart/alternative",
                    "parts": [
                        {
                            "mimeType": "text/plain",
                            "body": {"data": _encode_body("SEEK alert. See HTML for job cards.")},
                        },
                        {
                            "mimeType": "text/html",
                            "body": {"data": _encode_body(SEEK_HTML)},
                        },
                    ],
                },
                "labelIds": ["UNREAD"],
            }
        )

    def modify(self, userId, id, body):
        self.modified.append({"id": id, "body": body})
        return FakeExecute({})


class FakeLabels:
    def __init__(self):
        self.labels = [{"id": "LBL_DONE", "name": PROCESSED_LABEL_NAME}]

    def list(self, **kwargs):
        return FakeExecute({"labels": self.labels})

    def create(self, **kwargs):
        return FakeExecute({"id": "LBL_DONE"})


class FakeUsers:
    def __init__(self):
        self._messages = FakeMessages()
        self._labels = FakeLabels()

    def messages(self):
        return self._messages

    def labels(self):
        return self._labels


class FakeService:
    def __init__(self):
        self._users = FakeUsers()

    def users(self):
        return self._users


class TestSeekGmailIngestor(GmailIngestor):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fake_service = FakeService()

    def _build_service(self, interactive: bool):
        return self.fake_service

    def _load_credentials(self, interactive: bool):
        class Creds:
            valid = True

        return Creds()


class SeekGmailHtmlParsingTests(unittest.TestCase):
    def test_seek_html_cards_trust_saved_search_location_but_still_skip_clearly_non_nz_jobs(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            root = Path(temp_dir)
            data_dir = root / "data"
            applications_dir = root / "applications"
            data_dir.mkdir(parents=True, exist_ok=True)
            applications_dir.mkdir(parents=True, exist_ok=True)
            (data_dir / "templates").mkdir(parents=True, exist_ok=True)
            for file_name in ["profile.md", "base_cv.txt", "personal_details.json"]:
                source = PROJECT_ROOT / "data" / file_name
                if source.exists():
                    shutil.copy2(source, data_dir / file_name)
            template_source = PROJECT_ROOT / "data" / "templates" / "master_cover_letter.txt"
            if template_source.exists():
                shutil.copy2(template_source, data_dir / "templates" / "master_cover_letter.txt")

            database = Database(data_dir / "job_assistant.db")
            importer = JobImporter(database)
            generator = DocumentGenerator(database, root)
            ingestor = TestSeekGmailIngestor(database, importer, generator, root)

            result = ingestor.sync_messages(interactive_auth=False)
            jobs = database.search_jobs(status="All", min_fit_score=0)

            self.assertEqual(1, result["emails_processed"])
            self.assertEqual(3, result["created"])
            self.assertEqual(1, result["filtered_out"])
            self.assertEqual(3, result["auto_drafted"])
            self.assertEqual(3, len(jobs))
            self.assertIn("Junior Data Scientist - Fleet Operations", jobs["title"].tolist())
            self.assertIn("Data Analyst", jobs["title"].tolist())
            self.assertIn("Reporting Analyst", jobs["title"].tolist())
            self.assertNotIn("Business Intelligence Analyst", jobs["title"].tolist())
            for _, row in jobs.iterrows():
                self.assertTrue(str(row["url"]).startswith("https://www.seek.co.nz/job/"))
                self.assertNotIn("tracking=", str(row["url"]))

            docs = database.list_generated_documents(1)
            self.assertTrue(any(doc["doc_type"] == "tailored_cv_preview" for doc in docs))

    def test_seek_tracking_card_anchors_are_parsed_as_distinct_jobs(self) -> None:
        parsed = parse_email_html_jobs(SEEK_TRACKING_HTML, subject="20 new jobs for data", message_id="seek-msg-2")
        self.assertEqual(2, len(parsed.jobs))
        self.assertEqual(
            ["Digital Data & Innovation Analyst", "Senior Data Analyst"],
            [job["title"] for job in parsed.jobs],
        )
        self.assertEqual(
            ["Barker Fruit Processors", "Insight Partners"],
            [job["company"] for job in parsed.jobs],
        )
        self.assertEqual(
            ["Geraldine, Canterbury", "Auckland, New Zealand"],
            [job["location"] for job in parsed.jobs],
        )
        self.assertTrue(all(job["url"].startswith("https://email.s.seek.co.nz/uni/ss/c/") for job in parsed.jobs))

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            root = Path(temp_dir)
            data_dir = root / "data"
            applications_dir = root / "applications"
            data_dir.mkdir(parents=True, exist_ok=True)
            applications_dir.mkdir(parents=True, exist_ok=True)
            (data_dir / "templates").mkdir(parents=True, exist_ok=True)
            for file_name in ["profile.md", "base_cv.txt", "personal_details.json"]:
                source = PROJECT_ROOT / "data" / file_name
                if source.exists():
                    shutil.copy2(source, data_dir / file_name)
            template_source = PROJECT_ROOT / "data" / "templates" / "master_cover_letter.txt"
            if template_source.exists():
                shutil.copy2(template_source, data_dir / "templates" / "master_cover_letter.txt")

            database = Database(data_dir / "job_assistant.db")
            importer = JobImporter(database)
            generator = DocumentGenerator(database, root)
            ingestor = TestSeekGmailIngestor(database, importer, generator, root)
            ingestor.fake_service._users._messages.get = lambda **kwargs: FakeExecute(
                {
                    "id": "seek-msg-2",
                    "internalDate": _current_internal_date_ms(),
                    "payload": {
                        "headers": [
                            {"name": "Subject", "value": "20 new jobs for data"},
                            {"name": "From", "value": "jobmail@seek.co.nz"},
                        ],
                        "mimeType": "multipart/alternative",
                        "parts": [
                            {
                                "mimeType": "text/plain",
                                "body": {"data": _encode_body("SEEK alert. See HTML for job cards.")},
                            },
                            {
                                "mimeType": "text/html",
                                "body": {"data": _encode_body(SEEK_TRACKING_HTML)},
                            },
                        ],
                    },
                    "labelIds": ["UNREAD"],
                }
            )

            result = ingestor.sync_messages(interactive_auth=False)
            jobs = database.search_jobs(status="All", min_fit_score=0)

            self.assertEqual(2, result["jobs_found_in_email"])
            self.assertEqual(2, result["created"])
            self.assertEqual(0, result["duplicates"])
            self.assertEqual(2, len(jobs))
            self.assertEqual(
                sorted(jobs["title"].tolist()),
                ["Digital Data & Innovation Analyst", "Senior Data Analyst"],
            )
            self.assertTrue(all(str(url).startswith("https://email.s.seek.co.nz/uni/ss/c/") for url in jobs["url"].tolist()))
            self.assertEqual(
                sorted(result["imported_job_titles"]),
                ["Digital Data & Innovation Analyst", "Senior Data Analyst"],
            )
            self.assertGreaterEqual(result["auto_drafted"], 2)


if __name__ == "__main__":
    unittest.main()
