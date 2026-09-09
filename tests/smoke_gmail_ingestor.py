from __future__ import annotations

import base64
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.database import Database
from src.document_generator import DocumentGenerator
from src.gmail_ingestor import GmailIngestor, PROCESSED_LABEL_NAME
from src.job_importer import JobImporter


def _encode_body(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("utf-8").rstrip("=")


class FakeExecute:
    def __init__(self, payload):
        self.payload = payload

    def execute(self):
        return self.payload


class FakeMessages:
    def __init__(self, store):
        self.store = store
        self.modified = []

    def list(self, **kwargs):
        return FakeExecute({"messages": [{"id": "msg-1"}, {"id": "msg-1"}]})

    def get(self, **kwargs):
        return FakeExecute(
            {
                "id": "msg-1",
                "internalDate": "1778102400000",
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
        self.labels = [{"id": "LBL_JOBS", "name": "Jobs"}, {"id": "LBL_DONE", "name": PROCESSED_LABEL_NAME}]

    def list(self, **kwargs):
        return FakeExecute({"labels": self.labels})

    def create(self, **kwargs):
        return FakeExecute({"id": "LBL_DONE"})


class FakeUsers:
    def __init__(self, store):
        self._messages = FakeMessages(store)
        self._labels = FakeLabels()

    def messages(self):
        return self._messages

    def labels(self):
        return self._labels


class FakeService:
    def __init__(self):
        self.store = {}
        self._users = FakeUsers(self.store)

    def users(self):
        return self._users


class TestGmailIngestor(GmailIngestor):
    def __init__(self, *args, fake_service=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fake_service = fake_service or FakeService()

    def _build_service(self, interactive: bool):
        return self.fake_service

    def _load_credentials(self, interactive: bool):
        class Creds:
            valid = True

        return Creds()


def main() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
        root = Path(temp_dir)
        (root / "data").mkdir(parents=True, exist_ok=True)
        (root / "applications").mkdir(parents=True, exist_ok=True)
        (root / "data" / "templates").mkdir(parents=True, exist_ok=True)
        (root / "data" / "base_cv.txt").write_text(
            "SQL\nPower BI\nPython\nStakeholder management\nAuckland analytics delivery",
            encoding="utf-8",
        )
        (root / "data" / "profile.md").write_text(
            "Data analyst profile with SQL, Power BI, Python, and stakeholder management.",
            encoding="utf-8",
        )
        template_source = Path(__file__).resolve().parents[1] / "data" / "templates" / "master_cover_letter.txt"
        if template_source.exists():
            (root / "data" / "templates" / "master_cover_letter.txt").write_text(
                template_source.read_text(encoding="utf-8"),
                encoding="utf-8",
            )
        personal_details_source = Path(__file__).resolve().parents[1] / "data" / "personal_details.json"
        if personal_details_source.exists():
            (root / "data" / "personal_details.json").write_text(
                personal_details_source.read_text(encoding="utf-8"),
                encoding="utf-8",
            )

        database = Database(root / "data" / "job_assistant.db")
        importer = JobImporter(database)
        generator = DocumentGenerator(database, root)
        ingestor = TestGmailIngestor(database, importer, generator, root)

        result = ingestor.sync_messages(interactive_auth=False)
        documents = database.list_generated_documents(1)

        assert result["emails_processed"] == 1
        assert result["created"] == 1
        assert result["duplicate_emails"] == 1
        assert any(doc["doc_type"] == "application_rationale" for doc in documents)
        assert database.has_processed_email_message("gmail", "msg-1")
        print("gmail smoke test passed")


if __name__ == "__main__":
    main()
