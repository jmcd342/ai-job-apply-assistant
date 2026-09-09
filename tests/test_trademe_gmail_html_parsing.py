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


TRADEME_HTML = """
<html>
  <body>
    <h1>Trade Me Jobs</h1>
    <table>
      <tr>
        <td>
          <a class="responsiveTitle" href="https://www.trademe.co.nz/5923014090?utm_source=email"><b>Construction Labourer needed in ANTARCTICA!</b></a>
          <div>Hays</div>
          <div>Auckland City</div>
          <div>If you are looking to elevate your career with a busy delivery team, this role offers hands-on reporting and operations support.</div>
        </td>
      </tr>
      <tr>
        <td>
          <a class="responsiveTitle" href="https://www.trademe.co.nz/5923014091?utm_source=email"><b>Data Reporting Coordinator</b></a>
          <div>Insight Partners</div>
          <div>Christchurch City</div>
          <div>Support reporting uplift and internal analytics delivery.</div>
        </td>
      </tr>
    </table>
  </body>
</html>
"""


NO_CARDS_HTML = """
<html>
  <body>
    <h1>Trade Me Jobs</h1>
    <p>Your saved search has been updated.</p>
    <p>Visit Trade Me Jobs to see the latest listings.</p>
  </body>
</html>
"""


class FakeExecute:
    def __init__(self, payload):
        self.payload = payload

    def execute(self):
        return self.payload


class FakeMessages:
    def __init__(self, html_body: str, message_id: str = "trademe-msg-1"):
        self.html_body = html_body
        self.message_id = message_id
        self.modified = []

    def list(self, **kwargs):
        return FakeExecute({"messages": [{"id": self.message_id}]})

    def get(self, **kwargs):
        return FakeExecute(
            {
                "id": self.message_id,
                "internalDate": _current_internal_date_ms(),
                "payload": {
                    "headers": [
                        {"name": "Subject", "value": "Trade Me Jobs alert"},
                        {"name": "From", "value": "site@site.trademe.co.nz"},
                    ],
                    "mimeType": "multipart/alternative",
                    "parts": [
                        {
                            "mimeType": "text/plain",
                            "body": {"data": _encode_body("Trade Me Jobs alert. See HTML for job cards.")},
                        },
                        {
                            "mimeType": "text/html",
                            "body": {"data": _encode_body(self.html_body)},
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
    def __init__(self, html_body: str):
        self._messages = FakeMessages(html_body)
        self._labels = FakeLabels()

    def messages(self):
        return self._messages

    def labels(self):
        return self._labels


class FakeService:
    def __init__(self, html_body: str):
        self._users = FakeUsers(html_body)

    def users(self):
        return self._users


class TestTradeMeGmailIngestor(GmailIngestor):
    def __init__(self, *args, html_body: str, **kwargs):
        super().__init__(*args, **kwargs)
        self.fake_service = FakeService(html_body)

    def _build_service(self, interactive: bool):
        return self.fake_service

    def _load_credentials(self, interactive: bool):
        class Creds:
            valid = True

        return Creds()


class TradeMeGmailHtmlParsingTests(unittest.TestCase):
    def _build_ingestor(self, temp_dir: str, html_body: str) -> tuple[Database, TestTradeMeGmailIngestor]:
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
        ingestor = TestTradeMeGmailIngestor(database, importer, generator, root, html_body=html_body)
        return database, ingestor

    def test_trademe_html_cards_trust_saved_search_location_and_generate_drafts(self) -> None:
        parsed = parse_email_html_jobs(TRADEME_HTML, subject="Trade Me Jobs alert", message_id="tm-1")
        self.assertEqual(2, len(parsed.jobs))
        self.assertEqual(2, parsed.card_counts["trademe_responsiveTitle_links_found"])
        self.assertEqual(["5923014090", "5923014091"], [job["source_job_id"] for job in parsed.jobs])

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            database, ingestor = self._build_ingestor(temp_dir, TRADEME_HTML)

            result = ingestor.sync_messages(interactive_auth=False)
            jobs = database.search_jobs(status="All", min_fit_score=0)

            self.assertEqual(1, result["emails_processed"])
            self.assertEqual(2, result["created"])
            self.assertEqual(0, result["filtered_out"])
            self.assertEqual(2, result["auto_drafted"])
            self.assertEqual(2, len(jobs))
            self.assertIn("Construction Labourer needed in ANTARCTICA!", jobs["title"].tolist())
            self.assertIn("Data Reporting Coordinator", jobs["title"].tolist())
            for _, row in jobs.iterrows():
                self.assertEqual("trademe", row["source"])
                self.assertTrue(str(row["url"]).startswith("https://www.trademe.co.nz/592301409"))
                self.assertNotIn("utm_source", str(row["url"]))

            docs = database.list_generated_documents(int(jobs.iloc[0]["job_id"]))
            self.assertTrue(any(doc["doc_type"] == "tailored_cv_preview" for doc in docs))

    def test_trademe_email_with_no_cards_is_not_marked_processed(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            database, ingestor = self._build_ingestor(temp_dir, NO_CARDS_HTML)

            result = ingestor.sync_messages(interactive_auth=False)
            processed_rows = database.fetch_all("SELECT * FROM processed_email_messages")
            log_rows = database.fetch_all(
                "SELECT action, details FROM run_logs WHERE run_type = ? ORDER BY id ASC",
                ("gmail_ingestion",),
            )

            self.assertEqual(0, result["emails_processed"])
            self.assertEqual(0, result["created"])
            self.assertEqual(0, len(processed_rows))
            self.assertEqual([], ingestor.fake_service.users().messages().modified)
            self.assertTrue(any(row["action"] == "no_trademe_job_cards_found" for row in log_rows))


if __name__ == "__main__":
    unittest.main()
