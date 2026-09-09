from __future__ import annotations

import json
from pathlib import Path

from src.database import Database
from src.duplicate_checker import canonicalize_url
from src.fit_scorer import score_job
from src.text_cleaner import clean_job_description

APP_ROOT = Path(__file__).resolve().parent
DATA_DIR = APP_ROOT / "data"
DB_PATH = DATA_DIR / "job_assistant.db"


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    database = Database(DB_PATH)
    rows = database.fetch_all(
        """
        SELECT id, source, title, company, location, salary, description, url, metadata_json
             , date_found, email_received_at, age_in_days
        FROM jobs
        ORDER BY id
        """
    )

    updated = 0
    unchanged = 0
    for row in rows:
        metadata = _load_metadata(row["metadata_json"])
        raw_text = str(metadata.get("raw_imported_text") or row["description"] or "")
        cleaned = clean_job_description(
            raw_text,
            title=str(row["title"] or ""),
            company=str(row["company"] or ""),
            location=str(row["location"] or ""),
            source=str(row["source"] or ""),
            existing_url=str(row["url"] or ""),
        )
        metadata["raw_imported_text"] = raw_text
        metadata["clean_description_generated"] = cleaned["clean_description"] != str(raw_text).strip()
        email_received_at = str(metadata.get("email_received_at") or row["email_received_at"] or "").strip() or None
        age_in_days = _calculate_age_in_days(email_received_at=email_received_at, date_found=str(row["date_found"] or ""))
        metadata["email_received_at"] = email_received_at
        metadata["age_in_days"] = age_in_days

        new_url = cleaned["job_url"] or str(row["url"] or "")
        new_description = cleaned["clean_description"]
        current_description = str(row["description"] or "").strip()
        current_url = str(row["url"] or "").strip()

        scored = score_job(
            title=str(row["title"] or ""),
            location=str(row["location"] or ""),
            salary=str(row["salary"] or ""),
            description=new_description,
        )

        if (
            new_description != current_description
            or new_url != current_url
            or email_received_at != str(row["email_received_at"] or "").strip()
            or age_in_days != int(row["age_in_days"] or 0)
        ):
            database.update_job_content(
                int(row["id"]),
                description=new_description,
                url=new_url or None,
                canonical_url=canonicalize_url(new_url),
                metadata=metadata,
                email_received_at=email_received_at,
                age_in_days=age_in_days,
            )
            updated += 1
        else:
            unchanged += 1
        database.update_job_enrichment(int(row["id"]), scored.enrichment, scored.score)

    database.log_run("clean_existing_jobs", "clean_all", "success", f"Updated {updated} jobs; unchanged {unchanged}.")
    print(f"Cleaned {updated} job(s). Unchanged {unchanged} job(s).")


def _load_metadata(payload: str | None) -> dict:
    try:
        return json.loads(payload or "{}")
    except Exception:  # noqa: BLE001
        return {}


def _calculate_age_in_days(email_received_at: str | None, date_found: str) -> int:
    from datetime import date, datetime

    reference_value = email_received_at or date_found
    if not reference_value:
        return 0
    try:
        parsed = datetime.fromisoformat(reference_value.replace("Z", "+00:00"))
        reference_date = parsed.date()
    except ValueError:
        try:
            reference_date = date.fromisoformat(reference_value[:10])
        except ValueError:
            return 0
    return max(0, (date.today() - reference_date).days)


if __name__ == "__main__":
    main()
