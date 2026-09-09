from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from src.auto_apply_rules import auto_apply_block_reason, is_auto_apply_blocked


BLOCKING_SUBMIT_STATUSES = {
    "browser_profile_locked",
    "failed",
    "paused_for_seek_login",
    "paused_for_seek_verification",
    "paused_for_captcha",
    "paused_for_manual_questionnaire",
    "paused_for_manual_resume_upload",
}


@dataclass
class DailyApplicationResult:
    prepared: int = 0
    attempted: int = 0
    submitted: int = 0
    blocked: int = 0
    failed: int = 0
    stopped_early: bool = False
    results: list[dict[str, str]] = field(default_factory=list)


def application_outcome_messages(
    *,
    prepared: int,
    submitted_by_daily_run: int,
    submitted_today: int,
) -> tuple[str, str]:
    prepared_count = max(0, int(prepared))
    submitted_by_run_count = max(0, int(submitted_by_daily_run))
    submitted_today_count = max(0, int(submitted_today))
    outcome = (
        "APPLICATION OUTCOME: "
        f"applications_submitted_by_daily_run={submitted_by_run_count} "
        f"applications_submitted_today={submitted_today_count} "
        f"packages_prepared={prepared_count}"
    )
    awaiting_submission = max(0, prepared_count - submitted_by_run_count)
    if awaiting_submission:
        action = (
            "ACTION REQUIRED: "
            f"{awaiting_submission} prepared application(s) were NOT submitted. "
            "Open the dashboard: process Unprocessed jobs, submit jobs in Ready to submit, "
            "and resolve anything in Problems."
        )
    else:
        action = "ACTION REQUIRED: none for applications prepared during this run."
    return outcome, action


def _effective_status(job: dict[str, Any]) -> str:
    return str(job.get("application_status") or job.get("status") or "").strip().lower()


def _is_quick_apply_job(job: dict[str, Any]) -> bool:
    return str(job.get("apply_type") or "").strip().lower() == "quick apply" or bool(
        job.get("has_quick_apply_button")
    )


def _review_url_from_run_dir(run_dir: Path) -> str:
    log_path = run_dir / "apply_run_log.txt"
    try:
        lines = log_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    for line in reversed(lines):
        if line.startswith("final_url="):
            return line.partition("=")[2].strip()
    return ""


