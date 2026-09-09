from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup

from src.auto_apply_rules import is_auto_apply_blocked
from src.database import Database
from src.document_generator import DocumentGenerator
from src.seek_apply_assist import (
    assist_seek_apply,
    close_visible_seek_profile_windows,
    refresh_seek_session_in_browser,
    validate_seek_session,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SINGLE_APPLY_SCRIPT = PROJECT_ROOT / "scripts" / "run_single_apply_assist.py"
VALIDATE_SEEK_SESSION_SCRIPT = PROJECT_ROOT / "scripts" / "run_validate_seek_session.py"
PAUSED_BULK_STATUSES = {
    "paused_for_seek_verification",
    "paused_for_captcha",
    "paused_for_seek_login",
    "paused_for_manual_resume_upload",
    "paused_for_manual_questionnaire",
}
DEFAULT_PER_JOB_TIMEOUT_SECONDS = 120
DEFAULT_SUBPROCESS_ENCODING = "utf-8"


def _windows_subprocess_kwargs() -> dict[str, Any]:
    if sys.platform != "win32":
        return {}
    create_no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = 0
    return {
        "creationflags": create_no_window,
        "startupinfo": startupinfo,
    }


def _subprocess_text_kwargs() -> dict[str, Any]:
    return {
        "text": True,
        "encoding": DEFAULT_SUBPROCESS_ENCODING,
        "errors": "replace",
    }


def _subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("PYTHONIOENCODING", DEFAULT_SUBPROCESS_ENCODING)
    env.setdefault("PYTHONUTF8", "1")
    return env


def validate_seek_session_subprocess(
    db_path: Path,
    *,
    use_bulk_profile: bool = False,
    visible: bool = False,
) -> dict[str, Any]:
    command = [
        sys.executable,
        str(VALIDATE_SEEK_SESSION_SCRIPT),
        "--db-path",
        str(Path(db_path)),
        "--visible",
        "true" if visible else "false",
    ]
    if use_bulk_profile:
        command.append("--bulk-profile")
    completed = subprocess.run(
        command,
        capture_output=True,
        env=_subprocess_env(),
        timeout=120,
        check=False,
        **_subprocess_text_kwargs(),
        **_windows_subprocess_kwargs(),
    )
    stdout_text = str(completed.stdout or "").strip()
    if not stdout_text:
        return {
            "ok": False,
            "state": "validation_failed",
            "reason": str(completed.stderr or "").strip() or "SEEK session validation subprocess produced no output.",
        }
    last_line = stdout_text.splitlines()[-1].strip()
    try:
        payload = json.loads(last_line)
    except Exception:  # noqa: BLE001
        return {
            "ok": False,
            "state": "validation_failed",
            "reason": last_line or "SEEK session validation subprocess returned invalid JSON.",
        }
    if not isinstance(payload, dict):
        return {
            "ok": False,
            "state": "validation_failed",
            "reason": "SEEK session validation subprocess returned an unexpected payload.",
        }
    return payload


@dataclass
class BulkPrepareResult:
    job_id: int
    title: str
    company: str
    status: str
    resume_included: bool
    cover_letter_included: bool
    questions_answered: bool
    failure_reason: str
    evidence_folder: str
    job_url: str = ""
    reached_review_page: bool = False
    resume_text_seen: str = ""
    cover_letter_text_seen: str = ""
    question_answer_count_text: str = ""
    no_validation_errors: bool = False
    final_submit_not_clicked: bool = True
    screenshot_path: str = ""
    html_path: str = ""
    root_failure_reason: str = ""
    evidence_capture_errors: list[str] | None = None
    screenshot_saved: bool = False
    html_saved: bool = False
    json_saved: bool = False
    subprocess_exit_code: int | None = None


def _attempted_job_ids(database: Database) -> set[int]:
    runs_root = Path(database.db_path).resolve().parent / "apply_runs"
    if not runs_root.exists():
        return set()
    attempted: set[int] = set()
    for checklist_path in runs_root.glob("*/final_review_checklist.json"):
        try:
            payload = json.loads(checklist_path.read_text(encoding="utf-8"))
            job_id = int(payload.get("job_id") or 0)
        except Exception:  # noqa: BLE001
            continue
        if job_id > 0:
            attempted.add(job_id)
    return attempted


def find_quick_apply_jobs(
    database: Database,
    max_jobs: int = 5,
    *,
    selected_job_ids: list[int] | None = None,
    include_attempted: bool = False,
) -> list[dict[str, Any]]:
    jobs = database.search_jobs(status="All", source="seek", min_fit_score=0)
    if jobs.empty:
        return []
    attempted_job_ids = _attempted_job_ids(database) if not include_attempted else set()
    def _series_or_default(column_name: str, default: str = "") -> Any:
        series = jobs.get(column_name)
        if series is None:
            return jobs.index.to_series().map(lambda _: default)
        return series
    job_status = (
        _series_or_default("application_status", "")
        .where(_series_or_default("application_status", "").fillna("").astype(str).str.strip() != "", _series_or_default("status", ""))
        .fillna("")
        .astype(str)
        .str.strip()
        .str.lower()
    )
    application_record_status = _series_or_default("status", "").fillna("").astype(str).str.strip().str.lower()
    applied_at = _series_or_default("applied_at", "").fillna("").astype(str).str.strip()
    submitted_at = _series_or_default("submitted_at", "").fillna("").astype(str).str.strip()
    jobs = jobs[
        (job_status != "applied")
        & (application_record_status != "applied")
        & (job_status != "closed")
        & (application_record_status != "closed")
        & (job_status != "ignored")
        & (application_record_status != "ignored")
        & (job_status != "archived")
        & (application_record_status != "archived")
        & (job_status != "rejected")
        & (application_record_status != "rejected")
        & (applied_at == "")
        & (submitted_at == "")
    ].copy()
    if attempted_job_ids and "job_id" in jobs.columns:
        jobs = jobs[~jobs["job_id"].astype(int).isin(attempted_job_ids)].copy()
    if jobs.empty:
        return []
    filtered = jobs[
        jobs.apply(
            lambda row: str(row.get("apply_type") or "").strip().lower() == "quick apply"
            or bool(row.get("has_quick_apply_button")),
            axis=1,
        )
    ].copy()
    if filtered.empty:
        return []
    if selected_job_ids:
        selected_set = {int(job_id) for job_id in selected_job_ids}
        filtered = filtered[filtered["job_id"].astype(int).isin(selected_set)].copy()
        if filtered.empty:
            return []
    sort_columns = [column for column in ["imported_at", "fit_score", "job_id"] if column in filtered.columns]
    ascending = [False] * len(sort_columns)
    filtered = filtered.sort_values(by=sort_columns, ascending=ascending, kind="stable")
    rows: list[dict[str, Any]] = []
    for _, row in filtered.head(max_jobs).iterrows():
        details = database.get_job_details(int(row["job_id"]))
        if details and not is_auto_apply_blocked(details):
            rows.append(details)
    return rows


def execute_single_quick_apply_job(
    database: Database,
    document_generator: DocumentGenerator,
    *,
    job_id: int,
    run_dir: Path,
    visible: bool = False,
    force_new_profile: bool = False,
    use_bulk_profile: bool = False,
    non_interactive: bool = False,
) -> BulkPrepareResult:
    run_dir.mkdir(parents=True, exist_ok=True)
    job = database.get_job_details(job_id)
    if not job:
        result = BulkPrepareResult(
            job_id=job_id,
            title="",
            company="",
            status="failed_needs_fix",
            resume_included=False,
            cover_letter_included=False,
            questions_answered=False,
            failure_reason=f"Job {job_id} not found.",
            root_failure_reason=f"Job {job_id} not found.",
            evidence_folder=str(run_dir),
            json_saved=True,
        )
        _write_apply_run_artifacts(run_dir, result, assist_result=None)
        return result

    document_generator.ensure_current_cover_letter(job_id, force_regenerate=True)
    assist_result = assist_seek_apply(
        job_id,
        database.db_path,
        visible=visible,
        force_new_profile=force_new_profile,
        use_bulk_profile=use_bulk_profile,
        keep_browser_open_override=False,
        resume_upload_only=False,
        input_func=(lambda: "") if non_interactive else input,
        allow_manual_login_prompt=not non_interactive,
    )
    if non_interactive and assist_result.status in {"paused_for_seek_login", "paused_for_seek_verification", "paused_for_captcha"}:
        for recovery_attempt in range(1, 3):
            paused_status = assist_result.status
            database.log_run(
                "bulk_prepare_quick_apply",
                "auto_seek_session_refresh_started",
                "success",
                f"job_id={job_id} paused_status={paused_status} use_bulk_profile={use_bulk_profile} attempt={recovery_attempt}",
            )
            try:
                channel, profile_dir = refresh_seek_session_in_browser(
                    database.db_path,
                    use_bulk_profile=use_bulk_profile,
                )
                database.log_run(
                    "bulk_prepare_quick_apply",
                    "auto_seek_session_refresh_opened",
                    "success",
                    f"job_id={job_id} channel={channel} profile_dir={profile_dir} attempt={recovery_attempt}",
                )
                validation = validate_seek_session(
                    database.db_path,
                    use_bulk_profile=use_bulk_profile,
                    visible=False,
                )
                database.log_run(
                    "bulk_prepare_quick_apply",
                    "auto_seek_session_refresh_validated",
                    "success" if validation.get("ok") else "warning",
                    f"job_id={job_id} state={validation.get('state')} reason={validation.get('reason')} attempt={recovery_attempt}",
                )
                if not validation.get("ok"):
                    break
                try:
                    closed_windows = close_visible_seek_profile_windows()
                except Exception as exc:  # noqa: BLE001
                    closed_windows = 0
                    database.log_run(
                        "bulk_prepare_quick_apply",
                        "auto_seek_session_refresh_close_windows_failed",
                        "warning",
                        f"job_id={job_id} attempt={recovery_attempt} {exc}",
                    )
                if closed_windows:
                    database.log_run(
                        "bulk_prepare_quick_apply",
                        "auto_seek_session_refresh_closed_windows",
                        "success",
                        f"job_id={job_id} closed_windows={closed_windows} attempt={recovery_attempt}",
                    )
                assist_result = assist_seek_apply(
                    job_id,
                    database.db_path,
                    visible=visible,
                    force_new_profile=force_new_profile,
                    use_bulk_profile=use_bulk_profile,
                    keep_browser_open_override=False,
                    resume_upload_only=False,
                    input_func=(lambda: "") if non_interactive else input,
                    allow_manual_login_prompt=not non_interactive,
                )
                if assist_result.status not in {"paused_for_seek_login", "paused_for_seek_verification", "paused_for_captcha"}:
                    break
            except Exception as exc:  # noqa: BLE001
                database.log_run(
                    "bulk_prepare_quick_apply",
                    "auto_seek_session_refresh_failed",
                    "error",
                    f"job_id={job_id} paused_status={paused_status} attempt={recovery_attempt} {exc}",
                )
                break
    result = _build_bulk_result(job, assist_result, run_dir)
    _write_apply_run_artifacts(run_dir, result, assist_result=assist_result)
    if result.status == "ready_for_human_review":
        database.update_application_state(job_id, "ready_to_apply", note="Prepared to SEEK review page and awaiting final submit.")
    return result


def run_bulk_quick_apply(
    database: Database,
    document_generator: DocumentGenerator,
    *,
    max_jobs: int = 5,
    dry_run: bool = False,
    progress_callback: Any | None = None,
    pause_between_jobs_seconds: float = 3.0,
    per_job_timeout_seconds: int = DEFAULT_PER_JOB_TIMEOUT_SECONDS,
    subprocess_runner: Any | None = None,
    python_executable: str | None = None,
    selected_job_ids: list[int] | None = None,
    include_attempted: bool = False,
    should_cancel: Any | None = None,
) -> list[BulkPrepareResult]:
    jobs = find_quick_apply_jobs(
        database,
        max_jobs=max_jobs,
        selected_job_ids=selected_job_ids,
        include_attempted=include_attempted,
    )
    if dry_run:
        return [
            BulkPrepareResult(
                job_id=int(job["job_id"]),
                title=str(job.get("title") or ""),
                company=str(job.get("company") or ""),
                status="skipped",
                resume_included=False,
                cover_letter_included=False,
                questions_answered=False,
                failure_reason="Dry run preview only.",
                root_failure_reason="Dry run preview only.",
                evidence_folder=str(_build_apply_run_dir(database.db_path, int(job["job_id"]))),
                job_url=str(job.get("url") or ""),
                json_saved=True,
            )
            for job in jobs
        ]

    runner = subprocess_runner or subprocess.run
    executable = python_executable or sys.executable
    results: list[BulkPrepareResult] = []
    total = len(jobs)
    for index, job in enumerate(jobs, start=1):
        if should_cancel is not None and should_cancel():
            break
        job_id = int(job["job_id"])
        title = str(job.get("title") or "")
        company = str(job.get("company") or "")
        run_dir = _build_apply_run_dir(database.db_path, job_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        if progress_callback is not None:
            progress_callback(index - 1, total, title, company, results)

        command = [
            executable,
            str(SINGLE_APPLY_SCRIPT),
            "--job-id",
            str(job_id),
            "--db-path",
            str(database.db_path),
            "--run-folder",
            str(run_dir),
            "--visible",
            "false",
            "--no-submit",
            "--non-interactive",
        ]
        result: BulkPrepareResult | None = None
        last_exit_code: int | None = None
        last_cancelled = False
        max_auth_recovery_attempts = 2
        for auth_attempt in range(1, max_auth_recovery_attempts + 1):
            stdout_text = ""
            stderr_text = ""
            exit_code: int | None = None
            timeout_error = ""
            cancelled = False
            use_simple_timeout_runner = subprocess_runner is not None or should_cancel is None
            if use_simple_timeout_runner:
                try:
                    run_kwargs: dict[str, Any] = {}
                    if subprocess_runner is None:
                        run_kwargs.update(_windows_subprocess_kwargs())
                    completed = runner(
                        command,
                        capture_output=True,
                        env=_subprocess_env(),
                        timeout=per_job_timeout_seconds,
                        **_subprocess_text_kwargs(),
                        **run_kwargs,
                    )
                    stdout_text = str(getattr(completed, "stdout", "") or "")
                    stderr_text = str(getattr(completed, "stderr", "") or "")
                    exit_code = int(getattr(completed, "returncode", 1))
                except subprocess.TimeoutExpired as exc:
                    stdout_text = str(getattr(exc, "stdout", "") or "")
                    stderr_text = str(getattr(exc, "stderr", "") or "")
                    timeout_error = _build_timeout_error_message(
                        stdout_text,
                        stderr_text,
                        per_job_timeout_seconds,
                    )
                    exit_code = 124
                except Exception as exc:  # noqa: BLE001
                    stderr_text = str(exc)
                    exit_code = 1
            else:
                process = subprocess.Popen(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=_subprocess_env(),
                    **_subprocess_text_kwargs(),
                    **_windows_subprocess_kwargs(),
                )
                started_at = time.monotonic()
                while True:
                    if should_cancel is not None and should_cancel():
                        cancelled = True
                        try:
                            process.terminate()
                            process.wait(timeout=5)
                        except Exception:  # noqa: BLE001
                            try:
                                process.kill()
                            except Exception:  # noqa: BLE001
                                pass
                        break
                    if process.poll() is not None:
                        break
                    if (time.monotonic() - started_at) >= per_job_timeout_seconds:
                        timeout_error = _build_timeout_error_message(
                            stdout_text,
                            stderr_text,
                            per_job_timeout_seconds,
                        )
                        try:
                            process.terminate()
                            process.wait(timeout=5)
                        except Exception:  # noqa: BLE001
                            try:
                                process.kill()
                            except Exception:  # noqa: BLE001
                                pass
                        break
                    time.sleep(0.5)

                try:
                    stdout_text, stderr_text = process.communicate(timeout=5)
                except Exception:  # noqa: BLE001
                    stdout_text = stdout_text or ""
                    stderr_text = stderr_text or ""
                exit_code = int(process.returncode if process.returncode is not None else (130 if cancelled else 1))

            result = _read_result_from_run_dir(run_dir)
            if result is None:
                result = BulkPrepareResult(
                    job_id=job_id,
                    title=title,
                    company=company,
                    status="cancelled" if cancelled else "failed_needs_fix",
                    resume_included=False,
                    cover_letter_included=False,
                    questions_answered=False,
                    failure_reason=(
                        "Bulk Quick Apply was cancelled by the user."
                        if cancelled
                        else timeout_error or stderr_text or "Bulk Quick Apply subprocess failed before writing evidence."
                    ),
                    root_failure_reason=(
                        "Bulk Quick Apply was cancelled by the user."
                        if cancelled
                        else timeout_error or stderr_text or "Bulk Quick Apply subprocess failed before writing evidence."
                    ),
                    evidence_folder=str(run_dir),
                    job_url=str(job.get("url") or ""),
                    final_submit_not_clicked=True,
                    json_saved=True,
                )
                _write_checklist_only(run_dir, result)
            result.subprocess_exit_code = exit_code
            if timeout_error and not result.root_failure_reason:
                result.root_failure_reason = timeout_error
                result.failure_reason = timeout_error
            if cancelled:
                result.status = "cancelled"
                result.root_failure_reason = "Bulk Quick Apply was cancelled by the user."
                result.failure_reason = "Bulk Quick Apply was cancelled by the user."
            _append_subprocess_log(run_dir, command, stdout_text, stderr_text, exit_code)
            _write_checklist_only(run_dir, result)
            last_exit_code = exit_code
            last_cancelled = cancelled

            paused_for_auth = result.status in {"paused_for_seek_login", "paused_for_seek_verification", "paused_for_captcha"}
            if cancelled or not paused_for_auth or auth_attempt >= max_auth_recovery_attempts:
                break

            database.log_run(
                "bulk_prepare_quick_apply",
                "outer_auto_seek_session_refresh_started",
                "success",
                f"job_id={job_id} paused_status={result.status} attempt={auth_attempt}",
            )
            try:
                channel, profile_dir = refresh_seek_session_in_browser(
                    database.db_path,
                    use_bulk_profile=False,
                )
                database.log_run(
                    "bulk_prepare_quick_apply",
                    "outer_auto_seek_session_refresh_opened",
                    "success",
                    f"job_id={job_id} channel={channel} profile_dir={profile_dir} attempt={auth_attempt}",
                )
                validation = validate_seek_session_subprocess(
                    database.db_path,
                    use_bulk_profile=False,
                    visible=False,
                )
                database.log_run(
                    "bulk_prepare_quick_apply",
                    "outer_auto_seek_session_refresh_validated",
                    "success" if validation.get("ok") else "warning",
                    f"job_id={job_id} state={validation.get('state')} reason={validation.get('reason')} attempt={auth_attempt}",
                )
                if not validation.get("ok"):
                    break
                closed_windows = close_visible_seek_profile_windows()
                if closed_windows:
                    database.log_run(
                        "bulk_prepare_quick_apply",
                        "outer_auto_seek_session_refresh_closed_windows",
                        "success",
                        f"job_id={job_id} closed_windows={closed_windows} attempt={auth_attempt}",
                    )
            except Exception as exc:  # noqa: BLE001
                database.log_run(
                    "bulk_prepare_quick_apply",
                    "outer_auto_seek_session_refresh_failed",
                    "error",
                    f"job_id={job_id} paused_status={result.status} attempt={auth_attempt} {exc}",
                )
                break

        if result is None:
            result = BulkPrepareResult(
                job_id=job_id,
                title=title,
                company=company,
                status="cancelled" if last_cancelled else "failed_needs_fix",
                resume_included=False,
                cover_letter_included=False,
                questions_answered=False,
                failure_reason="Bulk Quick Apply subprocess failed before writing evidence.",
                root_failure_reason="Bulk Quick Apply subprocess failed before writing evidence.",
                evidence_folder=str(run_dir),
                job_url=str(job.get("url") or ""),
                final_submit_not_clicked=True,
                json_saved=True,
                subprocess_exit_code=last_exit_code,
            )
        if result.status == "ready_for_human_review":
            database.update_application_state(job_id, "ready_to_apply", note="Prepared to SEEK review page and awaiting final submit.")
        results.append(result)
        if progress_callback is not None:
            progress_callback(index, total, title, company, results)
        if cancelled:
            break
        if index < total:
            time.sleep(max(0.0, pause_between_jobs_seconds))
    return results


def _build_apply_run_dir(db_path: Path, job_id: int) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return db_path.parent / "apply_runs" / f"{timestamp}_{job_id}"


def _load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _load_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""


def _strip_html_text(raw_html: str) -> str:
    if not raw_html:
        return ""
    try:
        soup = BeautifulSoup(raw_html, "html.parser")
        return re.sub(r"\s+", " ", soup.get_text(" ", strip=True)).strip()
    except Exception:
        return re.sub(r"\s+", " ", raw_html).strip()


def _copy_if_exists(src: Path, dest: Path) -> bool:
    if not src.exists():
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    return True


def _find_review_checkpoint(debug_dir: Path) -> tuple[Path | None, Path | None, dict[str, Any]]:
    stems = [
        "resume_confirmed_included",
        "review_page_checked",
        "resume_missing_on_review",
        "resume_state_lost",
        "final_review_checklist_failed",
    ]
    for stem in stems:
        html_path = debug_dir / f"{stem}.html"
        json_path = debug_dir / f"{stem}.json"
        if html_path.exists() or json_path.exists():
            return html_path if html_path.exists() else None, json_path if json_path.exists() else None, _load_json(json_path) if json_path.exists() else {}
    return None, None, {}


def _find_failure_checkpoint_payload(debug_dir: Path) -> dict[str, Any]:
    stems = [
        "resume_upload_blocked_before_continue",
        "resume_upload_complete_timeout",
        "resume_step_failed",
        "resume_state_lost",
        "resume_missing_on_review",
        "final_review_checklist_failed",
    ]
    for stem in stems:
        json_path = debug_dir / f"{stem}.json"
        if json_path.exists():
            payload = _load_json(json_path)
            if payload:
                return payload
    return {}


def _extract_question_answer_count(text: str) -> str:
    match = re.search(r"You answered\s+\d+\s+out of\s+\d+", text, re.I)
    return match.group(0) if match else ""


def _infer_timeout_stage(stdout_text: str, stderr_text: str) -> str:
    combined = f"{stdout_text}\n{stderr_text}".lower()
    if "resume_page_title: choose documents" in combined or "checkpoint=choose_documents_loaded" in combined:
        return "stuck on Choose documents / resume handling"
    if "existing_resume_delete_button_clicked" in combined or "resume_deleted_for_space" in combined:
        return "stuck while replacing or uploading the resume"
    if "handle_resume_upload_started" in combined or "clicked_upload_resume_label" in combined:
        return "stuck during resume upload setup"
    if "clicked_quick_apply" in combined and "handle_resume_upload_started" not in combined:
        return "stuck after Quick Apply opened"
    if "opened_job" in combined and "clicked_quick_apply" not in combined:
        return "stuck after opening the job page before Quick Apply"
    if "page.goto: timeout 15000ms exceeded" in combined:
        return "stuck opening the tracked email/redirect job URL"
    return "stuck in an unclassified SEEK step"


def _build_timeout_error_message(stdout_text: str, stderr_text: str, timeout_seconds: int) -> str:
    stage = _infer_timeout_stage(stdout_text, stderr_text)
    return (
        f"Bulk Quick Apply subprocess timed out after {timeout_seconds} seconds "
        f"({stage})."
    )


def _build_bulk_result(job: dict[str, Any], assist_result: Any, run_dir: Path) -> BulkPrepareResult:
    debug_dir = Path(str(getattr(assist_result, "debug_dir", "") or ""))
    checklist_path = debug_dir / "final_review_checklist.json" if debug_dir else Path()
    checklist = _load_json(checklist_path) if checklist_path.exists() else {}
    review_html_path, _review_json_path, review_payload = _find_review_checkpoint(debug_dir) if debug_dir else (None, None, {})
    failure_payload = _find_failure_checkpoint_payload(debug_dir) if debug_dir else {}
    review_html_text = _load_text(review_html_path) if review_html_path else ""
    review_body_text = _strip_html_text(review_html_text)
    if not review_body_text:
        review_body_text = str(review_payload.get("body_text_snippet") or "")

    reached_review_page = (
        "/review" in str(getattr(assist_result, "final_url", "") or "").lower()
        or bool(checklist)
        or bool(review_payload)
        or "review and submit" in review_body_text.lower()
    )
    resume_info = checklist.get("resume_review", {}) if isinstance(checklist.get("resume_review"), dict) else {}
    cover_info = checklist.get("cover_letter_review", {}) if isinstance(checklist.get("cover_letter_review"), dict) else {}
    resume_text_seen = ""
    resume_match = re.search(r"\b([A-Za-z0-9._ -]+\.(?:doc|docx|pdf))\b", review_body_text, re.I)
    if resume_match:
        resume_text_seen = resume_match.group(1)
    question_answer_count_text = _extract_question_answer_count(review_body_text)
    checklist_has_explicit_values = any(
        key in checklist
        for key in (
            "resume_included",
            "cover_letter_included",
            "questions_answered",
            "no_validation_errors",
            "final_submit_not_clicked",
            "all_checks_passed",
        )
    )
    resume_included = bool(checklist.get("resume_included")) or (
        reached_review_page and "no resume included" not in review_body_text.lower() and bool(resume_text_seen)
    )
    cover_letter_included = bool(checklist.get("cover_letter_included")) or (
        "you wrote a cover letter for this application" in review_body_text.lower()
        or bool(cover_info.get("positive_phrase_visible"))
    )
    questions_answered = bool(checklist.get("questions_answered")) or bool(question_answer_count_text)
    no_validation_errors = bool(checklist.get("no_validation_errors")) or not re.search(
        r"please make a selection|required field",
        review_body_text,
        re.I,
    )
    final_submit_not_clicked = bool(checklist.get("final_submit_not_clicked", True))
    all_checks_passed = bool(checklist.get("all_checks_passed", False)) if checklist_has_explicit_values else False
    root_failure_reason = str(getattr(assist_result, "root_failure_reason", "") or getattr(assist_result, "error", "") or "")
    if not root_failure_reason and checklist and not bool(checklist.get("all_checks_passed", False)):
        root_failure_reason = "Final review checklist failed."
    if not root_failure_reason and getattr(assist_result, "status", "") not in {"prepared_for_manual_review"}:
        root_failure_reason = str(getattr(assist_result, "status", "") or "")

    screenshot_path_obj = run_dir / "final_review.png"
    html_path_obj = run_dir / "final_review.html"
    screenshot_saved = screenshot_path_obj.exists()
    html_saved = html_path_obj.exists()
    evidence_capture_errors = list(getattr(assist_result, "evidence_capture_errors", []) or [])
    evidence_capture_errors.extend(list(failure_payload.get("evidence_capture_errors") or []))
    deduped_capture_errors: list[str] = []
    for item in evidence_capture_errors:
        text = str(item or "").strip()
        if text and text not in deduped_capture_errors:
            deduped_capture_errors.append(text)

    status = "ready_for_human_review"
    assist_status = str(getattr(assist_result, "status", "") or "")
    if assist_status == "skipped":
        status = "skipped"
    elif assist_status in PAUSED_BULK_STATUSES:
        status = assist_status
    elif checklist_has_explicit_values and not all_checks_passed:
        status = "failed_needs_fix"
    elif not (
        reached_review_page
        and resume_included
        and cover_letter_included
        and questions_answered
        and no_validation_errors
        and final_submit_not_clicked
    ):
        status = "failed_needs_fix"

    return BulkPrepareResult(
        job_id=int(job["job_id"]),
        title=str(job.get("title") or ""),
        company=str(job.get("company") or ""),
        job_url=str(job.get("url") or ""),
        status=status,
        reached_review_page=reached_review_page,
        resume_included=resume_included,
        resume_text_seen=resume_text_seen or str(resume_info.get("expected_filename_visible") or ""),
        cover_letter_included=cover_letter_included,
        cover_letter_text_seen=str(cover_info.get("expected_excerpt") or ""),
        questions_answered=questions_answered,
        question_answer_count_text=question_answer_count_text,
        no_validation_errors=no_validation_errors,
        final_submit_not_clicked=final_submit_not_clicked,
        failure_reason=root_failure_reason,
        root_failure_reason=root_failure_reason,
        evidence_folder=str(run_dir),
        screenshot_path=str(screenshot_path_obj),
        html_path=str(html_path_obj),
        evidence_capture_errors=deduped_capture_errors,
        screenshot_saved=screenshot_saved,
        html_saved=html_saved,
        json_saved=True,
    )


def _write_checklist_only(run_dir: Path, result: BulkPrepareResult) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    checklist_payload = asdict(result)
    (run_dir / "final_review_checklist.json").write_text(
        json.dumps(checklist_payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _write_apply_run_artifacts(run_dir: Path, result: BulkPrepareResult, assist_result: Any | None) -> None:
    debug_dir = Path(str(getattr(assist_result, "debug_dir", "") or "")) if assist_result is not None else Path()
    debug_dir_exists = bool(debug_dir and debug_dir.exists())
    review_copied = False
    if debug_dir_exists:
        for stem in ["resume_confirmed_included", "review_page_checked", "resume_missing_on_review", "resume_state_lost"]:
            if _copy_if_exists(debug_dir / f"{stem}.png", run_dir / "final_review.png"):
                review_copied = True
            if _copy_if_exists(debug_dir / f"{stem}.html", run_dir / "final_review.html"):
                review_copied = True
            if review_copied:
                break
        _copy_if_exists(debug_dir / "dynamic_question_scan.json", run_dir / "dynamic_question_scan.json")
        _copy_if_exists(debug_dir / "codex_prompt_resume_failure.txt", run_dir / "codex_prompt_review_failure.txt")
        for stem in [
            "resume_upload_complete_timeout",
            "resume_upload_blocked_before_continue",
            "resume_step_failed",
            "resume_state_lost",
        ]:
            _copy_if_exists(debug_dir / f"{stem}.png", run_dir / f"{stem}.png")
            _copy_if_exists(debug_dir / f"{stem}.html", run_dir / f"{stem}.html")
            _copy_if_exists(debug_dir / f"{stem}.json", run_dir / f"{stem}.json")

    result.screenshot_saved = Path(result.screenshot_path).exists()
    result.html_saved = Path(result.html_path).exists()
    result.json_saved = True
    _write_checklist_only(run_dir, result)

    log_lines = [
        f"job_id={result.job_id}",
        f"title={result.title}",
        f"company={result.company}",
        f"status={result.status}",
        f"assist_status={getattr(assist_result, 'status', '') if assist_result is not None else ''}",
        f"resume_included={result.resume_included}",
        f"cover_letter_included={result.cover_letter_included}",
        f"questions_answered={result.questions_answered}",
        f"no_validation_errors={result.no_validation_errors}",
        f"final_submit_not_clicked={result.final_submit_not_clicked}",
        f"failure_reason={result.failure_reason}",
        f"root_failure_reason={result.root_failure_reason}",
        f"evidence_capture_errors={json.dumps(result.evidence_capture_errors or [], ensure_ascii=False)}",
        f"debug_dir={getattr(assist_result, 'debug_dir', '') if assist_result is not None else ''}",
        f"final_url={getattr(assist_result, 'final_url', '') if assist_result is not None else ''}",
        f"steps_completed={', '.join(getattr(assist_result, 'steps_completed', []) or []) if assist_result is not None else ''}",
    ]
    (run_dir / "apply_run_log.txt").write_text("\n".join(log_lines), encoding="utf-8")
    if result.status != "ready_for_human_review" and not (run_dir / "codex_prompt_review_failure.txt").exists():
        prompt_lines = [
            "Bulk Quick Apply review failure",
            f"Job ID: {result.job_id}",
            f"Title: {result.title}",
            f"Company: {result.company}",
            f"Failure reason: {result.root_failure_reason or result.failure_reason or 'Checklist failed.'}",
            f"Evidence folder: {run_dir}",
            f"Final review HTML: {run_dir / 'final_review.html'}",
            f"Final review checklist: {run_dir / 'final_review_checklist.json'}",
            "Suggested next fix:",
            "Inspect why the final review evidence did not confirm resume, cover letter, or answered questions, and tighten the SEEK state verification before advancing.",
        ]
        (run_dir / "codex_prompt_review_failure.txt").write_text("\n".join(prompt_lines), encoding="utf-8")


def _append_subprocess_log(run_dir: Path, command: list[str], stdout_text: str, stderr_text: str, exit_code: int | None) -> None:
    log_path = run_dir / "apply_run_log.txt"
    existing = _load_text(log_path)
    sections: list[str] = []
    if existing.strip():
        sections.append(existing.rstrip())
    sections.append("subprocess_command=" + " ".join(command))
    sections.append(f"subprocess_exit_code={'' if exit_code is None else exit_code}")
    sections.append("subprocess_stdout:")
    sections.append(stdout_text.rstrip() or "(none)")
    sections.append("subprocess_stderr:")
    sections.append(stderr_text.rstrip() or "(none)")
    log_path.write_text("\n\n".join(sections).rstrip() + "\n", encoding="utf-8")


def _read_result_from_run_dir(run_dir: Path) -> BulkPrepareResult | None:
    checklist_path = run_dir / "final_review_checklist.json"
    payload = _load_json(checklist_path)
    if not payload:
        return None
    evidence_errors = payload.get("evidence_capture_errors")
    if not isinstance(evidence_errors, list):
        evidence_errors = []
    return BulkPrepareResult(
        job_id=int(payload.get("job_id") or 0),
        title=str(payload.get("title") or ""),
        company=str(payload.get("company") or ""),
        status=str(payload.get("status") or "failed_needs_fix"),
        resume_included=bool(payload.get("resume_included")),
        cover_letter_included=bool(payload.get("cover_letter_included")),
        questions_answered=bool(payload.get("questions_answered")),
        failure_reason=str(payload.get("failure_reason") or ""),
        evidence_folder=str(payload.get("evidence_folder") or run_dir),
        job_url=str(payload.get("job_url") or ""),
        reached_review_page=bool(payload.get("reached_review_page")),
        resume_text_seen=str(payload.get("resume_text_seen") or ""),
        cover_letter_text_seen=str(payload.get("cover_letter_text_seen") or ""),
        question_answer_count_text=str(payload.get("question_answer_count_text") or ""),
        no_validation_errors=bool(payload.get("no_validation_errors")),
        final_submit_not_clicked=bool(payload.get("final_submit_not_clicked", True)),
        screenshot_path=str(payload.get("screenshot_path") or (run_dir / "final_review.png")),
        html_path=str(payload.get("html_path") or (run_dir / "final_review.html")),
        root_failure_reason=str(payload.get("root_failure_reason") or payload.get("failure_reason") or ""),
        evidence_capture_errors=evidence_errors,
        screenshot_saved=bool(payload.get("screenshot_saved")),
        html_saved=bool(payload.get("html_saved")),
        json_saved=bool(payload.get("json_saved", True)),
        subprocess_exit_code=payload.get("subprocess_exit_code"),
    )
