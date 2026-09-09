from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
import time

import pandas as pd

from src.application_tracker import ApplicationTracker
from src.daily_application_runner import (
    DailyApplicationResult,
    application_outcome_messages,
    run_daily_application_cycle,
)
from src.database import Database
from src.document_generator import DocumentGenerator
from src.email_alert_ingestor import EmailAlertIngestor
from src.gmail_ingestor import DEFAULT_GMAIL_LOOKBACK_DAYS, GmailIngestor
from src.job_enricher import JobEnricher
from src.job_importer import JobImporter

APP_ROOT = Path(__file__).resolve().parent
DATA_DIR = APP_ROOT / "data"
DB_PATH = DATA_DIR / "job_assistant.db"
DAILY_IMPORT_PATH = DATA_DIR / "daily_jobs.csv"
PAUSE_FLAG_PATH = DATA_DIR / "pause_daily_run.flag"


def _timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _log(message: str) -> None:
    print(f"[{_timestamp()}] {message}", flush=True)


def _format_elapsed(seconds: float) -> str:
    total_seconds = max(0, int(round(seconds)))
    minutes, seconds = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m {seconds}s"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


def _timing_suffix(payload: dict, state: dict[str, float]) -> str:
    started_at = float(state.get("started_at", 0.0) or 0.0)
    elapsed = max(0.0, time.monotonic() - started_at) if started_at else 0.0
    index = int(payload.get("index", 0) or 0)
    total = int(payload.get("total", 0) or 0)
    suffix = f" | elapsed={_format_elapsed(elapsed)}"
    if index > 0 and total > 0:
        average_per_email = elapsed / index
        remaining = max(0, total - index)
        suffix += f" | eta={_format_elapsed(average_per_email * remaining)}"
    return suffix


def _count_applications_submitted_on(database: Database, day: str) -> int:
    row = database.fetch_one(
        """
        SELECT COUNT(DISTINCT jobs.id) AS count
        FROM jobs
        LEFT JOIN applications ON applications.job_id = jobs.id
        WHERE COALESCE(NULLIF(trim(jobs.application_status), ''), applications.status) IN ('applied', 'closed')
          AND substr(
              COALESCE(NULLIF(trim(jobs.applied_at), ''), applications.submitted_at),
              1,
              10
          ) = ?
        """,
        (day,),
    )
    return int(row["count"] if row else 0)


