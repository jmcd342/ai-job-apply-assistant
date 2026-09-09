from __future__ import annotations

import csv
import io
import json
import re
import subprocess
import zipfile
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from xml.etree import ElementTree as ET

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

from src.application_tracker import ApplicationTracker
from src.apply_assist import build_apply_assist_payload
from src.auto_apply_rules import auto_apply_block_reason, is_auto_apply_blocked
from src.bulk_prepare_quick_apply import (
    find_quick_apply_jobs,
    run_bulk_quick_apply,
    validate_seek_session_subprocess,
)
from src.database import Database
from src.document_generator import DocumentGenerator
from src.email_alert_ingestor import EmailAlertIngestor
from src.fit_scorer import explain_score_job
from src.gmail_followup import GmailFollowupManager
from src.gmail_ingestor import GmailIngestor
from src.job_enricher import JobEnricher
from src.job_importer import JobImporter
from src.salary_estimator import estimate_salary_expectation
from src.seek_apply_assist import (
    assist_seek_apply,
    open_seek_login_in_browser,
    open_seek_url_in_browser,
    read_seek_session_state,
    submit_seek_review,
    validate_seek_session,
)
from src.text_cleaner import is_valid_job_url

APP_ROOT = Path(__file__).resolve().parent
DATA_DIR = APP_ROOT / "data"
APPLICATIONS_DIR = APP_ROOT / "applications"
DB_PATH = DATA_DIR / "job_assistant.db"
PAUSE_FLAG_PATH = DATA_DIR / "pause_daily_run.flag"
PROCESS_CANCEL_FLAG_PATH = DATA_DIR / "cancel_processing.flag"
PROCESS_STATE_PATH = DATA_DIR / "active_processing_state.json"
STATUS_OPTIONS = [
    "discovered",
    "drafted",
    "needs_user_action",
    "enriched",
    "reviewed",
    "ready_to_apply",
    "applied",
    "closed",
    "rejected",
    "ignored",
    "interview",
    "archived",
]
DESIRED_DEFAULT_STATUSES = ["discovered", "drafted", "needs_user_action", "enriched", "reviewed", "ready_to_apply", "applied", "closed"]
CV_PROFILE_LABELS = {
    "data_engineer_cv": "Data Engineer CV",
    "analytics_engineer_bi_cv": "BI / Analytics CV",
    "ai_automation_data_science_cv": "AI / Automation CV",
    "Pending": "Pending",
}
BATCH_SUBMIT_BLOCKING_STATUSES = {
    "browser_profile_locked",
    "failed",
    "paused_for_seek_login",
    "paused_for_seek_verification",
    "paused_for_captcha",
    "paused_for_manual_questionnaire",
    "paused_for_manual_resume_upload",
}
PAUSED_BULK_STATUSES = {
    "paused_for_seek_verification",
    "paused_for_captcha",
    "paused_for_seek_login",
    "paused_for_manual_resume_upload",
    "paused_for_manual_questionnaire",
}
QUEUE_READY_RUN_STATUSES = {
    "ready_for_human_review",
    "prepared_for_manual_review",
}
QUEUE_PROBLEM_RUN_STATUSES = {
    "failed",
    "failed_needs_fix",
    "browser_profile_locked",
    "paused_for_seek_verification",
    "paused_for_captcha",
    "paused_for_seek_login",
    "paused_for_manual_resume_upload",
    "paused_for_manual_questionnaire",
    "paused_for_resume_upload_incomplete",
}
PROBLEM_CATEGORY_LABELS = {
    "post_quick_apply": "Post-Quick-Apply Stuck",
    "resume_documents": "Resume / Documents",
    "questionnaire": "Questionnaire / Review Transition",
    "job_open_redirect": "Job Open / Redirect",
    "quick_apply_missing": "Quick Apply Button Missing",
    "login_verification": "Login / Verification",
    "closed_stale": "Closed / Stale",
    "submit_failure": "Submit Failure",
    "timeout_other": "Other Timeout",
    "other": "Other / Unknown",
}


