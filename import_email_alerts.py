from __future__ import annotations

from pathlib import Path

from src.database import Database
from src.document_generator import DocumentGenerator
from src.email_alert_ingestor import EmailAlertIngestor
from src.job_importer import JobImporter

APP_ROOT = Path(__file__).resolve().parent
DATA_DIR = APP_ROOT / "data"
DB_PATH = DATA_DIR / "job_assistant.db"


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    database = Database(DB_PATH)
    importer = JobImporter(database)
    generator = DocumentGenerator(database, APP_ROOT)
    ingestor = EmailAlertIngestor(database, importer, generator, DATA_DIR)

    summary = ingestor.ingest_pending_files(run_type="email_alert_batch_import")
    print(
        "Email alert import complete. "
        f"Processed {summary['files_processed']} file(s), "
        f"imported {summary['created']} job(s), "
        f"skipped {summary['duplicates']} duplicate(s), "
        f"filtered out {summary['auckland_filtered_out']} non-Auckland job(s), "
        f"failed {summary['failed']} item(s), "
        f"auto-drafted {summary['auto_drafted']} job(s), "
        f"file failures {summary['files_failed']}."
    )


if __name__ == "__main__":
    main()
