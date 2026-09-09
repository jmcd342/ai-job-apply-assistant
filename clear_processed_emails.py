from __future__ import annotations

from pathlib import Path

from src.database import Database

APP_ROOT = Path(__file__).resolve().parent
DATA_DIR = APP_ROOT / "data"
DB_PATH = DATA_DIR / "job_assistant.db"


def main() -> None:
    confirmation = input("This will delete processed email tracking only. Type CLEAR to continue: ").strip()
    if confirmation != "CLEAR":
        print("Clear cancelled.")
        return

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    database = Database(DB_PATH)
    database.clear_processed_email_messages()
    database.log_run(
        "clear_processed_emails",
        "clear",
        "success",
        "Cleared processed email tracking only.",
    )
    print("Processed email tracking cleared. Jobs, documents, settings, and Gmail auth files were preserved.")


if __name__ == "__main__":
    main()
