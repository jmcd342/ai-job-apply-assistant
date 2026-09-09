from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.database import Database
from src.job_enricher import JobEnricher

TARGET_FIELDS = [
    "apply_type",
    "has_apply_button",
    "has_quick_apply_button",
    "apply_area_salary_text",
    "date_posted_text",
]


def log(message: str) -> None:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {message}", flush=True)


def is_missing_value(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    return False


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Re-run enrichment for existing SEEK jobs without resetting the database.")
    parser.add_argument("--limit", type=int, default=0, help="Maximum number of SEEK jobs to re-enrich.")
    parser.add_argument("--job-id", type=int, default=0, help="Only re-enrich a single job ID.")
    parser.add_argument(
        "--only-missing-fields",
        action="store_true",
        help="Only re-enrich SEEK jobs missing one or more target metadata fields.",
    )
    return parser


def load_seek_jobs(database: Database, *, job_id: int = 0) -> list[dict]:
    sql = """
        SELECT
            id,
            title,
            company,
            url,
            source,
            apply_type,
            has_apply_button,
            has_quick_apply_button,
            apply_area_salary_text,
            date_posted_text,
            enrichment_status,
            imported_at,
            updated_at
        FROM jobs
        WHERE lower(source) = 'seek'
    """
    params: list[object] = []
    if job_id:
        sql += " AND id = ?"
        params.append(job_id)
    sql += " ORDER BY COALESCE(imported_at, updated_at, id) DESC, id DESC"
    return [dict(row) for row in database.fetch_all(sql, tuple(params))]


def job_needs_reenrichment(job: dict) -> bool:
    return any(is_missing_value(job.get(field)) for field in TARGET_FIELDS)


def format_job_summary(job: dict) -> str:
    title = str(job.get("title") or "Untitled role")
    company = str(job.get("company") or "Unknown company")
    return f"job_id={job['id']} title={title} company={company}"


def main() -> int:
    parser = build_argument_parser()
    args = parser.parse_args()

    data_dir = PROJECT_ROOT / "data"
    db_path = data_dir / "job_assistant.db"
    if not db_path.exists():
        log(f"Database not found: {db_path}")
        return 1

    database = Database(db_path)
    enricher = JobEnricher(database, PROJECT_ROOT)

    jobs = load_seek_jobs(database, job_id=args.job_id)
    if args.only_missing_fields:
        jobs = [job for job in jobs if job_needs_reenrichment(job)]
    if args.limit and args.limit > 0:
        jobs = jobs[: args.limit]

    if not jobs:
        scope = f"job_id={args.job_id}" if args.job_id else "SEEK jobs"
        filter_text = " with missing target fields" if args.only_missing_fields else ""
        log(f"No {scope}{filter_text} found to re-enrich.")
        return 0

    log(f"Starting SEEK re-enrichment for {len(jobs)} job(s).")
    log("Existing generated cover letters/CVs will be preserved; this script updates enrichment fields only.")

    summary = {
        "attempted": 0,
        "success": 0,
        "failed": 0,
        "skipped": 0,
    }

    for index, job in enumerate(jobs, start=1):
        summary["attempted"] += 1
        missing_fields = [field for field in TARGET_FIELDS if is_missing_value(job.get(field))]
        log(
            f"[{index}/{len(jobs)}] Re-enriching {format_job_summary(job)} "
            f"missing_fields={', '.join(missing_fields) if missing_fields else 'none'}"
        )
        try:
            result = enricher.enrich_job(int(job["id"]), force=True)
        except Exception as exc:  # noqa: BLE001
            summary["failed"] += 1
            log(f"FAILED {format_job_summary(job)} error={exc}")
            continue

        status = str(result.get("status") or "failed")
        if status == "success":
            summary["success"] += 1
            refreshed = database.get_job_details(int(job["id"])) or {}
            updated_bits = [
                f"apply_type={refreshed.get('apply_type') or 'unknown'}",
                f"has_apply_button={refreshed.get('has_apply_button')}",
                f"has_quick_apply_button={refreshed.get('has_quick_apply_button')}",
                f"apply_area_salary_text={refreshed.get('apply_area_salary_text') or '(none)'}",
                f"date_posted_text={refreshed.get('date_posted_text') or '(none)'}",
            ]
            log(f"SUCCESS {format_job_summary(job)} {'; '.join(updated_bits)}")
        elif status == "skipped":
            summary["skipped"] += 1
            log(f"SKIPPED {format_job_summary(job)} reason={result.get('reason', 'unknown')}")
        else:
            summary["failed"] += 1
            log(f"FAILED {format_job_summary(job)} error={result.get('error', 'unknown')}")

    log(
        "Completed SEEK re-enrichment. "
        f"attempted={summary['attempted']} success={summary['success']} "
        f"skipped={summary['skipped']} failed={summary['failed']}"
    )
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
