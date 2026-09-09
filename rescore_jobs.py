from __future__ import annotations

from pathlib import Path

from src.database import Database
from src.fit_scorer import score_job

APP_ROOT = Path(__file__).resolve().parent
DATA_DIR = APP_ROOT / "data"
DB_PATH = DATA_DIR / "job_assistant.db"


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    database = Database(DB_PATH)
    rows = database.fetch_all("SELECT id, title, location, salary, description FROM jobs ORDER BY id")
    rescored = 0
    for row in rows:
        scored = score_job(
            title=str(row["title"] or ""),
            location=str(row["location"] or ""),
            salary=str(row["salary"] or ""),
            description=str(row["description"] or ""),
        )
        database.update_job_enrichment(int(row["id"]), scored.enrichment, scored.score)
        rescored += 1

    database.log_run("rescore_jobs", "rescore_all", "success", f"Rescored {rescored} jobs.")
    print(f"Rescored {rescored} job(s).")


if __name__ == "__main__":
    main()
