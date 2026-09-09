from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
import time

from src.database import Database
from src.document_generator import DocumentGenerator
from src.gmail_ingestor import DEFAULT_GMAIL_LOOKBACK_DAYS, GmailIngestor
from src.job_enricher import JobEnricher
from src.job_importer import JobImporter

APP_ROOT = Path(__file__).resolve().parent
DATA_DIR = APP_ROOT / "data"
DB_PATH = DATA_DIR / "job_assistant.db"


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


def _print_progress(payload: dict, *, verbose: bool = False, state: dict[str, float] | None = None) -> None:
    state = state or {}
    index = payload.get("index", "?")
    total = payload.get("total", "?")
    status = payload.get("status", "unknown")
    subject = payload.get("subject", "").strip() or "(no subject)"
    source = str(payload.get("source", "")).strip()
    source_label = source.title() if source else "Unknown"
    if status == "starting_sync":
        mode = "unread only" if payload.get("unread_only", True) else "all unprocessed recent mail"
        _log(
            f"Starting Gmail sync. Max emails: {payload.get('max_results', '?')} | "
            f"Lookback: {payload.get('lookback_days', '?')} days | Mode: {mode}"
        )
        if payload.get("ignore_processed_tracking"):
            _log("Reprocess mode enabled.")
            _log("Ignoring processed email tracking.")
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
        if verbose:
            _log(f"[{index}/{total}] Sender: {payload.get('sender', '(unknown sender)')}")
    elif status == "reprocessing_email" and verbose:
        _log(f"[{index}/{total}] Reprocessing email despite existing processed-email history: {payload.get('message_id', '')}")
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
            f"Jobs extracted: {payload.get('extracted_jobs_count', 0)}{counts_text}"
        )
    elif status == "missing_job_url":
        _log(
            f"[{index}/{total}] Warning: {payload.get('missing_url_count', 0)} extracted job card(s) were missing a job URL."
        )
    elif status == "documents_generated":
        if verbose:
            _log(f"[{index}/{total}] Generated documents for job {payload.get('job_id', '?')}.")
    elif status == "processed":
        _log(
            f"[{index}/{total}] Completed: {subject} | "
            f"imported={payload.get('imported', 0)} duplicates={payload.get('duplicates', 0)} "
            f"non_auckland_filtered={payload.get('filtered_out', 0)} "
            f"jobs_enriched={payload.get('jobs_enriched', 0)} "
            f"enrichment_failed={payload.get('enrichment_failed', 0)} "
            f"drafts_generated={payload.get('auto_drafted', 0)}"
            f"{_timing_suffix(payload, state)}"
        )
    elif status == "duplicate_email":
        _log(f"[{index}/{total}] Skipped duplicate email: {payload.get('message_id', '')}{_timing_suffix(payload, state)}")
    elif status == "skipped_old":
        _log(f"[{index}/{total}] Skipped old email: {subject}{_timing_suffix(payload, state)}")
    elif status in {"no_job_cards_found", "no_trademe_job_cards_found"}:
        _log(f"[{index}/{total}] Parser found no valid job cards. Subject: {subject}{_timing_suffix(payload, state)}")
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
    parser = argparse.ArgumentParser(description="Sync Gmail job alerts into the AI Job Apply Assistant.")
    parser.add_argument("--max-results", type=int, default=10, help="Maximum number of matching unread Gmail messages to process. Default: 10")
    parser.add_argument("--reprocess", action="store_true", help="Ignore processed email tracking and parse matching unread emails again.")
    parser.add_argument("--include-read", action="store_true", help="Include already-read emails and rely on processed-email tracking instead of unread status.")
    parser.add_argument("--lookback-days", type=int, default=DEFAULT_GMAIL_LOOKBACK_DAYS, help=f"How many recent days of Gmail job alerts to scan. Default: {DEFAULT_GMAIL_LOOKBACK_DAYS}")
    parser.add_argument("--verbose", action="store_true", help="Print detailed live sync progress.")
    args = parser.parse_args()

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    database = Database(DB_PATH)
    importer = JobImporter(database)
    generator = DocumentGenerator(database, APP_ROOT)
    enricher = JobEnricher(database, APP_ROOT)
    ingestor = GmailIngestor(database, importer, generator, APP_ROOT, job_enricher=enricher)
    progress_state = {"started_at": time.monotonic()}
    _log("Starting standalone Gmail ingestion.")
    result = ingestor.sync_messages(
        interactive_auth=True,
        max_results=args.max_results,
        unread_only=not args.include_read,
        lookback_days=args.lookback_days,
        ignore_processed_tracking=args.reprocess,
        progress_callback=lambda payload: _print_progress(payload, verbose=args.verbose, state=progress_state),
    )
    _log(
        "Final summary: "
        f"emails processed={result['emails_processed']}, "
        f"jobs found in email={result['jobs_found_in_email']}, "
        f"jobs imported={result['created']}, "
        f"duplicates skipped={result['duplicates']}, "
        f"non-Auckland filtered={result['non_auckland_filtered']}, "
        f"drafts generated={result['auto_drafted']}, "
        f"jobs enriched={result['jobs_enriched']}, "
        f"enrichment failed={result['enrichment_failed']}, "
        f"failures={result['failed']}"
    )


if __name__ == "__main__":
    main()
