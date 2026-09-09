from __future__ import annotations

import base64
import shutil
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.database import Database
from src.document_generator import DocumentGenerator
from src.gmail_ingestor import GmailIngestor, PROCESSED_LABEL_NAME
from src.job_importer import JobImporter


def _encode_body(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("utf-8").rstrip("=")


def _current_internal_date_ms() -> str:
    return str(int(datetime.now(timezone.utc).timestamp() * 1000))


class FakeExecute:
    def __init__(self, payload):
        self.payload = payload

    def execute(self):
        return self.payload


class FakeMessages:
    def __init__(self):
        self.modified = []

    def list(self, **kwargs):
        return FakeExecute({"messages": [{"id": "msg-1"}]})

    def get(self, **kwargs):
        return FakeExecute(
            {
                "id": "msg-1",
                "internalDate": _current_internal_date_ms(),
                "payload": {
                    "headers": [
                        {"name": "Subject", "value": "Job alert"},
                        {"name": "From", "value": "alerts@seek.co.nz"},
                    ],
                    "mimeType": "text/plain",
                    "body": {
                        "data": _encode_body(
                            "Data Analyst\nExample Co\nAuckland\n$100,000 annual\nSQL, Power BI, Python, stakeholder management\nhttps://www.seek.co.nz/job/abc123"
                        )
                    },
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


class TestGmailIngestor(GmailIngestor):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fake_service = FakeService()

    def _build_service(self, interactive: bool):
        return self.fake_service

    def _load_credentials(self, interactive: bool):
        class Creds:
            valid = True

        return Creds()


class GmailReprocessTests(unittest.TestCase):
    def test_reprocess_mode_reimports_after_reset_even_if_email_is_marked_processed(self) -> None:
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
            ingestor = TestGmailIngestor(database, importer, generator, root)

            first_result = ingestor.sync_messages(interactive_auth=False)
            self.assertEqual(1, first_result["created"])
            self.assertTrue(database.has_processed_email_message("gmail", "msg-1"))

            with closing(database.connect()) as connection:
                connection.execute("DELETE FROM generated_documents")
                connection.execute("DELETE FROM applications")
                connection.execute("DELETE FROM jobs")
                connection.commit()
            database.clear_processed_email_messages()
            database.record_processed_email_message(
                provider="gmail",
                message_id="msg-1",
                source="seek",
                imported_jobs=0,
                duplicate_jobs=0,
                filtered_jobs=0,
                status="success",
                details="stale processed marker for test",
            )

            second_result = ingestor.sync_messages(interactive_auth=False, ignore_processed_tracking=True)
            jobs = database.search_jobs(status="All", min_fit_score=0)

            self.assertEqual(1, second_result["emails_processed"])
            self.assertEqual(1, second_result["created"])
            self.assertEqual(0, second_result["duplicate_emails"])
            self.assertEqual(1, len(jobs))
            self.assertTrue(database.has_processed_email_message("gmail", "msg-1"))


if __name__ == "__main__":
    unittest.main()
