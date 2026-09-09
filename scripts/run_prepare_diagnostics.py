from __future__ import annotations

import argparse
import json
from collections import Counter
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
import sys

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.bulk_prepare_quick_apply import (  # noqa: E402
    BulkPrepareResult,
    PAUSED_BULK_STATUSES,
    DEFAULT_PER_JOB_TIMEOUT_SECONDS,
    find_quick_apply_jobs,
    run_bulk_quick_apply,
)
from src.database import Database  # noqa: E402
from src.document_generator import DocumentGenerator  # noqa: E402


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a headless diagnostic sample of Quick Apply jobs and write a summary report."
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        default=PROJECT_ROOT / "data" / "job_assistant.db",
        help="Path to the SQLite database.",
    )
    parser.add_argument(
        "--bucket",
        choices=["problems", "unprocessed", "mixed"],
        default="problems",
        help="Which queue bucket to sample from.",
    )
    parser.add_argument(
        "--sample-size",
        type=int,
        default=3,
        help="How many jobs to run.",
    )
    parser.add_argument(
        "--job-ids",
        nargs="*",
        type=int,
        default=None,
        help="Explicit job IDs to diagnose. If set, bucket sampling is skipped.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=DEFAULT_PER_JOB_TIMEOUT_SECONDS,
        help="Per-job timeout in seconds.",
    )
    parser.add_argument(
        "--pause-between-jobs-seconds",
        type=float,
        default=1.0,
        help="Pause between jobs in the sample.",
    )
    return parser.parse_args()


def _latest_apply_runs_by_job(db_path: Path) -> dict[int, dict[str, Any]]:
    runs_root = db_path.resolve().parent / "apply_runs"
    latest: dict[int, dict[str, Any]] = {}
    if not runs_root.exists():
        return latest
    for run_dir in runs_root.iterdir():
        if not run_dir.is_dir():
            continue
        checklist_path = run_dir / "final_review_checklist.json"
        if not checklist_path.exists():
            continue
        try:
            payload = json.loads(checklist_path.read_text(encoding="utf-8"))
            job_id = int(payload.get("job_id") or 0)
        except Exception:  # noqa: BLE001
            continue
        if job_id <= 0:
            continue
        current = latest.get(job_id)
        mtime = checklist_path.stat().st_mtime
        if current is None or mtime > float(current["mtime"]):
            latest[job_id] = {
                "run_dir": run_dir,
                "payload": payload,
                "mtime": mtime,
            }
    return latest


def _problem_candidates(database: Database, limit: int) -> list[dict[str, Any]]:
    jobs = database.search_jobs(status="All", source="seek", min_fit_score=0)
    if jobs.empty:
        return []
    latest_runs = _latest_apply_runs_by_job(database.db_path)
    candidates: list[dict[str, Any]] = []
    for _, row in jobs.iterrows():
        job_id = int(row["job_id"])
        run = latest_runs.get(job_id)
        if not run:
            continue
        payload = dict(run["payload"])
        if str(payload.get("status") or "").strip() == "ready_for_human_review":
            continue
        details = database.get_job_details(job_id)
        if not details:
            continue
        effective_status = str(details.get("application_status") or details.get("status") or "").strip().lower()
        if effective_status in {"applied", "ignored", "archived", "rejected"}:
            continue
        candidates.append(
            {
                "job_id": job_id,
                "title": str(details.get("title") or ""),
                "company": str(details.get("company") or ""),
                "bucket": "problems",
                "latest_status": str(payload.get("status") or ""),
                "latest_failure_reason": str(
                    payload.get("failure_reason") or payload.get("root_failure_reason") or ""
                ).strip(),
                "latest_run_dir": str(run["run_dir"]),
                "mtime": float(run["mtime"]),
            }
        )
    candidates.sort(key=lambda item: item["mtime"], reverse=True)
    return candidates[:limit]


def _unprocessed_candidates(database: Database, limit: int) -> list[dict[str, Any]]:
    jobs = find_quick_apply_jobs(database, max_jobs=limit)
    return [
        {
            "job_id": int(job["job_id"]),
            "title": str(job.get("title") or ""),
            "company": str(job.get("company") or ""),
            "bucket": "unprocessed",
            "latest_status": "",
            "latest_failure_reason": "",
            "latest_run_dir": "",
            "mtime": 0.0,
        }
        for job in jobs
    ]


