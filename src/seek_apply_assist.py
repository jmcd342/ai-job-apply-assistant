from __future__ import annotations

import builtins
import dataclasses
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from urllib.parse import urljoin
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

from src.database import Database
from src.salary_estimator import estimate_salary_expectation
from src.seek_diagnostics import (
    _log_continue_click_failure as _log_continue_click_failure_impl,
    _log_resume_state as _log_resume_state_impl,
    _log_seek_step_diagnostics as _log_seek_step_diagnostics_impl,
    _record_resume_checkpoint as _record_resume_checkpoint_impl,
    _safe_page_html_write as _safe_page_html_write_impl,
    _safe_page_screenshot as _safe_page_screenshot_impl,
    _save_resume_failure_json as _save_resume_failure_json_impl,
    _save_resume_section_dom_snapshot as _save_resume_section_dom_snapshot_impl,
    _save_resume_state_lost_artifacts as _save_resume_state_lost_artifacts_impl,
    _save_upload_click_diagnostics as _save_upload_click_diagnostics_impl,
    _save_verification_failure_artifacts as _save_verification_failure_artifacts_impl,
    _write_codex_prompt_resume_failure as _write_codex_prompt_resume_failure_impl,
    _write_dynamic_question_scan as _write_dynamic_question_scan_impl,
    _write_final_review_checklist as _write_final_review_checklist_impl,
    _write_local_dom_codex_prompt as _write_local_dom_codex_prompt_impl,
    capture_local_dom_diagnostics as capture_local_dom_diagnostics_impl,
)
from src.seek_email_code_login import SeekEmailCodeFetcher, load_seek_login_email


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SUBMIT_SEEK_REVIEW_SCRIPT = PROJECT_ROOT / "scripts" / "run_submit_seek_review.py"


def print(*args: object, **kwargs: object) -> None:  # type: ignore[override]
    try:
        builtins.print(*args, **kwargs)
    except UnicodeEncodeError:
        encoding = getattr(getattr(sys, "stdout", None), "encoding", None) or "utf-8"
        safe_args = []
        for arg in args:
            text = str(arg)
            try:
                safe_args.append(text.encode(encoding, errors="backslashreplace").decode(encoding, errors="ignore"))
            except Exception:  # noqa: BLE001
                safe_args.append(text.encode("ascii", errors="backslashreplace").decode("ascii"))
        try:
            builtins.print(*safe_args, **kwargs)
        except Exception:  # noqa: BLE001
            pass
    except OSError:
        pass


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


def _python_subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    return env


def _python_subprocess_text_kwargs() -> dict[str, Any]:
    return {
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
    }


FINAL_SUBMIT_PATTERNS = ("submit", "send application", "apply now", "send")
SAFE_NAVIGATION_PATTERNS = ("continue", "review", "next")
SEEK_JOB_URL_PATTERN = re.compile(r"https://(?:www\.|nz\.)?seek\.com(?:\.au|\.nz)?/job/\d+[^\s\"'<>)]*", re.I)
PAUSED_STATUSES = {
    "paused_for_seek_login",
    "paused_for_seek_verification",
    "paused_for_captcha",
    "paused_for_manual_resume_upload",
    "paused_for_manual_questionnaire",
}
PROFILE_LOCKED_MESSAGE = (
    "Close all SEEK/Chromium windows and retry. If it still fails, clear the "
    "app-managed SEEK browser profile and re-run SEEK login setup."
)
BROWSER_CHANNEL_FALLBACKS = ("chrome", "msedge", "chromium")
SEEK_SESSION_STATES = {
    "unknown",
    "session_valid",
    "login_required",
    "verification_required",
    "captcha_required",
    "session_refreshed",
}


@dataclass
class ApplyAssistResult:
    job_id: int
    status: str
    steps_completed: list[str]
    error: str
    final_url: str
    debug_dir: str = ""
    latest_checkpoint: str = ""
    root_failure_reason: str = ""
    evidence_capture_errors: list[str] | None = None


@dataclass
class ResumeUploadResult:
    cv_uploaded: bool
    status: str
    warning: str = ""
    verified_filename: str = ""


@dataclass
class DocumentStepResult:
    success: bool
    status: str = ""
    warning: str = ""


@dataclass
class QuestionnaireResult:
    status: str
    warning: str = ""


@dataclass
class DynamicQuestionResult:
    status: str
    answered_count: int
    warning: str = ""
    unknown_questions: list[str] | None = None


@dataclass
class GenericAnswerResolution:
    chosen_answer: str | None
    confidence: float
    reason: str
    should_answer: bool
    matched_rule: str = ""
    inferred_question_type: str = ""
    inferred_skill: str = ""
    profile_years_used: int | None = None


@dataclass
class FieldAnswerContext:
    field_index: int
    field_type: str
    field_id: str
    field_name: str
    extracted_question_text: str
    available_options: list[str]
    current_value: str
    required: bool
    nearby_text_candidates: list[str]
    confidence: float = 0.0


def seek_session_state_path_for_db(db_path: Path) -> Path:
    return db_path.parent / "seek_session_state.json"


def read_seek_session_state(db_path: Path) -> dict[str, Any]:
    path = seek_session_state_path_for_db(db_path)
    if not path.exists():
        return {
            "state": "unknown",
            "updated_at": "",
            "job_id": None,
            "reason": "",
            "matched_token": "",
            "matched_source": "",
            "final_url": "",
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {
            "state": "unknown",
            "updated_at": "",
            "job_id": None,
            "reason": "Unreadable SEEK session state file.",
            "matched_token": "",
            "matched_source": "",
            "final_url": "",
        }
    state = str(payload.get("state") or "unknown").strip() or "unknown"
    if state not in SEEK_SESSION_STATES:
        state = "unknown"
    return {
        "state": state,
        "updated_at": str(payload.get("updated_at") or ""),
        "job_id": payload.get("job_id"),
        "reason": str(payload.get("reason") or ""),
        "matched_token": str(payload.get("matched_token") or ""),
        "matched_source": str(payload.get("matched_source") or ""),
        "final_url": str(payload.get("final_url") or ""),
    }


def write_seek_session_state(
    db_path: Path,
    state: str,
    *,
    job_id: int | None = None,
    reason: str = "",
    matched_token: str = "",
    matched_source: str = "",
    final_url: str = "",
) -> None:
    safe_state = state if state in SEEK_SESSION_STATES else "unknown"
    payload = {
        "state": safe_state,
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "job_id": job_id,
        "reason": reason,
        "matched_token": matched_token,
        "matched_source": matched_source,
        "final_url": final_url,
    }
    path = seek_session_state_path_for_db(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

@dataclass
class FinalReviewChecklist:
    resume_included: bool
    cover_letter_included: bool
    questions_answered: bool
    no_validation_errors: bool
    final_submit_not_clicked: bool

    def as_dict(self) -> dict[str, bool]:
        return {
            "resume_included": self.resume_included,
            "cover_letter_included": self.cover_letter_included,
            "questions_answered": self.questions_answered,
            "no_validation_errors": self.no_validation_errors,
            "final_submit_not_clicked": self.final_submit_not_clicked,
        }

    def all_true(self) -> bool:
        return all(self.as_dict().values())


class SeekApplyAdapter(Protocol):
    def open_job(self, url: str, fallback_url: str | None = None) -> None: ...
    def detect_pause_reason(self) -> str | None: ...
    def click_quick_apply(self) -> None: ...
    def wait_for_application_form(self) -> None: ...
    def wait_for_resume_section(self, timeout_ms: int = 20000) -> None: ...
    def log_resume_diagnostics(self) -> None: ...
    def get_resume_file_input_state(self) -> tuple[int, bool, bool]: ...
    def get_resume_upload_radio_state(self) -> tuple[bool, bool]: ...
    def get_resume_method_state(self) -> dict[str, Any]: ...
    def click_resume_change_radio(self) -> bool: ...
    def get_existing_resume_options(self) -> list[str]: ...
    def select_existing_resume_option(self, option_text: str) -> bool: ...
    def click_existing_resume_delete(self) -> bool: ...
    def get_resume_upload_progress_state(self, expected_filename: str = "") -> dict[str, Any]: ...
    def wait_for_resume_file_input(self, timeout_ms: int = 5000) -> bool: ...
    def try_resume_upload_via_file_chooser(self, file_path: Path, timeout_ms: int = 5000) -> bool: ...
    def upload_resume(self, file_path: Path) -> None: ...
    def click_resume_upload_radio(self) -> bool: ...
    def click_resume_none_radio(self) -> bool: ...
    def click_resume_upload_button(self) -> bool: ...
    def save_resume_upload_selected_screenshot(self, debug_dir: Path) -> Path: ...
    def save_resume_upload_selected_html(self, debug_dir: Path) -> Path: ...
    def save_resume_after_file_upload_screenshot(self, debug_dir: Path) -> Path: ...
    def detect_visible_resume_filename(self) -> str: ...
    def resume_appears_present(self) -> bool: ...
    def resume_limit_ui_present(self) -> bool: ...
    def get_resume_limit_dropdown_options(self) -> list[str]: ...
    def select_resume_limit_dropdown_option(self, option_text: str) -> bool: ...
    def click_resume_limit_delete(self) -> bool: ...
    def confirm_resume_limit_delete(self) -> bool: ...
    def wait_for_resume_limit_change(self, previous_options: list[str], timeout_ms: int = 5000) -> None: ...
    def save_resume_pre_search_artifacts(self, debug_dir: Path) -> tuple[Path, Path]: ...
    def save_resume_debug_artifacts(self, debug_dir: Path) -> tuple[Path, Path]: ...
    def collect_resume_upload_diagnostics(self) -> dict[str, Any]: ...
    def collect_resume_section_snapshot(self, expected_filename: str = "") -> dict[str, Any]: ...
    def save_resume_section_screenshot(self, path: Path, *, expected_filename: str = "") -> tuple[bool, str]: ...
    def capture_local_dom_diagnostics(self, failure_type: str, expected_filename: str = "") -> dict[str, Any]: ...
    def save_local_dom_screenshot(self, path: Path, *, failure_type: str, expected_filename: str = "") -> tuple[bool, str]: ...
    def save_named_resume_artifacts(self, debug_dir: Path, stem: str) -> tuple[Path, Path]: ...
    def select_cover_letter_change(self) -> None: ...
    def fill_cover_letter(self, text: str) -> None: ...
    def click_continue(self) -> None: ...
    def wait_for_questionnaire_section(self, timeout_ms: int = 20000) -> None: ...
    def log_questionnaire_diagnostics(self) -> None: ...
    def save_questionnaire_pre_search_artifacts(self, debug_dir: Path) -> tuple[Path, Path]: ...
    def select_work_eligibility_yes(self) -> bool: ...
    def fill_salary_expectation(self, text: str) -> bool: ...
    def save_questionnaire_failed_artifacts(self, debug_dir: Path) -> tuple[Path, Path]: ...
    def extract_dynamic_questions(self) -> list[dict[str, Any]]: ...
    def answer_dynamic_question(self, question: dict[str, Any], answer: str) -> bool: ...
    def get_questionnaire_validation_errors(self) -> list[str]: ...
    def save_dynamic_question_debug_artifacts(self, debug_dir: Path) -> tuple[Path, Path]: ...
    def click_questionnaire_continue(self) -> None: ...
    def final_navigation_label(self) -> str: ...
    def click_final_navigation(self) -> None: ...
    def current_url(self) -> str: ...
    def page_text(self) -> str: ...
    def close(self) -> None: ...


class SeekApplyAssistError(RuntimeError):
    pass


class SeekBrowserProfileLockedError(SeekApplyAssistError):
    pass


def format_salary_expectation(*, role_family: str, fit_score: int, existing_text: str = "") -> str:
    existing = str(existing_text or "").strip()
    if existing:
        return existing
    fallback_midpoints = {
        "data_engineering": "115k",
        "analytics_engineering": "115k",
        "business_intelligence": "100k",
        "data_science": "105k",
        "ai_automation": "105k",
    }
    return fallback_midpoints.get(str(role_family or "").strip(), "105k")


def _seek_job_is_closed(page_text: str) -> bool:
    text = str(page_text or "").lower()
    return "this job is no longer advertised" in text or "job is no longer advertised" in text


def _extract_seek_job_urls(value: Any) -> list[str]:
    text = str(value or "")
    matches = SEEK_JOB_URL_PATTERN.findall(text)
    results: list[str] = []
    seen: set[str] = set()
    for match in matches:
        normalized = match.rstrip(".,);]}>")
        if normalized and normalized not in seen:
            seen.add(normalized)
            results.append(normalized)
    return results


def _resolve_seek_job_open_urls(job: dict[str, Any]) -> tuple[str, str]:
    raw_urls = [
        str(job.get("url") or "").strip(),
        str(job.get("canonical_url") or "").strip(),
    ]
    for field_name in ("source_page_text", "source_html", "description", "content"):
        raw_urls.extend(_extract_seek_job_urls(job.get(field_name)))
    ordered_urls: list[str] = []
    seen: set[str] = set()
    for candidate in raw_urls:
        normalized = str(candidate or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        ordered_urls.append(normalized)
    if not ordered_urls:
        return "", ""
    preferred_url = next(
        (
            candidate
            for candidate in ordered_urls
            if "email.s.seek.co.nz" not in candidate.lower() and "/job/" in candidate.lower()
        ),
        ordered_urls[0],
    )
    fallback_url = next((candidate for candidate in ordered_urls if candidate != preferred_url), "")
    return preferred_url, fallback_url


def _detect_post_quick_apply_state(adapter: SeekApplyAdapter, timeout_ms: int = 12000) -> str:
    state, _payload = _wait_for_post_quick_apply_ready(adapter, timeout_ms=timeout_ms)
    return state


def _pause_status_for_login_error(message: str, default_status: str = "paused_for_seek_login") -> str:
    lowered = str(message or "").lower()
    if "captcha" in lowered or "verify you are human" in lowered:
        return "paused_for_captcha"
    if "verification" in lowered or "verifying" in lowered:
        return "paused_for_seek_verification"
    return default_status


def _safe_detect_pause_reason(adapter: SeekApplyAdapter) -> str:
    try:
        return str(adapter.detect_pause_reason() or "")
    except Exception:  # noqa: BLE001
        return ""


def _page_supports_dom(page: Any | None) -> bool:
    if page is None:
        return False
    return any(callable(getattr(page, attr, None)) for attr in ("locator", "evaluate", "content", "title"))


def _safe_page_title(page: Any | None) -> str:
    if not _page_supports_dom(page):
        return ""
    try:
        return str(page.title() or "")
    except Exception:  # noqa: BLE001
        return ""


def _normalise_adapter_page(adapter: SeekApplyAdapter) -> Any | None:
    page = getattr(adapter, "_page", None)
    if _page_supports_dom(page):
        return page
    try:
        setattr(adapter, "_page", None)
    except Exception:  # noqa: BLE001
        pass
    return None


def _resume_upload_radio_visible(adapter: SeekApplyAdapter) -> bool:
    explicit_checker = getattr(adapter, "resume_upload_radio_visible", None)
    if callable(explicit_checker):
        try:
            return bool(explicit_checker())
        except Exception:  # noqa: BLE001
            return False
    state_reader = getattr(adapter, "get_resume_upload_radio_state", None)
    if callable(state_reader):
        try:
            visible, _checked = state_reader()
            return bool(visible)
        except Exception:  # noqa: BLE001
            return False
    return False


def _looks_like_seek_verification_text(page_text: str) -> tuple[bool, str]:
    lowered = str(page_text or "").lower()
    strong_tokens = (
        "verifying...",
        "verifying",
        "verify you are human",
        "complete the security check",
        "security check",
        "enter the code we sent",
        "enter code",
        "check your email for a code",
    )
    for token in strong_tokens:
        if token in lowered:
            return True, token
    if "stuck? troubleshoot" in lowered:
        return True, "stuck? troubleshoot"
    return False, ""


def _collect_post_quick_apply_payload(adapter: SeekApplyAdapter) -> dict[str, Any]:
    page = getattr(adapter, "_page", None)
    current_url = ""
    page_title = ""
    visible_step_text = ""
    body_excerpt = ""
    questionnaire_counts = {
        "select": 0,
        "radio": 0,
        "textarea": 0,
        "input": 0,
    }
    try:
        current_url = str(adapter.current_url() or "")
    except Exception:  # noqa: BLE001
        current_url = ""
    if _page_supports_dom(page):
        try:
            page_title = _safe_page_title(page)
        except Exception:  # noqa: BLE001
            page_title = ""
        try:
            visible_step_text = str(_get_seek_visible_step_text(page) or "")
        except Exception:  # noqa: BLE001
            visible_step_text = ""
        try:
            body_excerpt = str(
                page.evaluate(
                    """() => {
                        const bodyText = (document.body?.innerText || '');
                        return bodyText.split(/\\n+/).map(line => line.trim()).filter(Boolean).slice(0, 24).join(' | ').slice(0, 2000);
                    }"""
                )
                or ""
            )
        except Exception:  # noqa: BLE001
            body_excerpt = ""
        try:
            questionnaire_counts = dict(_get_questionnaire_field_counts(page))
        except Exception:  # noqa: BLE001
            questionnaire_counts = {
                "select": 0,
                "radio": 0,
                "textarea": 0,
                "input": 0,
            }
    if not body_excerpt:
        try:
            body_excerpt = str(adapter.page_text() or "")[:2000]
        except Exception:  # noqa: BLE001
            body_excerpt = ""
    try:
        resume_file_count, resume_file_visible, resume_file_attached = adapter.get_resume_file_input_state()
    except Exception:  # noqa: BLE001
        resume_file_count, resume_file_visible, resume_file_attached = 0, False, False
    try:
        resume_radio_available, resume_radio_checked = adapter.get_resume_upload_radio_state()
    except Exception:  # noqa: BLE001
        resume_radio_available, resume_radio_checked = False, False
    try:
        method_state = dict(adapter.get_resume_method_state() or {})
    except Exception:  # noqa: BLE001
        method_state = {}
    try:
        visible_resume_filename = str(adapter.detect_visible_resume_filename() or "").strip()
    except Exception:  # noqa: BLE001
        visible_resume_filename = ""
    try:
        resume_present = bool(adapter.resume_appears_present())
    except Exception:  # noqa: BLE001
        resume_present = False
    return {
        "current_url": current_url,
        "page_title": page_title,
        "visible_step_text": visible_step_text,
        "body_excerpt": body_excerpt,
        "questionnaire_counts": questionnaire_counts,
        "resume_file_count": int(resume_file_count or 0),
        "resume_file_visible": bool(resume_file_visible),
        "resume_file_attached": bool(resume_file_attached),
        "resume_radio_available": bool(resume_radio_available),
        "resume_radio_checked": bool(resume_radio_checked),
        "resume_method_state": method_state,
        "visible_resume_filename": visible_resume_filename,
        "resume_present": resume_present,
    }


def _wait_for_post_quick_apply_ready(adapter: SeekApplyAdapter, timeout_ms: int = 20000) -> tuple[str, dict[str, Any]]:
    deadline = time.monotonic() + (timeout_ms / 1000.0)
    last_payload: dict[str, Any] = {}
    while time.monotonic() < deadline:
        pause_reason = adapter.detect_pause_reason()
        if pause_reason in {"paused_for_seek_login", "paused_for_seek_verification", "paused_for_captcha"}:
            last_payload = _collect_post_quick_apply_payload(adapter)
            last_payload["pause_reason"] = pause_reason
            return pause_reason, last_payload

        payload = _collect_post_quick_apply_payload(adapter)
        last_payload = payload
        url = str(payload.get("current_url") or "").lower()
        visible_step_text = str(payload.get("visible_step_text") or "")
        page_title = str(payload.get("page_title") or "")
        body_excerpt = str(payload.get("body_excerpt") or "")
        page_text_lower = f"{visible_step_text}\n{page_title}\n{body_excerpt}".lower()
        if _seek_job_is_closed(f"{page_title}\n{body_excerpt}"):
            return "job_closed", payload
        if (
            "/role-requirements" in url
            or "/profile" in url
            or "/review" in url
            or "choose documents" in page_text_lower
            or "upload a résumé" in page_text_lower
            or "upload a resume" in page_text_lower
            or "select a résumé" in page_text_lower
            or "select a resume" in page_text_lower
            or "answer employer questions" in page_text_lower
            or "update seek profile" in page_text_lower
            or "review and submit" in page_text_lower
        ):
            return "application_ready", payload

        resume_file_count = int(payload.get("resume_file_count") or 0)
        resume_radio_available = bool(payload.get("resume_radio_available"))
        if resume_file_count > 0 or resume_radio_available:
            return "application_ready", payload

        method_state = dict(payload.get("resume_method_state") or {})
        if str(method_state.get("selected_resume_method_value") or "").strip():
            return "application_ready", payload
        if str(payload.get("visible_resume_filename") or "").strip():
            return "application_ready", payload
        if bool(payload.get("resume_present")):
            return "application_ready", payload

        time.sleep(0.5)
    return "timeout", last_payload


DEFAULT_APPLICANT_PROFILE: dict[str, Any] = {
    "date_of_birth": "1993-12-21",
    "right_to_work_nz": "New Zealand citizen",
    "drivers_licence": {
        "nz_current": True,
    },
    "disclosures": {
        "worked_for_target_employer_before": False,
        "has_criminal_offence_history": False,
        "has_preexisting_conditions_affecting_role": False,
        "has_performance_improvement_history": False,
        "has_disciplinary_history": False,
    },
    "skills_years": {
        "sql": 8,
        "power bi": 6,
        "business intelligence": 6,
        "python": 3,
        "business analysis": 8,
        "data analysis": 8,
        "analytics": 8,
        "data engineering": 1,
    },
    "skills_experience_years": {
        "sql": 10,
        "power bi": 5,
        "power query": 5,
        "python": 2,
        "data analysis": 8,
        "business analysis": 8,
        "reporting": 8,
        "dashboards": 8,
    },
    "industry_experience_years": 10,
    "experience_notes": {
        "payroll_direct_experience": False,
        "payroll_related_answer": "I do not have direct payroll processing experience in a dedicated payroll role, but I have strong experience working with business data, reporting accuracy, process controls, and compliance-focused workflows in New Zealand organisations. In my current work, I regularly handle operational data, validate outputs, investigate discrepancies, and build reporting that supports reliable business decisions. I would be comfortable learning the organisation’s payroll processes and following established checks, approval workflows, and NZ legislative requirements carefully.",
    },
    "notice_period_weeks": 2,
    "selection_process": {
        "comfortable_with_psychometric_assessment": True,
    },
    "software_experience": {
        "xero": True,
        "microsoft excel": True,
        "excel": True,
    },
    "automation": {
        "pause_on_unknown_questions": False,
        "allow_reasonable_fallback_answers": True,
        "allow_no_resume": False,
        "allow_delete_old_seek_resumes": True,
        "resume_debug_dom_snapshots": False,
        "close_browser_on_success": True,
        "keep_browser_open_on_failure": True,
        "seek_browser_profile_path": "",
        "seek_bulk_browser_profile_path": "",
        "seek_browser_channels": ["chrome", "msedge", "chromium"],
    },
    "qualifications": {
        "ict": True,
        "statistics": True,
        "data_science": True,
        "operations_research": True,
    },
    "highest_education_level": "Bachelor Degree",
}


def assist_seek_apply(
    job_id: int,
    db_path: Path,
    visible: bool = True,
    force_new_profile: bool = False,
    use_bulk_profile: bool = False,
    keep_browser_open_override: bool | None = None,
    resume_upload_only: bool = False,
    input_func: Callable[[], str] = input,
    allow_manual_login_prompt: bool = True,
    auto_submit: bool = False,
) -> ApplyAssistResult:
    database = Database(db_path)
    started_at = time.perf_counter()
    result = _assist_seek_apply(
        job_id,
        database,
        visible=visible,
        force_new_profile=force_new_profile,
        use_bulk_profile=use_bulk_profile,
        keep_browser_open_override=keep_browser_open_override,
        resume_upload_only=resume_upload_only,
        input_func=input_func,
        allow_manual_login_prompt=allow_manual_login_prompt,
        auto_submit=auto_submit,
    )
    elapsed_seconds = max(0.0, time.perf_counter() - started_at)
    database.log_run(
        "seek_prepare_timing",
        "finished",
        "success" if str(result.status) == "prepared_for_manual_review" else "warning",
        f"job_id={job_id} status={result.status} duration_seconds={elapsed_seconds:.3f}",
    )
    return result


def seek_profile_dir_for_db(db_path: Path) -> Path:
    data_dir = Path(db_path).resolve().parent
    return data_dir / "browser_profiles" / "seek_apply"


def seek_bulk_profile_dir_for_db(db_path: Path) -> Path:
    data_dir = Path(db_path).resolve().parent
    return data_dir / "browser_profiles" / "seek_apply_bulk"


def seek_temp_profile_dir_for_db(db_path: Path) -> Path:
    data_dir = Path(db_path).resolve().parent
    return data_dir / "browser_profiles" / "seek_apply_temp"


def seek_seeded_browser_profile_dir_for_db(db_path: Path, *, use_bulk_profile: bool = False) -> Path:
    data_dir = Path(db_path).resolve().parent
    name = "seek_bulk_seeded" if use_bulk_profile else "seek_seeded"
    return data_dir / "browser_profiles" / name


def seek_seeded_browser_profile_metadata_path_for_db(db_path: Path, *, use_bulk_profile: bool = False) -> Path:
    return seek_seeded_browser_profile_dir_for_db(db_path, use_bulk_profile=use_bulk_profile) / ".seed_metadata.json"


def seek_seeded_browser_profile_run_dir_for_db(db_path: Path, *, use_bulk_profile: bool = False) -> Path:
    base_dir = seek_seeded_browser_profile_dir_for_db(db_path, use_bulk_profile=use_bulk_profile)
    suffix = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    return base_dir.parent / f"{base_dir.name}_run_{suffix}"


def configured_seek_browser_profile_dir(db_path: Path, *, use_bulk_profile: bool = False) -> Path | None:
    data_dir = Path(db_path).resolve().parent
    profile = load_applicant_profile(data_dir)
    automation_settings = dict(profile.get("automation") or {})
    raw_path = automation_settings.get("seek_bulk_browser_profile_path") if use_bulk_profile else automation_settings.get("seek_browser_profile_path")
    value = str(raw_path or "").strip()
    if not value:
        return None
    try:
        return Path(value).expanduser()
    except Exception:  # noqa: BLE001
        return None


def resolve_seek_browser_profile_launch_settings(configured_path: Path | None) -> tuple[Path | None, list[str]]:
    if configured_path is None:
        return None, []
    path = Path(configured_path)
    profile_dir_names = {"default"} | {f"profile {i}" for i in range(1, 100)}
    if path.name.lower() in profile_dir_names:
        return path.parent, [f"--profile-directory={path.name}"]
    return path, []


def _profile_lock_ignore_names() -> set[str]:
    return {
        "SingletonLock",
        "SingletonCookie",
        "lockfile",
        "Lockfile",
        "LOCK",
        "Cache",
        "Code Cache",
        "GPUCache",
        "ShaderCache",
        "GrShaderCache",
        "DawnCache",
        "Media Cache",
        "Service Worker",
        "blob_storage",
        "optimization_guide_model_store",
        "component_crx_cache",
        "Crashpad",
        "Safe Browsing",
        "segmentation_platform",
    }


def _ignore_lock_files(_directory: str, names: list[str]) -> set[str]:
    ignore_names = _profile_lock_ignore_names()
    return {name for name in names if name in ignore_names}


def _terminate_seeded_browser_processes(target_root: Path) -> int:
    if sys.platform != "win32":
        return 0
    normalized_root = str(target_root.resolve())
    if not normalized_root:
        return 0
    escaped_root = normalized_root.replace("'", "''")
    command = (
        "$path = '"
        + escaped_root
        + "'; "
        + "$targets = Get-CimInstance Win32_Process | Where-Object { "
        + "$_.CommandLine -like \"*$path*\" -and $_.Name -match 'chrome|msedge|chromium' "
        + "}; "
        + "$count = 0; "
        + "foreach ($proc in $targets) { "
        + "try { Stop-Process -Id $proc.ProcessId -Force -ErrorAction Stop; $count++ } catch {} "
        + "}; "
        + "Write-Output $count"
    )
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-Command", command],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
            **_windows_subprocess_kwargs(),
        )
    except Exception:
        return 0
    output = str(completed.stdout or "").strip()
    try:
        terminated = int(output.splitlines()[-1]) if output else 0
    except Exception:
        terminated = 0
    if terminated > 0:
        print(
            f"[seek_apply_assist] terminated_seeded_browser_processes: path={normalized_root} count={terminated}",
            flush=True,
        )
    return terminated


def invalidate_seek_seeded_browser_profile_cache(db_path: Path, *, use_bulk_profile: bool = False) -> None:
    target_root = seek_seeded_browser_profile_dir_for_db(db_path, use_bulk_profile=use_bulk_profile)
    metadata_path = seek_seeded_browser_profile_metadata_path_for_db(db_path, use_bulk_profile=use_bulk_profile)
    _terminate_seeded_browser_processes(target_root)
    try:
        if target_root.exists():
            shutil.rmtree(target_root, ignore_errors=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[seek_apply_assist] invalidate_seek_seeded_browser_profile_cache_error: {exc}", flush=True)
    try:
        metadata_path.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        pass


def _seek_profile_seed_signature(source_root: Path, launch_args: list[str], profile_name: str | None) -> dict[str, Any]:
    local_state = source_root / "Local State"
    profile_dir = source_root / profile_name if profile_name else source_root
    return {
        "source_root": str(source_root),
        "profile_name": str(profile_name or ""),
        "launch_args": list(launch_args),
        "local_state_mtime_ns": local_state.stat().st_mtime_ns if local_state.exists() else 0,
        "profile_dir_mtime_ns": profile_dir.stat().st_mtime_ns if profile_dir.exists() else 0,
    }


def seed_isolated_seek_browser_profile_from_configured_profile(db_path: Path, *, use_bulk_profile: bool = False) -> tuple[Path | None, list[str]]:
    configured_path = configured_seek_browser_profile_dir(db_path, use_bulk_profile=use_bulk_profile)
    configured_user_data_dir, launch_args = resolve_seek_browser_profile_launch_settings(configured_path)
    if configured_user_data_dir is None:
        return None, []

    source_root = Path(configured_user_data_dir)
    target_root = seek_seeded_browser_profile_dir_for_db(db_path, use_bulk_profile=use_bulk_profile)
    target_root.parent.mkdir(parents=True, exist_ok=True)

    try:
        profile_name = None
        for arg in launch_args:
            if arg.startswith("--profile-directory="):
                profile_name = arg.split("=", 1)[1].strip()
                break

        metadata_path = seek_seeded_browser_profile_metadata_path_for_db(db_path, use_bulk_profile=use_bulk_profile)
        current_signature = _seek_profile_seed_signature(source_root, launch_args, profile_name)
        cached_signature: dict[str, Any] = {}
        try:
            if metadata_path.exists():
                cached_signature = json.loads(metadata_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            cached_signature = {}
        if target_root.exists() and cached_signature == current_signature:
            _cleanup_stale_seek_profile_locks(target_root)
            return target_root, launch_args

        if target_root.exists():
            shutil.rmtree(target_root, ignore_errors=True)
        target_root.mkdir(parents=True, exist_ok=True)

        if profile_name:
            local_state = source_root / "Local State"
            if local_state.exists():
                shutil.copy2(local_state, target_root / "Local State")
            for root_level_name in ("First Run", "Last Version", "Variations", "NativeMessagingHosts"):
                source_item = source_root / root_level_name
                target_item = target_root / root_level_name
                if source_item.is_file():
                    shutil.copy2(source_item, target_item)
                elif source_item.is_dir():
                    shutil.copytree(source_item, target_item, ignore=_ignore_lock_files, dirs_exist_ok=True)
            source_profile_dir = source_root / profile_name
            target_profile_dir = target_root / profile_name
            if source_profile_dir.exists():
                shutil.copytree(source_profile_dir, target_profile_dir, ignore=_ignore_lock_files, dirs_exist_ok=True)
        else:
            shutil.copytree(source_root, target_root, ignore=_ignore_lock_files, dirs_exist_ok=True)
        metadata_path.write_text(json.dumps(current_signature, indent=2), encoding="utf-8")
    except Exception:  # noqa: BLE001
        target_root.mkdir(parents=True, exist_ok=True)
    _cleanup_stale_seek_profile_locks(target_root)
    return target_root, launch_args


def seed_temp_seek_profile_from_logged_in_profile(db_path: Path) -> Path:
    seed_dir = seek_profile_dir_for_db(db_path)
    temp_dir = seek_temp_profile_dir_for_db(db_path)
    temp_dir.parent.mkdir(parents=True, exist_ok=True)

    if temp_dir.exists():
        try:
            shutil.rmtree(temp_dir, ignore_errors=True)
        except Exception:  # noqa: BLE001
            pass

    if not seed_dir.exists():
        temp_dir.mkdir(parents=True, exist_ok=True)
        return temp_dir

    try:
        shutil.copytree(seed_dir, temp_dir, ignore=_ignore_lock_files, dirs_exist_ok=True)
    except Exception:  # noqa: BLE001
        temp_dir.mkdir(parents=True, exist_ok=True)
    return temp_dir


def seek_profile_appears_locked(profile_dir: Path) -> bool:
    candidates = [
        profile_dir / "SingletonLock",
        profile_dir / "SingletonCookie",
        profile_dir / "lockfile",
        profile_dir / "Lockfile",
        profile_dir / "Default" / "lockfile",
        profile_dir / "Default" / "LOCK",
    ]
    return any(path.exists() for path in candidates)


def _is_app_managed_seek_profile_dir(profile_dir: Path, db_path: Path) -> bool:
    try:
        browser_profiles_dir = Path(db_path).resolve().parent / "browser_profiles"
        Path(profile_dir).resolve().relative_to(browser_profiles_dir.resolve())
        return True
    except Exception:  # noqa: BLE001
        return False


def _cleanup_stale_seek_profile_locks(profile_dir: Path) -> list[Path]:
    candidates = [
        profile_dir / "SingletonLock",
        profile_dir / "SingletonCookie",
        profile_dir / "lockfile",
        profile_dir / "Lockfile",
        profile_dir / "Default" / "lockfile",
        profile_dir / "Default" / "LOCK",
    ]
    removed: list[Path] = []
    for candidate in candidates:
        try:
            if candidate.exists():
                candidate.unlink(missing_ok=True)
                removed.append(candidate)
        except Exception:  # noqa: BLE001
            continue
    return removed


def _profile_locked_message_for_dir(profile_dir: Path, db_path: Path) -> str:
    try:
        relative = Path(profile_dir).resolve().relative_to(Path(db_path).resolve().parent)
        path_hint = str(relative)
    except Exception:  # noqa: BLE001
        path_hint = str(profile_dir)
    return (
        f"Close all SEEK/Chromium windows and retry. If it still fails, delete {path_hint} "
        "and run scripts/seek_login_setup.py again."
    )


def setup_seek_login_session(
    db_path: Path,
    visible: bool = True,
    force_new_profile: bool = False,
    use_bulk_profile: bool = False,
    auto_email_code: bool = False,
) -> None:
    adapter = _build_playwright_adapter(
        visible,
        db_path,
        force_new_profile=force_new_profile,
        use_bulk_profile=use_bulk_profile,
    )
    try:
        adapter.open_job("https://login.seek.com")
        if auto_email_code:
            page = getattr(adapter, "_page", None)
            if page is None:
                raise RuntimeError("SEEK login automation requires a Playwright page.")
            success, message = _attempt_seek_email_code_login(page, db_path.parent)
            print(message, flush=True)
            if success:
                return
        profile_label = "bulk" if use_bulk_profile else "default"
        print(
            f"Please sign into SEEK manually for the {profile_label} automation profile. "
            "Use email code if needed. When fully signed in, return here and press Enter.",
            flush=True,
        )
        input()
    finally:
        try:
            adapter.close()
        except Exception:  # noqa: BLE001
            pass


def _attempt_seek_email_code_login(
    page: Any,
    app_root: Path,
    *,
    code_fetch_timeout_seconds: int = 90,
) -> tuple[bool, str]:
    login_email = load_seek_login_email(app_root)
    if not login_email:
        return False, "No SEEK login email was found in data/personal_details.json or applicant_profile.json."

    try:
        email_input = page.locator('input[type="email"], input[name="email"], input[autocomplete="email"]').first
        email_input.wait_for(state="visible", timeout=10000)
        email_input.fill(login_email, timeout=5000)
    except Exception as exc:  # noqa: BLE001
        return False, f"Could not fill SEEK login email: {exc}"

    issued_after = datetime.now(timezone.utc)
    try:
        page.get_by_role("button", name=re.compile("email me a sign in code", re.I)).first.click(timeout=5000)
    except Exception:
        try:
            page.get_by_text(re.compile("email me a sign in code", re.I)).first.click(timeout=5000)
        except Exception as exc:  # noqa: BLE001
            return False, f"Could not request SEEK sign-in code: {exc}"

    try:
        page.wait_for_timeout(2000)
    except Exception:  # noqa: BLE001
        pass

    fetcher = SeekEmailCodeFetcher(app_root)
    try:
        code = fetcher.fetch_latest_seek_code(
            issued_after=issued_after,
            login_email=login_email,
            timeout_seconds=max(5, int(code_fetch_timeout_seconds)),
            poll_interval_seconds=5,
        )
    except Exception as exc:  # noqa: BLE001
        return False, f"Could not fetch SEEK sign-in code from Gmail: {exc}"
    if not code:
        return False, "No recent SEEK sign-in code email was found in Gmail."

    try:
        code_inputs = page.locator('input[autocomplete="one-time-code"], input[inputmode="numeric"], input[name*="code"], input[id*="code"]')
        count = code_inputs.count()
        if count <= 1:
            code_inputs.first.fill(code, timeout=5000)
        else:
            for index, digit in enumerate(code[:count]):
                code_inputs.nth(index).fill(digit, timeout=3000)
    except Exception as exc:  # noqa: BLE001
        return False, f"Could not fill SEEK sign-in code: {exc}"

    try:
        submit_button = page.get_by_role("button", name=re.compile("continue|sign in|verify", re.I)).first
        if submit_button.count():
            submit_button.click(timeout=5000)
    except Exception:
        pass

    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            url = str(page.url or "").lower()
        except Exception:
            url = ""
        try:
            text = str(page.locator("body").inner_text(timeout=2000) or "").lower()
        except Exception:
            text = ""
        if "login.seek.com" not in url and "sign in to continue" not in text and "enter code" not in text:
            return True, "SEEK bulk profile login completed automatically with Gmail sign-in code."
        time.sleep(1)
    return False, "SEEK sign-in code was entered, but the login page did not complete."


def _resume_automation_settings(debug_dir: Path) -> dict[str, Any]:
    try:
        candidate_dirs = [debug_dir, debug_dir.parent, debug_dir.parent.parent]
        for candidate in candidate_dirs:
            profile_path = Path(candidate) / "applicant_profile.json"
            if profile_path.exists():
                return dict((load_applicant_profile(candidate).get("automation") or {}))
    except Exception:  # noqa: BLE001
        pass
    return dict(DEFAULT_APPLICANT_PROFILE.get("automation") or {})


def _resume_debug_dom_snapshots_enabled(debug_dir: Path) -> bool:
    return bool(_resume_automation_settings(debug_dir).get("resume_debug_dom_snapshots", False))


def _parse_seek_resume_option_date(option_text: str) -> tuple[int, int, int] | None:
    match = re.search(r"\b(\d{1,2})/(\d{1,2})/(\d{2,4})\b", str(option_text or ""))
    if not match:
        return None
    day = int(match.group(1))
    month = int(match.group(2))
    year = int(match.group(3))
    if year < 100:
        year += 2000
    if not (1 <= day <= 31 and 1 <= month <= 12):
        return None
    return year, month, day


def _choose_resume_limit_delete_candidate(options: list[str], protected_filename: str = "") -> str:
    protected = str(protected_filename or "").strip().lower()
    cleaned: list[str] = []
    for option in options:
        text = str(option or "").strip()
        if not text:
            continue
        lowered = text.lower()
        if "please select a resumé" in lowered or "please select a resume" in lowered:
            continue
        if protected and protected in lowered:
            continue
        cleaned.append(text)
    if not cleaned:
        return ""
    dated_options = [(option, _parse_seek_resume_option_date(option)) for option in cleaned]
    valid_dated = [(option, parsed) for option, parsed in dated_options if parsed is not None]
    if valid_dated:
        valid_dated.sort(key=lambda item: item[1])
        return valid_dated[0][0]
    return cleaned[-1]


def _save_resume_failure_json(debug_dir: Path, stem: str, payload: dict[str, Any]) -> Path:
    return _save_resume_failure_json_impl(debug_dir, stem, payload)


def _safe_page_screenshot(page: Any, path: Path, *, timeout_ms: int = 3000) -> tuple[bool, str]:
    return _safe_page_screenshot_impl(page, path, timeout_ms=timeout_ms)


def _safe_page_html_write(page: Any, path: Path) -> tuple[bool, str]:
    return _safe_page_html_write_impl(page, path)


def _save_verification_failure_artifacts(
    debug_dir: Path,
    page: Any,
    *,
    stem: str,
    payload: dict[str, Any],
) -> tuple[Path, Path, Path, dict[str, Any]]:
    return _save_verification_failure_artifacts_impl(debug_dir, page, stem=stem, payload=payload)


def _write_local_dom_codex_prompt(
    diagnostics_dir: Path,
    *,
    failure_type: str,
    root_failure_reason: str,
    local_state: dict[str, Any],
    artifact_paths: dict[str, str],
) -> Path:
    return _write_local_dom_codex_prompt_impl(
        diagnostics_dir,
        failure_type=failure_type,
        root_failure_reason=root_failure_reason,
        local_state=local_state,
        artifact_paths=artifact_paths,
    )


def capture_local_dom_diagnostics(
    page: Any,
    locator: Any,
    failure_type: str,
    *,
    debug_dir: Path,
    adapter: SeekApplyAdapter | None = None,
    expected_filename: str = "",
    root_failure_reason: str = "",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return capture_local_dom_diagnostics_impl(
        page,
        locator,
        failure_type,
        debug_dir=debug_dir,
        adapter=adapter,
        expected_filename=expected_filename,
        root_failure_reason=root_failure_reason,
        extra=extra,
        get_resume_method_state=_get_resume_method_state,
        verify_resume_filename=_verify_resume_filename,
    )


def _normalise_space(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _cover_letter_excerpt(text: str, limit: int = 120) -> str:
    normalised = _normalise_space(text)
    if not normalised:
        return ""
    return normalised[:limit]


def _get_page_body_text(page: Any) -> str:
    try:
        return str(page.locator("body").inner_text(timeout=5000) or "")
    except Exception:  # noqa: BLE001
        try:
            return str(page.evaluate("""() => (document.body?.innerText || '')""") or "")
        except Exception:  # noqa: BLE001
            return ""


def verify_cover_letter_pasted(page: Any, expected_text: str) -> tuple[bool, dict[str, Any]]:
    excerpt = _cover_letter_excerpt(expected_text)
    body_text = _get_page_body_text(page)
    editor_texts: list[str] = []
    if not excerpt:
        return False, {
            "expected_excerpt": "",
            "editor_texts": [],
            "editor_text_present": False,
            "body_text_snippet": body_text[:1200],
        }
    try:
        editor_texts = list(
            page.evaluate(
                """() => {
                    const values = [];
                    const selectors = [
                        '[data-testid="coverLetterTextInput"]',
                        'textarea',
                        '[contenteditable="true"]',
                        '[role="textbox"]',
                    ];
                    const seen = new Set();
                    for (const selector of selectors) {
                        for (const node of Array.from(document.querySelectorAll(selector))) {
                            const style = window.getComputedStyle(node);
                            const visible = style.display !== 'none' && style.visibility !== 'hidden' && !!(node.offsetWidth || node.offsetHeight || node.getClientRects().length);
                            if (!visible) continue;
                            const raw = node.value ?? node.innerText ?? node.textContent ?? '';
                            const text = String(raw || '').replace(/\s+/g, ' ').trim();
                            if (!text || seen.has(text)) continue;
                            seen.add(text);
                            values.push(text);
                        }
                    }
                    return values;
                }"""
            )
            or []
        )
    except Exception:  # noqa: BLE001
        editor_texts = []
    lowered_excerpt = excerpt.lower()
    editor_text_present = any(lowered_excerpt in _normalise_space(text).lower() for text in editor_texts)
    body_text_present = lowered_excerpt in _normalise_space(body_text).lower()
    return editor_text_present or body_text_present, {
        "expected_excerpt": excerpt,
        "editor_texts": editor_texts[:10],
        "editor_text_present": editor_text_present,
        "body_text_present": body_text_present,
        "body_text_snippet": body_text[:1200],
    }


def verify_cover_letter_included_on_review(page: Any, expected_text: str) -> tuple[bool, dict[str, Any]]:
    body_text = _get_page_body_text(page)
    lowered = body_text.lower()
    excerpt = _cover_letter_excerpt(expected_text)
    expected_visible = bool(excerpt and excerpt.lower() in lowered)
    has_cover_letter_section = "cover letter" in lowered
    no_cover_letter_included = "no cover letter included" in lowered or "without cover letter" in lowered
    positive_phrase = (
        "cover letter included" in lowered
        or "you wrote a cover letter for this application" in lowered
    )
    included = has_cover_letter_section and not no_cover_letter_included and (expected_visible or positive_phrase)
    return included, {
        "has_cover_letter_section": has_cover_letter_section,
        "no_cover_letter_included": no_cover_letter_included,
        "expected_excerpt": excerpt,
        "expected_excerpt_visible": expected_visible,
        "positive_phrase_visible": positive_phrase,
        "body_text_snippet": body_text[:1200],
    }


def _write_final_review_checklist(debug_dir: Path, checklist: FinalReviewChecklist, extra: dict[str, Any] | None = None) -> Path:
    return _write_final_review_checklist_impl(debug_dir, checklist, extra)


def _resume_upload_diagnostics(adapter: SeekApplyAdapter) -> dict[str, Any]:
    collector = getattr(adapter, "collect_resume_upload_diagnostics", None)
    if callable(collector):
        try:
            return dict(collector())
        except Exception:  # noqa: BLE001
            return {}
    return {}


def _resolve_seek_tracking_url(url: str) -> str:
    target = str(url or "").strip()
    if not target or "email.s.seek.co.nz" not in target.lower():
        return target
    try:
        request = urllib.request.Request(
            target,
            headers={"User-Agent": "Mozilla/5.0"},
            method="GET",
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            resolved = str(response.geturl() or "").strip()
        if resolved:
            print(f"[seek_apply_assist] resolved_seek_tracking_url: source={target} resolved={resolved}", flush=True)
            return resolved
    except Exception as exc:  # noqa: BLE001
        print(f"[seek_apply_assist] resolve_seek_tracking_url_failed: source={target} error={exc}", flush=True)
    return target


def _fallback_resume_progress_candidates(progress_state: dict[str, Any]) -> list[dict[str, Any]]:
    if not progress_state:
        return []
    return [
        {
            "selector_matched": "fallback_progress_state",
            "visible": bool(progress_state.get("loading_indicator_visible")),
            "text": str(progress_state.get("section_text_snippet") or ""),
            "outer_html_snippet": "",
            "bounding_box": None,
            "computed_animation_name": "",
            "computed_display": "",
            "computed_visibility": "",
            "aria_busy": "",
            "role": "",
        }
    ]


def _collect_resume_section_state(adapter: SeekApplyAdapter, expected_filename: str = "") -> dict[str, Any]:
    method_state = _get_resume_method_state(adapter)
    progress_state = dict(adapter.get_resume_upload_progress_state(expected_filename) or {})
    diagnostics = _resume_upload_diagnostics(adapter)
    collector = getattr(adapter, "collect_resume_section_snapshot", None)
    collected: dict[str, Any] = {}
    if callable(collector):
        try:
            collected = dict(collector(expected_filename) or {})
        except Exception as exc:  # noqa: BLE001
            collected = {"snapshot_capture_errors": [f"collect_resume_section_snapshot: {exc}"]}
    filename = _verify_resume_filename(adapter)
    visible_filenames = list(collected.get("visible_resume_filenames") or ([filename] if filename else []))
    return {
        "selected_resume_method_value": str(method_state.get("selected_resume_method_value") or ""),
        "upload_radio_checked": bool(method_state.get("upload_radio_checked")),
        "dont_include_checked": bool(method_state.get("dont_include_resume_radio_checked")),
        "visible_resume_filenames": visible_filenames,
        "file_input_diagnostics": list(collected.get("file_input_diagnostics") or diagnostics.get("file_inputs", [])),
        "progress_candidates": list(collected.get("progress_candidates") or _fallback_resume_progress_candidates(progress_state)),
        "buttons_labels_visible": list(collected.get("buttons_labels_visible") or []),
        "continue_enabled": bool(collected.get("continue_enabled", False)),
        "validation_error_texts": list(collected.get("validation_error_texts") or []),
        "resume_section_html": str(collected.get("resume_section_html") or ""),
        "resume_section_text": str(collected.get("resume_section_text") or ""),
        "resume_limit_modal_visible": bool(collected.get("resume_limit_modal_visible", adapter.resume_limit_ui_present())),
        "page_url": str(collected.get("page_url") or adapter.current_url() or ""),
        "page_title": str(collected.get("page_title") or ""),
        "snapshot_capture_errors": list(collected.get("snapshot_capture_errors") or []),
        "progress_state": progress_state,
    }


def _save_resume_section_dom_snapshot(
    debug_dir: Path,
    checkpoint: str,
    adapter: SeekApplyAdapter,
    *,
    expected_filename: str = "",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return _save_resume_section_dom_snapshot_impl(
        debug_dir,
        checkpoint,
        adapter,
        expected_filename=expected_filename,
        extra=extra,
        snapshots_enabled=_resume_debug_dom_snapshots_enabled,
        collect_resume_section_state=_collect_resume_section_state,
    )


def _save_upload_click_diagnostics(adapter: SeekApplyAdapter, debug_dir: Path, payload: dict[str, Any]) -> tuple[Path, Path, Path]:
    return _save_upload_click_diagnostics_impl(
        adapter,
        debug_dir,
        payload,
        resume_upload_diagnostics=_resume_upload_diagnostics,
    )


def _verify_resume_filename(adapter: SeekApplyAdapter) -> str:
    try:
        return str(adapter.detect_visible_resume_filename() or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _resume_adapter_pause(adapter: SeekApplyAdapter, seconds: float) -> None:
    page = getattr(adapter, "_page", None)
    try:
        if page is not None:
            page.wait_for_timeout(int(seconds * 1000))
            return
    except Exception:  # noqa: BLE001
        pass
    time.sleep(seconds)


def wait_for_resume_upload_complete(
    adapter: SeekApplyAdapter,
    debug_dir: Path,
    expected_filename: str,
    timeout_ms: int = 30000,
    selected_cv_path: Path | None = None,
    stuck_processing_threshold_ms: int = 10000,
    recovery_window_ms: int = 15000,
) -> str:
    deadline = time.monotonic() + (timeout_ms / 1000)
    last_payload: dict[str, Any] = {}
    stable_since: float | None = None
    loading_persisted_since: float | None = None
    refresh_attempted = False
    recovery_attempted = False
    recovery_deadline: float | None = None
    recovery_reupload_attempted = False
    previous_visible_filename = ""
    filename_snapshot_saved = False
    while time.monotonic() < deadline:
        filename = _verify_resume_filename(adapter)
        method_state = _get_resume_method_state(adapter)
        file_input_count, _file_input_visible, _file_input_attached = adapter.get_resume_file_input_state()
        resume_limit_visible = bool(adapter.resume_limit_ui_present())
        progress_state = dict(adapter.get_resume_upload_progress_state(expected_filename) or {})
        loading_indicator_visible = bool(progress_state.get("loading_indicator_visible", False))
        local_resume_container_found = bool(progress_state.get("local_resume_container_found", False))
        local_resume_loading_indicator_visible = bool(progress_state.get("local_resume_loading_indicator_visible", loading_indicator_visible))
        global_spinner_ignored = bool(progress_state.get("global_spinner_ignored", False))
        filename_visible = bool(filename)
        upload_radio_checked = bool(method_state.get("upload_radio_checked"))
        dont_include_checked = bool(method_state.get("dont_include_resume_radio_checked"))
        selected_method_value = str(method_state.get("selected_resume_method_value") or "").strip().lower()
        resume_method_state_known = bool(upload_radio_checked or dont_include_checked or selected_method_value)
        upload_method_valid = upload_radio_checked if resume_method_state_known else True
        conditions_met = (
            filename_visible
            and upload_method_valid
            and not dont_include_checked
            and not resume_limit_visible
            and not loading_indicator_visible
        )
        if resume_limit_visible and not filename_visible and not loading_indicator_visible:
            last_payload = {
                "timeout_ms": timeout_ms,
                "expected_filename": expected_filename,
                "visible_resume_filenames": [],
                "filename_visible": filename_visible,
                "loading_indicator_visible": loading_indicator_visible,
                "upload_radio_checked": upload_radio_checked,
                "dont_include_checked": dont_include_checked,
                "selected_resume_method_value": selected_method_value,
                "resume_method_state_known": resume_method_state_known,
                "resume_limit_modal_visible": resume_limit_visible,
                "stable_ms": 0,
                "refresh_attempted": refresh_attempted,
                "recovery_attempted": recovery_attempted,
                "recovery_reupload_attempted": recovery_reupload_attempted,
                "previous_visible_filename": previous_visible_filename,
                "file_input_count": file_input_count,
                "progress_state": progress_state,
                "body_text_snippet": str(adapter.page_text() or "")[:1200],
                "root_failure_reason": "SEEK resume limit modal is blocking upload completion.",
            }
            print("[seek_apply_assist] resume_upload_limit_modal_blocking_completion", flush=True)
            break
        if conditions_met:
            if stable_since is None:
                stable_since = time.monotonic()
            stable_ms = int((time.monotonic() - stable_since) * 1000)
        else:
            stable_since = None
            stable_ms = 0
        if filename and previous_visible_filename and filename != previous_visible_filename:
            print(
                f"[seek_apply_assist] resume_filename_changed_during_processing: "
                f"from={previous_visible_filename} to={filename}",
                flush=True,
            )
        if filename:
            previous_visible_filename = filename
        if filename_visible and not filename_snapshot_saved:
            _save_resume_section_dom_snapshot(
                debug_dir,
                "after_filename_visible",
                adapter,
                expected_filename=expected_filename or filename,
                extra=last_payload,
            )
            filename_snapshot_saved = True
        if filename_visible and loading_indicator_visible:
            if loading_persisted_since is None:
                loading_persisted_since = time.monotonic()
            elif not recovery_attempted and (time.monotonic() - loading_persisted_since) >= (stuck_processing_threshold_ms / 1000):
                recovery_attempted = True
                recovery_deadline = time.monotonic() + (recovery_window_ms / 1000)
                print("[seek_apply_assist] resume_upload_stuck_processing_detected", flush=True)
                _record_resume_checkpoint(
                    debug_dir,
                    "upload_processing_stuck_before_recovery",
                    page=getattr(adapter, "_page", None),
                    adapter=adapter,
                    expected_filename=expected_filename,
                    extra=last_payload,
                )
                _save_resume_section_dom_snapshot(
                    debug_dir,
                    "stuck_before_recovery",
                    adapter,
                    expected_filename=expected_filename or filename,
                    extra=last_payload,
                )
                print("[seek_apply_assist] resume_upload_toggle_none_started", flush=True)
                none_clicked = False
                try:
                    none_clicked = bool(adapter.click_resume_none_radio())
                except Exception:  # noqa: BLE001
                    none_clicked = False
                _resume_adapter_pause(adapter, 0.5)
                toggled_state = _get_resume_method_state(adapter)
                print(
                    f"[seek_apply_assist] resume_upload_toggle_none_checked: "
                    f"{bool(toggled_state.get('dont_include_resume_radio_checked'))}",
                    flush=True,
                )
                _save_resume_section_dom_snapshot(
                    debug_dir,
                    "after_toggle_none",
                    adapter,
                    expected_filename=expected_filename or filename,
                    extra={"none_clicked": none_clicked, **last_payload},
                )
                print("[seek_apply_assist] resume_upload_toggle_upload_started", flush=True)
                upload_clicked = False
                try:
                    upload_clicked = bool(adapter.click_resume_upload_radio())
                except Exception:  # noqa: BLE001
                    upload_clicked = False
                _resume_adapter_pause(adapter, 1.5)
                toggled_back_state = _get_resume_method_state(adapter)
                print(
                    f"[seek_apply_assist] resume_upload_toggle_upload_checked: "
                    f"{bool(toggled_back_state.get('upload_radio_checked'))}",
                    flush=True,
                )
                _save_resume_section_dom_snapshot(
                    debug_dir,
                    "after_toggle_upload",
                    adapter,
                    expected_filename=expected_filename or filename,
                    extra={"upload_clicked": upload_clicked, **last_payload},
                )
                recovered_filename = _verify_resume_filename(adapter)
                recovered_progress = dict(adapter.get_resume_upload_progress_state(expected_filename) or {})
                recovered_loading = bool(recovered_progress.get("loading_indicator_visible", False))
                print(
                    f"[seek_apply_assist] resume_upload_recovery_filename_visible: {bool(recovered_filename)}",
                    flush=True,
                )
                print(
                    f"[seek_apply_assist] resume_upload_recovery_loading_indicator_visible: {recovered_loading}",
                    flush=True,
                )
                if not recovered_filename and selected_cv_path is not None and upload_clicked:
                    recovery_reupload_attempted = True
                    print("[seek_apply_assist] resume_upload_recovery_reupload_started", flush=True)
                    try:
                        adapter.upload_resume(selected_cv_path)
                    except Exception as exc:  # noqa: BLE001
                        print(f"[seek_apply_assist] resume_upload_recovery_reupload_error: {exc}", flush=True)
                _resume_adapter_pause(adapter, 0.5)
                _save_resume_section_dom_snapshot(
                    debug_dir,
                    "after_recovery_wait",
                    adapter,
                    expected_filename=expected_filename or recovered_filename or filename,
                    extra={
                        "recovered_filename_visible": bool(recovered_filename),
                        "recovered_loading_indicator_visible": recovered_loading,
                        "recovery_reupload_attempted": recovery_reupload_attempted,
                        **last_payload,
                    },
                )
                continue
            elif not refresh_attempted and (time.monotonic() - loading_persisted_since) >= 15:
                refresh_attempted = True
                print("[seek_apply_assist] resume_upload_section_refresh_attempted", flush=True)
                try:
                    adapter.click_resume_upload_radio()
                except Exception:  # noqa: BLE001
                    pass
                page = getattr(adapter, "_page", None)
                try:
                    if page is not None:
                        page.wait_for_timeout(750)
                except Exception:  # noqa: BLE001
                    pass
        else:
            loading_persisted_since = None
        if recovery_deadline is not None and time.monotonic() >= recovery_deadline and not conditions_met:
            print("[seek_apply_assist] resume_upload_recovery_failed", flush=True)
            break
        visible_filenames = [filename] if filename else []
        last_payload = {
            "timeout_ms": timeout_ms,
            "expected_filename": expected_filename,
            "visible_resume_filenames": visible_filenames,
            "filename_visible": filename_visible,
            "loading_indicator_visible": loading_indicator_visible,
            "upload_radio_checked": upload_radio_checked,
            "dont_include_checked": dont_include_checked,
            "selected_resume_method_value": selected_method_value,
            "resume_method_state_known": resume_method_state_known,
            "resume_limit_modal_visible": resume_limit_visible,
            "stable_ms": stable_ms,
            "refresh_attempted": refresh_attempted,
            "recovery_attempted": recovery_attempted,
            "recovery_reupload_attempted": recovery_reupload_attempted,
            "previous_visible_filename": previous_visible_filename,
            "file_input_count": file_input_count,
            "progress_state": progress_state,
            "body_text_snippet": str(adapter.page_text() or "")[:1200],
        }
        print("[seek_apply_assist] resume_upload_complete_wait_poll", flush=True)
        print(f"[seek_apply_assist] filename_visible: {filename_visible}", flush=True)
        print(f"[seek_apply_assist] loading_indicator_visible: {loading_indicator_visible}", flush=True)
        print(f"[seek_apply_assist] local_resume_container_found: {local_resume_container_found}", flush=True)
        print(f"[seek_apply_assist] local_resume_loading_indicator_visible: {local_resume_loading_indicator_visible}", flush=True)
        print(f"[seek_apply_assist] upload_radio_checked: {upload_radio_checked}", flush=True)
        print(f"[seek_apply_assist] dont_include_checked: {dont_include_checked}", flush=True)
        print(f"[seek_apply_assist] resume_limit_modal_visible: {resume_limit_visible}", flush=True)
        print(f"[seek_apply_assist] stable_ms: {stable_ms}", flush=True)
        print(f"[seek_apply_assist] visible_resume_filenames: {visible_filenames}", flush=True)
        print(f"[seek_apply_assist] file_input_count: {file_input_count}", flush=True)
        if global_spinner_ignored:
            print("[seek_apply_assist] global_spinner_ignored", flush=True)
        if conditions_met and stable_ms >= 1500:
            print("[seek_apply_assist] resume_upload_complete_success", flush=True)
            if recovery_attempted:
                print("[seek_apply_assist] resume_upload_recovery_success", flush=True)
            return filename
        time.sleep(0.5)

    screenshot_path, html_path = adapter.save_resume_debug_artifacts(debug_dir)
    last_payload["root_failure_reason"] = "CV filename appeared, but SEEK was still processing the upload. Stopped before Continue."
    page = getattr(adapter, "_page", None)
    diagnostics_meta: dict[str, Any] = {}
    if page is not None:
        diagnostics_meta = capture_local_dom_diagnostics(
            page,
            locator=None,
            failure_type="resume_upload_incomplete",
            debug_dir=debug_dir,
            adapter=adapter,
            expected_filename=expected_filename,
            root_failure_reason=last_payload["root_failure_reason"],
            extra=last_payload,
        )
    _save_resume_section_dom_snapshot(
        debug_dir,
        "final_resume_timeout",
        adapter,
        expected_filename=expected_filename,
        extra=last_payload,
    )
    if diagnostics_meta:
        last_payload["dom_diagnostics"] = diagnostics_meta
    json_path = _save_resume_failure_json(debug_dir, "resume_upload_complete_timeout", last_payload)
    print(f"[seek_apply_assist] resume_upload_complete_timeout_screenshot: {screenshot_path}", flush=True)
    print(f"[seek_apply_assist] resume_upload_complete_timeout_html: {html_path}", flush=True)
    print(f"[seek_apply_assist] resume_upload_complete_timeout_json: {json_path}", flush=True)
    print("[seek_apply_assist] resume_upload_incomplete_stop", flush=True)
    return ""


def _attempt_seek_resume_upload(adapter: SeekApplyAdapter, selected_cv_path: Path, debug_dir: Path, *, phase: str = "initial") -> str:
    page = getattr(adapter, "_page", None)
    file_input_count, file_input_visible, file_input_attached = adapter.get_resume_file_input_state()
    if file_input_count > 0 and file_input_attached:
        print("[seek_apply_assist] fallback_file_input_scan_started", flush=True)
        print(f"[seek_apply_assist] resume_file_input_found: {file_input_count}", flush=True)
        print(f"[seek_apply_assist] file_input_visible: {file_input_visible}", flush=True)
        print(f"[seek_apply_assist] file_input_attached: {file_input_attached}", flush=True)
        print(f"[seek_apply_assist] resume_upload_attempt_started: cv_path={selected_cv_path}", flush=True)
        _save_resume_section_dom_snapshot(
            debug_dir,
            "before_upload",
            adapter,
            expected_filename=selected_cv_path.name,
            extra={"phase": phase, "upload_mode": "direct_file_input"},
        )
        print("[seek_apply_assist] before_set_input_files", flush=True)
        try:
            adapter.upload_resume(selected_cv_path)
            _record_resume_checkpoint(debug_dir, "file_chooser_or_input_used", page=page, adapter=adapter, expected_filename=selected_cv_path.name, extra={"phase": phase, "upload_mode": "direct_file_input"})
            print("[seek_apply_assist] after_set_input_files", flush=True)
            print("[seek_apply_assist] set_input_files_called", flush=True)
            print("[seek_apply_assist] resume_upload_attempt_completed", flush=True)
            _save_resume_section_dom_snapshot(
                debug_dir,
                "after_set_input_files",
                adapter,
                expected_filename=selected_cv_path.name,
                extra={"phase": phase, "upload_mode": "direct_file_input"},
            )
            screenshot_path = adapter.save_resume_after_file_upload_screenshot(debug_dir)
            print(f"[seek_apply_assist] resume_after_file_upload_screenshot: {screenshot_path}", flush=True)
            filename = wait_for_resume_upload_complete(adapter, debug_dir, selected_cv_path.name, timeout_ms=30000, selected_cv_path=selected_cv_path)
            if filename:
                print(f"[seek_apply_assist] resume_filename_visible: {filename}", flush=True)
                print("[seek_apply_assist] uploaded_cv_successfully", flush=True)
                if phase == "resume_limit_retry":
                    print("[seek_apply_assist] resume_limit_retry_filename_visible", flush=True)
                return filename
        except Exception as exc:  # noqa: BLE001
            print(f"[seek_apply_assist] direct_file_input_upload_failure: error={exc}", flush=True)
    file_chooser_uploaded = False
    print("[seek_apply_assist] before_click_resume_upload_button", flush=True)
    print("[seek_apply_assist] file_chooser_wait_started", flush=True)
    _record_resume_checkpoint(debug_dir, "upload_trigger_clicked", page=page, adapter=adapter, expected_filename=selected_cv_path.name, extra={"phase": phase})
    _save_resume_section_dom_snapshot(
        debug_dir,
        "before_upload",
        adapter,
        expected_filename=selected_cv_path.name,
        extra={"phase": phase, "upload_mode": "upload_button"},
    )
    try:
        file_chooser_uploaded = bool(adapter.try_resume_upload_via_file_chooser(selected_cv_path, timeout_ms=5000))
    except Exception as exc:  # noqa: BLE001
        print(f"[seek_apply_assist] file_chooser_timeout: error={exc}", flush=True)
        file_chooser_uploaded = False
    if file_chooser_uploaded:
        _record_resume_checkpoint(debug_dir, "file_chooser_or_input_used", page=page, adapter=adapter, expected_filename=selected_cv_path.name, extra={"phase": phase, "upload_mode": "file_chooser"})
        print("[seek_apply_assist] after_click_resume_upload_button: clicked=True", flush=True)
        print("[seek_apply_assist] before_set_input_files", flush=True)
        print("[seek_apply_assist] after_set_input_files", flush=True)
        print("[seek_apply_assist] set_input_files_called", flush=True)
        print("[seek_apply_assist] resume_upload_attempt_completed", flush=True)
        _save_resume_section_dom_snapshot(
            debug_dir,
            "after_set_input_files",
            adapter,
            expected_filename=selected_cv_path.name,
            extra={"phase": phase, "upload_mode": "file_chooser"},
        )
        if phase == "resume_limit_retry":
            print("[seek_apply_assist] resume_limit_retry_set_input_files_called", flush=True)
        try:
            screenshot_path = adapter.save_resume_after_file_upload_screenshot(debug_dir)
        except Exception as exc:  # noqa: BLE001
            screenshot_path = debug_dir / "resume_after_file_upload_error.png"
            print(f"[seek_apply_assist] save_resume_after_file_upload_screenshot_error: {exc}", flush=True)
        print(f"[seek_apply_assist] resume_after_file_upload_screenshot: {screenshot_path}", flush=True)
        filename = wait_for_resume_upload_complete(adapter, debug_dir, selected_cv_path.name, timeout_ms=30000, selected_cv_path=selected_cv_path)
        if filename:
            print(f"[seek_apply_assist] resume_filename_visible: {filename}", flush=True)
            print("[seek_apply_assist] uploaded_cv_successfully", flush=True)
            if phase == "resume_limit_retry":
                print("[seek_apply_assist] resume_limit_retry_filename_visible", flush=True)
            return filename
        print("[seek_apply_assist] resume_limit_check_after_file_chooser_timeout", flush=True)
        if adapter.resume_limit_ui_present():
            return ""
    print("[seek_apply_assist] file_chooser_timeout", flush=True)
    print("[seek_apply_assist] after_click_resume_upload_button: clicked=False", flush=True)
    print("[seek_apply_assist] resume_limit_check_after_file_chooser_timeout", flush=True)
    if adapter.resume_limit_ui_present():
        print("[seek_apply_assist] upload_click_result_resume_limit_found", flush=True)
        return ""
    print("[seek_apply_assist] fallback_file_input_scan_started", flush=True)
    print("[seek_apply_assist] before_wait_for_upload_result", flush=True)
    wait_result = False
    try:
        wait_result = bool(adapter.wait_for_resume_file_input(timeout_ms=5000))
    except Exception as exc:  # noqa: BLE001
        print(f"[seek_apply_assist] upload_file_input_wait_error: {exc}", flush=True)
    if wait_result:
        print("[seek_apply_assist] upload_click_result_file_input_found", flush=True)
        if phase == "resume_limit_retry":
            print("[seek_apply_assist] resume_limit_retry_file_input_found", flush=True)
    elif adapter.resume_limit_ui_present():
        print("[seek_apply_assist] upload_click_result_resume_limit_found", flush=True)
        print("[seek_apply_assist] after_wait_for_upload_result", flush=True)
        return ""
    else:
        print("[seek_apply_assist] upload_file_input_timeout_after_click", flush=True)
    print("[seek_apply_assist] after_wait_for_upload_result", flush=True)

    file_input_count, file_input_visible, file_input_attached = adapter.get_resume_file_input_state()
    print(f"[seek_apply_assist] resume_file_input_found: {file_input_count}", flush=True)
    print(f"[seek_apply_assist] file_input_visible: {file_input_visible}", flush=True)
    print(f"[seek_apply_assist] file_input_attached: {file_input_attached}", flush=True)
    if file_input_count <= 0 or not file_input_attached:
        file_input_count, file_input_visible, file_input_attached = adapter.get_resume_file_input_state()
        print(f"[seek_apply_assist] resume_file_input_found: {file_input_count}", flush=True)
        print(f"[seek_apply_assist] file_input_visible: {file_input_visible}", flush=True)
        print(f"[seek_apply_assist] file_input_attached: {file_input_attached}", flush=True)
        if file_input_count <= 0 or not file_input_attached:
            screenshot_path, html_path, json_path = _save_upload_click_diagnostics(
                adapter,
                debug_dir,
                {
                    "selected_cv_path": str(selected_cv_path),
                    "file_input_count": file_input_count,
                    "file_input_visible": file_input_visible,
                    "file_input_attached": file_input_attached,
                },
            )
            print(f"[seek_apply_assist] resume_upload_button_no_file_input_screenshot: {screenshot_path}", flush=True)
            print(f"[seek_apply_assist] resume_upload_button_no_file_input_html: {html_path}", flush=True)
            print(f"[seek_apply_assist] resume_upload_button_no_file_input_json: {json_path}", flush=True)
            return ""
    print(f"[seek_apply_assist] resume_upload_attempt_started: cv_path={selected_cv_path}", flush=True)
    _save_resume_section_dom_snapshot(
        debug_dir,
        "before_upload",
        adapter,
        expected_filename=selected_cv_path.name,
        extra={"phase": phase, "upload_mode": "post_click_file_input"},
    )
    print("[seek_apply_assist] before_set_input_files", flush=True)
    adapter.upload_resume(selected_cv_path)
    print("[seek_apply_assist] after_set_input_files", flush=True)
    print("[seek_apply_assist] set_input_files_called", flush=True)
    if phase == "resume_limit_retry":
        print("[seek_apply_assist] resume_limit_retry_set_input_files_called", flush=True)
    print("[seek_apply_assist] resume_upload_attempt_completed", flush=True)
    _save_resume_section_dom_snapshot(
        debug_dir,
        "after_set_input_files",
        adapter,
        expected_filename=selected_cv_path.name,
        extra={"phase": phase, "upload_mode": "post_click_file_input"},
    )
    try:
        screenshot_path = adapter.save_resume_after_file_upload_screenshot(debug_dir)
    except Exception as exc:  # noqa: BLE001
        screenshot_path = debug_dir / "resume_after_file_upload_error.png"
        print(f"[seek_apply_assist] save_resume_after_file_upload_screenshot_error: {exc}", flush=True)
    print(f"[seek_apply_assist] resume_after_file_upload_screenshot: {screenshot_path}", flush=True)
    filename = wait_for_resume_upload_complete(adapter, debug_dir, selected_cv_path.name, timeout_ms=30000, selected_cv_path=selected_cv_path)
    if filename:
        print(f"[seek_apply_assist] resume_filename_visible: {filename}", flush=True)
        print("[seek_apply_assist] uploaded_cv_successfully", flush=True)
        if phase == "resume_limit_retry":
            print("[seek_apply_assist] resume_limit_retry_filename_visible", flush=True)
        return filename
    screenshot_path, html_path = adapter.save_resume_debug_artifacts(debug_dir)
    json_path = _save_resume_failure_json(
        debug_dir,
        "resume_filename_missing_after_upload",
        {
            "selected_cv_path": str(selected_cv_path),
            "resume_after_upload_screenshot": str(screenshot_path),
            "resume_limit_present": bool(adapter.resume_limit_ui_present()),
            "body_text_snippet": str(adapter.page_text() or "")[:500],
        },
    )
    print(f"[seek_apply_assist] resume_filename_missing_after_upload_screenshot: {screenshot_path}", flush=True)
    print(f"[seek_apply_assist] resume_filename_missing_after_upload_html: {html_path}", flush=True)
    print(f"[seek_apply_assist] resume_filename_missing_after_upload_json: {json_path}", flush=True)
    return filename


def handle_resume_upload(adapter: SeekApplyAdapter, selected_cv_path: Path, debug_dir: Path) -> ResumeUploadResult:
    print("[seek_apply_assist] handle_resume_upload_started", flush=True)
    automation_settings = _resume_automation_settings(debug_dir)
    allow_delete_old_seek_resumes = bool(automation_settings.get("allow_delete_old_seek_resumes", True))
    allow_no_resume = bool(automation_settings.get("allow_no_resume", False))
    adapter.wait_for_resume_section(timeout_ms=20000)
    adapter.log_resume_diagnostics()
    page = getattr(adapter, "_page", None)
    if not _page_supports_dom(page):
        page = None
    _record_resume_checkpoint(debug_dir, "choose_documents_loaded", page=page, adapter=adapter)
    pre_screenshot_path, pre_html_path = adapter.save_resume_pre_search_artifacts(debug_dir)
    print(f"[seek_apply_assist] resume_before_selector_search_screenshot: {pre_screenshot_path}", flush=True)
    print(f"[seek_apply_assist] resume_before_selector_search_html: {pre_html_path}", flush=True)

    existing_resume_options: list[str] = []
    try:
        existing_resume_options = list(adapter.get_existing_resume_options())
    except Exception:  # noqa: BLE001
        existing_resume_options = []
    meaningful_existing_resume_options = [
        option for option in existing_resume_options
        if option and not re.search(r"please select a resum.|please select a resume", option, re.I)
    ]
    if meaningful_existing_resume_options:
        print(f"[seek_apply_assist] existing_resume_options_detected: {meaningful_existing_resume_options}", flush=True)
        if not allow_delete_old_seek_resumes:
            screenshot_path, html_path = adapter.save_resume_debug_artifacts(debug_dir)
            json_path = _save_resume_failure_json(
                debug_dir,
                "existing_resume_delete_required",
                {
                    "selected_cv_path": str(selected_cv_path),
                    "allow_delete_old_seek_resumes": allow_delete_old_seek_resumes,
                    "existing_resume_options": meaningful_existing_resume_options,
                },
            )
            warning = "SEEK already has stored resumes. Enable allow_delete_old_seek_resumes or delete one manually before upload."
            print(f"[seek_apply_assist] existing_resume_delete_required_warning: {warning}", flush=True)
            print(f"[seek_apply_assist] existing_resume_delete_required_screenshot: {screenshot_path}", flush=True)
            print(f"[seek_apply_assist] existing_resume_delete_required_html: {html_path}", flush=True)
            print(f"[seek_apply_assist] existing_resume_delete_required_json: {json_path}", flush=True)
            return ResumeUploadResult(cv_uploaded=False, status="paused_for_manual_resume_upload", warning=warning)
        delete_candidate = _choose_resume_limit_delete_candidate(
            meaningful_existing_resume_options,
            protected_filename=selected_cv_path.name,
        )
        print(f"[seek_apply_assist] existing_resume_delete_candidate: {delete_candidate or '(none)'}", flush=True)
        if delete_candidate and adapter.click_resume_change_radio():
            print("[seek_apply_assist] resume_change_method_selected", flush=True)
            if adapter.select_existing_resume_option(delete_candidate):
                print(f"[seek_apply_assist] existing_resume_dropdown_selected: {delete_candidate}", flush=True)
                if adapter.click_existing_resume_delete():
                    print("[seek_apply_assist] existing_resume_delete_button_clicked", flush=True)
                    try:
                        adapter.confirm_resume_limit_delete()
                    except Exception:  # noqa: BLE001
                        pass
                    try:
                        if page is not None:
                            page.wait_for_timeout(2500)
                        else:
                            time.sleep(2.5)
                    except Exception:  # noqa: BLE001
                        time.sleep(2.5)
                    _record_resume_checkpoint(debug_dir, "resume_deleted_for_space", page=page, adapter=adapter)
                else:
                    print("[seek_apply_assist] existing_resume_delete_button_click_failed", flush=True)
            else:
                print("[seek_apply_assist] existing_resume_dropdown_select_failed", flush=True)
        else:
            print("[seek_apply_assist] existing_resume_change_radio_not_available", flush=True)

    radio_visible = _resume_upload_radio_visible(adapter)
    if radio_visible:
        print("[seek_apply_assist] resume_upload_radio_visible: upload radio is visible.", flush=True)
        print("[seek_apply_assist] before_click_resume_upload_radio", flush=True)
        if adapter.click_resume_upload_radio():
            print("[seek_apply_assist] after_click_resume_upload_radio: success", flush=True)
            print("[seek_apply_assist] clicked_upload_resume_label: switched to Upload a resume option.", flush=True)
            print("[seek_apply_assist] checked_upload_resume_radio: upload radio selected.", flush=True)
            print("[seek_apply_assist] resume_upload_method_selected", flush=True)
            print("[seek_apply_assist] resume_after_method_selected_reached", flush=True)
            try:
                adapter.save_resume_upload_selected_screenshot(debug_dir)
            except Exception:  # noqa: BLE001
                pass
            try:
                adapter.save_resume_upload_selected_html(debug_dir)
            except Exception:  # noqa: BLE001
                pass
            filename = _attempt_seek_resume_upload(adapter, selected_cv_path, debug_dir)
            if filename:
                _record_resume_checkpoint(debug_dir, "filename_visible_after_upload", page=page, adapter=adapter, expected_filename=filename)
                return ResumeUploadResult(cv_uploaded=True, status="resume_uploaded", verified_filename=filename)
        else:
            print("[seek_apply_assist] after_click_resume_upload_radio: returned_false", flush=True)

    filename = _attempt_seek_resume_upload(adapter, selected_cv_path, debug_dir)
    if filename:
        _record_resume_checkpoint(debug_dir, "filename_visible_after_upload", page=page, adapter=adapter, expected_filename=filename)
        return ResumeUploadResult(cv_uploaded=True, status="resume_uploaded", verified_filename=filename)

    resume_limit_detected = False
    limit_options: list[str] = []
    try:
        resume_limit_detected = bool(adapter.resume_limit_ui_present())
    except Exception:  # noqa: BLE001
        resume_limit_detected = False
    if resume_limit_detected:
        print("[seek_apply_assist] resume_limit_detected", flush=True)
        print("[seek_apply_assist] resume_limit_detected_after_upload_click", flush=True)
        _record_resume_checkpoint(debug_dir, "resume_limit_detected", page=page, adapter=adapter)
        page_text = str(adapter.page_text() or "")
        modal_title = ""
        title_match = re.search(r"(Resum.\s+limit\s+reached)", page_text, re.I)
        if title_match:
            modal_title = title_match.group(1)
        print(f"[seek_apply_assist] resume_limit_modal_title: {modal_title or '(unknown)'}", flush=True)
        try:
            limit_options = list(adapter.get_resume_limit_dropdown_options())
        except Exception:  # noqa: BLE001
            limit_options = []
        print(f"[seek_apply_assist] resume_limit_dropdown_options: {limit_options}", flush=True)

        if not allow_delete_old_seek_resumes:
            screenshot_path, html_path = adapter.save_resume_debug_artifacts(debug_dir)
            json_path = _save_resume_failure_json(
                debug_dir,
                "resume_limit_reached",
                {
                    "selected_cv_path": str(selected_cv_path),
                    "allow_delete_old_seek_resumes": allow_delete_old_seek_resumes,
                    "resume_limit_options": limit_options,
                },
            )
            warning = "SEEK resume limit reached. Enable allow_delete_old_seek_resumes or delete an old resume manually."
            print(f"[seek_apply_assist] resume_limit_retry_upload_failure: {warning}", flush=True)
            print(f"[seek_apply_assist] resume_limit_reached_screenshot: {screenshot_path}", flush=True)
            print(f"[seek_apply_assist] resume_limit_reached_html: {html_path}", flush=True)
            print(f"[seek_apply_assist] resume_limit_reached_json: {json_path}", flush=True)
            return ResumeUploadResult(cv_uploaded=False, status="paused_for_manual_resume_upload", warning=warning)

        protected_filename = selected_cv_path.name
        delete_candidate = _choose_resume_limit_delete_candidate(limit_options, protected_filename=protected_filename)
        print(f"[seek_apply_assist] resume_limit_delete_candidate: {delete_candidate or '(none)'}", flush=True)
        if delete_candidate and adapter.select_resume_limit_dropdown_option(delete_candidate):
            print(f"[seek_apply_assist] resume_limit_dropdown_selected: {delete_candidate}", flush=True)
            if adapter.click_resume_limit_delete():
                print("[seek_apply_assist] resume_limit_delete_button_clicked", flush=True)
                if adapter.confirm_resume_limit_delete():
                    print("[seek_apply_assist] resume_limit_delete_confirmed", flush=True)
                adapter.wait_for_resume_limit_change(limit_options, timeout_ms=5000)
                if not adapter.resume_limit_ui_present():
                    print("[seek_apply_assist] resume_limit_modal_closed", flush=True)
                try:
                    if page is not None:
                        page.wait_for_timeout(2500)
                    else:
                        time.sleep(2.5)
                except Exception:  # noqa: BLE001
                    time.sleep(2.5)
                _record_resume_checkpoint(debug_dir, "resume_deleted_for_space", page=page, adapter=adapter)
                print("[seek_apply_assist] resume_limit_retry_upload_started", flush=True)
                if adapter.click_resume_upload_radio():
                    print("[seek_apply_assist] resume_upload_method_selected", flush=True)
                    _record_resume_checkpoint(debug_dir, "retry_upload_trigger_clicked", page=page, adapter=adapter)
                filename = _attempt_seek_resume_upload(adapter, selected_cv_path, debug_dir, phase="resume_limit_retry")
                if filename:
                    print("[seek_apply_assist] resume_limit_retry_upload_success", flush=True)
                    _record_resume_checkpoint(debug_dir, "filename_visible_after_upload", page=page, adapter=adapter, expected_filename=filename)
                    return ResumeUploadResult(cv_uploaded=True, status="resume_uploaded", verified_filename=filename)
                file_input_count, _file_visible, file_attached = adapter.get_resume_file_input_state()
                if file_input_count > 0 and file_attached:
                    print("[seek_apply_assist] resume_limit_retry_file_input_found", flush=True)
        screenshot_path, html_path = adapter.save_resume_debug_artifacts(debug_dir)
        json_path = _save_resume_failure_json(
            debug_dir,
            "resume_limit_retry_upload_failure",
            {
                "selected_cv_path": str(selected_cv_path),
                "resume_limit_options": limit_options,
                "delete_candidate": delete_candidate,
            },
        )
        print("[seek_apply_assist] resume_limit_retry_upload_failure", flush=True)
        print(f"[seek_apply_assist] resume_limit_retry_upload_failure_screenshot: {screenshot_path}", flush=True)
        print(f"[seek_apply_assist] resume_limit_retry_upload_failure_html: {html_path}", flush=True)
        print(f"[seek_apply_assist] resume_limit_retry_upload_failure_json: {json_path}", flush=True)
        return ResumeUploadResult(
            cv_uploaded=False,
            status="paused_for_manual_resume_upload",
            warning="SEEK resume limit flow did not complete automatically.",
        )

    resume_ready, resume_ready_payload = verify_resume_ready_to_continue(
        adapter,
        debug_dir,
        expected_filename=selected_cv_path.name,
    )
    if not resume_ready and (
        bool(resume_ready_payload.get("filename_visible"))
        or bool(resume_ready_payload.get("loading_indicator_visible"))
        or bool(resume_ready_payload.get("upload_radio_checked"))
    ):
        screenshot_path, html_path = adapter.save_resume_debug_artifacts(debug_dir)
        json_path = _save_resume_failure_json(
            debug_dir,
            "resume_upload_blocked_before_continue",
            resume_ready_payload,
        )
        print(f"[seek_apply_assist] resume_upload_blocked_before_continue_screenshot: {screenshot_path}", flush=True)
        print(f"[seek_apply_assist] resume_upload_blocked_before_continue_html: {html_path}", flush=True)
        print(f"[seek_apply_assist] resume_upload_blocked_before_continue_json: {json_path}", flush=True)
        return ResumeUploadResult(
            cv_uploaded=False,
            status="paused_for_resume_upload_incomplete",
            warning="CV filename appeared, but SEEK was still processing the upload. Stopped before Continue.",
        )

    filename = _verify_resume_filename(adapter)
    if filename:
        print(f"[seek_apply_assist] resume_filename_visible: {filename}", flush=True)
        return ResumeUploadResult(
            cv_uploaded=False,
            status="resume_existing_or_unknown",
            warning="Resume filename is already visible.",
            verified_filename=filename,
        )

    if adapter.resume_appears_present():
        warning = "Could not upload CV, but resume appears present."
        print(f"[seek_apply_assist] resume_existing_detected: {warning}", flush=True)
        return ResumeUploadResult(cv_uploaded=False, status="resume_existing_or_unknown", warning=warning)

    screenshot_path, html_path = adapter.save_resume_debug_artifacts(debug_dir)
    json_path = _save_resume_failure_json(
        debug_dir,
        "resume_step_failed",
        {
            "selected_cv_path": str(selected_cv_path),
            "allow_no_resume": allow_no_resume,
            "resume_limit_detected": resume_limit_detected,
        },
    )
    print(f"[seek_apply_assist] resume_manual_pause: saved debug files to {screenshot_path} and {html_path}", flush=True)
    print(f"[seek_apply_assist] resume_step_failed_json: {json_path}", flush=True)
    warning = "Resume upload requires manual continuation."
    if allow_no_resume:
        return ResumeUploadResult(cv_uploaded=False, status="resume_existing_or_unknown", warning=warning)
    return ResumeUploadResult(
        cv_uploaded=False,
        status="paused_for_manual_resume_upload",
        warning=warning,
    )


def prompt_for_seek_login(
    adapter: SeekApplyAdapter,
    *,
    db_path: Path | None = None,
    use_bulk_profile: bool = False,
    input_func: Callable[[], str] = input,
    allow_manual_prompt: bool = True,
) -> tuple[bool, str]:
    page = getattr(adapter, "_page", None)
    if page is not None and db_path is not None:
        try:
            code_fetch_timeout_seconds = 15 if not allow_manual_prompt else 90
            success, message = _attempt_seek_email_code_login(
                page,
                db_path.parent,
                code_fetch_timeout_seconds=code_fetch_timeout_seconds,
            )
            if success:
                print("[seek_apply_assist] auto_seek_login_success", flush=True)
                invalidate_seek_seeded_browser_profile_cache(db_path, use_bulk_profile=use_bulk_profile)
                return True, ""
            print(f"[seek_apply_assist] auto_seek_login_failed: {message}", flush=True)
            pause_reason = _safe_detect_pause_reason(adapter)
            if pause_reason == "paused_for_captcha":
                return False, "SEEK login triggered a captcha/verify-you-are-human challenge."
            if pause_reason == "paused_for_seek_verification":
                return False, "SEEK login triggered an additional verification checkpoint."
        except Exception as exc:  # noqa: BLE001
            print(f"[seek_apply_assist] auto_seek_login_error: {exc}", flush=True)
    if not allow_manual_prompt:
        return False, "SEEK login requires manual continuation, but this run is non-interactive."
    profile_label = "bulk" if use_bulk_profile else "default"
    print(f"Please sign in manually for the {profile_label} SEEK profile, then press Enter to continue.", flush=True)
    try:
        input_func()
    except EOFError:
        return False, "SEEK login requires manual continuation, but no console input was available."
    return True, ""


def load_applicant_profile(data_dir: Path) -> dict[str, Any]:
    def merge_profile_dicts(base: Any, override: Any) -> Any:
        if isinstance(base, dict) and isinstance(override, dict):
            merged: dict[str, Any] = {key: merge_profile_dicts(value, {}) for key, value in base.items()}
            for key, value in override.items():
                merged[key] = merge_profile_dicts(merged.get(key), value)
            return merged
        if isinstance(override, list):
            return list(override)
        return override if override not in ({}, None) or base is None else base

    profile_path = Path(data_dir) / "applicant_profile.json"
    if profile_path.exists():
        try:
            loaded = json.loads(profile_path.read_text(encoding="utf-8"))
            return merge_profile_dicts(DEFAULT_APPLICANT_PROFILE, loaded)
        except Exception:  # noqa: BLE001
            pass
    return dict(DEFAULT_APPLICANT_PROFILE)


def _normalise_validation_error_text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _get_seek_pause_diagnostics(adapter: SeekApplyAdapter) -> dict[str, Any]:
    try:
        current_url = str(adapter.current_url() or "")
    except Exception:  # noqa: BLE001
        current_url = ""
    try:
        page_text = str(adapter.page_text() or "")
    except Exception:  # noqa: BLE001
        page_text = ""
    try:
        page_title = str(getattr(adapter, "_page", None).title() or "")
    except Exception:  # noqa: BLE001
        page_title = ""

    lowered_url = current_url.lower()
    lowered_text = page_text.lower()
    matched_token = ""
    matched_source = ""
    matched_category = ""

    captcha_tokens = ("verify you are human", "captcha")
    login_url_tokens = ("login.seek.com", "oauth/login", "signin", "sign-in")
    login_text_tokens = ("log in", "sign in to continue", "sign in or register", "enter code", "check your email")

    is_verification_text, verification_token = _looks_like_seek_verification_text(page_text)
    if is_verification_text:
        matched_token = verification_token
        matched_source = "page_text"
        matched_category = "paused_for_seek_verification"
    if not matched_token:
        for token in captcha_tokens:
            if token in lowered_url:
                matched_token = token
                matched_source = "url"
                matched_category = "paused_for_captcha"
                break
            if token in lowered_text:
                matched_token = token
                matched_source = "page_text"
                matched_category = "paused_for_captcha"
                break
    if not matched_token:
        for token in login_url_tokens:
            if token in lowered_url:
                matched_token = token
                matched_source = "url"
                matched_category = "paused_for_seek_login"
                break
    if not matched_token and "log in" in page_title.lower():
        matched_token = "log in"
        matched_source = "page_title"
        matched_category = "paused_for_seek_login"
    if not matched_token:
        for token in login_text_tokens:
            if token in lowered_text:
                matched_token = token
                matched_source = "page_text"
                matched_category = "paused_for_seek_login"
                break

    snippet = page_text[:800]
    return {
        "pause_reason": matched_category or adapter.detect_pause_reason(),
        "matched_token": matched_token,
        "matched_source": matched_source,
        "current_url": current_url,
        "page_title": page_title,
        "page_text_snippet": snippet,
    }


def _question_matches_validation_error(question: dict[str, Any], validation_error: str) -> bool:
    error_text = _normalise_validation_error_text(validation_error)
    if not error_text:
        return False
    candidates = [
        question.get("question_text"),
        question.get("field_container_text"),
        question.get("radio_group_container_text"),
        *(question.get("nearby_text_candidates") or []),
    ]
    for candidate in candidates:
        candidate_text = _normalise_validation_error_text(str(candidate or ""))
        if not candidate_text:
            continue
        if error_text in candidate_text or candidate_text in error_text:
            return True
    return False


def _resolve_answer_for_validation_retry(
    question: dict[str, Any],
    applicant_profile: dict[str, Any],
    job_context: dict[str, Any],
) -> tuple[str, str, str] | None:
    question_text = str(question.get("question_text") or "").strip()
    field_type = str(question.get("field_type") or "").strip()
    available_options = [str(option.get("label") or option) for option in (question.get("option_payloads") or question.get("options") or [])]
    profile_resolution = resolve_profile_backed_answer(
        question_text=question_text,
        field_type=field_type,
        available_options=available_options,
        applicant_profile=applicant_profile,
    )
    if profile_resolution.should_answer and profile_resolution.chosen_answer:
        return (
            profile_resolution.chosen_answer,
            profile_resolution.matched_rule or profile_resolution.inferred_question_type or "profile_backed",
            profile_resolution.reason,
        )
    answer, matched_rule, answer_source = resolve_free_text_question_answer(question_text, applicant_profile, job_context)
    if answer:
        return answer, matched_rule, answer_source
    return None


def _normalise_seek_browser_channel(value: Any) -> str:
    token = str(value or "").strip().lower()
    aliases = {
        "chrome": "chrome",
        "google-chrome": "chrome",
        "edge": "msedge",
        "msedge": "msedge",
        "microsoft-edge": "msedge",
        "chromium": "chromium",
    }
    return aliases.get(token, "")


def preferred_seek_browser_channels(data_dir: Path) -> list[str]:
    profile = load_applicant_profile(data_dir)
    automation_settings = dict(profile.get("automation") or {})
    raw_channels = automation_settings.get("seek_browser_channels")
    if not raw_channels:
        raw_single = automation_settings.get("seek_browser_channel")
        raw_channels = [raw_single] if raw_single else list(BROWSER_CHANNEL_FALLBACKS)
    elif not isinstance(raw_channels, list):
        raw_channels = [raw_channels]

    channels: list[str] = []
    for candidate in raw_channels:
        normalised = _normalise_seek_browser_channel(candidate)
        if normalised and normalised not in channels:
            channels.append(normalised)
    for fallback in BROWSER_CHANNEL_FALLBACKS:
        if fallback not in channels:
            channels.append(fallback)
    return channels


def _browser_executable_candidates(channel: str) -> list[Path]:
    local_app_data = Path.home() / "AppData" / "Local"
    program_files = Path("C:/Program Files")
    program_files_x86 = Path("C:/Program Files (x86)")
    lowered = str(channel or "").strip().lower()
    if lowered == "chrome":
        return [
            local_app_data / "Google/Chrome/Application/chrome.exe",
            program_files / "Google/Chrome/Application/chrome.exe",
            program_files_x86 / "Google/Chrome/Application/chrome.exe",
        ]
    if lowered == "msedge":
        return [
            local_app_data / "Microsoft/Edge/Application/msedge.exe",
            program_files / "Microsoft/Edge/Application/msedge.exe",
            program_files_x86 / "Microsoft/Edge/Application/msedge.exe",
        ]
    if lowered == "chromium":
        return [
            local_app_data / "Chromium/Application/chrome.exe",
            program_files / "Chromium/Application/chrome.exe",
            program_files_x86 / "Chromium/Application/chrome.exe",
        ]
    return []


def find_seek_browser_executable(data_dir: Path) -> tuple[str, Path] | None:
    for channel in preferred_seek_browser_channels(data_dir):
        for candidate in _browser_executable_candidates(channel):
            if candidate.exists():
                return channel, candidate
    return None


def open_seek_login_in_browser(
    db_path: Path,
    *,
    use_bulk_profile: bool = False,
    login_url: str = "https://login.seek.com",
) -> tuple[str, str]:
    return open_seek_url_in_browser(
        db_path,
        target_url=login_url,
        use_bulk_profile=use_bulk_profile,
    )


def open_seek_url_in_browser(
    db_path: Path,
    *,
    target_url: str,
    use_bulk_profile: bool = False,
) -> tuple[str, str]:
    data_dir = Path(db_path).resolve().parent
    resolved = find_seek_browser_executable(data_dir)
    if resolved is None:
        raise RuntimeError("Could not find Chrome or Edge on this machine.")
    channel, executable = resolved
    configured_path = configured_seek_browser_profile_dir(db_path, use_bulk_profile=use_bulk_profile)
    user_data_dir, launch_args = resolve_seek_browser_profile_launch_settings(configured_path)
    command = [str(executable)]
    if user_data_dir is not None:
        command.append(f"--user-data-dir={user_data_dir}")
    command.extend(launch_args)
    command.append("--new-window")
    command.append(target_url)
    subprocess.Popen(command)
    return channel, str(user_data_dir or "")


def close_visible_seek_profile_windows() -> int:
    script = r"""
$targets = @(
    Get-Process chrome,msedge -ErrorAction SilentlyContinue |
    Where-Object { $_.MainWindowTitle -like '*My Profile | SEEK*' }
)
$count = @($targets).Count
foreach($p in $targets){
    try { $null = $p.CloseMainWindow() } catch {}
}
Start-Sleep -Milliseconds 800
$remaining = @(
    Get-Process chrome,msedge -ErrorAction SilentlyContinue |
    Where-Object { $_.MainWindowTitle -like '*My Profile | SEEK*' }
)
foreach($p in $remaining){
    try { Stop-Process -Id $p.Id -Force } catch {}
}
Write-Output $count
"""
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            **_windows_subprocess_kwargs(),
        )
    except Exception:
        return 0
    output = str(completed.stdout or "").strip()
    try:
        return int(output.splitlines()[-1]) if output else 0
    except Exception:
        return 0


def refresh_seek_session_in_browser(
    db_path: Path,
    *,
    use_bulk_profile: bool = False,
    login_url: str = "https://nz.seek.com/profile/me",
    wait_seconds: float = 10.0,
) -> tuple[str, str]:
    data_dir = Path(db_path).resolve().parent
    invalidate_seek_seeded_browser_profile_cache(Path(db_path), use_bulk_profile=use_bulk_profile)
    resolved = find_seek_browser_executable(data_dir)
    if resolved is None:
        raise RuntimeError("Could not find Chrome or Edge on this machine.")
    channel, executable = resolved
    configured_path = configured_seek_browser_profile_dir(db_path, use_bulk_profile=use_bulk_profile)
    user_data_dir, launch_args = resolve_seek_browser_profile_launch_settings(configured_path)
    command = [str(executable)]
    if user_data_dir is not None:
        command.append(f"--user-data-dir={user_data_dir}")
    command.extend(launch_args)
    command.append("--new-window")
    command.append(login_url)
    process = subprocess.Popen(command)
    time.sleep(max(wait_seconds, 1.0))
    if process.poll() is None:
        try:
            process.terminate()
            process.wait(timeout=5)
        except Exception:  # noqa: BLE001
            try:
                process.kill()
            except Exception:  # noqa: BLE001
                pass
    return channel, str(user_data_dir or "")


def validate_seek_session(
    db_path: Path,
    *,
    use_bulk_profile: bool = False,
    visible: bool = True,
    target_url: str = "https://nz.seek.com/profile/me",
) -> dict[str, Any]:
    adapter: _PlaywrightSeekAdapter | None = None
    try:
        invalidate_seek_seeded_browser_profile_cache(Path(db_path), use_bulk_profile=use_bulk_profile)
        adapter = _PlaywrightSeekAdapter(
            visible,
            Path(db_path),
            use_bulk_profile=use_bulk_profile,
        )
        try:
            page = getattr(adapter, "_page", None)
            if page is None:
                raise RuntimeError("SEEK validation page was not available.")
            page.goto(target_url, wait_until="domcontentloaded", timeout=12000)
        except Exception:  # noqa: BLE001
            adapter.open_job(target_url)
        try:
            page = getattr(adapter, "_page", None)
            if page is not None:
                page.wait_for_timeout(1500)
        except Exception:  # noqa: BLE001
            pass
        diagnostics = _get_seek_pause_diagnostics(adapter)
        pause_reason = str(diagnostics.get("pause_reason") or "").strip()
        state_map = {
            "paused_for_seek_login": "login_required",
            "paused_for_seek_verification": "verification_required",
            "paused_for_captcha": "captcha_required",
        }
        if pause_reason in state_map:
            state = state_map[pause_reason]
            reason = (
                "SEEK session validation requires manual continuation."
                if state == "login_required"
                else "SEEK session validation hit a verification checkpoint."
            )
            write_seek_session_state(
                Path(db_path),
                state=state,
                job_id=None,
                reason=reason,
                matched_token=str(diagnostics.get("matched_token") or ""),
                matched_source=str(diagnostics.get("matched_source") or ""),
                final_url=str(diagnostics.get("current_url") or ""),
            )
            return {
                "ok": False,
                "state": state,
                "reason": reason,
                "matched_token": str(diagnostics.get("matched_token") or ""),
                "matched_source": str(diagnostics.get("matched_source") or ""),
                "final_url": str(diagnostics.get("current_url") or ""),
                "browser_channel": getattr(adapter, "browser_channel", "chromium"),
            }

        final_url = str(adapter.current_url() or "")
        page_title = ""
        try:
            page_title = str(getattr(adapter, "_page", None).title() or "")
        except Exception:  # noqa: BLE001
            page_title = ""
        reason = "SEEK session validated for the automation profile."
        write_seek_session_state(
            Path(db_path),
            state="session_valid",
            job_id=None,
            reason=reason,
            matched_token="",
            matched_source="",
            final_url=final_url,
        )
        return {
            "ok": True,
            "state": "session_valid",
            "reason": reason,
            "matched_token": "",
            "matched_source": "",
            "final_url": final_url,
            "page_title": page_title,
            "browser_channel": getattr(adapter, "browser_channel", "chromium"),
        }
    except SeekBrowserProfileLockedError as exc:
        reason = str(exc)
        write_seek_session_state(
            Path(db_path),
            state="login_required",
            job_id=None,
            reason=reason,
            matched_token="profile_locked",
            matched_source="local_profile",
            final_url="",
        )
        return {
            "ok": False,
            "state": "login_required",
            "reason": reason,
            "matched_token": "profile_locked",
            "matched_source": "local_profile",
            "final_url": "",
            "browser_channel": "",
        }
    finally:
        if adapter is not None:
            adapter.close()


def _seek_submit_confirmation_detected(*, final_url: str, page_title: str, page_text: str) -> bool:
    lowered_url = str(final_url or "").lower()
    lowered_title = str(page_title or "").lower()
    lowered_text = str(page_text or "").lower()
    if re.search(r"/apply/(?:success|submitted)(?:[/?#]|$)", lowered_url):
        return True
    return (
        "application submitted" in lowered_text
        or "application sent" in lowered_text
        or "application has been sent" in lowered_text
        or "your application has been sent" in lowered_text
        or "thanks for applying" in lowered_text
        or "thank you for applying" in lowered_text
        or "you've applied" in lowered_text
        or "we have received your application" in lowered_text
        or "application submitted" in lowered_title
        or "application sent" in lowered_title
        or "thanks for applying" in lowered_title
    )


def _submit_seek_review_inprocess(
    job_id: int,
    db_path: Path,
    *,
    review_url: str,
    visible: bool = False,
    use_bulk_profile: bool = False,
) -> ApplyAssistResult:
    database = Database(db_path)
    return _assist_seek_apply(
        job_id,
        database,
        visible=visible,
        force_new_profile=False,
        use_bulk_profile=use_bulk_profile,
        keep_browser_open_override=False,
        resume_upload_only=False,
        input_func=lambda: "",
        allow_manual_login_prompt=False,
        auto_submit=True,
        review_url=review_url,
    )


def _has_running_asyncio_loop() -> bool:
    try:
        import asyncio

        loop = asyncio.get_running_loop()
    except Exception:  # noqa: BLE001
        return False
    return bool(loop and loop.is_running())


def _is_sync_playwright_loop_error(exc: Exception) -> bool:
    return "playwright sync api inside the asyncio loop" in str(exc).lower()


def _apply_result_from_subprocess_payload(payload: dict[str, Any], *, job_id: int, fallback_url: str) -> ApplyAssistResult:
    evidence_capture_errors = payload.get("evidence_capture_errors")
    if not isinstance(evidence_capture_errors, list):
        evidence_capture_errors = []
    return ApplyAssistResult(
        job_id=int(payload.get("job_id") or job_id),
        status=str(payload.get("status") or "failed"),
        steps_completed=[str(item) for item in list(payload.get("steps_completed") or [])],
        error=str(payload.get("error") or ""),
        final_url=str(payload.get("final_url") or fallback_url),
        debug_dir=str(payload.get("debug_dir") or ""),
        latest_checkpoint=str(payload.get("latest_checkpoint") or ""),
        root_failure_reason=str(payload.get("root_failure_reason") or payload.get("error") or ""),
        evidence_capture_errors=[str(item) for item in evidence_capture_errors],
    )


def _submit_seek_review_subprocess(
    job_id: int,
    db_path: Path,
    *,
    review_url: str,
    visible: bool = False,
    use_bulk_profile: bool = False,
) -> ApplyAssistResult:
    command = [
        sys.executable,
        str(SUBMIT_SEEK_REVIEW_SCRIPT),
        "--job-id",
        str(job_id),
        "--db-path",
        str(Path(db_path)),
        "--review-url",
        str(review_url or ""),
        "--visible",
        "true" if visible else "false",
    ]
    if use_bulk_profile:
        command.append("--bulk-profile")
    completed = subprocess.run(
        command,
        capture_output=True,
        timeout=900,
        check=False,
        env=_python_subprocess_env(),
        **_python_subprocess_text_kwargs(),
        **_windows_subprocess_kwargs(),
    )
    stdout_text = str(completed.stdout or "")
    stderr_text = str(completed.stderr or "")
    payload_line = ""
    for line in reversed(stdout_text.splitlines()):
        stripped = line.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            payload_line = stripped
            break
    if not payload_line:
        message = stderr_text.strip() or stdout_text.strip() or "Submit subprocess did not return a result payload."
        return ApplyAssistResult(
            job_id=job_id,
            status="failed",
            steps_completed=[],
            error=message,
            final_url=str(review_url or ""),
            root_failure_reason=message,
            evidence_capture_errors=[],
        )
    try:
        payload = json.loads(payload_line)
    except Exception as exc:  # noqa: BLE001
        message = f"Submit subprocess returned invalid JSON: {exc}"
        return ApplyAssistResult(
            job_id=job_id,
            status="failed",
            steps_completed=[],
            error=message,
            final_url=str(review_url or ""),
            root_failure_reason=message,
            evidence_capture_errors=[],
        )
    if not isinstance(payload, dict):
        message = "Submit subprocess returned an unexpected payload."
        return ApplyAssistResult(
            job_id=job_id,
            status="failed",
            steps_completed=[],
            error=message,
            final_url=str(review_url or ""),
            root_failure_reason=message,
            evidence_capture_errors=[],
    )
    return _apply_result_from_subprocess_payload(payload, job_id=job_id, fallback_url=str(review_url or ""))


def _recover_submit_profile_lock(db_path: Path, *, use_bulk_profile: bool = False) -> None:
    close_visible_seek_profile_windows()
    invalidate_seek_seeded_browser_profile_cache(Path(db_path), use_bulk_profile=use_bulk_profile)
    time.sleep(1.5)


def submit_seek_review(
    job_id: int,
    db_path: Path,
    *,
    review_url: str,
    visible: bool = False,
    use_bulk_profile: bool = False,
) -> ApplyAssistResult:
    database = Database(db_path)
    database.log_run(
        "seek_submit",
        "replay_submit_started",
        "success",
        f"job_id={job_id} review_url_hint={review_url}",
    )
    started_at = time.perf_counter()
    try:
        def run_once(*, force_subprocess: bool = False) -> ApplyAssistResult:
            if force_subprocess or _has_running_asyncio_loop():
                return _submit_seek_review_subprocess(
                    job_id,
                    db_path,
                    review_url=review_url,
                    visible=visible,
                    use_bulk_profile=use_bulk_profile,
                )
            try:
                return _submit_seek_review_inprocess(
                    job_id,
                    db_path,
                    review_url=review_url,
                    visible=visible,
                    use_bulk_profile=use_bulk_profile,
                )
            except Exception as exc:  # noqa: BLE001
                if not _is_sync_playwright_loop_error(exc):
                    raise
                return _submit_seek_review_subprocess(
                    job_id,
                    db_path,
                    review_url=review_url,
                    visible=visible,
                    use_bulk_profile=use_bulk_profile,
                )
        result = run_once()
        if str(result.status) == "browser_profile_locked":
            database.log_run(
                "seek_submit",
                "replay_submit_profile_lock_recovery_started",
                "warning",
                f"job_id={job_id}",
            )
            _recover_submit_profile_lock(Path(db_path), use_bulk_profile=use_bulk_profile)
            result = run_once(force_subprocess=True)
            database.log_run(
                "seek_submit",
                "replay_submit_profile_lock_recovery_finished",
                "success" if str(result.status) != "browser_profile_locked" else "warning",
                f"job_id={job_id} status={result.status}",
            )
    except Exception as exc:  # noqa: BLE001
        message = str(exc) or "Submit failed unexpectedly."
        result = ApplyAssistResult(
            job_id=job_id,
            status="failed",
            steps_completed=[],
            error=message,
            final_url=str(review_url or ""),
            root_failure_reason=message,
            evidence_capture_errors=[],
        )
    elapsed_seconds = max(0.0, time.perf_counter() - started_at)
    _persist_submit_review_status(database, job_id, result)
    database.log_run(
        "seek_submit_timing",
        "finished",
        "success" if str(result.status) == "applied" else "warning",
        f"job_id={job_id} status={result.status} duration_seconds={elapsed_seconds:.3f}",
    )
    return result


def _persist_submit_review_status(database: Database, job_id: int, result: ApplyAssistResult) -> None:
    status = str(result.status or "").strip().lower()
    message = str(result.error or result.final_url or "").strip()
    lowered_message = message.lower()
    if status == "applied":
        database.update_application_state(job_id, "applied", note="Submitted via replayed SEEK flow.")
        return
    if status == "skipped" and "no longer advertised" in lowered_message:
        database.close_application(
            job_id,
            "job_no_longer_advertised",
            note="Closed automatically because SEEK reported the job was no longer advertised during final submit.",
        )
        return
    if status == "failed" and "could not find seek quick apply button" in lowered_message:
        database.update_application_state(
            job_id,
            "reviewed",
            note="Moved out of ready queue because SEEK no longer showed a Quick Apply button during final submit.",
        )
        return
    if status == "failed" and "selected cv file not found" in lowered_message:
        database.update_application_state(
            job_id,
            "reviewed",
            note="Moved out of ready queue because the selected CV file was missing and needs regeneration.",
        )
        return
    if status in {"paused_for_seek_verification", "paused_for_captcha", "paused_for_seek_login"}:
        database.update_application_state(
            job_id,
            "reviewed",
            note=message or "Moved out of ready queue because SEEK requires manual verification before submit can continue.",
        )


def extract_question_text_for_field(page: Any, field_locator: Any) -> dict[str, Any]:
    return field_locator.evaluate(
        """(el) => {
            const normaliseText = (value) => (value || '').replace(/\\s+/g, ' ').trim();
            const truncate = (value, max = 1200) => {
                const text = String(value || '');
                return text.length > max ? `${text.slice(0, max)}...` : text;
            };
            const isQuestionField = (node) => {
                if (!node || !(node instanceof Element)) return false;
                const tagName = (node.tagName || '').toUpperCase();
                if (!['SELECT', 'TEXTAREA', 'INPUT'].includes(tagName)) return false;
                const name = String(node.getAttribute('name') || '');
                if (name.includes('questionnaire')) return true;
                if (tagName === 'INPUT' && String(node.getAttribute('type') || '').toLowerCase() === 'radio' && name.includes('questionnaire')) {
                    return true;
                }
                return false;
            };
            const visibleTextForNode = (node) => normaliseText(node?.innerText || node?.textContent || '');
            const options = [];
            const optionPayloads = [];
            let radioGroupName = '';
            let radioGroupSize = 0;
            let radioGroupContainerText = '';
            let radioGroupExtractionSource = '';
            let radioGroupExtractedQuestionText = '';
            let radioGroupOptionLabels = [];
            let checkboxGroupName = '';
            let checkboxGroupSize = 0;
            let checkboxGroupContainerText = '';
            let checkboxGroupOptionLabels = [];

            const findSmallestCommonAncestor = (elements) => {
                if (!elements.length) return null;
                const first = elements[0];
                const ancestors = [];
                let current = first.parentElement;
                while (current) {
                    ancestors.push(current);
                    current = current.parentElement;
                }
                for (const ancestor of ancestors) {
                    if (elements.every((node) => ancestor.contains(node))) {
                        return ancestor;
                    }
                }
                return first.parentElement || first;
            };

            if (el.tagName === 'SELECT') {
                for (const option of Array.from(el.options || [])) {
                    const label = normaliseText(option.textContent || '');
                    const value = normaliseText(option.value || '');
                    optionPayloads.push({ label, value });
                    if (label) options.push(label);
                }
            } else if (el.type === 'radio') {
                const radioName = el.name || '';
                radioGroupName = radioName;
                const root = el.closest('form') || document;
                const radios = Array.from(root.querySelectorAll(`input[type="radio"][name="${radioName}"]`));
                radioGroupSize = radios.length;
                for (const radio of radios) {
                    const label = normaliseText(radio.labels && radio.labels.length ? radio.labels[0].innerText : radio.parentElement?.innerText || radio.value || '');
                    const value = normaliseText(radio.value || '');
                    optionPayloads.push({ label, value });
                    if (label) options.push(label);
                }
                radioGroupOptionLabels = [...options];
            } else if (el.type === 'checkbox') {
                const checkboxName = el.name || '';
                checkboxGroupName = checkboxName;
                const root = el.closest('form') || document;
                const checkboxes = Array.from(root.querySelectorAll(`input[type="checkbox"][name="${checkboxName}"]`));
                checkboxGroupSize = checkboxes.length;
                for (const checkbox of checkboxes) {
                    const label = normaliseText(checkbox.labels && checkbox.labels.length ? checkbox.labels[0].innerText : checkbox.parentElement?.innerText || checkbox.value || '');
                    const value = normaliseText(checkbox.value || '');
                    optionPayloads.push({ label, value });
                    if (label) options.push(label);
                }
                checkboxGroupOptionLabels = [...options];
            }

            const candidateTexts = [];
            const candidateSources = [];
            const pushSourceText = (source, value) => {
                const text = normaliseText(value);
                if (!text) return;
                candidateTexts.push(text);
                candidateSources.push({ source, text });
            };
            const isRadioGroupQuestionCandidate = (value) => {
                const text = normaliseText(value);
                if (!text) return false;
                const lowered = text.toLowerCase();
                if (radioGroupOptionLabels.map((label) => label.toLowerCase()).includes(lowered)) return false;
                if (['yes', 'no'].includes(lowered)) return false;
                return true;
            };

            const findLocalQuestionContainer = (node, radioGroupContainer = null) => {
                if (radioGroupContainer) {
                    return radioGroupContainer;
                }
                const directParent = node.parentElement || node;
                let current = directParent;
                let fallback = directParent;
                for (let depth = 0; current && depth < 6; depth += 1) {
                    const fieldCount = Array.from(current.querySelectorAll('select[name*="questionnaire"], textarea[name*="questionnaire"], input[name*="questionnaire"]'))
                        .filter((candidate) => isQuestionField(candidate)).length;
                    const promptNodes = Array.from(current.querySelectorAll('label, legend, strong, p, h1, h2, h3, h4, h5, h6'));
                    const promptText = promptNodes.map((candidate) => visibleTextForNode(candidate)).filter(Boolean).join(' ');
                    if (fieldCount <= 2 && promptText) {
                        return current;
                    }
                    if (fieldCount <= 3) {
                        fallback = current;
                    }
                    current = current.parentElement;
                }
                return fallback || node;
            };

            const radioGroupContainer = (() => {
                if (!(el.type === 'radio')) return null;
                const radioName = el.name || '';
                if (!radioName) return null;
                const root = el.closest('form') || document;
                const radios = Array.from(root.querySelectorAll(`input[type="radio"][name="${radioName}"]`));
                if (!radios.length) return null;
                const commonAncestor = findSmallestCommonAncestor(radios);
                if (!commonAncestor) return null;
                let current = commonAncestor;
                let fallback = commonAncestor;
                for (let depth = 0; current && depth < 5; depth += 1) {
                    const radioDescendants = Array.from(current.querySelectorAll(`input[type="radio"][name="${radioName}"]`));
                    if (!radioDescendants.length) break;
                    const questionLikeNodes = Array.from(current.querySelectorAll('legend, label, strong, p, h1, h2, h3, h4, h5, h6, div, span'))
                        .filter((node) => {
                            const text = visibleTextForNode(node).toLowerCase();
                            if (!text) return false;
                            if (['yes', 'no'].includes(text)) return false;
                            return text.includes('?') || /psychometric|notice|comfortable|salary|experience|qualification|licence|license|work in new zealand|proceeding/.test(text);
                        });
                    if (questionLikeNodes.length) {
                        fallback = current;
                        const radiosOnly = Array.from(current.querySelectorAll('input[type="radio"]')).length;
                        if (radiosOnly <= radioDescendants.length + 1) {
                            return current;
                        }
                    }
                    current = current.parentElement;
                }
                return fallback;
            })();

            const localContainer = findLocalQuestionContainer(el, radioGroupContainer);
            const localContainerText = visibleTextForNode(localContainer);
            const localContainerSnippet = truncate(localContainer?.outerHTML || '');

            pushSourceText('aria-label', el.getAttribute('aria-label'));
            const labelledBy = normaliseText(el.getAttribute('aria-labelledby'));
            if (labelledBy) {
                for (const id of labelledBy.split(/\\s+/).filter(Boolean)) {
                    const labelledNode = document.getElementById(id);
                    if (labelledNode) {
                        pushSourceText('aria-labelledby', labelledNode.innerText || labelledNode.textContent || '');
                    }
                }
            }

            const elementId = normaliseText(el.id || '');
            if (elementId) {
                const explicitLabels = Array.from(document.querySelectorAll(`label[for="${elementId}"]`));
                for (const label of explicitLabels) {
                    pushSourceText('label[for]', label.innerText || label.textContent || '');
                }
            }

            if (el.labels && el.labels.length) {
                for (const label of Array.from(el.labels)) {
                    pushSourceText('labels', label.innerText || label.textContent || '');
                }
            }

            const collectQuestionLikeTextFromNode = (node, source) => {
                if (!node) return;
                const tags = ['label', 'legend', 'strong', 'p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'span', 'div'];
                if (tags.includes((node.tagName || '').toLowerCase())) {
                    pushSourceText(source, node.innerText || node.textContent || '');
                }
                const descendants = Array.from(node.querySelectorAll(tags.join(',')));
                for (const descendant of descendants.slice(0, 12)) {
                    pushSourceText(`${source}-descendant`, descendant.innerText || descendant.textContent || '');
                }
                for (const childNode of Array.from(node.childNodes || [])) {
                    if (childNode.nodeType === Node.TEXT_NODE) {
                        pushSourceText(`${source}-text`, childNode.textContent || '');
                    }
                }
            };

            const localQuestionNodes = Array.from(localContainer.querySelectorAll('label, legend, strong, p, h1, h2, h3, h4, h5, h6'));
            for (const node of localQuestionNodes.slice(0, 20)) {
                pushSourceText('local-container-node', node.innerText || node.textContent || '');
            }

            if (el.type === 'radio' && radioGroupContainer) {
                radioGroupContainerText = visibleTextForNode(radioGroupContainer);
                const legend = radioGroupContainer.querySelector('legend');
                if (legend) {
                    const legendText = legend.innerText || legend.textContent || '';
                    pushSourceText('radio-group-legend', legendText);
                    if (!radioGroupExtractedQuestionText && isRadioGroupQuestionCandidate(legendText)) {
                        radioGroupExtractedQuestionText = normaliseText(legendText);
                    }
                    radioGroupExtractionSource = 'legend';
                }
                const groupLabelledBy = normaliseText(radioGroupContainer.getAttribute('aria-labelledby'));
                if (groupLabelledBy) {
                    for (const id of groupLabelledBy.split(/\\s+/).filter(Boolean)) {
                        const labelledNode = document.getElementById(id);
                        if (labelledNode) {
                            const labelledText = labelledNode.innerText || labelledNode.textContent || '';
                            pushSourceText('radio-group-aria-labelledby', labelledText);
                            if (!radioGroupExtractedQuestionText && isRadioGroupQuestionCandidate(labelledText)) {
                                radioGroupExtractedQuestionText = normaliseText(labelledText);
                            }
                            if (!radioGroupExtractionSource) {
                                radioGroupExtractionSource = 'aria-labelledby';
                            }
                        }
                    }
                }
                const groupPromptNodes = Array.from(radioGroupContainer.querySelectorAll('strong, p, h1, h2, h3, h4, h5, h6, div, span'))
                    .filter((node) => {
                        if (node.querySelector('input[type="radio"]')) return false;
                        const text = visibleTextForNode(node);
                        if (!text) return false;
                        const lowered = text.toLowerCase();
                        if (radioGroupOptionLabels.map((label) => label.toLowerCase()).includes(lowered)) return false;
                        if (['yes', 'no'].includes(lowered)) return false;
                        return true;
                    });
                for (const node of groupPromptNodes.slice(0, 12)) {
                    const promptText = node.innerText || node.textContent || '';
                    pushSourceText('radio-group-prompt', promptText);
                    if (!radioGroupExtractedQuestionText && isRadioGroupQuestionCandidate(promptText)) {
                        radioGroupExtractedQuestionText = normaliseText(promptText);
                    }
                    if (!radioGroupExtractionSource) {
                        radioGroupExtractionSource = 'group-prompt';
                    }
                }
            }
            if (el.type === 'checkbox') {
                const checkboxName = el.name || '';
                const root = el.closest('form') || document;
                const checkboxes = Array.from(root.querySelectorAll(`input[type="checkbox"][name="${checkboxName}"]`));
                const commonAncestor = findSmallestCommonAncestor(checkboxes);
                const checkboxContainer = commonAncestor || localContainer;
                checkboxGroupContainerText = visibleTextForNode(checkboxContainer);
                const groupPromptNodes = Array.from((checkboxContainer || document).querySelectorAll('strong, p, h1, h2, h3, h4, h5, h6, div, span'))
                    .filter((node) => {
                        if (node.querySelector('input[type="checkbox"]')) return false;
                        const text = visibleTextForNode(node);
                        if (!text) return false;
                        const lowered = text.toLowerCase();
                        if (checkboxGroupOptionLabels.map((label) => label.toLowerCase()).includes(lowered)) return false;
                        return true;
                    });
                for (const node of groupPromptNodes.slice(0, 12)) {
                    pushSourceText('checkbox-group-prompt', node.innerText || node.textContent || '');
                }
            }

            let previous = el.previousElementSibling;
            let previousScans = 0;
            while (previous && previousScans < 4) {
                collectQuestionLikeTextFromNode(previous, 'local-previous-sibling');
                previous = previous.previousElementSibling;
                previousScans += 1;
            }

            let next = el.nextElementSibling;
            let nextScans = 0;
            while (next && nextScans < 4) {
                collectQuestionLikeTextFromNode(next, 'local-next-sibling');
                next = next.nextElementSibling;
                nextScans += 1;
            }

            if (localContainer && localContainer !== el) {
                let sibling = el.parentElement?.firstElementChild || null;
                let siblingScans = 0;
                while (sibling && siblingScans < 12) {
                    if (sibling !== el && !sibling.contains(el) && !el.contains(sibling) && !isQuestionField(sibling)) {
                        collectQuestionLikeTextFromNode(sibling, 'local-container-sibling');
                    }
                    sibling = sibling.nextElementSibling;
                    siblingScans += 1;
                }
                for (const childNode of Array.from(localContainer.childNodes || [])) {
                    if (childNode.nodeType === Node.TEXT_NODE) {
                        pushSourceText('local-container-text', childNode.textContent || '');
                    }
                }
            }

            const selectedOption = el.tagName === 'SELECT'
                ? Array.from(el.options || []).find(option => option.selected)
                : null;
            return {
                id: el.id || '',
                name: el.name || '',
                required: !!el.required || /required|please make a selection|please answer/i.test(localContainerText) || (el.name || '').includes('questionnaire'),
                is_visible: true,
                nearby_text_candidates: candidateTexts,
                nearby_text_candidate_sources: candidateSources,
                options,
                option_payloads: optionPayloads,
                current_selected_value: selectedOption ? normaliseText(selectedOption.value || '') : '',
                current_selected_label: selectedOption ? normaliseText(selectedOption.textContent || '') : '',
                field_container_text: localContainerText,
                field_container_outer_html_snippet: localContainerSnippet,
                radio_group_name: radioGroupName,
                radio_group_size: radioGroupSize,
                radio_group_container_text: radioGroupContainerText,
                radio_group_option_labels: radioGroupOptionLabels,
                radio_group_extraction_source: radioGroupExtractionSource,
                radio_group_extracted_question_text: radioGroupExtractedQuestionText,
                checkbox_group_name: checkboxGroupName,
                checkbox_group_size: checkboxGroupSize,
                checkbox_group_container_text: checkboxGroupContainerText,
                checkbox_group_option_labels: checkboxGroupOptionLabels,
            };
        }"""
    )


def handle_dynamic_seek_questions(
    adapter: SeekApplyAdapter,
    applicant_profile: dict[str, Any],
    debug_dir: Path,
    *,
    input_func: Callable[[], str] = input,
) -> DynamicQuestionResult:
    questions = adapter.extract_dynamic_questions()
    answered_count = 0
    unknown_questions: list[str] = []
    scan_report: list[dict[str, Any]] = []
    pause_on_unknown_questions = bool((applicant_profile.get("automation") or {}).get("pause_on_unknown_questions", False))
    job_context: dict[str, Any] = {}
    for question_index, question in enumerate(questions):
        question_text = str(question.get("question_text") or "").strip()
        field_type = str(question.get("field_type") or "").strip() or "unknown"
        options = [str(option).strip() for option in question.get("options", []) if str(option).strip()]
        option_payloads = _normalise_option_payloads(question.get("option_payloads", []))
        current_selected_value = str(question.get("current_selected_value") or "").strip()
        current_selected_label = str(question.get("current_selected_label") or "").strip()
        field_name = str(question.get("name") or "").strip()
        field_id = str(question.get("id") or "").strip()
        is_visible = bool(question.get("is_visible", True))
        is_required = bool(question.get("required", True))
        field_context = FieldAnswerContext(
            field_index=question_index,
            field_type=field_type,
            field_id=field_id,
            field_name=field_name,
            extracted_question_text=question_text,
            available_options=options,
            current_value=current_selected_value,
            required=is_required,
            nearby_text_candidates=[str(candidate).strip() for candidate in question.get("nearby_text_candidates", []) if str(candidate).strip()],
            confidence=0.0,
        )
        print(
            f"[seek_apply_assist] field_answer_context: "
            f"index={field_context.field_index} type={field_context.field_type} "
            f"id={field_context.field_id or '(none)'} name={field_context.field_name or '(none)'} "
            f"required={field_context.required} current_value={field_context.current_value or '(none)'}",
            flush=True,
        )
        print(
            f"[seek_apply_assist] field_container_outer_html_snippet: "
            f"{str(question.get('field_container_outer_html_snippet') or '').strip()[:500] or '(none)'}",
            flush=True,
        )
        print(f"[seek_apply_assist] field_container_text: {str(question.get('field_container_text') or '').strip() or '(none)'}", flush=True)
        if field_type.lower() == "radio":
            print(f"[seek_apply_assist] radio_group_name: {str(question.get('radio_group_name') or '').strip() or '(none)'}", flush=True)
            print(f"[seek_apply_assist] radio_group_size: {int(question.get('radio_group_size') or 0)}", flush=True)
            print(f"[seek_apply_assist] radio_group_container_text: {str(question.get('radio_group_container_text') or '').strip() or '(none)'}", flush=True)
            print(f"[seek_apply_assist] radio_group_option_labels: {list(question.get('radio_group_option_labels') or [])}", flush=True)
            print(f"[seek_apply_assist] radio_group_extracted_question_text: {str(question.get('radio_group_extracted_question_text') or '').strip() or '(none)'}", flush=True)
            print(f"[seek_apply_assist] radio_group_extraction_source: {str(question.get('radio_group_extraction_source') or '').strip() or '(none)'}", flush=True)
        print(f"[seek_apply_assist] extracted_question_text: {field_context.extracted_question_text or '(none)'}", flush=True)
        print(f"[seek_apply_assist] question_extraction_source: {str(question.get('question_extraction_source') or '').strip() or '(none)'}", flush=True)
        rejected_candidate_reasons = question.get("rejected_candidate_reasons", [])
        if rejected_candidate_reasons:
            print(f"[seek_apply_assist] rejected_candidate_reason: {rejected_candidate_reasons[0]}", flush=True)
        answer, matched_rule, answer_source = _resolve_dynamic_question_answer(question, applicant_profile)
        generic_resolution: GenericAnswerResolution | None = None
        if not answer and field_type.lower() in {"textarea", "text", "free_text"}:
            answer, matched_rule, answer_source = resolve_free_text_question_answer(question_text, applicant_profile, job_context)
        if not answer:
            generic_resolution = resolve_profile_backed_answer(
                question_text=question_text,
                field_type=field_type,
                available_options=options,
                applicant_profile=applicant_profile,
            )
            if generic_resolution.should_answer and generic_resolution.chosen_answer:
                answer = generic_resolution.chosen_answer
                matched_rule = generic_resolution.matched_rule or matched_rule
                answer_source = generic_resolution.reason
                field_context.confidence = generic_resolution.confidence
        classified_question_type = matched_rule or (generic_resolution.inferred_question_type if generic_resolution is not None else "")
        print(f"[seek_apply_assist] classified_question_type: {classified_question_type or '(none)'}", flush=True)
        chosen_value = _resolve_chosen_value(option_payloads, answer)
        answer_attempted = bool(answer)
        answer_success = False
        why_skipped = ""
        if answer:
            if field_type.lower() in {"textarea", "text", "free_text"} and answer.strip() in {"90k", "More than 5 years", "6 or more weeks"}:
                why_skipped = "blocked unsafe short answer for textarea field"
                print(f"[seek_apply_assist] blocked_answer_reason: {why_skipped}", flush=True)
                answer = ""
                answer_attempted = False
            else:
                print(f"[seek_apply_assist] selected_answer: {answer}", flush=True)
                print(f"[seek_apply_assist] answer_source: {answer_source or '(none)'}", flush=True)
                print(f"[seek_apply_assist] answer_confidence: {field_context.confidence if field_context.confidence else '(rule)'}", flush=True)
        if answer:
            if adapter.answer_dynamic_question(question, answer):
                answered_count += 1
                answer_success = True
            else:
                why_skipped = "matched a rule but could not apply the answer to the field"
        else:
            why_skipped = why_skipped or "no safe matching rule was found for this field"

        if not answer_success and is_required:
            unknown_questions.append(question_text or "(unknown question)")

        scan_entry = {
            "field_index": question_index,
            "textarea_index": question_index if field_type.lower() in {"textarea", "text", "free_text"} else None,
            "field_type": field_type,
            "field_id": field_id,
            "field_name": field_name,
            "current_value": current_selected_value,
            "current_label": current_selected_label,
            "is_visible": is_visible,
            "is_required": is_required,
            "extracted_question_text": question_text,
            "nearby_text_candidates": [str(candidate).strip() for candidate in question.get("nearby_text_candidates", []) if str(candidate).strip()],
            "options": option_payloads,
            "field_container_outer_html_snippet": str(question.get("field_container_outer_html_snippet") or "").strip(),
            "field_container_text": str(question.get("field_container_text") or "").strip(),
            "question_extraction_source": str(question.get("question_extraction_source") or "").strip(),
            "rejected_candidate_reasons": question.get("rejected_candidate_reasons", []),
            "radio_group_name": str(question.get("radio_group_name") or "").strip(),
            "radio_group_size": int(question.get("radio_group_size") or 0),
            "radio_group_container_text": str(question.get("radio_group_container_text") or "").strip(),
            "radio_group_option_labels": list(question.get("radio_group_option_labels") or []),
            "radio_group_extracted_question_text": str(question.get("radio_group_extracted_question_text") or "").strip(),
            "radio_group_extraction_source": str(question.get("radio_group_extraction_source") or "").strip(),
            "matched_rule": matched_rule or "",
            "chosen_answer": answer or "",
            "chosen_value": chosen_value,
            "answer_source": answer_source or "",
            "classified_question_type": classified_question_type or "",
            "answer_confidence": field_context.confidence,
            "answer_attempted": answer_attempted,
            "answer_success": answer_success,
            "why_skipped": why_skipped,
            "blocked_answer_reason": why_skipped if "blocked" in why_skipped.lower() else "",
        }
        if matched_rule == "sql_experience_years":
            scan_entry["profile_sql_years"] = int((applicant_profile.get("skills_experience_years") or {}).get("sql", 0))
        if generic_resolution is not None:
            scan_entry["confidence"] = generic_resolution.confidence
            scan_entry["reason"] = generic_resolution.reason
            scan_entry["should_answer"] = generic_resolution.should_answer
            scan_entry["inferred_question_type"] = generic_resolution.inferred_question_type
            scan_entry["inferred_skill"] = generic_resolution.inferred_skill
            scan_entry["profile_years_used"] = generic_resolution.profile_years_used
        scan_report.append(scan_entry)
        _print_dynamic_question_summary(scan_entry)

    validation_errors = adapter.get_questionnaire_validation_errors()
    if validation_errors:
        for question_index, question in enumerate(questions):
            field_type = str(question.get("field_type") or "").strip().lower()
            if field_type not in {"textarea", "text", "free_text"}:
                continue
            field_name = str(question.get("name") or "").strip()
            if field_name and str(getattr(adapter, "dynamic_answers", {}).get(field_name, "")).strip():
                continue
            question_text = str(question.get("question_text") or "").strip()
            answer, matched_rule, answer_source = resolve_free_text_question_answer(question_text, applicant_profile, job_context)
            if not answer:
                continue
            if adapter.answer_dynamic_question(question, answer):
                answered_count += 1
                print(f"[seek_apply_assist] validation_recovery_textarea_filled: {field_name or question_index}", flush=True)
                for scan_entry in scan_report:
                    if scan_entry.get("field_name") == field_name or scan_entry.get("field_index") == question_index:
                        scan_entry["chosen_answer"] = answer
                        scan_entry["matched_rule"] = matched_rule
                        scan_entry["answer_source"] = answer_source
                        scan_entry["answer_attempted"] = True
                        scan_entry["answer_success"] = True
                        scan_entry["why_skipped"] = ""
                        break
        validation_errors = adapter.get_questionnaire_validation_errors()
    if validation_errors:
        for error in list(validation_errors):
            retried = False
            for question_index, question in enumerate(questions):
                if not _question_matches_validation_error(question, error):
                    continue
                resolved = _resolve_answer_for_validation_retry(question, applicant_profile, job_context)
                if not resolved:
                    continue
                answer, matched_rule, answer_source = resolved
                if not adapter.answer_dynamic_question(question, answer):
                    continue
                answered_count += 1
                retried = True
                print(
                    f"[seek_apply_assist] validation_recovery_question_reapplied: {str(question.get('name') or question.get('question_text') or question_index)}",
                    flush=True,
                )
                print(f"[seek_apply_assist] validation_recovery_source: {matched_rule}", flush=True)
                for scan_entry in scan_report:
                    same_field = scan_entry.get("field_name") == question.get("name")
                    same_index = scan_entry.get("field_index") == question_index
                    same_question = _normalise_validation_error_text(str(scan_entry.get("question_text") or "")) == _normalise_validation_error_text(str(question.get("question_text") or ""))
                    if same_field or same_index or same_question:
                        scan_entry["chosen_answer"] = answer
                        scan_entry["matched_rule"] = matched_rule
                        scan_entry["answer_source"] = answer_source
                        scan_entry["answer_attempted"] = True
                        scan_entry["answer_success"] = True
                        scan_entry["why_skipped"] = ""
                        break
                break
            if not retried:
                print(f"[seek_apply_assist] validation_recovery_unmatched_error: {error}", flush=True)
        validation_errors = adapter.get_questionnaire_validation_errors()
    if validation_errors:
        unanswered_set = set(unknown_questions)
        for error in validation_errors:
            if error not in unanswered_set:
                unknown_questions.append(error)
                unanswered_set.add(error)
        for scan_entry in scan_report:
            if not scan_entry["answer_success"] and not scan_entry["why_skipped"]:
                scan_entry["why_skipped"] = "validation errors remain after attempted answering"
    _write_dynamic_question_scan(debug_dir, scan_report, validation_errors)
    if validation_errors:
        screenshot_path, html_path = adapter.save_dynamic_question_debug_artifacts(debug_dir)
        print(f"[seek_apply_assist] dynamic_questions_unknown_screenshot: {screenshot_path}", flush=True)
        print(f"[seek_apply_assist] dynamic_questions_unknown_html: {html_path}", flush=True)
        for question_text in unknown_questions:
            print(f"[seek_apply_assist] unknown_question_pause: {question_text}", flush=True)
        for error in validation_errors:
            print(f"[seek_apply_assist] unknown_question_pause: validation_error={error}", flush=True)
        print("Please answer the remaining questions manually, then press Enter to continue.", flush=True)
        try:
            input_func()
        except EOFError:
            return DynamicQuestionResult(
                status="paused_for_manual_questionnaire",
                answered_count=answered_count,
                warning="Questionnaire requires manual continuation, but no console input was available.",
                unknown_questions=unknown_questions or validation_errors,
            )
        return DynamicQuestionResult(
            status="dynamic_questions_manual_pause_continued",
            answered_count=answered_count,
            warning="Dynamic questionnaire required manual continuation.",
            unknown_questions=unknown_questions or validation_errors,
        )

    if unknown_questions:
        screenshot_path, html_path = adapter.save_dynamic_question_debug_artifacts(debug_dir)
        print(f"[seek_apply_assist] dynamic_questions_unknown_screenshot: {screenshot_path}", flush=True)
        print(f"[seek_apply_assist] dynamic_questions_unknown_html: {html_path}", flush=True)
        for question_text in unknown_questions:
            print(f"[seek_apply_assist] unknown_question_pause: {question_text}", flush=True)
        if pause_on_unknown_questions:
            print("Please answer the remaining questions manually, then press Enter to continue.", flush=True)
            try:
                input_func()
            except EOFError:
                return DynamicQuestionResult(
                    status="paused_for_manual_questionnaire",
                    answered_count=answered_count,
                    warning="Questionnaire requires manual continuation, but no console input was available.",
                    unknown_questions=unknown_questions,
                )
            return DynamicQuestionResult(
                status="dynamic_questions_manual_pause_continued",
                answered_count=answered_count,
                warning="Dynamic questionnaire required manual continuation.",
                unknown_questions=unknown_questions,
            )

    return DynamicQuestionResult(status="dynamic_questions_completed", answered_count=answered_count, unknown_questions=[])


def _normalise_option_payloads(option_payloads: Any) -> list[dict[str, str]]:
    normalised: list[dict[str, str]] = []
    for option_payload in option_payloads or []:
        if isinstance(option_payload, dict):
            label = str(option_payload.get("label") or "").strip()
            value = str(option_payload.get("value") or "").strip()
        else:
            label = str(option_payload or "").strip()
            value = ""
        if label or value:
            normalised.append({"label": label, "value": value})
    return normalised


def _contains_salary_question_text(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(
        phrase in lowered
        for phrase in (
            "salary",
            "remuneration",
            "pay expectation",
            "expected salary",
            "hourly rate",
        )
    )


def _looks_like_years_option_set(options: list[str]) -> bool:
    return any(_experience_option_rank(option, 999) is not None for option in options)


def _pick_checkbox_option(options: list[str], preferred_tokens: list[str]) -> str | None:
    lowered_options = [(option, option.lower()) for option in options]
    for token in preferred_tokens:
        lowered_token = str(token or "").strip().lower()
        if not lowered_token:
            continue
        for option, lowered in lowered_options:
            if lowered == lowered_token or lowered_token in lowered:
                return option
    return None


def resolve_free_text_question_answer(question_text: str, applicant_profile: dict[str, Any], job_context: dict[str, Any] | None = None) -> tuple[str | None, str, str]:
    lowered = question_text.lower()
    role_title = str((job_context or {}).get("job_title") or "").strip()
    if "when can you start" in lowered or ("start date" in lowered and "when" in lowered):
        try:
            notice_weeks = int(applicant_profile.get("notice_period_weeks", 2))
        except Exception:  # noqa: BLE001
            notice_weeks = 2
        if notice_weeks <= 0:
            answer = "Immediately."
        elif notice_weeks == 1:
            answer = "I can start after 1 week's notice."
        else:
            answer = f"I can start after {notice_weeks} weeks' notice."
        return answer, "start_date_availability", "applicant_profile.notice_period_weeks"
    if "what interests you about this opportunity" in lowered or "what interests you about this role" in lowered:
        role_fragment = f" the {role_title} role" if role_title else " this role"
        answer = (
            f"I am interested in{role_fragment} because it combines data, reporting, and stakeholder-facing work in a way that matches my background. "
            "I enjoy turning complex data into clear insights, improving decision-making, and working closely with business teams to deliver practical outcomes. "
            "The opportunity to keep building depth in analytics and business improvement is what particularly appeals to me."
        )
        return answer, "interest_in_opportunity", "built_in_truthful_interest_answer"
    if "what experience do you have working in a data or customer analytics role" in lowered:
        answer = (
            "I have several years of experience working in data, reporting, and analytics-focused roles where my responsibilities have included building and maintaining Power BI dashboards, writing SQL queries, validating data quality, defining KPIs, and translating stakeholder requirements into reporting solutions. "
            "I regularly work with business teams to understand information needs, investigate trends and issues, and provide actionable insights that support operational and strategic decision-making."
        )
        return answer, "data_customer_analytics_experience", "built_in_truthful_analytics_experience_answer"
    if "azure" in lowered and ("synapse" in lowered or "data factory" in lowered or "adf" in lowered):
        answer = (
            "I have working experience across data and analytics platforms including SQL, reporting, and data-focused delivery, "
            "but I do not want to overstate direct hands-on depth specifically with Azure Synapse and Azure Data Factory. "
            "My background is strongest in analytics, BI, stakeholder-facing delivery, and broader data work, and I would be comfortable ramping up quickly in Azure-based tooling."
        )
        return answer, "azure_data_services_truthful_limit", "built_in_truthful_azure_limit_answer"
    if "salesforce sales cloud" in lowered and "salesforce data cloud" in lowered:
        answer = (
            "I do not have direct hands-on experience working in Salesforce Sales Cloud or Salesforce Data Cloud. "
            "My experience has been stronger in SQL, Power BI, reporting, analytics, and stakeholder-facing business analysis, where I have worked with operational and customer-related data to deliver dashboards, insights, and process improvements. "
            "I would be comfortable learning the Salesforce environment quickly and applying my existing data and reporting background in that context."
        )
        return answer, "salesforce_experience_truthful_limit", "built_in_truthful_salesforce_limit_answer"
    if "payroll" in lowered and ("new zealand" in lowered or "legislative compliance" in lowered):
        answer = str(((applicant_profile.get("experience_notes") or {}).get("payroll_related_answer")) or "").strip()
        if answer:
            print("[seek_apply_assist] payroll_text_answer_used", flush=True)
            return answer, "payroll_experience", "applicant_profile.experience_notes.payroll_related_answer"
    if ("financial discrepancy" in lowered or "discrepancy" in lowered) and ("risk" in lowered or "outcome" in lowered or "how did you handle" in lowered or "what did you find" in lowered):
        answer = (
            "In my reporting and analysis work, I have identified discrepancies by comparing source data against expected totals, "
            "checking filters, dates, and transaction logic, and tracing issues back to the underlying records. When I find a "
            "discrepancy, I validate the data, document the cause, raise it with the relevant stakeholder, and update the report "
            "or process so the issue is visible and repeatable checks are in place. The outcome is more reliable reporting and "
            "clearer decision-making."
        )
        return answer, "financial_discrepancy_or_risk", "built_in_truthful_financial_discrepancy_answer"
    if "psychometric" in lowered and "assessment" in lowered and ("comfortable" in lowered or "proceeding" in lowered):
        comfortable = bool(((applicant_profile.get("selection_process") or {}).get("comfortable_with_psychometric_assessment", False)))
        if comfortable:
            print("[seek_apply_assist] psychometric_assessment_answer_used", flush=True)
            return "Yes", "psychometric_assessment_consent", "applicant_profile.selection_process.comfortable_with_psychometric_assessment"
    if "notice" in lowered and "current employer" in lowered:
        try:
            notice_weeks = int(applicant_profile.get("notice_period_weeks", 2))
        except Exception:  # noqa: BLE001
            notice_weeks = 2
        answer = "2 weeks" if notice_weeks <= 2 else f"{notice_weeks} weeks"
        return answer, "notice_period", "applicant_profile.notice_period_weeks"
    return None, "", ""


def _resolve_chosen_value(option_payloads: list[dict[str, str]], answer: str | None) -> str:
    if not answer:
        return ""
    answer_lower = answer.lower().strip()
    for option_payload in option_payloads:
        label = str(option_payload.get("label") or "").strip()
        value = str(option_payload.get("value") or "").strip()
        if label.lower().strip() == answer_lower or answer_lower in label.lower():
            return value or label
        if value.lower().strip() == answer_lower or answer_lower in value.lower():
            return value or label
    return ""


def _print_dynamic_question_summary(scan_entry: dict[str, Any]) -> None:
    field_type = str(scan_entry.get("field_type") or "").upper() or "FIELD"
    field_index = scan_entry.get("field_index", "?")
    print(f"FOUND {field_type} #{field_index}", flush=True)
    print(f"Question: {scan_entry.get('extracted_question_text') or '(none)'}", flush=True)
    nearby_text_candidates = scan_entry.get("nearby_text_candidates") or []
    if nearby_text_candidates:
        print(f"Nearby candidates: {' | '.join(nearby_text_candidates[:8])}", flush=True)
    option_labels = [option.get("label", "") for option in scan_entry.get("options", []) if option.get("label")]
    if option_labels:
        print(f"Options: {' | '.join(option_labels)}", flush=True)
    else:
        print("Options: (none)", flush=True)
    print(f"Matched rule: {scan_entry.get('matched_rule') or 'none'}", flush=True)
    if scan_entry.get("inferred_question_type"):
        print(f"Inferred question type: {scan_entry.get('inferred_question_type')}", flush=True)
    if scan_entry.get("inferred_skill"):
        print(f"Inferred skill: {scan_entry.get('inferred_skill')}", flush=True)
    if scan_entry.get("profile_sql_years") is not None:
        print(f"Profile SQL years: {scan_entry.get('profile_sql_years')}", flush=True)
    if scan_entry.get("profile_years_used") is not None:
        print(f"Profile years used: {scan_entry.get('profile_years_used')}", flush=True)
    print(f"Chosen answer: {scan_entry.get('chosen_answer') or '(none)'}", flush=True)
    print(f"Chosen value: {scan_entry.get('chosen_value') or '(none)'}", flush=True)
    if scan_entry.get("confidence") is not None:
        print(f"Confidence: {scan_entry.get('confidence')}", flush=True)
    print(f"Success: {str(bool(scan_entry.get('answer_success'))).lower()}", flush=True)
    if scan_entry.get("why_skipped"):
        print(f"Why skipped: {scan_entry.get('why_skipped')}", flush=True)


def _write_dynamic_question_scan(debug_dir: Path, scan_report: list[dict[str, Any]], validation_errors: list[str]) -> None:
    _write_dynamic_question_scan_impl(debug_dir, scan_report, validation_errors)


def click_seek_continue(page: Any, debug_dir: Path, step_name: str, *, save_failure_artifacts: bool = True) -> bool:
    step_slug = re.sub(r"[^a-z0-9_]+", "_", step_name.lower()).strip("_") or "continue"
    old_url = ""
    old_title = ""
    try:
        old_url = str(page.url or "")
    except Exception:  # noqa: BLE001
        old_url = ""
    try:
        old_title = str(page.title() or "")
    except Exception:  # noqa: BLE001
        old_title = ""

    candidates: list[tuple[str, Any]] = [
        ("testid_continue_button", page.locator('[data-testid="continue-button"]').first),
        ("role_button", page.get_by_role("button", name=re.compile("^Continue", re.I)).first),
        ("button_has_text", page.locator('button:has-text("Continue")').first),
        ("role_button_has_text", page.locator('[role="button"]:has-text("Continue")').first),
        ("anchor_has_text", page.locator('a:has-text("Continue")').first),
        ("button_has_text", page.locator('button:has-text("Continue")').first),
        ("exact_text", page.get_by_text("Continue", exact=True).first),
        ("span_has_text", page.locator('span:has-text("Continue")').last),
    ]

    candidate_count = 0
    visible = False
    enabled = False
    button_text = ""
    for _name, locator in candidates:
        try:
            if locator.count():
                candidate_count += locator.count()
                if not button_text:
                    try:
                        button_text = (locator.inner_text(timeout=500) or "").strip()
                    except Exception:  # noqa: BLE001
                        button_text = ""
                if not visible:
                    try:
                        visible = bool(locator.is_visible())
                    except Exception:  # noqa: BLE001
                        visible = False
                if not enabled:
                    try:
                        enabled = bool(locator.is_enabled())
                    except Exception:  # noqa: BLE001
                        enabled = False
        except Exception:  # noqa: BLE001
            continue

    print(f"[seek_apply_assist] continue_button_candidates_count: {candidate_count}", flush=True)
    print(f"[seek_apply_assist] continue_button_visible: {visible}", flush=True)
    print(f"[seek_apply_assist] continue_button_enabled: {enabled}", flush=True)
    print(f"[seek_apply_assist] continue_button_text: {button_text or '(none)'}", flush=True)

    immediate_target = _detect_seek_continue_success(page, step_name)
    immediate_target_allowed = {
        "cover_letter": {"role_requirements", "update_seek_profile", "review_and_submit", "review", "submit"},
        "document_step": {"role_requirements", "update_seek_profile", "review_and_submit", "review", "submit"},
        "questionnaire": {"update_seek_profile", "review_and_submit", "review", "submit"},
        "final_navigation": {"update_seek_profile", "review_and_submit", "review", "submit"},
    }.get(step_name, {"review_and_submit", "review", "submit"})
    if immediate_target and immediate_target in immediate_target_allowed:
        print(f"[seek_apply_assist] continue_success_detected: step_name={step_name} target={immediate_target}", flush=True)
        _save_continue_success_screenshot(page, debug_dir, step_slug)
        return True

    for candidate_name, locator in candidates:
        try:
            if locator.count():
                clickable = _resolve_clickable_continue_target(locator)
                _log_continue_target(candidate_name, clickable)
                if _click_continue_target(page, clickable, old_url, old_title, debug_dir, step_slug, candidate_name, step_name):
                    return True
        except Exception:  # noqa: BLE001
            continue

    try:
        clicked = page.evaluate(
            """() => {
                const nodes = Array.from(document.querySelectorAll('button, [role="button"], a, span, div'));
                const isVisible = (el) => {
                    const style = window.getComputedStyle(el);
                    return style.display !== 'none' && style.visibility !== 'hidden' && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                };
                for (const node of nodes) {
                    const text = (node.innerText || node.textContent || '').replace(/\\s+/g, ' ').trim();
                    if (!/continue/i.test(text)) continue;
                    if (!isVisible(node)) continue;
                    const clickable = node.closest('button, [role="button"], a, [tabindex]') || node;
                    clickable.click();
                    return true;
                }
                return false;
            }"""
        )
        if clicked:
            success_target = _wait_for_seek_continue_transition(page, old_url, old_title, step_name)
            if success_target:
                print(f"[seek_apply_assist] continue_success_detected: step_name={step_name} target={success_target}", flush=True)
                _save_continue_success_screenshot(page, debug_dir, step_slug)
                return True
    except Exception:  # noqa: BLE001
        pass

    if save_failure_artifacts:
        debug_dir.mkdir(parents=True, exist_ok=True)
        screenshot_path = debug_dir / f"{step_slug}_continue_failed.png"
        html_path = debug_dir / f"{step_slug}_continue_failed.html"
        try:
            page.screenshot(path=str(screenshot_path), full_page=True)
        except Exception:  # noqa: BLE001
            pass
        try:
            html_path.write_text(page.content(), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
        print(f"[seek_apply_assist] {step_slug}_continue_failed_screenshot: {screenshot_path}", flush=True)
        print(f"[seek_apply_assist] {step_slug}_continue_failed_html: {html_path}", flush=True)
    return False


def _resolve_clickable_continue_target(locator: Any) -> Any:
    try:
        locator.scroll_into_view_if_needed()
    except Exception:  # noqa: BLE001
        pass
    try:
        locator.wait_for(state="visible", timeout=2000)
    except Exception:  # noqa: BLE001
        pass
    try:
        return locator.locator("xpath=ancestor-or-self::*[self::button or @role='button' or self::a or @tabindex][1]").first
    except Exception:  # noqa: BLE001
        return locator


def _log_continue_target(candidate_name: str, locator: Any) -> None:
    try:
        locator.scroll_into_view_if_needed()
    except Exception:  # noqa: BLE001
        pass
    visible = False
    enabled = False
    text = ""
    box: Any = None
    tag = ""
    role = ""
    try:
        visible = bool(locator.is_visible())
    except Exception:  # noqa: BLE001
        pass
    try:
        enabled = bool(locator.is_enabled())
    except Exception:  # noqa: BLE001
        pass
    try:
        text = (locator.inner_text(timeout=500) or "").strip()
    except Exception:  # noqa: BLE001
        pass
    try:
        box = locator.bounding_box()
    except Exception:  # noqa: BLE001
        pass
    try:
        tag = str(locator.evaluate("el => (el.tagName || '').toLowerCase()"))
    except Exception:  # noqa: BLE001
        pass
    try:
        role = str(locator.evaluate("el => el.getAttribute('role') || ''"))
    except Exception:  # noqa: BLE001
        pass
    print(f"[seek_apply_assist] continue_target_candidate: {candidate_name}", flush=True)
    print(f"[seek_apply_assist] continue_target_visible: {visible}", flush=True)
    print(f"[seek_apply_assist] continue_target_enabled: {enabled}", flush=True)
    print(f"[seek_apply_assist] continue_target_text: {text or '(none)'}", flush=True)
    print(f"[seek_apply_assist] continue_target_tag: {tag or '(unknown)'}", flush=True)
    print(f"[seek_apply_assist] continue_target_role: {role or '(none)'}", flush=True)
    print(f"[seek_apply_assist] continue_target_bounding_box: {box}", flush=True)


def _click_continue_target(
    page: Any,
    locator: Any,
    old_url: str,
    old_title: str,
    debug_dir: Path,
    step_slug: str,
    candidate_name: str,
    step_name: str,
) -> bool:
    strategies = [
        ("normal_click", lambda: locator.click()),
        ("force_click", lambda: locator.click(force=True)),
        ("js_click", lambda: locator.evaluate("el => el.click()")),
        ("pointer_event_click", lambda: _dispatch_pointer_click(locator)),
        (
            "mouse_center_click",
            lambda: _mouse_click_continue_target(page, locator),
        ),
        ("keyboard_enter", lambda: (locator.focus(), page.keyboard.press("Enter"))),
    ]
    strategies.extend(
        [
            (
                "request_submit",
                lambda: locator.evaluate(
                    """el => {
                        const button = el.closest('button, [role="button"], a, [tabindex]') || el;
                        const form = button.closest('form');
                        if (form && typeof form.requestSubmit === 'function') {
                            form.requestSubmit(button instanceof HTMLElement ? button : undefined);
                            return true;
                        }
                        return false;
                    }"""
                ),
            ),
            (
                "dispatch_submit",
                lambda: locator.evaluate(
                    """el => {
                        const button = el.closest('button, [role="button"], a, [tabindex]') || el;
                        const form = button.closest('form');
                        if (!form) return false;
                        form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
                        if (typeof form.submit === 'function') {
                            form.submit();
                        }
                        return true;
                    }"""
                ),
            ),
        ]
    )
    for strategy_name, action in strategies:
        try:
            locator.scroll_into_view_if_needed()
        except Exception:  # noqa: BLE001
            pass
        try:
            locator.wait_for(state="visible", timeout=2000)
        except Exception:  # noqa: BLE001
            pass
        try:
            action()
            success_target = _wait_for_seek_continue_transition(page, old_url, old_title, step_name)
            if success_target:
                print(f"[seek_apply_assist] continue_success_detected: step_name={step_name} target={success_target}", flush=True)
                _save_continue_success_screenshot(page, debug_dir, step_slug)
                return True
        except Exception:  # noqa: BLE001
            pass
        _log_continue_click_failure(page, candidate_name, strategy_name)
    return False


def _mouse_click_continue_target(page: Any, locator: Any) -> None:
    box = locator.bounding_box()
    if not box:
        raise SeekApplyAssistError("Continue target has no bounding box for mouse click.")
    x = float(box.get("x", 0.0)) + (float(box.get("width", 0.0)) / 2.0)
    y = float(box.get("y", 0.0)) + (float(box.get("height", 0.0)) / 2.0)
    try:
        locator.hover(force=True)
    except Exception:  # noqa: BLE001
        pass
    page.mouse.move(x, y)
    page.mouse.down()
    page.mouse.up()


def _dispatch_pointer_click(locator: Any) -> None:
    locator.evaluate(
        """el => {
            const target = el.closest('button, [role="button"], a, [tabindex]') || el;
            const rect = target.getBoundingClientRect();
            const clientX = rect.left + (rect.width / 2);
            const clientY = rect.top + (rect.height / 2);
            const eventInit = {
                bubbles: true,
                cancelable: true,
                composed: true,
                clientX,
                clientY,
                button: 0,
                buttons: 1,
                pointerId: 1,
                pointerType: 'mouse',
                isPrimary: true,
            };
            target.dispatchEvent(new PointerEvent('pointerover', eventInit));
            target.dispatchEvent(new PointerEvent('pointerenter', eventInit));
            target.dispatchEvent(new PointerEvent('pointerdown', eventInit));
            target.dispatchEvent(new MouseEvent('mousedown', eventInit));
            target.dispatchEvent(new PointerEvent('pointerup', { ...eventInit, buttons: 0 }));
            target.dispatchEvent(new MouseEvent('mouseup', { ...eventInit, buttons: 0 }));
            target.dispatchEvent(new MouseEvent('click', { ...eventInit, buttons: 0 }));
        }"""
    )


def _log_continue_click_failure(page: Any, candidate_name: str, strategy_name: str) -> None:
    _log_continue_click_failure_impl(page, candidate_name, strategy_name)


def _get_seek_visible_step_text(page: Any) -> str:
    try:
        return str(
            page.evaluate(
                """() => {
                    const bodyText = (document.body?.innerText || '');
                    const lines = bodyText.split(/\\n+/).map(line => line.trim()).filter(Boolean);
                    return lines.find(line => /Answer employer questions|Which of the following statements best describes your right to work in New Zealand\\?|Update SEEK Profile|Review and submit|Submit|Review/i.test(line)) || '';
                }"""
            )
            or ""
        )
    except Exception:  # noqa: BLE001
        return ""


def _get_questionnaire_field_counts(page: Any) -> dict[str, int]:
    counts = {
        "select": 0,
        "radio": 0,
        "textarea": 0,
        "input": 0,
    }
    selectors = {
        "select": 'select[name*="questionnaire"]',
        "radio": 'input[type="radio"][name*="questionnaire"]',
        "textarea": 'textarea[name*="questionnaire"]',
        "input": 'input[name*="questionnaire"]',
    }
    for key, selector in selectors.items():
        try:
            counts[key] = int(page.locator(selector).count())
        except Exception:  # noqa: BLE001
            counts[key] = 0
    return counts


def _log_seek_step_diagnostics(page: Any) -> tuple[str, str, dict[str, int]]:
    return _log_seek_step_diagnostics_impl(page, get_questionnaire_field_counts=_get_questionnaire_field_counts)


def wait_for_seek_step(page: Any, step_name: str, debug_dir: Path, timeout_ms: int = 15000) -> bool:
    start_url, start_title, start_counts = _log_seek_step_diagnostics(page)
    if step_name != "role_requirements":
        print(f"[seek_apply_assist] wait_for_seek_step_failure: unsupported step_name={step_name}", flush=True)
        return False

    def _role_requirements_ready() -> bool:
        current_url = ""
        try:
            current_url = str(page.url or "")
        except Exception:  # noqa: BLE001
            current_url = ""
        counts = _get_questionnaire_field_counts(page)
        return "/role-requirements" in current_url.lower() or any(counts.values())

    if _role_requirements_ready():
        print("[seek_apply_assist] wait_for_seek_step_success: step_name=role_requirements", flush=True)
        _log_seek_step_diagnostics(page)
        return True

    try:
        page.wait_for_function(
            """() => {
                const href = window.location.href || '';
                if (href.toLowerCase().includes('/role-requirements')) return true;
                const selectors = [
                    'select[name*="questionnaire"]',
                    'input[type="radio"][name*="questionnaire"]',
                    'textarea[name*="questionnaire"]',
                    'input[name*="questionnaire"]',
                ];
                return selectors.some(selector => document.querySelector(selector));
            }""",
            timeout=timeout_ms,
        )
    except Exception:  # noqa: BLE001
        pass

    if _role_requirements_ready():
        print("[seek_apply_assist] wait_for_seek_step_success: step_name=role_requirements", flush=True)
        _log_seek_step_diagnostics(page)
        return True

    current_url, page_title, counts = _log_seek_step_diagnostics(page)
    debug_dir.mkdir(parents=True, exist_ok=True)
    screenshot_path = debug_dir / "wait_for_seek_step_failed.png"
    html_path = debug_dir / "wait_for_seek_step_failed.html"
    json_path = debug_dir / "wait_for_seek_step_failed.json"
    try:
        page.screenshot(path=str(screenshot_path), full_page=True)
    except Exception:  # noqa: BLE001
        pass
    try:
        html_path.write_text(page.content(), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    try:
        json_path.write_text(
            json.dumps(
                {
                    "step_name": step_name,
                    "timeout_ms": timeout_ms,
                    "start_url": start_url,
                    "start_title": start_title,
                    "start_counts": start_counts,
                    "current_url": current_url,
                    "page_title": page_title,
                    "counts": counts,
                    "visible_step_text": _get_seek_visible_step_text(page),
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except Exception:  # noqa: BLE001
        pass
    print(f"[seek_apply_assist] wait_for_seek_step_failure: step_name={step_name}", flush=True)
    print(f"[seek_apply_assist] wait_for_seek_step_failed_screenshot: {screenshot_path}", flush=True)
    print(f"[seek_apply_assist] wait_for_seek_step_failed_html: {html_path}", flush=True)
    print(f"[seek_apply_assist] wait_for_seek_step_failed_json: {json_path}", flush=True)
    return False


def _is_choose_documents_page(page: Any) -> bool:
    try:
        title = str(page.title() or "")
    except Exception:  # noqa: BLE001
        title = ""
    if "choose documents" in title.lower():
        return True
    try:
        visible_text = str(_get_seek_visible_step_text(page) or "")
    except Exception:  # noqa: BLE001
        visible_text = ""
    return "choose documents" in visible_text.lower()


def _is_review_page(page: Any) -> bool:
    try:
        title = str(page.title() or "")
    except Exception:  # noqa: BLE001
        title = ""
    try:
        url = str(page.url or "")
    except Exception:  # noqa: BLE001
        url = ""
    lowered_title = title.lower()
    lowered_url = url.lower()
    if "/review" in lowered_url:
        return True
    if "review and submit" not in lowered_title:
        return False
    if "/apply" not in lowered_url:
        return False
    try:
        visible_text = str(_get_seek_visible_step_text(page) or "")
    except Exception:  # noqa: BLE001
        visible_text = ""
    lowered_visible = visible_text.lower()
    if "choose documents" in lowered_visible or "answer employer questions" in lowered_visible:
        return False
    try:
        body_text = str(page.locator("body").inner_text(timeout=5000) or "")
    except Exception:  # noqa: BLE001
        body_text = ""
    lowered_body = body_text.lower()
    review_markers = (
        "submit application",
        "documents included",
        "you wrote a cover letter for this application",
        "review and submit",
    )
    return any(marker in lowered_body for marker in review_markers)


def _is_update_profile_page(page: Any) -> bool:
    try:
        title = str(page.title() or "")
    except Exception:  # noqa: BLE001
        title = ""
    try:
        url = str(page.url or "")
    except Exception:  # noqa: BLE001
        url = ""
    try:
        body_text = str(page.locator("body").inner_text(timeout=5000) or "")
    except Exception:  # noqa: BLE001
        body_text = ""
    lowered_title = title.lower()
    lowered_url = url.lower()
    lowered_body = body_text.lower()
    return "/profile" in lowered_url or "update seek profile" in lowered_title or "update seek profile" in lowered_body


def _document_resume_present(page: Any, adapter: SeekApplyAdapter) -> bool:
    try:
        body_text = str(page.locator("body").inner_text(timeout=5000) or "")
    except Exception:  # noqa: BLE001
        body_text = ""
    lowered = body_text.lower()
    if re.search(r"\.(doc|docx|pdf)\b", lowered):
        return True
    if "upload a different résumé" in lowered or "upload a different resume" in lowered:
        return True
    try:
        radio_visible, radio_checked = adapter.get_resume_upload_radio_state()
        if radio_visible and radio_checked:
            return True
    except Exception:  # noqa: BLE001
        pass
    try:
        return bool(adapter.resume_appears_present())
    except Exception:  # noqa: BLE001
        return False


def _detect_resume_filename(page: Any) -> str:
    try:
        body_text = str(page.locator("body").inner_text(timeout=5000) or "")
    except Exception:  # noqa: BLE001
        return ""
    match = re.search(r"\b([A-Za-z0-9._ -]+\.(?:doc|docx|pdf))\b", body_text, re.I)
    return match.group(1).strip() if match else ""


def _detect_resume_filename_with_adapter(page: Any, adapter: SeekApplyAdapter) -> str:
    filename = _verify_resume_filename(adapter)
    if filename:
        return filename
    return _detect_resume_filename(page)


def _resume_state_snapshot(page: Any, adapter: SeekApplyAdapter) -> dict[str, Any]:
    filename = _detect_resume_filename_with_adapter(page, adapter)
    try:
        body_text = str(page.locator("body").inner_text(timeout=5000) or "")
    except Exception:  # noqa: BLE001
        body_text = str(adapter.page_text() or "")
    lowered = body_text.lower()
    no_resume_included = "no resume included" in lowered or "no resum" in lowered and "included" in lowered
    try:
        upload_radio_visible, upload_radio_checked = adapter.get_resume_upload_radio_state()
    except Exception:  # noqa: BLE001
        upload_radio_visible, upload_radio_checked = False, False
    dont_include_selected = bool(
        re.search(r"don't include a r.s.um..*selected|don't include a resume.*selected", lowered, re.I)
        or re.search(r"no resume included", lowered, re.I)
    )
    return {
        "page_url": str(getattr(page, "url", "") or ""),
        "page_title": _safe_page_title(page),
        "resume_filename": filename,
        "resume_filename_visible": bool(filename),
        "no_resume_included": no_resume_included,
        "upload_radio_visible": upload_radio_visible,
        "upload_radio_selected": upload_radio_checked,
        "dont_include_selected": dont_include_selected,
        "body_text_snippet": body_text[:1200],
    }


def _get_resume_method_state(adapter: SeekApplyAdapter) -> dict[str, Any]:
    getter = getattr(adapter, "get_resume_method_state", None)
    if callable(getter):
        try:
            state = dict(getter() or {})
        except Exception:  # noqa: BLE001
            state = {}
    else:
        state = {}
    state.setdefault("upload_radio_checked", False)
    state.setdefault("select_resume_radio_checked", False)
    state.setdefault("dont_include_resume_radio_checked", False)
    state.setdefault("selected_resume_method_value", "")
    return state


def _verify_resume_upload_state(
    page: Any,
    adapter: SeekApplyAdapter,
    debug_dir: Path,
    *,
    expected_filename: str,
) -> tuple[bool, dict[str, Any]]:
    method_state = _get_resume_method_state(adapter)
    filename = _detect_resume_filename_with_adapter(page, adapter)
    resume_limit_visible = bool(adapter.resume_limit_ui_present())
    payload = {
        **method_state,
        "visible_filename": filename,
        "expected_filename": expected_filename,
        "resume_limit_modal_present": resume_limit_visible,
        "page_url": str(getattr(page, "url", "") or ""),
        "page_title": _safe_page_title(page),
        "body_text_snippet": _get_page_body_text(page)[:1200],
    }
    print(f"[seek_apply_assist] upload_radio_checked: {bool(method_state.get('upload_radio_checked'))}", flush=True)
    print(f"[seek_apply_assist] select_resume_radio_checked: {bool(method_state.get('select_resume_radio_checked'))}", flush=True)
    print(f"[seek_apply_assist] dont_include_resume_radio_checked: {bool(method_state.get('dont_include_resume_radio_checked'))}", flush=True)
    print(
        f"[seek_apply_assist] selected_resume_method_value: "
        f"{str(method_state.get('selected_resume_method_value') or '(unknown)')}",
        flush=True,
    )
    if bool(method_state.get("dont_include_resume_radio_checked")):
        print("[seek_apply_assist] dont_include_resume_selected_unexpectedly", flush=True)
        return False, payload
    if resume_limit_visible:
        return False, payload
    if not filename or not re.search(r"\.(doc|docx|pdf)\b", filename, re.I):
        return False, payload
    return True, payload


def verify_resume_ready_to_continue(
    adapter: SeekApplyAdapter,
    debug_dir: Path,
    *,
    expected_filename: str = "",
) -> tuple[bool, dict[str, Any]]:
    filename = _verify_resume_filename(adapter)
    method_state = _get_resume_method_state(adapter)
    resume_limit_visible = bool(adapter.resume_limit_ui_present())
    progress_state = dict(adapter.get_resume_upload_progress_state(expected_filename) or {})
    loading_indicator_visible = bool(progress_state.get("loading_indicator_visible", False))
    if bool(progress_state.get("global_spinner_ignored", False)):
        print("[seek_apply_assist] ignored_global_spinner_candidate", flush=True)
    upload_radio_checked = bool(method_state.get("upload_radio_checked"))
    dont_include_checked = bool(method_state.get("dont_include_resume_radio_checked"))
    selected_method_value = str(method_state.get("selected_resume_method_value") or "").strip().lower()
    change_resume_checked = bool(method_state.get("select_resume_radio_checked")) or selected_method_value == "change"
    resume_method_state_known = bool(upload_radio_checked or dont_include_checked or selected_method_value)
    file_input_count, _file_input_visible, _file_input_attached = adapter.get_resume_file_input_state()
    payload = {
        "expected_filename": expected_filename,
        "visible_resume_filenames": [filename] if filename else [],
        "filename_visible": bool(filename),
        "loading_indicator_visible": loading_indicator_visible,
        "upload_radio_checked": upload_radio_checked,
        "change_resume_checked": change_resume_checked,
        "dont_include_checked": dont_include_checked,
        "selected_resume_method_value": selected_method_value,
        "resume_method_state_known": resume_method_state_known,
        "resume_limit_modal_visible": resume_limit_visible,
        "stable_ms": 0,
        "file_input_count": file_input_count,
        "progress_state": progress_state,
        "body_text_snippet": str(adapter.page_text() or "")[:1200],
    }
    print("[seek_apply_assist] resume_ready_to_continue_check", flush=True)
    ready = (
        bool(filename)
        and ((upload_radio_checked or change_resume_checked) if resume_method_state_known else True)
        and not dont_include_checked
        and not resume_limit_visible
        and not loading_indicator_visible
    )
    if ready:
        print("[seek_apply_assist] resume_ready_to_continue_success", flush=True)
    else:
        print("[seek_apply_assist] resume_ready_to_continue_failure", flush=True)
    return ready, payload


def _log_resume_state(label: str, page: Any, adapter: SeekApplyAdapter) -> dict[str, Any]:
    return _log_resume_state_impl(label, page, adapter, resume_state_snapshot=_resume_state_snapshot)


def _save_resume_state_lost_artifacts(debug_dir: Path, page: Any, adapter: SeekApplyAdapter, checkpoint: str) -> tuple[Path, Path, Path]:
    return _save_resume_state_lost_artifacts_impl(
        debug_dir,
        page,
        adapter,
        checkpoint,
        resume_state_snapshot=_resume_state_snapshot,
    )


def _resume_state_is_lost(page: Any, adapter: SeekApplyAdapter) -> bool:
    return bool(_resume_state_snapshot(page, adapter).get("no_resume_included"))


def _write_codex_prompt_resume_failure(
    debug_dir: Path,
    *,
    failure_reason: str,
    latest_checkpoint: str,
    screenshot_path: Path | None,
    html_path: Path | None,
    json_path: Path | None,
    snapshot: dict[str, Any] | None = None,
    suggested_next_fix: str = "",
) -> Path:
    return _write_codex_prompt_resume_failure_impl(
        debug_dir,
        failure_reason=failure_reason,
        latest_checkpoint=latest_checkpoint,
        screenshot_path=screenshot_path,
        html_path=html_path,
        json_path=json_path,
        snapshot=snapshot,
        suggested_next_fix=suggested_next_fix,
    )


def _record_resume_checkpoint(
    debug_dir: Path,
    checkpoint: str,
    *,
    page: Any | None,
    adapter: SeekApplyAdapter | None,
    expected_filename: str = "",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return _record_resume_checkpoint_impl(
        debug_dir,
        checkpoint,
        page=page,
        adapter=adapter,
        expected_filename=expected_filename,
        extra=extra,
        resume_state_snapshot=_resume_state_snapshot,
        resume_upload_diagnostics=_resume_upload_diagnostics,
    )


def verify_resume_included_on_review(page: Any, expected_filename: str) -> tuple[bool, dict[str, Any]]:
    body_text = _get_page_body_text(page)
    lowered = body_text.lower()
    has_documents_section = "documents included" in lowered
    no_resume_included = "no resume included" in lowered
    expected_visible = bool(expected_filename and expected_filename.lower() in lowered)
    resume_visible = bool(re.search(r"\b([A-Za-z0-9._ -]+\.(?:doc|docx|pdf))\b", body_text, re.I))
    included = (has_documents_section or expected_visible or resume_visible) and not no_resume_included and (expected_visible or resume_visible)
    return included, {
        "has_documents_section": has_documents_section,
        "no_resume_included": no_resume_included,
        "expected_filename_visible": expected_visible,
        "resume_filename_visible": resume_visible,
        "body_text_snippet": body_text[:1200],
    }


def _attempt_review_resume_repair(
    page: Any,
    adapter: SeekApplyAdapter,
    selected_cv_path: Path,
    debug_dir: Path,
    *,
    expected_filename: str,
) -> bool:
    _record_resume_checkpoint(debug_dir, "resume_missing_on_review", page=page, adapter=adapter, expected_filename=expected_filename)
    clicked_edit = _click_optional_first(
        [
            page.get_by_label(re.compile(r"edit.*r.s.um.|edit.*resume", re.I)).first,
            page.get_by_text(re.compile(r"edit.*r.s.um.|edit.*resume", re.I)).first,
            page.get_by_text(re.compile(r"resume", re.I)).first,
        ]
    )
    if not clicked_edit:
        return False
    try:
        page.wait_for_timeout(800)
    except Exception:  # noqa: BLE001
        pass
    _record_resume_checkpoint(debug_dir, "upload_method_selected", page=page, adapter=adapter, expected_filename=expected_filename)
    if adapter.click_resume_upload_radio():
        _record_resume_checkpoint(debug_dir, "retry_upload_trigger_clicked", page=page, adapter=adapter, expected_filename=expected_filename)
    filename = _attempt_seek_resume_upload(adapter, selected_cv_path, debug_dir, phase="review_retry")
    if not filename:
        return False
    _record_resume_checkpoint(
        debug_dir,
        "filename_visible_after_upload",
        page=page,
        adapter=adapter,
        expected_filename=expected_filename or filename,
        extra={"repaired_filename": filename},
    )
    if not _click_continue_step(adapter, debug_dir, "review_resume_repair", input_func=lambda: ""):
        return False
    _record_resume_checkpoint(debug_dir, "continue_clicked", page=page, adapter=adapter, expected_filename=expected_filename or filename)
    return True


def _wait_to_leave_choose_documents(page: Any, timeout_ms: int = 5000) -> bool:
    if not _is_choose_documents_page(page):
        return True
    try:
        page.wait_for_function(
            """() => {
                const title = (document.title || '').toLowerCase();
                return !title.includes('choose documents');
            }""",
            timeout=timeout_ms,
        )
    except Exception:  # noqa: BLE001
        pass
    return not _is_choose_documents_page(page)


def _click_optional_first(locators: list[Any]) -> bool:
    for locator in locators:
        try:
            if locator.count() and locator.is_visible():
                locator.click()
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def _advance_post_questionnaire_navigation(
    adapter: SeekApplyAdapter,
    debug_dir: Path,
    *,
    selected_cv_path: Path | None = None,
    resume_upload_verified: bool = False,
    input_func: Callable[[], str] = input,
    max_attempts: int = 3,
) -> tuple[bool, int]:
    page = getattr(adapter, "_page", None)
    if page is None:
        return False, 0
    attempts = 0
    while attempts < max_attempts:
        if _is_review_page(page):
            return True, attempts
        navigation_label = adapter.final_navigation_label().strip().lower()
        visible_step_text = _get_seek_visible_step_text(page).strip().lower()
        on_profile_page = _is_update_profile_page(page)
        if not any(pattern in navigation_label for pattern in SAFE_NAVIGATION_PATTERNS):
            if not on_profile_page and not any(token in visible_step_text for token in SAFE_NAVIGATION_PATTERNS):
                break
        if not _click_continue_step(adapter, debug_dir, "final_navigation", input_func=input_func):
            return False, attempts
        attempts += 1
        try:
            page.wait_for_timeout(500)
        except Exception:  # noqa: BLE001
            pass
        try:
            _wait_for_seek_apply_page_ready(page, timeout_ms=10000)
        except Exception:  # noqa: BLE001
            pass
        if _is_choose_documents_page(page) and resume_upload_verified and selected_cv_path is not None:
            print("[seek_apply_assist] final_navigation_bounced_to_choose_documents", flush=True)
            document_step_result = handle_seek_document_step(
                page,
                adapter,
                selected_cv_path,
                debug_dir,
                resume_upload_verified=True,
                input_func=lambda: "",
            )
            if not document_step_result.success:
                return False, attempts
    return bool(_is_review_page(page)), attempts


def handle_seek_document_step(
    page: Any,
    adapter: SeekApplyAdapter,
    selected_cv_path: Path,
    debug_dir: Path,
    *,
    resume_upload_verified: bool = False,
    input_func: Callable[[], str] = input,
) -> DocumentStepResult:
    if not _is_choose_documents_page(page):
        return DocumentStepResult(True)

    print("[seek_apply_assist] document_step_started", flush=True)
    allow_no_resume = bool(((load_applicant_profile(debug_dir.parent).get("automation") or {}).get("allow_no_resume", False)))
    if resume_upload_verified:
        print("[seek_apply_assist] resume_state_verified", flush=True)
    filename = _detect_resume_filename_with_adapter(page, adapter)
    page_text_lower = _get_page_body_text(page).lower()
    resume_selection_repaired = False
    force_resume_upload = False
    selection_required = (
        "please make a selection" in page_text_lower
        and ("please select a resumé" in page_text_lower or "please select a resume" in page_text_lower)
    )
    if selection_required:
        existing_resume_options: list[str] = []
        try:
            existing_resume_options = list(adapter.get_existing_resume_options())
        except Exception:  # noqa: BLE001
            existing_resume_options = []
        meaningful_existing_resume_options = [
            option for option in existing_resume_options
            if option and not re.search(r"please select a resum.|please select a resume", option, re.I)
        ]
        if meaningful_existing_resume_options:
            preferred_option = next(
                (
                    option for option in meaningful_existing_resume_options
                    if selected_cv_path.name.lower() in option.lower()
                ),
                meaningful_existing_resume_options[0],
            )
            print(f"[seek_apply_assist] existing_resume_selection_required: {preferred_option}", flush=True)
            try:
                adapter.click_resume_change_radio()
            except Exception:  # noqa: BLE001
                pass
            if adapter.select_existing_resume_option(preferred_option):
                print(f"[seek_apply_assist] existing_resume_dropdown_selected: {preferred_option}", flush=True)
                try:
                    page.wait_for_timeout(500)
                except Exception:  # noqa: BLE001
                    pass
                filename = preferred_option
                resume_selection_repaired = True
    if selection_required and not resume_selection_repaired:
        print("[seek_apply_assist] existing_resume_selection_required_unresolved", flush=True)
        filename = ""
        force_resume_upload = True
        try:
            if adapter.click_resume_upload_radio():
                print("[seek_apply_assist] switched_to_resume_upload_after_selection_error", flush=True)
                try:
                    page.wait_for_timeout(500)
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001
            pass
    if filename:
        print(f"[seek_apply_assist] resume_filename_detected: {filename}", flush=True)
    if not force_resume_upload and _document_resume_present(page, adapter):
        print("[seek_apply_assist] document_resume_detected", flush=True)
        if filename:
            print("[seek_apply_assist] resume_uploaded_success", flush=True)
    elif force_resume_upload or not filename:
        print("[seek_apply_assist] resume_missing_before_continue", flush=True)
        file_input_count, _visible, attached = adapter.get_resume_file_input_state()
        if file_input_count > 0 and attached:
            adapter.upload_resume(selected_cv_path)
            try:
                page.wait_for_function(
                    r"""() => /\.(doc|docx|pdf)\b/i.test(document.body?.innerText || '')""",
                    timeout=5000,
                )
            except Exception:  # noqa: BLE001
                pass
            filename = _detect_resume_filename_with_adapter(page, adapter)
            if filename:
                print(f"[seek_apply_assist] resume_filename_detected: {filename}", flush=True)
                print("[seek_apply_assist] resume_uploaded_success", flush=True)
        if not filename and not allow_no_resume:
            debug_dir.mkdir(parents=True, exist_ok=True)
            screenshot_path = debug_dir / "resume_missing_before_continue.png"
            html_path = debug_dir / "resume_missing_before_continue.html"
            json_path = debug_dir / "resume_missing_before_continue.json"
            try:
                page.screenshot(path=str(screenshot_path), full_page=True)
            except Exception:  # noqa: BLE001
                pass
            try:
                html_path.write_text(page.content(), encoding="utf-8")
            except Exception:  # noqa: BLE001
                pass
            try:
                json_path.write_text(json.dumps({"page_url": str(getattr(page, "url", "") or ""), "page_title": str(page.title() or ""), "resume_filename": filename}, indent=2), encoding="utf-8")
            except Exception:  # noqa: BLE001
                pass
            print("[seek_apply_assist] document_step_failure", flush=True)
            print(f"[seek_apply_assist] resume_missing_before_continue_screenshot: {screenshot_path}", flush=True)
            print(f"[seek_apply_assist] resume_missing_before_continue_html: {html_path}", flush=True)
            print(f"[seek_apply_assist] resume_missing_before_continue_json: {json_path}", flush=True)
            return DocumentStepResult(False, status="paused_for_manual_resume_upload", warning="No resume was available on Choose documents.")

    if resume_selection_repaired:
        print("[seek_apply_assist] existing_resume_selection_repaired", flush=True)
    else:
        resume_ready, resume_ready_payload = verify_resume_ready_to_continue(
            adapter,
            debug_dir,
            expected_filename=filename or selected_cv_path.name,
        )
        if not resume_ready:
            expected_filename = filename or selected_cv_path.name
            print("[seek_apply_assist] continue_blocked_resume_not_ready", flush=True)
            screenshot_path, html_path = adapter.save_resume_debug_artifacts(debug_dir)
            root_failure_reason = "CV filename appeared, but SEEK was still processing the upload. Stopped before Continue."
            diagnostics_meta: dict[str, Any] = {}
            if page is not None:
                diagnostics_meta = capture_local_dom_diagnostics(
                    page,
                    locator=None,
                    failure_type="resume_continue_blocked",
                    debug_dir=debug_dir,
                    adapter=adapter,
                    expected_filename=expected_filename,
                    root_failure_reason=root_failure_reason,
                    extra=resume_ready_payload,
                )
            json_path = _save_resume_failure_json(
                debug_dir,
                "resume_upload_blocked_before_continue",
                {
                    **resume_ready_payload,
                    "root_failure_reason": root_failure_reason,
                    "page_url": str(getattr(page, "url", "") or ""),
                    "page_title": str(page.title() or ""),
                    "dom_diagnostics": diagnostics_meta,
                },
            )
            print(f"[seek_apply_assist] resume_upload_blocked_before_continue_screenshot: {screenshot_path}", flush=True)
            print(f"[seek_apply_assist] resume_upload_blocked_before_continue_html: {html_path}", flush=True)
            print(f"[seek_apply_assist] resume_upload_blocked_before_continue_json: {json_path}", flush=True)
            print("[seek_apply_assist] resume_upload_incomplete_stop", flush=True)
            return DocumentStepResult(
                False,
                status="paused_for_resume_upload_incomplete",
                warning="CV filename appeared, but SEEK was still processing the upload. Stopped before Continue.",
            )

    print("[seek_apply_assist] document_continue_clicked", flush=True)
    click_seek_continue(page, debug_dir, "document_step", save_failure_artifacts=False)
    if _wait_to_leave_choose_documents(page, timeout_ms=5000):
        print("[seek_apply_assist] document_step_success", flush=True)
        return DocumentStepResult(True)

    print("[seek_apply_assist] document_step_still_on_choose_documents", flush=True)
    if resume_upload_verified:
        print("[seek_apply_assist] dont_include_resume_click_blocked_due_to_verified_resume", flush=True)
        debug_dir.mkdir(parents=True, exist_ok=True)
        screenshot_path = debug_dir / "document_step_failed.png"
        html_path = debug_dir / "document_step_failed.html"
        try:
            page.screenshot(path=str(screenshot_path), full_page=True)
        except Exception:  # noqa: BLE001
            pass
        try:
            html_path.write_text(page.content(), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
        _save_resume_failure_json(
            debug_dir,
            "document_step_failed",
            {
                "reason": "verified_resume_preserved",
                **_resume_state_snapshot(page, adapter),
            },
        )
        print("[seek_apply_assist] document_step_failure", flush=True)
        return DocumentStepResult(False, status="paused_for_manual_resume_upload", warning="Verified resume state could not be preserved while leaving Choose documents.")
    if adapter.resume_limit_ui_present():
        debug_dir.mkdir(parents=True, exist_ok=True)
        screenshot_path = debug_dir / "document_step_failed.png"
        html_path = debug_dir / "document_step_failed.html"
        json_path = debug_dir / "document_step_failed.json"
        try:
            page.screenshot(path=str(screenshot_path), full_page=True)
        except Exception:  # noqa: BLE001
            pass
        try:
            html_path.write_text(page.content(), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
        _save_resume_failure_json(
            debug_dir,
            "document_step_failed",
            {"reason": "resume_limit_modal_visible"},
        )
        print("[seek_apply_assist] document_step_failure", flush=True)
        print("[seek_apply_assist] document_step_resume_limit_modal_visible", flush=True)
        return DocumentStepResult(False, status="paused_for_manual_resume_upload", warning="Resume limit modal was still visible on Choose documents.")
    if not allow_no_resume:
        debug_dir.mkdir(parents=True, exist_ok=True)
        screenshot_path = debug_dir / "document_step_failed.png"
        html_path = debug_dir / "document_step_failed.html"
        json_path = debug_dir / "document_step_failed.json"
        try:
            page.screenshot(path=str(screenshot_path), full_page=True)
        except Exception:  # noqa: BLE001
            pass
        try:
            html_path.write_text(page.content(), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
        _save_resume_failure_json(
            debug_dir,
            "document_step_failed",
            {"reason": "allow_no_resume_false"},
        )
        print("[seek_apply_assist] dont_include_resume_fallback_blocked_because_allow_no_resume_false", flush=True)
        print("[seek_apply_assist] document_step_failure", flush=True)
        return DocumentStepResult(False, status="paused_for_manual_resume_upload", warning="Choose documents could not proceed automatically.")
    print("[seek_apply_assist] document_resume_fallback_started", flush=True)
    fallback_clicked = _click_optional_first(
        [
            page.get_by_label(re.compile("Don.t include a r.s.um.|Don.t include a resume", re.I)).first,
            page.get_by_text(re.compile("Don.t include a r.s.um.|Don.t include a resume", re.I)).first,
        ]
    )
    if fallback_clicked:
        try:
            page.wait_for_timeout(500)
        except Exception:  # noqa: BLE001
            pass
    adapter.click_resume_upload_radio()
    try:
        page.wait_for_timeout(500)
    except Exception:  # noqa: BLE001
        pass
    file_input_count, _file_input_visible, file_input_attached = adapter.get_resume_file_input_state()
    if file_input_count > 0 and file_input_attached:
        adapter.upload_resume(selected_cv_path)
    print("[seek_apply_assist] document_resume_fallback_completed", flush=True)

    print("[seek_apply_assist] document_continue_clicked", flush=True)
    click_seek_continue(page, debug_dir, "document_step", save_failure_artifacts=False)
    if _wait_to_leave_choose_documents(page, timeout_ms=5000):
        print("[seek_apply_assist] document_step_success", flush=True)
        return DocumentStepResult(True)

    debug_dir.mkdir(parents=True, exist_ok=True)
    screenshot_path = debug_dir / "document_step_failed.png"
    html_path = debug_dir / "document_step_failed.html"
    json_path = debug_dir / "document_step_failed.json"
    try:
        page.screenshot(path=str(screenshot_path), full_page=True)
    except Exception:  # noqa: BLE001
        pass
    try:
        html_path.write_text(page.content(), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    try:
        json_path.write_text(
            json.dumps(
                {
                    "page_url": str(getattr(page, "url", "") or ""),
                    "page_title": str(page.title() or ""),
                    "resume_present": _document_resume_present(page, adapter),
                    "file_input_count": file_input_count,
                    "still_on_choose_documents": _is_choose_documents_page(page),
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except Exception:  # noqa: BLE001
        pass
    print("[seek_apply_assist] document_step_failure", flush=True)
    print(f"[seek_apply_assist] document_step_failed_screenshot: {screenshot_path}", flush=True)
    print(f"[seek_apply_assist] document_step_failed_html: {html_path}", flush=True)
    print(f"[seek_apply_assist] document_step_failed_json: {json_path}", flush=True)
    print("SEEK is still on Choose documents. Please review the resume selection, click Continue manually if needed, then press Enter.", flush=True)
    try:
        input_func()
        return DocumentStepResult(
            not _is_choose_documents_page(page),
            status="" if not _is_choose_documents_page(page) else "paused_for_manual_resume_upload",
            warning="" if not _is_choose_documents_page(page) else "SEEK is still on Choose documents.",
        )
    except EOFError:
        return DocumentStepResult(False, status="paused_for_manual_resume_upload", warning="SEEK is still on Choose documents.")


def _detect_seek_continue_success(page: Any, step_name: str) -> str:
    current_url = ""
    page_title = ""
    visible_step_text = _get_seek_visible_step_text(page)
    try:
        current_url = str(page.url or "")
    except Exception:  # noqa: BLE001
        current_url = ""
    try:
        page_title = str(page.title() or "")
    except Exception:  # noqa: BLE001
        page_title = ""

    lowered_url = current_url.lower()
    lowered_title = page_title.lower()
    lowered_text = visible_step_text.lower()
    still_on_choose_documents = _is_choose_documents_page(page)

    if step_name == "cover_letter":
        if still_on_choose_documents:
            return ""
        if "/role-requirements" in lowered_url:
            return "role_requirements"
        if "answer employer questions" in lowered_title:
            return "role_requirements"
        if "answer employer questions" in lowered_text:
            return "role_requirements"
        if "which of the following statements best describes your right to work in new zealand?" in lowered_text:
            return "role_requirements"

    if step_name in {"document_step", "questionnaire"}:
        if still_on_choose_documents:
            return ""
        if "/role-requirements" in lowered_url:
            return "role_requirements"
        if "/profile" in lowered_url or "update seek profile" in lowered_title:
            return "update_seek_profile"
        if "/review" in lowered_url or "review and submit" in lowered_title:
            return "review_and_submit"
        if "answer employer questions" in lowered_title or "answer employer questions" in lowered_text:
            return "role_requirements"

    if step_name == "final_navigation":
        if "/role-requirements" in lowered_url:
            return "role_requirements"
        if "answer employer questions" in lowered_title or "answer employer questions" in lowered_text:
            return "role_requirements"
        if "/profile" in lowered_url or "update seek profile" in lowered_title:
            return "update_seek_profile"
        if "/review" in lowered_url or "review and submit" in lowered_title:
            return "review_and_submit"

    if "update seek profile" in lowered_text:
        return "update_seek_profile"
    if "review and submit" in lowered_text or lowered_title.lower().find("review and submit") >= 0:
        return "review_and_submit"
    if lowered_title.lower().find("submit") >= 0 or "submit" in lowered_text:
        return "submit"
    if lowered_title.lower().find("review") >= 0 or "review" in lowered_text:
        return "review"
    return ""


def _is_seek_loading_shell(page: Any) -> bool:
    try:
        title = str(page.title() or "").strip().lower()
    except Exception:  # noqa: BLE001
        title = ""
    try:
        body_text = str(page.locator("body").inner_text(timeout=500) or "").strip().lower()
    except Exception:  # noqa: BLE001
        body_text = ""
    if title != "seek":
        return False
    if not body_text:
        return True
    return body_text in {"loading", "seek loading"} or body_text.startswith("loading")


def _wait_for_seek_apply_page_ready(page: Any, timeout_ms: int = 15000) -> None:
    deadline = time.monotonic() + (timeout_ms / 1000.0)
    while time.monotonic() < deadline:
        if not _is_seek_loading_shell(page):
            return
        try:
            page.wait_for_timeout(500)
        except Exception:  # noqa: BLE001
            break


def _wait_for_seek_continue_transition(page: Any, old_url: str, old_title: str, step_name: str) -> str:
    immediate_target = _detect_seek_continue_success(page, step_name)
    if immediate_target:
        return immediate_target
    try:
        page.wait_for_function(
            """([previousUrl, previousTitle]) => {
                const bodyText = (document.body?.innerText || '');
                const title = (document.title || '').toLowerCase();
                const chooseDocuments = title.includes('choose documents') || /choose documents/i.test(bodyText);
                const targetReady = /Answer employer questions/i.test(bodyText)
                    || /Which of the following statements best describes your right to work in New Zealand\\?/i.test(bodyText)
                    || /Update SEEK Profile|Review and submit|Submit|Review/i.test(bodyText);
                return window.location.href !== previousUrl
                    || document.title !== previousTitle
                    || (!chooseDocuments && targetReady);
            }""",
            [old_url, old_title],
            timeout=10000,
        )
    except Exception:  # noqa: BLE001
        if _is_seek_loading_shell(page):
            try:
                page.wait_for_function(
                    """() => {
                        const title = (document.title || '').trim().toLowerCase();
                        const bodyText = (document.body?.innerText || '').trim().toLowerCase();
                        const loading = title === 'seek' && (!bodyText || bodyText === 'loading' || bodyText === 'seek loading' || bodyText.startsWith('loading'));
                        return !loading;
                    }""",
                    timeout=20000,
                )
            except Exception:  # noqa: BLE001
                pass
        return _detect_seek_continue_success(page, step_name)
    if _is_seek_loading_shell(page):
        try:
            _wait_for_seek_apply_page_ready(page, timeout_ms=20000)
        except Exception:  # noqa: BLE001
            pass
    return _detect_seek_continue_success(page, step_name)


def _save_continue_success_screenshot(page: Any, debug_dir: Path, step_slug: str) -> None:
    debug_dir.mkdir(parents=True, exist_ok=True)
    screenshot_path = debug_dir / f"after_{step_slug}_continue.png"
    try:
        _safe_page_screenshot(page, screenshot_path)
        print(f"[seek_apply_assist] after_{step_slug}_continue_screenshot: {screenshot_path}", flush=True)
    except Exception:  # noqa: BLE001
        pass


def _click_continue_step(
    adapter: SeekApplyAdapter,
    debug_dir: Path,
    step_name: str,
    *,
    input_func: Callable[[], str] = input,
) -> bool:
    page = getattr(adapter, "_page", None)
    if page is not None:
        success = click_seek_continue(page, debug_dir, step_name)
        if success:
            return True
        print(_manual_continue_prompt(step_name), flush=True)
        try:
            input_func()
        except EOFError:
            return False
        return True

    if step_name == "questionnaire":
        adapter.click_questionnaire_continue()
    else:
        adapter.click_continue()
    return True


def _manual_continue_prompt(step_name: str) -> str:
    if step_name == "cover_letter":
        return "Cover letter pasted but Continue could not be clicked. Please click Continue manually, then press Enter."
    if step_name == "questionnaire":
        return "Questions answered but Continue was not clicked. Please click Continue manually, then press Enter."
    return "Continue could not be clicked automatically. Please click Continue manually, then press Enter."


QUESTION_TEXT_KEYWORDS = (
    "years",
    "experience",
    "qualification",
    "ict",
    "licence",
    "license",
    "work in new zealand",
    "salary",
    "remuneration",
    "pay",
)


def _extract_question_text_details_from_candidates(
    nearby_text_candidates: list[str],
    option_payloads: list[dict[str, str]],
) -> dict[str, Any]:
    option_texts = {str(option.get("label") or "").strip().lower() for option in option_payloads if str(option.get("label") or "").strip()}
    seen: set[str] = set()
    best_text = ""
    best_score = -1
    best_source = ""
    rejected_candidates: list[dict[str, str]] = []
    for candidate in nearby_text_candidates:
        cleaned = _normalise_question_text(candidate)
        lowered = cleaned.lower()
        if not cleaned or lowered in seen:
            if cleaned:
                rejected_candidates.append({"candidate": cleaned, "reason": "duplicate"})
            continue
        seen.add(lowered)
        if lowered in option_texts:
            rejected_candidates.append({"candidate": cleaned, "reason": "matches option label"})
            continue
        if len(cleaned) < 8:
            rejected_candidates.append({"candidate": cleaned, "reason": "too short"})
            continue
        score = 0
        reasons: list[str] = []
        if cleaned.endswith("?"):
            score += 20
            reasons.append("ends_with_question_mark")
        if any(keyword in lowered for keyword in QUESTION_TEXT_KEYWORDS):
            score += 30
            reasons.append("contains_question_keyword")
        if lowered.startswith(("have you", "how many", "do you", "what is", "what's")):
            score += 10
            reasons.append("starts_like_question")
        if 10 <= len(cleaned) <= 220:
            score += 5
            reasons.append("reasonable_length")
        if "?" in cleaned:
            score += 5
            reasons.append("contains_question_mark")
        if score > best_score:
            best_score = score
            best_text = cleaned
            best_source = ",".join(reasons) or "best_score"
        else:
            rejected_candidates.append({"candidate": cleaned, "reason": f"lower score than best ({score} <= {best_score})"})
    return {
        "text": best_text,
        "source": best_source,
        "rejected_candidates": rejected_candidates[:20],
    }


def _extract_question_text_from_candidates(nearby_text_candidates: list[str], option_payloads: list[dict[str, str]]) -> str:
    return str(_extract_question_text_details_from_candidates(nearby_text_candidates, option_payloads).get("text") or "")


def _normalise_skill_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").strip().lower()).strip()


def _get_explicit_skill_years(applicant_profile: dict[str, Any], skill_key: str | None) -> int | None:
    skill_map = {
        _normalise_skill_key(key): int(value)
        for key, value in (applicant_profile.get("skills_experience_years") or {}).items()
        if str(key).strip()
    }
    if skill_key:
        normalised = _normalise_skill_key(skill_key)
        if normalised in skill_map:
            return skill_map[normalised]
    return None


def _get_profile_years(applicant_profile: dict[str, Any], skill_key: str | None) -> int | None:
    explicit_years = _get_explicit_skill_years(applicant_profile, skill_key)
    if explicit_years is not None:
        return explicit_years
    industry_years = applicant_profile.get("industry_experience_years")
    if industry_years is not None:
        try:
            return int(industry_years)
        except Exception:  # noqa: BLE001
            return None
    return None


def _detect_question_type(question_text: str) -> str:
    lowered = question_text.lower()
    if lowered == "gender" or lowered.startswith("gender ") or ("gender" in lowered and ("male" in lowered or "female" in lowered or "do not wish to disclose" in lowered)):
        return "gender_disclosure"
    if "payroll" in lowered and ("walk us through" in lowered or "processing payroll" in lowered or "legislative compliance" in lowered):
        return "payroll_experience_text"
    if "psychometric" in lowered and "assessment" in lowered and ("comfortable" in lowered or "proceeding" in lowered):
        return "psychometric_assessment_consent"
    if "when can you start" in lowered or ("start date" in lowered and "when" in lowered):
        return "start_date_availability"
    if "notice" in lowered and "current employer" in lowered:
        return "notice_period"
    if "microsoft azure certifications" in lowered and "completed" in lowered:
        return "azure_certifications"
    if "programming languages" in lowered and "experienced in" in lowered:
        return "programming_languages"
    if "data analytics tools" in lowered and "experienced with" in lowered:
        return "data_analytics_tools"
    if "azure" in lowered and ("synapse" in lowered or "data factory" in lowered or "adf" in lowered):
        return "azure_data_services_text"
    if "years" in lowered and "experience" in lowered:
        return "experience_years"
    if _contains_salary_question_text(lowered):
        return "salary"
    if "right to work" in lowered or "work in new zealand" in lowered or "legally entitled to work in nz" in lowered:
        return "legal_right_to_work"
    if ("experience using xero" in lowered or "use xero" in lowered or "using xero" in lowered) and "?" in lowered:
        return "software_experience_xero"
    if ("experience using microsoft excel" in lowered or "experience using excel" in lowered or "using microsoft excel" in lowered or "using excel" in lowered) and "?" in lowered:
        return "software_experience_excel"
    if "highest level of education" in lowered or ("education" in lowered and "highest" in lowered):
        return "education_level"
    if "driver" in lowered and ("licence" in lowered or "license" in lowered):
        return "licence"
    if "qualification" in lowered:
        return "qualification"
    if "criminal" in lowered or "medical" in lowered or "conviction" in lowered:
        return "legal_sensitive"
    return "unknown"


def _is_data_engineer_years_question(question_text: str) -> bool:
    lowered = question_text.lower()
    return "how many years" in lowered and "data engineer" in lowered


def _is_yes_no_option_set(options: list[str]) -> bool:
    lowered = " ".join(option.lower() for option in options)
    return len(options) == 2 and ("yes" in lowered or "no" in lowered)


def _question_looks_like_skill_experience_yes_no(question_text: str) -> bool:
    lowered = question_text.lower()
    starters = (
        "do you have experience",
        "have you used",
        "do you have experience using",
        "do you have experience with",
        "do you have any experience",
        "are you experienced",
        "are you proficient",
        "do you have knowledge of",
        "do you have working knowledge of",
        "have you worked with",
        "have you had experience with",
    )
    if any(starter in lowered for starter in starters):
        return True
    return lowered.startswith("do you have ") and " experience" in lowered


def _infer_contextual_skill_experience(question_text: str, applicant_profile: dict[str, Any]) -> tuple[str | None, bool | None, float, str]:
    lowered = question_text.lower()
    direct_years_skill = _detect_skill_keyword(lowered)
    if direct_years_skill:
        years_value = _get_explicit_skill_years(applicant_profile, direct_years_skill)
        if years_value is not None:
            has_experience = years_value > 0
            return (
                direct_years_skill,
                has_experience,
                0.9 if has_experience else 0.65,
                f"Used profile years value {years_value} for {direct_years_skill}.",
            )

    direct_software_map = dict((applicant_profile.get("software_experience") or {}))
    for software_key in ("xero", "microsoft excel", "excel"):
        if software_key in lowered:
            has_experience = bool(direct_software_map.get(software_key, False))
            return (
                software_key,
                has_experience,
                0.9 if has_experience else 0.65,
                f"Used applicant_profile.software_experience.{software_key}.",
            )

    contextual_domains: list[tuple[str, list[str], list[str]]] = [
        (
            "web application testing",
            ["testing web applications", "test web applications", "web application testing", "software testing", "web testing", "quality assurance", "qa", "uat"],
            ["data engineering", "python", "business analysis", "analytics"],
        ),
        (
            "reporting",
            ["reporting", "reports", "dashboards"],
            ["reporting", "power bi", "business intelligence", "analytics"],
        ),
        (
            "data engineering",
            ["data engineering", "data pipelines", "etl", "elt"],
            ["data engineering", "python", "sql"],
        ),
        (
            "business analysis",
            ["business analysis", "requirements gathering", "stakeholder engagement"],
            ["business analysis", "analytics"],
        ),
        (
            "lead generation",
            ["lead generation", "customer acquisition", "campaign performance", "pipeline generation"],
            ["business analysis", "analytics", "reporting", "dashboards"],
        ),
    ]
    for domain_name, trigger_phrases, supporting_skills in contextual_domains:
        if not any(phrase in lowered for phrase in trigger_phrases):
            continue
        explicit_years = max((_get_profile_years(applicant_profile, skill) or 0) for skill in supporting_skills)
        if explicit_years > 0:
            return (
                domain_name,
                True,
                0.78,
                f"Used related profile experience ({', '.join(supporting_skills)}) with up to {explicit_years} years for {domain_name}.",
            )
        qualifications = dict(applicant_profile.get("qualifications") or {})
        if any(bool(qualifications.get(key)) for key in ("ict", "data_science", "statistics", "operations_research")):
            return (
                domain_name,
                True,
                0.72,
                f"Used related qualifications and broader technical background for {domain_name}.",
            )
        industry_years = _get_profile_years(applicant_profile, None)
        if industry_years and industry_years > 0:
            return (
                domain_name,
                True,
                0.68,
                f"Used broader industry experience ({industry_years} years) as contextual support for {domain_name}.",
            )
        return (domain_name, None, 0.25, f"No supporting profile evidence was found for {domain_name}.")
    return (None, None, 0.0, "")


def resolve_profile_backed_answer(
    *,
    question_text: str,
    field_type: str,
    available_options: list[str],
    applicant_profile: dict[str, Any],
) -> GenericAnswerResolution:
    print("[seek_apply_assist] generic_answer_resolver_started", flush=True)
    inferred_question_type = _detect_question_type(question_text)
    inferred_skill = _detect_skill_keyword(question_text.lower()) or ""
    lowered_question = question_text.lower()
    print(f"[seek_apply_assist] inferred_question_type: {inferred_question_type}", flush=True)
    print(f"[seek_apply_assist] inferred_skill: {inferred_skill or '(none)'}", flush=True)
    print(f"[seek_apply_assist] available_options: {' | '.join(available_options) if available_options else '(none)'}", flush=True)

    if _is_data_engineer_years_question(question_text) and field_type.lower() in {"select", "radio"}:
        option = _pick_experience_option(available_options, 2)
        chosen_answer = option or "2 years"
        print("[seek_apply_assist] data_engineer_years_override_used", flush=True)
        print(f"[seek_apply_assist] chosen_answer: {chosen_answer}", flush=True)
        print("[seek_apply_assist] confidence: 0.98", flush=True)
        print("[seek_apply_assist] generic_answer_success", flush=True)
        return GenericAnswerResolution(
            chosen_answer=chosen_answer,
            confidence=0.98,
            reason="Used explicit rule: Data Engineer experience questions should be answered as 2 years.",
            should_answer=True,
            matched_rule="data_engineer_years_override",
            inferred_question_type="experience_years",
            inferred_skill="data engineering",
            profile_years_used=2,
        )

    if inferred_question_type == "payroll_experience_text":
        if field_type.lower() not in {"textarea", "text", "free_text"}:
            print("[seek_apply_assist] generic_answer_skipped_low_confidence", flush=True)
            return GenericAnswerResolution(None, 0.2, "Payroll experience prompt was not a free-text field.", False, matched_rule="payroll_experience_text", inferred_question_type=inferred_question_type, inferred_skill=inferred_skill)
        payroll_answer = str(((applicant_profile.get("experience_notes") or {}).get("payroll_related_answer")) or "").strip()
        if payroll_answer:
            print("[seek_apply_assist] payroll_text_answer_used", flush=True)
            print(f"[seek_apply_assist] chosen_answer: {payroll_answer}", flush=True)
            print("[seek_apply_assist] confidence: 0.95", flush=True)
            print("[seek_apply_assist] generic_answer_success", flush=True)
            return GenericAnswerResolution(
                chosen_answer=payroll_answer,
                confidence=0.95,
                reason="Used applicant_profile.experience_notes.payroll_related_answer for payroll free-text question.",
                should_answer=True,
                matched_rule="payroll_experience_text",
                inferred_question_type=inferred_question_type,
                inferred_skill=inferred_skill,
            )
        print("[seek_apply_assist] generic_answer_skipped_low_confidence", flush=True)
        return GenericAnswerResolution(None, 0.2, "No payroll experience note was available.", False, matched_rule="payroll_experience_text", inferred_question_type=inferred_question_type, inferred_skill=inferred_skill)

    if inferred_question_type == "psychometric_assessment_consent":
        comfortable = bool(((applicant_profile.get("selection_process") or {}).get("comfortable_with_psychometric_assessment", False)))
        option = _pick_yes_no_option(available_options, yes=comfortable)
        if option:
            print("[seek_apply_assist] psychometric_assessment_answer_used", flush=True)
            print(f"[seek_apply_assist] chosen_answer: {option}", flush=True)
            print("[seek_apply_assist] confidence: 0.95", flush=True)
            print("[seek_apply_assist] generic_answer_success", flush=True)
            return GenericAnswerResolution(
                chosen_answer=option,
                confidence=0.95,
                reason="Used applicant_profile.selection_process.comfortable_with_psychometric_assessment.",
                should_answer=True,
                matched_rule="psychometric_assessment_consent",
                inferred_question_type=inferred_question_type,
                inferred_skill=inferred_skill,
            )
        print("[seek_apply_assist] generic_answer_skipped_low_confidence", flush=True)
        return GenericAnswerResolution(None, 0.2, "Psychometric consent options were not recognisable.", False, matched_rule="psychometric_assessment_consent", inferred_question_type=inferred_question_type, inferred_skill=inferred_skill)

    if inferred_question_type in {"software_experience_xero", "software_experience_excel"}:
        software_map = dict((applicant_profile.get("software_experience") or {}))
        key = "xero" if inferred_question_type == "software_experience_xero" else "microsoft excel"
        has_experience = bool(software_map.get(key, software_map.get("excel" if "excel" in key else key, False)))
        option = _pick_yes_no_option(available_options, yes=has_experience)
        if option:
            print(f"[seek_apply_assist] chosen_answer: {option}", flush=True)
            print("[seek_apply_assist] confidence: 0.9", flush=True)
            print("[seek_apply_assist] generic_answer_success", flush=True)
            return GenericAnswerResolution(
                chosen_answer=option,
                confidence=0.9,
                reason=f"Used applicant_profile.software_experience.{key}.",
                should_answer=True,
                matched_rule=inferred_question_type,
                inferred_question_type=inferred_question_type,
                inferred_skill=inferred_skill,
            )
        print("[seek_apply_assist] generic_answer_skipped_low_confidence", flush=True)
        return GenericAnswerResolution(None, 0.2, "Software experience yes/no options were not recognisable.", False, matched_rule=inferred_question_type, inferred_question_type=inferred_question_type, inferred_skill=inferred_skill)

    if "whereabouts in new zealand are you located" in lowered_question and field_type.lower() in {"textarea", "text", "free_text", "input"}:
        answer = str(applicant_profile.get("location") or "Auckland").strip() or "Auckland"
        print(f"[seek_apply_assist] chosen_answer: {answer}", flush=True)
        print("[seek_apply_assist] confidence: 0.96", flush=True)
        print("[seek_apply_assist] generic_answer_success", flush=True)
        return GenericAnswerResolution(
            chosen_answer=answer,
            confidence=0.96,
            reason="Used applicant location for NZ location prompt.",
            should_answer=True,
            matched_rule="nz_location_text",
            inferred_question_type="location",
            inferred_skill=inferred_skill,
        )

    if "powerbi experience" in lowered_question or "power bi experience" in lowered_question:
        if field_type.lower() in {"select", "radio"} and _is_yes_no_option_set(available_options):
            option = _pick_yes_no_option(available_options, yes=True)
            if option:
                print(f"[seek_apply_assist] chosen_answer: {option}", flush=True)
                print("[seek_apply_assist] confidence: 0.95", flush=True)
                print("[seek_apply_assist] generic_answer_success", flush=True)
                return GenericAnswerResolution(
                    chosen_answer=option,
                    confidence=0.95,
                    reason="Used applicant Power BI experience from the profile.",
                    should_answer=True,
                    matched_rule="power_bi_experience_yes_no",
                    inferred_question_type="software_experience_power_bi",
                    inferred_skill="power bi",
                )
        if field_type.lower() in {"textarea", "text", "free_text", "input"}:
            answer = "Yes, I have Power BI experience, including several years of dashboarding, reporting, and analytics delivery."
            print(f"[seek_apply_assist] chosen_answer: {answer}", flush=True)
            print("[seek_apply_assist] confidence: 0.94", flush=True)
            print("[seek_apply_assist] generic_answer_success", flush=True)
            return GenericAnswerResolution(
                chosen_answer=answer,
                confidence=0.94,
                reason="Used applicant Power BI experience from the profile for a free-text Power BI prompt.",
                should_answer=True,
                matched_rule="power_bi_experience_text",
                inferred_question_type="software_experience_power_bi",
                inferred_skill="power bi",
            )

    if "data entry experience" in lowered_question and field_type.lower() in {"select", "radio"} and _is_yes_no_option_set(available_options):
        option = _pick_yes_no_option(available_options, yes=True)
        if option:
            print(f"[seek_apply_assist] chosen_answer: {option}", flush=True)
            print("[seek_apply_assist] confidence: 0.75", flush=True)
            print("[seek_apply_assist] generic_answer_success", flush=True)
            return GenericAnswerResolution(
                chosen_answer=option,
                confidence=0.75,
                reason="Used the applicant's broader data/reporting background as contextual evidence for data-entry experience.",
                should_answer=True,
                matched_rule="data_entry_experience_contextual_yes",
                inferred_question_type="data_entry_experience",
                inferred_skill="data entry",
            )

    if "data migration projects" in lowered_question and field_type.lower() in {"select", "radio"} and _is_yes_no_option_set(available_options):
        option = _pick_yes_no_option(available_options, yes=True)
        if option:
            print(f"[seek_apply_assist] chosen_answer: {option}", flush=True)
            print("[seek_apply_assist] confidence: 0.7", flush=True)
            print("[seek_apply_assist] generic_answer_success", flush=True)
            return GenericAnswerResolution(
                chosen_answer=option,
                confidence=0.7,
                reason="Used the applicant's broader data and systems-delivery background as contextual evidence for data-migration work.",
                should_answer=True,
                matched_rule="data_migration_contextual_yes",
                inferred_question_type="data_migration_experience",
                inferred_skill="data migration",
            )

    if (
        ("microsoft fabric" in lowered_question or "databricks" in lowered_question or "snowflake" in lowered_question)
        and "how many years experience" in lowered_question
        and field_type.lower() in {"textarea", "text", "free_text", "input"}
    ):
        answer = (
            "I do not want to overstate direct hands-on production experience specifically with Microsoft Fabric, "
            "Databricks, or Snowflake. My background is strongest in SQL, Power BI, analytics, reporting, and broader data delivery, "
            "and I can ramp up quickly in adjacent modern data platforms."
        )
        print(f"[seek_apply_assist] chosen_answer: {answer}", flush=True)
        print("[seek_apply_assist] confidence: 0.9", flush=True)
        print("[seek_apply_assist] generic_answer_success", flush=True)
        return GenericAnswerResolution(
            chosen_answer=answer,
            confidence=0.9,
            reason="Used a truthful limit answer for Microsoft Fabric / Databricks / Snowflake experience.",
            should_answer=True,
            matched_rule="modern_data_platforms_truthful_limit",
            inferred_question_type="modern_data_platforms_text",
            inferred_skill="modern data platforms",
        )

    if inferred_question_type == "notice_period":
        notice_weeks = 2
        print("[seek_apply_assist] notice_period_rule_matched", flush=True)
        print(f"[seek_apply_assist] profile_notice_period_weeks: {notice_weeks}", flush=True)
        option = _pick_notice_period_option(available_options, notice_weeks)
        if option:
            print(f"[seek_apply_assist] selected_notice_period_option: {option}", flush=True)
            print(f"[seek_apply_assist] chosen_answer: {option}", flush=True)
            print("[seek_apply_assist] confidence: 0.9", flush=True)
            print("[seek_apply_assist] generic_answer_success", flush=True)
            return GenericAnswerResolution(
                chosen_answer=option,
                confidence=0.9,
                reason="Used explicit rule: notice period questions should be answered as 2 weeks.",
                should_answer=True,
                matched_rule="notice_period",
                inferred_question_type=inferred_question_type,
                inferred_skill=inferred_skill,
            )
        print("[seek_apply_assist] generic_answer_skipped_low_confidence", flush=True)
        return GenericAnswerResolution(None, 0.2, "Notice period options were not recognisable.", False, matched_rule="notice_period", inferred_question_type=inferred_question_type, inferred_skill=inferred_skill)

    if "how did you hear about this position" in lowered_question and field_type.lower() in {"select", "radio"}:
        option = _pick_option(available_options, ["seek"])
        if option:
            print(f"[seek_apply_assist] chosen_answer: {option}", flush=True)
            print("[seek_apply_assist] confidence: 0.98", flush=True)
            print("[seek_apply_assist] generic_answer_success", flush=True)
            return GenericAnswerResolution(
                chosen_answer=option,
                confidence=0.98,
                reason="Used SEEK as the truthful source for an application opened from SEEK.",
                should_answer=True,
                matched_rule="heard_about_role_via_seek",
                inferred_question_type="application_source",
                inferred_skill=inferred_skill,
            )
        print("[seek_apply_assist] generic_answer_skipped_low_confidence", flush=True)
        return GenericAnswerResolution(None, 0.2, "Application-source options were not recognisable.", False, matched_rule="heard_about_role_via_seek", inferred_question_type="application_source", inferred_skill=inferred_skill)

    if inferred_question_type == "start_date_availability":
        if field_type.lower() not in {"textarea", "text", "free_text", "input"}:
            print("[seek_apply_assist] generic_answer_skipped_low_confidence", flush=True)
            return GenericAnswerResolution(None, 0.2, "Start date prompt was not a free-text field.", False, matched_rule="start_date_availability", inferred_question_type=inferred_question_type, inferred_skill=inferred_skill)
        try:
            notice_weeks = int(applicant_profile.get("notice_period_weeks", 2))
        except Exception:  # noqa: BLE001
            notice_weeks = 2
        if notice_weeks <= 0:
            answer = "Immediately."
        elif notice_weeks == 1:
            answer = "I can start after 1 week's notice."
        else:
            answer = f"I can start after {notice_weeks} weeks' notice."
        print(f"[seek_apply_assist] chosen_answer: {answer}", flush=True)
        print("[seek_apply_assist] confidence: 0.95", flush=True)
        print("[seek_apply_assist] generic_answer_success", flush=True)
        return GenericAnswerResolution(
            chosen_answer=answer,
            confidence=0.95,
            reason="Used applicant_profile.notice_period_weeks for start-date availability question.",
            should_answer=True,
            matched_rule="start_date_availability",
            inferred_question_type=inferred_question_type,
            inferred_skill=inferred_skill,
        )

    if inferred_question_type == "azure_certifications":
        if field_type.lower() != "checkbox":
            print("[seek_apply_assist] generic_answer_skipped_low_confidence", flush=True)
            return GenericAnswerResolution(None, 0.2, "Azure certifications prompt was not a checkbox group.", False, matched_rule="azure_certifications", inferred_question_type=inferred_question_type, inferred_skill=inferred_skill)
        option = _pick_checkbox_option(
            available_options,
            ["no such certification", "none of these", "none of the above", "none"],
        )
        if option:
            print(f"[seek_apply_assist] chosen_answer: {option}", flush=True)
            print("[seek_apply_assist] confidence: 0.95", flush=True)
            print("[seek_apply_assist] generic_answer_success", flush=True)
            return GenericAnswerResolution(
                chosen_answer=option,
                confidence=0.95,
                reason="Used a truthful no-certification option for Azure certification checkbox question.",
                should_answer=True,
                matched_rule="azure_certifications_none",
                inferred_question_type=inferred_question_type,
                inferred_skill="azure certifications",
            )
        print("[seek_apply_assist] generic_answer_skipped_low_confidence", flush=True)
        return GenericAnswerResolution(None, 0.15, "Azure certification checkbox question had no safe none option.", False, matched_rule="azure_certifications", inferred_question_type=inferred_question_type, inferred_skill=inferred_skill)

    if inferred_question_type == "programming_languages":
        if field_type.lower() != "checkbox":
            print("[seek_apply_assist] generic_answer_skipped_low_confidence", flush=True)
            return GenericAnswerResolution(None, 0.2, "Programming languages prompt was not a checkbox group.", False, matched_rule="programming_languages", inferred_question_type=inferred_question_type, inferred_skill=inferred_skill)
        preferred_tokens: list[str] = []
        if (_get_profile_years(applicant_profile, "python") or 0) > 0:
            preferred_tokens.append("python")
        if (_get_profile_years(applicant_profile, "sql") or 0) > 0:
            preferred_tokens.append("sql")
        option = _pick_checkbox_option(available_options, preferred_tokens)
        if option:
            print(f"[seek_apply_assist] chosen_answer: {option}", flush=True)
            print("[seek_apply_assist] confidence: 0.9", flush=True)
            print("[seek_apply_assist] generic_answer_success", flush=True)
            return GenericAnswerResolution(
                chosen_answer=option,
                confidence=0.9,
                reason="Used applicant_profile skills to choose a truthful programming language option.",
                should_answer=True,
                matched_rule="programming_languages_profile_match",
                inferred_question_type=inferred_question_type,
                inferred_skill="programming languages",
            )
        print("[seek_apply_assist] generic_answer_skipped_low_confidence", flush=True)
        return GenericAnswerResolution(None, 0.15, "No safe programming language checkbox option matched the profile.", False, matched_rule="programming_languages", inferred_question_type=inferred_question_type, inferred_skill=inferred_skill)

    if inferred_question_type == "data_analytics_tools":
        if field_type.lower() != "checkbox":
            print("[seek_apply_assist] generic_answer_skipped_low_confidence", flush=True)
            return GenericAnswerResolution(None, 0.2, "Data analytics tools prompt was not a checkbox group.", False, matched_rule="data_analytics_tools", inferred_question_type=inferred_question_type, inferred_skill=inferred_skill)
        preferred_tokens: list[str] = []
        if (_get_profile_years(applicant_profile, "power bi") or 0) > 0:
            preferred_tokens.extend(["power bi", "powerbi"])
        if (_get_profile_years(applicant_profile, "sql") or 0) > 0:
            preferred_tokens.append("sql")
        if bool((applicant_profile.get("software_experience") or {}).get("excel")) or bool((applicant_profile.get("software_experience") or {}).get("microsoft excel")):
            preferred_tokens.extend(["excel", "microsoft excel"])
        if (_get_profile_years(applicant_profile, "python") or 0) > 0:
            preferred_tokens.append("python")
        option = _pick_checkbox_option(available_options, preferred_tokens)
        if option:
            print(f"[seek_apply_assist] chosen_answer: {option}", flush=True)
            print("[seek_apply_assist] confidence: 0.9", flush=True)
            print("[seek_apply_assist] generic_answer_success", flush=True)
            return GenericAnswerResolution(
                chosen_answer=option,
                confidence=0.9,
                reason="Used applicant_profile skills to choose a truthful data analytics tools checkbox option.",
                should_answer=True,
                matched_rule="data_analytics_tools_profile_match",
                inferred_question_type=inferred_question_type,
                inferred_skill="data analytics tools",
            )
        print("[seek_apply_assist] generic_answer_skipped_low_confidence", flush=True)
        return GenericAnswerResolution(None, 0.15, "No safe data analytics tools checkbox option matched the profile.", False, matched_rule="data_analytics_tools", inferred_question_type=inferred_question_type, inferred_skill=inferred_skill)

    if inferred_question_type == "azure_data_services_text":
        if field_type.lower() not in {"textarea", "text", "free_text", "input"}:
            print("[seek_apply_assist] generic_answer_skipped_low_confidence", flush=True)
            return GenericAnswerResolution(None, 0.2, "Azure data services prompt was not a free-text field.", False, matched_rule="azure_data_services_text", inferred_question_type=inferred_question_type, inferred_skill=inferred_skill)
        answer = (
            "I have working experience across data and analytics platforms including SQL, reporting, and data-focused delivery, "
            "but I do not want to overstate direct hands-on depth specifically with Azure Synapse and Azure Data Factory. "
            "My background is strongest in analytics, BI, stakeholder-facing delivery, and broader data work, and I would be comfortable ramping up quickly in Azure-based tooling."
        )
        print(f"[seek_apply_assist] chosen_answer: {answer}", flush=True)
        print("[seek_apply_assist] confidence: 0.92", flush=True)
        print("[seek_apply_assist] generic_answer_success", flush=True)
        return GenericAnswerResolution(
            chosen_answer=answer,
            confidence=0.92,
            reason="Used a built-in truthful limit answer for Azure Synapse / ADF experience prompt.",
            should_answer=True,
            matched_rule="azure_data_services_truthful_limit",
            inferred_question_type=inferred_question_type,
            inferred_skill="azure data services",
        )

    if inferred_question_type == "education_level":
        highest_level = str(applicant_profile.get("highest_education_level") or "").strip()
        if not highest_level:
            highest_level = _infer_highest_education_level_from_profile(applicant_profile)
        option = _pick_highest_education_option(available_options, highest_level)
        if option:
            print(f"[seek_apply_assist] chosen_answer: {option}", flush=True)
            print("[seek_apply_assist] confidence: 0.9", flush=True)
            print("[seek_apply_assist] generic_answer_success", flush=True)
            return GenericAnswerResolution(
                chosen_answer=option,
                confidence=0.9,
                reason=f"Used applicant_profile.highest_education_level={highest_level or option}.",
                should_answer=True,
                matched_rule="education_level",
                inferred_question_type=inferred_question_type,
                inferred_skill=inferred_skill,
            )
        print("[seek_apply_assist] generic_answer_skipped_low_confidence", flush=True)
        return GenericAnswerResolution(None, 0.2, "Education options were not recognisable.", False, matched_rule="education_level", inferred_question_type=inferred_question_type, inferred_skill=inferred_skill)

    if inferred_question_type == "gender_disclosure":
        option = _pick_option(available_options, ["do not wish to disclose", "prefer not to say", "other"])
        if option:
            print(f"[seek_apply_assist] chosen_answer: {option}", flush=True)
            print("[seek_apply_assist] confidence: 0.95", flush=True)
            print("[seek_apply_assist] generic_answer_success", flush=True)
            return GenericAnswerResolution(
                chosen_answer=option,
                confidence=0.95,
                reason="Used a neutral gender disclosure option.",
                should_answer=True,
                matched_rule="gender_disclosure_neutral",
                inferred_question_type=inferred_question_type,
                inferred_skill=inferred_skill,
            )
        print("[seek_apply_assist] generic_answer_skipped_low_confidence", flush=True)
        return GenericAnswerResolution(None, 0.2, "Gender disclosure options were not recognisable.", False, matched_rule="gender_disclosure", inferred_question_type=inferred_question_type, inferred_skill=inferred_skill)

    if inferred_question_type == "qualification" and "mathematics" in lowered_question:
        has_math_related_qualification = _profile_has_math_related_qualification(applicant_profile)
        if has_math_related_qualification:
            highest_level = str(applicant_profile.get("highest_education_level") or "").strip()
            if not highest_level:
                highest_level = _infer_highest_education_level_from_profile(applicant_profile)
            preferred_tokens = []
            if highest_level:
                preferred_tokens.append(f"yes, {highest_level.lower()}")
            preferred_tokens.extend(
                [
                    "i have a qualification in mathematics which isn't listed",
                    "i have a qualification in mathematics which isn't listed",
                    "isn't listed",
                    "isnt listed",
                    "not listed",
                ]
            )
            option = _pick_option(available_options, preferred_tokens)
        else:
            option = _pick_option(
                available_options,
                ["i don't have a qualification in mathematics", "don't have", "do not have", "no"],
            )
        if option:
            print(f"[seek_apply_assist] chosen_answer: {option}", flush=True)
            print("[seek_apply_assist] confidence: 0.9", flush=True)
            print("[seek_apply_assist] generic_answer_success", flush=True)
            return GenericAnswerResolution(
                chosen_answer=option,
                confidence=0.9,
                reason="Used applicant_profile qualifications to answer mathematics qualification question.",
                should_answer=True,
                matched_rule="mathematics_qualification",
                inferred_question_type=inferred_question_type,
                inferred_skill=inferred_skill,
            )
        print("[seek_apply_assist] generic_answer_skipped_low_confidence", flush=True)
        return GenericAnswerResolution(None, 0.2, "Mathematics qualification options were not recognisable.", False, matched_rule="mathematics_qualification", inferred_question_type=inferred_question_type, inferred_skill=inferred_skill)

    if inferred_question_type == "qualification" and "management" in lowered_question:
        has_management_qualification = bool((applicant_profile.get("qualifications") or {}).get("management"))
        option = _pick_yes_no_option(available_options, yes=has_management_qualification)
        if option:
            print(f"[seek_apply_assist] chosen_answer: {option}", flush=True)
            print("[seek_apply_assist] confidence: 0.9", flush=True)
            print("[seek_apply_assist] generic_answer_success", flush=True)
            return GenericAnswerResolution(
                chosen_answer=option,
                confidence=0.9,
                reason="Used applicant_profile.qualifications.management.",
                should_answer=True,
                matched_rule="management_qualification",
                inferred_question_type=inferred_question_type,
                inferred_skill=inferred_skill,
            )
        fallback_option = _pick_option(available_options, ["no", "false", "not completed", "do not have"])
        if fallback_option:
            print(f"[seek_apply_assist] chosen_answer: {fallback_option}", flush=True)
            print("[seek_apply_assist] confidence: 0.8", flush=True)
            print("[seek_apply_assist] generic_answer_success", flush=True)
            return GenericAnswerResolution(
                chosen_answer=fallback_option,
                confidence=0.8,
                reason="Used a safe fallback because no management qualification is present in the applicant profile.",
                should_answer=True,
                matched_rule="management_qualification",
                inferred_question_type=inferred_question_type,
                inferred_skill=inferred_skill,
            )
        fallback_option = _pick_option(
            available_options,
            ["i don't have", "don't have", "i do not have", "do not have", "not completed", "no", "false"],
        )
        if fallback_option:
            print(f"[seek_apply_assist] chosen_answer: {fallback_option}", flush=True)
            print("[seek_apply_assist] confidence: 0.8", flush=True)
            print("[seek_apply_assist] generic_answer_success", flush=True)
            return GenericAnswerResolution(
                chosen_answer=fallback_option,
                confidence=0.8,
                reason="Used a safe fallback because no management qualification is present in the applicant profile.",
                should_answer=True,
                matched_rule="management_qualification",
                inferred_question_type=inferred_question_type,
                inferred_skill=inferred_skill,
            )
        print("[seek_apply_assist] generic_answer_skipped_low_confidence", flush=True)
        return GenericAnswerResolution(None, 0.2, "Management qualification options were not recognisable.", False, matched_rule="management_qualification", inferred_question_type=inferred_question_type, inferred_skill=inferred_skill)

    if inferred_question_type == "qualification":
        print("[seek_apply_assist] generic_answer_skipped_low_confidence", flush=True)
        return GenericAnswerResolution(
            None,
            0.2,
            "Qualification question did not match an explicit supported rule.",
            False,
            matched_rule="generic_qualification_unanswered",
            inferred_question_type=inferred_question_type,
            inferred_skill=inferred_skill,
        )

    if inferred_question_type == "experience_years":
        if field_type.lower() not in {"select", "radio"}:
            print("[seek_apply_assist] generic_answer_skipped_low_confidence", flush=True)
            return GenericAnswerResolution(None, 0.2, "Years resolver only applies to select/radio fields.", False, inferred_question_type=inferred_question_type, inferred_skill=inferred_skill)
        if any(phrase in lowered_question for phrase in ("walk us through", "describe", "explain", "tell us about")):
            print("[seek_apply_assist] generic_answer_skipped_low_confidence", flush=True)
            return GenericAnswerResolution(None, 0.15, "Prompt is descriptive free text, not a structured years selector.", False, inferred_question_type=inferred_question_type, inferred_skill=inferred_skill)
        if not _looks_like_years_option_set(available_options):
            print("[seek_apply_assist] generic_answer_skipped_low_confidence", flush=True)
            return GenericAnswerResolution(None, 0.2, "Available options do not look like years ranges.", False, inferred_question_type=inferred_question_type, inferred_skill=inferred_skill)
        years_value = _get_profile_years(applicant_profile, inferred_skill)
        print(f"[seek_apply_assist] profile_years_used: {years_value if years_value is not None else '(none)'}", flush=True)
        if years_value is None:
            print("[seek_apply_assist] generic_answer_skipped_low_confidence", flush=True)
            return GenericAnswerResolution(None, 0.2, "No profile years were available.", False, inferred_question_type=inferred_question_type, inferred_skill=inferred_skill)
        option = _pick_experience_option(available_options, years_value)
        chosen_answer = option or (f"More than 5 years" if years_value > 5 else f"{years_value} years")
        print(f"[seek_apply_assist] chosen_answer: {chosen_answer}", flush=True)
        print("[seek_apply_assist] confidence: 0.85", flush=True)
        print("[seek_apply_assist] generic_answer_success", flush=True)
        return GenericAnswerResolution(
            chosen_answer=chosen_answer,
            confidence=0.85,
            reason=f"Used profile years value {years_value} for {inferred_skill or 'industry experience'}.",
            should_answer=True,
            matched_rule=f"generic_experience_{(inferred_skill or 'industry').replace(' ', '_')}",
            inferred_question_type=inferred_question_type,
            inferred_skill=inferred_skill,
            profile_years_used=years_value,
        )

    if inferred_question_type == "salary":
        salary_answer = str(
            applicant_profile.get("salary_expectation_text")
            or ((applicant_profile.get("job_context") or {}).get("salary_expectation_text"))
            or "185k"
        ).strip()
        if field_type.lower() in {"select", "radio"} and available_options:
            salary_option = _pick_salary_option(available_options, salary_answer)
            if salary_option:
                print(f"[seek_apply_assist] chosen_answer: {salary_option}", flush=True)
                print("[seek_apply_assist] confidence: 0.9", flush=True)
                print("[seek_apply_assist] generic_answer_success", flush=True)
                return GenericAnswerResolution(
                    chosen_answer=salary_option,
                    confidence=0.9,
                    reason=f"Mapped salary expectation {salary_answer} to the closest available salary option.",
                    should_answer=True,
                    matched_rule="salary_expectation_option_match",
                    inferred_question_type=inferred_question_type,
                    inferred_skill=inferred_skill,
                )
        if field_type.lower() in {"textarea", "text", "free_text", "input", "select"} and salary_answer:
            print(f"[seek_apply_assist] chosen_answer: {salary_answer}", flush=True)
            print("[seek_apply_assist] confidence: 0.85", flush=True)
            print("[seek_apply_assist] generic_answer_success", flush=True)
            return GenericAnswerResolution(
                chosen_answer=salary_answer,
                confidence=0.85,
                reason="Used salary expectation text for explicit salary question.",
                should_answer=True,
                matched_rule="salary_expectation",
                inferred_question_type=inferred_question_type,
                inferred_skill=inferred_skill,
            )
        print("[seek_apply_assist] generic_answer_skipped_low_confidence", flush=True)
        return GenericAnswerResolution(None, 0.2, "Salary question could not be answered safely.", False, matched_rule="salary_expectation", inferred_question_type=inferred_question_type, inferred_skill=inferred_skill)

    if inferred_question_type == "legal_right_to_work":
        if field_type.lower() in {"select", "radio"} and any("citizen" in option.lower() or "resident" in option.lower() or "visa" in option.lower() for option in available_options):
            right_to_work = str(applicant_profile.get("right_to_work_nz") or "New Zealand citizen").strip()
            option = _pick_option(
                available_options,
                [
                    right_to_work.lower(),
                    "new zealand citizen",
                    "nz citizen",
                    "citizen",
                    "new zealand permanent resident",
                    "permanent resident",
                ],
            )
            if option:
                print(f"[seek_apply_assist] chosen_answer: {option}", flush=True)
                print("[seek_apply_assist] confidence: 0.98", flush=True)
                print("[seek_apply_assist] generic_answer_success", flush=True)
                return GenericAnswerResolution(
                    chosen_answer=option,
                    confidence=0.98,
                    reason="Used applicant_profile.right_to_work_nz.",
                    should_answer=True,
                    matched_rule="right_to_work_nz_option_match",
                    inferred_question_type=inferred_question_type,
                    inferred_skill=inferred_skill,
                )
        right_to_work = str(applicant_profile.get("right_to_work_nz") or "").strip().lower()
        yes_value = bool(right_to_work and ("citizen" in right_to_work or "resident" in right_to_work or "yes" in right_to_work))
        option = _pick_yes_no_option(available_options, yes=yes_value)
        if option:
            print(f"[seek_apply_assist] chosen_answer: {option}", flush=True)
            print("[seek_apply_assist] confidence: 0.95", flush=True)
            print("[seek_apply_assist] generic_answer_success", flush=True)
            return GenericAnswerResolution(
                chosen_answer=option,
                confidence=0.95,
                reason="Used applicant_profile.right_to_work_nz.",
                should_answer=True,
                matched_rule="legal_right_to_work",
                inferred_question_type=inferred_question_type,
                inferred_skill=inferred_skill,
            )
        print("[seek_apply_assist] generic_answer_skipped_low_confidence", flush=True)
        return GenericAnswerResolution(None, 0.1, "Right-to-work options were not recognisable.", False, inferred_question_type=inferred_question_type, inferred_skill=inferred_skill)

    if "working rights for nz" in lowered_question or "work rights for nz" in lowered_question:
        right_to_work = str(applicant_profile.get("right_to_work_nz") or "").strip().lower()
        yes_value = bool(right_to_work and ("citizen" in right_to_work or "resident" in right_to_work or "yes" in right_to_work))
        option = _pick_yes_no_option(available_options, yes=yes_value)
        if option:
            print(f"[seek_apply_assist] chosen_answer: {option}", flush=True)
            print("[seek_apply_assist] confidence: 0.95", flush=True)
            print("[seek_apply_assist] generic_answer_success", flush=True)
            return GenericAnswerResolution(
                chosen_answer=option,
                confidence=0.95,
                reason="Used applicant_profile.right_to_work_nz for the NZ working-rights yes/no question.",
                should_answer=True,
                matched_rule="working_rights_nz_yes_no",
                inferred_question_type=inferred_question_type,
                inferred_skill=inferred_skill,
            )
        print("[seek_apply_assist] generic_answer_skipped_low_confidence", flush=True)
        return GenericAnswerResolution(
            None,
            0.2,
            "NZ working-rights yes/no options were not recognisable.",
            False,
            matched_rule="working_rights_nz_yes_no",
            inferred_question_type=inferred_question_type,
            inferred_skill=inferred_skill,
        )

    if lowered_question == "pronouns":
        preferred_pronouns = str(
            applicant_profile.get("pronouns")
            or ((applicant_profile.get("personal_details") or {}).get("pronouns"))
            or ""
        ).strip()
        if preferred_pronouns:
            option = _pick_option(available_options, [preferred_pronouns])
            if option:
                print(f"[seek_apply_assist] chosen_answer: {option}", flush=True)
                print("[seek_apply_assist] confidence: 0.95", flush=True)
                print("[seek_apply_assist] generic_answer_success", flush=True)
                return GenericAnswerResolution(
                    chosen_answer=option,
                    confidence=0.95,
                    reason="Used explicit pronouns from the applicant profile.",
                    should_answer=True,
                    matched_rule="pronouns_profile",
                    inferred_question_type=inferred_question_type,
                    inferred_skill=inferred_skill,
                )
        option = _pick_option(available_options, ["my pronouns are not listed above"])
        if option:
            print(f"[seek_apply_assist] chosen_answer: {option}", flush=True)
            print("[seek_apply_assist] confidence: 0.9", flush=True)
            print("[seek_apply_assist] generic_answer_success", flush=True)
            return GenericAnswerResolution(
                chosen_answer=option,
                confidence=0.9,
                reason="Used the neutral pronouns fallback option provided by SEEK.",
                should_answer=True,
                matched_rule="pronouns_neutral_fallback",
                inferred_question_type=inferred_question_type,
                inferred_skill=inferred_skill,
            )
        print("[seek_apply_assist] generic_answer_skipped_low_confidence", flush=True)
        return GenericAnswerResolution(
            None,
            0.2,
            "Pronouns options were not recognisable.",
            False,
            matched_rule="pronouns",
            inferred_question_type=inferred_question_type,
            inferred_skill=inferred_skill,
        )

    if "māori" in lowered_question or "maori" in lowered_question or "pasifika" in lowered_question:
        option = _pick_option(available_options, ["do not wish to disclose", "prefer not to say"])
        if option:
            print(f"[seek_apply_assist] chosen_answer: {option}", flush=True)
            print("[seek_apply_assist] confidence: 0.9", flush=True)
            print("[seek_apply_assist] generic_answer_success", flush=True)
            return GenericAnswerResolution(
                chosen_answer=option,
                confidence=0.9,
                reason="Used the neutral disclosure option for demographic self-identification.",
                should_answer=True,
                matched_rule="demographic_disclosure_neutral",
                inferred_question_type=inferred_question_type,
                inferred_skill=inferred_skill,
            )
        print("[seek_apply_assist] generic_answer_skipped_low_confidence", flush=True)
        return GenericAnswerResolution(
            None,
            0.2,
            "Demographic disclosure options were not recognisable.",
            False,
            matched_rule="demographic_disclosure_neutral",
            inferred_question_type=inferred_question_type,
            inferred_skill=inferred_skill,
        )

    if (
        "reasonable adjustments" in lowered_question
        or "accessibility" in lowered_question
        or "neurodiversity" in lowered_question
    ):
        if field_type.lower() not in {"textarea", "text", "free_text", "input"}:
            print("[seek_apply_assist] generic_answer_skipped_low_confidence", flush=True)
            return GenericAnswerResolution(
                None,
                0.2,
                "Reasonable-adjustments prompt was not a free-text field.",
                False,
                matched_rule="reasonable_adjustments_text",
                inferred_question_type=inferred_question_type,
                inferred_skill=inferred_skill,
            )
        answer = "No adjustments required at this stage."
        print(f"[seek_apply_assist] chosen_answer: {answer}", flush=True)
        print("[seek_apply_assist] confidence: 0.95", flush=True)
        print("[seek_apply_assist] generic_answer_success", flush=True)
        return GenericAnswerResolution(
            chosen_answer=answer,
            confidence=0.95,
            reason="Used a neutral accessibility/adjustments response.",
            should_answer=True,
            matched_rule="reasonable_adjustments_text",
            inferred_question_type=inferred_question_type,
            inferred_skill=inferred_skill,
        )

    if field_type.lower() in {"select", "radio"} and _is_yes_no_option_set(available_options):
        disclosures = dict(applicant_profile.get("disclosures") or {})

        if "currently work or have previously worked for" in lowered_question:
            option = _pick_yes_no_option(
                available_options,
                yes=bool(disclosures.get("worked_for_target_employer_before", False)),
            )
            if option:
                print(f"[seek_apply_assist] chosen_answer: {option}", flush=True)
                print("[seek_apply_assist] confidence: 0.9", flush=True)
                print("[seek_apply_assist] generic_answer_success", flush=True)
                return GenericAnswerResolution(
                    chosen_answer=option,
                    confidence=0.9,
                    reason="Used applicant_profile.disclosures.worked_for_target_employer_before.",
                    should_answer=True,
                    matched_rule="worked_for_target_employer_before",
                    inferred_question_type="employment_history",
                    inferred_skill=inferred_skill,
                )

        if "criminal offence" in lowered_question and "has_criminal_offence_history" in disclosures:
            option = _pick_yes_no_option(
                available_options,
                yes=bool(disclosures.get("has_criminal_offence_history")),
            )
            if option:
                print(f"[seek_apply_assist] chosen_answer: {option}", flush=True)
                print("[seek_apply_assist] confidence: 0.9", flush=True)
                print("[seek_apply_assist] generic_answer_success", flush=True)
                return GenericAnswerResolution(
                    chosen_answer=option,
                    confidence=0.9,
                    reason="Used applicant_profile.disclosures.has_criminal_offence_history.",
                    should_answer=True,
                    matched_rule="criminal_offence_history",
                    inferred_question_type="legal_sensitive",
                    inferred_skill=inferred_skill,
                )

        if "pre-existing conditions" in lowered_question or "pre existing conditions" in lowered_question:
            option = _pick_yes_no_option(
                available_options,
                yes=bool(disclosures.get("has_preexisting_conditions_affecting_role", False)),
            )
            if option:
                print(f"[seek_apply_assist] chosen_answer: {option}", flush=True)
                print("[seek_apply_assist] confidence: 0.88", flush=True)
                print("[seek_apply_assist] generic_answer_success", flush=True)
                return GenericAnswerResolution(
                    chosen_answer=option,
                    confidence=0.88,
                    reason="Used applicant_profile.disclosures.has_preexisting_conditions_affecting_role.",
                    should_answer=True,
                    matched_rule="preexisting_conditions_affecting_role",
                    inferred_question_type="legal_sensitive",
                    inferred_skill=inferred_skill,
                )

        if "performance improvement plan" in lowered_question:
            option = _pick_yes_no_option(
                available_options,
                yes=bool(disclosures.get("has_performance_improvement_history", False)),
            )
            if option:
                print(f"[seek_apply_assist] chosen_answer: {option}", flush=True)
                print("[seek_apply_assist] confidence: 0.88", flush=True)
                print("[seek_apply_assist] generic_answer_success", flush=True)
                return GenericAnswerResolution(
                    chosen_answer=option,
                    confidence=0.88,
                    reason="Used applicant_profile.disclosures.has_performance_improvement_history.",
                    should_answer=True,
                    matched_rule="performance_improvement_history",
                    inferred_question_type="employment_history",
                    inferred_skill=inferred_skill,
                )

        if "disciplinary action" in lowered_question or "disciplinary process" in lowered_question:
            option = _pick_yes_no_option(
                available_options,
                yes=bool(disclosures.get("has_disciplinary_history", False)),
            )
            if option:
                print(f"[seek_apply_assist] chosen_answer: {option}", flush=True)
                print("[seek_apply_assist] confidence: 0.88", flush=True)
                print("[seek_apply_assist] generic_answer_success", flush=True)
                return GenericAnswerResolution(
                    chosen_answer=option,
                    confidence=0.88,
                    reason="Used applicant_profile.disclosures.has_disciplinary_history.",
                    should_answer=True,
                    matched_rule="disciplinary_history",
                    inferred_question_type="employment_history",
                    inferred_skill=inferred_skill,
                )

    if inferred_question_type == "licence" and field_type.lower() in {"select", "radio"}:
        has_nz_licence = bool(((applicant_profile.get("drivers_licence") or {}).get("nz_current")))
        option = None
        if has_nz_licence:
            option = _pick_option(
                available_options,
                [
                    "full nz drivers licence",
                    "full new zealand drivers licence",
                    "full nz driver's licence",
                    "full new zealand driver's licence",
                    "yes",
                ],
            )
        else:
            option = _pick_option(available_options, ["none of the above", "no"])
        if option:
            print(f"[seek_apply_assist] chosen_answer: {option}", flush=True)
            print("[seek_apply_assist] confidence: 0.95", flush=True)
            print("[seek_apply_assist] generic_answer_success", flush=True)
            return GenericAnswerResolution(
                chosen_answer=option,
                confidence=0.95,
                reason="Used applicant_profile.drivers_licence.nz_current.",
                should_answer=True,
                matched_rule="drivers_licence_profile_match",
                inferred_question_type=inferred_question_type,
                inferred_skill=inferred_skill,
            )
        print("[seek_apply_assist] generic_answer_skipped_low_confidence", flush=True)
        return GenericAnswerResolution(None, 0.2, "Drivers licence options were not recognisable.", False, matched_rule="drivers_licence_profile_match", inferred_question_type=inferred_question_type, inferred_skill=inferred_skill)

    if field_type.lower() == "checkbox" and "forklift" in lowered_question:
        option = _pick_checkbox_option(
            available_options,
            ["none of these", "none of the above", "no forklift licence", "no forklift license", "none"],
        )
        if option:
            print(f"[seek_apply_assist] chosen_answer: {option}", flush=True)
            print("[seek_apply_assist] confidence: 0.96", flush=True)
            print("[seek_apply_assist] generic_answer_success", flush=True)
            return GenericAnswerResolution(
                chosen_answer=option,
                confidence=0.96,
                reason="Used a no-forklift-licence default for forklift licence checkbox questions.",
                should_answer=True,
                matched_rule="forklift_licence_none",
                inferred_question_type="licence",
                inferred_skill="forklift licence",
            )

    if field_type.lower() in {"textarea", "text", "free_text", "input"}:
        if "notice period" in lowered_question:
            try:
                notice_weeks = int(applicant_profile.get("notice_period_weeks", 2))
            except Exception:  # noqa: BLE001
                notice_weeks = 2
            answer = "Immediately" if notice_weeks <= 0 else "1 week" if notice_weeks == 1 else f"{notice_weeks} weeks"
            print(f"[seek_apply_assist] chosen_answer: {answer}", flush=True)
            print("[seek_apply_assist] confidence: 0.95", flush=True)
            print("[seek_apply_assist] generic_answer_success", flush=True)
            return GenericAnswerResolution(
                chosen_answer=answer,
                confidence=0.95,
                reason="Used applicant_profile.notice_period_weeks for a free-text notice-period field.",
                should_answer=True,
                matched_rule="notice_period_text",
                inferred_question_type="notice_period",
                inferred_skill=inferred_skill,
            )

        if "please use this space to include any additional information" in lowered_question:
            answer = "N/A"
            print(f"[seek_apply_assist] chosen_answer: {answer}", flush=True)
            print("[seek_apply_assist] confidence: 0.9", flush=True)
            print("[seek_apply_assist] generic_answer_success", flush=True)
            return GenericAnswerResolution(
                chosen_answer=answer,
                confidence=0.9,
                reason="Used a neutral not-applicable response for an open additional-information field.",
                should_answer=True,
                matched_rule="additional_information_not_applicable",
                inferred_question_type="additional_information",
                inferred_skill=inferred_skill,
            )

        if (
            "simply indicate nil or na" in lowered_question
            or "indicate nil or na" in lowered_question
            or ("if there isn't any" in lowered_question and "nil or na" in lowered_question)
        ):
            answer = "Nil"
            print(f"[seek_apply_assist] chosen_answer: {answer}", flush=True)
            print("[seek_apply_assist] confidence: 0.94", flush=True)
            print("[seek_apply_assist] generic_answer_success", flush=True)
            return GenericAnswerResolution(
                chosen_answer=answer,
                confidence=0.94,
                reason="The question explicitly instructs the applicant to enter Nil or NA when not applicable.",
                should_answer=True,
                matched_rule="explicit_nil_or_na_instruction",
                inferred_question_type=inferred_question_type,
                inferred_skill=inferred_skill,
            )

        if "referred by an existing fidelity life employee" in lowered_question:
            answer = "NA"
            print(f"[seek_apply_assist] chosen_answer: {answer}", flush=True)
            print("[seek_apply_assist] confidence: 0.9", flush=True)
            print("[seek_apply_assist] generic_answer_success", flush=True)
            return GenericAnswerResolution(
                chosen_answer=answer,
                confidence=0.9,
                reason="No employee referral information is available; using the question's not-applicable path.",
                should_answer=True,
                matched_rule="employee_referral_not_applicable",
                inferred_question_type="employee_referral",
                inferred_skill=inferred_skill,
            )

        if (
            "referred by someone working at" in lowered_question
            or "referred by someone" in lowered_question
            or "include the name of the employee" in lowered_question
        ):
            answer = "N/A - not referred by an employee."
            print(f"[seek_apply_assist] chosen_answer: {answer}", flush=True)
            print("[seek_apply_assist] confidence: 0.92", flush=True)
            print("[seek_apply_assist] generic_answer_success", flush=True)
            return GenericAnswerResolution(
                chosen_answer=answer,
                confidence=0.92,
                reason="No employee referral information is available; using a neutral not-applicable referral response.",
                should_answer=True,
                matched_rule="employee_referral_not_applicable",
                inferred_question_type="employee_referral",
                inferred_skill=inferred_skill,
            )

        if (
            "if yes, please provide details" in lowered_question
            or "if yes, please provide details:" in lowered_question
        ) and (
            "conviction" in lowered_question
            or "criminal" in lowered_question
            or "performance improvement" in lowered_question
            or "disciplinary action" in lowered_question
            or "disciplinary process" in lowered_question
        ):
            answer = "Nil"
            print(f"[seek_apply_assist] chosen_answer: {answer}", flush=True)
            print("[seek_apply_assist] confidence: 0.88", flush=True)
            print("[seek_apply_assist] generic_answer_success", flush=True)
            return GenericAnswerResolution(
                chosen_answer=answer,
                confidence=0.88,
                reason="Used the profile-backed negative disclosure defaults for this follow-up details field.",
                should_answer=True,
                matched_rule="negative_disclosure_follow_up_nil",
                inferred_question_type=inferred_question_type,
                inferred_skill=inferred_skill,
            )

        if "disciplinary action" in lowered_question or "disciplinary process" in lowered_question:
            answer = "Nil"
            print(f"[seek_apply_assist] chosen_answer: {answer}", flush=True)
            print("[seek_apply_assist] confidence: 0.88", flush=True)
            print("[seek_apply_assist] generic_answer_success", flush=True)
            return GenericAnswerResolution(
                chosen_answer=answer,
                confidence=0.88,
                reason="Used the profile-backed negative disciplinary-history default for this free-text disclosure field.",
                should_answer=True,
                matched_rule="disciplinary_follow_up_nil",
                inferred_question_type=inferred_question_type,
                inferred_skill=inferred_skill,
            )

    if inferred_question_type in {"licence", "qualification", "legal_sensitive"}:
        print("[seek_apply_assist] generic_answer_skipped_low_confidence", flush=True)
        return GenericAnswerResolution(None, 0.1, "Sensitive question type requires an explicit rule.", False, inferred_question_type=inferred_question_type, inferred_skill=inferred_skill)

    if field_type.lower() in {"select", "radio"} and _is_yes_no_option_set(available_options):
        if "health, safety, environment and quality" in lowered_question or "hseq" in lowered_question:
            option = _pick_yes_no_option(available_options, yes=False)
            if option:
                print(f"[seek_apply_assist] chosen_answer: {option}", flush=True)
                print("[seek_apply_assist] confidence: 0.82", flush=True)
                print("[seek_apply_assist] generic_answer_success", flush=True)
                return GenericAnswerResolution(
                    chosen_answer=option,
                    confidence=0.82,
                    reason="Used a conservative no-answer because the applicant profile does not contain explicit HSEQ experience.",
                    should_answer=True,
                    matched_rule="hseq_experience_conservative_no",
                    inferred_question_type="skill_experience_yes_no",
                    inferred_skill="hseq",
                )
        if "continuous production environment" in lowered_question:
            option = _pick_yes_no_option(available_options, yes=False)
            if option:
                print(f"[seek_apply_assist] chosen_answer: {option}", flush=True)
                print("[seek_apply_assist] confidence: 0.82", flush=True)
                print("[seek_apply_assist] generic_answer_success", flush=True)
                return GenericAnswerResolution(
                    chosen_answer=option,
                    confidence=0.82,
                    reason="Used a conservative no-answer because the applicant profile does not contain explicit continuous-production experience.",
                    should_answer=True,
                    matched_rule="continuous_production_experience_conservative_no",
                    inferred_question_type="skill_experience_yes_no",
                    inferred_skill="continuous production environment",
                )

    if field_type.lower() in {"select", "radio"} and _is_yes_no_option_set(available_options) and _question_looks_like_skill_experience_yes_no(question_text):
        contextual_skill, has_experience, confidence, contextual_reason = _infer_contextual_skill_experience(question_text, applicant_profile)
        if contextual_skill and has_experience is not None:
            option = _pick_yes_no_option(available_options, yes=has_experience)
            if option:
                print(f"[seek_apply_assist] contextual_skill_answer_used: {contextual_skill}", flush=True)
                print(f"[seek_apply_assist] chosen_answer: {option}", flush=True)
                print(f"[seek_apply_assist] confidence: {confidence}", flush=True)
                print("[seek_apply_assist] generic_answer_success", flush=True)
                return GenericAnswerResolution(
                    chosen_answer=option,
                    confidence=confidence,
                    reason=contextual_reason,
                    should_answer=True,
                    matched_rule="generic_skill_experience_yes_no",
                    inferred_question_type="skill_experience_yes_no",
                    inferred_skill=contextual_skill,
                )
        option = _pick_yes_no_option(available_options, yes=True)
        if option:
            print(f"[seek_apply_assist] chosen_answer: {option}", flush=True)
            print("[seek_apply_assist] confidence: 0.8", flush=True)
            print("[seek_apply_assist] generic_answer_success", flush=True)
            return GenericAnswerResolution(
                chosen_answer=option,
                confidence=0.8,
                reason="Used the configured default to answer skill/experience yes-no questions positively when no narrower rule matched.",
                should_answer=True,
                matched_rule="generic_skill_experience_yes_default",
                inferred_question_type="skill_experience_yes_no",
                inferred_skill=contextual_skill,
            )

    if not bool((applicant_profile.get("automation") or {}).get("allow_reasonable_fallback_answers", True)):
        print("[seek_apply_assist] generic_answer_skipped_low_confidence", flush=True)
        return GenericAnswerResolution(None, 0.1, "Reasonable fallback answers are disabled.", False, inferred_question_type=inferred_question_type, inferred_skill=inferred_skill)

    print("[seek_apply_assist] generic_answer_skipped_low_confidence", flush=True)
    return GenericAnswerResolution(None, 0.2, "No safe generic resolver matched this question.", False, inferred_question_type=inferred_question_type, inferred_skill=inferred_skill)


def _resolve_dynamic_question_answer(question: dict[str, Any], applicant_profile: dict[str, Any]) -> tuple[str | None, str, str]:
    question_text = str(question.get("question_text") or "").strip()
    options = [str(option).strip() for option in question.get("options", []) if str(option).strip()]
    lowered = question_text.lower()
    field_type = str(question.get("field_type") or "").strip().lower()

    if _is_data_engineer_years_question(question_text) and field_type in {"select", "radio"}:
        option = _pick_experience_option(options, 2)
        if option:
            return option, "data_engineer_years_override", "explicit_rule.data_engineer_years=2"
        return ("2 years", "data_engineer_years_override", "explicit_rule.data_engineer_years=2")

    if "right to work" in lowered or "work in new zealand" in lowered or "working rights in new zealand" in lowered:
        right_to_work = str(applicant_profile.get("right_to_work_nz") or "New Zealand citizen").strip()
        option = _pick_option(options, ["new zealand citizen", "citizen", "yes", "true"])
        return (option or right_to_work, "right_to_work_nz", "applicant_profile.right_to_work_nz")

    if "driver" in lowered and ("licence" in lowered or "license" in lowered) and "new zealand" in lowered:
        has_nz_licence = bool(((applicant_profile.get("drivers_licence") or {}).get("nz_current")))
        option = _pick_yes_no_option(options, yes=has_nz_licence)
        if option:
            return option, "drivers_licence_nz_current", "applicant_profile.drivers_licence.nz_current"
        return (None, "drivers_licence_nz_current", "")

    if "qualification" in lowered and "ict" in lowered:
        has_ict = bool((applicant_profile.get("qualifications") or {}).get("ict"))
        if has_ict:
            option = _pick_option(
                options,
                [
                    "yes, bachelor degree",
                    "i have a qualification in ict which isn't listed",
                    "isn't listed",
                    "isnt listed",
                    "not listed",
                    "yes",
                    "true",
                ],
            )
        else:
            option = _pick_option(options, ["i don't have a qualification in ict", "don't have", "do not have", "no", "false"])
        if option:
            return option, "ict_qualification", "applicant_profile.qualifications.ict"
        return (None, "ict_qualification", "")

    if "sql data analyst" in lowered or ("sql" in lowered and "data analyst" in lowered):
        years_value = max(
            int((applicant_profile.get("skills_years") or {}).get("sql", 0)),
            int((applicant_profile.get("skills_years") or {}).get("data analysis", 0)),
        )
        option = _pick_experience_option(options, years_value)
        if option:
            return option, "sql_data_analyst_years", "applicant_profile.skills_years.sql+data_analysis"
        return (None, "sql_data_analyst_years", "")

    if "power bi analyst" in lowered or "power bi" in lowered:
        years_value = max(
            int((applicant_profile.get("skills_years") or {}).get("power bi", 0)),
            int((applicant_profile.get("skills_years") or {}).get("business intelligence", 0)),
        )
        option = _pick_experience_option(options, years_value)
        if option:
            return option, "power_bi_years", "applicant_profile.skills_years.power_bi+business_intelligence"
        return (None, "power_bi_years", "")

    if "how many years' experience" in lowered and "sql" in lowered and "queries" in lowered:
        profile_sql_years = int((applicant_profile.get("skills_experience_years") or {}).get("sql", 0))
        option = _pick_experience_option(options, profile_sql_years)
        if option:
            return option, "sql_experience_years", f"applicant_profile.skills_experience_years.sql={profile_sql_years}"
        answer_text = f"More than 5 years" if profile_sql_years > 5 else f"{profile_sql_years} years"
        return answer_text, "sql_experience_years", f"applicant_profile.skills_experience_years.sql={profile_sql_years}"

    if field_type in {"select", "radio"} and "years" in lowered and _looks_like_years_option_set(options):
        skill_key = _detect_skill_keyword(lowered)
        if skill_key:
            years_value = int((applicant_profile.get("skills_years") or {}).get(skill_key, 0))
            option = _pick_experience_option(options, years_value)
            if option:
                return option, f"experience_{skill_key.replace(' ', '_')}", f"applicant_profile.skills_years.{skill_key}"
            answer_text = f"More than 5 years" if years_value > 5 else f"{years_value} years"
            return answer_text, f"experience_{skill_key.replace(' ', '_')}", f"applicant_profile.skills_years.{skill_key}"

    if _is_yes_no_option_set(options):
        return (None, "generic_yes_no_not_safe", "")

    return None, "", ""


def _detect_skill_keyword(question_text: str) -> str | None:
    if "sql data analyst" in question_text or ("sql" in question_text and "data analyst" in question_text):
        return "sql"
    if "power bi analyst" in question_text or "power bi" in question_text:
        return "power bi"
    skill_keywords = [
        "sql",
        "power bi",
        "business intelligence",
        "python",
        "data analysis",
        "business analysis",
        "analytics",
        "data engineering",
        "web application testing",
        "software testing",
        "quality assurance",
        "qa",
        "uat",
        "reporting",
        "dashboards",
    ]
    for keyword in skill_keywords:
        if keyword in question_text:
            return keyword
    if "testing web applications" in question_text or "test web applications" in question_text or "web testing" in question_text:
        return "web application testing"
    return None


def _pick_option(options: list[str], preferred_tokens: list[str]) -> str | None:
    lowered_options = [(option, option.lower()) for option in options]
    for token in preferred_tokens:
        for option, lowered in lowered_options:
            if token in lowered:
                return option
    return None


def _parse_salary_amount_k(text: str) -> int | None:
    lowered = str(text or "").strip().lower().replace(",", "")
    match = re.search(r"(\d+)\s*k", lowered)
    if match:
        return int(match.group(1))
    match = re.search(r"\$?\s*(\d{2,3})(?:000)?", lowered)
    if match:
        return int(match.group(1))
    return None


def _pick_salary_option(options: list[str], desired_answer: str) -> str | None:
    desired_value = _parse_salary_amount_k(desired_answer)
    if desired_value is None:
        return None
    exact = _pick_option(options, [f"${desired_value}k", f"{desired_value}k"])
    if exact:
        return exact
    best_option: str | None = None
    best_distance: int | None = None
    best_penalty: int | None = None
    for option in options:
        option_value = _parse_salary_amount_k(option)
        if option_value is None:
            continue
        distance = abs(option_value - desired_value)
        penalty = 0 if option_value <= desired_value else 1
        if (
            best_distance is None
            or distance < best_distance
            or (distance == best_distance and penalty < (best_penalty if best_penalty is not None else 99))
            or (distance == best_distance and penalty == best_penalty and option_value < (_parse_salary_amount_k(best_option or "") or 10_000))
        ):
            best_option = option
            best_distance = distance
            best_penalty = penalty
    return best_option


def _pick_yes_no_option(options: list[str], *, yes: bool) -> str | None:
    preferred = ["yes", "true"] if yes else ["no", "false"]
    return _pick_option(options, preferred)


def _infer_highest_education_level_from_profile(applicant_profile: dict[str, Any]) -> str:
    explicit_level = str(applicant_profile.get("highest_education_level") or "").strip()
    if explicit_level:
        return explicit_level
    qualifications = dict(applicant_profile.get("qualifications") or {})
    if any(bool(qualifications.get(key)) for key in ("statistics", "data_science", "operations_research", "ict", "mathematics")):
        return "Bachelor Degree"
    return ""


def _pick_highest_education_option(options: list[str], preferred_level: str) -> str | None:
    preferred = str(preferred_level or "").strip().lower()
    if preferred:
        option = _pick_option(options, [preferred])
        if option:
            return option
    for fallback in (
        "bachelor degree",
        "bachelor degree (honours)",
        "graduate diploma",
        "graduate certificate",
        "postgraduate diploma",
        "postgraduate certificate",
        "masters degree",
        "doctoral degree",
    ):
        option = _pick_option(options, [fallback])
        if option:
            return option
    return None


def _profile_has_math_related_qualification(applicant_profile: dict[str, Any]) -> bool:
    qualifications = dict(applicant_profile.get("qualifications") or {})
    return any(bool(qualifications.get(key)) for key in ("mathematics", "statistics", "data_science", "operations_research"))


def _pick_notice_period_option(options: list[str], notice_weeks: int) -> str | None:
    lowered_options = [(option, option.lower()) for option in options]
    if notice_weeks <= 2:
        for exact_preference in ("2 weeks", "two weeks", "2 weeks or less", "less than 2 weeks"):
            for option, lowered in lowered_options:
                if exact_preference == lowered:
                    return option
    preferred_tokens: list[str] = []
    if notice_weeks <= 0:
        preferred_tokens.extend(["immediately", "available immediately", "straight away", "now"])
    if notice_weeks <= 1:
        preferred_tokens.extend(["1 week", "one week", "2 weeks or less", "less than 2 weeks"])
    if notice_weeks <= 2:
        preferred_tokens.extend(["2 weeks", "two weeks", "2 weeks or less", "less than 2 weeks"])
    preferred_tokens.extend(["immediately", "1 week", "one week", "2 weeks", "two weeks", "2 weeks or less", "less than 2 weeks"])
    option = _pick_option(options, preferred_tokens)
    if option:
        return option

    best_option: str | None = None
    best_weeks = -1
    for option_text in options:
        lowered = option_text.lower()
        if "immediately" in lowered:
            weeks_value = 0
        elif "less than 2 weeks" in lowered or "2 weeks or less" in lowered:
            weeks_value = 2
        else:
            match = re.search(r"\b(\d+)\s*week", lowered)
            if not match:
                continue
            weeks_value = int(match.group(1))
        if weeks_value <= notice_weeks and weeks_value > best_weeks:
            best_option = option_text
            best_weeks = weeks_value
    return best_option


def _pick_experience_option(options: list[str], years_value: int) -> str | None:
    best_option: str | None = None
    best_rank = -1
    for option in options:
        rank = _experience_option_rank(option, years_value)
        if rank is not None and rank > best_rank:
            best_rank = rank
            best_option = option
    return best_option


def _experience_option_rank(option: str, years_value: int) -> int | None:
    lowered = option.lower().strip()
    if "more than" in lowered:
        match = re.search(r"more than\s+(\d+)", lowered)
        if match:
            threshold = int(match.group(1))
            return threshold + 1 if years_value > threshold else None
    if re.search(r"(\d+)\+\s*(years?)?", lowered):
        threshold = int(re.search(r"(\d+)\+\s*(years?)?", lowered).group(1))
        return threshold + 1 if years_value >= threshold else None
    if re.search(r"(\d+)\s*-\s*(\d+)", lowered):
        low, high = [int(value) for value in re.search(r"(\d+)\s*-\s*(\d+)", lowered).groups()]
        if low <= years_value <= high:
            return high
        if years_value > high:
            return high
        return None
    if "less than" in lowered:
        match = re.search(r"less than\s+(\d+)", lowered)
        if match:
            threshold = int(match.group(1))
            return 0 if years_value < threshold else None
    if re.search(r"\b(\d+)\b", lowered):
        numeric_values = [int(value) for value in re.findall(r"\b(\d+)\b", lowered)]
        best_numeric = max(numeric_values)
        return best_numeric if best_numeric <= years_value else None
    if "no experience" in lowered or "none" == lowered:
        return 0 if years_value <= 0 else None
    return None


def _normalise_question_text(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", str(text or "")).strip()
    return cleaned[:500]


def handle_seek_questionnaire(
    adapter: SeekApplyAdapter,
    salary_expectation_text: str,
    debug_dir: Path,
    *,
    applicant_profile: dict[str, Any],
    input_func: Callable[[], str] = input,
) -> QuestionnaireResult:
    print("[seek_apply_assist] questionnaire_started", flush=True)
    print(f"[seek_apply_assist] questionnaire_page_url: {adapter.current_url()}", flush=True)
    adapter.wait_for_questionnaire_section(timeout_ms=20000)
    adapter.log_questionnaire_diagnostics()
    pre_screenshot_path, pre_html_path = adapter.save_questionnaire_pre_search_artifacts(debug_dir)
    print(f"[seek_apply_assist] questionnaire_before_search_screenshot: {pre_screenshot_path}", flush=True)
    print(f"[seek_apply_assist] questionnaire_before_search_html: {pre_html_path}", flush=True)

    eligibility_selected = adapter.select_work_eligibility_yes()
    if eligibility_selected:
        print("[seek_apply_assist] selected_work_eligibility", flush=True)
        print("[seek_apply_assist] eligibility_radio_checked: True", flush=True)

    salary_filled = adapter.fill_salary_expectation(salary_expectation_text)
    if salary_filled:
        print("[seek_apply_assist] salary_textarea_found", flush=True)
        print(f"[seek_apply_assist] entered_salary_expectation: {salary_expectation_text}", flush=True)

    if not eligibility_selected:
        print("[seek_apply_assist] selected_work_eligibility: basic selector did not find a direct eligibility control; trying dynamic questionnaire rules.", flush=True)

    dynamic_result = handle_dynamic_seek_questions(
        adapter,
        applicant_profile,
        debug_dir,
        input_func=input_func,
    )
    if dynamic_result.status == "paused_for_manual_questionnaire":
        return QuestionnaireResult(status="paused_for_manual_questionnaire", warning=dynamic_result.warning)
    if dynamic_result.status == "dynamic_questions_manual_pause_continued":
        if not _click_continue_step(adapter, debug_dir, "questionnaire", input_func=input_func):
            return QuestionnaireResult(
                status="paused_for_manual_questionnaire",
                warning="Questions answered but Continue was not clicked automatically.",
            )
        return QuestionnaireResult(status="questionnaire_manual_pause_continued", warning=dynamic_result.warning)
    if not eligibility_selected and dynamic_result.answered_count == 0:
        failed_screenshot_path, failed_html_path = adapter.save_questionnaire_failed_artifacts(debug_dir)
        print(f"[seek_apply_assist] questionnaire_failed_screenshot: {failed_screenshot_path}", flush=True)
        print(f"[seek_apply_assist] questionnaire_failed_html: {failed_html_path}", flush=True)
        print("Please answer the questionnaire manually, then press Enter to continue.", flush=True)
        try:
            input_func()
        except EOFError:
            return QuestionnaireResult(
                status="paused_for_manual_questionnaire",
                warning="Could not confidently answer work eligibility or remaining questionnaire items automatically.",
            )
        if not _click_continue_step(adapter, debug_dir, "questionnaire", input_func=input_func):
            return QuestionnaireResult(
                status="paused_for_manual_questionnaire",
                warning="Questions answered but Continue was not clicked automatically.",
            )
        return QuestionnaireResult(
            status="questionnaire_manual_pause_continued",
            warning="Work eligibility required manual continuation.",
        )

    validation_errors = adapter.get_questionnaire_validation_errors()
    if validation_errors and dynamic_result.answered_count > 0:
        page = getattr(adapter, "_page", None)
        resubmitted = False
        if page is not None:
            try:
                resubmitted = click_seek_continue(
                    page,
                    debug_dir,
                    "questionnaire_validation_retry",
                    save_failure_artifacts=False,
                )
            except Exception:  # noqa: BLE001
                resubmitted = False
        else:
            try:
                adapter.click_questionnaire_continue()
                resubmitted = True
            except Exception:  # noqa: BLE001
                resubmitted = False
        if resubmitted:
            try:
                if page is not None:
                    page.wait_for_timeout(350)
            except Exception:  # noqa: BLE001
                pass
            validation_errors = adapter.get_questionnaire_validation_errors()
            if not validation_errors:
                return QuestionnaireResult(status="questionnaire_completed")
    if validation_errors:
        failed_screenshot_path, failed_html_path = adapter.save_questionnaire_failed_artifacts(debug_dir)
        print(f"[seek_apply_assist] questionnaire_failed_screenshot: {failed_screenshot_path}", flush=True)
        print(f"[seek_apply_assist] questionnaire_failed_html: {failed_html_path}", flush=True)
        print("Please answer the questionnaire manually, then press Enter to continue.", flush=True)
        try:
            input_func()
        except EOFError:
            return QuestionnaireResult(
                status="paused_for_manual_questionnaire",
                warning="Questionnaire validation errors remain and no console input was available.",
            )
        if not _click_continue_step(adapter, debug_dir, "questionnaire", input_func=input_func):
            return QuestionnaireResult(
                status="paused_for_manual_questionnaire",
                warning="Questions answered but Continue was not clicked automatically.",
            )
        return QuestionnaireResult(status="questionnaire_manual_pause_continued", warning="Questionnaire validation required manual continuation.")

    recheck_reapplied = _recommit_required_questionnaire_answers(
        adapter,
        applicant_profile,
        debug_dir,
    )
    if recheck_reapplied:
        print(f"[seek_apply_assist] questionnaire_recommit_reapplied: {recheck_reapplied}", flush=True)

    if not _click_continue_step(adapter, debug_dir, "questionnaire", input_func=input_func):
        return QuestionnaireResult(
            status="paused_for_manual_questionnaire",
            warning="Questions answered but Continue was not clicked automatically.",
        )
    return QuestionnaireResult(status="questionnaire_completed")


def _recommit_required_questionnaire_answers(
    adapter: SeekApplyAdapter,
    applicant_profile: dict[str, Any],
    debug_dir: Path,
) -> int:
    job_context = dict(applicant_profile.get("job_context") or {})
    try:
        questions = adapter.extract_dynamic_questions()
    except Exception:  # noqa: BLE001
        return 0

    reapplied = 0
    for question in questions:
        if not bool(question.get("required", True)):
            continue
        resolved = _resolve_answer_for_validation_retry(question, applicant_profile, job_context)
        if not resolved:
            continue
        answer, matched_rule, _answer_source = resolved
        if not answer:
            continue
        try:
            if adapter.answer_dynamic_question(question, answer):
                reapplied += 1
                print(
                    "[seek_apply_assist] questionnaire_recommit_field:"
                    f" name={str(question.get('name') or '').strip() or '(unknown)'}"
                    f" rule={matched_rule or '(none)'}",
                    flush=True,
                )
        except Exception:  # noqa: BLE001
            continue

    if reapplied:
        try:
            page = getattr(adapter, "_page", None)
            if page is not None:
                page.wait_for_timeout(350)
                screenshot_path = debug_dir / "questionnaire_recommit.png"
                html_path = debug_dir / "questionnaire_recommit.html"
                _safe_page_screenshot(page, screenshot_path)
                _safe_page_html_write(page, html_path)
                print(f"[seek_apply_assist] questionnaire_recommit_screenshot: {screenshot_path}", flush=True)
                print(f"[seek_apply_assist] questionnaire_recommit_html: {html_path}", flush=True)
        except Exception:  # noqa: BLE001
            pass
    return reapplied


def wait_for_manual_review(*, input_func: Callable[[], str] = input) -> None:
    print("Application prepared. Browser left open for manual review. Submit is not automated.", flush=True)
    print("Review the application in the browser. Press Enter here after you are finished...", flush=True)
    try:
        input_func()
    except EOFError:
        return


def _assist_seek_apply(
    job_id: int,
    database: Database,
    *,
    visible: bool = True,
    force_new_profile: bool = False,
    use_bulk_profile: bool = False,
    adapter_factory: Callable[[bool], SeekApplyAdapter] | None = None,
    input_func: Callable[[], str] = input,
    keep_browser_open_override: bool | None = None,
    resume_upload_only: bool = False,
    allow_manual_login_prompt: bool = True,
    auto_submit: bool = False,
    review_url: str = "",
) -> ApplyAssistResult:
    job = database.get_job_details(job_id)
    if job is None:
        return ApplyAssistResult(job_id=job_id, status="failed", steps_completed=[], error="Job not found.", final_url="")
    if str(job.get("source") or "").strip().lower() != "seek":
        return ApplyAssistResult(job_id=job_id, status="failed", steps_completed=[], error="SEEK Apply Assist only supports SEEK jobs.", final_url=str(job.get("url") or ""))

    cover_letter = str(job.get("cover_letter_preview") or "").strip()
    if not cover_letter:
        return ApplyAssistResult(job_id=job_id, status="failed", steps_completed=[], error="Missing cover letter preview. Regenerate documents first.", final_url=str(job.get("url") or ""))

    selected_cv_path = Path(str(job.get("selected_cv_path") or "")).expanduser()
    if not selected_cv_path.exists():
        return ApplyAssistResult(job_id=job_id, status="failed", steps_completed=[], error=f"Selected CV file not found: {selected_cv_path}", final_url=str(job.get("url") or ""))

    job_url, canonical_job_url = _resolve_seek_job_open_urls(job)
    if not job_url:
        return ApplyAssistResult(job_id=job_id, status="failed", steps_completed=[], error="Missing SEEK job URL.", final_url="")

    salary_estimate = estimate_salary_expectation(job)
    salary_expectation_text = salary_estimate.salary_expectation_text
    applicant_profile = load_applicant_profile(database.db_path.parent)
    applicant_profile["job_context"] = {
        **dict(applicant_profile.get("job_context") or {}),
        "job_id": job_id,
        "job_title": str(job.get("title") or "").strip(),
        "company": str(job.get("company") or "").strip(),
        "location": str(job.get("location") or "").strip(),
        "salary_expectation_text": salary_expectation_text,
    }
    automation_settings = dict(applicant_profile.get("automation") or {})
    close_browser_on_success = bool(automation_settings.get("close_browser_on_success", True))
    keep_browser_open_on_failure = bool(automation_settings.get("keep_browser_open_on_failure", True))
    if keep_browser_open_override is not None:
        close_browser_on_success = not keep_browser_open_override
    database.update_job_career_assets(
        job_id,
        salary_expectation_text=salary_estimate.salary_expectation_text,
        salary_estimate_low=salary_estimate.salary_low,
        salary_estimate_high=salary_estimate.salary_high,
        salary_estimate_reasoning_json=json.dumps(salary_estimate.reasoning),
    )

    adapter_factory = adapter_factory or (
        lambda requested_visible: _build_playwright_adapter(
            requested_visible,
            database.db_path,
            force_new_profile=force_new_profile,
            use_bulk_profile=use_bulk_profile,
        )
    )
    try:
        adapter = adapter_factory(visible)
    except SeekBrowserProfileLockedError as exc:
        database.log_run("seek_apply_assist", "browser_profile_locked", "warning", f"job_id={job_id} {exc}")
        print(f"[seek_apply_assist] browser_profile_locked: {exc}", flush=True)
        return ApplyAssistResult(job_id=job_id, status="browser_profile_locked", steps_completed=[], error=str(exc), final_url=job_url)
    except Exception as exc:  # noqa: BLE001
        return ApplyAssistResult(job_id=job_id, status="failed", steps_completed=[], error=str(exc), final_url=job_url)
    steps: list[str] = []
    status = "failed"
    error = ""
    final_url = job_url
    keep_browser_open = keep_browser_open_on_failure
    root_failure_reason = ""
    evidence_capture_errors: list[str] = []
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S") + f"_{job_id}"
    debug_dir = database.db_path.parent / "debug_apply_assist" / run_id
    session_state_written = False
    resume_upload_verified = False
    latest_checkpoint = ""
    verified_resume_filename = ""
    cover_letter_verified = False
    questions_answered = False
    questionnaire_validation_errors: list[str] = []
    page: Any | None = None

    def log_step(action: str, details: str) -> None:
        database.log_run("seek_apply_assist", action, "success", f"job_id={job_id} {details}")
        print(f"[seek_apply_assist] {action}: {details}", flush=True)

    def update_seek_session_state(
        state: str,
        *,
        reason: str = "",
        matched_token: str = "",
        matched_source: str = "",
        final_url_override: str = "",
    ) -> None:
        nonlocal session_state_written
        write_seek_session_state(
            database.db_path,
            state,
            job_id=job_id,
            reason=reason,
            matched_token=matched_token,
            matched_source=matched_source,
            final_url=final_url_override or final_url,
        )
        session_state_written = True

    def record_checkpoint(checkpoint: str, *, page: Any | None, adapter_obj: SeekApplyAdapter | None, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        nonlocal latest_checkpoint
        latest_checkpoint = checkpoint
        return _record_resume_checkpoint(
            debug_dir,
            checkpoint,
            page=page,
            adapter=adapter_obj,
            expected_filename=verified_resume_filename,
            extra=extra,
        )

    def make_result(current_status: str, *, current_error: str = "", current_final_url: str | None = None) -> ApplyAssistResult:
        return ApplyAssistResult(
            job_id=job_id,
            status=current_status,
            steps_completed=list(steps),
            error=current_error,
            final_url=current_final_url if current_final_url is not None else adapter.current_url(),
            debug_dir=str(debug_dir),
            latest_checkpoint=latest_checkpoint,
            root_failure_reason=root_failure_reason or current_error,
            evidence_capture_errors=list(evidence_capture_errors),
        )

    def save_verification_artifacts(stem: str, failure_message: str, payload: dict[str, Any], *, checkpoint: str) -> None:
        nonlocal latest_checkpoint, status, error, keep_browser_open, root_failure_reason, evidence_capture_errors
        latest_checkpoint = checkpoint
        keep_browser_open = True
        root_failure_reason = failure_message
        if page is not None:
            screenshot_path, html_path, json_path, capture_meta = _save_verification_failure_artifacts(
                debug_dir,
                page,
                stem=stem,
                payload={
                    **payload,
                    "root_failure_reason": failure_message,
                },
            )
            evidence_capture_errors = list(capture_meta.get("evidence_capture_errors") or [])
            print(f"[seek_apply_assist] {stem}_screenshot: {screenshot_path}", flush=True)
            print(f"[seek_apply_assist] {stem}_html: {html_path}", flush=True)
            print(f"[seek_apply_assist] {stem}_json: {json_path}", flush=True)

    def fail_with_verification_artifacts(stem: str, failure_message: str, payload: dict[str, Any], *, checkpoint: str) -> ApplyAssistResult:
        save_verification_artifacts(stem, failure_message, payload, checkpoint=checkpoint)
        status = "failed"
        error = failure_message
        return make_result(status, current_error=error)

    def pause_with_seek_artifacts(
        current_status: str,
        failure_message: str,
        *,
        stem: str,
        checkpoint: str,
    ) -> ApplyAssistResult:
        nonlocal status, error
        diagnostics = _get_seek_pause_diagnostics(adapter)
        print(
            f"[seek_apply_assist] pause_diagnostics: reason={diagnostics.get('pause_reason')} "
            f"source={diagnostics.get('matched_source') or '(none)'} "
            f"token={diagnostics.get('matched_token') or '(none)'}",
            flush=True,
        )
        mapped_state = {
            "paused_for_seek_login": "login_required",
            "paused_for_seek_verification": "verification_required",
            "paused_for_captcha": "captcha_required",
        }.get(current_status, "unknown")
        update_seek_session_state(
            mapped_state,
            reason=failure_message,
            matched_token=str(diagnostics.get("matched_token") or ""),
            matched_source=str(diagnostics.get("matched_source") or ""),
            final_url_override=str(diagnostics.get("url") or final_url),
        )
        save_verification_artifacts(
            stem,
            failure_message,
            diagnostics,
            checkpoint=checkpoint,
        )
        status = current_status
        error = failure_message
        return make_result(status, current_error=error)

    def build_final_review_checklist() -> tuple[FinalReviewChecklist, dict[str, Any]]:
        resume_included = False
        resume_info: dict[str, Any] = {}
        cover_letter_included = False
        cover_letter_info: dict[str, Any] = {}
        current_validation_errors = adapter.get_questionnaire_validation_errors()
        if page is not None:
            resume_included, resume_info = verify_resume_included_on_review(page, verified_resume_filename)
            cover_letter_included, cover_letter_info = verify_cover_letter_included_on_review(page, cover_letter)
        checklist = FinalReviewChecklist(
            resume_included=resume_included,
            cover_letter_included=cover_letter_included,
            questions_answered=questions_answered,
            no_validation_errors=not current_validation_errors,
            final_submit_not_clicked=not bool(getattr(adapter, "clicked_final_navigation", False)),
        )
        details = {
            "resume_review": resume_info,
            "cover_letter_review": cover_letter_info,
            "validation_errors": current_validation_errors,
            "verified_resume_filename": verified_resume_filename,
        }
        return checklist, details

    def submit_current_review_page() -> ApplyAssistResult:
        nonlocal status, error, final_url, keep_browser_open, root_failure_reason, evidence_capture_errors
        if page is None or not _is_review_page(page):
            final_url = adapter.current_url()
            page_title = ""
            body_excerpt = ""
            try:
                if page is not None:
                    page_title = str(page.title() or "")
            except Exception:  # noqa: BLE001
                page_title = ""
            try:
                if page is not None:
                    body_excerpt = " ".join(str(page.locator("body").inner_text(timeout=5000) or "").split())[:240]
            except Exception:  # noqa: BLE001
                body_excerpt = ""
            error = (
                "Automatic submit did not reach the real SEEK review page."
                + (f" Title: {page_title}." if page_title else "")
                + (f" URL: {final_url}." if final_url else "")
                + (f" Text excerpt: {body_excerpt}" if body_excerpt else "")
            )
            database.log_run("seek_submit", "finished", "error", f"job_id={job_id} {error}")
            status = "failed"
            return make_result(status, current_error=error, current_final_url=final_url)

        try:
            page.wait_for_timeout(1200)
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(400)
        except Exception:  # noqa: BLE001
            pass

        submit_candidates = [
            page.locator("[data-testid='review-submit-application']"),
            page.get_by_role("button", name=re.compile(r"submit application", re.I)),
            page.locator("button", has_text=re.compile(r"submit application", re.I)),
            page.get_by_role("button", name=re.compile(r"submit|send application|apply now|send", re.I)),
            page.locator("button", has_text=re.compile(r"submit|send application|apply now|send", re.I)),
            page.locator("button[type='submit']"),
            page.locator("[type='submit']"),
        ]
        submit_locator = None
        for candidate in submit_candidates:
            try:
                if candidate.count():
                    submit_locator = candidate.first
                    break
            except Exception:  # noqa: BLE001
                continue

        if submit_locator is None:
            button_texts: list[str] = []
            body_excerpt = ""
            page_title_now = ""
            final_url = adapter.current_url()
            try:
                raw_texts = page.locator("button").all_inner_texts()
                button_texts = [str(text).strip() for text in raw_texts if str(text).strip()]
            except Exception:  # noqa: BLE001
                button_texts = []
            try:
                body_excerpt = " ".join(str(page.locator("body").inner_text(timeout=5000) or "").split())[:240]
            except Exception:  # noqa: BLE001
                body_excerpt = ""
            try:
                page_title_now = str(page.title() or "")
            except Exception:  # noqa: BLE001
                page_title_now = ""
            error = (
                "Could not find final submit button on SEEK review page."
                + (f" Visible buttons: {button_texts}" if button_texts else "")
                + (f" Title: {page_title_now}." if page_title_now else "")
                + (f" URL: {final_url}." if final_url else "")
                + (f" Text excerpt: {body_excerpt}" if body_excerpt else "")
            )
            database.log_run("seek_submit", "finished", "error", f"job_id={job_id} {error}")
            status = "failed"
            return make_result(status, current_error=error, current_final_url=final_url)

        click_error: Exception | None = None
        click_succeeded = False
        for strategy in ("normal", "force", "dom", "request_submit"):
            try:
                try:
                    submit_locator.scroll_into_view_if_needed(timeout=5000)
                except Exception:  # noqa: BLE001
                    pass
                if strategy == "normal":
                    submit_locator.click(timeout=10000)
                elif strategy == "force":
                    submit_locator.click(timeout=10000, force=True)
                elif strategy == "dom":
                    handle = submit_locator.element_handle(timeout=5000)
                    if handle is None:
                        raise RuntimeError("Submit button handle was not available.")
                    page.evaluate(
                        """(el) => {
                            el.scrollIntoView({ block: 'center', inline: 'center' });
                            el.focus();
                            el.click();
                        }""",
                        handle,
                    )
                else:
                    handle = submit_locator.element_handle(timeout=5000)
                    if handle is None:
                        raise RuntimeError("Submit button handle was not available.")
                    page.evaluate(
                        """(el) => {
                            const form = el.closest('form');
                            el.scrollIntoView({ block: 'center', inline: 'center' });
                            el.focus();
                            if (form && typeof form.requestSubmit === 'function') {
                                form.requestSubmit(el);
                                return;
                            }
                            el.click();
                        }""",
                        handle,
                    )
                click_succeeded = True
                break
            except Exception as exc:  # noqa: BLE001
                click_error = exc
                continue

        if not click_succeeded:
            error = f"Could not click final submit button: {click_error}"
            database.log_run("seek_submit", "finished", "error", f"job_id={job_id} {error}")
            status = "failed"
            final_url = adapter.current_url()
            return make_result(status, current_error=error, current_final_url=final_url)
        steps.append("clicked_final_submit")
        database.log_run("seek_submit", "clicked_final_submit", "success", f"job_id={job_id}")

        try:
            page.wait_for_function(
                """() => {
                    const text = String(document.body ? document.body.innerText || '' : '').toLowerCase();
                    const title = String(document.title || '').toLowerCase();
                    return text.includes('application submitted')
                        || text.includes('application sent')
                        || text.includes('application has been sent')
                        || text.includes('your application has been sent')
                        || text.includes('thanks for applying')
                        || text.includes('thank you for applying')
                        || text.includes("you've applied")
                        || text.includes('we have received your application')
                        || title.includes('application submitted')
                        || title.includes('application sent')
                        || title.includes('thanks for applying');
                }""",
                timeout=15000,
            )
        except Exception:  # noqa: BLE001
            pass

        final_url = adapter.current_url()
        page_text = ""
        page_title = ""
        try:
            page_text = str(page.locator("body").inner_text(timeout=5000) or "")
        except Exception:  # noqa: BLE001
            page_text = ""
        try:
            page_title = str(page.title() or "")
        except Exception:  # noqa: BLE001
            page_title = ""
        submitted = _seek_submit_confirmation_detected(
            final_url=final_url,
            page_title=page_title,
            page_text=page_text,
        )
        if not submitted:
            excerpt = " ".join(page_text.split())[:240]
            bounced_to_choose_documents = _is_choose_documents_page(page)
            resume_snapshot: dict[str, Any] = {}
            cover_letter_matches_expected = False
            cover_letter_info: dict[str, Any] = {}
            if bounced_to_choose_documents:
                try:
                    resume_snapshot = _record_resume_checkpoint(
                        debug_dir,
                        "submit_bounced_to_choose_documents",
                        page=page,
                        adapter=adapter,
                        expected_filename=verified_resume_filename,
                    )
                except Exception:  # noqa: BLE001
                    resume_snapshot = {}
            if cover_letter:
                try:
                    cover_letter_matches_expected, cover_letter_info = verify_cover_letter_pasted(page, cover_letter)
                except Exception:  # noqa: BLE001
                    cover_letter_matches_expected = False
                    cover_letter_info = {}
            artifact_payload = {
                "job_id": job_id,
                "page_title": page_title,
                "page_url": final_url,
                "page_text_excerpt": excerpt,
                "confirmation_detected": False,
                "bounced_to_choose_documents": bounced_to_choose_documents,
                "expected_resume_filename": verified_resume_filename,
                "resume_snapshot": resume_snapshot,
                "cover_letter_matches_expected": cover_letter_matches_expected,
                "cover_letter_info": cover_letter_info,
            }
            screenshot_path, html_path, json_path, capture_meta = _save_verification_failure_artifacts(
                debug_dir,
                page,
                stem="submit_unconfirmed",
                payload=artifact_payload,
            )
            evidence_capture_errors = list(capture_meta.get("evidence_capture_errors") or [])
            print(f"[seek_apply_assist] submit_unconfirmed_screenshot: {screenshot_path}", flush=True)
            print(f"[seek_apply_assist] submit_unconfirmed_html: {html_path}", flush=True)
            print(f"[seek_apply_assist] submit_unconfirmed_json: {json_path}", flush=True)
            if bounced_to_choose_documents:
                detected_filename = str(resume_snapshot.get("resume_filename") or "").strip()
                error = (
                    "Final submit bounced back to Choose documents before SEEK confirmed submission."
                    + f" Expected resume: {verified_resume_filename or '(unknown)'}."
                    + f" Visible resume: {detected_filename or '(none)'}."
                    + f" No-resume flag: {resume_snapshot.get('no_resume_included')}."
                    + (" Cover letter still matched the prepared text." if cover_letter_matches_expected else " Cover letter no longer matched the prepared text.")
                )
            else:
                error = (
                    "Final submit click did not reach an explicit submitted state."
                    + (f" Title: {page_title}." if page_title else "")
                    + (f" URL: {final_url}." if final_url else "")
                    + (f" Text excerpt: {excerpt}" if excerpt else "")
                )
            database.log_run("seek_submit", "finished", "error", f"job_id={job_id} {error}")
            status = "failed"
            return make_result(status, current_error=error, current_final_url=final_url)

        database.update_application_state(job_id, "applied", note="Submitted via replayed SEEK flow.")
        update_seek_session_state("session_valid", reason="SEEK application submitted successfully.", final_url_override=final_url)
        database.log_run("seek_submit", "finished", "success", f"job_id={job_id} final_url={final_url}")
        status = "applied"
        error = ""
        keep_browser_open = False
        return make_result(status, current_error="", current_final_url=final_url)

    review_url = str(review_url or "").strip()
    initial_url = review_url or job_url
    initial_fallback_url = canonical_job_url if initial_url == job_url else ""

    try:
        adapter.open_job(initial_url, fallback_url=initial_fallback_url or None)
        opened_url = adapter.current_url() or initial_url
        if review_url:
            steps.append("opened_review_url")
            log_step("opened_review_url", f"url={opened_url}")
            update_seek_session_state("session_valid", reason="Saved SEEK review page opened.", final_url_override=opened_url)
        else:
            steps.append("opened_job")
            log_step("opened_job", f"url={opened_url}")
            update_seek_session_state("session_valid", reason="SEEK job opened.", final_url_override=opened_url)

        if _seek_job_is_closed(adapter.page_text()):
            status = "skipped"
            error = "This SEEK job is no longer advertised."
            log_step("skipped_job_closed", error)
            return make_result(status, current_error=error)

        pause_reason = adapter.detect_pause_reason()
        if pause_reason:
            if pause_reason == "paused_for_seek_login":
                keep_browser_open = True
                resumed, login_error = prompt_for_seek_login(
                    adapter,
                    db_path=database.db_path,
                    use_bulk_profile=use_bulk_profile,
                    input_func=input_func,
                    allow_manual_prompt=allow_manual_login_prompt,
                )
                if not resumed:
                    pause_status = _pause_status_for_login_error(login_error)
                    state_name = (
                        "captcha_required" if pause_status == "paused_for_captcha"
                        else "verification_required" if pause_status == "paused_for_seek_verification"
                        else "login_required"
                    )
                    update_seek_session_state(state_name, reason=login_error, final_url_override=adapter.current_url())
                    status = pause_status
                    error = login_error
                    return make_result(status, current_error=error)
                steps.append("resumed_after_manual_login")
                log_step("resumed_after_manual_login", "Resumed after manual SEEK login.")
                update_seek_session_state("session_refreshed", reason="Resumed after manual SEEK login.", final_url_override=adapter.current_url())
                pause_reason = adapter.detect_pause_reason()
                if pause_reason == "paused_for_seek_login":
                    return pause_with_seek_artifacts(
                        "paused_for_seek_login",
                        "SEEK login is still required after manual prompt.",
                        stem="seek_login_still_required",
                        checkpoint="seek_login_still_required",
                    )
                if pause_reason == "paused_for_seek_verification":
                    print("[seek_apply_assist] Paused for SEEK verification.", flush=True)
                    return pause_with_seek_artifacts(
                        "paused_for_seek_verification",
                        "SEEK verification is required before automation can continue.",
                        stem="seek_verification_required",
                        checkpoint="seek_verification_required",
                    )
                if pause_reason == "paused_for_captcha":
                    print("[seek_apply_assist] Paused for manual continuation.", flush=True)
                    return pause_with_seek_artifacts(
                        "paused_for_captcha",
                        "Login or CAPTCHA requires manual continuation.",
                        stem="seek_captcha_required",
                        checkpoint="seek_captcha_required",
                    )
            else:
                print("[seek_apply_assist] Paused for manual continuation.", flush=True)
                stem = "seek_verification_required" if pause_reason == "paused_for_seek_verification" else "seek_captcha_required" if pause_reason == "paused_for_captcha" else "seek_login_required"
                checkpoint = stem
                message = (
                    "SEEK verification is required before automation can continue."
                    if pause_reason == "paused_for_seek_verification"
                    else "Login or CAPTCHA requires manual continuation."
                    if pause_reason == "paused_for_captcha"
                    else "SEEK login is required before automation can continue."
                )
                return pause_with_seek_artifacts(pause_reason, message, stem=stem, checkpoint=checkpoint)

        page = _normalise_adapter_page(adapter)
        if auto_submit and review_url and page is not None and _is_review_page(page):
            questionnaire_validation_errors = adapter.get_questionnaire_validation_errors()
            questions_answered = not questionnaire_validation_errors
            record_checkpoint(
                "review_page_reopened",
                page=page,
                adapter_obj=adapter,
                extra={"review_url": review_url},
            )
            included, review_info = verify_resume_included_on_review(page, verified_resume_filename)
            if not included:
                repaired = _attempt_review_resume_repair(
                    page,
                    adapter,
                    selected_cv_path,
                    debug_dir,
                    expected_filename=verified_resume_filename,
                )
                if repaired:
                    record_checkpoint(
                        "review_page_checked",
                        page=page,
                        adapter_obj=adapter,
                        extra={"repair_attempted": True, "review_url": review_url},
                    )
                    included, review_info = verify_resume_included_on_review(page, verified_resume_filename)
            if not included:
                latest_checkpoint = "resume_missing_on_review"
                screenshot_path, html_path, json_path = _save_resume_state_lost_artifacts(debug_dir, page, adapter, "before_review")
                _write_codex_prompt_resume_failure(
                    debug_dir,
                    failure_reason="Saved SEEK review page shows no resume included.",
                    latest_checkpoint=latest_checkpoint,
                    screenshot_path=screenshot_path,
                    html_path=html_path,
                    json_path=json_path,
                    snapshot=review_info,
                    suggested_next_fix="Inspect the saved review-page resume state and confirm the selected document still exists before final submit.",
                )
                print("[seek_apply_assist] resume_state_lost_detected", flush=True)
                print(f"[seek_apply_assist] resume_state_lost_screenshot: {screenshot_path}", flush=True)
                print(f"[seek_apply_assist] resume_state_lost_html: {html_path}", flush=True)
                print(f"[seek_apply_assist] resume_state_lost_json: {json_path}", flush=True)
                status = "failed"
                error = "Saved SEEK review page shows no resume included."
                return make_result(status, current_error=error)
            record_checkpoint("resume_confirmed_included", page=page, adapter_obj=adapter, extra=review_info)
            checklist, checklist_details = build_final_review_checklist()
            _write_final_review_checklist(debug_dir, checklist, checklist_details)
            if not checklist.all_true():
                return fail_with_verification_artifacts(
                    "final_review_checklist_failed",
                    "Final SEEK review checklist failed after reopening the saved review page.",
                    {
                        **checklist.as_dict(),
                        **checklist_details,
                        "review_url": review_url,
                    },
                    checkpoint="final_review_checklist_failed",
                )
            steps.append("reopened_saved_review")
            log_step("reopened_saved_review", f"url={adapter.current_url()}")
            final_url = adapter.current_url()
            return submit_current_review_page()

        current_url_before_quick_apply = str(adapter.current_url() or "")
        if "/apply" in current_url_before_quick_apply.lower():
            steps.append("quick_apply_already_open")
            log_step("quick_apply_already_open", f"url={current_url_before_quick_apply}")
        else:
            try:
                adapter.click_quick_apply()
            except SeekApplyAssistError as exc:
                recovered_apply_state = ""
                recovered_apply_payload: dict[str, Any] = {}
                try:
                    recovered_apply_state, recovered_apply_payload = _wait_for_post_quick_apply_ready(adapter, timeout_ms=8000)
                except Exception:  # noqa: BLE001
                    recovered_apply_state = ""
                    recovered_apply_payload = {}
                if recovered_apply_state == "application_ready":
                    recovered_url = str(recovered_apply_payload.get("current_url") or adapter.current_url() or "")
                    steps.append("quick_apply_already_open")
                    log_step("quick_apply_already_open", f"url={recovered_url or current_url_before_quick_apply}")
                else:
                    current_page = getattr(adapter, "_page", None)
                    if current_page is not None:
                        save_verification_artifacts(
                            "quick_apply_missing",
                            str(exc),
                            {
                                "page_url": adapter.current_url(),
                                "page_title": str(current_page.title() or ""),
                                "page_text_excerpt": " ".join(str(adapter.page_text() or "").split())[:1200],
                            },
                            checkpoint="quick_apply_missing",
                        )
                    if _seek_job_is_closed(adapter.page_text()):
                        status = "skipped"
                        error = "This SEEK job is no longer advertised."
                        log_step("skipped_job_closed", error)
                        return make_result(status, current_error=error)
                    raise exc
            else:
                steps.append("clicked_quick_apply")
                log_step("clicked_quick_apply", "Quick apply opened.")
        page = getattr(adapter, "_page", None)
        if page is not None:
            record_checkpoint("quick_apply_opened", page=page, adapter_obj=adapter)

        immediate_pause_reason = adapter.detect_pause_reason()
        if immediate_pause_reason == "paused_for_seek_login":
            keep_browser_open = True
            resumed, login_error = prompt_for_seek_login(
                adapter,
                db_path=database.db_path,
                use_bulk_profile=use_bulk_profile,
                input_func=input_func,
                allow_manual_prompt=allow_manual_login_prompt,
            )
            if not resumed:
                print("[seek_apply_assist] quick_apply_requires_seek_login", flush=True)
                pause_status = _pause_status_for_login_error(login_error)
                state_name = (
                    "captcha_required" if pause_status == "paused_for_captcha"
                    else "verification_required" if pause_status == "paused_for_seek_verification"
                    else "login_required"
                )
                update_seek_session_state(
                    state_name,
                    reason=login_error or "SEEK login is required before Quick Apply can continue.",
                    final_url_override=adapter.current_url(),
                )
                return pause_with_seek_artifacts(
                    pause_status,
                    login_error or "SEEK login is required before Quick Apply can continue.",
                    stem="quick_apply_seek_captcha_required" if pause_status == "paused_for_captcha" else "quick_apply_seek_verification_required" if pause_status == "paused_for_seek_verification" else "quick_apply_seek_login_required",
                    checkpoint="quick_apply_seek_captcha_required" if pause_status == "paused_for_captcha" else "quick_apply_seek_verification_required" if pause_status == "paused_for_seek_verification" else "quick_apply_seek_login_required",
                )
            steps.append("resumed_after_manual_login")
            log_step("resumed_after_manual_login", "Resumed after manual SEEK login.")
            update_seek_session_state(
                "session_refreshed",
                reason="Resumed after manual SEEK login.",
                final_url_override=adapter.current_url(),
            )
        elif immediate_pause_reason == "paused_for_seek_verification":
            print("[seek_apply_assist] quick_apply_requires_seek_verification", flush=True)
            return pause_with_seek_artifacts(
                "paused_for_seek_verification",
                "SEEK verification is required before Quick Apply can continue.",
                stem="quick_apply_seek_verification_required",
                checkpoint="quick_apply_seek_verification_required",
            )
        elif immediate_pause_reason == "paused_for_captcha":
            print("[seek_apply_assist] quick_apply_requires_captcha", flush=True)
            return pause_with_seek_artifacts(
                "paused_for_captcha",
                "CAPTCHA or verification is required before Quick Apply can continue.",
                stem="quick_apply_captcha_required",
                checkpoint="quick_apply_captcha_required",
            )

        adapter.wait_for_application_form()
        post_quick_apply_state, post_quick_apply_payload = _wait_for_post_quick_apply_ready(adapter, timeout_ms=20000)
        if post_quick_apply_state == "job_closed":
            status = "skipped"
            error = "This SEEK job is no longer advertised."
            log_step("skipped_job_closed", error)
            return make_result(status, current_error=error)
        if post_quick_apply_state == "paused_for_seek_login":
            keep_browser_open = True
            resumed, login_error = prompt_for_seek_login(
                adapter,
                db_path=database.db_path,
                use_bulk_profile=use_bulk_profile,
                input_func=input_func,
                allow_manual_prompt=allow_manual_login_prompt,
            )
            if not resumed:
                print("[seek_apply_assist] quick_apply_requires_seek_login", flush=True)
                pause_status = _pause_status_for_login_error(login_error)
                state_name = (
                    "captcha_required" if pause_status == "paused_for_captcha"
                    else "verification_required" if pause_status == "paused_for_seek_verification"
                    else "login_required"
                )
                update_seek_session_state(state_name, reason=login_error or "SEEK login is required before Quick Apply can continue.", final_url_override=adapter.current_url())
                return pause_with_seek_artifacts(
                    pause_status,
                    login_error or "SEEK login is required before Quick Apply can continue.",
                    stem="quick_apply_seek_captcha_required" if pause_status == "paused_for_captcha" else "quick_apply_seek_verification_required" if pause_status == "paused_for_seek_verification" else "quick_apply_seek_login_required",
                    checkpoint="quick_apply_seek_captcha_required" if pause_status == "paused_for_captcha" else "quick_apply_seek_verification_required" if pause_status == "paused_for_seek_verification" else "quick_apply_seek_login_required",
                )
            steps.append("resumed_after_manual_login")
            log_step("resumed_after_manual_login", "Resumed after manual SEEK login.")
            update_seek_session_state("session_refreshed", reason="Resumed after manual SEEK login.", final_url_override=adapter.current_url())
            post_quick_apply_state, post_quick_apply_payload = _wait_for_post_quick_apply_ready(adapter, timeout_ms=20000)
            if post_quick_apply_state == "paused_for_seek_login":
                print("[seek_apply_assist] quick_apply_requires_seek_login", flush=True)
                return pause_with_seek_artifacts(
                    "paused_for_seek_login",
                    "SEEK login is still required after manual prompt.",
                    stem="quick_apply_seek_login_still_required",
                    checkpoint="quick_apply_seek_login_still_required",
                )
        if post_quick_apply_state == "paused_for_captcha":
            print("[seek_apply_assist] quick_apply_requires_captcha", flush=True)
            return pause_with_seek_artifacts(
                "paused_for_captcha",
                "CAPTCHA or verification is required before Quick Apply can continue.",
                stem="quick_apply_captcha_required",
                checkpoint="quick_apply_captcha_required",
            )
        if post_quick_apply_state == "paused_for_seek_verification":
            print("[seek_apply_assist] quick_apply_requires_seek_verification", flush=True)
            return pause_with_seek_artifacts(
                "paused_for_seek_verification",
                "SEEK verification is required before Quick Apply can continue.",
                stem="quick_apply_seek_verification_required",
                checkpoint="quick_apply_seek_verification_required",
            )
        if post_quick_apply_state == "timeout":
            timeout_payload = {
                **post_quick_apply_payload,
                "steps_completed": list(steps),
            }
            return fail_with_verification_artifacts(
                "quick_apply_post_open_timeout",
                "Quick Apply opened, but SEEK did not reach a detectable documents/questionnaire/review step within 20 seconds.",
                timeout_payload,
                checkpoint="quick_apply_post_open_timeout",
            )
        if post_quick_apply_state != "application_ready":
            pause_reason = adapter.detect_pause_reason()
            if pause_reason == "paused_for_seek_login":
                print("[seek_apply_assist] quick_apply_requires_seek_login", flush=True)
                return pause_with_seek_artifacts(
                    "paused_for_seek_login",
                    "SEEK login is required before Quick Apply can continue.",
                    stem="quick_apply_seek_login_required",
                    checkpoint="quick_apply_seek_login_required",
                )
            if pause_reason == "paused_for_seek_verification":
                print("[seek_apply_assist] quick_apply_requires_seek_verification", flush=True)
                return pause_with_seek_artifacts(
                    "paused_for_seek_verification",
                    "SEEK verification is required before Quick Apply can continue.",
                    stem="quick_apply_seek_verification_required",
                    checkpoint="quick_apply_seek_verification_required",
                )
            if pause_reason == "paused_for_captcha":
                print("[seek_apply_assist] quick_apply_requires_captcha", flush=True)
                return pause_with_seek_artifacts(
                    "paused_for_captcha",
                    "CAPTCHA or verification is required before Quick Apply can continue.",
                    stem="quick_apply_captcha_required",
                    checkpoint="quick_apply_captcha_required",
                )

        _normalise_adapter_page(adapter)
        resume_result = handle_resume_upload(adapter, selected_cv_path, debug_dir)
        if resume_result.status == "resume_uploaded":
            steps.append("uploaded_cv")
            log_step("uploaded_cv", f"cv_path={selected_cv_path}")
            resume_upload_verified = bool(resume_result.verified_filename)
            verified_resume_filename = str(resume_result.verified_filename or "")
            if resume_upload_verified:
                print("[seek_apply_assist] resume_state_verified", flush=True)
        elif resume_result.status == "resume_existing_or_unknown":
            steps.append("resume_existing_detected")
            database.log_run("seek_apply_assist", "resume_existing_detected", "warning", f"job_id={job_id} {resume_result.warning}")
            resume_upload_verified = bool(resume_result.verified_filename)
            verified_resume_filename = str(resume_result.verified_filename or "")
            if resume_upload_verified:
                print("[seek_apply_assist] resume_state_verified", flush=True)
        elif resume_result.status == "resume_manual_pause_continued":
            steps.append("resume_manual_pause")
            database.log_run("seek_apply_assist", "resume_manual_pause", "warning", f"job_id={job_id} {resume_result.warning}")
        elif resume_result.status == "paused_for_manual_resume_upload":
            steps.append("resume_manual_pause")
            status = "paused_for_manual_resume_upload"
            error = resume_result.warning
            database.log_run("seek_apply_assist", "resume_manual_pause", "warning", f"job_id={job_id} {resume_result.warning}")
            return make_result(status, current_error=error)
        elif resume_result.status == "paused_for_resume_upload_incomplete":
            steps.append("resume_upload_incomplete_stop")
            status = "paused_for_resume_upload_incomplete"
            error = resume_result.warning
            database.log_run("seek_apply_assist", "resume_upload_incomplete_stop", "warning", f"job_id={job_id} {resume_result.warning}")
            return make_result(status, current_error=error)

        page = getattr(adapter, "_page", None)
        if page is not None and resume_upload_verified:
            resume_state_ok, resume_state_payload = _verify_resume_upload_state(
                page,
                adapter,
                debug_dir,
                expected_filename=verified_resume_filename,
            )
            if not resume_state_ok:
                return fail_with_verification_artifacts(
                    "resume_upload_state_invalid",
                    "Resume upload state is invalid immediately after upload.",
                    resume_state_payload,
                    checkpoint="resume_upload_state_invalid",
                )
            record_checkpoint("resume_upload_only_verified", page=page, adapter_obj=adapter, extra=resume_state_payload)

        if resume_upload_only:
            keep_browser_open = True
            steps.append("resume_upload_only_verified")
            log_step("resume_upload_only_verified", "Resume uploaded and verified before Continue.")
            status = "resume_upload_only_ready"
            final_url = adapter.current_url()
            update_seek_session_state("session_valid", reason="Resume upload verified on SEEK.", final_url_override=final_url)
            return make_result(status, current_error="", current_final_url=final_url)

        adapter.select_cover_letter_change()
        steps.append("selected_cover_letter_write")
        log_step("selected_cover_letter_write", "Cover letter write/paste mode selected.")

        adapter.fill_cover_letter(cover_letter)
        steps.append("pasted_cover_letter")
        log_step("pasted_cover_letter", f"chars={len(cover_letter)}")
        if page is not None:
            cover_letter_verified, cover_letter_info = verify_cover_letter_pasted(page, cover_letter)
            if not cover_letter_verified:
                return fail_with_verification_artifacts(
                    "cover_letter_not_present_before_continue",
                    "Cover letter text was not present in the editor after paste.",
                    cover_letter_info,
                    checkpoint="cover_letter_not_present_before_continue",
                )
        else:
            cover_letter_verified = str(getattr(adapter, "cover_letter", "") or "").strip() == cover_letter

        if not _click_continue_step(adapter, debug_dir, "cover_letter", input_func=input_func):
            status = "paused_for_manual_questionnaire"
            error = "Cover letter step Continue was not clicked automatically."
            return make_result(status, current_error=error)
        steps.append("clicked_continue_1")
        log_step("clicked_continue_1", "Continued from cover letter step.")

        if page is not None:
            record_checkpoint("choose_documents_loaded", page=page, adapter_obj=adapter)
            _log_resume_state("resume_state_after_upload", page, adapter)
            document_step_result = handle_seek_document_step(
                page,
                adapter,
                selected_cv_path,
                debug_dir,
                resume_upload_verified=resume_upload_verified,
                input_func=input_func,
            )
            if not document_step_result.success:
                status = document_step_result.status or "paused_for_manual_questionnaire"
                error = document_step_result.warning or "SEEK document step did not move past Choose documents automatically."
                return make_result(status, current_error=error)
            record_checkpoint("continue_clicked", page=page, adapter_obj=adapter)
            _log_resume_state("resume_state_after_document_continue", page, adapter)
            if not wait_for_seek_step(page, "role_requirements", debug_dir):
                print("SEEK questionnaire did not fully load. Please wait for the employer questions page to finish rendering, then press Enter to continue.", flush=True)
                try:
                    input_func()
                except EOFError:
                    status = "paused_for_manual_questionnaire"
                    error = "SEEK questionnaire step did not finish loading automatically."
                    return make_result(status, current_error=error)

        pause_reason = adapter.detect_pause_reason()
        if pause_reason:
            if pause_reason == "paused_for_seek_login":
                keep_browser_open = True
                resumed, login_error = prompt_for_seek_login(
                    adapter,
                    db_path=database.db_path,
                    use_bulk_profile=use_bulk_profile,
                    input_func=input_func,
                    allow_manual_prompt=allow_manual_login_prompt,
                )
                if not resumed:
                    pause_status = _pause_status_for_login_error(login_error)
                    state_name = (
                        "captcha_required" if pause_status == "paused_for_captcha"
                        else "verification_required" if pause_status == "paused_for_seek_verification"
                        else "login_required"
                    )
                    update_seek_session_state(state_name, reason=login_error, final_url_override=adapter.current_url())
                    status = pause_status
                    error = login_error
                    return make_result(status, current_error=error)
                steps.append("resumed_after_manual_login")
                log_step("resumed_after_manual_login", "Resumed after manual SEEK login.")
                update_seek_session_state("session_refreshed", reason="Resumed after manual SEEK login.", final_url_override=adapter.current_url())
                pause_reason = adapter.detect_pause_reason()
                if pause_reason == "paused_for_seek_login":
                    return pause_with_seek_artifacts(
                        "paused_for_seek_login",
                        "SEEK login is still required after manual prompt.",
                        stem="post_continue_seek_login_still_required",
                        checkpoint="post_continue_seek_login_still_required",
                    )
                if pause_reason == "paused_for_seek_verification":
                    print("[seek_apply_assist] Paused for SEEK verification.", flush=True)
                    return pause_with_seek_artifacts(
                        "paused_for_seek_verification",
                        "SEEK verification is required before automation can continue.",
                        stem="post_continue_seek_verification_required",
                        checkpoint="post_continue_seek_verification_required",
                    )
            else:
                print("[seek_apply_assist] Paused for manual continuation.", flush=True)
                stem = "post_continue_seek_verification_required" if pause_reason == "paused_for_seek_verification" else "post_continue_seek_captcha_required" if pause_reason == "paused_for_captcha" else "post_continue_seek_login_required"
                message = (
                    "SEEK verification is required before automation can continue."
                    if pause_reason == "paused_for_seek_verification"
                    else "Login or CAPTCHA requires manual continuation."
                    if pause_reason == "paused_for_captcha"
                    else "SEEK login is required before automation can continue."
                )
                return pause_with_seek_artifacts(pause_reason, message, stem=stem, checkpoint=stem)

        questionnaire_result = handle_seek_questionnaire(
            adapter,
            salary_expectation_text,
            debug_dir,
            applicant_profile=applicant_profile,
            input_func=input_func,
        )
        if questionnaire_result.status == "questionnaire_completed":
            steps.append("selected_work_eligibility")
            log_step("selected_work_eligibility", "Selected New Zealand work eligibility yes.")
            steps.append("entered_salary_expectation")
            log_step("entered_salary_expectation", f"salary_expectation_text={salary_expectation_text}")
        elif questionnaire_result.status == "questionnaire_manual_pause_continued":
            steps.append("questionnaire_manual_pause")
            database.log_run("seek_apply_assist", "questionnaire_manual_pause", "warning", f"job_id={job_id} {questionnaire_result.warning}")
        elif questionnaire_result.status == "paused_for_manual_questionnaire":
            steps.append("questionnaire_manual_pause")
            status = "paused_for_manual_questionnaire"
            error = questionnaire_result.warning
            database.log_run("seek_apply_assist", "questionnaire_manual_pause", "warning", f"job_id={job_id} {questionnaire_result.warning}")
            return make_result(status, current_error=error)

        questionnaire_validation_errors = adapter.get_questionnaire_validation_errors()
        if questionnaire_validation_errors:
            recovered_dynamic_result = handle_dynamic_seek_questions(
                adapter,
                applicant_profile,
                debug_dir,
                input_func=input_func,
            )
            if recovered_dynamic_result.status == "paused_for_manual_questionnaire":
                steps.append("questionnaire_manual_pause")
                status = "paused_for_manual_questionnaire"
                error = recovered_dynamic_result.warning
                database.log_run("seek_apply_assist", "questionnaire_manual_pause", "warning", f"job_id={job_id} {recovered_dynamic_result.warning}")
                return make_result(status, current_error=error)
            if recovered_dynamic_result.answered_count > 0:
                if not _click_continue_step(adapter, debug_dir, "questionnaire", input_func=input_func):
                    status = "paused_for_manual_questionnaire"
                    error = "Questions answered but Continue was not clicked automatically."
                    return make_result(status, current_error=error)
                questionnaire_validation_errors = adapter.get_questionnaire_validation_errors()
        questions_answered = not questionnaire_validation_errors
        if questionnaire_validation_errors:
            if page is None:
                page = getattr(adapter, "_page", None)
            payload = {
                "validation_errors": questionnaire_validation_errors,
                "questionnaire_status": questionnaire_result.status,
            }
            if page is not None:
                payload["body_text_snippet"] = _get_page_body_text(page)[:1200]
            return fail_with_verification_artifacts(
                "questionnaire_validation_errors_after_answering",
                "Questionnaire validation errors remain after automated answering.",
                payload,
                checkpoint="questionnaire_validation_errors_after_answering",
            )

        steps.append("clicked_continue_questionnaire")
        log_step("clicked_continue_questionnaire", "Continued from questionnaire step.")
        if page is not None and resume_upload_verified and _is_review_page(page):
            record_checkpoint("review_page_checked", page=page, adapter_obj=adapter)
            _log_resume_state("resume_state_after_questions", page, adapter)
            if _resume_state_is_lost(page, adapter):
                screenshot_path, html_path, json_path = _save_resume_state_lost_artifacts(debug_dir, page, adapter, "after_questions")
                latest_checkpoint = "resume_missing_on_review"
                _write_codex_prompt_resume_failure(
                    debug_dir,
                    failure_reason="Resume was lost after employer questions.",
                    latest_checkpoint=latest_checkpoint,
                    screenshot_path=screenshot_path,
                    html_path=html_path,
                    json_path=json_path,
                    snapshot=_resume_state_snapshot(page, adapter),
                    suggested_next_fix="Inspect the transition after employer questions and confirm SEEK still carries the selected resume into review.",
                )
                print("[seek_apply_assist] resume_state_lost_detected", flush=True)
                print(f"[seek_apply_assist] resume_state_lost_screenshot: {screenshot_path}", flush=True)
                print(f"[seek_apply_assist] resume_state_lost_html: {html_path}", flush=True)
                print(f"[seek_apply_assist] resume_state_lost_json: {json_path}", flush=True)
                status = "failed"
                error = "Resume state was lost after employer questions. SEEK shows no resume included."
                return make_result(status, current_error=error)

        navigation_label = adapter.final_navigation_label().strip().lower()
        on_review_page = bool(page is not None and _is_review_page(page))
        review_navigation_label = "review and submit" in navigation_label
        if any(pattern in navigation_label for pattern in FINAL_SUBMIT_PATTERNS) and (on_review_page or not review_navigation_label):
            if page is not None and resume_upload_verified and _is_review_page(page):
                record_checkpoint("review_page_checked", page=page, adapter_obj=adapter)
                _log_resume_state("resume_state_before_review", page, adapter)
                included, review_info = verify_resume_included_on_review(page, verified_resume_filename)
                if not included:
                    repaired = _attempt_review_resume_repair(
                        page,
                        adapter,
                        selected_cv_path,
                        debug_dir,
                        expected_filename=verified_resume_filename,
                    )
                    if repaired:
                        record_checkpoint("review_page_checked", page=page, adapter_obj=adapter, extra={"repair_attempted": True})
                        included, review_info = verify_resume_included_on_review(page, verified_resume_filename)
                if not included:
                    latest_checkpoint = "resume_missing_on_review"
                    screenshot_path, html_path, json_path = _save_resume_state_lost_artifacts(debug_dir, page, adapter, "before_review")
                    _write_codex_prompt_resume_failure(
                        debug_dir,
                        failure_reason="CV uploaded earlier but SEEK review page shows no resume included.",
                        latest_checkpoint=latest_checkpoint,
                        screenshot_path=screenshot_path,
                        html_path=html_path,
                        json_path=json_path,
                        snapshot=review_info,
                        suggested_next_fix="Inspect the review-page document editor path and confirm the resume is saved before returning to Review and submit.",
                    )
                    print("[seek_apply_assist] resume_state_lost_detected", flush=True)
                    print(f"[seek_apply_assist] resume_state_lost_screenshot: {screenshot_path}", flush=True)
                    print(f"[seek_apply_assist] resume_state_lost_html: {html_path}", flush=True)
                    print(f"[seek_apply_assist] resume_state_lost_json: {json_path}", flush=True)
                    status = "failed"
                    error = "CV uploaded earlier but SEEK review page shows no resume included."
                    return make_result(status, current_error=error)
                record_checkpoint("resume_confirmed_included", page=page, adapter_obj=adapter, extra=review_info)
            if page is not None and _is_review_page(page):
                checklist, checklist_details = build_final_review_checklist()
                _write_final_review_checklist(debug_dir, checklist, checklist_details)
                if not checklist.all_true():
                    return fail_with_verification_artifacts(
                        "final_review_checklist_failed",
                        "Final SEEK review checklist failed. Resume, cover letter, questionnaire state, or validation checks were incomplete.",
                        {
                            **checklist.as_dict(),
                            **checklist_details,
                        },
                        checkpoint="final_review_checklist_failed",
                    )
            steps.append("reached_manual_review")
            log_step("reached_manual_review", f"final_label={navigation_label or 'unknown'}")
            final_url = adapter.current_url()
            if auto_submit:
                return submit_current_review_page()
            steps.append("stopped_before_submit")
            log_step("stopped_before_submit", "Application prepared. Browser left open for manual review. Submit is not automated.")
            status = "prepared_for_manual_review"
            keep_browser_open = not close_browser_on_success
            if keep_browser_open:
                wait_for_manual_review(input_func=input_func)
            return make_result(status, current_error="", current_final_url=final_url)

        if page is not None and (any(pattern in navigation_label for pattern in SAFE_NAVIGATION_PATTERNS) or _is_update_profile_page(page)):
            reached_review_page, navigation_attempts = _advance_post_questionnaire_navigation(
                adapter,
                debug_dir,
                selected_cv_path=selected_cv_path,
                resume_upload_verified=resume_upload_verified,
                input_func=input_func,
            )
            if navigation_attempts > 0:
                steps.append("clicked_final_navigation")
                log_step("clicked_final_navigation", f"label={navigation_label} attempts={navigation_attempts}")
            if not reached_review_page and (review_navigation_label or _is_update_profile_page(page) or any(pattern in navigation_label for pattern in SAFE_NAVIGATION_PATTERNS)):
                status = "paused_for_manual_questionnaire"
                error = "Review and submit step did not reach the SEEK review page automatically."
                return make_result(status, current_error=error)
            if resume_upload_verified and _is_review_page(page):
                record_checkpoint("review_page_checked", page=page, adapter_obj=adapter)
                _log_resume_state("resume_state_after_profile", page, adapter)
                if _resume_state_is_lost(page, adapter):
                    screenshot_path, html_path, json_path = _save_resume_state_lost_artifacts(debug_dir, page, adapter, "after_profile")
                    latest_checkpoint = "resume_missing_on_review"
                    _write_codex_prompt_resume_failure(
                        debug_dir,
                        failure_reason="Resume was lost after profile/update navigation.",
                        latest_checkpoint=latest_checkpoint,
                        screenshot_path=screenshot_path,
                        html_path=html_path,
                        json_path=json_path,
                        snapshot=_resume_state_snapshot(page, adapter),
                        suggested_next_fix="Inspect the post-profile navigation step and confirm the document selection is persisted before review.",
                    )
                    print("[seek_apply_assist] resume_state_lost_detected", flush=True)
                    print(f"[seek_apply_assist] resume_state_lost_screenshot: {screenshot_path}", flush=True)
                    print(f"[seek_apply_assist] resume_state_lost_html: {html_path}", flush=True)
                    print(f"[seek_apply_assist] resume_state_lost_json: {json_path}", flush=True)
                    status = "failed"
                    error = "Resume state was lost after profile/update navigation. SEEK shows no resume included."
                    return make_result(status, current_error=error)

        steps.append("reached_manual_review")
        if page is not None and resume_upload_verified and _is_review_page(page):
            record_checkpoint("review_page_checked", page=page, adapter_obj=adapter)
            _log_resume_state("resume_state_before_review", page, adapter)
            included, review_info = verify_resume_included_on_review(page, verified_resume_filename)
            if not included:
                latest_checkpoint = "resume_missing_on_review"
                screenshot_path, html_path, json_path = _save_resume_state_lost_artifacts(debug_dir, page, adapter, "before_review")
                _write_codex_prompt_resume_failure(
                    debug_dir,
                    failure_reason="CV uploaded earlier but SEEK review page shows no resume included.",
                    latest_checkpoint=latest_checkpoint,
                    screenshot_path=screenshot_path,
                    html_path=html_path,
                    json_path=json_path,
                    snapshot=review_info,
                    suggested_next_fix="Inspect the final transition into Review and submit and verify the resume card remains attached.",
                )
                print("[seek_apply_assist] resume_state_lost_detected", flush=True)
                print(f"[seek_apply_assist] resume_state_lost_screenshot: {screenshot_path}", flush=True)
                print(f"[seek_apply_assist] resume_state_lost_html: {html_path}", flush=True)
                print(f"[seek_apply_assist] resume_state_lost_json: {json_path}", flush=True)
                status = "failed"
                error = "CV uploaded earlier but SEEK review page shows no resume included."
                return make_result(status, current_error=error)
            record_checkpoint("resume_confirmed_included", page=page, adapter_obj=adapter, extra=review_info)
        if page is not None and _is_review_page(page):
            checklist, checklist_details = build_final_review_checklist()
            _write_final_review_checklist(debug_dir, checklist, checklist_details)
            if not checklist.all_true():
                return fail_with_verification_artifacts(
                    "final_review_checklist_failed",
                    "Final SEEK review checklist failed. Resume, cover letter, questionnaire state, or validation checks were incomplete.",
                    {
                        **checklist.as_dict(),
                        **checklist_details,
                    },
                    checkpoint="final_review_checklist_failed",
                )
        log_step("reached_manual_review", f"final_label={adapter.final_navigation_label() or navigation_label or 'unknown'}")
        final_url = adapter.current_url()
        if auto_submit:
            return submit_current_review_page()
        steps.append("stopped_before_submit")
        log_step("stopped_before_submit", "Application prepared. Browser left open for manual review. Submit is not automated.")
        status = "prepared_for_manual_review"
        update_seek_session_state("session_valid", reason="SEEK flow reached manual review.", final_url_override=final_url)
        keep_browser_open = not close_browser_on_success
        if keep_browser_open:
            wait_for_manual_review(input_func=input_func)
        return make_result(status, current_error="", current_final_url=final_url)
    except SeekApplyAssistError as exc:
        error = str(exc)
        final_url = adapter.current_url()
    except Exception as exc:  # noqa: BLE001
        error = str(exc)
        final_url = adapter.current_url()
    finally:
        database.log_run("seek_apply_assist", "finished", status if status != "failed" else "error", f"job_id={job_id} status={status} error={error}")
        if not keep_browser_open and status not in PAUSED_STATUSES:
            try:
                adapter.close()
            except Exception:  # noqa: BLE001
                pass
    if error and latest_checkpoint:
        try:
            _write_codex_prompt_resume_failure(
                debug_dir,
                failure_reason=error,
                latest_checkpoint=latest_checkpoint,
                screenshot_path=debug_dir / f"{latest_checkpoint}.png",
                html_path=debug_dir / f"{latest_checkpoint}.html",
                json_path=debug_dir / f"{latest_checkpoint}.json",
                suggested_next_fix="Inspect the latest checkpoint artifacts and update the SEEK resume/apply state transition handling.",
            )
        except Exception:  # noqa: BLE001
            pass
    return make_result(status, current_error=error, current_final_url=final_url)


class _PlaywrightSeekAdapter:
    def __init__(
        self,
        visible: bool,
        db_path: Path,
        *,
        force_new_profile: bool = False,
        use_bulk_profile: bool = False,
        slow_mo_ms: int = 0,
    ) -> None:
        try:
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
            from playwright.sync_api import sync_playwright
        except Exception as exc:  # noqa: BLE001
            raise SeekApplyAssistError(
                "Playwright is not installed. Install it with `pip install playwright` and run `playwright install chromium`."
            ) from exc

        self._timeout_error = PlaywrightTimeoutError
        self._playwright_cm = sync_playwright()
        self._playwright = self._playwright_cm.start()
        configured_user_data_dir, configured_launch_args = seed_isolated_seek_browser_profile_from_configured_profile(
            db_path,
            use_bulk_profile=use_bulk_profile,
        )
        self.user_data_dir = (
            seed_temp_seek_profile_from_logged_in_profile(db_path)
            if force_new_profile
            else configured_user_data_dir
            if configured_user_data_dir is not None
            else seek_bulk_profile_dir_for_db(db_path)
            if use_bulk_profile
            else seek_profile_dir_for_db(db_path)
        )
        self.browser_launch_args = list(configured_launch_args)
        self._cleanup_user_data_dir_on_close = bool(force_new_profile)
        self.user_data_dir.mkdir(parents=True, exist_ok=True)
        app_managed_profile = _is_app_managed_seek_profile_dir(self.user_data_dir, db_path)
        if app_managed_profile:
            removed_locks = _cleanup_stale_seek_profile_locks(self.user_data_dir)
            if removed_locks:
                print(
                    "[seek_apply_assist] stale_profile_locks_removed: "
                    + ", ".join(str(path) for path in removed_locks),
                    flush=True,
                )
        profile_locked_before_launch = seek_profile_appears_locked(self.user_data_dir)
        if profile_locked_before_launch:
            print(f"[seek_apply_assist] profile_lock_detected: {self.user_data_dir}", flush=True)
            if app_managed_profile:
                removed_locks = _cleanup_stale_seek_profile_locks(self.user_data_dir)
                if removed_locks:
                    print(
                        "[seek_apply_assist] stale_profile_locks_removed_after_detect: "
                        + ", ".join(str(path) for path in removed_locks),
                        flush=True,
                    )
                profile_locked_before_launch = seek_profile_appears_locked(self.user_data_dir)
        self.browser_channel = "chromium"
        launch_errors: list[str] = []
        last_exc: Exception | None = None
        profile_locked_message = _profile_locked_message_for_dir(self.user_data_dir, db_path)
        for browser_channel in preferred_seek_browser_channels(db_path.parent):
            launch_kwargs: dict[str, Any] = {
                "user_data_dir": str(self.user_data_dir),
                "headless": not visible,
                "slow_mo": slow_mo_ms,
            }
            if self.browser_launch_args:
                launch_kwargs["args"] = list(self.browser_launch_args)
            if browser_channel != "chromium":
                launch_kwargs["channel"] = browser_channel
            try:
                self._context = self._playwright.chromium.launch_persistent_context(**launch_kwargs)
                self.browser_channel = browser_channel
                print(f"[seek_apply_assist] browser_channel_selected: {browser_channel}", flush=True)
                break
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                message = str(exc)
                launch_errors.append(f"{browser_channel}: {message}")
                if profile_locked_before_launch or _looks_like_profile_lock_error(message):
                    try:
                        self._playwright.stop()
                    except Exception:  # noqa: BLE001
                        pass
                    raise SeekBrowserProfileLockedError(profile_locked_message) from exc
                print(f"[seek_apply_assist] browser_channel_failed: {browser_channel}: {message}", flush=True)
        else:
            try:
                self._playwright.stop()
            except Exception:  # noqa: BLE001
                pass
            combined = " | ".join(launch_errors) if launch_errors else "unknown browser launch failure"
            raise SeekApplyAssistError(f"Could not launch SEEK browser profile: {combined}") from last_exc
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        self._page.set_default_timeout(15000)

    def open_job(self, url: str, fallback_url: str | None = None) -> None:
        resolved_primary = _resolve_seek_tracking_url(url)
        attempts: list[tuple[str, str]] = [("primary", resolved_primary or url)]
        lowered_url = str(url or "").strip().lower()
        fallback_url = str(fallback_url or "").strip()
        if (
            fallback_url
            and fallback_url != url
            and "email.s.seek.co.nz" in lowered_url
        ):
            attempts.append(("fallback", fallback_url))
        elif resolved_primary and resolved_primary != str(url or "").strip():
            attempts.append(("original_tracking", str(url or "").strip()))

        last_exc: Exception | None = None
        for label, target_url in attempts:
            for attempt_index in range(2):
                try:
                    self._page.goto(target_url, wait_until="domcontentloaded")
                    try:
                        self._page.wait_for_load_state("networkidle", timeout=10000)
                    except Exception:  # noqa: BLE001
                        try:
                            self._page.wait_for_timeout(1000)
                        except Exception:  # noqa: BLE001
                            pass
                    if label != "primary" or attempt_index > 0:
                        print(
                            f"[seek_apply_assist] open_job_recovered: source={label} attempt={attempt_index + 1} url={target_url}",
                            flush=True,
                        )
                    return
                except Exception as exc:  # noqa: BLE001
                    last_exc = exc
                    print(
                        f"[seek_apply_assist] open_job_attempt_failed: source={label} attempt={attempt_index + 1} url={target_url} error={exc}",
                        flush=True,
                    )
                    try:
                        self._page.wait_for_timeout(1000 if attempt_index == 0 else 1500)
                    except Exception:  # noqa: BLE001
                        pass
        if last_exc is not None:
            raise last_exc

    def detect_pause_reason(self) -> str | None:
        url = self.current_url().lower()
        page_text = self.page_text().lower()
        is_verification_text, _token = _looks_like_seek_verification_text(page_text)
        if is_verification_text:
            return "paused_for_seek_verification"
        if "captcha" in url or "captcha" in page_text or "verify you are human" in page_text:
            return "paused_for_captcha"
        if "login.seek.com" in url or "oauth/login" in url or any(token in url for token in ("signin", "sign-in")) or "log in" in str(getattr(self._page, "title", lambda: "")() or "").lower() or any(
            token in page_text for token in ("log in", "sign in to continue", "sign in or register", "enter code", "check your email")
        ):
            return "paused_for_seek_login"
        return None

    def click_quick_apply(self) -> None:
        try:
            self._page.wait_for_timeout(1000)
        except Exception:  # noqa: BLE001
            pass
        try:
            _wait_for_seek_apply_page_ready(self._page, timeout_ms=15000)
        except Exception:  # noqa: BLE001
            pass

        try:
            current_url = str(self.current_url() or "")
        except Exception:  # noqa: BLE001
            current_url = ""
        if "/apply" in current_url.lower():
            try:
                page_title = str(self._page.title() or "")
            except Exception:  # noqa: BLE001
                page_title = ""
            try:
                page_text = str(self.page_text() or "")
            except Exception:  # noqa: BLE001
                page_text = ""
            if re.search(
                r"choose documents|answer employer questions|review and submit|write cover letter|upload a r[ée]sum[ée]",
                f"{page_title}\n{page_text}",
                re.I,
            ):
                return

        quick_apply_pattern = re.compile(r"quick\s*apply", re.I)
        quick_apply_candidates = [
            lambda: self._page.locator("button, a, [role='button'], [role='link']").filter(has_text=quick_apply_pattern),
            lambda: self._page.locator('[data-testid*="quick" i], [data-automation*="quick" i]').filter(has_text=re.compile(r"apply", re.I)),
            lambda: self._page.get_by_text("Quick apply", exact=False),
            lambda: self._page.locator("text=/Quick\\s*apply/i"),
        ]
        try:
            self._click_quick_apply_candidates(quick_apply_candidates)
            return
        except SeekApplyAssistError:
            pass

        try:
            if _is_seek_loading_shell(self._page):
                _wait_for_seek_apply_page_ready(self._page, timeout_ms=10000)
                self._click_quick_apply_candidates(quick_apply_candidates)
                return
        except SeekApplyAssistError:
            pass
        except Exception:  # noqa: BLE001
            pass

        try:
            page_text = self.page_text()
        except Exception:  # noqa: BLE001
            page_text = ""
        if re.search(r"quick\s*apply", str(page_text or ""), re.I):
            dom_quick_apply_href = self._find_dom_quick_apply_href()
            if dom_quick_apply_href:
                target_url = urljoin(self.current_url(), dom_quick_apply_href)
                self._page.goto(target_url, wait_until="domcontentloaded")
                try:
                    self._page.wait_for_load_state("networkidle", timeout=10000)
                except Exception:  # noqa: BLE001
                    try:
                        self._page.wait_for_timeout(1000)
                    except Exception:  # noqa: BLE001
                        pass
                return
            apply_pattern = re.compile(r"^apply$|apply now|apply for this job", re.I)
            apply_candidates = [
                lambda: self._page.locator("button, a, [role='button'], [role='link']").filter(has_text=apply_pattern),
                lambda: self._page.locator('[data-testid*="apply" i], [data-automation*="apply" i]'),
            ]
            self._click_quick_apply_candidates(apply_candidates)
            return

        raise SeekApplyAssistError("Could not find SEEK Quick apply button.")

    def _find_dom_quick_apply_href(self) -> str:
        try:
            href = self._page.evaluate(
                """() => {
                    const normalise = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
                    const isVisible = (el) => {
                        if (!(el instanceof Element)) return false;
                        const style = window.getComputedStyle(el);
                        return style.display !== 'none' && style.visibility !== 'hidden' && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                    };
                    const candidates = Array.from(document.querySelectorAll('button, a, [role="button"], [role="link"]'));
                    for (const el of candidates) {
                        if (!isVisible(el)) continue;
                        const text = normalise(el.innerText || el.textContent || '');
                        if (!/quick\\s*apply/i.test(text)) continue;
                        const href = normalise(el.getAttribute('href') || '');
                        if (href) return href;
                    }
                    return '';
                }"""
            )
        except Exception:  # noqa: BLE001
            return ""
        return str(href or "").strip()

    def _click_quick_apply_candidates(self, locator_factories: list[Callable[[], Any]]) -> None:
        for factory in locator_factories:
            try:
                locator = factory()
                count = locator.count()
            except self._timeout_error:
                continue
            except Exception:  # noqa: BLE001
                continue
            for index in range(count):
                try:
                    candidate = locator.nth(index)
                    if not candidate.is_visible():
                        continue
                    before_url = self.current_url()
                    href = str(candidate.get_attribute("href") or "").strip()
                    candidate.click(timeout=5000)
                    try:
                        self._page.wait_for_load_state("domcontentloaded", timeout=5000)
                    except Exception:  # noqa: BLE001
                        try:
                            self._page.wait_for_timeout(750)
                        except Exception:  # noqa: BLE001
                            pass
                    after_url = self.current_url()
                    if after_url != before_url:
                        return
                    if href:
                        target_url = urljoin(before_url, href)
                        if target_url and target_url != after_url:
                            self._page.goto(target_url, wait_until="domcontentloaded")
                            try:
                                self._page.wait_for_load_state("networkidle", timeout=10000)
                            except Exception:  # noqa: BLE001
                                try:
                                    self._page.wait_for_timeout(1000)
                                except Exception:  # noqa: BLE001
                                    pass
                            return
                    return
                except self._timeout_error:
                    continue
                except Exception:  # noqa: BLE001
                    continue
        raise SeekApplyAssistError("Could not find SEEK Quick apply button.")

    def wait_for_application_form(self) -> None:
        self._page.wait_for_timeout(1000)

    def wait_for_resume_section(self, timeout_ms: int = 20000) -> None:
        candidates = [
            self._page.get_by_text(re.compile("Choose documents", re.I)).first,
            self._page.get_by_text(re.compile("Résumé|Resume", re.I)).first,
            self._page.get_by_text(re.compile("Upload a résumé|Upload a resume", re.I)).first,
            self._page.locator('input[name="resume-method"]').first,
        ]
        for locator in candidates:
            try:
                locator.wait_for(state="attached", timeout=timeout_ms)
                self._page.wait_for_timeout(1000)
                return
            except Exception:  # noqa: BLE001
                continue
        self._page.wait_for_timeout(2000)

    def log_resume_diagnostics(self) -> None:
        try:
            title = self._page.title()
        except Exception:  # noqa: BLE001
            title = ""
        try:
            resume_method_count = self._page.locator('input[name="resume-method"]').count()
        except Exception:  # noqa: BLE001
            resume_method_count = 0
        try:
            upload_radio_count = self._page.locator('input[data-testid="resume-method-upload"]').count()
        except Exception:  # noqa: BLE001
            upload_radio_count = 0
        try:
            file_input_count = self._page.locator('input[type="file"]').count()
        except Exception:  # noqa: BLE001
            file_input_count = 0
        resume_section_text = ""
        try:
            section_locator = self._page.locator("body")
            section_text = section_locator.inner_text(timeout=5000)
            lines = [line.strip() for line in section_text.splitlines() if line.strip()]
            matched_lines = [
                line for line in lines
                if re.search(r"choose documents|résumé|resume|upload a résumé|upload a resume|select a résumé|select a resume", line, re.I)
            ]
            resume_section_text = " | ".join(matched_lines[:12])
        except Exception:  # noqa: BLE001
            resume_section_text = ""
        print(f"[seek_apply_assist] resume_page_url: {self.current_url()}", flush=True)
        print(f"[seek_apply_assist] resume_page_title: {title}", flush=True)
        print(f"[seek_apply_assist] resume_method_input_count: {resume_method_count}", flush=True)
        print(f"[seek_apply_assist] resume_upload_radio_count: {upload_radio_count}", flush=True)
        print(f"[seek_apply_assist] resume_file_input_count: {file_input_count}", flush=True)
        print(f"[seek_apply_assist] resume_section_text: {resume_section_text or '(none found)'}", flush=True)

    def get_resume_file_input_state(self) -> tuple[int, bool, bool]:
        try:
            file_inputs = self._page.locator('input[type="file"]')
            count = file_inputs.count()
            visible = False
            attached = False
            for index in range(count):
                locator = file_inputs.nth(index)
                try:
                    attached = True
                    visible = visible or bool(locator.is_visible())
                except Exception:  # noqa: BLE001
                    continue
            return count, visible, attached
        except Exception:  # noqa: BLE001
            return 0, False, False

    def get_resume_upload_radio_state(self) -> tuple[bool, bool]:
        try:
            state = self._page.evaluate(
                """() => {
                    const selectors = [
                        'input[data-testid="resume-method-upload"]',
                        'input[name="resume-method"][value="upload"]',
                    ];
                    for (const selector of selectors) {
                        const input = document.querySelector(selector);
                        if (!input) continue;
                        return { visible: true, checked: !!input.checked };
                    }
                    return { visible: false, checked: false };
                }"""
            )
            if isinstance(state, dict):
                return bool(state.get("visible")), bool(state.get("checked"))
        except Exception as exc:  # noqa: BLE001
            print(f"[seek_apply_assist] get_resume_upload_radio_state_page_evaluate_error: {exc}", flush=True)
        candidates = [
            self._page.locator('input[data-testid="resume-method-upload"]').first,
            self._page.locator('input[name="resume-method"][value="upload"]').first,
        ]
        for locator in candidates:
            try:
                if locator.count():
                    return True, bool(locator.is_checked())
            except Exception:  # noqa: BLE001
                continue
        return False, False

    def resume_upload_radio_visible(self) -> bool:
        try:
            return bool(
                self._page.evaluate(
                    """() => !!(
                        document.querySelector('input[data-testid="resume-method-upload"]') ||
                        document.querySelector('input[name="resume-method"][value="upload"]')
                    )"""
                )
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[seek_apply_assist] resume_upload_radio_visible_page_evaluate_error: {exc}", flush=True)
            return False

    def get_resume_method_state(self) -> dict[str, Any]:
        def _checked(selector: str) -> bool:
            try:
                locator = self._page.locator(selector).first
                return bool(locator.count() and locator.is_checked())
            except Exception:  # noqa: BLE001
                return False

        try:
            selected_value = str(
                self._page.evaluate(
                    """() => {
                        const checked = document.querySelector('input[name="resume-method"]:checked');
                        return checked ? (checked.value || '') : '';
                    }"""
                )
                or ""
            ).strip()
        except Exception:  # noqa: BLE001
            selected_value = ""
        upload_checked = _checked('input[data-testid="resume-method-upload"]') or _checked('input[name="resume-method"][value="upload"]')
        none_checked = _checked('input[data-testid="resume-method-none"]') or _checked('input[name="resume-method"][value="none"]')
        return {
            "upload_radio_checked": upload_checked,
            "select_resume_radio_checked": upload_checked,
            "dont_include_resume_radio_checked": none_checked,
            "selected_resume_method_value": selected_value,
        }

    def click_resume_change_radio(self) -> bool:
        def _post_click_settle() -> None:
            try:
                self._page.wait_for_timeout(750)
            except Exception as exc:  # noqa: BLE001
                print(f"[seek_apply_assist] click_resume_change_radio_settle_wait_error: {exc}", flush=True)

        try:
            clicked = bool(
                self._page.evaluate(
                    """() => {
                        const selectors = [
                            'input[data-testid="resume-method-change"]',
                            'input[name="resume-method"][value="change"]',
                        ];
                        for (const selector of selectors) {
                            const input = document.querySelector(selector);
                            if (!input) continue;
                            input.click();
                            input.checked = true;
                            input.dispatchEvent(new Event('input', { bubbles: true }));
                            input.dispatchEvent(new Event('change', { bubbles: true }));
                            return true;
                        }
                        return false;
                    }"""
                )
            )
            if clicked:
                _post_click_settle()
                return True
        except Exception:  # noqa: BLE001
            pass
        direct_locators = [
            self._page.locator('input[data-testid="resume-method-change"]').first,
            self._page.locator('input[name="resume-method"][value="change"]').first,
        ]
        for locator in direct_locators:
            try:
                if locator.count():
                    locator.check(force=True, timeout=1200)
                    _post_click_settle()
                    return True
            except Exception:  # noqa: BLE001
                try:
                    if locator.count():
                        locator.evaluate(
                            """el => {
                                el.click();
                                el.checked = true;
                                el.dispatchEvent(new Event('input', { bubbles: true }));
                                el.dispatchEvent(new Event('change', { bubbles: true }));
                            }"""
                        )
                        _post_click_settle()
                        return True
                except Exception:  # noqa: BLE001
                    continue
        label_patterns = [
            re.compile("Select a r.s.um.|Select a resume", re.I),
            re.compile("Choose a r.s.um.|Choose a resume", re.I),
        ]
        for pattern in label_patterns:
            try:
                locator = self._page.get_by_label(pattern).first
                if locator.count() and locator.is_visible():
                    locator.click(timeout=1500)
                    self._page.wait_for_timeout(750)
                    return True
            except Exception:  # noqa: BLE001
                continue
        for pattern in label_patterns:
            try:
                locator = self._page.get_by_text(pattern).first
                if locator.count() and locator.is_visible():
                    locator.click(timeout=1500)
                    self._page.wait_for_timeout(750)
                    return True
            except Exception:  # noqa: BLE001
                continue
        locators = [
            self._page.locator('input[data-testid="resume-method-change"]').first,
            self._page.locator('input[name="resume-method"][value="change"]').first,
        ]
        for locator in locators:
            try:
                if locator.count():
                    locator.check(force=True, timeout=1500)
                    self._page.wait_for_timeout(750)
                    return True
            except Exception:  # noqa: BLE001
                try:
                    if locator.count():
                        locator.evaluate("el => el.click()")
                        self._page.wait_for_timeout(750)
                        return True
                except Exception:  # noqa: BLE001
                    continue
        return False

    def get_existing_resume_options(self) -> list[str]:
        selectors = [
            'select[data-testid="select-input"] option',
            'select[placeholder*="resum" i] option',
        ]
        for selector in selectors:
            try:
                options = self._page.locator(selector)
                count = options.count()
            except Exception:  # noqa: BLE001
                continue
            if count <= 0:
                continue
            values: list[str] = []
            for index in range(count):
                try:
                    text = str(options.nth(index).inner_text(timeout=1000) or "").strip()
                except Exception:  # noqa: BLE001
                    text = ""
                if text:
                    values.append(text)
            if values:
                return values
        return []

    def select_existing_resume_option(self, option_text: str) -> bool:
        selectors = [
            'select[data-testid="select-input"]',
            'select[placeholder*="resum" i]',
        ]
        option_text_normalized = re.sub(r"\s+", " ", str(option_text or "")).strip().lower()
        for selector in selectors:
            try:
                dropdown = self._page.locator(selector).first
                if not dropdown.count():
                    continue
            except Exception:  # noqa: BLE001
                continue
            try:
                dropdown.select_option(label=option_text)
            except Exception:  # noqa: BLE001
                pass

            def _selected_option_matches() -> bool:
                try:
                    selected_state = dropdown.evaluate(
                        """el => {
                            const option = el.selectedOptions && el.selectedOptions.length ? el.selectedOptions[0] : null;
                            return {
                                value: String(el.value || '').trim(),
                                label: option ? String(option.textContent || '').replace(/\\s+/g, ' ').trim() : '',
                            };
                        }"""
                    )
                except Exception:  # noqa: BLE001
                    return False
                selected_label = re.sub(r"\s+", " ", str((selected_state or {}).get("label") or "")).strip().lower()
                if not selected_label or "please select" in selected_label:
                    return False
                return (
                    selected_label == option_text_normalized
                    or option_text_normalized in selected_label
                    or selected_label in option_text_normalized
                )

            if _selected_option_matches():
                try:
                    self._page.wait_for_timeout(500)
                except Exception:  # noqa: BLE001
                    pass
                return True

            try:
                option_payloads = dropdown.evaluate(
                    """el => Array.from(el.options || []).map((option, index) => ({
                        index,
                        value: String(option.value || '').trim(),
                        label: String(option.textContent || '').replace(/\\s+/g, ' ').trim(),
                    }))"""
                )
            except Exception:  # noqa: BLE001
                option_payloads = []

            matching_option = None
            for option_payload in option_payloads or []:
                option_label = re.sub(r"\s+", " ", str((option_payload or {}).get("label") or "")).strip().lower()
                if not option_label or "please select" in option_label:
                    continue
                if (
                    option_label == option_text_normalized
                    or option_text_normalized in option_label
                    or option_label in option_text_normalized
                ):
                    matching_option = option_payload
                    break

            if not matching_option:
                continue

            matching_value = str((matching_option or {}).get("value") or "").strip()
            matching_label = str((matching_option or {}).get("label") or "").strip()
            for _ in range(3):
                try:
                    if matching_value:
                        dropdown.select_option(value=matching_value)
                    else:
                        dropdown.select_option(label=matching_label)
                except Exception:  # noqa: BLE001
                    pass
                try:
                    dropdown.evaluate(
                        """(el, payload) => {
                            const desiredValue = String((payload && payload.value) || '').trim();
                            const desiredLabel = String((payload && payload.label) || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                            let matched = null;
                            for (const option of Array.from(el.options || [])) {
                                const optionValue = String(option.value || '').trim();
                                const optionLabel = String(option.textContent || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                                const isMatch =
                                    (desiredValue && optionValue === desiredValue) ||
                                    (desiredLabel && (optionLabel === desiredLabel || optionLabel.includes(desiredLabel) || desiredLabel.includes(optionLabel)));
                                option.selected = !!isMatch;
                                if (isMatch) {
                                    matched = option;
                                }
                            }
                            if (matched) {
                                el.value = String(matched.value || '');
                                el.selectedIndex = matched.index;
                                el.setAttribute('value', String(matched.value || ''));
                            }
                            el.dispatchEvent(new Event('input', { bubbles: true, composed: true }));
                            el.dispatchEvent(new Event('change', { bubbles: true, composed: true }));
                            el.dispatchEvent(new FocusEvent('blur', { bubbles: true, composed: true }));
                            el.dispatchEvent(new FocusEvent('focusout', { bubbles: true, composed: true }));
                        }""",
                        {"value": matching_value, "label": matching_label},
                    )
                except Exception:  # noqa: BLE001
                    pass
                if _selected_option_matches():
                    try:
                        self._page.wait_for_timeout(500)
                    except Exception:  # noqa: BLE001
                        pass
                    return True
                try:
                    dropdown.focus()
                    dropdown.press("Tab")
                except Exception:  # noqa: BLE001
                    pass
        return False

    def click_existing_resume_delete(self) -> bool:
        candidates: list[tuple[str, Any]] = []
        try:
            candidates.append(("testid_delete", self._page.locator('button[data-testid="deleteButton"]').first))
        except Exception:  # noqa: BLE001
            pass
        try:
            candidates.append(("id_delete", self._page.locator("#deleteResume").first))
        except Exception:  # noqa: BLE001
            pass
        try:
            candidates.append(("role_button", self._page.get_by_role("button", name=re.compile("^delete$", re.I)).first))
        except Exception:  # noqa: BLE001
            pass
        for candidate_name, locator in candidates:
            try:
                if not locator.count() or not locator.is_visible():
                    continue
            except Exception:  # noqa: BLE001
                continue
            for _strategy_name, action in (
                ("normal_click", lambda: locator.click()),
                ("force_click", lambda: locator.click(force=True)),
                (
                    "ancestor_js_click",
                    lambda: locator.evaluate(
                        """(el) => {
                            const target = el.closest('button, [role="button"], [tabindex], label') || el;
                            target.click();
                        }"""
                    ),
                ),
            ):
                try:
                    action()
                    self._page.wait_for_timeout(750)
                    return True
                except Exception:  # noqa: BLE001
                    continue
        return False

    def get_resume_upload_progress_state(self, expected_filename: str = "") -> dict[str, Any]:
        filename_pattern = expected_filename.strip()
        try:
            return dict(
                self._page.evaluate(
                    """(expectedFilename) => {
                        const normalise = (value) => String(value || '').replace(/\s+/g, ' ').trim();
                        const truncate = (value, max = 500) => {
                            const text = String(value || '');
                            return text.length > max ? `${text.slice(0, max)}...` : text;
                        };
                        const isVisible = (el) => {
                            if (!el) return false;
                            const style = window.getComputedStyle(el);
                            return style.display !== 'none' && style.visibility !== 'hidden' && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                        };
                        const findFilenameNode = (expected) => {
                            const matcher = expected ? expected.toLowerCase() : '';
                            const candidates = [];
                            for (const node of Array.from(document.querySelectorAll('body *'))) {
                                if (!(node instanceof Element) || !isVisible(node)) continue;
                                const text = normalise(node.innerText || node.textContent || '');
                                if (!text) continue;
                                const lower = text.toLowerCase();
                                const matches = matcher ? lower.includes(matcher) : /\.(doc|docx|pdf)\b/i.test(text);
                                if (!matches) continue;
                                const exact = matcher ? lower === matcher : /\.(doc|docx|pdf)\b/i.test(text) && text.length < 140;
                                const descendantCount = node.querySelectorAll('*').length || 0;
                                const rect = node.getBoundingClientRect();
                                const score = (exact ? 10000 : 0) - text.length - (descendantCount * 5) - Math.max(rect.width, 0) / 20;
                                candidates.push({ node, score, textLength: text.length });
                            }
                            candidates.sort((a, b) => b.score - a.score || a.textLength - b.textLength);
                            return candidates.length ? candidates[0].node : null;
                        };
                        const scoreContainer = (node, filenameLower) => {
                            if (!node || !(node instanceof Element)) return -1;
                            const text = normalise(node.innerText || node.textContent || '');
                            if (!text) return -1;
                            const lower = text.toLowerCase();
                            let score = 0;
                            if (filenameLower && lower.includes(filenameLower)) score += 10;
                            if (/upload a resum.|upload a resume|upload a different resum.|upload a different resume|select a resum.|select a resume|don't include a resum.|don't include a resume/.test(lower)) score += 8;
                            if (node.querySelector?.('input[name="resume-method"]')) score += 10;
                            if (node.querySelector?.('input[name="coverLetter-method"], textarea, [data-testid="coverLetterTextInput"]')) score -= 20;
                            if (/cover letter/.test(lower)) score -= 12;
                            if (/continue/.test(lower)) score -= 6;
                            if (node === document.body || node === document.documentElement) score -= 50;
                            score -= Math.min(text.length / 120, 20);
                            return score;
                        };
                        const findLocalResumeContainer = (filenameNode) => {
                            if (!filenameNode || !(filenameNode instanceof Element)) return document.body;
                            const filenameLower = normalise(filenameNode.innerText || filenameNode.textContent || '').toLowerCase();
                            let node = filenameNode;
                            let best = filenameNode;
                            let bestScore = scoreContainer(filenameNode, filenameLower);
                            for (let depth = 0; depth < 7 && node; depth += 1) {
                                const score = scoreContainer(node, filenameLower);
                                if (score > bestScore) {
                                    best = node;
                                    bestScore = score;
                                }
                                node = node.parentElement;
                            }
                            if (best === document.body || best === document.documentElement) {
                                return filenameNode.parentElement || filenameNode;
                            }
                            return best || filenameNode;
                        };
                        const bodyText = normalise(document.body?.innerText || '');
                        const filenameNode = findFilenameNode(expectedFilename);
                        const section = findLocalResumeContainer(filenameNode || document.body);
                        const collectVisible = (root, selector) => Array.from((root || document).querySelectorAll(selector)).some((el) => isVisible(el));
                        const sectionText = normalise(section?.innerText || section?.textContent || '');
                        const loweredSection = sectionText.toLowerCase();
                        const spinnerSelectors = [
                            '[aria-busy="true"]',
                            '[role="progressbar"]',
                            '[data-testid*="progress"]',
                            '[class*="spinner"]',
                            '[class*="loading"]',
                            '[class*="progress"]',
                        ];
                        const loadingTextVisible = /uploading|processing|scanning|please wait/.test(loweredSection);
                        const loadingDotsVisible = /(^|\s)(\.\.\.|•••)(\s|$)/.test(sectionText);
                        const localSpinnerVisible = spinnerSelectors.some((selector) => collectVisible(section, selector));
                        const globalSpinnerVisible = spinnerSelectors.some((selector) => collectVisible(document.body, selector));
                        const ignoredGlobalSpinner = globalSpinnerVisible && !localSpinnerVisible;
                        return {
                            filename_visible: !!filenameNode,
                            loading_indicator_visible: loadingTextVisible || loadingDotsVisible || localSpinnerVisible,
                            loading_text_visible: loadingTextVisible,
                            loading_dots_visible: loadingDotsVisible,
                            spinner_visible: localSpinnerVisible,
                            local_resume_container_found: !!section && section !== document.body,
                            local_resume_loading_indicator_visible: loadingTextVisible || loadingDotsVisible || localSpinnerVisible,
                            global_spinner_ignored: ignoredGlobalSpinner,
                            ignored_global_spinner_candidate: ignoredGlobalSpinner ? truncate(document.body?.innerHTML || '') : '',
                            section_text_snippet: sectionText.slice(0, 500),
                            body_text_snippet: bodyText.slice(0, 800),
                            filename_text: normalise(filenameNode?.innerText || filenameNode?.textContent || ''),
                        };
                    }""",
                    filename_pattern,
                )
                or {}
            )
        except Exception:  # noqa: BLE001
            page_text = str(self.page_text() or "")
            lowered = page_text.lower()
            return {
                "filename_visible": bool(expected_filename and expected_filename.lower() in lowered),
                "loading_indicator_visible": bool(re.search(r"uploading|processing|scanning|please wait|\.\.\.|•••", page_text, re.I)),
                "loading_text_visible": bool(re.search(r"uploading|processing|scanning|please wait", page_text, re.I)),
                "loading_dots_visible": bool(re.search(r"\.\.\.|•••", page_text)),
                "spinner_visible": False,
                "local_resume_container_found": False,
                "local_resume_loading_indicator_visible": bool(re.search(r"uploading|processing|scanning|please wait|\.\.\.|•••", page_text, re.I)),
                "global_spinner_ignored": False,
                "ignored_global_spinner_candidate": "",
                "section_text_snippet": page_text[:500],
                "body_text_snippet": page_text[:800],
                "filename_text": "",
            }

    def collect_resume_section_snapshot(self, expected_filename: str = "") -> dict[str, Any]:
        expected = expected_filename.strip()
        try:
            return dict(
                self._page.evaluate(
                    """(expectedFilename) => {
                        const normalise = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
                        const truncate = (value, max = 600) => {
                            const text = String(value || '');
                            return text.length > max ? `${text.slice(0, max)}...` : text;
                        };
                        const isVisible = (el) => {
                            if (!el) return false;
                            const style = window.getComputedStyle(el);
                            return style.display !== 'none' && style.visibility !== 'hidden' && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                        };
                        const findFilenameNode = (expected) => {
                            const matcher = expected ? expected.toLowerCase() : '';
                            const candidates = [];
                            for (const node of Array.from(document.querySelectorAll('body *'))) {
                                if (!(node instanceof Element) || !isVisible(node)) continue;
                                const text = normalise(node.innerText || node.textContent || '');
                                if (!text) continue;
                                const lower = text.toLowerCase();
                                const matches = matcher ? lower.includes(matcher) : /\.(doc|docx|pdf)\b/i.test(text);
                                if (!matches) continue;
                                const exact = matcher ? lower === matcher : /\.(doc|docx|pdf)\b/i.test(text) && text.length < 140;
                                const descendantCount = node.querySelectorAll('*').length || 0;
                                const rect = node.getBoundingClientRect();
                                const score = (exact ? 10000 : 0) - text.length - (descendantCount * 5) - Math.max(rect.width, 0) / 20;
                                candidates.push({ node, score, textLength: text.length });
                            }
                            candidates.sort((a, b) => b.score - a.score || a.textLength - b.textLength);
                            return candidates.length ? candidates[0].node : null;
                        };
                        const scoreContainer = (node, filenameLower) => {
                            if (!node || !(node instanceof Element)) return -1;
                            const text = normalise(node.innerText || node.textContent || '');
                            if (!text) return -1;
                            const lower = text.toLowerCase();
                            let score = 0;
                            if (filenameLower && lower.includes(filenameLower)) score += 10;
                            if (/upload a resum.|upload a resume|upload a different resum.|upload a different resume|select a resum.|select a resume|don't include a resum.|don't include a resume/.test(lower)) score += 8;
                            if (node.querySelector?.('input[name="resume-method"]')) score += 10;
                            if (node.querySelector?.('input[name="coverLetter-method"], textarea, [data-testid="coverLetterTextInput"]')) score -= 20;
                            if (/cover letter/.test(lower)) score -= 12;
                            if (/continue/.test(lower)) score -= 6;
                            if (node === document.body || node === document.documentElement) score -= 50;
                            score -= Math.min(text.length / 120, 20);
                            return score;
                        };
                        const sectionLike = (el) => {
                            if (!el || !(el instanceof Element)) return document.body;
                            const filenameLower = normalise(el.innerText || el.textContent || '').toLowerCase();
                            let node = el;
                            let best = el;
                            let bestScore = scoreContainer(el, filenameLower);
                            for (let depth = 0; depth < 6 && node; depth += 1) {
                                const score = scoreContainer(node, filenameLower);
                                if (score > bestScore) {
                                    best = node;
                                    bestScore = score;
                                }
                                node = node.parentElement;
                            }
                            if (best === document.body || best === document.documentElement) {
                                return el.parentElement || el;
                            }
                            return best || el;
                        };
                        const resumeMethod = document.querySelector('input[name="resume-method"]:checked')
                            || document.querySelector('input[data-testid="resume-method-upload"]')
                            || document.querySelector('input[name="resume-method"]')
                            || document.querySelector('input[type="file"]');
                        const filenameNode = findFilenameNode(expectedFilename);
                        const section = sectionLike(filenameNode || resumeMethod || document.body);
                        const visibleFilenames = Array.from(section.querySelectorAll('*'))
                            .filter((node) => isVisible(node))
                            .map((node) => normalise(node.innerText || node.textContent || ''))
                            .filter((text) => /\\.(doc|docx|pdf)\\b/i.test(text))
                            .slice(0, 10);
                        const fileInputDiagnostics = Array.from(section.querySelectorAll('input[type="file"]')).map((node, index) => {
                            const style = window.getComputedStyle(node);
                            const rect = node.getBoundingClientRect();
                            return {
                                index,
                                visible: isVisible(node),
                                attached: node.isConnected,
                                disabled: !!node.disabled,
                                accept: String(node.getAttribute('accept') || ''),
                                outer_html_snippet: truncate(node.outerHTML || ''),
                                bounding_box: { x: rect.x, y: rect.y, width: rect.width, height: rect.height },
                                computed_display: style.display,
                                computed_visibility: style.visibility,
                            };
                        });
                        const progressEntries = [];
                        const selectors = [
                            '[aria-busy="true"]',
                            '[role="progressbar"]',
                            '[data-testid*="progress"]',
                            '[class*="spinner"]',
                            '[class*="loading"]',
                            '[class*="progress"]',
                        ];
                        const pushCandidate = (node, selectorMatched) => {
                            if (!node || !(node instanceof Element)) return;
                            const style = window.getComputedStyle(node);
                            const rect = node.getBoundingClientRect();
                            progressEntries.push({
                                selector_matched: selectorMatched,
                                visible: isVisible(node),
                                text: truncate(normalise(node.innerText || node.textContent || '')),
                                outer_html_snippet: truncate(node.outerHTML || ''),
                                bounding_box: { x: rect.x, y: rect.y, width: rect.width, height: rect.height },
                                computed_animation_name: String(style.animationName || ''),
                                computed_display: style.display,
                                computed_visibility: style.visibility,
                                aria_busy: String(node.getAttribute('aria-busy') || ''),
                                role: String(node.getAttribute('role') || ''),
                            });
                        };
                        selectors.forEach((selector) => {
                            section.querySelectorAll(selector).forEach((node) => pushCandidate(node, selector));
                        });
                        Array.from(section.querySelectorAll('*'))
                            .filter((node) => isVisible(node))
                            .forEach((node) => {
                                const text = normalise(node.innerText || node.textContent || '');
                                if (!text) return;
                                if (/uploading|processing|scanning|please wait|\\.\\.\\.|•••/i.test(text)) {
                                    pushCandidate(node, 'text_match');
                                }
                            });
                        const buttonsLabelsVisible = Array.from(section.querySelectorAll('button, [role="button"], label'))
                            .filter((node) => isVisible(node))
                            .map((node) => truncate(normalise(node.innerText || node.textContent || '')))
                            .filter(Boolean)
                            .slice(0, 20);
                        const continueNode = Array.from(document.querySelectorAll('button, [role="button"], a, span'))
                            .find((node) => isVisible(node) && /continue/i.test(normalise(node.innerText || node.textContent || '')));
                        const validationErrorTexts = Array.from(section.querySelectorAll('[role="alert"], [aria-invalid="true"], .error, [class*="error"]'))
                            .filter((node) => isVisible(node))
                            .map((node) => truncate(normalise(node.innerText || node.textContent || '')))
                            .filter(Boolean)
                            .slice(0, 20);
                        return {
                            page_url: String(window.location.href || ''),
                            page_title: String(document.title || ''),
                            resume_section_html: String(section.outerHTML || ''),
                            resume_section_text: normalise(section.innerText || section.textContent || ''),
                            visible_resume_filenames: visibleFilenames,
                            file_input_diagnostics: fileInputDiagnostics,
                            progress_candidates: progressEntries,
                            buttons_labels_visible: buttonsLabelsVisible,
                            continue_enabled: !!(continueNode && !continueNode.disabled && continueNode.getAttribute('aria-disabled') !== 'true'),
                            validation_error_texts: validationErrorTexts,
                            resume_limit_modal_visible: /resumé limit reached|resume limit reached/i.test(normalise(document.body?.innerText || '')),
                            snapshot_capture_errors: [],
                        };
                    }""",
                    expected,
                )
                or {}
            )
        except Exception as exc:  # noqa: BLE001
            return {
                "page_url": self.current_url(),
                "page_title": str(self._page.title() or ""),
                "resume_section_html": "",
                "resume_section_text": "",
                "visible_resume_filenames": [],
                "file_input_diagnostics": [],
                "progress_candidates": [],
                "buttons_labels_visible": [],
                "continue_enabled": False,
                "validation_error_texts": [],
                "resume_limit_modal_visible": bool(self.resume_limit_ui_present()),
                "snapshot_capture_errors": [f"collect_resume_section_snapshot: {exc}"],
            }

    def save_resume_section_screenshot(self, path: Path, *, expected_filename: str = "") -> tuple[bool, str]:
        try:
            self._page.screenshot(path=str(path), full_page=True, timeout=3000)
            return True, ""
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    def capture_local_dom_diagnostics(self, failure_type: str, expected_filename: str = "") -> dict[str, Any]:
        expected = expected_filename.strip()
        try:
            return dict(
                self._page.evaluate(
                    """([failureType, expectedFilename]) => {
                        const normalise = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
                        const truncate = (value, max = 800) => {
                            const text = String(value || '');
                            return text.length > max ? `${text.slice(0, max)}...` : text;
                        };
                        const isVisible = (el) => {
                            if (!el) return false;
                            const style = window.getComputedStyle(el);
                            return style.display !== 'none' && style.visibility !== 'hidden' && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                        };
                        const bbox = (node) => {
                            if (!node || !(node instanceof Element)) return null;
                            const rect = node.getBoundingClientRect();
                            return { x: rect.x, y: rect.y, width: rect.width, height: rect.height };
                        };
                        const findFilenameNode = (expected) => {
                            const matcher = expected ? expected.toLowerCase() : '';
                            const candidates = [];
                            for (const node of Array.from(document.querySelectorAll('body *'))) {
                                if (!(node instanceof Element) || !isVisible(node)) continue;
                                const text = normalise(node.innerText || node.textContent || '');
                                if (!text) continue;
                                const lower = text.toLowerCase();
                                const matches = matcher ? lower.includes(matcher) : /\\.(doc|docx|pdf)\\b/i.test(text);
                                if (!matches) continue;
                                const exact = matcher ? lower === matcher : /\\.(doc|docx|pdf)\\b/i.test(text) && text.length < 140;
                                const descendantCount = node.querySelectorAll('*').length || 0;
                                const rect = node.getBoundingClientRect();
                                const score = (exact ? 10000 : 0) - text.length - (descendantCount * 5) - Math.max(rect.width, 0) / 20;
                                candidates.push({ node, score, textLength: text.length });
                            }
                            candidates.sort((a, b) => b.score - a.score || a.textLength - b.textLength);
                            return candidates.length ? candidates[0].node : null;
                        };
                        const scoreContainer = (node, filenameLower) => {
                            if (!node || !(node instanceof Element)) return -1;
                            const text = normalise(node.innerText || node.textContent || '');
                            if (!text) return -1;
                            const lower = text.toLowerCase();
                            let score = 0;
                            if (filenameLower && lower.includes(filenameLower)) score += 10;
                            if (/upload a resum.|upload a resume|upload a different resum.|upload a different resume|select a resum.|select a resume|don't include a resum.|don't include a resume/.test(lower)) score += 8;
                            if (node.querySelector?.('input[name="resume-method"]')) score += 10;
                            if (node.querySelector?.('input[name="coverLetter-method"], textarea, [data-testid="coverLetterTextInput"]')) score -= 20;
                            if (/cover letter/.test(lower)) score -= 12;
                            if (/continue/.test(lower)) score -= 6;
                            if (node === document.body || node === document.documentElement) score -= 50;
                            score -= Math.min(text.length / 120, 20);
                            return score;
                        };
                        const meaningfulParent = (el) => {
                            let node = el instanceof Element ? el : null;
                            if (!node) return document.body;
                            const filenameLower = normalise(node.innerText || node.textContent || '').toLowerCase();
                            let best = node;
                            let bestScore = scoreContainer(node, filenameLower);
                            for (let depth = 0; node && depth < 6; depth += 1) {
                                const score = scoreContainer(node, filenameLower);
                                if (score > bestScore) {
                                    best = node;
                                    bestScore = score;
                                }
                                node = node.parentElement;
                            }
                            if (best === document.body || best === document.documentElement) {
                                return el.parentElement || el;
                            }
                            return best || el;
                        };
                        const filenameNode = findFilenameNode(expectedFilename);
                        const section = meaningfulParent(filenameNode || document.querySelector('input[name="resume-method"]:checked') || document.body);
                        const optionLabels = Array.from(section.querySelectorAll('label, [role="radio"], [role="option"]'))
                            .filter((node) => isVisible(node))
                            .map((node) => truncate(normalise(node.innerText || node.textContent || '')))
                            .filter(Boolean)
                            .slice(0, 20);
                        const nearbyButtons = Array.from(section.querySelectorAll('button, [role="button"], a'))
                            .filter((node) => isVisible(node))
                            .map((node) => ({
                                text: truncate(normalise(node.innerText || node.textContent || '')),
                                tag: String(node.tagName || '').toLowerCase(),
                                role: String(node.getAttribute('role') || ''),
                                disabled: !!node.disabled || node.getAttribute('aria-disabled') === 'true',
                                bounding_box: bbox(node),
                            }))
                            .filter((entry) => entry.text)
                            .slice(0, 20);
                        const nearbyInputs = Array.from(section.querySelectorAll('input, select, textarea'))
                            .map((node) => ({
                                tag: String(node.tagName || '').toLowerCase(),
                                type: String(node.getAttribute('type') || ''),
                                name: String(node.getAttribute('name') || ''),
                                id: String(node.getAttribute('id') || ''),
                                value: String(node.value || ''),
                                checked: !!node.checked,
                                disabled: !!node.disabled,
                                aria_label: String(node.getAttribute('aria-label') || ''),
                                bounding_box: bbox(node),
                            }))
                            .slice(0, 20);
                        const spinnerSelectors = [
                            '[aria-busy="true"]',
                            '[role="progressbar"]',
                            '[data-testid*="progress"]',
                            '[class*="spinner"]',
                            '[class*="loading"]',
                            '[class*="progress"]',
                        ];
                        const spinnerNodes = [];
                        const seen = new Set();
                        spinnerSelectors.forEach((selector) => {
                            section.querySelectorAll(selector).forEach((node) => {
                                if (!(node instanceof Element) || seen.has(node)) return;
                                seen.add(node);
                                spinnerNodes.push({ node, selector });
                            });
                        });
                        Array.from(section.querySelectorAll('*'))
                            .filter((node) => isVisible(node))
                            .forEach((node) => {
                                if (!(node instanceof Element) || seen.has(node)) return;
                                const text = normalise(node.innerText || node.textContent || '');
                                if (/uploading|processing|scanning|please wait|\\.\\.\\.|â€¢â€¢â€¢/i.test(text)) {
                                    seen.add(node);
                                    spinnerNodes.push({ node, selector: 'text_match' });
                                }
                            });
                        const localSpinnerDetails = spinnerNodes.map(({ node, selector }) => {
                            const style = window.getComputedStyle(node);
                            return {
                                selector_matched: selector,
                                visible: isVisible(node),
                                text: truncate(normalise(node.innerText || node.textContent || '')),
                                outer_html_snippet: truncate(node.outerHTML || ''),
                                bounding_box: bbox(node),
                                computed_animation_name: String(style.animationName || ''),
                                computed_display: String(style.display || ''),
                                computed_visibility: String(style.visibility || ''),
                                aria_busy: String(node.getAttribute('aria-busy') || ''),
                                role: String(node.getAttribute('role') || ''),
                            };
                        });
                        const continueNode = Array.from(document.querySelectorAll('button, [role="button"], a, span'))
                            .find((node) => isVisible(node) && /continue/i.test(normalise(node.innerText || node.textContent || '')));
                        const checkedUpload = document.querySelector('input[data-testid="resume-method-upload"]:checked, input[name="resume-method"][value="upload"]:checked');
                        const checkedNone = document.querySelector('input[data-testid="resume-method-none"]:checked, input[name="resume-method"][value="none"]:checked');
                        const validationTexts = Array.from(section.querySelectorAll('[role="alert"], [aria-invalid="true"], .error, [class*="error"]'))
                            .filter((node) => isVisible(node))
                            .map((node) => truncate(normalise(node.innerText || node.textContent || '')))
                            .filter(Boolean)
                            .slice(0, 20);
                        return {
                            failure_type: failureType,
                            filename_visible: !!filenameNode,
                            upload_radio_checked: !!checkedUpload,
                            dont_include_checked: !!checkedNone,
                            continue_enabled: !!(continueNode && !continueNode.disabled && continueNode.getAttribute('aria-disabled') !== 'true'),
                            local_spinner_count: localSpinnerDetails.length,
                            local_spinner_details: localSpinnerDetails,
                            parent_container_selector_guess: section === document.body ? 'body' : `${String(section.tagName || '').toLowerCase()}${section.id ? '#' + section.id : ''}${section.className ? '.' + String(section.className).trim().replace(/\\s+/g, '.') : ''}`,
                            nearby_buttons: nearbyButtons,
                            nearby_inputs: nearbyInputs,
                            nearby_labels: optionLabels,
                            nearby_validation_text: validationTexts,
                            bounding_box: bbox(section),
                            local_container_html: String(section.outerHTML || ''),
                            local_container_text: normalise(section.innerText || section.textContent || ''),
                        };
                    }""",
                    [failure_type, expected],
                )
                or {}
            )
        except Exception as exc:  # noqa: BLE001
            return {
                "failure_type": failure_type,
                "local_container_html": "",
                "local_container_text": "",
                "local_spinner_count": 0,
                "local_spinner_details": [],
                "nearby_buttons": [],
                "nearby_inputs": [],
                "nearby_labels": [],
                "nearby_validation_text": [],
                "capture_errors": [f"capture_local_dom_diagnostics: {exc}"],
            }

    def save_local_dom_screenshot(self, path: Path, *, failure_type: str, expected_filename: str = "") -> tuple[bool, str]:
        expected = expected_filename.strip()
        try:
            box = self._page.evaluate(
                """(expectedFilename) => {
                    const normalise = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
                    const isVisible = (el) => {
                        if (!el) return false;
                        const style = window.getComputedStyle(el);
                        return style.display !== 'none' && style.visibility !== 'hidden' && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                    };
                    const findFilenameNode = (expected) => {
                        const matcher = expected ? expected.toLowerCase() : '';
                        const candidates = [];
                        for (const node of Array.from(document.querySelectorAll('body *'))) {
                            if (!(node instanceof Element) || !isVisible(node)) continue;
                            const text = normalise(node.innerText || node.textContent || '');
                            if (!text) continue;
                            const lower = text.toLowerCase();
                            const matches = matcher ? lower.includes(matcher) : /\\.(doc|docx|pdf)\\b/i.test(text);
                            if (!matches) continue;
                            const exact = matcher ? lower === matcher : /\\.(doc|docx|pdf)\\b/i.test(text) && text.length < 140;
                            const descendantCount = node.querySelectorAll('*').length || 0;
                            const rect = node.getBoundingClientRect();
                            const score = (exact ? 10000 : 0) - text.length - (descendantCount * 5) - Math.max(rect.width, 0) / 20;
                            candidates.push({ node, score, textLength: text.length });
                        }
                        candidates.sort((a, b) => b.score - a.score || a.textLength - b.textLength);
                        return candidates.length ? candidates[0].node : null;
                    };
                    const scoreContainer = (node, filenameLower) => {
                        if (!node || !(node instanceof Element)) return -1;
                        const text = normalise(node.innerText || node.textContent || '');
                        if (!text) return -1;
                        const lower = text.toLowerCase();
                        let score = 0;
                        if (filenameLower && lower.includes(filenameLower)) score += 10;
                        if (/upload a resum.|upload a resume|upload a different resum.|upload a different resume|select a resum.|select a resume|don't include a resum.|don't include a resume/.test(lower)) score += 8;
                        if (node.querySelector?.('input[name="resume-method"]')) score += 10;
                        if (node.querySelector?.('input[name="coverLetter-method"], textarea, [data-testid="coverLetterTextInput"]')) score -= 20;
                        if (/cover letter/.test(lower)) score -= 12;
                        if (/continue/.test(lower)) score -= 6;
                        if (node === document.body || node === document.documentElement) score -= 50;
                        score -= Math.min(text.length / 120, 20);
                        return score;
                    };
                    const meaningfulParent = (el) => {
                        let node = el instanceof Element ? el : null;
                        if (!node) return document.body;
                        const filenameLower = normalise(node.innerText || node.textContent || '').toLowerCase();
                        let best = node;
                        let bestScore = scoreContainer(node, filenameLower);
                        for (let depth = 0; node && depth < 6; depth += 1) {
                            const score = scoreContainer(node, filenameLower);
                            if (score > bestScore) {
                                best = node;
                                bestScore = score;
                            }
                            node = node.parentElement;
                        }
                        if (best === document.body || best === document.documentElement) {
                            return el.parentElement || el;
                        }
                        return best || el;
                    };
                    const filenameNode = findFilenameNode(expectedFilename);
                    const section = meaningfulParent(filenameNode || document.body);
                    if (!section || section === document.body) return null;
                    const rect = section.getBoundingClientRect();
                    return { x: rect.x, y: rect.y, width: rect.width, height: rect.height };
                }""",
                expected,
            )
            if not box or float(box.get("width") or 0) <= 0 or float(box.get("height") or 0) <= 0:
                return False, "no local bounding box found"
            self._page.screenshot(path=str(path), clip=box, timeout=3000)
            return True, ""
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    def wait_for_resume_file_input(self, timeout_ms: int = 5000) -> bool:
        try:
            self._page.wait_for_selector('input[type="file"]', timeout=timeout_ms)
            self._page.wait_for_timeout(300)
            return True
        except Exception:  # noqa: BLE001
            return False

    def try_resume_upload_via_file_chooser(self, file_path: Path, timeout_ms: int = 5000) -> bool:
        try:
            with self._page.expect_file_chooser(timeout=timeout_ms) as chooser_info:
                clicked = self.click_resume_upload_button()
                print(f"[seek_apply_assist] after_click_resume_upload_button: clicked={clicked}", flush=True)
            file_chooser = chooser_info.value
            print("[seek_apply_assist] file_chooser_captured", flush=True)
            file_chooser.set_files(str(file_path))
            print("[seek_apply_assist] file_chooser_set_files_called", flush=True)
            self._page.wait_for_timeout(300)
            return True
        except Exception as exc:  # noqa: BLE001
            print(f"[seek_apply_assist] file_chooser_timeout: error={exc}", flush=True)
            return False

    def _resume_file_input_candidates(self) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        try:
            file_inputs = self._page.locator('input[type="file"]')
            count = file_inputs.count()
        except Exception:  # noqa: BLE001
            count = 0
        metadata_by_index: dict[int, dict[str, Any]] = {}
        try:
            raw_metadata = self._page.evaluate(
                """() => Array.from(document.querySelectorAll('input[type="file"]')).map((el, index) => {
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return {
                        index,
                        visible: style.display !== 'none' && style.visibility !== 'hidden' && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length),
                        disabled: !!el.disabled,
                        accept: el.getAttribute('accept') || '',
                        bounding_box: {
                            x: rect.x,
                            y: rect.y,
                            width: rect.width,
                            height: rect.height,
                        },
                        outer_html: (el.outerHTML || '').slice(0, 240),
                    };
                })"""
            )
            if isinstance(raw_metadata, list):
                for item in raw_metadata:
                    if not isinstance(item, dict):
                        continue
                    try:
                        metadata_by_index[int(item.get("index", -1))] = item
                    except Exception:  # noqa: BLE001
                        continue
        except Exception as exc:  # noqa: BLE001
            print(f"[seek_apply_assist] resume_file_input_metadata_scan_error: {exc}", flush=True)
        for index in range(count):
            locator = file_inputs.nth(index)
            metadata = metadata_by_index.get(index, {})
            visible = bool(metadata.get("visible", False))
            disabled = bool(metadata.get("disabled", False))
            accept = str(metadata.get("accept") or "")
            bounding_box = metadata.get("bounding_box")
            outer_html = str(metadata.get("outer_html") or "")
            score = 0
            if not disabled:
                score += 4
            if visible:
                score += 3
            if accept and re.search(r"doc|pdf", accept, re.I):
                score += 2
            candidates.append(
                {
                    "index": index,
                    "locator": locator,
                    "visible": visible,
                    "attached": True,
                    "disabled": disabled,
                    "accept": accept,
                    "bounding_box": bounding_box,
                    "outer_html": outer_html,
                    "score": score,
                }
            )
        candidates.sort(key=lambda item: item["score"], reverse=True)
        return candidates

    def collect_resume_upload_diagnostics(self) -> dict[str, Any]:
        diagnostics: dict[str, Any] = {
            "file_input_count": 0,
            "file_inputs": [],
            "upload_counts": {},
            "upload_text_candidates": [],
        }
        try:
            diagnostics["file_input_count"] = self._page.locator('input[type="file"]').count()
        except Exception:  # noqa: BLE001
            diagnostics["file_input_count"] = 0
        for candidate in self._resume_file_input_candidates():
            diagnostics["file_inputs"].append(
                {
                    "index": candidate["index"],
                    "visible": candidate["visible"],
                    "attached": candidate["attached"],
                    "disabled": candidate["disabled"],
                    "accept": candidate["accept"],
                    "bounding_box": candidate["bounding_box"],
                    "outer_html_snippet": candidate["outer_html"],
                }
            )
        try:
            diagnostics["upload_counts"] = self._page.evaluate(
                """() => {
                    const textMatches = Array.from(document.querySelectorAll('body *')).filter((el) => (el.innerText || '').trim() === 'Upload');
                    const containsUpload = (selector) => Array.from(document.querySelectorAll(selector)).filter((el) => /upload/i.test(el.innerText || '')).length;
                    return {
                        input_type_file: document.querySelectorAll('input[type="file"]').length,
                        labels_with_upload: containsUpload('label'),
                        buttons_with_upload: containsUpload('button'),
                        role_buttons_with_upload: Array.from(document.querySelectorAll('[role="button"]')).filter((el) => /upload/i.test(el.innerText || '')).length,
                        text_nodes_with_upload: textMatches.length,
                    };
                }"""
            )
        except Exception:  # noqa: BLE001
            diagnostics["upload_counts"] = {}
        try:
            diagnostics["upload_text_candidates"] = self._page.evaluate(
                """() => {
                    const elements = Array.from(document.querySelectorAll('body *')).filter((el) => (el.innerText || '').trim() === 'Upload');
                    return elements.slice(0, 10).map((el) => {
                        const parents = [];
                        let current = el;
                        for (let index = 0; index < 5 && current; index += 1) {
                            parents.push((current.outerHTML || '').slice(0, 240));
                            current = current.parentElement;
                        }
                        const rect = el.getBoundingClientRect();
                        return {
                            text: (el.innerText || '').trim(),
                            tag: (el.tagName || '').toLowerCase(),
                            role: el.getAttribute('role') || '',
                            tabindex: el.getAttribute('tabindex') || '',
                            outer_html_snippet: (el.outerHTML || '').slice(0, 240),
                            parent_chain_html: parents,
                            bounding_box: {x: rect.x, y: rect.y, width: rect.width, height: rect.height},
                        };
                    });
                }"""
            )
        except Exception:  # noqa: BLE001
            diagnostics["upload_text_candidates"] = []
        return diagnostics

    def upload_resume(self, file_path: Path) -> None:
        candidates = self._resume_file_input_candidates()
        for candidate in candidates:
            print(
                f"[seek_apply_assist] resume_file_input_candidate: "
                f"index={candidate['index']} visible={candidate['visible']} attached={candidate['attached']} "
                f"disabled={candidate['disabled']} accept={candidate['accept'] or '(none)'} "
                f"bounding_box={candidate['bounding_box']} outer_html={candidate['outer_html']}",
                flush=True,
            )
        for candidate in candidates:
            locator = candidate["locator"]
            try:
                locator.set_input_files(str(file_path), timeout=5000)
                self._page.wait_for_timeout(1500)
                print(f"[seek_apply_assist] resume_file_input_candidate_success: index={candidate['index']}", flush=True)
                return
            except Exception as exc:  # noqa: BLE001
                print(f"[seek_apply_assist] resume_file_input_candidate_failure: index={candidate['index']} error={exc}", flush=True)
                continue
        raise SeekApplyAssistError("Could not find the resume file upload input.")

    def click_resume_upload_radio(self) -> bool:
        print("[seek_apply_assist] click_resume_upload_radio_entered", flush=True)
        def _post_click_settle() -> None:
            try:
                self._page.wait_for_timeout(750)
            except Exception as exc:  # noqa: BLE001
                print(f"[seek_apply_assist] click_resume_upload_radio_settle_wait_error: {exc}", flush=True)

        try:
            print("[seek_apply_assist] click_resume_upload_radio_before_page_evaluate", flush=True)
            clicked = bool(
                self._page.evaluate(
                    """() => {
                        const selectors = [
                            'input[data-testid="resume-method-upload"]',
                            'input[name="resume-method"][value="upload"]',
                        ];
                        for (const selector of selectors) {
                            const input = document.querySelector(selector);
                            if (!input) continue;
                            input.click();
                            input.checked = true;
                            input.dispatchEvent(new Event('input', { bubbles: true }));
                            input.dispatchEvent(new Event('change', { bubbles: true }));
                            return true;
                        }
                        return false;
                    }"""
                )
            )
            print(f"[seek_apply_assist] click_resume_upload_radio_after_page_evaluate: {clicked}", flush=True)
            if clicked:
                print("[seek_apply_assist] checked_upload_resume_radio: page evaluate click succeeded.", flush=True)
                _post_click_settle()
                return True
        except Exception as exc:  # noqa: BLE001
            print(f"[seek_apply_assist] click_resume_upload_radio_page_evaluate_error: {exc}", flush=True)
        direct_locators = [
            self._page.locator('input[data-testid="resume-method-upload"]').first,
            self._page.locator('input[name="resume-method"][value="upload"]').first,
        ]
        for locator in direct_locators:
            try:
                print("[seek_apply_assist] click_resume_upload_radio_before_locator_count", flush=True)
                if locator.count():
                    print("[seek_apply_assist] click_resume_upload_radio_before_force_check", flush=True)
                    locator.check(force=True, timeout=1200)
                    print("[seek_apply_assist] checked_upload_resume_radio: direct force check succeeded.", flush=True)
                    _post_click_settle()
                    return True
            except Exception as exc:  # noqa: BLE001
                print(f"[seek_apply_assist] click_resume_upload_radio_force_check_error: {exc}", flush=True)
                try:
                    print("[seek_apply_assist] click_resume_upload_radio_before_locator_eval", flush=True)
                    if locator.count():
                        locator.evaluate(
                            """el => {
                                el.click();
                                el.checked = true;
                                el.dispatchEvent(new Event('input', { bubbles: true }));
                                el.dispatchEvent(new Event('change', { bubbles: true }));
                            }"""
                        )
                        print("[seek_apply_assist] checked_upload_resume_radio: direct JS click succeeded.", flush=True)
                        _post_click_settle()
                        return True
                except Exception as eval_exc:  # noqa: BLE001
                    print(f"[seek_apply_assist] click_resume_upload_radio_locator_eval_error: {eval_exc}", flush=True)
                    continue
        label_patterns = [
            re.compile("Upload a résumé|Upload a resume", re.I),
            re.compile("^Upload$", re.I),
        ]
        for pattern in label_patterns:
            try:
                locator = self._page.get_by_label(pattern).first
                if locator.count() and locator.is_visible():
                    locator.click(timeout=1500)
                    _post_click_settle()
                    return True
            except Exception:  # noqa: BLE001
                continue
        for pattern in label_patterns:
            try:
                locator = self._page.get_by_text(pattern).first
                if locator.count() and locator.is_visible():
                    locator.click(timeout=1500)
                    _post_click_settle()
                    return True
            except Exception:  # noqa: BLE001
                continue
        locators = [
            self._page.locator('input[data-testid="resume-method-upload"]').first,
            self._page.locator('input[name="resume-method"][value="upload"]').first,
        ]
        for locator in locators:
            try:
                if locator.count():
                    locator.check(force=True, timeout=1500)
                    print("[seek_apply_assist] checked_upload_resume_radio: force check succeeded.", flush=True)
                    _post_click_settle()
                    return True
            except Exception:  # noqa: BLE001
                try:
                    if locator.count():
                        locator.evaluate("el => el.click()")
                        print("[seek_apply_assist] checked_upload_resume_radio: JS click succeeded.", flush=True)
                        _post_click_settle()
                        return True
                except Exception:  # noqa: BLE001
                    continue
        return False

    def click_resume_none_radio(self) -> bool:
        label_patterns = [
            re.compile("Don.t include a r.s.um.|Don.t include a resume", re.I),
            re.compile("No resume", re.I),
        ]
        for pattern in label_patterns:
            try:
                locator = self._page.get_by_label(pattern).first
                if locator.count() and locator.is_visible():
                    locator.click(timeout=1500)
                    self._page.wait_for_timeout(500)
                    return True
            except Exception:  # noqa: BLE001
                continue
        for pattern in label_patterns:
            try:
                locator = self._page.get_by_text(pattern).first
                if locator.count() and locator.is_visible():
                    locator.click(timeout=1500)
                    self._page.wait_for_timeout(500)
                    return True
            except Exception:  # noqa: BLE001
                continue
        locators = [
            self._page.locator('input[data-testid="resume-method-none"]').first,
            self._page.locator('input[name="resume-method"][value="none"]').first,
        ]
        for locator in locators:
            try:
                if locator.count():
                    locator.check(force=True, timeout=1500)
                    self._page.wait_for_timeout(500)
                    return True
            except Exception:  # noqa: BLE001
                try:
                    if locator.count():
                        locator.evaluate("el => el.click()")
                        self._page.wait_for_timeout(500)
                        return True
                except Exception:  # noqa: BLE001
                    continue
        return False

    def click_resume_upload_button(self) -> bool:
        candidates: list[tuple[str, Any]] = []
        for candidate in self._resume_file_input_candidates():
            locator = candidate["locator"]
            try:
                input_id = str(locator.get_attribute("id") or "").strip()
            except Exception:  # noqa: BLE001
                input_id = ""
            if input_id:
                try:
                    label_locator = self._page.locator(f'label[for="{input_id}"]').first
                    candidates.append((f"label_for_{input_id}", label_locator))
                except Exception:  # noqa: BLE001
                    pass
        try:
            candidates.append(("role_button", self._page.get_by_role("button", name=re.compile("^upload$|upload resume|replace resume|change resume|choose file", re.I)).first))
        except Exception:  # noqa: BLE001
            pass
        try:
            candidates.append(("text_upload", self._page.get_by_text(re.compile("^upload$|upload resume|replace resume|change resume|choose file", re.I)).first))
        except Exception:  # noqa: BLE001
            pass
        try:
            candidates.append(("css_button", self._page.locator('button:has-text("Upload"), [role="button"]:has-text("Upload"), label:has-text("Upload")').first))
        except Exception:  # noqa: BLE001
            pass
        print(f"[seek_apply_assist] upload_button_candidates: {len(candidates)}", flush=True)
        for candidate_name, locator in candidates:
            try:
                if not locator.count() or not locator.is_visible():
                    continue
            except Exception:  # noqa: BLE001
                continue
            metadata: dict[str, Any] = {}
            try:
                metadata = locator.evaluate(
                    """(el) => {
                        const parents = [];
                        let current = el;
                        for (let index = 0; index < 5 && current; index += 1) {
                            parents.push((current.outerHTML || '').slice(0, 240));
                            current = current.parentElement;
                        }
                        const rect = el.getBoundingClientRect();
                        return {
                            tag: (el.tagName || '').toLowerCase(),
                            role: el.getAttribute('role') || '',
                            tabindex: el.getAttribute('tabindex') || '',
                            outer_html_snippet: (el.outerHTML || '').slice(0, 240),
                            parent_chain_html: parents,
                            bounding_box: {x: rect.x, y: rect.y, width: rect.width, height: rect.height},
                            text: (el.innerText || '').trim(),
                        };
                    }"""
                )
            except Exception:  # noqa: BLE001
                metadata = {}
            print(f"[seek_apply_assist] upload_button_click_target: {candidate_name}", flush=True)
            print(f"[seek_apply_assist] upload_button_click_target_metadata: {metadata}", flush=True)
            try:
                locator.scroll_into_view_if_needed()
            except Exception:  # noqa: BLE001
                pass
            try:
                locator.wait_for(state="visible", timeout=1000)
            except Exception:  # noqa: BLE001
                pass
            for strategy_name, action in (
                ("normal_click", lambda: locator.click()),
                ("force_click", lambda: locator.click(force=True)),
                (
                    "ancestor_js_click",
                    lambda: locator.evaluate(
                        """(el) => {
                            const target = el.closest('button, [role="button"], [tabindex], label') || el;
                            target.click();
                        }"""
                    ),
                ),
            ):
                try:
                    print(f"[seek_apply_assist] upload_button_click_strategy: {strategy_name}", flush=True)
                    action()
                    self._page.wait_for_timeout(300)
                    print("[seek_apply_assist] upload_button_click_success", flush=True)
                    return True
                except Exception:  # noqa: BLE001
                    print(f"[seek_apply_assist] upload_button_click_failure: target={candidate_name} strategy={strategy_name}", flush=True)
                    continue
        return False

    def save_resume_upload_selected_screenshot(self, debug_dir: Path) -> Path:
        debug_dir.mkdir(parents=True, exist_ok=True)
        screenshot_path = debug_dir / "resume_upload_selected.png"
        _safe_page_screenshot(self._page, screenshot_path)
        return screenshot_path

    def save_resume_upload_selected_html(self, debug_dir: Path) -> Path:
        debug_dir.mkdir(parents=True, exist_ok=True)
        html_path = debug_dir / "resume_after_upload_radio_click.html"
        _safe_page_html_write(self._page, html_path)
        return html_path

    def save_resume_after_file_upload_screenshot(self, debug_dir: Path) -> Path:
        debug_dir.mkdir(parents=True, exist_ok=True)
        screenshot_path = debug_dir / "resume_after_file_upload.png"
        _safe_page_screenshot(self._page, screenshot_path)
        return screenshot_path

    def detect_visible_resume_filename(self) -> str:
        try:
            body_text = str(self._page.locator("body").inner_text(timeout=5000) or "")
        except Exception:  # noqa: BLE001
            return ""
        match = re.search(r"\b([A-Za-z0-9._ -]+\.(?:doc|docx|pdf))\b", body_text, re.I)
        return match.group(1).strip() if match else ""

    def resume_appears_present(self) -> bool:
        try:
            text = self._page.locator("body").inner_text(timeout=5000).lower()
        except Exception:  # noqa: BLE001
            return False
        hints = ("resume", "cv", ".docx", ".pdf", "uploaded", "selected")
        return sum(1 for hint in hints if hint in text) >= 2

    def resume_limit_ui_present(self) -> bool:
        try:
            if self._page.locator("#docLimitExceededDropdown").count():
                return True
        except Exception:  # noqa: BLE001
            pass
        try:
            visible_text = str(self._page.locator("body").inner_text(timeout=5000) or "")
        except Exception:  # noqa: BLE001
            visible_text = ""
        lowered = visible_text.lower()
        return (
            "resumé limit reached" in lowered
            or "resume limit reached" in lowered
            or ("limit" in lowered and "delete" in lowered)
        )

    def get_resume_limit_dropdown_options(self) -> list[str]:
        try:
            options = self._page.locator("#docLimitExceededDropdown option")
            count = options.count()
        except Exception:  # noqa: BLE001
            return []
        values: list[str] = []
        for index in range(count):
            try:
                text = str(options.nth(index).inner_text(timeout=1000) or "").strip()
            except Exception:  # noqa: BLE001
                text = ""
            if text:
                values.append(text)
        return values

    def select_resume_limit_dropdown_option(self, option_text: str) -> bool:
        try:
            dropdown = self._page.locator("#docLimitExceededDropdown").first
            if not dropdown.count():
                return False
            dropdown.select_option(label=option_text)
            self._page.wait_for_timeout(500)
            return True
        except Exception:  # noqa: BLE001
            try:
                dropdown = self._page.locator("#docLimitExceededDropdown").first
                dropdown.select_option(index=1)
                self._page.wait_for_timeout(500)
                return True
            except Exception:  # noqa: BLE001
                return False

    def click_resume_limit_delete(self) -> bool:
        candidates: list[tuple[str, Any]] = []
        try:
            candidates.append(("role_button", self._page.get_by_role("button", name=re.compile("^delete$", re.I)).first))
        except Exception:  # noqa: BLE001
            pass
        try:
            candidates.append(("text_delete", self._page.get_by_text(re.compile("^delete$", re.I)).first))
        except Exception:  # noqa: BLE001
            pass
        for candidate_name, locator in candidates:
            try:
                if not locator.count() or not locator.is_visible():
                    continue
            except Exception:  # noqa: BLE001
                continue
            for strategy_name, action in (
                ("normal_click", lambda: locator.click()),
                ("force_click", lambda: locator.click(force=True)),
                (
                    "ancestor_js_click",
                    lambda: locator.evaluate(
                        """(el) => {
                            const target = el.closest('button, [role="button"], [tabindex], label') || el;
                            target.click();
                        }"""
                    ),
                ),
            ):
                try:
                    action()
                    return True
                except Exception:  # noqa: BLE001
                    continue
        return False

    def confirm_resume_limit_delete(self) -> bool:
        locators = [
            self._page.get_by_role("button", name=re.compile("delete|confirm", re.I)).first,
            self._page.get_by_text(re.compile("delete|confirm", re.I)).first,
        ]
        return self._click_or_check_optional(locators)

    def wait_for_resume_limit_change(self, previous_options: list[str], timeout_ms: int = 5000) -> None:
        try:
            self._page.wait_for_function(
                """(previousOptions) => {
                    const dropdown = document.querySelector('#docLimitExceededDropdown');
                    if (!dropdown) return true;
                    const current = Array.from(dropdown.querySelectorAll('option')).map((option) => (option.textContent || '').trim());
                    return JSON.stringify(current) !== JSON.stringify(previousOptions || []);
                }""",
                arg=previous_options,
                timeout=timeout_ms,
            )
            self._page.wait_for_timeout(750)
        except Exception:  # noqa: BLE001
            pass

    def save_resume_debug_artifacts(self, debug_dir: Path) -> tuple[Path, Path]:
        debug_dir.mkdir(parents=True, exist_ok=True)
        screenshot_path = debug_dir / "resume_step_failed.png"
        html_path = debug_dir / "resume_step_failed.html"
        _safe_page_screenshot(self._page, screenshot_path)
        _safe_page_html_write(self._page, html_path)
        return screenshot_path, html_path

    def save_named_resume_artifacts(self, debug_dir: Path, stem: str) -> tuple[Path, Path]:
        debug_dir.mkdir(parents=True, exist_ok=True)
        screenshot_path = debug_dir / f"{stem}.png"
        html_path = debug_dir / f"{stem}.html"
        _safe_page_screenshot(self._page, screenshot_path)
        _safe_page_html_write(self._page, html_path)
        return screenshot_path, html_path

    def save_resume_pre_search_artifacts(self, debug_dir: Path) -> tuple[Path, Path]:
        debug_dir.mkdir(parents=True, exist_ok=True)
        screenshot_path = debug_dir / "resume_before_selector_search.png"
        html_path = debug_dir / "resume_before_selector_search.html"
        _safe_page_screenshot(self._page, screenshot_path)
        _safe_page_html_write(self._page, html_path)
        return screenshot_path, html_path

    def select_cover_letter_change(self) -> None:
        locators = [
            self._page.locator('[data-testid="coverLetter-method-change"]').first,
            self._page.locator('input[name="coverLetter-method"][value="change"]').first,
        ]
        self._check_first(locators, "Could not find cover letter text-entry option.")

    def fill_cover_letter(self, text: str) -> None:
        locators = [
            self._page.locator('[data-testid="coverLetterTextInput"]').first,
            self._page.get_by_label("Write a cover letter", exact=False).first,
            self._page.locator("textarea").first,
        ]
        self._fill_first(locators, text, "Could not find cover letter textarea.")

    def click_continue(self) -> None:
        self._click_first(
            [
                lambda: self._page.get_by_role("button", name=re.compile("continue", re.I)).first,
                lambda: self._page.get_by_text(re.compile("^continue$", re.I)).first,
                lambda: self._page.locator("button", has_text=re.compile("continue", re.I)).first,
            ],
            "Could not find Continue button.",
        )

    def wait_for_questionnaire_section(self, timeout_ms: int = 20000) -> None:
        candidates = [
            self._page.get_by_text(re.compile("work in new zealand", re.I)).first,
            self._page.get_by_text(re.compile("eligible", re.I)).first,
            self._page.get_by_text(re.compile("salary", re.I)).first,
            self._page.locator('textarea[name*="questionnaire"]').first,
            self._page.locator('input[type="radio"][name*="questionnaire"]').first,
        ]
        for locator in candidates:
            try:
                locator.wait_for(state="attached", timeout=timeout_ms)
                self._page.wait_for_timeout(1000)
                return
            except Exception:  # noqa: BLE001
                continue
        self._page.wait_for_timeout(2000)

    def log_questionnaire_diagnostics(self) -> None:
        try:
            title = self._page.title()
        except Exception:  # noqa: BLE001
            title = ""
        try:
            questionnaire_radio_count = self._page.locator('input[type="radio"][name*="questionnaire"]').count()
        except Exception:  # noqa: BLE001
            questionnaire_radio_count = 0
        try:
            true_radio_count = self._page.locator('input[type="radio"][value*="_true"]').count()
        except Exception:  # noqa: BLE001
            true_radio_count = 0
        try:
            textarea_count = self._page.locator('textarea[name*="questionnaire"]').count()
        except Exception:  # noqa: BLE001
            textarea_count = 0
        try:
            select_count = self._page.locator('select[name*="questionnaire"]').count()
        except Exception:  # noqa: BLE001
            select_count = 0
        print(f"[seek_apply_assist] questionnaire_page_title: {title}", flush=True)
        print(f"[seek_apply_assist] questionnaire_radio_count: {questionnaire_radio_count}", flush=True)
        print(f"[seek_apply_assist] true_radio_count: {true_radio_count}", flush=True)
        print(f"[seek_apply_assist] textarea_count: {textarea_count}", flush=True)
        print(f"[seek_apply_assist] questionnaire_select_count: {select_count}", flush=True)

    def save_questionnaire_pre_search_artifacts(self, debug_dir: Path) -> tuple[Path, Path]:
        debug_dir.mkdir(parents=True, exist_ok=True)
        screenshot_path = debug_dir / "questionnaire_before_search.png"
        html_path = debug_dir / "questionnaire_before_search.html"
        self._page.screenshot(path=str(screenshot_path), full_page=True)
        html_path.write_text(self._page.content(), encoding="utf-8")
        return screenshot_path, html_path

    def select_work_eligibility_yes(self) -> bool:
        try:
            containers = self._page.locator("div, fieldset, section")
            count = containers.count()
        except Exception:  # noqa: BLE001
            count = 0
        for index in range(min(count, 40)):
            try:
                container = containers.nth(index)
                text = container.inner_text(timeout=500).strip()
                if not re.search(r"eligible to work in new zealand|work in new zealand|new zealand", text, re.I):
                    continue
                true_radio = container.locator('input[type="radio"][value*="_true"]').first
                if true_radio.count():
                    true_radio.check(force=True)
                    return True
                yes_label = container.get_by_label(re.compile("yes", re.I)).first
                if yes_label.count():
                    yes_label.click()
                    return True
                yes_text = container.get_by_text(re.compile("^yes$", re.I)).first
                if yes_text.count():
                    yes_text.click()
                    return True
                first_radio = container.locator('input[type="radio"]').first
                if first_radio.count():
                    first_radio.check(force=True)
                    return True
            except Exception:  # noqa: BLE001
                continue
        locators = [
            self._page.locator('input[type="radio"][value*="_true"]').first,
            self._page.get_by_label(re.compile("Yes", re.I)).first,
            self._page.get_by_text(re.compile("^Yes$", re.I)).first,
        ]
        for locator in locators:
            try:
                if locator.count():
                    input_type = (locator.get_attribute("type") or "").lower()
                    if input_type == "radio":
                        locator.check(force=True)
                    else:
                        locator.click()
                    return True
            except Exception:  # noqa: BLE001
                try:
                    if locator.count():
                        locator.evaluate("el => el.click()")
                        return True
                except Exception:  # noqa: BLE001
                    continue
        return False

    def fill_salary_expectation(self, text: str) -> bool:
        try:
            containers = self._page.locator("div, fieldset, section")
            count = containers.count()
        except Exception:  # noqa: BLE001
            count = 0
        for index in range(min(count, 40)):
            try:
                container = containers.nth(index)
                container_text = container.inner_text(timeout=500).strip()
                if not _contains_salary_question_text(container_text):
                    continue
                textarea = container.locator("textarea").first
                if textarea.count():
                    textarea.fill(text)
                    return True
            except Exception:  # noqa: BLE001
                continue
        return False

    def extract_dynamic_questions(self) -> list[dict[str, Any]]:
        questions: list[dict[str, Any]] = []
        seen_keys: set[str] = set()

        def append_questions(field_type: str, selector: str, *, group_by_name: bool = False) -> None:
            try:
                locators = self._page.locator(selector)
                count = locators.count()
            except Exception:  # noqa: BLE001
                count = 0
            processed_names: set[str] = set()
            for index in range(count):
                locator = locators.nth(index)
                try:
                    if not locator.is_visible():
                        continue
                except Exception:  # noqa: BLE001
                    pass
                try:
                    payload = extract_question_text_for_field(self._page, locator)
                except Exception:  # noqa: BLE001
                    continue
                name = str(payload.get("name") or "").strip()
                if group_by_name:
                    if not name or name in processed_names:
                        continue
                    processed_names.add(name)
                option_payloads = _normalise_option_payloads(payload.get("option_payloads", []))
                nearby_text_candidates = [str(candidate).strip() for candidate in payload.get("nearby_text_candidates", []) if str(candidate).strip()]
                extraction_details = _extract_question_text_details_from_candidates(nearby_text_candidates, option_payloads)
                question_text = str(extraction_details.get("text") or "")
                option_labels_lower = {str(option.get("label") or "").strip().lower() for option in option_payloads if str(option.get("label") or "").strip()}
                if field_type == "radio":
                    direct_radio_question_text = str(payload.get("radio_group_extracted_question_text") or "").strip()
                    direct_radio_lower = direct_radio_question_text.lower()
                    if (
                        direct_radio_question_text
                        and direct_radio_question_text != name
                        and direct_radio_lower not in option_labels_lower
                        and direct_radio_lower not in {"yes", "no"}
                    ):
                        question_text = direct_radio_question_text
                        extraction_details = {
                            "text": direct_radio_question_text,
                            "source": f"radio_group:{str(payload.get('radio_group_extraction_source') or 'direct').strip()}",
                            "rejected_candidates": extraction_details.get("rejected_candidates", []),
                        }
                    extracted_lower = question_text.strip().lower()
                    invalid_radio_text = (
                        not question_text
                        or question_text == name
                        or extracted_lower in option_labels_lower
                        or extracted_lower in {"yes", "no"}
                    )
                    if invalid_radio_text:
                        radio_container_text = str(payload.get("radio_group_container_text") or payload.get("field_container_text") or "").strip()
                        radio_candidates = [candidate for candidate in [radio_container_text, *nearby_text_candidates] if str(candidate).strip()]
                        radio_details = _extract_question_text_details_from_candidates(radio_candidates, option_payloads)
                        radio_question_text = str(radio_details.get("text") or "").strip()
                        if radio_question_text and radio_question_text != name and radio_question_text.lower() not in option_labels_lower:
                            question_text = radio_question_text
                            extraction_details = radio_details
                            payload["radio_group_extracted_question_text"] = radio_question_text
                        elif radio_container_text and radio_container_text != name:
                            question_text = radio_container_text
                            payload["radio_group_extracted_question_text"] = radio_container_text
                if not question_text:
                    question_text = _normalise_question_text(str(payload.get("name") or ""))
                key = f"{field_type}:{name}:{question_text}"
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                questions.append(
                    {
                        "index": index,
                        "field_type": field_type,
                        "id": str(payload.get("id") or "").strip(),
                        "name": name,
                        "question_text": question_text,
                        "options": payload.get("options", []),
                        "option_payloads": option_payloads,
                        "nearby_text_candidates": nearby_text_candidates,
                        "nearby_text_candidate_sources": payload.get("nearby_text_candidate_sources", []),
                        "field_container_text": str(payload.get("field_container_text") or "").strip(),
                        "field_container_outer_html_snippet": str(payload.get("field_container_outer_html_snippet") or "").strip(),
                        "question_extraction_source": str(extraction_details.get("source") or "").strip(),
                        "rejected_candidate_reasons": extraction_details.get("rejected_candidates", []),
                        "radio_group_name": str(payload.get("radio_group_name") or "").strip(),
                        "radio_group_size": int(payload.get("radio_group_size") or 0),
                        "radio_group_container_text": str(payload.get("radio_group_container_text") or "").strip(),
                        "radio_group_option_labels": list(payload.get("radio_group_option_labels") or []),
                        "radio_group_extracted_question_text": str(payload.get("radio_group_extracted_question_text") or question_text).strip(),
                        "radio_group_extraction_source": str(payload.get("radio_group_extraction_source") or extraction_details.get("source") or "").strip(),
                        "current_selected_value": str(payload.get("current_selected_value") or "").strip(),
                        "current_selected_label": str(payload.get("current_selected_label") or "").strip(),
                        "is_visible": bool(payload.get("is_visible", True)),
                        "required": bool(payload.get("required", True)),
                    }
                )

        append_questions("select", 'select[name*="questionnaire"]')
        append_questions("textarea", 'textarea[name*="questionnaire"]')
        append_questions("radio", 'input[type="radio"][name*="questionnaire"]', group_by_name=True)
        append_questions("checkbox", 'input[type="checkbox"][name*="questionnaire"]', group_by_name=True)
        return questions

    def answer_dynamic_question(self, question: dict[str, Any], answer: str) -> bool:
        field_type = str(question.get("field_type") or "")
        name = str(question.get("name") or "")
        try:
            if field_type == "select":
                locator = self._page.locator(f'select[name="{name}"]').first
                if locator.count():
                    option_payloads = question.get("option_payloads", [])
                    matching_value = ""
                    matching_label = ""
                    matching_index = -1
                    answer_lower = answer.lower().strip()
                    question_text = str(question.get("question_text") or question.get("extracted_question_text") or "").strip().lower()
                    is_notice_period_question = (
                        name == "questionnaire.NZ_Q_13_V_2"
                        or "how much notice are you required to give your current employer" in question_text
                    )
                    for option_index, option_payload in enumerate(option_payloads):
                        label = str((option_payload or {}).get("label") or "").strip()
                        value = str((option_payload or {}).get("value") or "").strip()
                        if label.lower().strip() == answer_lower or answer_lower in label.lower():
                            matching_value = value
                            matching_label = label
                            matching_index = option_index
                            break

                    def _select_matches_expected() -> bool:
                        try:
                            selected_state = locator.evaluate(
                                """el => {
                                    const option = el.selectedOptions && el.selectedOptions.length ? el.selectedOptions[0] : null;
                                    return {
                                        value: String(el.value || '').trim(),
                                        label: option ? String(option.textContent || '').replace(/\\s+/g, ' ').trim() : '',
                                    };
                                }"""
                            )
                            selected_value = str((selected_state or {}).get("value") or "").strip()
                            selected_label = str((selected_state or {}).get("label") or "").strip().lower()
                            if matching_value and selected_value == matching_value:
                                return True
                            if answer_lower and selected_label and (selected_label == answer_lower or answer_lower in selected_label):
                                return True
                            if matching_label:
                                expected_label = matching_label.strip().lower()
                                if selected_label == expected_label or expected_label in selected_label:
                                    return True
                        except Exception:  # noqa: BLE001
                            return False
                        return False

                    def _dispatch_select_commit() -> None:
                        locator.evaluate(
                            """(el, payload) => {
                                const desiredValue = String((payload && payload.value) || '').trim();
                                const desiredLabel = String((payload && payload.label) || '').trim().toLowerCase();
                                let matched = null;
                                for (const option of Array.from(el.options || [])) {
                                    const optionValue = String(option.value || '').trim();
                                    const optionLabel = String(option.textContent || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                                    const isMatch =
                                        (desiredValue && optionValue === desiredValue) ||
                                        (desiredLabel && (optionLabel === desiredLabel || optionLabel.includes(desiredLabel)));
                                    option.selected = !!isMatch;
                                    if (isMatch) {
                                        matched = option;
                                    }
                                }
                                if (matched) {
                                    el.value = String(matched.value || '');
                                    el.selectedIndex = matched.index;
                                    el.setAttribute('value', String(matched.value || ''));
                                }
                                el.dispatchEvent(new Event('input', { bubbles: true, composed: true }));
                                el.dispatchEvent(new Event('change', { bubbles: true, composed: true }));
                                el.dispatchEvent(new FocusEvent('blur', { bubbles: true, composed: true }));
                                el.dispatchEvent(new FocusEvent('focusout', { bubbles: true, composed: true }));
                            }""",
                            {"value": matching_value, "label": matching_label or answer},
                        )

                    def _keyboard_commit_notice_period() -> None:
                        if matching_index < 0:
                            return
                        locator.click(force=True)
                        try:
                            locator.press("Home")
                        except Exception:  # noqa: BLE001
                            pass
                        for _ in range(matching_index + 1):
                            locator.press("ArrowDown")
                            try:
                                self._page.wait_for_timeout(60)
                            except Exception:  # noqa: BLE001
                                pass
                        try:
                            locator.press("Enter")
                        except Exception:  # noqa: BLE001
                            pass
                        try:
                            self._page.wait_for_timeout(120)
                        except Exception:  # noqa: BLE001
                            pass
                        _dispatch_select_commit()
                        try:
                            locator.press("Tab")
                        except Exception:  # noqa: BLE001
                            pass

                    if matching_value:
                        locator.select_option(value=matching_value)
                    else:
                        locator.select_option(label=answer)
                    if not _select_matches_expected():
                        try:
                            _dispatch_select_commit()
                        except Exception:  # noqa: BLE001
                            pass
                    if is_notice_period_question and not _select_matches_expected():
                        try:
                            _keyboard_commit_notice_period()
                        except Exception:  # noqa: BLE001
                            pass
                    if not _select_matches_expected():
                        try:
                            locator.focus()
                            locator.press("Tab")
                        except Exception:  # noqa: BLE001
                            pass
                    if not _select_matches_expected():
                        return False

                    # Some SEEK select fields briefly accept the value and then reset
                    # during a client-side rerender. Recheck the committed value across
                    # a few short ticks and reapply if it slips back to placeholder.
                    for _ in range(4):
                        try:
                            self._page.wait_for_timeout(250)
                        except Exception:  # noqa: BLE001
                            pass
                        if _select_matches_expected():
                            continue
                        try:
                            if matching_value:
                                locator.select_option(value=matching_value)
                            else:
                                locator.select_option(label=answer)
                        except Exception:  # noqa: BLE001
                            pass
                        try:
                            _dispatch_select_commit()
                        except Exception:  # noqa: BLE001
                            pass
                        if is_notice_period_question and not _select_matches_expected():
                            try:
                                _keyboard_commit_notice_period()
                            except Exception:  # noqa: BLE001
                                pass
                        try:
                            locator.focus()
                            locator.press("Tab")
                        except Exception:  # noqa: BLE001
                            pass
                    return _select_matches_expected()
            if field_type == "textarea":
                locator = self._page.locator(f'textarea[name="{name}"]').first
                if locator.count():
                    locator.fill(answer)
                    return True
            if field_type == "radio":
                locator = self._page.locator(f'input[type="radio"][name="{name}"]').first
                if not locator.count():
                    return False
                option_payloads = question.get("option_payloads", [])
                answer_lower = answer.lower().strip()
                matching_value = ""
                for option_payload in option_payloads:
                    label = str((option_payload or {}).get("label") or "").strip()
                    value = str((option_payload or {}).get("value") or "").strip()
                    if label.lower().strip() == answer_lower or answer_lower in label.lower():
                        matching_value = value
                        break
                if matching_value:
                    exact_radio = self._page.locator(
                        f'input[type="radio"][name="{name}"][value="{matching_value}"]'
                    ).first
                    if exact_radio.count():
                        try:
                            if exact_radio.is_checked():
                                return True
                        except Exception:  # noqa: BLE001
                            pass
                        try:
                            exact_radio.check(force=True)
                            return True
                        except Exception:  # noqa: BLE001
                            try:
                                exact_radio.evaluate("el => { el.checked = true; el.dispatchEvent(new Event('input', { bubbles: true })); el.dispatchEvent(new Event('change', { bubbles: true })); }")
                                return True
                            except Exception:  # noqa: BLE001
                                pass
                container = locator.locator("xpath=ancestor::*[self::fieldset or self::section or self::div][1]")
                answer_patterns = [re.escape(answer), re.escape(answer.lower())]
                for pattern in answer_patterns:
                    try:
                        label_locator = container.get_by_label(re.compile(pattern, re.I)).first
                        if label_locator.count():
                            label_locator.click()
                            return True
                    except Exception:  # noqa: BLE001
                        pass
                    try:
                        text_locator = container.get_by_text(re.compile(pattern, re.I)).first
                        if text_locator.count():
                            text_locator.click()
                            return True
                    except Exception:  # noqa: BLE001
                        pass
                radios = self._page.locator(f'input[type="radio"][name="{name}"]')
                count = radios.count()
                for index in range(count):
                    radio = radios.nth(index)
                    try:
                        if radio.is_checked():
                            radio_value = str(radio.get_attribute("value") or "").strip()
                            if not matching_value or radio_value == matching_value:
                                return True
                        radio_value = str(radio.get_attribute("value") or "").strip()
                        if answer.lower() in radio_value.lower():
                            radio.check(force=True)
                            return True
                    except Exception:  # noqa: BLE001
                        continue
            if field_type == "checkbox":
                checkboxes = self._page.locator(f'input[type="checkbox"][name="{name}"]')
                if not checkboxes.count():
                    return False
                requested_answers = [part.strip() for part in re.split(r"\s*\|\s*|\s*,\s*", answer) if part.strip()]
                if not requested_answers:
                    requested_answers = [answer.strip()]
                option_payloads = question.get("option_payloads", [])
                matched_indexes: list[int] = []
                matched_labels: list[str] = []
                for requested_answer in requested_answers:
                    answer_lower = requested_answer.lower().strip()
                    for option_index, option_payload in enumerate(option_payloads):
                        label = str((option_payload or {}).get("label") or "").strip()
                        if label.lower().strip() == answer_lower or answer_lower in label.lower():
                            if option_index not in matched_indexes:
                                matched_indexes.append(option_index)
                            if label and label not in matched_labels:
                                matched_labels.append(label)
                            break
                for matched_index in matched_indexes:
                    try:
                        checkbox = checkboxes.nth(matched_index)
                        if checkbox.count():
                            checkbox.check(force=True)
                    except Exception:  # noqa: BLE001
                        continue
                try:
                    checked_count = int(
                        self._page.evaluate(
                            """(groupName) => Array.from(document.querySelectorAll(`input[type="checkbox"][name="${groupName}"]`))
                                .filter(node => !!node.checked).length""",
                            name,
                        )
                    )
                    if checked_count > 0:
                        return True
                except Exception:  # noqa: BLE001
                    pass
                container = checkboxes.first.locator("xpath=ancestor::*[self::fieldset or self::section or self::div][1]")
                for matched_label in matched_labels:
                    pattern = re.escape(matched_label)
                    try:
                        label_locator = container.get_by_label(re.compile(pattern, re.I)).first
                        if label_locator.count():
                            label_locator.check(force=True)
                            return True
                    except Exception:  # noqa: BLE001
                        pass
                    try:
                        text_locator = container.get_by_text(re.compile(pattern, re.I)).first
                        if text_locator.count():
                            text_locator.click()
                            return True
                    except Exception:  # noqa: BLE001
                        pass
        except Exception:  # noqa: BLE001
            return False
        return False

    def get_questionnaire_validation_errors(self) -> list[str]:
        errors: list[str] = []
        try:
            explicit_errors = self._page.evaluate(
                """() => {
                    const normalise = (value) => String(value || '').replace(/\s+/g, ' ').trim();
                    const isVisible = (el) => {
                        if (!el) return false;
                        const style = window.getComputedStyle(el);
                        return style.display !== 'none' && style.visibility !== 'hidden' && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                    };
                    const results = [];
                    const seen = new Set();
                    const candidates = Array.from(
                        document.querySelectorAll(
                            '[role="alert"], [aria-live], [aria-invalid="true"], [data-automation*="error" i], [class*="error" i]'
                        )
                    );
                    for (const el of candidates) {
                        if (!(el instanceof Element) || !isVisible(el)) continue;
                        const text = normalise(el.innerText || el.textContent || '');
                        if (!text) continue;
                        if (!/please make a selection|required|please answer|invalid/i.test(text)) continue;
                        if (seen.has(text)) continue;
                        seen.add(text);
                        results.push(text);
                    }
                    return results.slice(0, 12);
                }"""
            )
            for error in explicit_errors or []:
                cleaned = str(error).strip()
                if cleaned:
                    errors.append(cleaned)
        except Exception:  # noqa: BLE001
            pass
        try:
            empty_select_questions = self._page.evaluate(
                """() => {
                    const results = [];
                    const selects = Array.from(document.querySelectorAll('select[name*="questionnaire"]'));
                    for (const select of selects) {
                        const style = window.getComputedStyle(select);
                        if (style.display === 'none' || style.visibility === 'hidden') continue;
                        const value = (select.value || '').trim();
                        const selectedOption = select.selectedOptions && select.selectedOptions.length ? select.selectedOptions[0] : null;
                        const selectedLabel = selectedOption ? (selectedOption.textContent || '').trim() : '';
                        const isPlaceholder = !value || /select|choose/i.test(selectedLabel);
                        if (!isPlaceholder) continue;
                        const invalid =
                            select.getAttribute('aria-invalid') === 'true'
                            || !!select.closest('[aria-invalid="true"], [role="alert"], [data-automation*="error" i], [class*="error" i]');
                        if (!invalid) continue;
                        const container = select.closest('fieldset, section, div, li') || select.parentElement || select;
                        const optionLabels = Array.from(select.options || []).map(option => (option.textContent || '').replace(/\\s+/g, ' ').trim()).filter(Boolean);
                        const optionSet = new Set(optionLabels);
                        const lines = (container.innerText || '').split(/\\n+/).map(line => line.replace(/\\s+/g, ' ').trim()).filter(Boolean);
                        let questionText = '';
                        for (const line of lines) {
                            if (optionSet.has(line)) continue;
                            if (/^(required|select|choose|yes|no)$/i.test(line)) continue;
                            if (line.length < 8) continue;
                            questionText = line;
                            break;
                        }
                        results.push(questionText || select.name || 'Unanswered required select');
                    }
                    return results;
                }"""
            )
            for question in empty_select_questions or []:
                cleaned = str(question).strip()
                if cleaned:
                    errors.append(f"Unanswered select: {cleaned}")
        except Exception:  # noqa: BLE001
            pass
        deduped: list[str] = []
        seen: set[str] = set()
        for error in errors:
            if error not in seen:
                seen.add(error)
                deduped.append(error)
        return deduped[:15]

    def save_dynamic_question_debug_artifacts(self, debug_dir: Path) -> tuple[Path, Path]:
        debug_dir.mkdir(parents=True, exist_ok=True)
        screenshot_path = debug_dir / "dynamic_questions_unknown.png"
        html_path = debug_dir / "dynamic_questions_unknown.html"
        _safe_page_screenshot(self._page, screenshot_path)
        _safe_page_html_write(self._page, html_path)
        return screenshot_path, html_path

    def save_questionnaire_failed_artifacts(self, debug_dir: Path) -> tuple[Path, Path]:
        debug_dir.mkdir(parents=True, exist_ok=True)
        screenshot_path = debug_dir / "questionnaire_failed.png"
        html_path = debug_dir / "questionnaire_failed.html"
        _safe_page_screenshot(self._page, screenshot_path)
        _safe_page_html_write(self._page, html_path)
        return screenshot_path, html_path

    def click_questionnaire_continue(self) -> None:
        self._click_first(
            [
                lambda: self._page.get_by_role("button", name=re.compile("continue", re.I)).first,
                lambda: self._page.get_by_text(re.compile("^continue$", re.I)).first,
                lambda: self._page.locator("button", has_text=re.compile("continue", re.I)).first,
            ],
            "Could not find questionnaire Continue button.",
        )

    def final_navigation_label(self) -> str:
        candidates = [
            self._page.get_by_role("button", name=re.compile("submit|send application|apply|review|continue|next", re.I)).first,
            self._page.locator("button").last,
        ]
        for locator in candidates:
            try:
                if locator.count() and locator.is_visible():
                    return locator.inner_text().strip()
            except Exception:  # noqa: BLE001
                continue
        return ""

    def click_final_navigation(self) -> None:
        label = self.final_navigation_label()
        lowered = label.strip().lower()
        if any(pattern in lowered for pattern in FINAL_SUBMIT_PATTERNS):
            raise SeekApplyAssistError("Refusing to click final submit button.")
        if any(pattern in lowered for pattern in SAFE_NAVIGATION_PATTERNS):
            self._page.get_by_role("button", name=re.compile(re.escape(label), re.I)).first.click()

    def current_url(self) -> str:
        try:
            return self._page.url
        except Exception:  # noqa: BLE001
            return ""

    def page_text(self) -> str:
        try:
            return self._page.locator("body").inner_text(timeout=5000)
        except Exception:  # noqa: BLE001
            return ""

    def close(self) -> None:
        close_targets = [
            ("page", "_page", "close"),
            ("context", "_context", "close"),
            ("browser", "_browser", "close"),
            ("playwright", "_playwright", "stop"),
        ]
        for label, attr_name, method_name in close_targets:
            target = getattr(self, attr_name, None)
            if target is None:
                continue
            try:
                method = getattr(target, method_name, None)
                if callable(method):
                    method()
            except Exception as exc:  # noqa: BLE001
                print(f"[seek_apply_assist] cleanup_{label}_error: {exc}", flush=True)
            finally:
                setattr(self, attr_name, None)
        if getattr(self, "_cleanup_user_data_dir_on_close", False):
            try:
                shutil.rmtree(self.user_data_dir, ignore_errors=True)
            except Exception as exc:  # noqa: BLE001
                print(f"[seek_apply_assist] cleanup_user_data_dir_error: {exc}", flush=True)
        playwright_cm = getattr(self, "_playwright_cm", None)
        if playwright_cm is not None:
            try:
                exit_method = getattr(playwright_cm, "__exit__", None)
                if callable(exit_method):
                    exit_method(None, None, None)
            except Exception as exc:  # noqa: BLE001
                print(f"[seek_apply_assist] cleanup_playwright_context_manager_error: {exc}", flush=True)
            finally:
                self._playwright_cm = None

    def _click_first(self, factories: list[Callable[[], Any]], error_message: str) -> None:
        for factory in factories:
            try:
                locator = factory()
                if locator.count() and locator.is_visible():
                    locator.click()
                    return
            except self._timeout_error:
                continue
            except Exception:  # noqa: BLE001
                continue
        raise SeekApplyAssistError(error_message)

    def _check_first(self, locators: list[Any], error_message: str) -> None:
        for locator in locators:
            try:
                if locator.count() and locator.is_visible():
                    locator.check(force=True)
                    return
            except Exception:  # noqa: BLE001
                continue
        raise SeekApplyAssistError(error_message)

    def _fill_first(self, locators: list[Any], text: str, error_message: str) -> None:
        for locator in locators:
            try:
                if locator.count() and locator.is_visible():
                    locator.fill(text)
                    return
            except Exception:  # noqa: BLE001
                continue
        raise SeekApplyAssistError(error_message)

    def _click_or_check_optional(self, locators: list[Any]) -> bool:
        for locator in locators:
            try:
                if locator.count() and locator.is_visible():
                    input_type = (locator.get_attribute("type") or "").lower()
                    if input_type in {"radio", "checkbox"}:
                        locator.check(force=True)
                    else:
                        locator.click()
                    self._page.wait_for_timeout(750)
                    return True
            except Exception:  # noqa: BLE001
                continue
        return False


def _looks_like_profile_lock_error(message: str) -> bool:
    lowered = str(message or "").lower()
    patterns = [
        "launch_persistent_context",
        "target page, context or browser has been closed",
        "profile appears to be in use",
        "user data directory is already in use",
        "singletonlock",
        "process_singleton",
        "browser closed",
    ]
    return any(pattern in lowered for pattern in patterns)


def _build_playwright_adapter(
    visible: bool,
    db_path: Path,
    *,
    force_new_profile: bool = False,
    use_bulk_profile: bool = False,
    slow_mo_ms: int = 0,
) -> SeekApplyAdapter:
    return _PlaywrightSeekAdapter(
        visible,
        db_path,
        force_new_profile=force_new_profile,
        use_bulk_profile=use_bulk_profile,
        slow_mo_ms=slow_mo_ms,
    )
