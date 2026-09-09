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
from src.document_generator import DocumentGenerator
from src.gmail_ingestor import GmailIngestor
from src.job_importer import JobImporter


class GmailIngestorSettingsTests(unittest.TestCase):
    def test_query_defaults_to_unread_last_30_days(self) -> None:
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
            generator = DocumentGenerator(database, root)
            ingestor = GmailIngestor(database, importer, generator, root)
            query = ingestor._build_search_query(jobs_label_only=False)  # noqa: SLF001
            self.assertIn("is:unread", query)
            self.assertIn("newer_than:30d", query)


if __name__ == "__main__":
    unittest.main()