def _latest_review_urls(database: Any, job_ids: set[int] | None = None) -> dict[int, str]:
    runs_root = Path(database.db_path).resolve().parent / "apply_runs"
    if not runs_root.exists():
        return {}
    wanted = {int(job_id) for job_id in (job_ids or set()) if int(job_id) > 0}
    latest: dict[int, tuple[float, str]] = {}
    try:
        run_dirs = sorted(
            (path for path in runs_root.iterdir() if path.is_dir()),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return {}
    for run_dir in run_dirs:
        checklist_path = run_dir / "final_review_checklist.json"
        try:
            payload = json.loads(checklist_path.read_text(encoding="utf-8"))
            job_id = int(payload.get("job_id") or 0)
            status = str(payload.get("status") or "")
            mtime = checklist_path.stat().st_mtime
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            continue
        if wanted and job_id not in wanted:
            continue
        if job_id <= 0 or status not in {"ready_for_human_review", "prepared_for_manual_review"}:
            continue
        review_url = _review_url_from_run_dir(checklist_path.parent)
        if not review_url:
            continue
        previous = latest.get(job_id)
        if previous is None or mtime > previous[0]:
            latest[job_id] = (mtime, review_url)
        if wanted and wanted.issubset(latest):
            break
    return {job_id: value[1] for job_id, value in latest.items()}


def _ready_jobs(database: Any, *, limit: int) -> list[dict[str, Any]]:
    if limit <= 0:
        return []
    jobs = database.search_jobs(status="All", source="seek", min_fit_score=0)
    if jobs.empty:
        return []
    rows: list[dict[str, Any]] = []
    for _, row in jobs.iterrows():
        job_id = int(row["job_id"])
        details = dict(database.get_job_details(job_id) or row.to_dict())
        status = _effective_status(details)
        if status != "ready_to_apply" or status in {"applied", "closed", "ignored", "archived", "rejected"}:
            continue
        if not _is_quick_apply_job(details):
            continue
        rows.append(details)
    review_urls = _latest_review_urls(
        database,
        {int(job.get("job_id") or 0) for job in rows},
    )
    for details in rows:
        details["review_url"] = review_urls.get(int(details.get("job_id") or 0), "")
    rows.sort(
        key=lambda job: (
            str(job.get("ready_to_apply_at") or ""),
            str(job.get("imported_at") or ""),
            int(job.get("job_id") or 0),
        ),
        reverse=True,
    )
    return rows[:limit]


def _result_message(result: Any) -> str:
    return str(result.error or result.final_url or result.status or "").strip()


def run_daily_application_cycle(
    database: Any,
    document_generator: Any,
    *,
    max_applications: int = 10,
    submit_func: Callable[..., Any] | None = None,
    prepare_func: Callable[..., list[Any]] | None = None,
) -> DailyApplicationResult:
    """Submit ready SEEK jobs, then prepare and submit fresh Quick Apply jobs.

    A blocking browser/login/questionnaire result stops the cycle so one broken
    session cannot produce a cascade of misleading failures.
    """
    if submit_func is None:
        from src.seek_apply_assist import submit_seek_review

        submit_func = submit_seek_review
    if prepare_func is None:
        from src.bulk_prepare_quick_apply import run_bulk_quick_apply

        prepare_func = run_bulk_quick_apply

    summary = DailyApplicationResult()
    remaining = max(0, int(max_applications))

    def submit_job(job: dict[str, Any]) -> bool:
        nonlocal remaining
        job_id = int(job.get("job_id") or 0)
        title = str(job.get("title") or f"Job {job_id}")
        if is_auto_apply_blocked(job):
            message = auto_apply_block_reason(job)
            database.update_application_state(job_id, "reviewed", note=message)
            summary.blocked += 1
            summary.results.append(
                {"job_id": str(job_id), "title": title, "status": "auto_apply_blocked", "message": message}
            )
            return True

        summary.attempted += 1
        remaining -= 1
        result = submit_func(
            job_id,
            database.db_path,
            review_url=str(job.get("review_url") or job.get("url") or ""),
            visible=False,
            use_bulk_profile=False,
        )
        status = str(result.status or "").strip().lower()
        message = _result_message(result)
        summary.results.append(
            {"job_id": str(job_id), "title": title, "status": status, "message": message}
        )
        database.log_run(
            "daily_apply",
            "submit_result",
            "success" if status == "applied" else "warning",
            f"job_id={job_id} status={status} message={message}",
        )
        if status == "applied":
            summary.submitted += 1
            return True
        if status in BLOCKING_SUBMIT_STATUSES:
            summary.blocked += 1
            summary.stopped_early = True
            return False
        summary.failed += 1
        return True

    for job in _ready_jobs(database, limit=remaining):
        if remaining <= 0 or not submit_job(job):
            break

    if remaining > 0 and not summary.stopped_early:
        prepared_results = prepare_func(
            database,
            document_generator,
            max_jobs=remaining,
            dry_run=False,
        )
        summary.prepared = sum(1 for item in prepared_results if str(item.status) == "ready_for_human_review")
        for item in prepared_results:
            if remaining <= 0:
                break
            if str(item.status) != "ready_for_human_review":
                summary.failed += 1
                summary.results.append(
                    {
                        "job_id": str(item.job_id),
                        "title": str(item.title),
                        "status": str(item.status),
                        "message": str(item.failure_reason or "Preparation did not reach final review."),
                    }
                )
                if str(item.status) in BLOCKING_SUBMIT_STATUSES:
                    summary.blocked += 1
                    summary.stopped_early = True
                    break
                continue
            prepared_job = dict(database.get_job_details(int(item.job_id)) or {})
            prepared_job.setdefault("job_id", int(item.job_id))
            prepared_job.setdefault("title", str(item.title))
            prepared_job["review_url"] = str(
                _review_url_from_run_dir(Path(item.evidence_folder))
                or item.job_url
                or prepared_job.get("url")
                or ""
            )
            if not submit_job(prepared_job):
                break

    database.log_run(
        "daily_apply",
        "cycle_complete",
        "success" if summary.failed == 0 and not summary.stopped_early else "warning",
        (
            f"prepared={summary.prepared} attempted={summary.attempted} "
            f"submitted={summary.submitted} blocked={summary.blocked} "
            f"failed={summary.failed} stopped_early={summary.stopped_early}"
        ),
    )
    return summary
