from __future__ import annotations

import shutil
from pathlib import Path

from src.database import Database

APP_ROOT = Path(__file__).resolve().parent
DATA_DIR = APP_ROOT / "data"
APPLICATIONS_DIR = APP_ROOT / "applications"
DB_PATH = DATA_DIR / "job_assistant.db"


def main() -> None:
    confirmation = input("This will delete all jobs, applications, generated documents, and processed email tracking. Type RESET to continue: ").strip()
    if confirmation != "RESET":
        print("Reset cancelled.")
        return

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    APPLICATIONS_DIR.mkdir(parents=True, exist_ok=True)
    database = Database(DB_PATH)

    with database.connect() as connection:
        connection.execute("DELETE FROM generated_documents")
        connection.execute("DELETE FROM applications")
        connection.execute("DELETE FROM jobs")
        connection.commit()
    database.clear_processed_email_messages()

    for child in APPLICATIONS_DIR.iterdir():
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
        elif child.is_file():
            child.unlink(missing_ok=True)

    database.log_run("reset_jobs", "reset", "success", "Cleared jobs, applications, generated documents, and processed email tracking.")
    print("Reset complete. App settings and Gmail auth files were preserved.")


if __name__ == "__main__":
    main()