def _load_active_processing_state() -> dict[str, str]:
    if not PROCESS_STATE_PATH.exists():
        return {}
    try:
        payload = json.loads(PROCESS_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    return payload if isinstance(payload, dict) else {}


def _set_active_processing_state(mode: str, message: str = "") -> None:
    started_at = datetime.now().isoformat(timespec="seconds")
    payload = {
        "mode": str(mode or "").strip(),
        "message": str(message or "").strip(),
        "started_at": started_at,
        "updated_at": started_at,
    }
    PROCESS_STATE_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _clear_active_processing_state() -> None:
    PROCESS_STATE_PATH.unlink(missing_ok=True)


def _request_processing_cancel() -> None:
    PROCESS_CANCEL_FLAG_PATH.write_text("cancel", encoding="utf-8")


def _clear_processing_cancel_request() -> None:
    PROCESS_CANCEL_FLAG_PATH.unlink(missing_ok=True)


def _processing_cancel_requested() -> bool:
    return PROCESS_CANCEL_FLAG_PATH.exists()


def _processing_elapsed_label(active_processing: dict[str, str] | None) -> str:
    payload = active_processing or {}
    started_at_raw = str(payload.get("started_at") or payload.get("updated_at") or "").strip()
    if not started_at_raw:
        return ""
    try:
        started_at = datetime.fromisoformat(started_at_raw)
    except Exception:  # noqa: BLE001
        return ""
    elapsed_seconds = max(0.0, (datetime.now() - started_at).total_seconds())
    return _format_duration_seconds(elapsed_seconds)


def initialise_app() -> tuple[Database, ApplicationTracker, JobImporter, DocumentGenerator, EmailAlertIngestor, GmailIngestor, GmailFollowupManager, JobEnricher]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    APPLICATIONS_DIR.mkdir(parents=True, exist_ok=True)
    print(f"[app] DB_PATH={DB_PATH.resolve()}", flush=True)

    database = Database(DB_PATH)
    tracker = ApplicationTracker(database, APP_ROOT)
    importer = JobImporter(database)
    document_generator = DocumentGenerator(database, APP_ROOT)
    job_enricher = JobEnricher(database, APP_ROOT)
    email_ingestor = EmailAlertIngestor(database, importer, document_generator, DATA_DIR)
    gmail_ingestor = GmailIngestor(database, importer, document_generator, APP_ROOT, job_enricher=job_enricher)
    gmail_followup = GmailFollowupManager(database, APP_ROOT)
    return database, tracker, importer, document_generator, email_ingestor, gmail_ingestor, gmail_followup, job_enricher


def render_sidebar(gmail_ingestor: GmailIngestor) -> None:
    st.sidebar.title("AI Job Apply Assistant")
    st.sidebar.caption("Human-in-the-loop Auckland job review workflow.")
    st.sidebar.info(
        "This app prepares applications and tracks progress. It does not bypass CAPTCHAs, "
        "logins, anti-bot protections, or submission controls."
    )
    if PAUSE_FLAG_PATH.exists():
        st.sidebar.warning("Daily drafting is paused.")
        if st.sidebar.button("Resume daily drafting"):
            PAUSE_FLAG_PATH.unlink(missing_ok=True)
            st.rerun()
    else:
        st.sidebar.success("Daily drafting is active.")
        if st.sidebar.button("Pause daily drafting"):
            PAUSE_FLAG_PATH.write_text("paused", encoding="utf-8")
            st.rerun()

    gmail_status = gmail_ingestor.get_connection_status()
    st.sidebar.markdown("#### Gmail")
    if gmail_status["authenticated"]:
        st.sidebar.success("Connected")
    elif gmail_status["configured"]:
        st.sidebar.warning("Reconnect required")
    else:
        st.sidebar.info("Not connected")
    st.sidebar.caption(f"Last sync: {gmail_status['last_sync_timestamp'] or 'Never'}")
    st.sidebar.caption(f"Last imported jobs: {gmail_status['last_sync_imported_jobs']}")
    if gmail_status["error"]:
        st.sidebar.error(gmail_status["error"])

    jobs_label_only = st.sidebar.checkbox("Use Gmail `Jobs` label only", value=False, key="sidebar_jobs_label_only")
    primary_gmail_action = "Reconnect Gmail" if gmail_status["configured"] and not gmail_status["authenticated"] else "Sync Gmail"
    if st.sidebar.button(primary_gmail_action, width="stretch"):
        result = gmail_ingestor.sync_messages(
            interactive_auth=True,
            jobs_label_only=jobs_label_only,
        )
        st.session_state["gmail_sync_result"] = result
        st.rerun()

    seek_session = read_seek_session_state(DB_PATH)
    seek_state = str(seek_session.get("state") or "unknown")
    st.sidebar.markdown("#### SEEK Session")
    if seek_state == "session_valid":
        st.sidebar.success("Valid")
    elif seek_state == "session_refreshed":
        st.sidebar.success("Refreshed recently")
    elif seek_state == "verification_required":
        st.sidebar.warning("Verification required")
    elif seek_state == "captcha_required":
        st.sidebar.warning("CAPTCHA required")
    elif seek_state == "login_required":
        st.sidebar.warning("Login required")
    else:
        st.sidebar.info("Unknown")
    st.sidebar.caption(f"Last update: {seek_session.get('updated_at') or 'Never'}")
    if seek_session.get("reason"):
        st.sidebar.caption(f"Last reason: {seek_session['reason']}")
    if seek_session.get("matched_token"):
        st.sidebar.caption(
            f"Matched token: {seek_session['matched_token']}"
            + (f" ({seek_session['matched_source']})" if seek_session.get("matched_source") else "")
        )
    st.sidebar.caption("For manual setup, use SEEK email sign-in in normal Chrome or Edge, not Google sign-in.")
    st.sidebar.caption("After signing in and closing Chrome, click `Validate SEEK Session` before rerunning bulk.")
    if st.sidebar.button("Validate SEEK Session", width="stretch"):
        try:
            result = validate_seek_session_subprocess(DB_PATH, use_bulk_profile=False, visible=False)
            if result.get("ok"):
                st.session_state["seek_session_validation_message"] = (
                    f"SEEK session validated in {result.get('browser_channel') or 'chromium'}."
                )
                st.session_state["seek_session_validation_error"] = ""
            else:
                token = str(result.get("matched_token") or "").strip()
                token_suffix = f" Matched token: {token}." if token else ""
                st.session_state["seek_session_validation_message"] = ""
                st.session_state["seek_session_validation_error"] = str(result.get("reason") or "SEEK session validation failed.") + token_suffix
        except Exception as exc:  # noqa: BLE001
            st.session_state["seek_session_validation_message"] = ""
            st.session_state["seek_session_validation_error"] = str(exc)
        st.rerun()
    if st.sidebar.button("Open SEEK Login In Chrome", width="stretch"):
        try:
            channel, profile_root = open_seek_login_in_browser(DB_PATH, use_bulk_profile=False)
            st.session_state["seek_login_launcher_message"] = (
                f"Opened SEEK in {channel}. "
                + (f"Profile root: {profile_root}" if profile_root else "Using default browser profile settings.")
            )
        except Exception as exc:  # noqa: BLE001
            st.session_state["seek_login_launcher_error"] = str(exc)
        st.rerun()
    if st.session_state.get("seek_login_launcher_message"):
        st.sidebar.success(str(st.session_state.get("seek_login_launcher_message")))
    if st.session_state.get("seek_login_launcher_error"):
        st.sidebar.error(str(st.session_state.get("seek_login_launcher_error")))
    if st.session_state.get("seek_session_validation_message"):
        st.sidebar.success(str(st.session_state.get("seek_session_validation_message")))
    if st.session_state.get("seek_session_validation_error"):
        st.sidebar.error(str(st.session_state.get("seek_session_validation_error")))

    st.sidebar.markdown("#### Mobile / Live View")
    auto_refresh_choice = st.sidebar.selectbox(
        "Auto-refresh",
        options=["Off", "15 seconds", "30 seconds", "60 seconds"],
        index=0,
        key="sidebar_auto_refresh_choice",
        help="Useful when the dashboard is open on your phone and PC at the same time.",
    )
    st.sidebar.caption("Use the same app URL on your phone and PC. Each screen updates from the same database and can auto-refresh independently.")

    active_processing = _load_active_processing_state()
    active_mode = str(active_processing.get("mode") or "").strip()
    active_message = str(active_processing.get("message") or "").strip()
    if active_mode:
        st.sidebar.markdown("#### Processing")
        st.sidebar.warning(f"Currently processing: {active_mode}")
        if active_message:
            st.sidebar.caption(active_message)
        elapsed_label = _processing_elapsed_label(active_processing)
        if elapsed_label:
            st.sidebar.caption(f"Current run time: {elapsed_label}")
        if st.sidebar.button("Cancel current processing", width="stretch", key="cancel_current_processing"):
            _request_processing_cancel()
            st.session_state["processing_cancel_message"] = "Cancel requested. The current bulk prepare or submit run will stop as soon as it reaches a safe checkpoint."
            st.rerun()
    if st.session_state.get("processing_cancel_message"):
        st.sidebar.info(str(st.session_state.get("processing_cancel_message")))


def render_gmail_sync_panel() -> None:
    result = st.session_state.get("gmail_sync_result")
    if not result:
        return
    message = (
        f"Imported {result['created']} job(s), skipped {result['duplicates']} duplicate job(s), "
        f"filtered out {result['filtered_out']} non-Auckland job(s), and generated {result['auto_drafted']} draft package(s)."
    )
    if result.get("errors"):
        st.warning(message)
        for error in result.get("errors", []):
            st.error(str(error))
    else:
        st.success(message)


def render_dashboard_css() -> None:
    st.markdown(
        """
        <style>
        .quick-queue-header {
            position: sticky;
            top: 0;
            z-index: 40;
            background: rgba(14, 17, 23, 0.98);
            border: 1px solid rgba(250, 250, 250, 0.08);
            border-radius: 0.6rem;
            padding: 0.7rem 0.9rem;
            margin: 0.4rem 0 0.8rem 0;
            backdrop-filter: blur(10px);
            box-shadow: 0 8px 18px rgba(0, 0, 0, 0.22);
        }
        .quick-queue-grid {
            display: grid;
            grid-template-columns: 2.1fr 1fr 1fr 1.2fr 1.2fr 0.9fr 1fr 1.8fr 1.1fr;
            gap: 1rem;
            align-items: center;
        }
        .quick-queue-grid strong {
            font-size: 0.9rem;
        }
        .quick-queue-subtle {
            color: rgba(250, 250, 250, 0.66);
            font-size: 0.86rem;
        }
        .job-status-text {
            color: rgba(250, 250, 250, 0.72);
            font-size: 0.84rem;
            margin-top: 0.2rem;
        }
        .job-row-compact {
            padding-top: 0.2rem;
            padding-bottom: 0.2rem;
        }
        [data-testid="stMetric"] {
            border: 1px solid rgba(250, 250, 250, 0.08);
            border-radius: 0.9rem;
            padding: 0.55rem 0.7rem;
            background: rgba(255, 255, 255, 0.02);
        }
        .stTabs [data-baseweb="tab-list"] {
            gap: 0.35rem;
            flex-wrap: wrap;
        }
        .stTabs [data-baseweb="tab"] {
            min-height: 44px;
            border-radius: 0.8rem;
            padding-left: 0.9rem;
            padding-right: 0.9rem;
        }
        .stButton > button,
        .stDownloadButton > button,
        .stLinkButton > a {
            min-height: 44px;
        }
        .queue-summary-grid {
            display: grid;
            grid-template-columns: repeat(5, minmax(0, 1fr));
            gap: 0.8rem;
            margin: 0.75rem 0 1rem 0;
        }
        .queue-summary-card {
            border: 1px solid rgba(250, 250, 250, 0.08);
            border-radius: 0.9rem;
            padding: 0.8rem 0.9rem;
            background: rgba(255, 255, 255, 0.02);
            min-height: 110px;
        }
        .queue-summary-label {
            color: rgba(250, 250, 250, 0.76);
            font-size: 0.9rem;
            margin-bottom: 0.55rem;
        }
        .queue-summary-value {
            font-size: 2rem;
            font-weight: 700;
            line-height: 1.1;
            margin-bottom: 0.35rem;
        }
        .queue-summary-subtitle {
            color: rgba(250, 250, 250, 0.66);
            font-size: 0.82rem;
            line-height: 1.3;
        }
        @media (max-width: 900px) {
            .quick-queue-header {
                display: none;
            }
            .block-container {
                padding-top: 2.25rem;
                padding-left: 0.75rem;
                padding-right: 0.75rem;
                padding-bottom: 2rem;
            }
            [data-testid="stMetric"] {
                padding: 0.7rem 0.8rem;
            }
            .stTabs [data-baseweb="tab-list"] {
                display: grid !important;
                grid-template-columns: 1fr !important;
                align-items: stretch !important;
                justify-items: stretch !important;
                gap: 0.45rem !important;
                width: 100% !important;
            }
            .stTabs [data-baseweb="tab"] {
                width: 100% !important;
                justify-content: flex-start !important;
                text-align: left !important;
                white-space: normal !important;
                padding-left: 1rem !important;
                padding-right: 1rem !important;
            }
            .stExpander {
                border-radius: 0.9rem;
            }
            .stTextArea textarea,
            .stTextInput input {
                font-size: 0.95rem;
            }
            .queue-summary-grid {
                grid-template-columns: 1fr;
            }
            .queue-summary-card {
                min-height: auto;
            }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def render_auto_refresh() -> None:
    choice = str(st.session_state.get("sidebar_auto_refresh_choice") or "Off").strip()
    interval_map = {
        "15 seconds": 15_000,
        "30 seconds": 30_000,
        "60 seconds": 60_000,
    }
    interval_ms = interval_map.get(choice)
    if not interval_ms:
        return
    st.caption(f"Live refresh is on ({choice.lower()}).")
    components.html(
        f"""
        <script>
        window.setTimeout(function () {{
            const topWindow = window.parent;
            if (topWindow && topWindow.location) {{
                topWindow.location.reload();
            }}
        }}, {interval_ms});
        </script>
        """,
        height=0,
        width=0,
    )


def _is_quick_apply_job(details: dict) -> bool:
    return str(details.get("apply_type") or "").strip().lower() == "quick apply" or bool(details.get("has_quick_apply_button"))


def _is_auckland_job(details: dict | pd.Series) -> bool:
    location_parts = [
        str(details.get("location") or "").strip(),
        str(details.get("enriched_location") or "").strip(),
        str(details.get("title") or "").strip(),
        str(details.get("description") or "").strip(),
        str(details.get("about_the_job") or "").strip(),
    ]
    haystack = " ".join(part for part in location_parts if part).lower()
    if not haystack:
        return False
    if "auckland" in haystack:
        return True
    if "north shore" in haystack:
        return True
    if "manukau" in haystack:
        return True
    if "waitakere" in haystack:
        return True
    return False


def _posted_age_label(details: dict) -> str:
    try:
        age = int(details.get("age_in_days") or 0)
    except (TypeError, ValueError):
        age = 0
    if age > 0:
        return "1 day old" if age == 1 else f"{age} days old"

    raw_date = str(details.get("date_posted") or "").strip()
    for candidate in [raw_date[:10], raw_date]:
        if not candidate:
            continue
        try:
            posted = datetime.fromisoformat(candidate).date() if "T" in candidate else date.fromisoformat(candidate)
            delta_days = max(0, (datetime.now().date() - posted).days)
            return "Today" if delta_days == 0 else "1 day old" if delta_days == 1 else f"{delta_days} days old"
        except Exception:  # noqa: BLE001
            continue

    imported_at = str(details.get("imported_at") or details.get("date_found") or "").strip()
    for candidate in [imported_at]:
        if not candidate:
            continue
        try:
            imported = datetime.fromisoformat(candidate.replace("Z", "+00:00")).date()
            delta_days = max(0, (datetime.now().date() - imported).days)
            return "Today" if delta_days == 0 else "1 day old" if delta_days == 1 else f"{delta_days} days old"
        except Exception:  # noqa: BLE001
            continue
    fallback = str(details.get("date_posted_text") or "").strip()
    return fallback or "Unknown"


def _latest_apply_runs_by_job() -> dict[int, dict]:
    runs_root = DATA_DIR / "apply_runs"
    if not runs_root.exists():
        return {}
    latest: dict[int, dict] = {}
    for checklist_path in runs_root.glob("*/final_review_checklist.json"):
        try:
            payload = json.loads(checklist_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        try:
            job_id = int(payload.get("job_id") or 0)
        except (TypeError, ValueError):
            continue
        if job_id <= 0:
            continue
        run_dir = checklist_path.parent
        record = {
            "payload": payload,
            "run_dir": run_dir,
            "mtime": checklist_path.stat().st_mtime,
        }
        existing = latest.get(job_id)
        if existing is None or record["mtime"] >= existing["mtime"]:
            latest[job_id] = record
    return latest


def _load_dynamic_question_answers(run_dir: Path) -> list[dict[str, str]]:
    scan_path = run_dir / "dynamic_question_scan.json"
    if not scan_path.exists():
        return []
    try:
        payload = json.loads(scan_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []
    fields = payload.get("fields")
    if not isinstance(fields, list):
        return []
    items: list[dict[str, str]] = []
    for field in fields:
        if not isinstance(field, dict):
            continue
        question = str(field.get("extracted_question_text") or "").strip()
        answer = str(field.get("chosen_answer") or field.get("current_label") or "").strip()
        if not question:
            continue
        items.append(
            {
                "question": question,
                "answer": answer or "(no answer captured)",
                "source": str(field.get("answer_source") or field.get("matched_rule") or "").strip(),
                "success": "Yes" if bool(field.get("answer_success")) else "No",
            }
        )
    return items


def _resolved_salary_display(details: dict, question_answers: list[dict[str, str]] | None = None) -> tuple[str, str]:
    estimate = estimate_salary_expectation(details)
    estimator_reason = estimate.reasoning[0] if estimate.reasoning else "Used the fixed Auckland salary table."
    answers = question_answers or []
    for item in answers:
        question = str(item.get("question") or "").strip().lower()
        answer = str(item.get("answer") or "").strip()
        if not question or not answer:
            continue
        if "salary" in question or "expected annual base salary" in question or "expected salary" in question:
            reason = f"{estimator_reason} Displayed value is the latest answered salary question."
            return answer, reason

    calculated = str(estimate.salary_expectation_text or details.get("salary_expectation_text") or "").strip()
    if calculated:
        return calculated, estimator_reason
    return "Not calculated", "No calculated salary is available yet."


def _extract_final_url_from_run_log(run_dir: Path) -> str:
    log_path = run_dir / "apply_run_log.txt"
    if not log_path.exists():
        return ""
    try:
        for line in log_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.startswith("final_url="):
                return line.split("=", 1)[1].strip()
    except Exception:  # noqa: BLE001
        return ""
    return ""


def _short_issue_text(value: str, max_chars: int = 120) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        return ""
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3].rstrip() + "..."


def _load_apply_run_log_text(run_dir: Path | None) -> str:
    if run_dir is None:
        return ""
    try:
        log_path = Path(run_dir) / "apply_run_log.txt"
        if not log_path.exists():
            return ""
        return log_path.read_text(encoding="utf-8", errors="ignore")
    except Exception:  # noqa: BLE001
        return ""


def _categorize_problem_job(
    *,
    latest_run_status: str,
    issue_text: str,
    latest_payload: dict[str, object] | None = None,
    latest_run_dir: Path | None = None,
    latest_submit_status: str = "",
) -> tuple[str, str]:
    if latest_submit_status and latest_submit_status != "applied":
        return "submit_failure", PROBLEM_CATEGORY_LABELS["submit_failure"]

    payload = latest_payload or {}
    reason = " ".join(
        part
        for part in [
            str(latest_run_status or ""),
            str(issue_text or ""),
            str(payload.get("failure_reason") or ""),
            str(payload.get("root_failure_reason") or ""),
        ]
        if str(part or "").strip()
    ).lower()

    if (
        "verification" in reason
        or "captcha" in reason
        or "seek login" in reason
        or "close all seek/chromium windows and retry" in reason
        or "browser_profiles\\seek_seeded" in reason
        or "profile is locked" in reason
        or latest_run_status in {"paused_for_seek_login", "paused_for_seek_verification", "paused_for_captcha"}
    ):
        return "login_verification", PROBLEM_CATEGORY_LABELS["login_verification"]
    if "could not find seek quick apply button" in reason:
        return "quick_apply_missing", PROBLEM_CATEGORY_LABELS["quick_apply_missing"]
    if "page.goto" in reason or "email.s.seek.co.nz" in reason or "redirect" in reason:
        return "job_open_redirect", PROBLEM_CATEGORY_LABELS["job_open_redirect"]
    if "no longer advertised" in reason or latest_run_status == "skipped" or "job_closed" in reason:
        return "closed_stale", PROBLEM_CATEGORY_LABELS["closed_stale"]
    if (
        "role-requirements" in reason
        or "answer employer questions" in reason
        or "manual_questionnaire" in reason
        or "review and submit step did not reach" in reason
        or "cover letter step continue" in reason
        or "questionnaire" in reason
    ):
        return "questionnaire", PROBLEM_CATEGORY_LABELS["questionnaire"]
    if (
        "choose documents" in reason
        or "resume" in reason
        or "upload" in reason
        or "manual_resume_upload" in reason
    ):
        return "resume_documents", PROBLEM_CATEGORY_LABELS["resume_documents"]

    run_log_text = _load_apply_run_log_text(latest_run_dir).lower()
    if run_log_text:
        if "clicked_quick_apply: quick apply opened." in run_log_text:
            if "handle_resume_upload_started" not in run_log_text and "checkpoint=choose_documents_loaded" not in run_log_text:
                return "post_quick_apply", PROBLEM_CATEGORY_LABELS["post_quick_apply"]
        if "handle_resume_upload_started" in run_log_text or "checkpoint=choose_documents_loaded" in run_log_text:
            return "resume_documents", PROBLEM_CATEGORY_LABELS["resume_documents"]
        if (
            "role-requirements" in run_log_text
            or "answer employer questions" in run_log_text
            or "review and submit" in run_log_text
        ):
            return "questionnaire", PROBLEM_CATEGORY_LABELS["questionnaire"]

    if "timed out" in reason:
        return "timeout_other", PROBLEM_CATEGORY_LABELS["timeout_other"]
    return "other", PROBLEM_CATEGORY_LABELS["other"]


def _submit_result_message(result: object) -> str:
    error = str(getattr(result, "error", "") or "").strip()
    if error:
        return error
    final_url = str(getattr(result, "final_url", "") or "").strip()
    if final_url:
        return final_url
    return "No final submit outcome was recorded."


def _persist_submit_result_state(tracker: ApplicationTracker, job_id: int, result: object, *, source_note: str) -> None:
    status = str(getattr(result, "status", "") or "").strip()
    message = _submit_result_message(result)
    if status == "applied":
        tracker.database.update_application_state(job_id, "applied", note=source_note)
        return
    if status == "skipped" and "no longer advertised" in message.lower():
        tracker.database.close_application(
            job_id,
            "job_no_longer_advertised",
            note="Closed automatically because SEEK reported the job was no longer advertised during final submit.",
        )
        return
    if status == "failed" and "could not find seek quick apply button" in message.lower():
        tracker.database.update_application_state(
            job_id,
            "reviewed",
            note="Moved out of ready queue because SEEK no longer showed a Quick Apply button during final submit.",
        )
        return
    if status == "failed" and "selected cv file not found" in message.lower():
        tracker.database.update_application_state(
            job_id,
            "reviewed",
            note="Moved out of ready queue because the selected CV file was missing and needs regeneration.",
        )
        return
    if status in {"paused_for_seek_verification", "paused_for_captcha", "paused_for_seek_login"}:
        tracker.database.update_application_state(
            job_id,
            "reviewed",
            note=message or "Moved out of ready queue because SEEK requires manual verification before submit can continue.",
        )


def _details_indicate_applied(details: dict) -> bool:
    application_status = str(details.get("application_status") or details.get("status") or "").strip().lower()
    applied_at = str(details.get("applied_at") or "").strip()
    submitted_at = str(details.get("submitted_at") or "").strip()
    return application_status in {"applied", "closed"} or bool(applied_at) or bool(submitted_at)


def _bucket_quick_apply_queue_item(
    *,
    latest_run: dict[str, object] | None,
    latest_run_status: str,
    latest_submit_status: str,
) -> str:
    if latest_submit_status and latest_submit_status != "applied":
        return "problems"
    if not latest_run:
        return "unprocessed"
    if latest_run_status in QUEUE_READY_RUN_STATUSES:
        return "ready"
    if latest_run_status in QUEUE_PROBLEM_RUN_STATUSES:
        return "problems"
    return "unprocessed"


def _applied_timestamp_series(frame: pd.DataFrame) -> pd.Series:
    applied_series = frame.get("applied_at", pd.Series([""] * len(frame), index=frame.index))
    submitted_series = frame.get("submitted_at", pd.Series([""] * len(frame), index=frame.index))
    applied_series = applied_series.fillna("").astype(str)
    submitted_series = submitted_series.fillna("").astype(str)
    return applied_series.where(applied_series.str.strip().ne(""), submitted_series)


def _details_indicate_hidden_from_queue(details: dict) -> bool:
    application_status = str(details.get("application_status") or details.get("status") or "").strip().lower()
    return application_status in {"applied", "closed", "ignored", "archived", "rejected"}


def _remove_job_from_active_queue(job_id: int, tracker: ApplicationTracker) -> None:
    tracker.update_application_status(
        job_id,
        "ignored",
        note="Removed from the active queue by the user. Kept in the database for duplicate detection.",
    )
    st.success("Job removed from the active queue. It will stay in the database for duplicate checks.")
    st.rerun()


def _submit_all_ready_jobs_batch(
    ready_items: list[dict[str, object]],
    tracker: ApplicationTracker,
    progress_callback: Callable[[int, int, str, str, list[dict[str, str]]], None] | None = None,
) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
    total = len(ready_items)
    for index, item in enumerate(ready_items):
        if progress_callback is not None:
            progress_callback(index, total, str(dict(item["details"]).get("title") or ""), str(dict(item["details"]).get("company") or ""), results)
        if _processing_cancel_requested():
            for remaining in ready_items[index:]:
                results.append(
                    {
                        "job_id": str(int(remaining["job_id"])),
                        "title": str(dict(remaining["details"]).get("title") or ""),
                        "status": "cancelled_before_attempt",
                        "message": "Batch submit was cancelled by the user.",
                    }
                )
            break
        job_id = int(item["job_id"])
        details = dict(tracker.get_job_details(job_id) or item["details"])
        if is_auto_apply_blocked(details):
            message = auto_apply_block_reason(details)
            tracker.database.update_application_state(job_id, "reviewed", message)
            results.append(
                {
                    "job_id": str(job_id),
                    "title": str(details.get("title") or ""),
                    "status": "auto_apply_blocked_skipped",
                    "message": message,
                }
            )
            tracker.database.log_run(
                "seek_submit",
                "bulk_submit_ready_job_result",
                "warning",
                f"job_id={job_id} status=auto_apply_blocked_skipped message={message}",
            )
            if progress_callback is not None:
                progress_callback(index + 1, total, str(details.get("title") or ""), str(details.get("company") or ""), results)
            continue
        if _details_indicate_applied(details):
            message = "Skipped because this job is already marked as applied."
            results.append(
                {
                    "job_id": str(job_id),
                    "title": str(details.get("title") or ""),
                    "status": "already_applied_skipped",
                    "message": message,
                }
            )
            tracker.database.log_run(
                "seek_submit",
                "bulk_submit_ready_job_result",
                "warning",
                f"job_id={job_id} status=already_applied_skipped message={message}",
            )
            if progress_callback is not None:
                progress_callback(index + 1, total, str(details.get("title") or ""), str(details.get("company") or ""), results)
            continue
        result = submit_seek_review(
            job_id,
            DB_PATH,
            review_url=str(item.get("review_url") or details.get("url") or ""),
            visible=False,
            use_bulk_profile=False,
        )
        message = _submit_result_message(result)
        results.append(
            {
                "job_id": str(job_id),
                "title": str(details.get("title") or ""),
                "status": str(result.status),
                "message": message,
            }
        )
        tracker.database.log_run(
            "seek_submit",
            "bulk_submit_ready_job_result",
            "success" if str(result.status) == "applied" else "warning",
            f"job_id={job_id} status={result.status} message={message}",
        )
        _persist_submit_result_state(
            tracker,
            job_id,
            result,
            source_note="Submitted from unified dashboard queue.",
        )
        if progress_callback is not None:
            progress_callback(index + 1, total, str(details.get("title") or ""), str(details.get("company") or ""), results)
        if str(result.status) == "applied":
            continue
        if str(result.status) in BATCH_SUBMIT_BLOCKING_STATUSES:
            for remaining in ready_items[index + 1 :]:
                results.append(
                    {
                        "job_id": str(int(remaining["job_id"])),
                        "title": str(dict(remaining["details"]).get("title") or ""),
                        "status": "not_attempted_after_batch_stop",
                        "message": f"Batch submit stopped after job {job_id} because {message}",
                    }
                )
            break
    success_count = sum(1 for item in results if str(item.get("status") or "") == "applied")
    tracker.database.log_run(
        "seek_submit",
        "bulk_submit_ready_jobs",
        "success" if success_count == len(results) else "warning",
        f"attempted={len(results)} submitted={success_count}",
    )
    return results


def _retry_prepare_job(job_id: int, tracker: ApplicationTracker, details: dict) -> None:
    result = assist_seek_apply(
        job_id,
        DB_PATH,
        visible=False,
        keep_browser_open_override=False,
        resume_upload_only=False,
        input_func=lambda: "",
        allow_manual_login_prompt=False,
    )
    debug_path = Path(result.debug_dir) if result.debug_dir else DB_PATH.parent / "debug_apply_assist"
    title = str(details.get("title") or f"job {job_id}")
    if result.status == "prepared_for_manual_review":
        tracker.database.update_application_state(
            job_id,
            "ready_to_apply",
            note="Prepared from Problems tab retry and awaiting final submit.",
        )
        st.success(f"{title} is ready to submit again.")
        st.rerun()
    if result.status == "browser_profile_locked":
        st.warning(
            "Close all SEEK/Chromium windows and retry. If it still fails, delete "
            "`data/browser_profiles/seek_seeded` and validate the session again."
        )
        if result.error:
            st.caption(result.error)
        return
    if result.status in {
        "paused_for_login",
        "paused_for_captcha",
        "paused_for_seek_verification",
        "paused_for_manual_resume_upload",
        "paused_for_seek_login",
        "paused_for_manual_questionnaire",
    }:
        st.warning(result.error or f"{title} paused again before completion.")
        st.caption(f"Debug artifacts: {debug_path}")
    else:
        st.error(result.error or f"{title} failed again during background prepare.")
        st.caption(f"Debug artifacts: {debug_path}")
    if result.latest_checkpoint:
        st.caption(f"Latest checkpoint: {result.latest_checkpoint}")
    st.caption(f"Final URL: {result.final_url or 'Unknown'}")
    if result.steps_completed:
        st.caption(f"Steps completed: {', '.join(result.steps_completed)}")


def _retry_problem_jobs_batch(
    problem_items: list[dict[str, object]],
    tracker: ApplicationTracker,
    document_generator: DocumentGenerator,
    *,
    selected_job_ids: list[int] | None = None,
    progress_callback: Callable[[int, int, str, str, list[BulkPrepareResult]], None] | None = None,
) -> None:
    if selected_job_ids:
        selected_set = {int(job_id) for job_id in selected_job_ids}
        filtered_items = [item for item in problem_items if int(item["job_id"]) in selected_set]
    else:
        filtered_items = list(problem_items)
    problem_job_ids = [int(item["job_id"]) for item in filtered_items]
    if not problem_job_ids:
        st.warning("No problem jobs are available to retry.")
        return
    tracker.database.log_run(
        "bulk_prepare_quick_apply",
        "problem_retry_started",
        "success",
        f"selected_job_ids={problem_job_ids}",
    )
    _clear_processing_cancel_request()
    _set_active_processing_state("prepare", f"Retrying {len(problem_job_ids)} problem job(s).")
    try:
        results = run_bulk_quick_apply(
            tracker.database,
            document_generator,
            max_jobs=len(problem_job_ids),
            dry_run=False,
            selected_job_ids=problem_job_ids,
            include_attempted=True,
            should_cancel=_processing_cancel_requested,
            progress_callback=progress_callback,
        )
    finally:
        _clear_active_processing_state()
        _clear_processing_cancel_request()
    tracker.database.log_run(
        "bulk_prepare_quick_apply",
        "problem_retry_finished",
        "success",
        f"attempted={len(results)} selected_job_ids={problem_job_ids}",
    )
    st.session_state["bulk_quick_apply_results"] = results
    st.session_state["problem_retry_results"] = [
        {
            "job_id": int(item.job_id),
            "title": str(item.title or ""),
            "status": str(item.status or ""),
            "message": str(item.failure_reason or item.root_failure_reason or ""),
            "company": str(item.company or ""),
        }
        for item in results
    ]
    st.rerun()


def _retry_auth_problem_jobs(
    problem_items: list[dict[str, object]],
    tracker: ApplicationTracker,
    document_generator: DocumentGenerator,
    *,
    selected_job_ids: list[int] | None = None,
    progress_callback: Callable[[int, int, str, str, list[BulkPrepareResult]], None] | None = None,
) -> None:
    auth_items = [
        item
        for item in problem_items
        if str(item.get("problem_category_key") or "") == "login_verification"
    ]
    if selected_job_ids:
        selected_set = {int(job_id) for job_id in selected_job_ids}
        auth_items = [item for item in auth_items if int(item["job_id"]) in selected_set]
    if not auth_items:
        st.warning("No auth-blocked problem jobs matched the current selection.")
        return

    validation = validate_seek_session_subprocess(DB_PATH, use_bulk_profile=False, visible=False)
    if not bool(validation.get("ok")):
        channel, profile_root = open_seek_login_in_browser(DB_PATH, use_bulk_profile=False)
        state = str(validation.get("state") or "login_required")
        if state == "captcha_required":
            st.warning("SEEK triggered a captcha / verify-you-are-human challenge. Finish it in Chrome, then click `Validate SEEK Session` in the sidebar and retry.")
        elif state == "verification_required":
            st.warning("SEEK triggered an extra verification step. Finish it in Chrome, then click `Validate SEEK Session` in the sidebar and retry.")
        else:
            st.warning("SEEK login needs refreshing in Chrome before these jobs can be retried.")
        st.caption(f"Opened SEEK in {channel}. Profile root: {profile_root or 'Default browser profile'}")
        return

    auth_job_ids = [int(item["job_id"]) for item in auth_items]
    st.success(f"SEEK session validated. Retrying {len(auth_job_ids)} auth-blocked job(s).")
    _retry_problem_jobs_batch(
        problem_items,
        tracker,
        document_generator,
        selected_job_ids=auth_job_ids,
        progress_callback=progress_callback,
    )


def _ready_review_queue(jobs: pd.DataFrame, tracker: ApplicationTracker) -> list[dict]:
    latest_runs = _latest_apply_runs_by_job()
    queue: list[dict] = []
    for _, row in jobs.iterrows():
        job_id = int(row["job_id"])
        run = latest_runs.get(job_id)
        if not run:
            continue
        payload = dict(run["payload"])
        if str(payload.get("status") or "") != "ready_for_human_review":
            continue
        details = tracker.get_job_details(job_id)
        if not details or not _is_quick_apply_job(details):
            continue
        effective_status = str(details.get("application_status") or details.get("status") or "").strip().lower()
        if effective_status == "applied":
            continue
        queue.append(
            {
                "job_id": job_id,
                "details": details,
                "payload": payload,
                "run_dir": run["run_dir"],
                "question_answers": _load_dynamic_question_answers(run["run_dir"]),
                "review_url": _extract_final_url_from_run_log(run["run_dir"]),
                "mtime": run["mtime"],
            }
        )
    queue.sort(key=lambda item: item["mtime"], reverse=True)
    return queue


def _handle_auto_drafts(result: dict, document_generator: DocumentGenerator) -> int:
    drafted = 0
    for job_id in result.get("auto_draft_job_ids", []):
        document_generator.generate_for_job(job_id)
        drafted += 1
    return drafted


def render_import_jobs(
    importer: JobImporter,
    document_generator: DocumentGenerator,
    email_ingestor: EmailAlertIngestor,
    gmail_ingestor: GmailIngestor,
) -> None:
    st.markdown("#### Import Jobs")
    st.caption(
        "Manual import tools for testing, pasted jobs, CSV uploads, saved-search text, and email alert files. "
        "All imported Auckland jobs are drafted automatically."
    )

    manual_tab, csv_tab, email_tab, search_tab = st.tabs(
        ["Paste Job Description", "Upload CSV", "Email Alert Import", "Saved Search URL / Page Text"]
    )

    with manual_tab:
        render_manual_import(importer, document_generator)
    with csv_tab:
        render_csv_import(importer, document_generator)
    with email_tab:
        render_email_import(importer, document_generator, email_ingestor, gmail_ingestor)
    with search_tab:
        render_saved_search_import(importer, document_generator)


def render_manual_import(importer: JobImporter, document_generator: DocumentGenerator) -> None:
    with st.form("manual_job_form", clear_on_submit=False):
        source = st.selectbox("Source", ["linkedin", "seek", "trademe", "manual"])
        url = st.text_input("Job URL")
        title = st.text_input("Job title")
        company = st.text_input("Company")
        location = st.text_input("Location", value="Auckland")
        salary = st.text_input("Salary")
        source_job_id = st.text_input("Source job ID")
        description = st.text_area("Job description", height=240)
        submitted = st.form_submit_button("Save job")

    if submitted:
        if not title or not company or not description:
            st.error("Title, company, and job description are required.")
            return

        result = importer.import_manual_job(
            source=source,
            url=url,
            title=title,
            company=company,
            location=location,
            salary=salary,
            description=description,
            source_job_id=source_job_id or None,
            import_channel="manual",
        )
        if result["created"]:
            drafted = _handle_auto_drafts(result, document_generator)
            st.success(
                f"Saved job #{result['job_id']} with fit score {result['fit_score']}. "
                f"Generated {drafted} draft package(s)."
            )
        else:
            st.warning(f"Duplicate prevented. Existing job #{result['job_id']} matched this entry.")


def render_csv_import(importer: JobImporter, document_generator: DocumentGenerator) -> None:
    st.caption("Supported columns: title, company, location, url, source, description, salary, date_found, source_job_id.")
    uploaded = st.file_uploader("Upload CSV", type=["csv"])
    if uploaded is None:
        sample = io.StringIO()
        writer = csv.DictWriter(
            sample,
            fieldnames=["title", "company", "location", "url", "source", "description", "salary", "date_found", "source_job_id"],
        )
        writer.writeheader()
        writer.writerow(
            {
                "title": "Data Analyst",
                "company": "Example Co",
                "location": "Auckland",
                "url": "https://www.linkedin.com/jobs/view/example",
                "source": "linkedin",
                "description": "SQL, Power BI, reporting, stakeholders, Auckland.",
                "salary": "$100,000",
                "date_found": "2026-05-07",
                "source_job_id": "12345",
            }
        )
        st.download_button("Download sample CSV", sample.getvalue(), file_name="job_import_template.csv")
        return

    if st.button("Import CSV"):
        frame = pd.read_csv(uploaded)
        result = importer.import_csv(frame)
        drafted = _handle_auto_drafts(result, document_generator)
        st.success(
            f"Imported {result['created_count']} new jobs. "
            f"Skipped {result['duplicate_count']} duplicates from {result['total_rows']} rows. "
            f"Generated {drafted} draft package(s)."
        )
        if result["errors"]:
            for item in result["errors"]:
                st.write(f"- Row {item['row']}: {item['error']}")


def render_email_import(
    importer: JobImporter,
    document_generator: DocumentGenerator,
    email_ingestor: EmailAlertIngestor,
    gmail_ingestor: GmailIngestor,
) -> None:
    st.caption("Paste email text or upload `.txt` / `.eml` alert files from LinkedIn, SEEK, or Trade Me Jobs.")
    gmail_status = gmail_ingestor.get_connection_status()
    st.write(
        {
            "Gmail status": "Connected" if gmail_status["authenticated"] else "Needs setup",
            "Last sync": gmail_status["last_sync_timestamp"] or "Never",
            "Last imported jobs": gmail_status["last_sync_imported_jobs"],
        }
    )

    uploaded_files = st.file_uploader(
        "Upload email alert files",
        type=["txt", "eml"],
        accept_multiple_files=True,
        key="email_alert_uploads",
    )
    if st.button("Import uploaded email alert files"):
        if not uploaded_files:
            st.error("Upload one or more `.txt` or `.eml` files first.")
        else:
            total = {"created": 0, "duplicates": 0, "failed": 0, "auto_drafted": 0, "files_processed": 0}
            for uploaded in uploaded_files:
                result = email_ingestor.ingest_uploaded_file(uploaded.name, uploaded.getvalue())
                total["created"] += result["created"]
                total["duplicates"] += result["duplicates"]
                total["failed"] += result["failed"]
                total["auto_drafted"] += result["auto_drafted"]
                total["files_processed"] += result["files_processed"]

            st.success(
                f"Processed {total['files_processed']} file(s). Imported {total['created']} job(s), "
                f"skipped {total['duplicates']} duplicate(s), failed {total['failed']} item(s), "
                f"and generated {total['auto_drafted']} draft package(s)."
            )

    email_text = st.text_area("Job alert email text", height=260, key="email_alert_text")
    if st.button("Import pasted email alert text"):
        if not email_text.strip():
            st.error("Paste the email text first.")
            return
        result = importer.import_email_alert_text(email_text)
        drafted = _handle_auto_drafts(result, document_generator)
        st.success(
            f"Imported {result['created_count']} Auckland job(s), skipped {result['duplicate_count']} duplicate(s), "
            f"filtered out {result.get('filtered_out_count', 0)} non-Auckland job(s), "
            f"and generated {drafted} draft package(s)."
        )


def render_saved_search_import(importer: JobImporter, document_generator: DocumentGenerator) -> None:
    st.caption("Paste a saved search URL and the visible job list text. The app will only parse what you provide.")
    search_url = st.text_input("Saved search URL")
    page_text = st.text_area("Visible page text", height=260, key="saved_search_text")
    if search_url:
        st.markdown(f"Review source page manually: [Open search URL]({search_url})")
    if st.button("Import saved search results"):
        if not search_url and not page_text.strip():
            st.error("Add a saved search URL or paste visible page text.")
            return
        result = importer.import_saved_search_text(search_url, page_text)
        drafted = _handle_auto_drafts(result, document_generator)
        st.success(
            f"Imported {result['created_count']} jobs, skipped {result['duplicate_count']} duplicates, "
            f"and generated {drafted} draft package(s)."
        )


def render_dashboard(
    tracker: ApplicationTracker,
    document_generator: DocumentGenerator,
    job_enricher: JobEnricher,
    *,
    heading: str,
    focus_bucket: str,
    show_bulk_panel: bool = False,
) -> None:
    st.subheader(heading)
    render_dashboard_css()
    render_gmail_sync_panel()
    summary = _build_queue_summary(tracker)
    if focus_bucket == "unprocessed":
        st.caption(
            f"{summary['unprocessed_count']} job(s) waiting | "
            f"Estimated run: {summary['estimated_prepare_total']}"
        )
    elif focus_bucket == "problems":
        st.caption(f"{summary['problem_count']} job(s) need attention.")
    elif focus_bucket == "ready":
        st.caption(
            f"{summary['ready_count']} job(s) ready | "
            f"Estimated submit run: {summary['estimated_submit_total']}"
        )
    jobs = tracker.search_jobs(
        query="",
        source="All",
        status="All",
        min_fit_score=0,
        company="",
        date_from=None,
        date_to=None,
    )
    if not jobs.empty:
        jobs = jobs[
            jobs.apply(
                lambda row: str(row.get("apply_type") or "").strip().lower() == "quick apply"
                or bool(row.get("has_quick_apply_button")),
                axis=1,
            )
        ].copy()
        jobs = jobs[jobs.apply(_is_auckland_job, axis=1)].copy()
        effective_status_all = _effective_status_series(jobs).astype(str).str.strip().str.lower()
        jobs = jobs[~effective_status_all.isin(["applied", "closed", "ignored", "archived", "rejected"])].copy()
    if jobs.empty:
        if show_bulk_panel:
            render_bulk_quick_apply_panel(tracker.database, document_generator)
        st.info("No Quick Apply jobs are available right now. Run Gmail sync or wait for a new job alert email.")
        return
    if show_bulk_panel:
        render_bulk_quick_apply_panel(tracker.database, document_generator)
    render_current_jobs_table(jobs, tracker, document_generator, job_enricher, focus_bucket=focus_bucket)


def render_bulk_quick_apply_panel(database: Database, document_generator: DocumentGenerator) -> None:
    st.markdown("**Bulk SEEK Quick Apply**")
    controls = st.columns([1.2, 1.0, 1.4])
    max_jobs = int(controls[0].number_input("Max jobs", min_value=1, max_value=50, value=5, step=1, key="bulk_quick_apply_max_jobs"))
    dry_run = controls[1].checkbox("Dry run preview", value=True, key="bulk_quick_apply_dry_run")
    run_clicked = controls[2].button("Prepare all Quick Apply jobs", use_container_width=True, key="bulk_quick_apply_run")

    selection_pool = find_quick_apply_jobs(database, max_jobs=50)
    selection_options = {
        f"{int(job['job_id'])} - {str(job.get('title') or '')}": int(job["job_id"])
        for job in selection_pool
    }
    selected_job_labels = st.multiselect(
        "Choose specific Quick Apply jobs to run (optional)",
        options=list(selection_options.keys()),
        default=[],
        key="bulk_quick_apply_selected_jobs",
    )
    selected_job_ids = [selection_options[label] for label in selected_job_labels]

    preview_jobs = find_quick_apply_jobs(
        database,
        max_jobs=max_jobs,
        selected_job_ids=selected_job_ids or None,
    )
    prepare_timing = _load_timing_history(database, "seek_prepare_timing")
    avg_prepare_seconds = _average_duration_seconds(prepare_timing)
    max_prepare_seconds = _max_duration_seconds(prepare_timing)
    estimated_prepare_seconds = avg_prepare_seconds * len(preview_jobs)
    estimated_prepare_max_seconds = max_prepare_seconds * len(preview_jobs)
    controls[0].caption(
        f"Estimated run: {_format_duration_seconds(estimated_prepare_seconds)} | "
        f"Max run: {_format_duration_seconds(estimated_prepare_max_seconds)}"
    )
    active_processing = _load_active_processing_state()
    if str(active_processing.get("mode") or "").strip() == "prepare":
        elapsed_label = _processing_elapsed_label(active_processing)
        if elapsed_label:
            st.caption(f"Current prepare run time: {elapsed_label}")
    preview_rows = [
        {
            "job_id": int(job["job_id"]),
            "title": str(job.get("title") or ""),
            "company": str(job.get("company") or ""),
            "apply_type": str(job.get("apply_type") or "Unknown - extraction failed"),
            "has_quick_apply_button": bool(job.get("has_quick_apply_button")),
            "job_url": str(job.get("url") or ""),
        }
        for job in preview_jobs
    ]
    if dry_run:
        if selected_job_ids:
            st.caption("Dry run preview of the selected Quick Apply jobs that would be processed.")
        else:
            st.caption("Dry run preview of Quick Apply jobs that would be processed.")
        if preview_rows:
            st.dataframe(pd.DataFrame(preview_rows), use_container_width=True, hide_index=True)
        else:
            st.info("No Quick Apply jobs matched the current stored data.")

    if not run_clicked:
        return
    if not preview_jobs:
        st.warning("No Quick Apply jobs available to prepare.")
        return

    progress_bar = st.progress(0.0)
    current_job_placeholder = st.empty()
    counts_placeholder = st.empty()
    current_failure_placeholder = st.empty()
    results_placeholder = st.empty()
    paused_statuses = {
        "paused_for_seek_verification",
        "paused_for_captcha",
        "paused_for_seek_login",
        "paused_for_manual_resume_upload",
        "paused_for_manual_questionnaire",
    }

    def update_progress(completed: int, total: int, title: str, company: str, current_results: list) -> None:
        progress_bar.progress(0.0 if total <= 0 else completed / total)
        current_job_placeholder.caption(f"Current job: {title or 'Unknown'} at {company or 'Unknown'}")
        success_count = sum(1 for item in current_results if item.status == "ready_for_human_review")
        failed_count = sum(1 for item in current_results if item.status == "failed_needs_fix")
        paused_count = sum(1 for item in current_results if item.status in paused_statuses)
        skipped_count = sum(1 for item in current_results if item.status == "skipped")
        counts_placeholder.caption(
            f"Completed {completed}/{total} | Success {success_count} | Paused {paused_count} | Failed {failed_count} | Skipped {skipped_count}"
        )
        if current_results:
            latest_result = current_results[-1]
            if latest_result.status in paused_statuses:
                current_failure_placeholder.warning(
                    f"Latest paused: {latest_result.title or 'Unknown'} - {latest_result.failure_reason or latest_result.root_failure_reason or 'Manual continuation required.'}"
                )
            elif latest_result.status == "failed_needs_fix":
                current_failure_placeholder.error(
                    f"Latest failure: {latest_result.title or 'Unknown'} — {latest_result.failure_reason or latest_result.root_failure_reason or 'Unknown failure.'}"
                )
            elif latest_result.status == "skipped":
                current_failure_placeholder.warning(
                    f"Latest skipped: {latest_result.title or 'Unknown'} — {latest_result.failure_reason or latest_result.root_failure_reason or 'Skipped.'}"
                )
            else:
                current_failure_placeholder.success(
                    f"Latest success: {latest_result.title or 'Unknown'} is ready for human review."
                )

    _clear_processing_cancel_request()
    _set_active_processing_state("prepare", f"Preparing up to {len(preview_jobs)} Quick Apply job(s).")
    try:
        results = run_bulk_quick_apply(
            database,
            document_generator,
            max_jobs=max_jobs,
            dry_run=False,
            progress_callback=update_progress,
            selected_job_ids=selected_job_ids or None,
            should_cancel=_processing_cancel_requested,
        )
    finally:
        _clear_active_processing_state()
        _clear_processing_cancel_request()
    progress_bar.progress(1.0)
    results_rows = [
        {
            "job_id": item.job_id,
            "title": item.title,
            "company": item.company,
            "status": item.status,
            "resume_included": item.resume_included,
            "cover_letter_included": item.cover_letter_included,
            "questions_answered": item.questions_answered,
            "failure_reason": item.failure_reason,
            "evidence_folder": item.evidence_folder,
        }
        for item in results
    ]
    st.session_state["bulk_quick_apply_results"] = results
    results_placeholder.dataframe(pd.DataFrame(results_rows), use_container_width=True, hide_index=True)
    failed_results = [item for item in results if item.status == "failed_needs_fix"]
    paused_results = [item for item in results if item.status in paused_statuses]
    skipped_results = [item for item in results if item.status == "skipped"]
    if paused_results:
        paused = paused_results[0]
        st.warning(f"{paused.title or 'A job'} paused. Reason: {paused.failure_reason}. Evidence: {paused.evidence_folder}")
        paused_rows = [
            {
                "job_id": item.job_id,
                "title": item.title,
                "company": item.company,
                "status": item.status,
                "reason": item.failure_reason or item.root_failure_reason,
                "evidence_folder": item.evidence_folder,
            }
            for item in paused_results
        ]
        st.caption("Paused jobs")
        st.dataframe(pd.DataFrame(paused_rows), use_container_width=True, hide_index=True)
        st.caption("Resume a paused job")
        for item in paused_results:
            label = f"Resume {item.job_id} - {item.title or 'Paused SEEK job'}"
            if st.button(label, key=f"resume_paused_seek_job_{item.job_id}", use_container_width=True):
                result = assist_seek_apply(
                    item.job_id,
                    DB_PATH,
                    visible=True,
                    keep_browser_open_override=True,
                    resume_upload_only=False,
                )
                debug_path = Path(result.debug_dir) if result.debug_dir else DB_PATH.parent / "debug_apply_assist"
                if result.status == "prepared_for_manual_review":
                    st.success(f"{item.title or 'Paused SEEK job'} resumed successfully and is ready for human review.")
                elif result.status in paused_statuses:
                    st.warning(result.error or f"{item.title or 'Paused SEEK job'} is still paused and needs manual continuation.")
                else:
                    st.error(result.error or f"{item.title or 'Paused SEEK job'} failed during resume.")
                st.caption(f"Debug artifacts: {debug_path}")
                st.caption(f"Final URL: {result.final_url or 'Unknown'}")
                if result.steps_completed:
                    st.caption(f"Steps completed: {', '.join(result.steps_completed)}")
            paused_job_url = str(getattr(item, "final_url", "") or "")
            if not paused_job_url:
                paused_details = database.get_job_details(int(item.job_id))
                if paused_details:
                    paused_job_url = str(paused_details.get("url") or "")
            if paused_job_url and st.button(
                f"Open {item.job_id} in Chrome",
                key=f"open_paused_seek_job_{item.job_id}",
                use_container_width=True,
            ):
                channel, profile_root = open_seek_url_in_browser(
                    DB_PATH,
                    target_url=paused_job_url,
                    use_bulk_profile=False,
                )
                st.success(f"Opened {item.title or 'paused SEEK job'} in {channel}.")
                st.caption(f"Profile root: {profile_root or 'Default browser profile'}")
    if failed_results:
        failed = failed_results[0]
        st.error(f"{failed.title or 'A job'} needs review. Reason: {failed.failure_reason}. Evidence: {failed.evidence_folder}")
        failure_rows = [
            {
                "title": item.title,
                "company": item.company,
                "reason": item.failure_reason or item.root_failure_reason,
                "evidence_folder": item.evidence_folder,
            }
            for item in failed_results
        ]
        st.caption("Failure summary")
        st.dataframe(pd.DataFrame(failure_rows), use_container_width=True, hide_index=True)
    if skipped_results:
        skipped_rows = [
            {
                "title": item.title,
                "company": item.company,
                "reason": item.failure_reason or item.root_failure_reason,
            }
            for item in skipped_results
        ]
        st.caption("Skipped jobs")
        st.dataframe(pd.DataFrame(skipped_rows), use_container_width=True, hide_index=True)
    cancelled_results = [item for item in results if item.status == "cancelled"]
    if cancelled_results:
        st.warning("Bulk Quick Apply was cancelled before all selected jobs finished.")
    elif not failed_results and not paused_results:
        st.success("Bulk Quick Apply preparation completed without submission.")


def render_dashboard_summary(tracker: ApplicationTracker, jobs: pd.DataFrame) -> None:
    if jobs.empty:
        return

    stats = build_dashboard_stats(jobs)
    col1, col2, col3, col4, col5, col6 = st.columns(6)
    col1.metric("Quick Apply jobs", stats["total_jobs"])
    col2.metric("Quick Apply", stats["jobs_with_quick_apply"])
    col3.metric("Apply", stats["jobs_with_normal_apply"])
    col4.metric("Salary extracted", stats["jobs_with_salary_extracted"])
    col5.metric("Missing salary", stats["jobs_missing_salary"])
    col6.metric("Avg calc salary", stats["average_calculated_salary"])

    summary_col1, summary_col2 = st.columns(2)
    with summary_col1:
        st.markdown("**Fit level counts**")
        st.dataframe(pd.DataFrame(stats["fit_level_counts"]), use_container_width=True, hide_index=True)
    with summary_col2:
        st.markdown("**Chosen CV counts**")
        st.dataframe(pd.DataFrame(stats["chosen_cv_counts"]), use_container_width=True, hide_index=True)


def _load_cv_profiles() -> list[dict]:
    profiles_path = DATA_DIR / "cv_profiles.json"
    if not profiles_path.exists():
        return []
    try:
        payload = json.loads(profiles_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []
    profiles = payload.get("profiles")
    if not isinstance(profiles, list):
        return []
    return [item for item in profiles if isinstance(item, dict)]


def _display_cv_profile_name(profile_name: str) -> str:
    raw = str(profile_name or "").strip()
    if not raw:
        return "Pending"
    return CV_PROFILE_LABELS.get(raw, raw.replace("_", " ").title())


def _extract_docx_text(file_path: Path, max_chars: int = 5000) -> str:
    if not file_path.exists() or file_path.suffix.lower() != ".docx":
        return ""
    try:
        with zipfile.ZipFile(file_path) as zf:
            xml_bytes = zf.read("word/document.xml")
        root = ET.fromstring(xml_bytes)
        text_parts = []
        for node in root.iter():
            if node.tag.endswith("}t") and node.text:
                text_parts.append(node.text)
            elif node.tag.endswith("}p"):
                text_parts.append("\n")
        text = "".join(text_parts)
        text = "\n".join(line.strip() for line in text.splitlines() if line.strip())
        return text[:max_chars]
    except Exception:  # noqa: BLE001
        return ""


def render_cv_library(tracker: ApplicationTracker) -> None:
    st.subheader("CV Library")
    profiles = _load_cv_profiles()
    st.caption(f"{len(profiles)} CV profile(s) available.")
    if not profiles:
        st.info("No CV profiles found.")
        return
    for profile in profiles:
        profile_name = str(profile.get("profile_name") or "unknown")
        file_path = APP_ROOT / str(profile.get("file_path") or "")
        with st.expander(_display_cv_profile_name(profile_name), expanded=False):
            st.caption(f"File: {file_path}")
            st.write(str(profile.get("positioning_summary") or ""))
            strengths = profile.get("strengths") or []
            if strengths:
                st.caption("Strengths: " + ", ".join(str(item) for item in strengths))
            weaknesses = profile.get("weaknesses") or []
            if weaknesses:
                st.caption("Weaknesses: " + ", ".join(str(item) for item in weaknesses))
            when_to_use = str(profile.get("when_to_use") or "").strip()
            if when_to_use:
                st.caption(f"When to use: {when_to_use}")
            targets = profile.get("target_roles") or []
            if targets:
                st.caption("Target roles: " + ", ".join(str(item) for item in targets))
            actions = st.columns(2)
            if file_path.exists():
                actions[0].download_button(
                    "Download CV",
                    data=file_path.read_bytes(),
                    file_name=file_path.name,
                    key=f"download_cv_{profile_name}",
                    use_container_width=True,
                )
            if actions[1].button("Open CV folder", key=f"open_cv_folder_{profile_name}", use_container_width=True):
                open_application_folder(file_path.parent)
            preview_text = _extract_docx_text(file_path)
            if preview_text:
                st.text_area(
                    "CV preview",
                    preview_text,
                    height=260,
                    disabled=True,
                    key=f"cv_preview_{profile_name}",
                )
            else:
                st.caption("Preview text unavailable for this CV file.")


def render_ready_review_queue(jobs: pd.DataFrame, tracker: ApplicationTracker) -> None:
    queue = _ready_review_queue(jobs, tracker)
    st.markdown("**Ready To Review / Submit**")
    st.caption("These jobs have already been prepared to SEEK's review page. Review the saved answers below, then use one click to submit.")
    if not queue:
        st.info("No prepared Quick Apply jobs are waiting for final submit yet.")
        return

    submit_controls = st.columns([1.3, 3.0])
    if submit_controls[0].button("Submit all ready jobs", key="submit_all_ready_jobs", use_container_width=True):
        results: list[dict[str, str]] = []
        for item in queue:
            details = item["details"]
            result = submit_seek_review(
                int(item["job_id"]),
                DB_PATH,
                review_url=str(item["review_url"] or details.get("url") or ""),
                visible=False,
                use_bulk_profile=False,
            )
            results.append(
                {
                    "job_id": str(item["job_id"]),
                    "title": str(details.get("title") or ""),
                    "status": result.status,
                    "message": result.error or result.final_url or "",
                }
            )
        tracker.database.log_run("seek_submit", "bulk_submit_ready_jobs", "success", f"submitted_count={len(results)}")
        st.session_state["submit_all_ready_results"] = results
        st.rerun()
    submit_results = st.session_state.get("submit_all_ready_results") or []
    if submit_results:
        st.dataframe(pd.DataFrame(submit_results), use_container_width=True, hide_index=True)

    st.markdown(
        """
        <div class="quick-queue-header">
          <div class="quick-queue-grid">
            <strong>Title</strong>
            <strong>Page Salary</strong>
            <strong>Calc Salary</strong>
            <strong>Fit / CV</strong>
            <strong>Location</strong>
            <strong>Posted</strong>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    for item in queue:
        details = item["details"]
        summary = build_job_display_summary(details, item["question_answers"])
        job_id = int(item["job_id"])
        run_dir = Path(item["run_dir"])
        cols = st.columns([2.4, 1.1, 1.1, 1.3, 1.2, 1.0])
        cols[0].write(summary["title"])
        cols[1].write(summary["page_salary"])
        cols[2].write(summary["calculated_salary"])
        cols[3].write(summary["fit_and_cv"])
        cols[4].write(str(details.get("location") or "Auckland"))
        cols[5].write(_posted_age_label(details))

        with st.expander(f"Review details for {summary['title']}", expanded=False):
            st.caption(f"Evidence folder: {run_dir}")
            st.caption(f"Review page screenshot: {run_dir / 'final_review.png'}")
            st.caption(f"Calculated salary basis: {summary['calculated_salary_reason']}")
            if item["question_answers"]:
                st.markdown("**Questions and answers**")
                st.dataframe(pd.DataFrame(item["question_answers"]), use_container_width=True, hide_index=True)
            else:
                st.caption("No question/answer capture was saved for this job.")
            action_cols = st.columns(3)
            if action_cols[0].button("Open review screenshot folder", key=f"open_review_folder_{job_id}", use_container_width=True):
                open_application_folder(run_dir)
            if item["review_url"]:
                action_cols[1].link_button("Open review page", item["review_url"], use_container_width=True)
            submit_key = f"submit_prepared_job_{job_id}"
            if action_cols[2].button("Submit this job", key=submit_key, use_container_width=True):
                result = submit_seek_review(
                    job_id,
                    DB_PATH,
                    review_url=str(item["review_url"] or details.get("url") or ""),
                    visible=False,
                    use_bulk_profile=False,
                )
                if result.status == "applied":
                    st.success("Job submitted successfully.")
                    _persist_submit_result_state(
                        tracker,
                        job_id,
                        result,
                        source_note="Submitted from dashboard review queue.",
                    )
                    st.rerun()
                elif result.status == "skipped" and "no longer advertised" in str(result.error or "").lower():
                    _persist_submit_result_state(
                        tracker,
                        job_id,
                        result,
                        source_note="Submitted from dashboard review queue.",
                    )
                    st.warning(result.error or "This SEEK job is no longer advertised.")
                    st.rerun()
                elif result.status in {"paused_for_seek_login", "paused_for_seek_verification", "paused_for_captcha"}:
                    st.warning(result.error or "SEEK session needs manual continuation before final submit.")
                elif result.status == "browser_profile_locked":
                    st.error(result.error or "Browser profile was locked.")
                else:
                    st.error(result.error or "Final submit did not complete.")


def render_dashboard_filters() -> dict:
    query = st.text_input("Search title, company, or description")
    col1, col2, col3 = st.columns(3)
    default_statuses = [status for status in DESIRED_DEFAULT_STATUSES if status in STATUS_OPTIONS]
    with col1:
        source = st.selectbox("Source filter", ["All", "linkedin", "seek", "trademe", "manual", "email_alert"])
    with col2:
        statuses = st.multiselect(
            "Status filter",
            STATUS_OPTIONS,
            default=default_statuses,
        )
    with col3:
        company = st.text_input("Company filter")

    status_value = statuses[0] if len(statuses) == 1 else "All"
    filters = {
        "query": query,
        "source": source,
        "status": status_value,
        "min_fit_score": 0,
        "company": company,
        "date_from": None,
        "date_to": None,
    }
    filters["statuses"] = statuses
    return filters


def render_current_jobs_table(
    jobs: pd.DataFrame,
    tracker: ApplicationTracker,
    document_generator: DocumentGenerator,
    job_enricher: JobEnricher,
    *,
    focus_bucket: str = "unprocessed",
) -> None:
    display_jobs = jobs.copy()
    sort_column = "imported_at" if "imported_at" in display_jobs.columns else "date_found"
    display_jobs = display_jobs.sort_values(by=[sort_column, "fit_score"], ascending=[False, False], kind="stable")
    latest_runs = _latest_apply_runs_by_job()
    submit_results = st.session_state.get("submit_all_ready_results") or []
    submit_result_by_job = {
        str(entry.get("job_id") or ""): entry
        for entry in submit_results
        if isinstance(entry, dict)
    }
    queue_items: list[dict[str, object]] = []
    for _, row in display_jobs.iterrows():
        job_id = int(row["job_id"])
        details = tracker.get_job_details(job_id)
        if details is None:
            continue
        latest_run = latest_runs.get(job_id)
        latest_payload = dict(latest_run["payload"]) if latest_run else {}
        latest_run_status = str(latest_payload.get("status") or "").strip()
        question_answers = _load_dynamic_question_answers(Path(latest_run["run_dir"])) if latest_run else []
        review_url = _extract_final_url_from_run_log(Path(latest_run["run_dir"])) if latest_run else ""
        effective_status = str(details.get("application_status") or details.get("status") or "").strip().lower()
        issue_text = ""
        if latest_run:
            issue_text = str(latest_payload.get("failure_reason") or latest_payload.get("root_failure_reason") or "").strip()
        latest_submit_result = submit_result_by_job.get(str(job_id))
        latest_submit_status = str((latest_submit_result or {}).get("status") or "").strip()
        latest_submit_message = str((latest_submit_result or {}).get("message") or "").strip()
        if latest_submit_status and latest_submit_status != "applied":
            issue_text = latest_submit_message or issue_text
        bucket = _bucket_quick_apply_queue_item(
            latest_run=latest_run,
            latest_run_status=latest_run_status,
            latest_submit_status=latest_submit_status,
        )
        problem_category_key = ""
        problem_category_label = ""
        if bucket == "problems":
            problem_category_key, problem_category_label = _categorize_problem_job(
                latest_run_status=latest_run_status,
                issue_text=issue_text,
                latest_payload=latest_payload,
                latest_run_dir=Path(latest_run["run_dir"]) if latest_run else None,
                latest_submit_status=latest_submit_status,
            )
        queue_items.append(
            {
                "job_id": job_id,
                "details": details,
                "latest_run": latest_run,
                "latest_run_status": latest_run_status,
                "latest_payload": latest_payload,
                "question_answers": question_answers,
                "review_url": review_url,
                "effective_status": effective_status,
                "issue_text": issue_text,
                "bucket": bucket,
                "problem_category_key": problem_category_key,
                "problem_category_label": problem_category_label,
            }
        )

    unprocessed_items = [item for item in queue_items if item["bucket"] == "unprocessed"]
    ready_items = [item for item in queue_items if item["bucket"] == "ready"]
    problem_items = [item for item in queue_items if item["bucket"] == "problems"]

    unprocessed_count = len(unprocessed_items)
    ready_count = len(ready_items)
    problem_count = len(problem_items)

    if focus_bucket == "ready" and ready_items:
        submit_controls = st.columns([1.2, 3.2])
        submit_progress_bar = st.progress(0.0)
        submit_current_placeholder = st.empty()
        submit_counts_placeholder = st.empty()
        submit_status_placeholder = st.empty()
        active_processing = _load_active_processing_state()
        if str(active_processing.get("mode") or "").strip() == "submit":
            elapsed_label = _processing_elapsed_label(active_processing)
            if elapsed_label:
                st.caption(f"Current submit run time: {elapsed_label}")

        def update_submit_progress(completed: int, total: int, title: str, company: str, current_results: list[dict[str, str]]) -> None:
            submit_progress_bar.progress(0.0 if total <= 0 else completed / total)
            submit_current_placeholder.caption(f"Current job: {title or 'Unknown'} at {company or 'Unknown'}")
            passed_count = sum(1 for item in current_results if str(item.get("status") or "") == "applied")
            skipped_count = sum(
                1
                for item in current_results
                if str(item.get("status") or "") in {"already_applied_skipped", "auto_apply_blocked_skipped", "not_attempted_after_batch_stop", "cancelled_before_attempt"}
            )
            failed_count = sum(
                1
                for item in current_results
                if str(item.get("status") or "") not in {"applied", "already_applied_skipped", "not_attempted_after_batch_stop", "cancelled_before_attempt"}
            )
            submit_counts_placeholder.caption(
                f"Completed {completed}/{total} | Passed {passed_count} | Failed {failed_count} | Skipped {skipped_count}"
            )
            if current_results:
                latest = current_results[-1]
                latest_status = str(latest.get("status") or "")
                latest_title = str(latest.get("title") or "Unknown")
                latest_message = str(latest.get("message") or "").strip()
                if latest_status == "applied":
                    submit_status_placeholder.success(f"Latest success: {latest_title} submitted.")
                elif latest_status in {"already_applied_skipped", "auto_apply_blocked_skipped", "not_attempted_after_batch_stop", "cancelled_before_attempt"}:
                    submit_status_placeholder.warning(f"Latest skipped: {latest_title} - {latest_message or 'Skipped.'}")
                else:
                    submit_status_placeholder.error(f"Latest failure: {latest_title} - {latest_message or 'Unknown failure.'}")

        if submit_controls[0].button(
            "Submit all ready jobs",
            key=f"submit_all_ready_jobs_inline_{focus_bucket}",
            use_container_width=True,
        ):
            _clear_processing_cancel_request()
            _set_active_processing_state("submit", f"Submitting up to {len(ready_items)} ready job(s).")
            try:
                results = _submit_all_ready_jobs_batch(ready_items, tracker, progress_callback=update_submit_progress)
            finally:
                _clear_active_processing_state()
                _clear_processing_cancel_request()
            submit_progress_bar.progress(1.0)
            st.session_state["submit_all_ready_results"] = results
            st.rerun()
        submit_results = st.session_state.get("submit_all_ready_results") or []
        if submit_results:
            success_count = sum(1 for item in submit_results if str(item.get("status") or "") == "applied")
            failed_count = len(submit_results) - success_count
            if success_count:
                st.success(f"Submitted {success_count} ready job(s).")
            if failed_count:
                cancelled = next(
                    (
                        item
                        for item in submit_results
                        if str(item.get("status") or "") == "cancelled_before_attempt"
                    ),
                    None,
                )
                if cancelled:
                    st.warning("Batch submit was cancelled before all ready jobs were attempted.")
                else:
                    blocking = next(
                        (
                            item
                            for item in submit_results
                            if str(item.get("status") or "") in BATCH_SUBMIT_BLOCKING_STATUSES
                        ),
                        None,
                    )
                    if blocking:
                        st.warning(
                            f"{failed_count} ready job(s) did not submit. Batch stopped on "
                            f"{blocking.get('title') or ('job ' + str(blocking.get('job_id') or ''))}: "
                            f"{blocking.get('message') or 'Unknown failure.'}"
                        )
                    else:
                        st.warning(f"{failed_count} ready job(s) did not submit. Open details on those rows to review the latest state.")
    if focus_bucket == "unprocessed":
        st.markdown(f"### Unprocessed ({unprocessed_count})")
        if not unprocessed_items:
            st.info("No unprocessed Quick Apply jobs are waiting in the queue.")
        else:
            st.caption("These jobs have not been prepared yet.")
            render_queue_header()
            for item in unprocessed_items:
                render_job_row(item, tracker, document_generator, job_enricher)
    elif focus_bucket == "ready":
        st.markdown(f"### Ready to submit ({ready_count})")
        if not ready_items:
            st.info("No prepared Quick Apply jobs are waiting for final submit.")
        else:
            st.caption("These jobs have already been processed and are ready for your final review / submit.")
            render_queue_header()
            for item in ready_items:
                render_job_row(item, tracker, document_generator, job_enricher)
    elif focus_bucket == "problems":
        st.markdown(f"### Problems ({problem_count})")
        if not problem_items:
            st.info("No processed jobs are sitting in Problems right now.")
        else:
            problem_category_counts = Counter(
                str(item.get("problem_category_label") or PROBLEM_CATEGORY_LABELS["other"])
                for item in problem_items
            )
            problem_retry_results = st.session_state.get("problem_retry_results") or []
            if problem_retry_results:
                st.markdown("**Latest problem retry results**")
                success_count = sum(1 for item in problem_retry_results if str(item.get("status") or "") == "ready_for_human_review")
                paused_count = sum(1 for item in problem_retry_results if str(item.get("status") or "") in PAUSED_BULK_STATUSES)
                skipped_count = sum(1 for item in problem_retry_results if str(item.get("status") or "") == "skipped")
                failed_count = len(problem_retry_results) - success_count - paused_count - skipped_count
                summary_cols = st.columns(5)
                summary_cols[0].metric("Attempted", int(len(problem_retry_results)))
                summary_cols[1].metric("Success", int(success_count))
                summary_cols[2].metric("Paused", int(paused_count))
                summary_cols[3].metric("Failed", int(failed_count))
                summary_cols[4].metric("Skipped", int(skipped_count))
                if success_count:
                    st.success(f"Problem retry finished: {success_count} ready to submit.")
                if failed_count or paused_count or skipped_count:
                    st.warning(
                        f"Problem retry finished: {len(problem_retry_results)} attempted | "
                        f"{success_count} success | {paused_count} paused | {failed_count} failed | {skipped_count} skipped"
                    )
                st.dataframe(pd.DataFrame(problem_retry_results), use_container_width=True, hide_index=True)
                st.markdown("---")

            st.markdown("**Problem categories**")
            st.caption("These categories are inferred from the latest failure reason and the latest saved apply-run evidence.")
            category_rows = [
                {"Category": label, "Jobs": count}
                for label, count in sorted(problem_category_counts.items(), key=lambda item: (-item[1], item[0]))
            ]
            st.dataframe(pd.DataFrame(category_rows), use_container_width=True, hide_index=True)
            top_category_label = category_rows[0]["Category"] if category_rows else PROBLEM_CATEGORY_LABELS["other"]
            top_category_count = category_rows[0]["Jobs"] if category_rows else 0
            st.info(f"Top root-cause bucket: {top_category_label} ({top_category_count} job(s)).")

            prepare_timing = _load_timing_history(tracker.database, "seek_prepare_timing")
            avg_prepare_seconds = _average_duration_seconds(prepare_timing)
            max_prepare_seconds = _max_duration_seconds(prepare_timing)
            retry_selection_options = {
                f"{int(item['job_id'])} - {str(dict(item['details']).get('title') or '')}": int(item["job_id"])
                for item in problem_items
            }
            selected_problem_labels = list(st.session_state.get("problem_jobs_selected_retry", []))
            selected_problem_job_ids = [retry_selection_options[label] for label in selected_problem_labels if label in retry_selection_options]
            selected_problem_count = len(selected_problem_job_ids) if selected_problem_job_ids else problem_count
            auth_problem_items = [
                item
                for item in problem_items
                if str(item.get("problem_category_key") or "") == "login_verification"
            ]
            selected_auth_problem_count = (
                sum(1 for item in auth_problem_items if int(item["job_id"]) in set(selected_problem_job_ids))
                if selected_problem_job_ids
                else len(auth_problem_items)
            )
            st.caption(
                f"Selected retry: {selected_problem_count} job(s) | "
                f"Estimated run: {_format_duration_seconds(avg_prepare_seconds * selected_problem_count)} | "
                f"Max run: {_format_duration_seconds(max_prepare_seconds * selected_problem_count)}"
            )
            st.caption(
                f"All problem jobs: {problem_count} job(s) | "
                f"Estimated run: {_format_duration_seconds(avg_prepare_seconds * problem_count)} | "
                f"Max run: {_format_duration_seconds(max_prepare_seconds * problem_count)}"
            )
            if auth_problem_items:
                st.info(
                    f"Auth / verification blocked: {selected_auth_problem_count if selected_problem_job_ids else len(auth_problem_items)} "
                    f"selected of {len(auth_problem_items)} total. These jobs hit SEEK login, verification, or captcha gates."
                )

            retry_progress_bar = st.progress(0.0)
            retry_current_placeholder = st.empty()
            retry_counts_placeholder = st.empty()
            retry_status_placeholder = st.empty()
            active_processing = _load_active_processing_state()
            if str(active_processing.get("mode") or "").strip() == "prepare":
                elapsed_label = _processing_elapsed_label(active_processing)
                if elapsed_label:
                    st.caption(f"Current retry run time: {elapsed_label}")

            def update_retry_progress(completed: int, total: int, title: str, company: str, current_results: list[BulkPrepareResult]) -> None:
                retry_progress_bar.progress(0.0 if total <= 0 else completed / total)
                retry_current_placeholder.caption(f"Current job: {title or 'Unknown'} at {company or 'Unknown'}")
                success_count = sum(1 for item in current_results if item.status == "ready_for_human_review")
                failed_count = sum(1 for item in current_results if item.status == "failed_needs_fix")
                paused_count = sum(1 for item in current_results if item.status in PAUSED_BULK_STATUSES)
                skipped_count = sum(1 for item in current_results if item.status == "skipped")
                retry_counts_placeholder.caption(
                    f"Completed {completed}/{total} | Success {success_count} | Paused {paused_count} | Failed {failed_count} | Skipped {skipped_count}"
                )
                if current_results:
                    latest_result = current_results[-1]
                    if latest_result.status in PAUSED_BULK_STATUSES:
                        retry_status_placeholder.warning(
                            f"Latest paused: {latest_result.title or 'Unknown'} - "
                            f"{latest_result.failure_reason or latest_result.root_failure_reason or 'Manual continuation required.'}"
                        )
                    elif latest_result.status == "failed_needs_fix":
                        retry_status_placeholder.warning(
                            f"Latest failed: {latest_result.title or 'Unknown'} - "
                            f"{latest_result.failure_reason or latest_result.root_failure_reason or 'Needs attention.'}"
                        )
                    elif latest_result.status == "ready_for_human_review":
                        retry_status_placeholder.success(
                            f"Latest success: {latest_result.title or 'Unknown'} is ready to submit."
                        )
                    elif latest_result.status == "skipped":
                        retry_status_placeholder.info(
                            f"Latest skipped: {latest_result.title or 'Unknown'} - "
                            f"{latest_result.failure_reason or latest_result.root_failure_reason or 'Skipped.'}"
                        )

            retry_button_label = "Retry selected problem jobs" if selected_problem_job_ids else "Retry all problem jobs"
            selected_problem_labels = st.multiselect(
                "Choose specific problem jobs to retry (optional)",
                options=list(retry_selection_options.keys()),
                default=selected_problem_labels,
                key="problem_jobs_selected_retry",
            )
            retry_cols = st.columns([1.4, 3.0])
            retry_clicked = retry_cols[0].button(
                retry_button_label,
                key="retry_problem_jobs_button",
                use_container_width=True,
            )
            retry_cols[1].caption(
                "These jobs were already attempted once. Retry here will only rerun these problem jobs, "
                "not the unprocessed queue. If you select specific jobs above, only those will be retried."
            )
            if retry_clicked:
                selected_problem_job_ids = [retry_selection_options[label] for label in selected_problem_labels if label in retry_selection_options]
                st.session_state["problem_retry_results"] = []
                st.info(
                    f"Starting retry for {len(selected_problem_job_ids) if selected_problem_job_ids else problem_count} problem job(s)..."
                )
                _retry_problem_jobs_batch(
                    problem_items,
                    tracker,
                    document_generator,
                    selected_job_ids=selected_problem_job_ids or None,
                    progress_callback=update_retry_progress,
                )
            if auth_problem_items:
                auth_retry_label = (
                    "Refresh SEEK auth + retry selected auth-blocked jobs"
                    if selected_problem_job_ids
                    else "Refresh SEEK auth + retry auth-blocked jobs"
                )
                if st.button(
                    auth_retry_label,
                    key="retry_auth_problem_jobs_button",
                    use_container_width=True,
                ):
                    st.session_state["problem_retry_results"] = []
                    _retry_auth_problem_jobs(
                        problem_items,
                        tracker,
                        document_generator,
                        selected_job_ids=selected_problem_job_ids or None,
                        progress_callback=update_retry_progress,
                    )
            st.caption("These jobs were processed at least once but did not complete cleanly. The latest issue is shown inline.")
            grouped_problem_items: dict[str, list[dict[str, object]]] = {}
            for item in problem_items:
                label = str(item.get("problem_category_label") or PROBLEM_CATEGORY_LABELS["other"])
                grouped_problem_items.setdefault(label, []).append(item)
            for category_label, items_in_category in sorted(
                grouped_problem_items.items(),
                key=lambda pair: (-len(pair[1]), pair[0]),
            ):
                st.markdown(f"#### {category_label} ({len(items_in_category)})")
                render_queue_header()
                for item in items_in_category:
                    render_job_row(item, tracker, document_generator, job_enricher)


def render_queue_header() -> None:
    st.markdown(
        """
        <div class="quick-queue-header">
          <div class="quick-queue-grid">
            <strong>Title</strong>
            <strong>Page Salary</strong>
            <strong>Calc Salary</strong>
            <strong>Fit / CV</strong>
            <strong>Location</strong>
            <strong>Posted</strong>
            <strong>Status</strong>
            <strong>Issue</strong>
            <strong>Action</strong>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_job_row(item: dict[str, object], tracker: ApplicationTracker, document_generator: DocumentGenerator, job_enricher: JobEnricher) -> None:
    job_id = int(item["job_id"])
    details = dict(item["details"])
    documents = tracker.list_generated_documents(job_id)
    document_map = {doc["doc_type"]: doc for doc in documents}
    latest_run = item.get("latest_run")
    latest_run_status = str(item.get("latest_run_status") or "").strip()
    question_answers = list(item.get("question_answers") or [])
    review_url = str(item.get("review_url") or "")
    effective_status = str(item.get("effective_status") or "").strip().lower()
    issue_text = str(item.get("issue_text") or "").strip()
    problem_category_key = str(item.get("problem_category_key") or "")
    submit_results = st.session_state.get("submit_all_ready_results") or []
    submit_result_by_job = {
        str(entry.get("job_id") or ""): entry
        for entry in submit_results
        if isinstance(entry, dict)
    }
    latest_submit_result = submit_result_by_job.get(str(job_id))
    latest_submit_status = str((latest_submit_result or {}).get("status") or "").strip()
    latest_submit_message = str((latest_submit_result or {}).get("message") or "").strip()
    if latest_submit_status and latest_submit_status != "applied":
        issue_text = latest_submit_message or issue_text
    summary = build_job_display_summary(details, question_answers)
    if latest_run_status == "ready_for_human_review":
        status_text = "Ready to submit"
    elif problem_category_key == "login_verification" and latest_run_status:
        status_text = "Auth / CAPTCHA"
    elif latest_run_status == "skipped":
        status_text = "Skipped"
    elif latest_run_status.startswith("paused_for_"):
        status_text = "Paused"
    elif latest_run_status:
        status_text = "Problem"
    elif effective_status == "applied":
        status_text = "Applied"
    else:
        status_text = "Unprocessed"
    if latest_submit_status and latest_submit_status != "applied":
        status_text = "Problem"
    issue_display = _short_issue_text(issue_text)

    with st.container(border=True):
        cols = st.columns([2.1, 1.0, 1.0, 1.2, 1.2, 0.9, 1.0, 1.8, 1.1])
        cols[0].write(summary["title"])
        cols[1].write(summary["page_salary"])
        cols[2].write(summary["calculated_salary"])
        cols[3].write(summary["fit_and_cv"])
        cols[4].write(details["location"] or "Auckland")
        cols[5].write(_posted_age_label(details))
        cols[6].write(status_text)
        cols[7].write(issue_display or "-")
        if latest_run_status == "ready_for_human_review":
            if cols[8].button("Submit", key=f"submit_row_{job_id}", use_container_width=True):
                refreshed_details = tracker.get_job_details(job_id) or details
                if _details_indicate_applied(refreshed_details):
                    st.warning("This job is already marked as applied, so it was not submitted again.")
                    st.rerun()
                result = submit_seek_review(
                    job_id,
                    DB_PATH,
                    review_url=str(review_url or details.get("url") or ""),
                    visible=False,
                    use_bulk_profile=False,
                )
                if result.status == "applied":
                    _persist_submit_result_state(
                        tracker,
                        job_id,
                        result,
                        source_note="Submitted from compact dashboard row.",
                    )
                    st.success(f"Submitted {summary['title']}.")
                    st.rerun()
                elif result.status == "skipped" and "no longer advertised" in str(result.error or "").lower():
                    _persist_submit_result_state(
                        tracker,
                        job_id,
                        result,
                        source_note="Submitted from compact dashboard row.",
                    )
                    st.warning(result.error or "This SEEK job is no longer advertised.")
                    st.rerun()
                elif result.status in {"paused_for_seek_login", "paused_for_seek_verification", "paused_for_captcha"}:
                    st.warning(result.error or "SEEK session needs manual continuation before final submit.")
                elif result.status == "browser_profile_locked":
                    st.error(result.error or "Browser profile was locked.")
                else:
                    st.error(result.error or "Final submit did not complete.")
        elif latest_run_status and effective_status != "applied":
            retry_label = "Refresh auth + retry" if problem_category_key == "login_verification" else "Retry"
            if cols[8].button(retry_label, key=f"retry_row_{job_id}", use_container_width=True):
                if problem_category_key == "login_verification":
                    _retry_auth_problem_jobs([item], tracker, document_generator, selected_job_ids=[job_id])
                else:
                    _retry_prepare_job(job_id, tracker, details)
        else:
            cols[8].write(" ")
        if effective_status != "applied":
            if cols[8].button("Remove", key=f"remove_row_{job_id}", use_container_width=True):
                _remove_job_from_active_queue(job_id, tracker)

        with st.expander(f"Details for {summary['title']}", expanded=False):
            render_job_detail_contents(
                details,
                document_map,
                job_id,
                document_generator,
                job_enricher,
                latest_run=latest_run,
                latest_run_status=latest_run_status,
                question_answers=question_answers,
                review_url=review_url,
                tracker=tracker,
            )


def render_job_detail_contents(
    details: dict,
    document_map: dict[str, dict],
    job_id: int,
    document_generator: DocumentGenerator,
    job_enricher: JobEnricher,
    *,
    latest_run: dict | None = None,
    latest_run_status: str = "",
    question_answers: list[dict[str, str]] | None = None,
    review_url: str = "",
    tracker: ApplicationTracker | None = None,
) -> None:
    latest_payload = dict(latest_run["payload"]) if latest_run else {}
    latest_issue = str(latest_payload.get("failure_reason") or latest_payload.get("root_failure_reason") or "").strip()
    st.write(f"Relevance: {int(details['fit_score'])} {_relevance_label(int(details['fit_score']))}")
    st.caption(f"Imported {_age_label(details).lower()}")
    st.caption(f"Enrichment status: {details.get('enrichment_status') or 'pending'}")
    st.caption(f"Documents generated from: {_document_basis_label(details)}")
    st.caption(f"Enriched title: {details.get('enriched_title') or details.get('title') or 'Unknown'}")
    st.caption(f"Posted age: {_posted_age_label(details)}")
    st.caption(f"Date posted text: {details.get('date_posted') or details.get('date_posted_text') or 'Unknown'}")
    st.caption(f"Apply type: {details.get('apply_type') or 'unknown'}")
    st.caption(f"Has Apply button: {'Yes' if details.get('has_apply_button') else 'No'}")
    st.caption(f"Has Quick Apply button: {'Yes' if details.get('has_quick_apply_button') else 'No'}")
    st.caption(f"Salary text near apply: {details.get('apply_area_salary_text') or 'Not extracted'}")
    latest_question_answers = question_answers or []
    resolved_salary, resolved_salary_reason = _resolved_salary_display(details, latest_question_answers)
    if details.get("salary_expectation_text"):
        estimate_low = details.get("salary_estimate_low")
        estimate_high = details.get("salary_estimate_high")
        range_text = ""
        if estimate_low and estimate_high:
            range_text = f" ({estimate_low}k-{estimate_high}k)"
        st.caption(f"Salary expectation: {details['salary_expectation_text']}{range_text}")
        reasoning = _salary_reasoning(details)
        if reasoning:
            st.caption(f"Salary reasoning: {reasoning[0]}")
    st.caption(f"Displayed calculated salary: {resolved_salary}")
    st.caption(f"Displayed salary basis: {resolved_salary_reason}")
    st.caption(f"Description characters: {len(_best_description(details))}")
    st.caption(
        f"Selected CV profile: {_display_cv_profile_name(str(details.get('selected_cv_profile') or 'Pending'))}"
    )
    st.caption(f"CV selection reason: {details.get('cv_selection_reason') or 'Not selected yet.'}")
    if latest_run_status and latest_run_status != "ready_for_human_review" and latest_issue:
        st.caption(f"Latest issue: {latest_issue}")
    if details.get("selected_cv_path"):
        st.caption(f"Chosen CV path: {details['selected_cv_path']}")
    if details.get("enrichment_error"):
        st.caption(f"Enrichment error: {details['enrichment_error']}")
    if latest_run_status == "ready_for_human_review" and latest_run:
        run_dir = Path(latest_run["run_dir"])
        st.caption(f"Evidence folder: {run_dir}")
        st.caption(f"Review page screenshot: {run_dir / 'final_review.png'}")
        if latest_question_answers:
            st.markdown("**Questions and answers**")
            st.dataframe(pd.DataFrame(latest_question_answers), use_container_width=True, hide_index=True)
        review_cols = st.columns(3)
        if review_cols[0].button("Open review screenshot folder", key=f"open_review_folder_{job_id}", use_container_width=True):
            open_application_folder(run_dir)
        if review_url:
            review_cols[1].link_button("Open review page", review_url, use_container_width=True)
        job_url = str(details.get("url") or "")
        if job_url and review_cols[2].button("Open job in Chrome", key=f"open_job_in_chrome_{job_id}", use_container_width=True):
            channel, profile_root = open_seek_url_in_browser(
                DB_PATH,
                target_url=job_url,
                use_bulk_profile=False,
            )
            st.success(f"Opened {details.get('title') or 'job'} in {channel}.")
            st.caption(f"Profile root: {profile_root or 'Default browser profile'}")
    st.write(_concise_rating_reason(details))
    st.caption(f"Available actions: {', '.join(visible_job_actions(details))}")
    action_cols = st.columns(4)
    if action_cols[0].button("Regenerate cover letter", key=f"detail_regenerate_{job_id}"):
        document_generator.generate_for_job(job_id)
        st.success("Application packet regenerated.")
        st.rerun()
    if action_cols[1].button("Regenerate interview prep", key=f"detail_regenerate_prep_{job_id}"):
        document_generator.generate_for_job(job_id)
        st.success("Interview prep regenerated.")
        st.rerun()
    if action_cols[2].button("Interview prep", key=f"detail_open_prep_{job_id}"):
        st.session_state[f"interview_prep_open_{job_id}"] = True
    if action_cols[3].button("Remove from queue", key=f"detail_remove_from_queue_{job_id}", use_container_width=True):
        tracker_ref = tracker or ApplicationTracker(Database(DB_PATH), APP_ROOT)
        _remove_job_from_active_queue(job_id, tracker_ref)

    st.markdown("**Clean job summary**")
    st.text_area(
        "Clean job summary",
        _best_description(details),
        height=220,
        disabled=True,
        key=f"detail_desc_{job_id}",
    )
    raw_text = ""
    metadata = details.get("metadata") or {}
    if isinstance(metadata, dict):
        raw_text = str(metadata.get("raw_imported_text") or "").strip()
    if raw_text and st.toggle("Show raw imported text", value=False, key=f"raw_toggle_{job_id}"):
        st.markdown("**Raw imported text**")
        st.text_area("Raw imported text", raw_text, height=180, disabled=True, key=f"raw_detail_desc_{job_id}")
    render_document_preview(document_map.get("application_rationale"), "Rationale", job_id)
    render_document_preview(document_map.get("tailored_cv_preview"), "CV preview", job_id)
    render_document_preview(document_map.get("cover_letter"), "Cover letter preview", job_id)
    render_document_preview(document_map.get("interview_prep"), "Interview prep", job_id)
    if st.session_state.get(f"interview_prep_open_{job_id}"):
        st.info("Interview prep preview is shown above.")
    render_document_preview(document_map.get("demo_recommendation"), "Demo recommendation", job_id)
    render_document_preview(document_map.get("checklist"), "Checklist", job_id)
    render_apply_assist(details, document_map, job_id, document_generator)

    st.markdown("#### Rating debug notes")
    render_fit_score_explanation(details)


def render_fit_score_explanation(details: dict) -> None:
    scored = explain_score_job(
        title=details["title"],
        location=details["location"] or "",
        salary=details["salary"] or "",
        description=_best_description(details),
    )
    for reason in scored.enrichment.get("reasons", []):
        st.write(f"- {reason}")
    debug_notes = scored.enrichment.get("debug_notes", {})
    if not isinstance(debug_notes, dict):
        return
    st.markdown("**Matched title signals**")
    st.write(", ".join(debug_notes.get("matched_title_signals", [])) or "None")
    st.markdown("**Matched technical skills**")
    st.write(", ".join(debug_notes.get("matched_technical_skills", [])) or "None")
    st.markdown("**Matched business skills**")
    st.write(", ".join(debug_notes.get("matched_business_skills", [])) or "None")
    st.markdown("**Pathway value**")
    pathway_value = debug_notes.get("pathway_value", {})
    if isinstance(pathway_value, dict):
        st.write(pathway_value.get("summary", "No pathway summary available."))
        if pathway_value.get("signals"):
            st.caption(f"Signals: {', '.join(pathway_value['signals'])}")
    st.markdown("**Penalties**")
    penalties = debug_notes.get("penalties", [])
    st.write(", ".join(penalties) or "None")


def render_document_download_or_regenerate(document: dict | None, label: str, job_id: int, document_generator: DocumentGenerator) -> None:
    if document and Path(document["file_path"]).exists():
        file_path = Path(document["file_path"])
        st.download_button(
            label,
            data=file_path.read_bytes(),
            file_name=file_path.name,
            key=f"download_{job_id}_{label}_{file_path.name}",
            use_container_width=True,
        )
    else:
        if st.button("Regenerate docs", key=f"regen_{job_id}_{label}", use_container_width=True):
            document_generator.generate_for_job(job_id)
            st.rerun()


def render_document_preview(document: dict | None, heading: str, job_id: int) -> None:
    st.markdown(f"**{heading}**")
    if not document:
        st.caption("Missing. Regenerate documents to recreate it.")
        return
    file_path = Path(document["file_path"])
    if not file_path.exists():
        st.caption("Missing on disk. Regenerate documents to recreate it.")
        return
    st.text_area(
        heading,
        file_path.read_text(encoding="utf-8", errors="ignore")[:6000],
        height=180,
        disabled=True,
        key=f"preview_{job_id}_{heading}",
    )


def build_dashboard_stats(jobs: pd.DataFrame) -> dict:
    fit_series = jobs.get("fit_score", pd.Series(dtype="float64")).fillna(0).astype(float)
    quick_apply = jobs.get("has_quick_apply_button", pd.Series([0] * len(jobs), index=jobs.index)).fillna(0).astype(int)
    apply_button = jobs.get("has_apply_button", pd.Series([0] * len(jobs), index=jobs.index)).fillna(0).astype(int)
    salary_text = jobs.get("apply_area_salary_text", pd.Series([""] * len(jobs), index=jobs.index)).fillna("").astype(str)
    calc_salary = jobs.get("salary_expectation_text", pd.Series([""] * len(jobs), index=jobs.index)).fillna("").astype(str)
    cv_profiles = jobs.get("selected_cv_profile", pd.Series(["Pending"] * len(jobs), index=jobs.index)).fillna("Pending").astype(str)

    fit_counts = {
        "High": int((fit_series >= 80).sum()),
        "Medium": int(((fit_series >= 60) & (fit_series < 80)).sum()),
        "Possible": int(((fit_series >= 40) & (fit_series < 60)).sum()),
        "Low": int((fit_series < 40).sum()),
    }
    chosen_cv_counts = (
        cv_profiles.replace("", "Pending").map(_display_cv_profile_name)
        .value_counts(dropna=False)
        .rename_axis("cv_profile")
        .reset_index(name="count")
        .to_dict("records")
    )

    numeric_salary_values = (
        calc_salary.str.extract(r"(\d+)", expand=False)
        .dropna()
        .astype(int)
    )
    avg_salary = f"{int(round(numeric_salary_values.mean() / 5.0) * 5)}k" if not numeric_salary_values.empty else "n/a"

    return {
        "total_jobs": int(len(jobs)),
        "jobs_with_quick_apply": int((quick_apply == 1).sum()),
        "jobs_with_normal_apply": int(((apply_button == 1) & (quick_apply != 1)).sum()),
        "jobs_with_salary_extracted": int((salary_text.str.strip() != "").sum()),
        "jobs_missing_salary": int((salary_text.str.strip() == "").sum()),
        "average_calculated_salary": avg_salary,
        "fit_level_counts": [{"fit_level": key, "count": value} for key, value in fit_counts.items()],
        "chosen_cv_counts": chosen_cv_counts or [{"cv_profile": "Pending", "count": 0}],
    }


def _closed_reason_label(value: str) -> str:
    reason = str(value or "").strip().lower()
    if not reason:
        return "Not set"
    labels = {
        "rejection_email": "Rejected by recruiter",
        "closed_rejected": "Rejected by recruiter",
        "feedback_received": "Closed with feedback",
        "closed_feedback_received": "Closed with feedback",
        "stale_no_response": "No response after 30 days",
        "manually_closed": "Manually closed",
    }
    return labels.get(reason, reason.replace("_", " ").title())


def _response_type_label(value: str) -> str:
    response_type = str(value or "").strip().lower()
    if not response_type:
        return "No response tracked"
    labels = {
        "confirmation": "Application confirmation",
        "interview": "Interview / next step",
        "feedback": "Feedback",
        "rejection": "Rejection",
        "other": "Other recruiter reply",
    }
    return labels.get(response_type, response_type.replace("_", " ").title())


def render_applied_stats(tracker: ApplicationTracker, gmail_followup: GmailFollowupManager) -> None:
    st.subheader("Completed")
    auto_closed_count = tracker.database.close_stale_applied_jobs(days=30)
    if auto_closed_count:
        st.caption(f"Auto-closed {auto_closed_count} submitted job(s) with no recruiter response after 30 days.")

    jobs = tracker.search_jobs(status="All", min_fit_score=0)
    if jobs.empty:
        st.info("No jobs in the database yet.")
        return
    effective_status = _effective_status_series(jobs).astype(str).str.strip().str.lower()
    removed_jobs = jobs[effective_status == "ignored"].copy()
    waiting_jobs = jobs[effective_status == "applied"].copy()
    closed_jobs = jobs[effective_status == "closed"].copy()
    submitted_jobs = jobs[effective_status.isin(["applied", "closed"])].copy()

    sync_cols = st.columns([1.2, 1.2, 2.0])
    if sync_cols[0].button("Sync recruiter replies", use_container_width=True, key="gmail_followup_sync_draft"):
        result = gmail_followup.sync_application_followups(auto_send_rejection_replies=False, close_stale_days=30)
        st.success(
            f"Processed {result['processed_messages']} email(s). "
            f"Matched {result['matched_jobs']} job(s), closed {result['closed_jobs']} job(s), "
            f"drafted {result['drafted_replies']} follow-up reply(s), sent {result['sent_replies']} reply(s), "
            f"and auto-closed {result['auto_closed_stale']} stale application(s)."
        )
        st.rerun()
    if sync_cols[1].button("Sync and auto-send safe replies", use_container_width=True, key="gmail_followup_sync_send"):
        result = gmail_followup.sync_application_followups(auto_send_rejection_replies=True, close_stale_days=30)
        st.success(
            f"Processed {result['processed_messages']} email(s). "
            f"Matched {result['matched_jobs']} job(s), closed {result['closed_jobs']} job(s), "
            f"drafted {result['drafted_replies']} follow-up reply(s), sent {result['sent_replies']} reply(s), "
            f"and auto-closed {result['auto_closed_stale']} stale application(s)."
        )
        st.rerun()
    sync_cols[2].caption(
        "Confirmations are tracked only. Rejection emails can create a draft reply or auto-send a safe feedback request."
    )

    top_cols = st.columns(4)
    top_cols[0].metric("Submitted", int(len(submitted_jobs)))
    top_cols[1].metric("Waiting response", int(len(waiting_jobs)))
    top_cols[2].metric("Closed", int(len(closed_jobs)))
    top_cols[3].metric("Removed", int(len(removed_jobs)))

    st.markdown("**Recruiter reply history**")
    followup_events = gmail_followup.list_followup_events(limit=200)
    if followup_events.empty:
        st.caption("No recruiter reply events have been tracked yet.")
    else:
        st.dataframe(followup_events, use_container_width=True, hide_index=True)

    if submitted_jobs.empty:
        st.info("No jobs have been marked as submitted yet.")
        st.markdown("---")
        render_question_analysis(tracker, show_heading=False)
        return

    submitted_jobs["applied_at"] = _applied_timestamp_series(submitted_jobs)
    submitted_jobs["fit_score"] = submitted_jobs.get("fit_score", pd.Series([0] * len(submitted_jobs), index=submitted_jobs.index)).fillna(0).astype(int)
    col1, col2, col3, col4 = st.columns(4)
    avg_fit = int(round(submitted_jobs["fit_score"].mean())) if not submitted_jobs.empty else 0
    col1.metric("Applied jobs", int(len(submitted_jobs)))
    col2.metric("Avg fit", avg_fit)
    col3.metric("Companies", int(submitted_jobs.get("company", pd.Series(dtype='object')).astype(str).nunique()))
    recent_mask = submitted_jobs["applied_at"].astype(str).str[:10].eq(date.today().isoformat())
    col4.metric("Applied today", int(recent_mask.sum()))

    submitted_jobs["applied_at_dt"] = pd.to_datetime(submitted_jobs["applied_at"], errors="coerce")
    chart_cols = st.columns(2)

    weekday_order = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    week_start = pd.Timestamp(date.today()) - pd.Timedelta(days=date.today().weekday())
    week_end = week_start + pd.Timedelta(days=7)
    this_week = submitted_jobs[
        submitted_jobs["applied_at_dt"].notna()
        & (submitted_jobs["applied_at_dt"] >= week_start)
        & (submitted_jobs["applied_at_dt"] < week_end)
    ].copy()
    weekly_counts = pd.Series(0, index=weekday_order, dtype="int64")
    if not this_week.empty:
        weekday_names = this_week["applied_at_dt"].dt.day_name().str[:3]
        weekly_actual = weekday_names.value_counts()
        for label in weekday_order:
            weekly_counts.loc[label] = int(weekly_actual.get(label, 0))
    weekly_chart = pd.DataFrame({"Applications": weekly_counts}, index=weekday_order)

    monthly_chart = pd.DataFrame(columns=["Applications"])
    monthly_source = submitted_jobs[submitted_jobs["applied_at_dt"].notna()].copy()
    if not monthly_source.empty:
        monthly_counts = (
            monthly_source.assign(month_label=monthly_source["applied_at_dt"].dt.strftime("%Y-%m"))
            .groupby("month_label")
            .size()
            .sort_index()
        )
        monthly_chart = pd.DataFrame({"Applications": monthly_counts})

    with chart_cols[0]:
        st.markdown("**Applied This Week**")
        st.caption("Day of week on the x-axis, applied count on the y-axis.")
        st.bar_chart(weekly_chart, use_container_width=True)

    with chart_cols[1]:
        st.markdown("**Applied By Month**")
        st.caption("Month on the x-axis, total applied count on the y-axis.")
        if monthly_chart.empty:
            st.info("No monthly applied history is available yet.")
        else:
            st.bar_chart(monthly_chart, use_container_width=True)

    st.markdown("**Automation Timing**")
    timing_cols = st.columns(2)
    prepare_timing = _load_timing_history(tracker.database, "seek_prepare_timing")
    submit_timing = _load_timing_history(tracker.database, "seek_submit_timing")
    with timing_cols[0]:
        _render_timing_summary("Prepare Timing", prepare_timing)
    with timing_cols[1]:
        _render_timing_summary("Submit Timing", submit_timing)

    st.markdown("---")
    render_role_market_analysis(jobs, heading="Tracked Market Insights")
    if not submitted_jobs.empty:
        st.markdown("---")
        render_role_market_analysis(submitted_jobs, heading="Submitted Role Insights")

    latest_runs = _latest_apply_runs_by_job()
    display = submitted_jobs[[
        "job_id",
        "title",
        "company",
        "location",
        "salary_expectation_text",
        "fit_score",
        "selected_cv_profile",
        "applied_at",
        "application_status",
        "last_response_type",
        "closed_reason",
    ]].copy()
    display = display.rename(
        columns={
            "salary_expectation_text": "Calc Salary",
            "fit_score": "Fit",
            "selected_cv_profile": "CV",
            "applied_at": "Applied At",
            "application_status": "State",
            "last_response_type": "Last Response",
            "closed_reason": "Closed Reason",
        }
    )
    display["CV"] = display["CV"].fillna("Pending").astype(str).map(_display_cv_profile_name)
    display["State"] = display["State"].fillna("").astype(str).str.replace("_", " ").str.title()
    display["Last Response"] = display["Last Response"].map(_response_type_label)
    display["Closed Reason"] = display["Closed Reason"].map(_closed_reason_label)
    st.dataframe(display.sort_values(by=["Applied At", "Fit"], ascending=[False, False], kind="stable"), use_container_width=True, hide_index=True)

    st.markdown("**Submitted job details**")
    detailed_jobs = submitted_jobs.sort_values(by=["applied_at", "fit_score"], ascending=[False, False], kind="stable")
    for _, row in detailed_jobs.iterrows():
        job_id = int(row["job_id"])
        details = tracker.get_job_details(job_id) or {}
        latest_run = latest_runs.get(job_id)
        run_dir = Path(latest_run["run_dir"]) if latest_run else None
        question_answers = _load_dynamic_question_answers(run_dir) if run_dir else []
        summary = build_job_display_summary(details, question_answers)
        with st.expander(f"{summary['title']} — applied", expanded=False):
            st.caption(f"Applied at: {details.get('applied_at') or row.get('applied_at') or 'Unknown'}")
            st.caption(f"Company: {details.get('company') or row.get('company') or 'Unknown'}")
            st.caption(f"Location: {details.get('location') or row.get('location') or 'Unknown'}")
            st.caption(f"Calc salary: {summary['calculated_salary']}")
            st.caption(f"Fit / CV: {summary['fit_and_cv']}")
            st.caption(f"Latest recruiter response: {_response_type_label(details.get('last_response_type') or row.get('last_response_type') or '')}")
            if str(details.get("last_response_at") or row.get("last_response_at") or "").strip():
                st.caption(f"Latest recruiter response at: {details.get('last_response_at') or row.get('last_response_at')}")
            if str(details.get("closed_reason") or row.get("closed_reason") or "").strip():
                st.caption(f"Closed reason: {_closed_reason_label(details.get('closed_reason') or row.get('closed_reason') or '')}")
            if str(details.get("closed_at") or row.get("closed_at") or "").strip():
                st.caption(f"Closed at: {details.get('closed_at') or row.get('closed_at')}")
            feedback_summary = str(details.get("follow_up_feedback_summary") or row.get("follow_up_feedback_summary") or "").strip()
            if feedback_summary:
                st.caption(f"Feedback summary: {feedback_summary}")
            if run_dir:
                st.caption(f"Latest run folder: {run_dir}")
                review_png = run_dir / "final_review.png"
                submit_png = run_dir / "submit_unconfirmed.png"
                if review_png.exists():
                    st.caption(f"Final review screenshot: {review_png}")
                elif submit_png.exists():
                    st.caption(f"Latest submit screenshot: {submit_png}")
            if question_answers:
                st.markdown("**Questions and answers**")
                st.dataframe(pd.DataFrame(question_answers), use_container_width=True, hide_index=True)
            else:
                st.caption("No question/answer capture was saved for this submitted job.")

    st.markdown("---")
    render_question_analysis(tracker, show_heading=False)


def build_job_display_summary(details: dict, question_answers: list[dict[str, str]] | None = None) -> dict[str, object]:
    fit_score = int(details.get("fit_score") or 0)
    calculated_salary, calculated_salary_reason = _resolved_salary_display(details, question_answers)
    return {
        "title": details.get("enriched_title") or details.get("title") or "Untitled role",
        "date_posted": details.get("date_posted") or details.get("date_posted_text") or "Unknown",
        "apply_type": details.get("apply_type") or "unknown",
        "has_apply_button": bool(details.get("has_apply_button")),
        "has_quick_apply_button": bool(details.get("has_quick_apply_button")),
        "page_salary": details.get("apply_area_salary_text") or "Not extracted",
        "calculated_salary": calculated_salary,
        "calculated_salary_reason": calculated_salary_reason,
        "fit_reason": _concise_rating_reason(details),
        "fit_and_cv": (
            f"{fit_score} {_relevance_label(fit_score)}"
            f" | {_display_cv_profile_name(str(details.get('selected_cv_profile') or 'Pending'))}"
        ),
    }


def visible_job_actions(details: dict) -> list[str]:
    actions = ["Regenerate cover letter", "Interview prep"]
    if str(details.get("source") or "").strip().lower() == "seek":
        actions.append("Apply Assist - SEEK")
    return actions


def render_apply_assist(details: dict, document_map: dict[str, dict], job_id: int, document_generator: DocumentGenerator) -> None:
    st.markdown("**Apply Assist**")
    current_details = dict(details)
    generation_mode = str(current_details.get("cover_letter_basis") or "unknown")
    preview_text = str(current_details.get("cover_letter_preview") or "")
    banned_phrase_detected = document_generator.cover_letter_contains_banned_phrases(preview_text)
    sanitized = generation_mode == "template_fallback_sanitized"
    st.caption(f"Cover letter generation mode: {generation_mode}")
    st.caption(f"Template used: {document_generator.cover_letter_template_path.name}")
    st.caption(f"Sanitized fallback used: {'Yes' if sanitized else 'No'}")
    st.caption(f"Banned phrase detected: {banned_phrase_detected or 'No'}")
    st.text_area(
        "Cover letter debug preview",
        preview_text[:200] if preview_text else "No cover letter preview stored yet.",
        height=120,
        disabled=True,
        key=f"apply_cl_debug_{job_id}",
    )
    payload = build_apply_assist_payload(current_details)
    st.warning(payload["warning"])
    if is_valid_job_url(payload["apply_url"]):
        st.link_button("Open application page", payload["apply_url"], use_container_width=True)
    if str(details.get("source") or "").strip().lower() == "seek":
        st.caption("SEEK background prepare uses the same hidden automation path as bulk. If the session is valid, it prepares the application without opening a browser window.")
        st.caption("If SEEK challenges the session again, refresh it from the sidebar with `Open SEEK Login In Chrome` and `Validate SEEK Session`.")
        if st.button("Prepare In Background - SEEK", key=f"seek_apply_assist_{job_id}", use_container_width=True):
            st.warning("Preparing this SEEK application in the background. It will stop before final submit.")
            current_details = document_generator.ensure_current_cover_letter(job_id, force_regenerate=True)
            build_apply_assist_payload(current_details)
            result = assist_seek_apply(
                job_id,
                DB_PATH,
                visible=False,
                keep_browser_open_override=False,
                resume_upload_only=False,
                input_func=lambda: "",
                allow_manual_login_prompt=False,
            )
            debug_path = Path(result.debug_dir) if result.debug_dir else DB_PATH.parent / "debug_apply_assist"
            if result.status == "prepared_for_manual_review":
                tracker_db = Database(DB_PATH)
                tracker_db.update_application_state(job_id, "ready_to_apply", note="Prepared in background and awaiting final submit.")
                st.success("Application prepared successfully.")
            elif result.status == "browser_profile_locked":
                st.warning(
                    "Close all SEEK/Chromium windows and retry. If it still fails, delete "
                    "`data/browser_profiles/seek_seeded` and validate the session again."
                )
                if result.error:
                    st.caption(result.error)
            elif result.status in {"paused_for_login", "paused_for_captcha", "paused_for_seek_verification", "paused_for_manual_resume_upload", "paused_for_seek_login", "paused_for_manual_questionnaire"}:
                st.warning(result.error or "SEEK background prepare paused before completion.")
                st.caption(f"Debug artifacts: {debug_path}")
            else:
                st.error(result.error or "SEEK background prepare failed.")
                st.caption(f"Debug artifacts: {debug_path}")
            if result.latest_checkpoint:
                st.caption(f"Latest checkpoint: {result.latest_checkpoint}")
            st.caption(f"Final URL: {result.final_url or 'Unknown'}")
            if result.steps_completed:
                st.caption(f"Steps completed: {', '.join(result.steps_completed)}")
    st.text_area("Cover letter for copy/paste", payload["cover_letter"], height=180, disabled=True, key=f"apply_cl_{job_id}")
    st.text_area("Profile summary for copy/paste", payload["profile_summary"], height=100, disabled=True, key=f"apply_profile_{job_id}")
    st.text_input("Salary expectation for copy/paste", payload["salary_expectation"], disabled=True, key=f"apply_salary_{job_id}")


def _relevance_label(score: int) -> str:
    if score >= 80:
        return "High"
    if score >= 60:
        return "Medium"
    if score >= 40:
        return "Possible"
    return "Low"


def _concise_rating_reason(details: dict) -> str:
    scored = explain_score_job(
        title=str(details.get("title") or ""),
        location=str(details.get("location") or ""),
        salary=str(details.get("salary") or ""),
        description=_best_description(details),
    )
    return str(scored.enrichment.get("concise_reason") or "General analytics alignment.")


def _age_label(details: dict) -> str:
    try:
        age = int(details.get("age_in_days", 0) or 0)
    except (TypeError, ValueError):
        age = 0
    if age <= 0:
        return "0 days ago"
    if age == 1:
        return "1 day ago"
    return f"{age} days ago"


def _document_basis_label(details: dict) -> str:
    basis = str(details.get("document_basis") or "").strip()
    if basis:
        return basis
    if str(details.get("about_the_job") or "").strip():
        return "about_the_job"
    if str(details.get("full_description") or "").strip():
        return "full_description"
    if str(details.get("source_page_text") or "").strip():
        return "source_page_text"
    return "email_snippet_fallback"


def _salary_reasoning(details: dict) -> list[str]:
    raw = str(details.get("salary_estimate_reasoning_json") or "").strip()
    if not raw:
        return []
    try:
        payload = json.loads(raw)
    except Exception:  # noqa: BLE001
        return []
    if isinstance(payload, list):
        return [str(item) for item in payload if str(item).strip()]
    return []


def _best_description(details: dict) -> str:
    return (
        str(details.get("about_the_job") or "").strip()
        or str(details.get("full_description") or "").strip()
        or str(details.get("source_page_text") or "").strip()
        or str(details.get("description") or "").strip()
    )


def open_application_folder(application_dir: Path) -> None:
    application_dir.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.Popen(["explorer", str(application_dir)])  # noqa: S603
    except Exception as exc:  # noqa: BLE001
        st.warning(f"Could not open folder automatically: {exc}")


def render_run_logs(database: Database) -> None:
    logs = database.fetch_run_logs(limit=25)
    if logs.empty:
        st.caption("No logs yet.")
        return
    st.dataframe(logs, use_container_width=True, hide_index=True)


def render_dashboard_debug(database: Database) -> None:
    snapshot = database.get_jobs_debug_snapshot()
    latest_job = snapshot.get("latest_job") or {}
    st.write(
        {
            "active_database_path": snapshot["db_path"],
            "database_exists": snapshot["db_exists"],
            "file_size_bytes": snapshot["file_size"],
            "total_jobs_count": snapshot["total_jobs"],
            "total_applications_count": snapshot["total_applications"],
            "latest_job_id": latest_job.get("id"),
            "latest_job_title": latest_job.get("title"),
            "latest_imported_timestamp": latest_job.get("imported_at") or latest_job.get("created_at"),
        }
    )
    st.markdown("**Jobs by application_status**")
    status_counts = pd.DataFrame(snapshot["application_status_counts"])
    if status_counts.empty:
        st.caption("No application status rows yet.")
    else:
        st.dataframe(status_counts, use_container_width=True, hide_index=True)
    st.markdown("**Jobs by enrichment_status**")
    enrichment_counts = pd.DataFrame(snapshot["enrichment_status_counts"])
    if enrichment_counts.empty:
        st.caption("No enrichment status rows yet.")
    else:
        st.dataframe(enrichment_counts, use_container_width=True, hide_index=True)
    st.markdown("**Newest 10 jobs**")
    latest_jobs = pd.DataFrame(snapshot["latest_jobs"])
    if latest_jobs.empty:
        st.caption("No jobs in the database.")
    else:
        st.dataframe(latest_jobs, use_container_width=True, hide_index=True)


def render_admin_tools(database: Database) -> None:
    st.caption("Debugging and maintenance tools.")
    if st.button("Clear processed email tracking", use_container_width=True):
        database.clear_processed_email_messages()
        database.log_run("manual_update", "clear_processed_email_tracking", "success", "Cleared processed email tracking from the dashboard.")
        st.success("Processed email tracking cleared.")
        st.rerun()


def main() -> None:
    st.set_page_config(page_title="AI Job Apply Assistant", layout="wide")
    database, tracker, importer, document_generator, email_ingestor, gmail_ingestor, gmail_followup, job_enricher = initialise_app()
    if not st.session_state.get("_stale_cover_letters_invalidated"):
        invalidation_stats = document_generator.invalidate_stale_cover_letters()
        st.session_state["_stale_cover_letters_invalidated"] = True
        print(
            "[cover_letter] startup_invalidation "
            f"scanned={invalidation_stats['scanned']} regenerated={invalidation_stats['regenerated']}",
            flush=True,
        )
    snapshot = database.get_jobs_debug_snapshot()
    latest_job = snapshot.get("latest_job") or {}
    print(
        "[app] startup diagnostics "
        f"db_path={snapshot['db_path']} "
        f"jobs_count={snapshot['total_jobs']} "
        f"latest_job_id={latest_job.get('id')} "
        f"latest_imported_at={latest_job.get('imported_at') or latest_job.get('created_at')}",
        flush=True,
    )
    render_sidebar(gmail_ingestor)
    render_auto_refresh()

    nav_labels = _build_navigation_labels(tracker)
    page_options = [
        nav_labels["unprocessed"],
        nav_labels["problems"],
        nav_labels["completed"],
        nav_labels["ready"],
        nav_labels["cv"],
    ]
    selected_page = st.selectbox(
        "Page",
        options=page_options,
        index=0,
        key="top_level_page_selector",
    )
    render_shared_queue_summary(tracker)
    if selected_page == nav_labels["unprocessed"]:
        render_dashboard(
            tracker,
            document_generator,
            job_enricher,
            heading="Unprocessed Jobs",
            focus_bucket="unprocessed",
            show_bulk_panel=True,
        )
    elif selected_page == nav_labels["problems"]:
        render_dashboard(
            tracker,
            document_generator,
            job_enricher,
            heading="Processed Jobs With Problems",
            focus_bucket="problems",
            show_bulk_panel=False,
        )
    elif selected_page == nav_labels["completed"]:
        render_applied_stats(tracker, gmail_followup)
    elif selected_page == nav_labels["ready"]:
        render_dashboard(
            tracker,
            document_generator,
            job_enricher,
            heading="Ready To Submit",
            focus_bucket="ready",
            show_bulk_panel=False,
        )
    else:
        render_cv_library(tracker)


def _effective_status_series(jobs: pd.DataFrame) -> pd.Series:
    application_status = jobs.get("application_status")
    tracker_status = jobs.get("status")
    if application_status is None and tracker_status is None:
        return pd.Series([""] * len(jobs), index=jobs.index, dtype="object")
    if application_status is None:
        return tracker_status.fillna("").astype(str)
    application_status = application_status.fillna("").astype(str)
    if tracker_status is None:
        return application_status
    tracker_status = tracker_status.fillna("").astype(str)
    return application_status.where(application_status.str.strip() != "", tracker_status)


def _load_timing_history(database: Database, run_type: str, limit: int = 5000) -> pd.DataFrame:
    logs = database.fetch_run_logs_by_type(run_type, limit=limit)
    if logs.empty:
        return pd.DataFrame(columns=["job_id", "status", "duration_seconds", "created_at"])

    pattern_duration = re.compile(r"duration_seconds=([0-9]+(?:\.[0-9]+)?)")
    pattern_job_id = re.compile(r"job_id=(\d+)")
    pattern_status = re.compile(r"status=([A-Za-z0-9_]+)")

    rows: list[dict[str, object]] = []
    for _, row in logs.iterrows():
        details = str(row.get("details") or "")
        duration_match = pattern_duration.search(details)
        if not duration_match:
            continue
        job_match = pattern_job_id.search(details)
        status_match = pattern_status.search(details)
        rows.append(
            {
                "job_id": int(job_match.group(1)) if job_match else 0,
                "status": str(status_match.group(1) if status_match else ""),
                "duration_seconds": float(duration_match.group(1)),
                "created_at": str(row.get("created_at") or ""),
            }
        )
    return pd.DataFrame(rows)


def _render_timing_summary(title: str, frame: pd.DataFrame) -> None:
    st.markdown(f"**{title}**")
    if frame.empty:
        st.info("No timing data recorded yet.")
        return
    durations = frame["duration_seconds"].astype(float)
    metric_cols = st.columns(4)
    metric_cols[0].metric("Runs", int(len(durations)))
    metric_cols[1].metric("Average", f"{durations.mean():.1f}s")
    metric_cols[2].metric("Quickest", f"{durations.min():.1f}s")
    metric_cols[3].metric("Longest", f"{durations.max():.1f}s")


def _average_duration_seconds(frame: pd.DataFrame) -> float:
    if frame.empty or "duration_seconds" not in frame.columns:
        return 0.0
    durations = frame["duration_seconds"].astype(float)
    if durations.empty:
        return 0.0
    return float(durations.mean())


def _max_duration_seconds(frame: pd.DataFrame) -> float:
    if frame.empty or "duration_seconds" not in frame.columns:
        return 0.0
    durations = frame["duration_seconds"].astype(float)
    if durations.empty:
        return 0.0
    return float(durations.max())


def _format_duration_seconds(value: float) -> str:
    seconds = max(0, int(round(float(value or 0.0))))
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


def _build_queue_summary(tracker: ApplicationTracker) -> dict[str, object]:
    jobs = tracker.search_jobs(
        query="",
        source="All",
        status="All",
        min_fit_score=0,
        company="",
        date_from=None,
        date_to=None,
    )
    if not jobs.empty:
        jobs = jobs[
            jobs.apply(
                lambda row: str(row.get("apply_type") or "").strip().lower() == "quick apply"
                or bool(row.get("has_quick_apply_button")),
                axis=1,
            )
        ].copy()
        jobs = jobs[jobs.apply(_is_auckland_job, axis=1)].copy()
        effective_status_all = _effective_status_series(jobs).astype(str).str.strip().str.lower()
        jobs = jobs[~effective_status_all.isin(["applied", "closed", "ignored", "archived", "rejected"])].copy()

    latest_runs = _latest_apply_runs_by_job()
    unprocessed_count = 0
    ready_count = 0
    problem_count = 0
    if not jobs.empty:
        for _, row in jobs.iterrows():
            job_id = int(row["job_id"])
            latest_run = latest_runs.get(job_id)
            latest_payload = dict(latest_run["payload"]) if latest_run else {}
            latest_run_status = str(latest_payload.get("status") or "").strip()
            bucket = _bucket_quick_apply_queue_item(
                latest_run=latest_run,
                latest_run_status=latest_run_status,
                latest_submit_status="",
            )
            if bucket == "unprocessed":
                unprocessed_count += 1
            elif bucket == "ready":
                ready_count += 1
            elif bucket == "problems":
                problem_count += 1

    all_jobs = tracker.search_jobs(status="All", min_fit_score=0)
    applied_today_count = 0
    applied_all_time_count = 0
    removed_count = 0
    if not all_jobs.empty:
        quick_apply_jobs = all_jobs[
            all_jobs.apply(
                lambda row: _is_quick_apply_job(row) and _is_auckland_job(row),
                axis=1,
            )
        ].copy()
        if not quick_apply_jobs.empty:
            effective_status = _effective_status_series(quick_apply_jobs).astype(str).str.strip().str.lower()
            applied_jobs = quick_apply_jobs[effective_status.isin(["applied", "closed"])].copy()
            removed_jobs = quick_apply_jobs[effective_status == "ignored"].copy()
            applied_all_time_count = int(len(applied_jobs))
            removed_count = int(len(removed_jobs))
            if not applied_jobs.empty:
                applied_at_series = _applied_timestamp_series(applied_jobs)
                applied_today_count = int(applied_at_series.str[:10].eq(date.today().isoformat()).sum())

    prepare_timing = _load_timing_history(tracker.database, "seek_prepare_timing")
    submit_timing = _load_timing_history(tracker.database, "seek_submit_timing")
    avg_prepare_seconds = _average_duration_seconds(prepare_timing)
    avg_submit_seconds = _average_duration_seconds(submit_timing)
    return {
        "unprocessed_count": unprocessed_count,
        "ready_count": ready_count,
        "problem_count": problem_count,
        "applied_today_count": applied_today_count,
        "applied_all_time_count": applied_all_time_count,
        "removed_count": removed_count,
        "estimated_prepare_total": _format_duration_seconds(avg_prepare_seconds * unprocessed_count),
        "estimated_submit_total": _format_duration_seconds(avg_submit_seconds * ready_count),
    }


def render_shared_queue_summary(tracker: ApplicationTracker) -> None:
    summary = _build_queue_summary(tracker)
    applied_today = int(summary["applied_today_count"])
    ready_count = int(summary["ready_count"])
    problem_count = int(summary["problem_count"])
    unprocessed_count = int(summary["unprocessed_count"])

    first_row = st.columns(4)
    first_row[0].metric("Actually submitted today", applied_today)
    first_row[1].metric("Ready but NOT submitted", ready_count, delta=f"Est. {summary['estimated_submit_total']}")
    first_row[2].metric("Blocked — needs you", problem_count)
    first_row[3].metric("Not prepared in SEEK", unprocessed_count, delta=f"Est. {summary['estimated_prepare_total']}")
    st.caption(f"Actually submitted all time: {int(summary['applied_all_time_count'])}")

    if problem_count:
        st.error(
            f"ACTION REQUIRED: {problem_count} application(s) are blocked. "
            "Open Problems to resolve CAPTCHA/login verification, questionnaires, or document uploads."
        )
    if ready_count:
        st.warning(
            f"ACTION REQUIRED: {ready_count} application(s) are prepared but NOT submitted. "
            "Open Ready to submit and complete the final submission."
        )
    if unprocessed_count:
        st.warning(
            f"ACTION REQUIRED: {unprocessed_count} Quick Apply job(s) have not been prepared in SEEK. "
            "Open Unprocessed and run Prepare all Quick Apply jobs."
        )
    if not any([problem_count, ready_count, unprocessed_count]):
        st.success("No application intervention is currently waiting.")


def _build_navigation_labels(tracker: ApplicationTracker) -> dict[str, str]:
    return {
        "unprocessed": "Unprocessed",
        "problems": "Problems",
        "completed": "Completed",
        "ready": "Ready to submit",
        "cv": "CV Library",
    }


def _normalise_question_text_for_analysis(value: str) -> str:
    text = " ".join(str(value or "").strip().lower().split())
    if not text:
        return ""
    text = re.sub(r"\b\d+\+?\s*(years?|yrs?)\b", "<years>", text)
    text = re.sub(r"\$\s?\d[\d,]*(?:\.\d+)?", "<money>", text)
    text = re.sub(r"\b\d+\b", "<num>", text)
    return text


def _classify_question_theme(question_text: str) -> str:
    text = _normalise_question_text_for_analysis(question_text)
    theme_rules = [
        ("Salary", ["salary", "remuneration", "pay expectation", "expected annual base"]),
        ("Work Rights", ["right to work", "work in new zealand", "visa", "citizen", "residency"]),
        ("Experience Years", ["how many years", "years of experience", "years experience", "<years>"]),
        ("Tools / Skills", ["sql", "python", "power bi", "excel", "salesforce", "fabric", "etl", "dashboard", "lead generation"]),
        ("Availability / Notice", ["notice period", "when can you start", "available to start", "availability"]),
        ("Location / Travel", ["relocate", "travel", "auckland", "hybrid", "onsite", "on-site", "remote"]),
        ("Education / Qualifications", ["degree", "qualification", "education", "bachelor", "mathematics"]),
        ("Leadership / Management", ["manage", "lead", "leadership", "people manager", "team lead"]),
        ("Interest / Motivation", ["why are you interested", "what interests you", "why do you want", "motivation"]),
        ("Assessment / Compliance", ["psychometric", "background check", "police check", "consent", "assessment"]),
    ]
    for label, needles in theme_rules:
        if any(needle in text for needle in needles):
            return label
    return "Other"


def _role_family_label(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return "Unknown"
    return text.replace("_", " ").title()


def _load_json_string_list(value: object) -> list[str]:
    raw = str(value or "").strip()
    if not raw:
        return []
    try:
        payload = json.loads(raw)
    except Exception:  # noqa: BLE001
        return []
    if not isinstance(payload, list):
        return []
    cleaned: list[str] = []
    for item in payload:
        text = str(item or "").strip()
        if text:
            cleaned.append(text)
    return cleaned


def _salary_midpoint_k(details: dict[str, object]) -> float | None:
    low = details.get("salary_estimate_low")
    high = details.get("salary_estimate_high")
    try:
        low_value = int(low) if low not in (None, "") else 0
        high_value = int(high) if high not in (None, "") else 0
    except (TypeError, ValueError):
        low_value = 0
        high_value = 0
    if low_value > 0 and high_value > 0:
        return float((low_value + high_value) / 2.0)

    salary_text = str(details.get("salary_expectation_text") or "").strip().lower()
    if not salary_text:
        return None
    match = re.search(r"(\d+(?:\.\d+)?)\s*k", salary_text)
    if match:
        try:
            return float(match.group(1))
        except ValueError:
            return None
    digits = re.search(r"(\d[\d,]{3,})", salary_text)
    if digits:
        try:
            return float(int(digits.group(1).replace(",", "")) / 1000.0)
        except ValueError:
            return None
    return None


def _build_role_market_frame(jobs: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for _, row in jobs.iterrows():
        details = row.to_dict()
        matched_skills = _load_json_string_list(details.get("matched_skills_json"))
        missing_skills = _load_json_string_list(details.get("missing_skills_json"))
        skill_signals = sorted({skill.strip() for skill in matched_skills + missing_skills if skill.strip()})
        rows.append(
            {
                "job_id": int(details.get("job_id") or 0),
                "role_family": _role_family_label(str(details.get("role_family") or "")),
                "company": str(details.get("company") or "").strip(),
                "fit_score": int(details.get("fit_score") or 0),
                "salary_midpoint_k": _salary_midpoint_k(details),
                "matched_skills": matched_skills,
                "missing_skills": missing_skills,
                "skill_signals": skill_signals,
                "skill_signal_count": len(skill_signals),
                "application_status": str(details.get("application_status") or details.get("status") or "").strip().lower(),
            }
        )
    return pd.DataFrame(rows)


def render_role_market_analysis(jobs: pd.DataFrame, *, heading: str = "Role Market Insights") -> None:
    st.markdown(f"**{heading}**")
    market_frame = _build_role_market_frame(jobs)
    if market_frame.empty:
        st.info("No role classification data is available yet.")
        return

    known_roles = market_frame[market_frame["role_family"] != "Unknown"].copy()
    analysis_frame = known_roles if not known_roles.empty else market_frame
    with_salary = analysis_frame[analysis_frame["salary_midpoint_k"].notna()].copy()
    role_counts = analysis_frame["role_family"].value_counts()

    metric_cols = st.columns(4)
    metric_cols[0].metric("Tracked roles", int(len(analysis_frame)))
    metric_cols[1].metric("Role families", int(analysis_frame["role_family"].nunique()))
    metric_cols[2].metric("Avg calc salary", f"{analysis_frame['salary_midpoint_k'].dropna().mean():.0f}k" if not with_salary.empty else "n/a")
    metric_cols[3].metric("Avg skill signals", f"{analysis_frame['skill_signal_count'].mean():.1f}")

    chart_cols = st.columns(2)
    with chart_cols[0]:
        st.markdown("**Roles By Type**")
        st.caption("Which role families show up most often in your tracked jobs.")
        st.bar_chart(pd.DataFrame({"Jobs": role_counts}))

    role_salary_chart = pd.DataFrame(columns=["Average Salary (k)"])
    if not with_salary.empty:
        grouped_salary = with_salary.groupby("role_family")["salary_midpoint_k"].mean().sort_values(ascending=False)
        role_salary_chart = pd.DataFrame({"Average Salary (k)": grouped_salary})
    with chart_cols[1]:
        st.markdown("**Average Pay By Role Type**")
        st.caption("Average calculated salary midpoint across jobs in each role family.")
        if role_salary_chart.empty:
            st.info("Not enough salary data is available yet.")
        else:
            st.bar_chart(role_salary_chart, use_container_width=True)

    skill_counter: Counter[str] = Counter()
    role_skill_counter: dict[str, Counter[str]] = {}
    for _, row in analysis_frame.iterrows():
        role_label = str(row.get("role_family") or "Unknown")
        skill_counter.update(row.get("skill_signals") or [])
        role_skill_counter.setdefault(role_label, Counter()).update(row.get("skill_signals") or [])

    top_skills = pd.DataFrame(
        [{"Skill": skill, "Jobs": count} for skill, count in skill_counter.most_common(15)]
    )
    if not top_skills.empty:
        st.markdown("**Most Common Skill Signals**")
        st.caption("Skills most often detected as relevant or required across tracked jobs.")
        st.dataframe(top_skills, use_container_width=True, hide_index=True)

    role_breakdown_rows: list[dict[str, object]] = []
    for role_label, role_rows in analysis_frame.groupby("role_family", dropna=False):
        role_rows = role_rows.copy()
        salary_values = role_rows["salary_midpoint_k"].dropna().astype(float)
        top_role_skills = ", ".join(skill for skill, _ in role_skill_counter.get(str(role_label), Counter()).most_common(5)) or "None"
        role_breakdown_rows.append(
            {
                "Role Type": role_label,
                "Jobs": int(len(role_rows)),
                "Avg Calc Salary": f"{salary_values.mean():.0f}k" if not salary_values.empty else "n/a",
                "Avg Fit": int(round(role_rows["fit_score"].astype(float).mean())) if not role_rows.empty else 0,
                "Avg Skill Signals": round(float(role_rows["skill_signal_count"].astype(float).mean()), 1) if not role_rows.empty else 0.0,
                "Top Skill Signals": top_role_skills,
            }
        )
    if role_breakdown_rows:
        role_breakdown = pd.DataFrame(role_breakdown_rows).sort_values(by=["Jobs", "Avg Fit"], ascending=[False, False], kind="stable")
        st.markdown("**Role Type Breakdown**")
        st.dataframe(role_breakdown, use_container_width=True, hide_index=True)


def _load_question_analysis_frame(tracker: ApplicationTracker) -> pd.DataFrame:
    latest_runs = _latest_apply_runs_by_job()
    rows: list[dict[str, object]] = []
    for job_id, run in latest_runs.items():
        run_dir = Path(run["run_dir"])
        answers = _load_dynamic_question_answers(run_dir)
        if not answers:
            continue
        details = tracker.get_job_details(int(job_id)) or {}
        latest_payload = dict(run.get("payload") or {})
        for answer in answers:
            question_text = str(answer.get("question") or "").strip()
            normalized_text = _normalise_question_text_for_analysis(question_text)
            if not question_text or not normalized_text:
                continue
            rows.append(
                {
                    "job_id": int(job_id),
                    "title": str(details.get("title") or ""),
                    "company": str(details.get("company") or ""),
                    "selected_cv_profile": _display_cv_profile_name(str(details.get("selected_cv_profile") or "Pending")),
                    "run_status": str(latest_payload.get("status") or ""),
                    "question": question_text,
                    "question_normalized": normalized_text,
                    "answer": str(answer.get("answer") or "").strip(),
                    "source": str(answer.get("source") or "").strip(),
                    "success": str(answer.get("success") or "").strip(),
                    "theme": _classify_question_theme(question_text),
                }
            )
    return pd.DataFrame(rows)


def render_question_analysis(tracker: ApplicationTracker, *, show_heading: bool = True) -> None:
    if show_heading:
        st.subheader("Question Analysis")
    question_frame = _load_question_analysis_frame(tracker)
    if question_frame.empty:
        st.info("No captured application questions are available yet.")
        return

    total_questions = int(len(question_frame))
    unique_questions = int(question_frame["question_normalized"].nunique())
    jobs_with_questions = int(question_frame["job_id"].nunique())
    theme_count = int(question_frame["theme"].nunique())
    success_rate = float((question_frame["success"].astype(str).str.lower() == "yes").mean() * 100.0)

    metric_cols = st.columns(5)
    metric_cols[0].metric("Question instances", total_questions)
    metric_cols[1].metric("Unique question patterns", unique_questions)
    metric_cols[2].metric("Jobs with questions", jobs_with_questions)
    metric_cols[3].metric("Themes", theme_count)
    metric_cols[4].metric("Answer success rate", f"{success_rate:.0f}%")

    chart_cols = st.columns(2)
    theme_counts = question_frame.groupby("theme").size().sort_values(ascending=False)
    with chart_cols[0]:
        st.markdown("**Question Themes**")
        st.caption("High-level semantic grouping of the questions captured across applications.")
        st.bar_chart(pd.DataFrame({"Count": theme_counts}))

    source_counts = question_frame.groupby("source").size().sort_values(ascending=False)
    with chart_cols[1]:
        st.markdown("**Answer Source / Rule Usage**")
        st.caption("Which rule or answer source is being used most often.")
        st.bar_chart(pd.DataFrame({"Count": source_counts}))

    repeated_questions = (
        question_frame.groupby(["question_normalized", "theme"], dropna=False)
        .agg(
            count=("question", "size"),
            sample_question=("question", "first"),
            sample_answer=("answer", "first"),
            success_rate=("success", lambda series: float((series.astype(str).str.lower() == "yes").mean() * 100.0)),
        )
        .reset_index()
        .sort_values(by=["count", "success_rate"], ascending=[False, False], kind="stable")
    )
    st.markdown("**Top Repeated Questions**")
    st.dataframe(
        repeated_questions.head(25)[["theme", "count", "sample_question", "sample_answer", "success_rate"]].rename(
            columns={
                "theme": "Theme",
                "count": "Count",
                "sample_question": "Example Question",
                "sample_answer": "Example Answer",
                "success_rate": "Success %",
            }
        ),
        use_container_width=True,
        hide_index=True,
    )

    theme_breakdown = (
        question_frame.groupby("theme", dropna=False)
        .agg(
            count=("question", "size"),
            unique_patterns=("question_normalized", "nunique"),
            jobs=("job_id", "nunique"),
            success_rate=("success", lambda series: float((series.astype(str).str.lower() == "yes").mean() * 100.0)),
        )
        .reset_index()
        .sort_values(by=["count", "unique_patterns"], ascending=[False, False], kind="stable")
    )
    st.markdown("**Theme Breakdown**")
    st.dataframe(
        theme_breakdown.rename(
            columns={
                "theme": "Theme",
                "count": "Questions",
                "unique_patterns": "Unique Patterns",
                "jobs": "Jobs",
                "success_rate": "Success %",
            }
        ),
        use_container_width=True,
        hide_index=True,
    )

    st.markdown("**Recent Captured Questions**")
    st.dataframe(
        question_frame[["title", "company", "theme", "question", "answer", "source", "success"]]
        .rename(
            columns={
                "title": "Title",
                "company": "Company",
                "theme": "Theme",
                "question": "Question",
                "answer": "Answer",
                "source": "Source",
                "success": "Success",
            }
        )
        .head(150),
        use_container_width=True,
        hide_index=True,
    )


if __name__ == "__main__":
    main()