def _print_gmail_progress(payload: dict, *, verbose: bool = False, state: dict[str, float] | None = None) -> None:
    state = state or {}
    index = payload.get("index", "?")
    total = payload.get("total", "?")
    status = payload.get("status", "unknown")
    subject = str(payload.get("subject", "")).strip() or "(no subject)"
    source = str(payload.get("source", "")).strip()
    source_label = source.title() if source else "Unknown"

    if status == "starting_sync":
        mode = "unread only" if payload.get("unread_only", True) else "all unprocessed recent mail"
        _log(
            f"Starting Gmail sync. Max emails: {payload.get('max_results', '?')} | "
            f"Lookback: {payload.get('lookback_days', '?')} days | Mode: {mode}"
        )
        if payload.get("trust_saved_search_location", True):
            _log("TRUST_SAVED_SEARCH_LOCATION is enabled. Gmail alert locations will be trusted by default.")
        if verbose:
            _log(f"Gmail query: {payload.get('query', '')}")
    elif status == "messages_found":
        total_found = payload.get("total", 0)
        descriptor = "Unread matching emails" if payload.get("unread_only", True) else "Matching recent unprocessed emails"
        if total_found:
            _log(f"{descriptor} found: {total_found}")
        else:
            _log(f"No {descriptor.lower()} found.")
    elif status == "no_messages_found":
        descriptor = "unread recent job-alert emails" if payload.get("unread_only", True) else "recent unprocessed job-alert emails"
        _log(f"No {descriptor} found.")
    elif status == "processing_email":
        _log(f"[{index}/{total}] Processing {source_label} email: {subject}")
    elif status == "jobs_extracted":
        counts = payload.get("card_counts") or {}
        counts_text = ""
        if counts:
            counts_text = (
                f" seek_cards_found={counts.get('seek_cards_found', 0)}"
                f" linkedin_jobcard_body_links_found={counts.get('linkedin_jobcard_body_links_found', 0)}"
                f" trademe_responsiveTitle_links_found={counts.get('trademe_responsiveTitle_links_found', 0)}"
            )
        _log(
            f"[{index}/{total}] Source detected: {source_label}. "
            f"Jobs extracted: {payload.get('extracted_jobs_count', 0)}"
            f" seek_cards_parsed={payload.get('seek_cards_parsed', 0)}{counts_text}"
        )
    elif status == "missing_job_url":
        _log(
            f"[{index}/{total}] Warning: {payload.get('missing_url_count', 0)} extracted job card(s) were missing a job URL."
        )
    elif status == "documents_generated":
        if verbose:
            _log(f"[{index}/{total}] Generated documents for job {payload.get('job_id', '?')}.")
    elif status == "processed":
        imported_titles = payload.get("imported_job_titles") or []
        imported_titles_text = f" imported_job_titles={', '.join(imported_titles)}" if imported_titles else ""
        _log(
            f"[{index}/{total}] Completed: {subject} | "
            f"jobs_imported={payload.get('jobs_imported', payload.get('imported', 0))} "
            f"skipped={payload.get('filtered_out', 0)} "
            f"jobs_skipped_duplicate={payload.get('jobs_skipped_duplicate', payload.get('duplicates', 0))} "
            f"jobs_enriched={payload.get('jobs_enriched', 0)} "
            f"enrichment_failed={payload.get('enrichment_failed', 0)} "
            f"drafts_generated={payload.get('auto_drafted', 0)}"
            f"{imported_titles_text}"
            f"{_timing_suffix(payload, state)}"
        )
    elif status == "duplicate_email":
        _log(f"[{index}/{total}] Skipped duplicate email: {payload.get('message_id', '')}{_timing_suffix(payload, state)}")
    elif status == "skipped_old":
        _log(f"[{index}/{total}] Skipped old email: {subject}{_timing_suffix(payload, state)}")
    elif status in {"no_job_cards_found", "no_trademe_job_cards_found"}:
        _log(f"[{index}/{total}] Parser found no valid job cards.{_timing_suffix(payload, state)}")
    elif status == "failed":
        _log(
            f"[{index}/{total}] Failed: {payload.get('message_id', '')} | "
            f"{payload.get('error', 'Unknown error')}{_timing_suffix(payload, state)}"
        )
    elif status == "sync_complete" and verbose:
        summary = payload.get("summary", {})
        _log(
            "Gmail sync complete. "
            f"emails_processed={summary.get('emails_processed', 0)} "
            f"jobs_found_in_email={summary.get('jobs_found_in_email', 0)} "
            f"jobs_imported={summary.get('created', 0)} "
            f"duplicates_skipped={summary.get('duplicates', 0)} "
            f"non_auckland_filtered={summary.get('non_auckland_filtered', 0)} "
            f"drafts_generated={summary.get('auto_drafted', 0)} "
            f"jobs_enriched={summary.get('jobs_enriched', 0)} "
            f"enrichment_failed={summary.get('enrichment_failed', 0)} "
            f"failures={summary.get('failed', 0)}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the daily AI Job Apply Assistant ingestion and application workflow.")
    parser.add_argument("--reprocess", action="store_true", help="Ignore processed email tracking and re-parse matching unread Gmail emails.")
    parser.add_argument("--verbose", action="store_true", help="Print detailed live progress while the daily run executes.")
    parser.add_argument(
        "--prepare-only",
        action="store_true",
        help="Prepare application packets without submitting them. Daily runs submit by default.",
    )
    parser.add_argument(
        "--max-applications",
        type=int,
        default=10,
        help="Maximum SEEK applications to attempt during this run (default: 10).",
    )
    args = parser.parse_args()

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    _log(f"Resolved database path: {DB_PATH.resolve()}")
    database = Database(DB_PATH)
    tracker = ApplicationTracker(database, APP_ROOT)
    importer = JobImporter(database)
    generator = DocumentGenerator(database, APP_ROOT)
    enricher = JobEnricher(database, APP_ROOT)
    email_ingestor = EmailAlertIngestor(database, importer, generator, DATA_DIR)
    gmail_ingestor = GmailIngestor(database, importer, generator, APP_ROOT, job_enricher=enricher)

    _log("Starting daily run.")
    snapshot = database.get_jobs_debug_snapshot()
    latest_job = snapshot.get("latest_job") or {}
    _log(
        "Startup diagnostics: "
        f"db_path={snapshot['db_path']} "
        f"jobs_count={snapshot['total_jobs']} "
        f"latest_job_id={latest_job.get('id')} "
        f"latest_imported_at={latest_job.get('imported_at') or latest_job.get('created_at')}"
    )
    database.log_run("daily_run", "start", "success", f"Daily run started. Import file: {DAILY_IMPORT_PATH}")
    if PAUSE_FLAG_PATH.exists():
        database.log_run("daily_run", "pause_check", "skipped", "Daily run is paused by user flag.")
        _log("Daily run skipped because pause flag is enabled.")
        return

    gmail_progress_state = {"started_at": time.monotonic()}
    gmail_summary = gmail_ingestor.sync_messages(
        interactive_auth=False,
        unread_only=False,
        max_results=50,
        lookback_days=DEFAULT_GMAIL_LOOKBACK_DAYS,
        ignore_processed_tracking=args.reprocess,
        progress_callback=lambda payload: _print_gmail_progress(payload, verbose=args.verbose, state=gmail_progress_state),
    )
    database.log_run("daily_run", "gmail_import", "success", str(gmail_summary))
    _log(
        "Gmail summary: "
        f"emails processed={gmail_summary['emails_processed']}, "
        f"jobs found in email={gmail_summary['jobs_found_in_email']}, "
        f"jobs imported={gmail_summary['created']}, "
        f"duplicates skipped={gmail_summary['duplicates']}, "
        f"non-Auckland filtered={gmail_summary['non_auckland_filtered']}, "
        f"jobs enriched={gmail_summary['jobs_enriched']}, "
        f"enrichment failed={gmail_summary['enrichment_failed']}, "
        f"drafts generated={gmail_summary['auto_drafted']}, "
        f"failures={gmail_summary['failed']}"
    )

    _log("Processing saved email-alert files from data/email_alerts.")
    email_summary = email_ingestor.ingest_pending_files(run_type="daily_run_email_alerts")
    database.log_run("daily_run", "email_alert_import", "success", str(email_summary))
    _log(
        "Email-alert folder summary: "
        f"files processed={email_summary['files_processed']}, "
        f"jobs imported={email_summary['created']}, "
        f"duplicates skipped={email_summary['duplicates']}, "
        f"non-Auckland filtered={email_summary['auckland_filtered_out']}, "
        f"drafts generated={email_summary['auto_drafted']}, "
        f"failures={email_summary['failed']}"
    )

    if DAILY_IMPORT_PATH.exists():
        _log(f"Importing CSV jobs from {DAILY_IMPORT_PATH}.")
        frame = pd.read_csv(DAILY_IMPORT_PATH)
        result = importer.import_csv(frame, run_type="daily_run")
        database.log_run("daily_run", "csv_import", "success", str(result))
        _log(
            "CSV import summary: "
            f"jobs imported={result['created_count']}, "
            f"duplicates skipped={result['duplicate_count']}, "
            f"non-Auckland filtered={result['filtered_out_count']}, "
            f"failures={len(result['errors'])}"
        )
    else:
        database.log_run(
            "daily_run",
            "csv_import",
            "skipped",
            f"No CSV found at {DAILY_IMPORT_PATH}. Manual imports remain supported in the MVP.",
        )
        _log(f"No CSV import file found at {DAILY_IMPORT_PATH}.")

    pending_enrichment = database.fetch_jobs_missing_enrichment()
    enrichment_summary = {"success": 0, "failed": 0, "skipped": 0}
    if pending_enrichment:
        _log(f"Enriching {len(pending_enrichment)} job(s) from source job pages.")
        enrichment_summary = enricher.enrich_jobs(pending_enrichment)
        _log(
            "Enrichment summary: "
            f"jobs_enriched={enrichment_summary['success']}, "
            f"enrichment_failed={enrichment_summary['failed']}, "
            f"skipped={enrichment_summary['skipped']}"
        )
        database.log_run("daily_run", "enrichment", "success", str(enrichment_summary))
    else:
        _log("No jobs currently need enrichment.")

    candidates = tracker.jobs_for_daily_drafting()
    if candidates:
        _log(f"Preparing draft packages for {len(candidates)} job(s).")
    else:
        _log("No additional jobs need draft generation.")

    drafted = 0
    for job_id in candidates:
        details = tracker.get_job_details(job_id) or {}
        title = details.get("title", f"Job {job_id}")
        _log(f"Generating documents for {title} (job_id={job_id}).")
        result = generator.generate_for_job(job_id)
        drafted += 1
        docs = tracker.list_generated_documents(job_id)
        database.log_run("daily_run", "draft_application", "success", str(result))
        _log(f"Generated documents: {len(docs)} file(s) for {title}.")

    total_prepared = gmail_summary["auto_drafted"] + email_summary["auto_drafted"] + drafted
    application_result = DailyApplicationResult()
    if args.prepare_only:
        _log("Prepare-only mode enabled; no applications will be submitted.")
    else:
        _log(f"Starting SEEK application cycle (maximum {max(0, args.max_applications)} application attempts).")
        application_result = run_daily_application_cycle(
            database,
            generator,
            max_applications=max(0, args.max_applications),
        )
        _log(
            "SEEK application cycle completed. "
            f"prepared_in_seek={application_result.prepared}, "
            f"attempted={application_result.attempted}, "
            f"submitted={application_result.submitted}, "
            f"blocked={application_result.blocked}, "
            f"failed={application_result.failed}"
        )
    applications_submitted_by_daily_run = application_result.submitted
    applications_submitted_today = _count_applications_submitted_on(database, datetime.now().date().isoformat())
    outcome_message, action_required_message = application_outcome_messages(
        prepared=total_prepared,
        submitted_by_daily_run=applications_submitted_by_daily_run,
        submitted_today=applications_submitted_today,
    )
    database.log_run(
        "daily_run",
        "application_outcome",
        "warning" if total_prepared > applications_submitted_by_daily_run else "success",
        f"{outcome_message} {action_required_message}",
    )
    unresolved_applications = max(0, total_prepared - applications_submitted_by_daily_run)
    finish_status = "warning" if unresolved_applications or application_result.failed or application_result.blocked else "success"
    database.log_run(
        "daily_run",
        "finish",
        finish_status,
        (
            f"Daily run completed. Drafted {drafted} jobs. "
            f"Applications submitted {applications_submitted_by_daily_run}."
        ),
    )
    _log(
        "Daily run completed. "
        f"emails processed={gmail_summary['emails_processed']}, "
        f"jobs imported={gmail_summary['created'] + email_summary['created']}, "
        f"duplicates skipped={gmail_summary['duplicates'] + email_summary['duplicates']}, "
        f"non-Auckland filtered={gmail_summary['non_auckland_filtered'] + email_summary['auckland_filtered_out']}, "
        f"jobs enriched={gmail_summary['jobs_enriched'] + enrichment_summary['success']}, "
        f"enrichment failed={gmail_summary['enrichment_failed'] + enrichment_summary['failed']}, "
        f"drafts generated={total_prepared}, "
        f"failures={gmail_summary['failed'] + email_summary['failed']}"
    )
    _log(outcome_message)
    _log(action_required_message)


if __name__ == "__main__":
    main()