def _explicit_candidates(database: Database, job_ids: list[int]) -> list[dict[str, Any]]:
    latest_runs = _latest_apply_runs_by_job(database.db_path)
    candidates: list[dict[str, Any]] = []
    for job_id in job_ids:
        details = database.get_job_details(int(job_id))
        if not details:
            continue
        latest = latest_runs.get(int(job_id))
        payload = dict(latest["payload"]) if latest else {}
        candidates.append(
            {
                "job_id": int(job_id),
                "title": str(details.get("title") or ""),
                "company": str(details.get("company") or ""),
                "bucket": "problems" if latest else "unprocessed",
                "latest_status": str(payload.get("status") or ""),
                "latest_failure_reason": str(
                    payload.get("failure_reason") or payload.get("root_failure_reason") or ""
                ).strip(),
                "latest_run_dir": str(latest["run_dir"]) if latest else "",
                "mtime": float(latest["mtime"]) if latest else 0.0,
            }
        )
    return candidates


def _sample_candidates(database: Database, *, bucket: str, sample_size: int, job_ids: list[int] | None) -> list[dict[str, Any]]:
    if job_ids:
        return _explicit_candidates(database, job_ids)
    if bucket == "problems":
        return _problem_candidates(database, sample_size)
    if bucket == "unprocessed":
        return _unprocessed_candidates(database, sample_size)
    mixed: list[dict[str, Any]] = []
    problem_take = max(1, sample_size // 2)
    unprocessed_take = max(1, sample_size - problem_take)
    mixed.extend(_problem_candidates(database, problem_take))
    problem_ids = {int(item["job_id"]) for item in mixed}
    for item in _unprocessed_candidates(database, sample_size * 2):
        if int(item["job_id"]) in problem_ids:
            continue
        mixed.append(item)
        if len(mixed) >= sample_size:
            break
    return mixed[:sample_size]


def _classify_reason(reason: str, status: str) -> str:
    lowered = f"{status}\n{reason}".lower()
    if status == "ready_for_human_review":
        return "success"
    if "seek verification" in lowered:
        return "seek_verification"
    if "seek login" in lowered or "login" in lowered:
        return "login"
    if "captcha" in lowered:
        return "captcha"
    if "quick apply" in lowered and "could not find" in lowered:
        return "quick_apply_button_missing"
    if "page.goto" in lowered or "redirect" in lowered:
        return "job_open_or_redirect_timeout"
    if "choose documents" in lowered or "resume" in lowered:
        return "resume_or_documents"
    if "questionnaire" in lowered or "role-requirements" in lowered or "employer questions" in lowered:
        return "questionnaire"
    if "timed out" in lowered:
        return "timeout_other"
    if "skipped" in lowered or "no longer advertised" in lowered:
        return "job_closed_or_skipped"
    return "other"


def _build_report_payload(
    *,
    selected_jobs: list[dict[str, Any]],
    results: list[BulkPrepareResult],
    started_at: str,
    finished_at: str,
    args: argparse.Namespace,
) -> dict[str, Any]:
    result_rows: list[dict[str, Any]] = []
    category_counts: Counter[str] = Counter()
    status_counts: Counter[str] = Counter()
    for result in results:
        category = _classify_reason(result.failure_reason or result.root_failure_reason or "", result.status)
        category_counts[category] += 1
        status_counts[result.status] += 1
        result_rows.append(
            {
                **asdict(result),
                "diagnostic_category": category,
            }
        )
    return {
        "started_at": started_at,
        "finished_at": finished_at,
        "bucket": args.bucket,
        "sample_size_requested": int(args.sample_size),
        "per_job_timeout_seconds": int(args.timeout_seconds),
        "selected_job_ids": [int(item["job_id"]) for item in selected_jobs],
        "selected_jobs": selected_jobs,
        "result_count": len(results),
        "status_counts": dict(status_counts),
        "category_counts": dict(category_counts),
        "results": result_rows,
    }


def _write_report(report_dir: Path, payload: dict[str, Any]) -> tuple[Path, Path]:
    report_dir.mkdir(parents=True, exist_ok=True)
    json_path = report_dir / "summary.json"
    markdown_path = report_dir / "report.md"
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    lines: list[str] = []
    lines.append("# Prepare Diagnostics Report")
    lines.append("")
    lines.append(f"- Started: `{payload['started_at']}`")
    lines.append(f"- Finished: `{payload['finished_at']}`")
    lines.append(f"- Bucket: `{payload['bucket']}`")
    lines.append(f"- Requested sample size: `{payload['sample_size_requested']}`")
    lines.append(f"- Attempted jobs: `{payload['result_count']}`")
    lines.append("")
    lines.append("## Status Counts")
    lines.append("")
    for key, value in sorted(dict(payload["status_counts"]).items()):
        lines.append(f"- `{key}`: {value}")
    lines.append("")
    lines.append("## Failure Categories")
    lines.append("")
    for key, value in sorted(dict(payload["category_counts"]).items()):
        lines.append(f"- `{key}`: {value}")
    lines.append("")
    lines.append("## Selected Jobs")
    lines.append("")
    for item in payload["selected_jobs"]:
        lines.append(
            f"- `{item['job_id']}` {item['title']} at {item['company']} "
            f"(`{item['bucket']}`)"
            + (f" - previous issue: {item['latest_failure_reason']}" if item.get("latest_failure_reason") else "")
        )
    lines.append("")
    lines.append("## Results")
    lines.append("")
    for item in payload["results"]:
        lines.append(
            f"- `{item['job_id']}` {item['title']} at {item['company']}: "
            f"`{item['status']}` / `{item['diagnostic_category']}`"
        )
        if item.get("failure_reason"):
            lines.append(f"  Reason: {item['failure_reason']}")
        if item.get("evidence_folder"):
            lines.append(f"  Evidence: `{item['evidence_folder']}`")
    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, markdown_path


def main() -> int:
    args = _parse_args()
    database = Database(args.db_path)
    document_generator = DocumentGenerator(database, PROJECT_ROOT)
    selected_jobs = _sample_candidates(
        database,
        bucket=args.bucket,
        sample_size=max(1, int(args.sample_size)),
        job_ids=list(args.job_ids or []),
    )
    if not selected_jobs:
        print("No matching jobs found for diagnostics.", flush=True)
        return 1

    selected_ids = [int(item["job_id"]) for item in selected_jobs]
    started_at = datetime.now().isoformat(timespec="seconds")
    database.log_run(
        "prepare_diagnostics",
        "started",
        "success",
        f"bucket={args.bucket} selected_job_ids={selected_ids}",
    )
    results = run_bulk_quick_apply(
        database,
        document_generator,
        max_jobs=len(selected_ids),
        dry_run=False,
        selected_job_ids=selected_ids,
        include_attempted=True,
        pause_between_jobs_seconds=max(0.0, float(args.pause_between_jobs_seconds)),
        per_job_timeout_seconds=max(30, int(args.timeout_seconds)),
    )
    finished_at = datetime.now().isoformat(timespec="seconds")

    report_dir = (
        args.db_path.resolve().parent
        / "diagnostic_runs"
        / datetime.now().strftime("%Y%m%d_%H%M%S_prepare_diagnostics")
    )
    payload = _build_report_payload(
        selected_jobs=selected_jobs,
        results=results,
        started_at=started_at,
        finished_at=finished_at,
        args=args,
    )
    json_path, markdown_path = _write_report(report_dir, payload)
    database.log_run(
        "prepare_diagnostics",
        "finished",
        "success",
        f"attempted={len(results)} report_dir={report_dir}",
    )

    print(f"Prepare diagnostics finished. Report: {markdown_path}", flush=True)
    print(json.dumps(payload["status_counts"], ensure_ascii=False), flush=True)
    print(json.dumps(payload["category_counts"], ensure_ascii=False), flush=True)
    print(f"JSON: {json_path}", flush=True)
    print(f"MD: {markdown_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
