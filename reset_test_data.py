from __future__ import annotations

import shutil
from pathlib import Path

from src.database import Database

APP_ROOT = Path(__file__).resolve().parent
DATA_DIR = APP_ROOT / "data"
DB_PATH = DATA_DIR / "job_assistant.db"

GENERATED_FOLDERS = [
    DATA_DIR / "generated_documents",
    DATA_DIR / "application_packets",
]


def main() -> None:
    confirmation = input("This will clear jobs, documents, applications, logs, and processed email tracking. Type RESET to continue: ").strip()

    if confirmation != "RESET":
        print("Reset cancelled.")
        return

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    database = Database(DB_PATH)

    tables = [
        "documents",
        "applications",
        "jobs",
        "processed_email_messages",
        "run_logs",
    ]

    for table in tables:
        try:
            database.execute(f"DELETE FROM {table}")
            print(f"Cleared table: {table}")
        except Exception as exc:
            print(f"Skipped table {table}: {exc}")

    try:
        database.execute("VACUUM")
        print("Vacuumed database.")
    except Exception as exc:
        print(f"Skipped VACUUM: {exc}")

    for folder in GENERATED_FOLDERS:
        if folder.exists():
            shutil.rmtree(folder)
            print(f"Deleted folder: {folder}")

    print("Reset complete. CV variants, cv_profiles.json, Gmail auth, and settings were preserved.")


if __name__ == "__main__":
    main()