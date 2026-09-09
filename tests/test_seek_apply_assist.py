from __future__ import annotations

import builtins
import json
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import src.seek_apply_assist as seek_apply_assist_module
from src.database import Database
from src.job_importer import JobImporter
from src.seek_apply_assist import (
    DEFAULT_APPLICANT_PROFILE,
    _PlaywrightSeekAdapter,
    _advance_post_questionnaire_navigation,
    _wait_for_seek_apply_page_ready,
    _terminate_seeded_browser_processes,
    _wait_for_post_quick_apply_ready,
    _get_seek_pause_diagnostics,
    _resolve_seek_job_open_urls,
    _is_review_page,
    capture_local_dom_diagnostics,
    _choose_resume_limit_delete_candidate,
    _cleanup_stale_seek_profile_locks,
    configured_seek_browser_profile_dir,
    _extract_question_text_details_from_candidates,
    _extract_question_text_from_candidates,
    _manual_continue_prompt,
    _save_resume_section_dom_snapshot,
    _save_verification_failure_artifacts,
    click_seek_continue,
    handle_seek_document_step,
    handle_dynamic_seek_questions,
    handle_seek_questionnaire,
    open_seek_login_in_browser,
    preferred_seek_browser_channels,
    prompt_for_seek_login,
    read_seek_session_state,
    resolve_seek_browser_profile_launch_settings,
    resolve_free_text_question_answer,
    resolve_profile_backed_answer,
    seek_session_state_path_for_db,
    verify_resume_ready_to_continue,
    wait_for_resume_upload_complete,
    PROFILE_LOCKED_MESSAGE,
    SeekBrowserProfileLockedError,
    _assist_seek_apply,
    format_salary_expectation,
    handle_resume_upload,
    load_applicant_profile,
    seek_profile_dir_for_db,
    seek_profile_appears_locked,
    seed_isolated_seek_browser_profile_from_configured_profile,
    seed_temp_seek_profile_from_logged_in_profile,
    seek_temp_profile_dir_for_db,
    submit_seek_review,
    wait_for_seek_step,
    write_seek_session_state,
)


class FakeSeekAdapter:
    def __init__(
        self,
        visible: bool,
        *,
        final_label: str = "Submit application",
        pause_reason: str | None = None,
        has_file_input: bool = True,
        radio_available: bool = False,
        button_available: bool = False,
        resume_present: bool = False,
        resume_filename: str = "",
        existing_resume_options: list[str] | None = None,
        change_radio_available: bool = False,
        resume_limit_present: bool = False,
        resume_limit_options: list[str] | None = None,
        upload_succeeds: bool = True,
        file_input_after_upload_click: bool = False,
        file_chooser_available: bool = False,
        file_input_candidates: list[dict] | None = None,
        questionnaire_eligibility_available: bool = True,
        questionnaire_salary_available: bool = True,
        dynamic_questions: list[dict] | None = None,
        validation_errors: list[str] | None = None,
        clear_validation_errors_on_questionnaire_continue: bool = False,
        questionnaire_continue_validation_sequence: list[list[str]] | None = None,
        dont_include_checked: bool = False,
        loading_indicator_visible: bool = False,
        loading_polls_before_complete: int = 0,
        recover_after_stuck_toggle: bool = False,
        filename_disappears_after_toggle: bool = False,
        loading_stays_after_recovery: bool = False,
        resume_section_screenshot_fails: bool = False,
        global_spinner_visible: bool = False,
        page_text_value: str = "",
        quick_apply_error: str = "",
    ) -> None:
        self.visible = visible
        self._final_label = final_label
        self._pause_reason = pause_reason
        self._has_file_input = has_file_input
        self._radio_available = radio_available
        self._button_available = button_available
        self._resume_present = resume_present
        self._resume_filename = resume_filename
        self._existing_resume_options = list(existing_resume_options or [])
        self._change_radio_available = change_radio_available
        self._resume_limit_present = resume_limit_present
        self._resume_limit_options = list(resume_limit_options or [])
        self._upload_succeeds = upload_succeeds
        self._file_input_after_upload_click = file_input_after_upload_click
        self._file_chooser_available = file_chooser_available
        self._file_input_candidates = list(file_input_candidates or [])
        self._questionnaire_eligibility_available = questionnaire_eligibility_available
        self._questionnaire_salary_available = questionnaire_salary_available
        self._dynamic_questions = dynamic_questions or []
        self._validation_errors = validation_errors or []
        self._clear_validation_errors_on_questionnaire_continue = clear_validation_errors_on_questionnaire_continue
        self._questionnaire_continue_validation_sequence = list(questionnaire_continue_validation_sequence or [])
        self._questionnaire_continue_clicks = 0
        self._radio_checked = False
        self._dont_include_checked = dont_include_checked
        self._loading_indicator_visible = loading_indicator_visible
        self._loading_polls_before_complete = loading_polls_before_complete
        self._recover_after_stuck_toggle = recover_after_stuck_toggle
        self._filename_disappears_after_toggle = filename_disappears_after_toggle
        self._loading_stays_after_recovery = loading_stays_after_recovery
        self._resume_section_screenshot_fails = resume_section_screenshot_fails
        self._global_spinner_visible = global_spinner_visible
        self._page_text_value = page_text_value
        self._quick_apply_error = quick_apply_error
        self._resume_progress_polls = 0
        self.uploaded_path: str | None = None
        self.clicked_final_navigation = False
        self.saved_debug = False
        self.saved_selected_screenshot = False
        self.url = "https://www.seek.co.nz/job/12345678/apply"
        self.password_fill_attempted = False
        self.close_called = False
        self.dynamic_answers: dict[str, str] = {}
        self.questionnaire_continue_clicked = False
        self.dynamic_unknown_saved = False
        self.cover_continue_clicked = False
        self.resume_limit_selected_option: str | None = None
        self.existing_resume_selected_option: str | None = None
        self.resume_limit_delete_clicked = False
        self.resume_limit_delete_confirmed = False
        self.existing_resume_delete_clicked = False
        self.upload_attempts = 0
        self.upload_button_clicks = 0
        self.wait_for_file_input_calls = 0
        self.dont_include_clicked = False
        self.none_radio_clicks = 0
        self.upload_radio_clicks = 0
        self.recovery_reupload_count = 0
        self.local_dom_capture_calls = 0

    def open_job(self, url: str, fallback_url: str | None = None) -> None:
        self.url = url

    def detect_pause_reason(self) -> str | None:
        return self._pause_reason

    def click_quick_apply(self) -> None:
        if self._quick_apply_error:
            raise seek_apply_assist_module.SeekApplyAssistError(self._quick_apply_error)
        return None

    def wait_for_application_form(self) -> None:
        return None

    def wait_for_resume_section(self, timeout_ms: int = 20000) -> None:
        return None

    def log_resume_diagnostics(self) -> None:
        return None

    def get_resume_file_input_state(self) -> tuple[int, bool, bool]:
        if self._file_input_candidates:
            count = len(self._file_input_candidates)
            visible = any(bool(candidate.get("visible", False)) for candidate in self._file_input_candidates)
            attached = any(not bool(candidate.get("detached", False)) for candidate in self._file_input_candidates)
            return count, visible, attached
        return (1 if self._has_file_input else 0, self._has_file_input, self._has_file_input)

    def get_resume_upload_radio_state(self) -> tuple[bool, bool]:
        return self._radio_available, self._radio_checked

    def get_resume_method_state(self) -> dict[str, object]:
        if self._dont_include_checked:
            selected_value = "none"
        elif self._radio_checked:
            selected_value = "upload"
        elif self._change_radio_available and self._existing_resume_options:
            selected_value = "change"
        else:
            selected_value = ""
        return {
            "upload_radio_checked": self._radio_checked,
            "select_resume_radio_checked": bool(selected_value == "change"),
            "dont_include_resume_radio_checked": self._dont_include_checked,
            "selected_resume_method_value": selected_value,
        }

    def click_resume_change_radio(self) -> bool:
        if self._change_radio_available:
            self._radio_checked = False
            self._dont_include_checked = False
            return True
        return False

    def get_existing_resume_options(self) -> list[str]:
        return list(self._existing_resume_options)

    def select_existing_resume_option(self, option_text: str) -> bool:
        if option_text in self._existing_resume_options:
            self.existing_resume_selected_option = option_text
            return True
        return False

    def click_existing_resume_delete(self) -> bool:
        if self.existing_resume_selected_option:
            self.existing_resume_delete_clicked = True
            self._existing_resume_options = [
                option for option in self._existing_resume_options if option != self.existing_resume_selected_option
            ]
            return True
        return False

    def get_resume_upload_progress_state(self, expected_filename: str = "") -> dict[str, object]:
        self._resume_progress_polls += 1
        loading_visible = self._loading_indicator_visible
        if self._loading_polls_before_complete > 0:
            loading_visible = True
            self._loading_polls_before_complete -= 1
        else:
            loading_visible = self._loading_indicator_visible
        return {
            "filename_visible": bool(self._resume_filename),
            "loading_indicator_visible": loading_visible,
            "loading_text_visible": loading_visible,
            "loading_dots_visible": loading_visible,
            "spinner_visible": loading_visible,
            "local_resume_container_found": True,
            "local_resume_loading_indicator_visible": loading_visible,
            "global_spinner_ignored": bool(self._global_spinner_visible and not loading_visible),
            "ignored_global_spinner_candidate": "<div class='spinner'></div>" if self._global_spinner_visible and not loading_visible else "",
            "section_text_snippet": "Example_Candidate_CV.docx ..." if loading_visible else "Example_Candidate_CV.docx",
            "body_text_snippet": "Example_Candidate_CV.docx ..." if loading_visible else "Example_Candidate_CV.docx",
            "filename_text": self._resume_filename,
        }

    def wait_for_resume_file_input(self, timeout_ms: int = 5000) -> bool:
        self.wait_for_file_input_calls += 1
        if self._file_input_candidates:
            return any(not bool(candidate.get("detached", False)) for candidate in self._file_input_candidates)
        return self._has_file_input

    def try_resume_upload_via_file_chooser(self, file_path: Path, timeout_ms: int = 5000) -> bool:
        clicked = self.click_resume_upload_button()
        if not self._file_chooser_available or not clicked:
            return False
        self.uploaded_path = str(file_path)
        if self._upload_succeeds:
            self._resume_filename = Path(file_path).name
            self._resume_present = True
        return True

    def upload_resume(self, file_path: Path) -> None:
        if self.none_radio_clicks > 0 and self._filename_disappears_after_toggle:
            self.recovery_reupload_count += 1
        self.upload_attempts += 1
        if self._file_input_candidates:
            ranked = sorted(
                self._file_input_candidates,
                key=lambda candidate: (
                    0 if candidate.get("disabled") else 1,
                    1 if candidate.get("visible") else 0,
                    1 if "doc" in str(candidate.get("accept", "")).lower() or "pdf" in str(candidate.get("accept", "")).lower() else 0,
                ),
                reverse=True,
            )
            for candidate in ranked:
                if candidate.get("disabled") or candidate.get("detached"):
                    continue
                if candidate.get("works", True):
                    self.uploaded_path = str(file_path)
                    self._resume_filename = Path(file_path).name
                    self._resume_present = True
                    return
            raise RuntimeError("No working resume file input candidate")
        self.uploaded_path = str(file_path)
        if self._upload_succeeds:
            self._resume_filename = Path(file_path).name
            self._resume_present = True

    def click_resume_upload_radio(self) -> bool:
        if self._radio_available:
            self.upload_radio_clicks += 1
            self._radio_checked = True
            self._dont_include_checked = False
            if not self._file_input_after_upload_click:
                self._has_file_input = True
            if self.none_radio_clicks > 0 and self._recover_after_stuck_toggle:
                if self._filename_disappears_after_toggle:
                    self._resume_filename = ""
                if not self._loading_stays_after_recovery:
                    self._loading_indicator_visible = False
                    self._loading_polls_before_complete = 0
            return True
        return False

    def click_resume_none_radio(self) -> bool:
        self.none_radio_clicks += 1
        self.dont_include_clicked = True
        self._dont_include_checked = True
        self._radio_checked = False
        if self._filename_disappears_after_toggle:
            self._resume_filename = ""
            self._resume_present = False
        return True

    def click_resume_upload_button(self) -> bool:
        self.upload_button_clicks += 1
        if self._button_available:
            self._has_file_input = True
            return True
        return False

    def save_resume_upload_selected_screenshot(self, debug_dir: Path) -> Path:
        debug_dir.mkdir(parents=True, exist_ok=True)
        screenshot = debug_dir / "resume_upload_selected.png"
        screenshot.write_bytes(b"selected")
        self.saved_selected_screenshot = True
        return screenshot

    def save_resume_upload_selected_html(self, debug_dir: Path) -> Path:
        debug_dir.mkdir(parents=True, exist_ok=True)
        html = debug_dir / "resume_after_upload_radio_click.html"
        html.write_text("<html>after upload radio</html>", encoding="utf-8")
        return html

    def save_resume_after_file_upload_screenshot(self, debug_dir: Path) -> Path:
        debug_dir.mkdir(parents=True, exist_ok=True)
        screenshot = debug_dir / "resume_after_file_upload.png"
        screenshot.write_bytes(b"after-file-upload")
        return screenshot

    def detect_visible_resume_filename(self) -> str:
        return self._resume_filename

    def resume_appears_present(self) -> bool:
        return self._resume_present

    def resume_limit_ui_present(self) -> bool:
        return self._resume_limit_present

    def get_resume_limit_dropdown_options(self) -> list[str]:
        return list(self._resume_limit_options)

    def select_resume_limit_dropdown_option(self, option_text: str) -> bool:
        if option_text in self._resume_limit_options:
            self.resume_limit_selected_option = option_text
            return True
        return False

    def click_resume_limit_delete(self) -> bool:
        if self.resume_limit_selected_option:
            self.resume_limit_delete_clicked = True
            self._resume_limit_options = [option for option in self._resume_limit_options if option != self.resume_limit_selected_option]
            return True
        return False

    def confirm_resume_limit_delete(self) -> bool:
        if self.resume_limit_delete_clicked:
            self.resume_limit_delete_confirmed = True
            self._resume_limit_present = False
            self._has_file_input = True
            self._upload_succeeds = True
            if self._file_input_candidates:
                self._file_input_candidates = [
                    {"visible": True, "disabled": False, "accept": ".doc,.docx,.pdf", "works": True}
                ]
            return True
            return True
        if self.existing_resume_delete_clicked:
            self.resume_limit_delete_confirmed = True
            self._has_file_input = True
            self._upload_succeeds = True
            return True
        return False

    def wait_for_resume_limit_change(self, previous_options: list[str], timeout_ms: int = 5000) -> None:
        return None

    def save_resume_debug_artifacts(self, debug_dir: Path) -> tuple[Path, Path]:
        debug_dir.mkdir(parents=True, exist_ok=True)
        screenshot = debug_dir / "resume_step_failed.png"
        html = debug_dir / "resume_step_failed.html"
        screenshot.write_bytes(b"fake-image")
        html.write_text("<html>resume failed</html>", encoding="utf-8")
        self.saved_debug = True
        return screenshot, html

    def save_named_resume_artifacts(self, debug_dir: Path, stem: str) -> tuple[Path, Path]:
        debug_dir.mkdir(parents=True, exist_ok=True)
        screenshot = debug_dir / f"{stem}.png"
        html = debug_dir / f"{stem}.html"
        screenshot.write_bytes(b"named-artifact")
        html.write_text(f"<html>{stem}</html>", encoding="utf-8")
        return screenshot, html

    def collect_resume_upload_diagnostics(self) -> dict[str, object]:
        return {
            "file_input_count": len(self._file_input_candidates) if self._file_input_candidates else (1 if self._has_file_input else 0),
            "file_inputs": list(self._file_input_candidates),
            "upload_counts": {
                "input_type_file": len(self._file_input_candidates) if self._file_input_candidates else (1 if self._has_file_input else 0),
                "labels_with_upload": 1 if self._radio_available else 0,
                "buttons_with_upload": 1 if self._button_available else 0,
                "role_buttons_with_upload": 1 if self._button_available else 0,
                "text_nodes_with_upload": 1 if self._button_available else 0,
            },
            "upload_text_candidates": [
                {
                    "text": "Upload",
                    "tag": "button",
                    "role": "button",
                    "tabindex": "",
                    "outer_html_snippet": "<button><span>Upload</span></button>",
                    "parent_chain_html": ["<button><span>Upload</span></button>"],
                    "bounding_box": {"x": 10, "y": 20, "width": 120, "height": 40},
                }
            ] if self._button_available else [],
        }

    def collect_resume_section_snapshot(self, expected_filename: str = "") -> dict[str, object]:
        progress_text = f"{self._resume_filename} ..." if self._loading_indicator_visible else self._resume_filename
        return {
            "page_url": self.url,
            "page_title": self.title(),
            "resume_section_html": "<section><label>Upload a resume</label></section>",
            "resume_section_text": progress_text or "Upload a resume",
            "visible_resume_filenames": [self._resume_filename] if self._resume_filename else [],
            "file_input_diagnostics": list(self._file_input_candidates) if self._file_input_candidates else [
                {
                    "index": 0,
                    "visible": self._has_file_input,
                    "attached": self._has_file_input,
                    "disabled": False,
                    "accept": ".doc,.docx,.pdf",
                    "outer_html_snippet": "<input type='file'>",
                    "bounding_box": None,
                }
            ],
            "progress_candidates": [
                {
                    "selector_matched": "text_match" if self._loading_indicator_visible else "global_spinner",
                    "visible": self._loading_indicator_visible or self._global_spinner_visible,
                    "text": progress_text if self._loading_indicator_visible else "",
                    "outer_html_snippet": "<div class='loading'>...</div>" if self._loading_indicator_visible else "<div class='spinner'></div>",
                    "bounding_box": {"x": 10, "y": 10, "width": 40, "height": 12},
                    "computed_animation_name": "pulse" if (self._loading_indicator_visible or self._global_spinner_visible) else "",
                    "computed_display": "block",
                    "computed_visibility": "visible",
                    "aria_busy": "true" if (self._loading_indicator_visible or self._global_spinner_visible) else "",
                    "role": "progressbar" if (self._loading_indicator_visible or self._global_spinner_visible) else "",
                }
            ],
            "buttons_labels_visible": ["Upload", "Don't include a resume"],
            "continue_enabled": True,
            "validation_error_texts": [],
            "resume_limit_modal_visible": self._resume_limit_present,
            "snapshot_capture_errors": [],
        }

    def save_resume_section_screenshot(self, path: Path, *, expected_filename: str = "") -> tuple[bool, str]:
        if self._resume_section_screenshot_fails:
            return False, "fake screenshot failure"
        path.write_bytes(b"resume-section")
        return True, ""

    def capture_local_dom_diagnostics(self, failure_type: str, expected_filename: str = "") -> dict[str, object]:
        self.local_dom_capture_calls += 1
        return {
            "failure_type": failure_type,
            "filename_visible": bool(self._resume_filename),
            "upload_radio_checked": self._radio_checked,
            "dont_include_checked": self._dont_include_checked,
            "continue_enabled": True,
            "local_spinner_count": 1 if self._loading_indicator_visible else 0,
            "local_spinner_details": [
                {
                    "selector_matched": "text_match",
                    "visible": self._loading_indicator_visible,
                    "text": "processing..." if self._loading_indicator_visible else "",
                    "outer_html_snippet": "<div class='loading'>...</div>",
                    "bounding_box": {"x": 10, "y": 20, "width": 40, "height": 12},
                    "computed_animation_name": "pulse" if self._loading_indicator_visible else "",
                    "computed_display": "block",
                    "computed_visibility": "visible",
                    "aria_busy": "true" if self._loading_indicator_visible else "",
                    "role": "progressbar" if self._loading_indicator_visible else "",
                }
            ] if self._loading_indicator_visible else [],
            "parent_container_selector_guess": "section.resume-row",
            "nearby_buttons": [{"text": "Upload a different resume", "tag": "button", "role": "button", "disabled": False}],
            "nearby_inputs": [{"tag": "input", "type": "radio", "name": "resume-method", "value": "upload", "checked": self._radio_checked}],
            "nearby_labels": ["Upload a resume", "Don't include a resume"],
            "nearby_validation_text": [],
            "local_container_html": "<section class='resume-row'><span>Example_Candidate_CV.docx</span></section>",
            "local_container_text": self._resume_filename or "Upload a resume",
        }

    def save_local_dom_screenshot(self, path: Path, *, failure_type: str, expected_filename: str = "") -> tuple[bool, str]:
        if self._resume_section_screenshot_fails:
            return False, "fake screenshot failure"
        path.write_bytes(b"cropped-local-dom")
        return True, ""

    def save_resume_pre_search_artifacts(self, debug_dir: Path) -> tuple[Path, Path]:
        debug_dir.mkdir(parents=True, exist_ok=True)
        screenshot = debug_dir / "resume_before_selector_search.png"
        html = debug_dir / "resume_before_selector_search.html"
        screenshot.write_bytes(b"before-search")
        html.write_text("<html>before search</html>", encoding="utf-8")
        return screenshot, html

    def select_cover_letter_change(self) -> None:
        return None

    def fill_cover_letter(self, text: str) -> None:
        self.cover_letter = text
        page = getattr(self, "_page", None)
        if page is not None:
            page._visible_text = f"Write cover letter {text}"

    def click_continue(self) -> None:
        self.cover_continue_clicked = True
        return None

    def wait_for_questionnaire_section(self, timeout_ms: int = 20000) -> None:
        return None

    def log_questionnaire_diagnostics(self) -> None:
        return None

    def save_questionnaire_pre_search_artifacts(self, debug_dir: Path) -> tuple[Path, Path]:
        debug_dir.mkdir(parents=True, exist_ok=True)
        screenshot = debug_dir / "questionnaire_before_search.png"
        html = debug_dir / "questionnaire_before_search.html"
        screenshot.write_bytes(b"questionnaire-before")
        html.write_text("<html>questionnaire before</html>", encoding="utf-8")
        return screenshot, html

    def select_work_eligibility_yes(self) -> bool:
        return self._questionnaire_eligibility_available

    def fill_salary_expectation(self, text: str) -> bool:
        self.salary_expectation = text
        return self._questionnaire_salary_available

    def save_questionnaire_failed_artifacts(self, debug_dir: Path) -> tuple[Path, Path]:
        debug_dir.mkdir(parents=True, exist_ok=True)
        screenshot = debug_dir / "questionnaire_failed.png"
        html = debug_dir / "questionnaire_failed.html"
        screenshot.write_bytes(b"questionnaire-failed")
        html.write_text("<html>questionnaire failed</html>", encoding="utf-8")
        return screenshot, html

    def extract_dynamic_questions(self) -> list[dict[str, str]]:
        return list(self._dynamic_questions)

    def answer_dynamic_question(self, question: dict[str, str], answer: str) -> bool:
        expected = question.get("expected_answer")
        if expected is not None and expected != answer:
            return False
        key = str(question.get("name") or question.get("question_text") or f"question-{len(self.dynamic_answers)}")
        self.dynamic_answers[key] = answer
        question_text = str(question.get("question_text") or "").strip()
        self._validation_errors = [
            error for error in self._validation_errors
            if question_text.lower() not in str(error or "").lower()
        ]
        return True

    def get_questionnaire_validation_errors(self) -> list[str]:
        return list(self._validation_errors)

    def save_dynamic_question_debug_artifacts(self, debug_dir: Path) -> tuple[Path, Path]:
        debug_dir.mkdir(parents=True, exist_ok=True)
        screenshot = debug_dir / "dynamic_questions_unknown.png"
        html = debug_dir / "dynamic_questions_unknown.html"
        screenshot.write_bytes(b"dynamic-question-unknown")
        html.write_text("<html>dynamic questions unknown</html>", encoding="utf-8")
        self.dynamic_unknown_saved = True
        return screenshot, html

    def click_questionnaire_continue(self) -> None:
        self.questionnaire_continue_clicked = True
        self._questionnaire_continue_clicks += 1
        if self._questionnaire_continue_validation_sequence:
            sequence_index = self._questionnaire_continue_clicks - 1
            if 0 <= sequence_index < len(self._questionnaire_continue_validation_sequence):
                self._validation_errors = list(self._questionnaire_continue_validation_sequence[sequence_index])
        if self._clear_validation_errors_on_questionnaire_continue:
            self._validation_errors = []
        return None

    def final_navigation_label(self) -> str:
        return self._final_label

    def click_final_navigation(self) -> None:
        self.clicked_final_navigation = True
        self._final_label = "Submit application"

    def current_url(self) -> str:
        return self.url

    def page_text(self) -> str:
        if self._page_text_value:
            return self._page_text_value
        if self._resume_limit_present:
            return "ResumÃ© limit reached Please select a resumÃ© to delete from your list and try again. Delete"
        return ""

    def close(self) -> None:
        self.close_called = True
        return None


class _CleanupRecorder:
    def __init__(self) -> None:
        self.calls = 0

    def close(self) -> None:
        self.calls += 1


class _PlaywrightStopRecorder:
    def __init__(self) -> None:
        self.stop_calls = 0

    def stop(self) -> None:
        self.stop_calls += 1


class _PlaywrightContextManagerRecorder:
    def __init__(self) -> None:
        self.exit_calls = 0

    def __exit__(self, exc_type, exc, tb) -> None:
        self.exit_calls += 1

    def stop(self) -> None:  # pragma: no cover - should never be called
        raise AssertionError("close() should not call stop() on PlaywrightContextManager")


class FakeContinueLocator:
    def __init__(
        self,
        page: "FakeContinuePage",
        text: str = "Continue",
        *,
        tag: str = "button",
        role: str = "button",
        count_value: int | None = None,
    ) -> None:
        self.page = page
        self.text = text
        self.tag = tag
        self.role = role
        self.click_attempts: list[str] = []
        self.count_value = count_value

    @property
    def first(self) -> "FakeContinueLocator":
        return self

    @property
    def last(self) -> "FakeContinueLocator":
        return self

    def count(self) -> int:
        return 1 if self.count_value is None else self.count_value

    def inner_text(self, timeout: int = 0) -> str:
        return self.text

    def is_visible(self) -> bool:
        return True

    def is_enabled(self) -> bool:
        return True

    def locator(self, _selector: str) -> "FakeContinueLocator":
        return self

    def scroll_into_view_if_needed(self) -> None:
        return None

    def wait_for(self, state: str = "visible", timeout: int = 0) -> None:
        return None

    def click(self, force: bool = False) -> None:
        self.click_attempts.append("force_click" if force else "normal_click")
        self.page._handle_locator_click(self.text)

    def evaluate(self, script: str) -> str | None:
        if "tagName" in script:
            return self.tag
        if "getAttribute('role')" in script:
            return self.role
        if "el.click()" in script:
            self.click_attempts.append("js_click")
            self.page._handle_locator_click(self.text)
            return None
        return None

    def focus(self) -> None:
        self.page.focused = True

    def bounding_box(self) -> dict[str, float]:
        return {"x": 10.0, "y": 20.0, "width": 120.0, "height": 40.0}


class FakeKeyboard:
    def __init__(self, page: "FakeContinuePage") -> None:
        self.page = page

    def press(self, key: str) -> None:
        self.page.keyboard_presses.append(key)
        self.page._handle_locator_click("Continue")


class FakeContinuePage:
    def __init__(
        self,
        *,
        transition_mode: str = "role_requirements",
        select_count: int = 0,
        radio_count: int = 0,
        textarea_count: int = 0,
        input_count: int = 0,
    ) -> None:
        self.url = "https://www.seek.co.nz/job/12345678/apply"
        self._title = "Write cover letter | SEEK"
        self._visible_text = "Continue"
        self.keyboard = FakeKeyboard(self)
        self.locator_instance = FakeContinueLocator(self)
        self.keyboard_presses: list[str] = []
        self.focused = False
        self.transition_mode = transition_mode
        self.select_count = select_count
        self.radio_count = radio_count
        self.textarea_count = textarea_count
        self.input_count = input_count
        self.file_input_count = 1
        self.resume_toggle_clicked = False
        if self.transition_mode in {"choose_documents_stuck_then_fallback", "choose_documents_never_leaves"}:
            self._title = "Choose documents | SEEK"
            self._visible_text = "Choose documents Continue Example_Candidate_CV.docx Upload a different rÃ©sumÃ©"

    def title(self) -> str:
        return self._title

    def get_by_role(self, _role: str, name: object = None) -> FakeContinueLocator:
        return self.locator_instance

    def locator(self, _selector: str) -> FakeContinueLocator:
        selector = _selector.strip()
        if selector == "body":
            return FakeContinueLocator(self, text=self._visible_text, tag="body")
        if selector == 'select[name*="questionnaire"]':
            return FakeContinueLocator(self, text="", tag="select", count_value=self.select_count)
        if selector == 'input[type="radio"][name*="questionnaire"]':
            return FakeContinueLocator(self, text="", tag="input", count_value=self.radio_count)
        if selector == 'textarea[name*="questionnaire"]':
            return FakeContinueLocator(self, text="", tag="textarea", count_value=self.textarea_count)
        if selector == 'input[name*="questionnaire"]':
            return FakeContinueLocator(self, text="", tag="input", count_value=self.input_count)
        if selector == 'input[type="file"]':
            return FakeContinueLocator(self, text="", tag="input", count_value=self.file_input_count)
        return self.locator_instance

    def get_by_text(self, _text: str, exact: bool = False) -> FakeContinueLocator:
        text_value = getattr(_text, "pattern", str(_text))
        return FakeContinueLocator(self, text=text_value)

    def get_by_label(self, _text: str, exact: bool = False) -> FakeContinueLocator:
        text_value = getattr(_text, "pattern", str(_text))
        return FakeContinueLocator(self, text=text_value)

    def evaluate(self, script: str):
        if "document.body?.innerText" in script:
            return self._visible_text
        if "querySelectorAll('button, [role=\"button\"], a, span, div')" in script or "querySelectorAll('button, [role=\"button\"], a, span, div')" in script.replace("'", '"'):
            self._apply_transition()
            return True
        return ""

    def wait_for_function(self, _script: str, _args: list[str] | None = None, timeout: int = 0) -> None:
        self._apply_transition()
        if self.transition_mode == "no_transition":
            raise RuntimeError("timeout waiting for transition")
        return None

    def screenshot(self, path: str, full_page: bool = True) -> None:
        Path(path).write_bytes(b"fake-screenshot")

    def content(self) -> str:
        return f"<html><body>{self._visible_text}</body></html>"

    def wait_for_timeout(self, _timeout_ms: int) -> None:
        return None

    def _apply_transition(self) -> None:
        if self.transition_mode == "role_requirements":
            self.url = "https://www.seek.co.nz/job/12345678/apply/role-requirements"
            self._title = "Answer employer questions | SEEK"
            self._visible_text = "Answer employer questions"
        elif self.transition_mode == "questionnaire_fields":
            self.url = "https://www.seek.co.nz/job/12345678/apply"
            self._title = "Write cover letter | SEEK"
            self._visible_text = "Answer employer questions"
            if self.select_count == 0 and self.radio_count == 0 and self.textarea_count == 0 and self.input_count == 0:
                self.select_count = 1
        elif self.transition_mode == "transient_text_only":
            self.url = "https://www.seek.co.nz/job/12345678/apply"
            self._title = "Write cover letter | SEEK"
            self._visible_text = "Answer employer questions"
        elif self.transition_mode == "no_transition":
            self.url = "https://www.seek.co.nz/job/12345678/apply"
            self._title = "Write cover letter | SEEK"
            self._visible_text = "Continue"

    def _handle_locator_click(self, text: str) -> None:
        lowered = str(text or "").lower()
        if "don't include" in lowered or "don.t include" in lowered:
            self.resume_toggle_clicked = True
            self._visible_text = "Choose documents Continue Upload a rÃ©sumÃ©"
            return
        if "upload" in lowered and "rÃ©sumÃ©" in lowered or "upload" in lowered and "resume" in lowered:
            self.file_input_count = 1
            self._visible_text = "Choose documents Continue Example_Candidate_CV.docx Upload a different rÃ©sumÃ©"
            return
        if "continue" in lowered:
            if self.transition_mode == "choose_documents_stuck_then_fallback":
                if self.resume_toggle_clicked:
                    self.url = "https://www.seek.co.nz/job/12345678/apply/role-requirements"
                    self._title = "Answer employer questions | SEEK"
                    self._visible_text = "Answer employer questions"
                    self.select_count = max(self.select_count, 1)
                else:
                    self._title = "Choose documents | SEEK"
                    self._visible_text = "Choose documents Continue Example_Candidate_CV.docx Upload a different rÃ©sumÃ©"
                return
            if self.transition_mode == "choose_documents_never_leaves":
                self._title = "Choose documents | SEEK"
                self._visible_text = "Choose documents Continue Example_Candidate_CV.docx Upload a different rÃ©sumÃ©"
                return
            self._apply_transition()


def _raise_eof() -> None:
    raise EOFError


class SeekApplyAssistTests(unittest.TestCase):
    def test_wait_for_seek_apply_page_ready_waits_until_loading_shell_clears(self) -> None:
        class LoadingShellPage:
            def __init__(self) -> None:
                self.calls = 0

            def title(self) -> str:
                return "SEEK" if self.calls < 2 else "Choose documents | SEEK"

            def locator(self, _selector: str) -> object:
                page = self

                class BodyLocator:
                    def inner_text(self, timeout: int = 0) -> str:
                        return "" if page.calls < 2 else "Choose documents Upload a resume"

                return BodyLocator()

            def wait_for_timeout(self, _timeout_ms: int) -> None:
                self.calls += 1

        page = LoadingShellPage()

        _wait_for_seek_apply_page_ready(page, timeout_ms=3000)

        self.assertGreaterEqual(page.calls, 2)

    def test_terminate_seeded_browser_processes_returns_reported_count(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            target_root = Path(temp_dir) / "seek_seeded"
            completed = mock.Mock(stdout="3\n")
            with mock.patch("src.seek_apply_assist.sys.platform", "win32"), mock.patch(
                "src.seek_apply_assist.subprocess.run",
                return_value=completed,
            ) as run_mock:
                terminated = _terminate_seeded_browser_processes(target_root)

            self.assertEqual(terminated, 3)
            run_mock.assert_called_once()

    def test_submit_seek_review_passes_saved_review_url_into_auto_submit_flow(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            db_path = Path(temp_dir) / "job_assistant.db"
            Database(db_path)
            expected_result = seek_apply_assist_module.ApplyAssistResult(
                job_id=77,
                status="applied",
                steps_completed=["opened_review_url", "clicked_final_submit"],
                error="",
                final_url="https://nz.seek.com/job/123/apply/submitted",
            )

            with mock.patch.object(
                seek_apply_assist_module,
                "_assist_seek_apply",
                return_value=expected_result,
            ) as assist_mock:
                result = submit_seek_review(
                    77,
                    db_path,
                    review_url="https://nz.seek.com/job/123/apply/review",
                    visible=False,
                    use_bulk_profile=False,
                )

            self.assertEqual(result.status, "applied")
            assist_mock.assert_called_once()
            self.assertEqual(assist_mock.call_args.kwargs["review_url"], "https://nz.seek.com/job/123/apply/review")
            self.assertTrue(assist_mock.call_args.kwargs["auto_submit"])

    def test_submit_seek_review_falls_back_to_subprocess_for_playwright_loop_error(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            db_path = Path(temp_dir) / "job_assistant.db"
            Database(db_path)
            expected_result = seek_apply_assist_module.ApplyAssistResult(
                job_id=91,
                status="applied",
                steps_completed=["opened_review_url", "clicked_final_submit"],
                error="",
                final_url="https://nz.seek.com/job/123/apply/success",
            )
            with mock.patch.object(
                seek_apply_assist_module,
                "_submit_seek_review_inprocess",
                side_effect=RuntimeError("It looks like you are using Playwright Sync API inside the asyncio loop."),
            ) as inprocess_mock, mock.patch.object(
                seek_apply_assist_module,
                "_submit_seek_review_subprocess",
                return_value=expected_result,
            ) as subprocess_mock:
                result = submit_seek_review(
                    91,
                    db_path,
                    review_url="https://nz.seek.com/job/123/apply/review",
                    visible=False,
                    use_bulk_profile=False,
                )

            self.assertEqual(result.status, "applied")
            inprocess_mock.assert_called_once()
            subprocess_mock.assert_called_once()

    def test_submit_seek_review_retries_after_browser_profile_locked(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            db_path = Path(temp_dir) / "job_assistant.db"
            Database(db_path)
            locked_result = seek_apply_assist_module.ApplyAssistResult(
                job_id=101,
                status="browser_profile_locked",
                steps_completed=[],
                error=PROFILE_LOCKED_MESSAGE,
                final_url="https://nz.seek.com/job/123/apply/review",
            )
            recovered_result = seek_apply_assist_module.ApplyAssistResult(
                job_id=101,
                status="applied",
                steps_completed=["clicked_final_submit"],
                error="",
                final_url="https://nz.seek.com/job/123/apply/success",
            )
            with mock.patch.object(
                seek_apply_assist_module,
                "_submit_seek_review_inprocess",
                return_value=locked_result,
            ) as inprocess_mock, mock.patch.object(
                seek_apply_assist_module,
                "_submit_seek_review_subprocess",
                return_value=recovered_result,
            ) as subprocess_mock, mock.patch.object(
                seek_apply_assist_module,
                "_recover_submit_profile_lock",
            ) as recover_mock:
                result = submit_seek_review(
                    101,
                    db_path,
                    review_url="https://nz.seek.com/job/123/apply/review",
                    visible=False,
                    use_bulk_profile=False,
                )

            self.assertEqual(result.status, "applied")
            inprocess_mock.assert_called_once()
            subprocess_mock.assert_called_once()
            recover_mock.assert_called_once()

    def test_submit_seek_review_closes_stale_ready_job_when_seek_reports_closed(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            db_path = Path(temp_dir) / "job_assistant.db"
            db = Database(db_path)
            importer = JobImporter(db)
            imported = importer.import_manual_job(
                source="seek",
                url="https://www.seek.co.nz/job/12345678",
                title="Data Analyst",
                company="Example Co",
                location="Auckland",
                salary="$100,000",
                description="SQL and reporting role.",
                import_channel="test",
            )
            job_id = int(imported["job_id"])
            db.update_job_career_assets(
                job_id,
                selected_cv_path=str(PROJECT_ROOT / "README.md"),
                cover_letter_preview="Example cover letter",
            )
            db.update_application_state(job_id, "ready_to_apply", note="Prepared to SEEK review page and awaiting final submit.")
            expected_result = seek_apply_assist_module.ApplyAssistResult(
                job_id=job_id,
                status="skipped",
                steps_completed=["opened_review_url"],
                error="This SEEK job is no longer advertised.",
                final_url="https://nz.seek.com/job/123",
            )

            with mock.patch.object(
                seek_apply_assist_module,
                "_assist_seek_apply",
                return_value=expected_result,
            ):
                result = submit_seek_review(
                    job_id,
                    db_path,
                    review_url="https://www.seek.co.nz/job/12345678",
                    visible=False,
                    use_bulk_profile=False,
                )

            refreshed = db.get_job_details(job_id)
            self.assertEqual(result.status, "skipped")
            self.assertEqual(refreshed["application_status"], "closed")
            self.assertEqual(refreshed["status"], "closed")
            self.assertEqual(int(refreshed["ready_to_apply"] or 0), 0)

    def test_submit_seek_review_moves_missing_cv_ready_job_back_to_reviewed(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            db_path = Path(temp_dir) / "job_assistant.db"
            db = Database(db_path)
            importer = JobImporter(db)
            imported = importer.import_manual_job(
                source="seek",
                url="https://www.seek.co.nz/job/87654321",
                title="BI Analyst",
                company="Example Co",
                location="Auckland",
                salary="$95,000",
                description="Power BI and SQL role.",
                import_channel="test",
            )
            job_id = int(imported["job_id"])
            db.update_job_career_assets(
                job_id,
                selected_cv_path=str(PROJECT_ROOT / "README.md"),
                cover_letter_preview="Example cover letter",
            )
            db.update_application_state(job_id, "ready_to_apply", note="Prepared to SEEK review page and awaiting final submit.")
            expected_result = seek_apply_assist_module.ApplyAssistResult(
                job_id=job_id,
                status="failed",
                steps_completed=["opened_review_url"],
                error="Selected CV file not found: C:\\temp\\missing.docx",
                final_url="https://nz.seek.com/job/456",
            )

            with mock.patch.object(
                seek_apply_assist_module,
                "_assist_seek_apply",
                return_value=expected_result,
            ):
                result = submit_seek_review(
                    job_id,
                    db_path,
                    review_url="https://www.seek.co.nz/job/87654321",
                    visible=False,
                    use_bulk_profile=False,
                )

            refreshed = db.get_job_details(job_id)
            self.assertEqual(result.status, "failed")
            self.assertEqual(refreshed["application_status"], "reviewed")
            self.assertEqual(refreshed["status"], "reviewed")
            self.assertEqual(int(refreshed["ready_to_apply"] or 0), 0)

    def test_seek_submit_confirmation_detects_success_url_without_body_copy(self) -> None:
        self.assertTrue(
            seek_apply_assist_module._seek_submit_confirmation_detected(
                final_url="https://nz.seek.com/job/123/apply/success",
                page_title="Review and submit | SEEK",
                page_text="Skip to content Page Loading",
            )
        )

    def test_wait_for_post_quick_apply_ready_detects_choose_documents_from_page_text(self) -> None:
        adapter = FakeSeekAdapter(False, page_text_value="Choose documents Upload a resume")

        state, payload = _wait_for_post_quick_apply_ready(adapter, timeout_ms=1000)

        self.assertEqual(state, "application_ready")
        self.assertIn("choose documents", str(payload.get("body_excerpt") or "").lower())

    def test_wait_for_post_quick_apply_ready_times_out_with_payload(self) -> None:
        adapter = FakeSeekAdapter(
            False,
            page_text_value="Application loading",
            has_file_input=False,
            radio_available=False,
        )

        state, payload = _wait_for_post_quick_apply_ready(adapter, timeout_ms=1000)

        self.assertEqual(state, "timeout")
        self.assertIn("application loading", str(payload.get("body_excerpt") or "").lower())

    def test_prompt_for_seek_login_returns_clean_pause_when_manual_prompt_is_disallowed(self) -> None:
        adapter = mock.Mock()
        adapter._page = None
        resumed, message = prompt_for_seek_login(
            adapter,
            allow_manual_prompt=False,
            input_func=lambda: self.fail("manual prompt should not be invoked"),
        )
        self.assertFalse(resumed)
        self.assertIn("non-interactive", message)

    def test_prompt_for_seek_login_uses_shorter_auto_code_timeout_when_non_interactive(self) -> None:
        adapter = mock.Mock()
        adapter._page = object()
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            db_path = Path(temp_dir) / "job_assistant.db"
            Database(db_path)
            with mock.patch(
                "src.seek_apply_assist._attempt_seek_email_code_login",
                return_value=(False, "No recent SEEK sign-in code email was found in Gmail."),
            ) as auto_login_mock:
                resumed, message = prompt_for_seek_login(
                    adapter,
                    db_path=db_path,
                    allow_manual_prompt=False,
                    input_func=lambda: self.fail("manual prompt should not be invoked"),
                )

        self.assertFalse(resumed)
        self.assertIn("non-interactive", message)
        auto_login_mock.assert_called_once()
        self.assertEqual(auto_login_mock.call_args.kwargs["code_fetch_timeout_seconds"], 15)

    def test_open_seek_login_in_browser_uses_configured_profile_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "data" / "job_assistant.db"
            db_path.parent.mkdir(parents=True, exist_ok=True)
            profile_json = db_path.parent / "applicant_profile.json"
            profile_json.write_text(
                json.dumps(
                    {
                        "automation": {
                            "seek_browser_profile_path": "C:/Users/ExampleUser/AppData/Local/Google/Chrome/User Data/Profile 2",
                            "seek_browser_channels": ["chrome"],
                        }
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch("src.seek_apply_assist.find_seek_browser_executable", return_value=("chrome", Path("C:/Program Files/Google/Chrome/Application/chrome.exe"))):
                with mock.patch("src.seek_apply_assist.subprocess.Popen") as popen_mock:
                    channel, profile_root = open_seek_login_in_browser(db_path)
            self.assertEqual(channel, "chrome")
            self.assertEqual(profile_root, "C:\\Users\\ExampleUser\\AppData\\Local\\Google\\Chrome\\User Data")
            command = popen_mock.call_args.args[0]
            self.assertIn("--profile-directory=Profile 2", command)
            self.assertIn("--user-data-dir=C:\\Users\\ExampleUser\\AppData\\Local\\Google\\Chrome\\User Data", command)
            self.assertEqual(command[-1], "https://login.seek.com")

    def test_write_and_read_seek_session_state_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "job_assistant.db"
            write_seek_session_state(
                db_path,
                "verification_required",
                job_id=123,
                reason="SEEK verification is required before automation can continue.",
                matched_token="troubleshoot",
                matched_source="page_text",
                final_url="https://login.seek.com/challenge",
            )
            payload = read_seek_session_state(db_path)
            self.assertEqual(payload["state"], "verification_required")
            self.assertEqual(payload["job_id"], 123)
            self.assertEqual(payload["matched_token"], "troubleshoot")
            self.assertEqual(payload["matched_source"], "page_text")
            self.assertEqual(payload["final_url"], "https://login.seek.com/challenge")
            self.assertTrue(seek_session_state_path_for_db(db_path).exists())

    def test_answer_dynamic_question_radio_uses_matching_option_value(self) -> None:
        class RadioLocator:
            def __init__(self, checked: bool = False, count_value: int = 1) -> None:
                self._checked = checked
                self._count = count_value
                self.checked_called = False

            @property
            def first(self):
                return self

            def count(self) -> int:
                return self._count

            def is_checked(self) -> bool:
                return self._checked

            def check(self, force: bool = False) -> None:
                self.checked_called = True
                self._checked = True

            def evaluate(self, _script: str):
                return None

            def locator(self, _selector: str):
                return self

            def get_by_label(self, _pattern):
                return RadioLocator(count_value=0)

            def get_by_text(self, _pattern):
                return RadioLocator(count_value=0)

            def get_attribute(self, name: str) -> str:
                if name == "value":
                    return "NZ_Q_298_V_2_A_23239"
                return ""

            def nth(self, _index: int):
                return self

        class RadioPage:
            def __init__(self) -> None:
                self.matching_radio = RadioLocator()
                self.generic_radio = RadioLocator()

            def locator(self, selector: str):
                if '[value="NZ_Q_298_V_2_A_23239"]' in selector:
                    return self.matching_radio
                if 'input[type="radio"][name="questionnaire.NZ_Q_298_V_2"]' in selector:
                    return self.generic_radio
                return RadioLocator(count_value=0)

        adapter = object.__new__(_PlaywrightSeekAdapter)
        adapter._page = RadioPage()
        question = {
            "field_type": "radio",
            "name": "questionnaire.NZ_Q_298_V_2",
            "option_payloads": [
                {"label": "Yes", "value": "NZ_Q_298_V_2_A_23239"},
                {"label": "No", "value": "NZ_Q_298_V_2_A_23240"},
            ],
        }
        self.assertTrue(adapter.answer_dynamic_question(question, "Yes"))
        self.assertTrue(adapter._page.matching_radio.checked_called)

    def test_playwright_adapter_close_uses_playwright_instance_and_is_idempotent(self) -> None:
        adapter = object.__new__(_PlaywrightSeekAdapter)
        page = _CleanupRecorder()
        context = _CleanupRecorder()
        playwright = _PlaywrightStopRecorder()
        playwright_cm = _PlaywrightContextManagerRecorder()
        adapter._page = page
        adapter._context = context
        adapter._browser = None
        adapter._playwright = playwright
        adapter._playwright_cm = playwright_cm

        adapter.close()
        adapter.close()

        self.assertEqual(page.calls, 1)
        self.assertEqual(context.calls, 1)
        self.assertEqual(playwright.stop_calls, 1)
        self.assertEqual(playwright_cm.exit_calls, 1)
        self.assertIsNone(adapter._page)
        self.assertIsNone(adapter._context)
        self.assertIsNone(adapter._playwright)
        self.assertIsNone(adapter._playwright_cm)

    def test_resume_limit_ui_present_does_not_treat_plain_resume_selection_prompt_as_limit_modal(self) -> None:
        class _Locator:
            def __init__(self, *, count_value: int = 0, text_value: str = "") -> None:
                self._count_value = count_value
                self._text_value = text_value

            def count(self) -> int:
                return self._count_value

            def inner_text(self, timeout: int = 5000) -> str:
                return self._text_value

        class _Page:
            def locator(self, selector: str) -> _Locator:
                if selector == "#docLimitExceededDropdown":
                    return _Locator(count_value=0)
                if selector == "body":
                    return _Locator(text_value="Please select a resumÃ© 2/7/26 - Example_Candidate_CV.docx")
                return _Locator()

        adapter = object.__new__(_PlaywrightSeekAdapter)
        adapter._page = _Page()

        self.assertFalse(adapter.resume_limit_ui_present())

    def test_click_quick_apply_returns_when_apply_page_is_already_open(self) -> None:
        class _Page:
            def wait_for_timeout(self, _timeout_ms: int) -> None:
                return None

            def title(self) -> str:
                return "Choose documents | SEEK"

            def locator(self, _selector: str):
                raise AssertionError("Quick Apply selectors should not run when the apply page is already open.")

        adapter = object.__new__(_PlaywrightSeekAdapter)
        adapter._page = _Page()
        adapter._timeout_error = RuntimeError
        adapter.current_url = lambda: "https://nz.seek.com/job/93051967/apply"
        adapter.page_text = lambda: "Choose documents Continue Upload a resume"

        with mock.patch("src.seek_apply_assist._wait_for_seek_apply_page_ready", return_value=None):
            adapter.click_quick_apply()

    def test_manual_continue_prompt_is_step_specific(self) -> None:
        self.assertIn("Cover letter pasted", _manual_continue_prompt("cover_letter"))
        self.assertIn("Questions answered", _manual_continue_prompt("questionnaire"))
        self.assertIn("Continue could not be clicked automatically", _manual_continue_prompt("final_navigation"))

    def test_print_wrapper_survives_unicode_encode_error(self) -> None:
        original_print = builtins.print
        calls: list[tuple[object, ...]] = []

        def fake_print(*args, **kwargs):
            calls.append(args)
            if len(calls) == 1:
                raise UnicodeEncodeError("charmap", "Continue\u2060", 0, 1, "character maps to <undefined>")
            return None

        builtins.print = fake_print
        try:
            seek_apply_assist_module.print("[seek_apply_assist] continue_button_text: Continue\u2060", flush=True)
        finally:
            builtins.print = original_print

        self.assertGreaterEqual(len(calls), 2)
        self.assertIn("\\u2060", str(calls[-1][0]))

    def test_click_seek_continue_treats_role_requirements_transition_as_success(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            page = FakeContinuePage()
            success = click_seek_continue(page, Path(temp_dir), "cover_letter")
            self.assertTrue(success)
            self.assertIn("/apply/role-requirements", page.url)
            self.assertEqual(page.title(), "Answer employer questions | SEEK")
            self.assertIn("normal_click", page.locator_instance.click_attempts)

    def test_click_seek_continue_does_not_treat_choose_documents_stay_as_success(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            page = FakeContinuePage(transition_mode="choose_documents_never_leaves")
            success = click_seek_continue(page, Path(temp_dir), "document_step")
            self.assertFalse(success)
            self.assertEqual(page.title(), "Choose documents | SEEK")
            self.assertTrue((Path(temp_dir) / "document_step_continue_failed.png").exists())

    def test_wait_for_seek_step_succeeds_when_url_becomes_role_requirements(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            page = FakeContinuePage(transition_mode="role_requirements")
            success = wait_for_seek_step(page, "role_requirements", Path(temp_dir))
            self.assertTrue(success)

    def test_wait_for_seek_step_succeeds_when_questionnaire_fields_appear_without_role_requirements_url(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            page = FakeContinuePage(transition_mode="questionnaire_fields")
            success = wait_for_seek_step(page, "role_requirements", Path(temp_dir))
            self.assertTrue(success)
            self.assertEqual(page.url, "https://www.seek.co.nz/job/12345678/apply")
            self.assertGreaterEqual(page.select_count, 1)

    def test_wait_for_seek_step_fails_when_neither_role_requirements_url_nor_fields_appear(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            debug_dir = Path(temp_dir)
            page = FakeContinuePage(transition_mode="no_transition")
            success = wait_for_seek_step(page, "role_requirements", debug_dir, timeout_ms=10)
            self.assertFalse(success)
            self.assertTrue((debug_dir / "wait_for_seek_step_failed.png").exists())
            self.assertTrue((debug_dir / "wait_for_seek_step_failed.html").exists())
            self.assertTrue((debug_dir / "wait_for_seek_step_failed.json").exists())

    def test_wait_for_seek_step_does_not_treat_transient_text_alone_as_success(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            page = FakeContinuePage(transition_mode="transient_text_only")
            success = wait_for_seek_step(page, "role_requirements", Path(temp_dir), timeout_ms=10)
            self.assertFalse(success)

    def test_handle_seek_document_step_uses_fallback_when_choose_documents_stays_open(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            data_dir = Path(temp_dir) / "data"
            debug_dir = data_dir / "debug_apply_assist"
            data_dir.mkdir(parents=True, exist_ok=True)
            Path(data_dir, "applicant_profile.json").write_text(
                '{"automation":{"allow_no_resume":true,"allow_delete_old_seek_resumes":true}}',
                encoding="utf-8",
            )
            page = FakeContinuePage(transition_mode="choose_documents_stuck_then_fallback")
            adapter = FakeSeekAdapter(True, has_file_input=True, radio_available=True, resume_present=True, resume_filename="Example_Candidate_CV.docx")
            adapter._radio_checked = True
            result = handle_seek_document_step(page, adapter, Path(temp_dir) / "Example_Candidate_CV.docx", debug_dir, input_func=lambda: "")
            self.assertTrue(result.success)
            self.assertTrue(page.resume_toggle_clicked)
            self.assertIn("/apply/role-requirements", page.url)

    def test_handle_seek_document_step_pauses_when_choose_documents_never_leaves(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            debug_dir = Path(temp_dir)
            page = FakeContinuePage(transition_mode="choose_documents_never_leaves")
            adapter = FakeSeekAdapter(True, has_file_input=True, resume_present=True)
            result = handle_seek_document_step(page, adapter, debug_dir / "Example_Candidate_CV.docx", debug_dir, input_func=lambda: "")
            self.assertFalse(result.success)
            self.assertEqual(result.status, "paused_for_resume_upload_incomplete")
            self.assertTrue((debug_dir / "resume_upload_blocked_before_continue.json").exists())

    def test_handle_seek_document_step_blocks_fallback_when_allow_no_resume_false(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            page = FakeContinuePage(transition_mode="choose_documents_stuck_then_fallback")
            adapter = FakeSeekAdapter(True, has_file_input=True, resume_present=True)
            result = handle_seek_document_step(page, adapter, Path(temp_dir) / "Example_Candidate_CV.docx", Path(temp_dir), input_func=lambda: "")
            self.assertFalse(result.success)
            self.assertFalse(page.resume_toggle_clicked)

    def test_handle_seek_document_step_blocks_fallback_when_resume_already_verified(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            data_dir = Path(temp_dir) / "data"
            data_dir.mkdir(parents=True, exist_ok=True)
            (data_dir / "applicant_profile.json").write_text(
                '{"automation":{"allow_no_resume":true,"allow_delete_old_seek_resumes":true}}',
                encoding="utf-8",
            )
            page = FakeContinuePage(transition_mode="choose_documents_stuck_then_fallback")
            adapter = FakeSeekAdapter(True, has_file_input=True, resume_present=True, resume_filename="Example_Candidate_CV.docx")
            result = handle_seek_document_step(
                page,
                adapter,
                Path(temp_dir) / "Example_Candidate_CV.docx",
                data_dir / "debug_apply_assist",
                resume_upload_verified=True,
                input_func=lambda: "",
            )
            self.assertFalse(result.success)
            self.assertFalse(page.resume_toggle_clicked)

    def test_handle_seek_document_step_never_clicks_dont_include_when_resume_limit_modal_visible(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            page = FakeContinuePage(transition_mode="choose_documents_stuck_then_fallback")
            adapter = FakeSeekAdapter(True, has_file_input=True, resume_present=True, resume_limit_present=True)
            result = handle_seek_document_step(page, adapter, Path(temp_dir) / "Example_Candidate_CV.docx", Path(temp_dir), input_func=lambda: "")
            self.assertFalse(result.success)
            self.assertFalse(page.resume_toggle_clicked)

    def test_handle_seek_document_step_blocks_when_resume_upload_not_ready(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            page = FakeContinuePage(transition_mode="choose_documents_never_leaves")
            adapter = FakeSeekAdapter(
                True,
                has_file_input=True,
                radio_available=True,
                resume_present=True,
                resume_filename="Example_Candidate_CV.docx",
                loading_indicator_visible=True,
            )
            adapter._radio_checked = True
            result = handle_seek_document_step(page, adapter, Path(temp_dir) / "Example_Candidate_CV.docx", Path(temp_dir), input_func=lambda: "")
            self.assertFalse(result.success)
            self.assertEqual(result.status, "paused_for_resume_upload_incomplete")
            self.assertIn("/apply", page.url)
            self.assertTrue((Path(temp_dir) / "resume_upload_blocked_before_continue.json").exists())

    def test_handle_seek_document_step_reuploads_when_resume_selection_is_required(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            page = FakeContinuePage(transition_mode="choose_documents_never_leaves")
            page._visible_text = (
                "Choose documents Before you can continue with the application, "
                "please address the following issues: Resume - Please make a selection "
                "Continue Please select a resume"
            )
            adapter = FakeSeekAdapter(
                True,
                has_file_input=True,
                radio_available=True,
                resume_present=False,
                resume_filename="stale_saved_resume.docx",
            )
            result = handle_seek_document_step(page, adapter, Path(temp_dir) / "Example_Candidate_CV.docx", Path(temp_dir), input_func=lambda: "")
            self.assertFalse(result.success)
            self.assertEqual(adapter.upload_radio_clicks, 1)
            self.assertEqual(adapter.upload_attempts, 1)
            self.assertEqual(Path(adapter.uploaded_path or "").name, "Example_Candidate_CV.docx")

    def test_salary_expectation_formats_midpoint(self) -> None:
        self.assertEqual(format_salary_expectation(role_family="data_engineering", fit_score=85), "115k")
        self.assertEqual(format_salary_expectation(role_family="business_intelligence", fit_score=60), "100k")
        self.assertEqual(format_salary_expectation(role_family="unknown", fit_score=20), "105k")
        self.assertEqual(format_salary_expectation(role_family="data_engineering", fit_score=85, existing_text="110k"), "110k")

    def test_final_submit_is_never_clicked_and_selected_cv_path_is_used(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            root = Path(temp_dir)
            database, job_id, cv_path = self._build_seek_job(root)
            adapter = FakeSeekAdapter(True, final_label="Submit application")
            result = _assist_seek_apply(job_id, database, visible=True, adapter_factory=lambda visible: adapter, input_func=lambda: "")

            self.assertEqual(result.status, "prepared_for_manual_review")
            self.assertIn("stopped_before_submit", result.steps_completed)
            self.assertFalse(adapter.clicked_final_navigation)
            self.assertTrue(adapter.close_called)
            self.assertEqual(Path(adapter.uploaded_path).name, "Example_Candidate_CV.docx")
            self.assertEqual(adapter.salary_expectation, "95k")
            details = database.get_job_details(job_id)
            self.assertEqual(details["salary_expectation_text"], "95k")

    def test_review_page_with_no_resume_included_triggers_failure(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            root = Path(temp_dir)
            database, job_id, _ = self._build_seek_job(root)
            adapter = FakeSeekAdapter(True, final_label="Submit application")
            review_page = FakeContinuePage(transition_mode="role_requirements")
            adapter._page = review_page

            def final_navigation_label() -> str:
                review_page.url = "https://www.seek.co.nz/job/12345678/apply/review"
                review_page._title = "Review and submit | SEEK"
                review_page._visible_text = "Review and submit No resume included Cover letter Example cover letter tailored for SEEK."
                return "Submit application"

            adapter.final_navigation_label = final_navigation_label
            result = _assist_seek_apply(job_id, database, visible=True, adapter_factory=lambda visible: adapter, input_func=lambda: "")
            self.assertEqual(result.status, "failed")
            self.assertIn("SEEK review page shows no resume included", result.error)
            self.assertFalse(adapter.close_called)

    def test_successful_upload_persists_through_flow(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            root = Path(temp_dir)
            database, job_id, _ = self._build_seek_job(root)
            adapter = FakeSeekAdapter(True, final_label="Submit application")
            review_page = FakeContinuePage(transition_mode="role_requirements")
            adapter._page = review_page

            def final_navigation_label() -> str:
                review_page.url = "https://www.seek.co.nz/job/12345678/apply/review"
                review_page._title = "Review and submit | SEEK"
                review_page._visible_text = "Review and submit Documents included Example_Candidate_CV.docx Cover letter Example cover letter tailored for SEEK."
                return "Submit application"

            adapter.final_navigation_label = final_navigation_label
            result = _assist_seek_apply(job_id, database, visible=True, adapter_factory=lambda visible: adapter, input_func=lambda: "")
            self.assertEqual(result.status, "prepared_for_manual_review")
            self.assertTrue((Path(result.debug_dir) / "final_review_checklist.json").exists())

    def test_review_and_submit_label_clicks_through_to_real_review_page(self) -> None:
        class ReviewTransitionPage(FakeContinuePage):
            def _handle_locator_click(self, text: str) -> None:
                lowered = str(text or "").lower()
                if "continue" in lowered:
                    self.url = "https://www.seek.co.nz/job/12345678/apply/review"
                    self._title = "Review and submit | SEEK"
                    self._visible_text = (
                        "Review and submit Documents included Example_Candidate_CV.docx "
                        "You wrote a cover letter for this application You answered 1 out of 1"
                    )
                    return
                super()._handle_locator_click(text)

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            root = Path(temp_dir)
            database, job_id, _ = self._build_seek_job(root)
            adapter = FakeSeekAdapter(True, final_label="Review and submit")
            review_page = ReviewTransitionPage(transition_mode="role_requirements")
            review_page.url = "https://www.seek.co.nz/job/12345678/apply/role-requirements"
            review_page._title = "Answer employer questions | SEEK"
            review_page._visible_text = "Answer employer questions Continue"
            adapter._page = review_page
            result = _assist_seek_apply(job_id, database, visible=True, adapter_factory=lambda visible: adapter, input_func=lambda: "")
            self.assertEqual(result.status, "prepared_for_manual_review")
            self.assertIn("reached_manual_review", result.steps_completed)
            self.assertIn("stopped_before_submit", result.steps_completed)

    def test_advance_post_questionnaire_navigation_steps_through_profile_to_review(self) -> None:
        class SimpleBodyLocator:
            def __init__(self, page: "SimpleProfilePage") -> None:
                self.page = page

            def inner_text(self, timeout: int = 0) -> str:
                return self.page._visible_text

        class SimpleProfilePage:
            def __init__(self) -> None:
                self.url = "https://www.seek.co.nz/job/12345678/apply/profile"
                self._title = "Update SEEK Profile | SEEK"
                self._visible_text = "Update SEEK Profile Continue"

            def title(self) -> str:
                return self._title

            def locator(self, _selector: str) -> SimpleBodyLocator:
                return SimpleBodyLocator(self)

            def evaluate(self, _script: str) -> str:
                return self._visible_text

            def wait_for_timeout(self, _timeout_ms: int) -> None:
                return None

        class SimpleAdapter:
            def __init__(self, page: SimpleProfilePage) -> None:
                self._page = page

            def final_navigation_label(self) -> str:
                return "Continue"

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            page = SimpleProfilePage()
            adapter = SimpleAdapter(page)

            def fake_click_continue_step(
                _adapter: object,
                _debug_dir: Path,
                _step_name: str,
                *,
                input_func: object = input,
            ) -> bool:
                page.url = "https://www.seek.co.nz/job/12345678/apply/review"
                page._title = "Review and submit | SEEK"
                page._visible_text = (
                    "Review and submit Documents included Example_Candidate_CV.docx "
                    "You wrote a cover letter for this application Submit application"
                )
                return True

            with mock.patch("src.seek_apply_assist._click_continue_step", side_effect=fake_click_continue_step) as click_mock:
                reached_review_page, attempts = _advance_post_questionnaire_navigation(
                    adapter,
                    Path(temp_dir),
                    input_func=lambda: "",
                )

            self.assertTrue(reached_review_page)
            self.assertEqual(attempts, 1)
            click_mock.assert_called_once()

    def test_advance_post_questionnaire_navigation_recovers_choose_documents_bounce(self) -> None:
        class SimpleBodyLocator:
            def __init__(self, page: "BouncePage") -> None:
                self.page = page

            def inner_text(self, timeout: int = 0) -> str:
                return self.page._visible_text

        class BouncePage:
            def __init__(self) -> None:
                self.url = "https://www.seek.co.nz/job/12345678/apply/role-requirements"
                self._title = "Answer employer questions | SEEK"
                self._visible_text = "Answer employer questions Continue"

            def title(self) -> str:
                return self._title

            def locator(self, _selector: str) -> SimpleBodyLocator:
                return SimpleBodyLocator(self)

            def evaluate(self, _script: str) -> str:
                return self._visible_text

            def wait_for_timeout(self, _timeout_ms: int) -> None:
                return None

        class SimpleAdapter:
            def __init__(self, page: BouncePage) -> None:
                self._page = page

            def final_navigation_label(self) -> str:
                return "Continue"

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            page = BouncePage()
            adapter = SimpleAdapter(page)
            selected_cv_path = Path(temp_dir) / "Example_Candidate_CV.docx"

            def fake_click_continue_step(
                _adapter: object,
                _debug_dir: Path,
                _step_name: str,
                *,
                input_func: object = input,
            ) -> bool:
                page.url = "https://www.seek.co.nz/job/12345678/apply"
                page._title = "Choose documents | SEEK"
                page._visible_text = (
                    "Choose documents Before you can continue with the application, "
                    "please address the following issues: Resume - Please make a selection Continue"
                )
                return True

            def fake_handle_seek_document_step(
                _page: object,
                _adapter: object,
                _selected_cv_path: Path,
                _debug_dir: Path,
                *,
                resume_upload_verified: bool = False,
                input_func: object = input,
            ) -> object:
                page.url = "https://www.seek.co.nz/job/12345678/apply/review"
                page._title = "Review and submit | SEEK"
                page._visible_text = "Review and submit Documents included Example_Candidate_CV.docx"
                return seek_apply_assist_module.DocumentStepResult(True)

            with (
                mock.patch("src.seek_apply_assist._click_continue_step", side_effect=fake_click_continue_step) as click_mock,
                mock.patch("src.seek_apply_assist.handle_seek_document_step", side_effect=fake_handle_seek_document_step) as document_mock,
            ):
                reached_review_page, attempts = _advance_post_questionnaire_navigation(
                    adapter,
                    Path(temp_dir),
                    selected_cv_path=selected_cv_path,
                    resume_upload_verified=True,
                    input_func=lambda: "",
                )

            self.assertTrue(reached_review_page)
            self.assertEqual(attempts, 1)
            click_mock.assert_called_once()
            document_mock.assert_called_once()

    def test_advance_post_questionnaire_navigation_waits_for_loading_shell(self) -> None:
        class LoadingShellPage:
            def __init__(self) -> None:
                self.url = "https://www.seek.co.nz/job/12345678/apply/role-requirements"
                self._title = "Answer employer questions | SEEK"
                self._visible_text = "Answer employer questions Continue"
                self.wait_calls = 0

            def title(self) -> str:
                return self._title

            def locator(self, _selector: str) -> object:
                page = self

                class BodyLocator:
                    def inner_text(self, timeout: int = 0) -> str:
                        return page._visible_text

                return BodyLocator()

            def evaluate(self, _script: str) -> str:
                return self._visible_text

            def wait_for_timeout(self, _timeout_ms: int) -> None:
                self.wait_calls += 1
                if self.wait_calls >= 2:
                    self.url = "https://www.seek.co.nz/job/12345678/apply/review"
                    self._title = "Review and submit | SEEK"
                    self._visible_text = "Review and submit Documents included Example_Candidate_CV.docx Submit application"

        class SimpleAdapter:
            def __init__(self, page: LoadingShellPage) -> None:
                self._page = page

            def final_navigation_label(self) -> str:
                return "Continue"

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            page = LoadingShellPage()
            adapter = SimpleAdapter(page)

            def fake_click_continue_step(
                _adapter: object,
                _debug_dir: Path,
                _step_name: str,
                *,
                input_func: object = input,
            ) -> bool:
                page.url = "https://www.seek.co.nz/job/12345678/apply/role-requirements?submitted=1"
                page._title = "SEEK"
                page._visible_text = ""
                return True

            with mock.patch("src.seek_apply_assist._click_continue_step", side_effect=fake_click_continue_step):
                reached_review_page, attempts = _advance_post_questionnaire_navigation(
                    adapter,
                    Path(temp_dir),
                    input_func=lambda: "",
                )

            self.assertTrue(reached_review_page)
            self.assertEqual(attempts, 1)

    def test_click_seek_continue_detects_final_navigation_progress_after_click(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            page = FakeContinuePage(transition_mode="role_requirements")

            success = click_seek_continue(
                page,
                Path(temp_dir),
                "final_navigation",
                save_failure_artifacts=False,
            )

            self.assertTrue(success)
            self.assertIn("/apply/role-requirements", page.url)

    def test_click_seek_continue_does_not_short_circuit_questionnaire_when_still_on_same_page(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            page = FakeContinuePage(transition_mode="no_transition")
            page.url = "https://www.seek.co.nz/job/12345678/apply"
            page._title = "Answer employer questions | SEEK"
            page._visible_text = "Answer employer questions Continue"

            success = click_seek_continue(
                page,
                Path(temp_dir),
                "questionnaire",
                save_failure_artifacts=False,
            )

            self.assertFalse(success)

    def test_resolve_seek_job_open_urls_prefers_real_seek_job_url_over_email_redirect(self) -> None:
        preferred_url, fallback_url = _resolve_seek_job_open_urls(
            {
                "url": "https://email.s.seek.co.nz/uni/ss/c/some-tracking-link",
                "canonical_url": "https://email.s.seek.co.nz/uni/ss/c/some-tracking-link",
                "source_page_text": (
                    "Apply now via https://nz.seek.com/job/92245186?tracking=T123 and "
                    "backup https://www.seek.com.au/job/12345678"
                ),
            }
        )

        self.assertEqual(preferred_url, "https://nz.seek.com/job/92245186?tracking=T123")
        self.assertEqual(fallback_url, "https://email.s.seek.co.nz/uni/ss/c/some-tracking-link")

    def test_assist_seek_apply_recovers_when_apply_flow_is_already_open(self) -> None:
        class ApplyFlowAlreadyOpenAdapter(FakeSeekAdapter):
            def open_job(self, url: str, fallback_url: str | None = None) -> None:
                self.url = "https://www.seek.co.nz/job/12345678/apply"
                page = FakeContinuePage(transition_mode="role_requirements")
                page.url = self.url
                page._title = "Choose documents | SEEK"
                page._visible_text = "Choose documents Continue Upload a resume"
                self._page = page

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            root = Path(temp_dir)
            database, job_id, _ = self._build_seek_job(root)
            adapter = ApplyFlowAlreadyOpenAdapter(True, quick_apply_error="Could not find SEEK Quick apply button.", radio_available=True)
            result = _assist_seek_apply(job_id, database, visible=True, adapter_factory=lambda visible: adapter, input_func=lambda: "")

            self.assertEqual(result.status, "prepared_for_manual_review")
            self.assertIn("quick_apply_already_open", result.steps_completed)

    def test_review_page_cover_letter_positive_phrase_counts_as_included(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            root = Path(temp_dir)
            database, job_id, _ = self._build_seek_job(root)
            adapter = FakeSeekAdapter(True, final_label="Submit application")
            review_page = FakeContinuePage(transition_mode="role_requirements")
            adapter._page = review_page

            def final_navigation_label() -> str:
                review_page.url = "https://www.seek.co.nz/job/12345678/apply/review"
                review_page._title = "Review and submit | SEEK"
                review_page._visible_text = (
                    "Review and submit Documents included Example_Candidate_CV.docx "
                    "Cover letter You wrote a cover letter for this application "
                    "Employer questions You answered 1 out of 1"
                )
                return "Submit application"

            adapter.final_navigation_label = final_navigation_label
            result = _assist_seek_apply(job_id, database, visible=True, adapter_factory=lambda visible: adapter, input_func=lambda: "")
            self.assertEqual(result.status, "prepared_for_manual_review")

    def test_review_page_missing_cover_letter_triggers_failure(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            root = Path(temp_dir)
            database, job_id, _ = self._build_seek_job(root)
            adapter = FakeSeekAdapter(True, final_label="Submit application")
            review_page = FakeContinuePage(transition_mode="role_requirements")
            adapter._page = review_page

            def final_navigation_label() -> str:
                review_page.url = "https://www.seek.co.nz/job/12345678/apply/review"
                review_page._title = "Review and submit | SEEK"
                review_page._visible_text = "Review and submit Documents included Example_Candidate_CV.docx"
                return "Submit application"

            adapter.final_navigation_label = final_navigation_label
            result = _assist_seek_apply(job_id, database, visible=True, adapter_factory=lambda visible: adapter, input_func=lambda: "")
            self.assertEqual(result.status, "failed")
            self.assertIn("Final SEEK review checklist failed", result.error)
            checklist_path = Path(result.debug_dir) / "final_review_checklist.json"
            self.assertTrue(checklist_path.exists())
            self.assertIn('"cover_letter_included": false', checklist_path.read_text(encoding="utf-8"))

    def test_verification_failure_artifacts_preserve_root_reason_when_screenshot_fails(self) -> None:
        class ScreenshotFailPage:
            def screenshot(self, *args: object, **kwargs: object) -> None:
                raise RuntimeError("Timeout 3000ms exceeded")

            def content(self) -> str:
                return "<html><body>Choose documents</body></html>"

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            debug_dir = Path(temp_dir)
            screenshot_path, html_path, json_path, capture_meta = _save_verification_failure_artifacts(
                debug_dir,
                ScreenshotFailPage(),
                stem="resume_upload_blocked_before_continue",
                payload={
                    "root_failure_reason": "CV filename appeared, but SEEK was still processing the upload. Stopped before Continue.",
                },
            )
            payload = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(screenshot_path.name, "resume_upload_blocked_before_continue.png")
            self.assertEqual(html_path.name, "resume_upload_blocked_before_continue.html")
            self.assertEqual(
                payload["root_failure_reason"],
                "CV filename appeared, but SEEK was still processing the upload. Stopped before Continue.",
            )
            self.assertFalse(payload["screenshot_saved"])
            self.assertTrue(payload["html_saved"])
            self.assertTrue(payload["json_saved"])
            self.assertTrue(capture_meta["evidence_capture_errors"])

    def test_final_review_checklist_requires_all_items_true(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            root = Path(temp_dir)
            database, job_id, _ = self._build_seek_job(root)
            adapter = FakeSeekAdapter(True, final_label="Submit application")
            review_page = FakeContinuePage(transition_mode="role_requirements")
            adapter._page = review_page

            def final_navigation_label() -> str:
                review_page.url = "https://www.seek.co.nz/job/12345678/apply/review"
                review_page._title = "Review and submit | SEEK"
                review_page._visible_text = "Review and submit Documents included Example_Candidate_CV.docx Cover letter Example cover letter tailored for SEEK."
                return "Submit application"

            adapter.final_navigation_label = final_navigation_label
            result = _assist_seek_apply(job_id, database, visible=True, adapter_factory=lambda visible: adapter, input_func=lambda: "")
            self.assertEqual(result.status, "prepared_for_manual_review")
            checklist_text = (Path(result.debug_dir) / "final_review_checklist.json").read_text(encoding="utf-8")
            self.assertIn('"resume_included": true', checklist_text)
            self.assertIn('"cover_letter_included": true', checklist_text)
            self.assertIn('"questions_answered": true', checklist_text)
            self.assertIn('"no_validation_errors": true', checklist_text)
            self.assertIn('"final_submit_not_clicked": true', checklist_text)

    def test_missing_cover_letter_fails_safely(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            root = Path(temp_dir)
            database, job_id, _ = self._build_seek_job(root, include_cover_letter=False)
            result = _assist_seek_apply(job_id, database, visible=True, adapter_factory=lambda visible: FakeSeekAdapter(visible))
            self.assertEqual(result.status, "failed")
            self.assertIn("Missing cover letter", result.error)

    def test_incomplete_resume_upload_returns_paused_for_resume_upload_incomplete(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            root = Path(temp_dir)
            database, job_id, _ = self._build_seek_job(root)
            adapter = FakeSeekAdapter(
                True,
                has_file_input=True,
                radio_available=True,
                resume_present=True,
                resume_filename="Example_Candidate_CV.docx",
                loading_indicator_visible=True,
            )
            adapter._radio_checked = True
            result = _assist_seek_apply(job_id, database, visible=True, adapter_factory=lambda visible: adapter, input_func=lambda: "")
            self.assertEqual(result.status, "paused_for_resume_upload_incomplete")
            self.assertNotIn("clicked_continue_1", result.steps_completed)

    def test_resume_existing_detection_allows_workflow_to_continue(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            root = Path(temp_dir)
            database, job_id, _ = self._build_seek_job(root)
            adapter = FakeSeekAdapter(True, final_label="Submit application", has_file_input=False, resume_present=True)
            result = _assist_seek_apply(job_id, database, visible=True, adapter_factory=lambda visible: adapter, input_func=lambda: "")
            self.assertEqual(result.status, "prepared_for_manual_review")
            self.assertIn("resume_existing_detected", result.steps_completed)
            self.assertTrue(adapter.close_called)

    def test_resume_radio_is_selected_before_upload_when_present(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            root = Path(temp_dir)
            database, job_id, cv_path = self._build_seek_job(root)
            adapter = FakeSeekAdapter(True, has_file_input=False, radio_available=True)
            result = _assist_seek_apply(job_id, database, visible=True, adapter_factory=lambda visible: adapter, input_func=lambda: "")
            self.assertEqual(result.status, "prepared_for_manual_review")
            self.assertTrue(adapter._radio_checked)
            self.assertTrue(adapter.saved_selected_screenshot)
            self.assertTrue(adapter.close_called)
            self.assertEqual(Path(adapter.uploaded_path).name, "Example_Candidate_CV.docx")

    def test_resume_upload_only_mode_stops_before_continue(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            root = Path(temp_dir)
            database, job_id, _ = self._build_seek_job(root)
            adapter = FakeSeekAdapter(True, has_file_input=False, radio_available=True)
            adapter._page = FakeContinuePage(transition_mode="role_requirements")
            result = _assist_seek_apply(
                job_id,
                database,
                visible=True,
                adapter_factory=lambda visible: adapter,
                input_func=lambda: "",
                resume_upload_only=True,
            )
            self.assertEqual(result.status, "resume_upload_only_ready")
            self.assertIn("resume_upload_only_verified", result.steps_completed)
            self.assertNotIn("selected_cover_letter_write", result.steps_completed)
            self.assertFalse(adapter.cover_continue_clicked)
            self.assertFalse(adapter.close_called)

    def test_resume_upload_only_fails_if_dont_include_resume_selected(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            root = Path(temp_dir)
            database, job_id, _ = self._build_seek_job(root)
            adapter = FakeSeekAdapter(
                True,
                has_file_input=True,
                radio_available=True,
                dont_include_checked=True,
                resume_filename="Example_Candidate_CV.docx",
                resume_present=True,
            )
            adapter._page = FakeContinuePage(transition_mode="role_requirements")
            result = _assist_seek_apply(
                job_id,
                database,
                visible=True,
                adapter_factory=lambda visible: adapter,
                input_func=lambda: "",
                resume_upload_only=True,
            )
            self.assertEqual(result.status, "resume_upload_only_ready")
            self.assertIn("resume_upload_only_verified", result.steps_completed)
            self.assertFalse(adapter.close_called)

    def test_resume_upload_radio_detected_by_data_testid(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            root = Path(temp_dir)
            adapter = FakeSeekAdapter(True, has_file_input=False, radio_available=True)
            result = handle_resume_upload(adapter, root / "Example_Candidate_CV.docx", root / "data" / "debug_apply_assist")
            self.assertEqual(result.status, "resume_uploaded")
            self.assertTrue(adapter._radio_checked)

    def test_choose_resume_limit_delete_candidate_prefers_oldest_dated_resume(self) -> None:
        options = [
            "Please select a resumÃ©",
            "10/5/26 - selected_cv.docx",
            "8/5/26 - older_resume.docx",
            "11/5/26 - newer_resume.docx",
        ]
        candidate = _choose_resume_limit_delete_candidate(options, protected_filename="Example_Candidate_CV.docx")
        self.assertEqual(candidate, "8/5/26 - older_resume.docx")

    def test_choose_resume_limit_delete_candidate_falls_back_to_last_option_when_dates_missing(self) -> None:
        options = [
            "Please select a resumÃ©",
            "alpha_resume.docx",
            "beta_resume.docx",
        ]
        candidate = _choose_resume_limit_delete_candidate(options, protected_filename="Example_Candidate_CV.docx")
        self.assertEqual(candidate, "beta_resume.docx")

    def test_resume_limit_flow_does_not_delete_when_profile_disables_it(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            root = Path(temp_dir)
            (root / "data").mkdir(parents=True, exist_ok=True)
            (root / "data" / "applicant_profile.json").write_text(
                """
{
  "automation": {
    "allow_no_resume": false,
    "allow_delete_old_seek_resumes": false
  }
}
                """.strip(),
                encoding="utf-8",
            )
            adapter = FakeSeekAdapter(
                True,
                has_file_input=False,
                radio_available=True,
                resume_limit_present=True,
                resume_limit_options=["Please select a resumÃ©", "10/5/26 - selected_cv.docx"],
                upload_succeeds=False,
            )
            result = handle_resume_upload(adapter, root / "Example_Candidate_CV.docx", root / "data" / "debug_apply_assist")
            self.assertEqual(result.status, "paused_for_manual_resume_upload")
            self.assertFalse(adapter.resume_limit_delete_clicked)

    def test_resume_limit_flow_retries_upload_after_delete(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            root = Path(temp_dir)
            unique_cv_path = root / "Example_Candidate_CV_20260513_1845.docx"
            adapter = FakeSeekAdapter(
                True,
                has_file_input=False,
                radio_available=True,
                resume_limit_present=True,
                resume_limit_options=[
                    "Please select a resumÃ©",
                    "10/5/26 - current_resume.docx",
                    "8/5/26 - old_resume.docx",
                ],
                upload_succeeds=False,
            )
            result = handle_resume_upload(adapter, unique_cv_path, root / "data" / "debug_apply_assist")
            self.assertEqual(result.status, "resume_uploaded")
            self.assertTrue(adapter.resume_limit_delete_clicked)
            self.assertTrue(adapter.resume_limit_delete_confirmed)
            self.assertEqual(adapter.resume_limit_selected_option, "8/5/26 - old_resume.docx")
            self.assertGreaterEqual(adapter.upload_attempts, 1)
            self.assertEqual(Path(adapter.uploaded_path).name, unique_cv_path.name)

    def test_existing_resume_is_deleted_before_upload_when_change_flow_is_available(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            root = Path(temp_dir)
            unique_cv_path = root / "Example_Candidate_CV_20260514_1900.docx"
            adapter = FakeSeekAdapter(
                True,
                has_file_input=False,
                radio_available=True,
                change_radio_available=True,
                existing_resume_options=[
                    "Please select a resumÃƒÂ©",
                    "12/5/26 - Example_Candidate_CV.docx",
                    "8/5/24 - Example Candidate CV 2024.docx",
                ],
                upload_succeeds=True,
            )
            result = handle_resume_upload(adapter, unique_cv_path, root / "data" / "debug_apply_assist")
            self.assertEqual(result.status, "resume_uploaded")
            self.assertTrue(adapter.existing_resume_delete_clicked)
            self.assertEqual(adapter.existing_resume_selected_option, "8/5/24 - Example Candidate CV 2024.docx")
            self.assertEqual(Path(adapter.uploaded_path).name, unique_cv_path.name)

    def test_existing_resume_flow_pauses_when_delete_is_disallowed(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            root = Path(temp_dir)
            (root / "data").mkdir(parents=True, exist_ok=True)
            (root / "data" / "applicant_profile.json").write_text(
                """
{
  "automation": {
    "allow_no_resume": false,
    "allow_delete_old_seek_resumes": false
  }
}
                """.strip(),
                encoding="utf-8",
            )
            adapter = FakeSeekAdapter(
                True,
                has_file_input=False,
                radio_available=True,
                change_radio_available=True,
                existing_resume_options=[
                    "Please select a resumÃƒÂ©",
                    "12/5/26 - Example_Candidate_CV.docx",
                ],
                upload_succeeds=True,
            )
            result = handle_resume_upload(adapter, root / "Example_Candidate_CV.docx", root / "data" / "debug_apply_assist")
            self.assertEqual(result.status, "paused_for_manual_resume_upload")
            self.assertFalse(adapter.existing_resume_delete_clicked)

    def test_resume_upload_verifies_filename_before_continuing(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            root = Path(temp_dir)
            adapter = FakeSeekAdapter(True, has_file_input=True, upload_succeeds=True)
            result = handle_resume_upload(adapter, root / "Example_Candidate_CV.docx", root / "data" / "debug_apply_assist")
            self.assertEqual(result.status, "resume_uploaded")
            self.assertTrue(adapter.detect_visible_resume_filename().endswith(".docx"))

    def test_resume_upload_complete_wait_does_not_succeed_while_loading_indicator_visible(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            adapter = FakeSeekAdapter(
                True,
                has_file_input=True,
                radio_available=True,
                upload_succeeds=True,
                loading_indicator_visible=True,
            )
            adapter._radio_checked = True
            adapter._resume_filename = "Example_Candidate_CV.docx"
            filename = wait_for_resume_upload_complete(adapter, Path(temp_dir), "Example_Candidate_CV.docx", timeout_ms=1000)
            self.assertEqual(filename, "")

    def test_resume_upload_complete_wait_succeeds_when_loading_clears_and_filename_stabilises(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            adapter = FakeSeekAdapter(
                True,
                has_file_input=True,
                radio_available=True,
                upload_succeeds=True,
                loading_polls_before_complete=2,
            )
            adapter._radio_checked = True
            adapter._resume_filename = "Example_Candidate_CV.docx"
            filename = wait_for_resume_upload_complete(adapter, Path(temp_dir), "Example_Candidate_CV.docx", timeout_ms=4000)
            self.assertEqual(filename, "Example_Candidate_CV.docx")

    def test_resume_upload_complete_wait_fails_when_dont_include_is_selected(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            adapter = FakeSeekAdapter(
                True,
                has_file_input=True,
                radio_available=True,
                upload_succeeds=True,
                dont_include_checked=True,
            )
            adapter._resume_filename = "Example_Candidate_CV.docx"
            filename = wait_for_resume_upload_complete(adapter, Path(temp_dir), "Example_Candidate_CV.docx", timeout_ms=1000)
            self.assertEqual(filename, "")

    def test_resume_upload_complete_wait_succeeds_with_upload_radio_and_stable_filename(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            adapter = FakeSeekAdapter(True, has_file_input=True, radio_available=True, upload_succeeds=True)
            adapter._radio_checked = True
            adapter._resume_filename = "Example_Candidate_CV.docx"
            filename = wait_for_resume_upload_complete(adapter, Path(temp_dir), "Example_Candidate_CV.docx", timeout_ms=2500)
            self.assertEqual(filename, "Example_Candidate_CV.docx")

    def test_verify_resume_ready_to_continue_blocks_when_loading_true(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            adapter = FakeSeekAdapter(
                True,
                has_file_input=True,
                radio_available=True,
                upload_succeeds=True,
                loading_indicator_visible=True,
            )
            adapter._radio_checked = True
            adapter._resume_filename = "Example_Candidate_CV.docx"
            ready, payload = verify_resume_ready_to_continue(adapter, Path(temp_dir), expected_filename="Example_Candidate_CV.docx")
            self.assertFalse(ready)
            self.assertTrue(payload["filename_visible"])
            self.assertTrue(payload["loading_indicator_visible"])

    def test_verify_resume_ready_to_continue_accepts_selected_existing_resume_change_state(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            adapter = FakeSeekAdapter(
                True,
                has_file_input=True,
                radio_available=True,
                change_radio_available=True,
                existing_resume_options=["2/7/26 - Example_Candidate_CV.docx"],
                resume_present=True,
            )
            adapter._resume_filename = "2/7/26 - Example_Candidate_CV.docx"
            ready, payload = verify_resume_ready_to_continue(
                adapter,
                Path(temp_dir),
                expected_filename="2/7/26 - Example_Candidate_CV.docx",
            )
            self.assertTrue(ready)
            self.assertTrue(payload["filename_visible"])
            self.assertEqual(payload["selected_resume_method_value"], "change")
            self.assertTrue(payload["change_resume_checked"])

    def test_resume_upload_stuck_processing_triggers_toggle_recovery(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            adapter = FakeSeekAdapter(
                True,
                has_file_input=True,
                radio_available=True,
                upload_succeeds=True,
                loading_indicator_visible=True,
                recover_after_stuck_toggle=True,
            )
            adapter._radio_checked = True
            adapter._resume_filename = "Example_Candidate_CV.docx"
            filename = wait_for_resume_upload_complete(
                adapter,
                Path(temp_dir),
                "Example_Candidate_CV.docx",
                timeout_ms=6000,
                stuck_processing_threshold_ms=1000,
                recovery_window_ms=2000,
            )
            self.assertEqual(filename, "Example_Candidate_CV.docx")
            self.assertEqual(adapter.none_radio_clicks, 1)
            self.assertGreaterEqual(adapter.upload_radio_clicks, 1)

    def test_resume_upload_toggle_recovery_only_runs_once(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            adapter = FakeSeekAdapter(
                True,
                has_file_input=True,
                radio_available=True,
                upload_succeeds=True,
                loading_indicator_visible=True,
                recover_after_stuck_toggle=False,
                loading_stays_after_recovery=True,
            )
            adapter._radio_checked = True
            adapter._resume_filename = "Example_Candidate_CV.docx"
            filename = wait_for_resume_upload_complete(
                adapter,
                Path(temp_dir),
                "Example_Candidate_CV.docx",
                timeout_ms=5000,
                stuck_processing_threshold_ms=1000,
                recovery_window_ms=1500,
            )
            self.assertEqual(filename, "")
            self.assertEqual(adapter.none_radio_clicks, 1)

    def test_resume_upload_toggle_recovery_reuploads_if_filename_disappears(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            root = Path(temp_dir)
            selected_cv = root / "Example_Candidate_CV_20260513_1900.docx"
            adapter = FakeSeekAdapter(
                True,
                has_file_input=True,
                radio_available=True,
                upload_succeeds=True,
                loading_indicator_visible=True,
                recover_after_stuck_toggle=True,
                filename_disappears_after_toggle=True,
            )
            adapter._radio_checked = True
            adapter._resume_filename = "Example_Candidate_CV_20260513_1900.docx"
            filename = wait_for_resume_upload_complete(
                adapter,
                root,
                "Example_Candidate_CV_20260513_1900.docx",
                timeout_ms=6500,
                selected_cv_path=selected_cv,
                stuck_processing_threshold_ms=1000,
                recovery_window_ms=2000,
            )
            self.assertEqual(filename, "Example_Candidate_CV_20260513_1900.docx")
            self.assertEqual(adapter.recovery_reupload_count, 1)

    def test_resume_upload_toggle_recovery_fails_safely_if_loading_persists(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            adapter = FakeSeekAdapter(
                True,
                has_file_input=True,
                radio_available=True,
                upload_succeeds=True,
                loading_indicator_visible=True,
                recover_after_stuck_toggle=True,
                loading_stays_after_recovery=True,
            )
            adapter._radio_checked = True
            adapter._resume_filename = "Example_Candidate_CV.docx"
            filename = wait_for_resume_upload_complete(
                adapter,
                Path(temp_dir),
                "Example_Candidate_CV.docx",
                timeout_ms=5000,
                stuck_processing_threshold_ms=1000,
                recovery_window_ms=1500,
            )
            self.assertEqual(filename, "")
            self.assertTrue((Path(temp_dir) / "resume_upload_complete_timeout.json").exists())

    def test_resume_section_snapshot_records_loading_candidate_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            root = Path(temp_dir)
            profile = json.loads(json.dumps(DEFAULT_APPLICANT_PROFILE))
            profile.setdefault("automation", {})["resume_debug_dom_snapshots"] = True
            (root / "applicant_profile.json").write_text(json.dumps(profile), encoding="utf-8")
            debug_dir = root / "debug_apply_assist"
            adapter = FakeSeekAdapter(
                True,
                radio_available=True,
                resume_filename="Example_Candidate_CV.docx",
                loading_indicator_visible=True,
            )
            adapter._radio_checked = True
            _save_resume_section_dom_snapshot(debug_dir, "after_set_input_files", adapter, expected_filename="Example_Candidate_CV.docx")
            state = json.loads((debug_dir / "resume_section_after_set_input_files" / "resume_section_state.json").read_text(encoding="utf-8"))
            self.assertTrue(state["progress_candidates"])
            self.assertTrue(state["progress_candidates"][0]["visible"])

    def test_resume_section_snapshot_writes_json_when_screenshot_fails(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            root = Path(temp_dir)
            profile = json.loads(json.dumps(DEFAULT_APPLICANT_PROFILE))
            profile.setdefault("automation", {})["resume_debug_dom_snapshots"] = True
            (root / "applicant_profile.json").write_text(json.dumps(profile), encoding="utf-8")
            debug_dir = root / "debug_apply_assist"
            adapter = FakeSeekAdapter(
                True,
                radio_available=True,
                resume_filename="Example_Candidate_CV.docx",
                resume_section_screenshot_fails=True,
            )
            adapter._radio_checked = True
            _save_resume_section_dom_snapshot(debug_dir, "before_upload", adapter, expected_filename="Example_Candidate_CV.docx")
            state_path = debug_dir / "resume_section_before_upload" / "resume_section_state.json"
            self.assertTrue(state_path.exists())
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertFalse(state["screenshot_saved"])
            self.assertTrue(state["html_saved"])
            self.assertTrue(state["text_saved"])
            self.assertTrue(any("screenshot:" in error for error in state["snapshot_capture_errors"]))

    def test_resume_upload_complete_timeout_json_keeps_root_failure_reason(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            root = Path(temp_dir)
            profile = json.loads(json.dumps(DEFAULT_APPLICANT_PROFILE))
            profile.setdefault("automation", {})["resume_debug_dom_snapshots"] = True
            (root / "applicant_profile.json").write_text(json.dumps(profile), encoding="utf-8")
            adapter = FakeSeekAdapter(
                True,
                has_file_input=True,
                radio_available=True,
                upload_succeeds=True,
                loading_indicator_visible=True,
            )
            adapter._page = FakeContinuePage(transition_mode="choose_documents_never_leaves")
            adapter._radio_checked = True
            adapter._resume_filename = "Example_Candidate_CV.docx"
            filename = wait_for_resume_upload_complete(
                adapter,
                root / "debug_apply_assist",
                "Example_Candidate_CV.docx",
                timeout_ms=2500,
                stuck_processing_threshold_ms=1000,
                recovery_window_ms=1000,
            )
            self.assertEqual(filename, "")
            payload = json.loads((root / "debug_apply_assist" / "resume_upload_complete_timeout.json").read_text(encoding="utf-8"))
            self.assertEqual(
                payload["root_failure_reason"],
                "CV filename appeared, but SEEK was still processing the upload. Stopped before Continue.",
            )
            self.assertIn("dom_diagnostics", payload)
            self.assertTrue((root / "debug_apply_assist" / "dom_diagnostics" / "codex_prompt.txt").exists())

    def test_capture_local_dom_diagnostics_writes_html_json_and_prompt(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            root = Path(temp_dir)
            adapter = FakeSeekAdapter(
                True,
                radio_available=True,
                resume_filename="Example_Candidate_CV.docx",
                loading_indicator_visible=True,
            )
            adapter._radio_checked = True
            result = capture_local_dom_diagnostics(
                page=object(),
                locator=None,
                failure_type="resume_upload_incomplete",
                debug_dir=root,
                adapter=adapter,
                expected_filename="Example_Candidate_CV.docx",
                root_failure_reason="CV filename appeared, but SEEK was still processing the upload. Stopped before Continue.",
            )
            state = json.loads((root / "dom_diagnostics" / "local_state.json").read_text(encoding="utf-8"))
            prompt = (root / "dom_diagnostics" / "codex_prompt.txt").read_text(encoding="utf-8")
            self.assertTrue((root / "dom_diagnostics" / "local_container.html").exists())
            self.assertEqual(state["local_spinner_count"], 1)
            self.assertIn("Determine whether the local spinner", prompt)
            self.assertEqual(result["codex_prompt"], str(root / "dom_diagnostics" / "codex_prompt.txt"))

    def test_capture_local_dom_diagnostics_survives_screenshot_failure(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            root = Path(temp_dir)
            adapter = FakeSeekAdapter(
                True,
                radio_available=True,
                resume_filename="Example_Candidate_CV.docx",
                resume_section_screenshot_fails=True,
            )
            adapter._radio_checked = True
            result = capture_local_dom_diagnostics(
                page=object(),
                locator=None,
                failure_type="resume_upload_incomplete",
                debug_dir=root,
                adapter=adapter,
                expected_filename="Example_Candidate_CV.docx",
                root_failure_reason="CV filename appeared, but SEEK was still processing the upload. Stopped before Continue.",
            )
            state = json.loads((root / "dom_diagnostics" / "local_state.json").read_text(encoding="utf-8"))
            self.assertTrue((root / "dom_diagnostics" / "local_container.html").exists())
            self.assertTrue((root / "dom_diagnostics" / "local_container.txt").exists())
            self.assertTrue(any("cropped_screenshot:" in error for error in state["capture_errors"]))
            self.assertFalse(result["cropped_screenshot_saved"])

    def test_global_spinner_outside_filename_container_does_not_block_upload_completion(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            adapter = FakeSeekAdapter(
                True,
                has_file_input=True,
                radio_available=True,
                upload_succeeds=True,
                loading_indicator_visible=False,
                global_spinner_visible=True,
            )
            adapter._radio_checked = True
            adapter._resume_filename = "Example_Candidate_CV.docx"
            filename = wait_for_resume_upload_complete(
                adapter,
                Path(temp_dir),
                "Example_Candidate_CV.docx",
                timeout_ms=2500,
            )
            self.assertEqual(filename, "Example_Candidate_CV.docx")

    def test_local_spinner_inside_filename_container_blocks_upload_completion(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            adapter = FakeSeekAdapter(
                True,
                has_file_input=True,
                radio_available=True,
                upload_succeeds=True,
                loading_indicator_visible=True,
            )
            adapter._radio_checked = True
            adapter._resume_filename = "Example_Candidate_CV.docx"
            filename = wait_for_resume_upload_complete(
                adapter,
                Path(temp_dir),
                "Example_Candidate_CV.docx",
                timeout_ms=2500,
                stuck_processing_threshold_ms=1000,
                recovery_window_ms=1000,
            )
            self.assertEqual(filename, "")

    def test_filename_stable_with_upload_selected_allows_continue(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            adapter = FakeSeekAdapter(
                True,
                has_file_input=True,
                radio_available=True,
                upload_succeeds=True,
                loading_indicator_visible=False,
            )
            adapter._radio_checked = True
            adapter._resume_filename = "Example_Candidate_CV.docx"
            ready, payload = verify_resume_ready_to_continue(
                adapter,
                Path(temp_dir),
                expected_filename="Example_Candidate_CV.docx",
            )
            self.assertTrue(ready)
            self.assertTrue(payload["filename_visible"])
            self.assertFalse(payload["loading_indicator_visible"])

    def test_resume_upload_uses_file_chooser_when_available(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            root = Path(temp_dir)
            adapter = FakeSeekAdapter(
                True,
                has_file_input=False,
                radio_available=True,
                button_available=True,
                file_chooser_available=True,
                upload_succeeds=True,
            )
            result = handle_resume_upload(adapter, root / "Example_Candidate_CV.docx", root / "data" / "debug_apply_assist")
            self.assertEqual(result.status, "resume_uploaded")
            self.assertEqual(adapter.wait_for_file_input_calls, 0)
            self.assertEqual(Path(adapter.uploaded_path).name, "Example_Candidate_CV.docx")

    def test_resume_upload_clicks_upload_before_waiting_for_file_input(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            root = Path(temp_dir)
            adapter = FakeSeekAdapter(
                True,
                has_file_input=False,
                radio_available=True,
                button_available=True,
                file_input_after_upload_click=True,
                upload_succeeds=True,
            )
            result = handle_resume_upload(adapter, root / "Example_Candidate_CV.docx", root / "data" / "debug_apply_assist")
            self.assertEqual(result.status, "resume_uploaded")
            self.assertGreaterEqual(adapter.upload_button_clicks, 1)
            self.assertGreaterEqual(adapter.wait_for_file_input_calls, 1)
            self.assertEqual(Path(adapter.uploaded_path).name, "Example_Candidate_CV.docx")

    def test_resume_upload_uses_second_file_input_when_first_is_disabled(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            root = Path(temp_dir)
            adapter = FakeSeekAdapter(
                True,
                radio_available=True,
                button_available=True,
                file_input_after_upload_click=True,
                file_input_candidates=[
                    {"visible": False, "disabled": True, "accept": ".doc,.docx,.pdf", "works": False},
                    {"visible": True, "disabled": False, "accept": ".doc,.docx,.pdf", "works": True},
                ],
            )
            result = handle_resume_upload(adapter, root / "Example_Candidate_CV.docx", root / "data" / "debug_apply_assist")
            self.assertEqual(result.status, "resume_uploaded")
            self.assertEqual(Path(adapter.uploaded_path).name, "Example_Candidate_CV.docx")

    def test_resume_upload_accepts_hidden_but_valid_file_input(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            root = Path(temp_dir)
            adapter = FakeSeekAdapter(
                True,
                radio_available=True,
                button_available=True,
                file_input_after_upload_click=True,
                file_input_candidates=[
                    {"visible": False, "disabled": False, "accept": ".doc,.docx,.pdf", "works": True},
                ],
            )
            result = handle_resume_upload(adapter, root / "Example_Candidate_CV.docx", root / "data" / "debug_apply_assist")
            self.assertEqual(result.status, "resume_uploaded")

    def test_resume_upload_retries_upload_click_when_file_input_missing(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            root = Path(temp_dir)
            adapter = FakeSeekAdapter(
                True,
                has_file_input=False,
                radio_available=True,
                button_available=False,
                file_input_after_upload_click=True,
                upload_succeeds=False,
            )
            result = handle_resume_upload(adapter, root / "Example_Candidate_CV.docx", root / "data" / "debug_apply_assist")
            self.assertEqual(result.status, "paused_for_resume_upload_incomplete")
            self.assertGreaterEqual(adapter.upload_button_clicks, 2)

    def test_resume_limit_modal_detected_before_stale_file_input_attempt(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            root = Path(temp_dir)
            adapter = FakeSeekAdapter(
                True,
                has_file_input=True,
                radio_available=True,
                button_available=True,
                resume_limit_present=True,
                resume_limit_options=["Please select a resumÃ©", "8/5/26 - old_resume.docx"],
                file_input_candidates=[
                    {"visible": True, "disabled": True, "accept": ".doc,.docx,.pdf", "works": False},
                ],
            )
            result = handle_resume_upload(adapter, root / "Example_Candidate_CV.docx", root / "data" / "debug_apply_assist")
            self.assertEqual(result.status, "resume_uploaded")
            self.assertTrue(adapter.resume_limit_delete_clicked)

    def test_resume_manual_pause_returns_paused_status_when_no_console_input(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            root = Path(temp_dir)
            adapter = FakeSeekAdapter(True, has_file_input=False, radio_available=False, button_available=False, resume_present=False)
            result = handle_resume_upload(adapter, root / "selected_cv.docx", root / "data" / "debug_apply_assist")
            self.assertEqual(result.status, "paused_for_manual_resume_upload")
            self.assertTrue(adapter.saved_debug)

    def test_questionnaire_manual_pause_returns_paused_status_when_no_console_input(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            root = Path(temp_dir)
            database, job_id, _ = self._build_seek_job(root)
            adapter = FakeSeekAdapter(True, questionnaire_eligibility_available=False)
            result = _assist_seek_apply(
                job_id,
                database,
                visible=True,
                adapter_factory=lambda visible: adapter,
                input_func=_raise_eof,
            )
            self.assertEqual(result.status, "paused_for_manual_questionnaire")

    def test_questionnaire_success_continues_to_review(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            root = Path(temp_dir)
            database, job_id, _ = self._build_seek_job(root)
            adapter = FakeSeekAdapter(True, questionnaire_eligibility_available=True, questionnaire_salary_available=True)
            result = _assist_seek_apply(job_id, database, visible=True, adapter_factory=lambda visible: adapter, input_func=lambda: "")
            self.assertEqual(result.status, "prepared_for_manual_review")
            self.assertIn("clicked_continue_questionnaire", result.steps_completed)
            self.assertTrue(adapter.questionnaire_continue_clicked)
            self.assertTrue(adapter.cover_continue_clicked)
            self.assertTrue(adapter.close_called)

    def test_questionnaire_validation_resubmit_can_clear_stale_errors(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            root = Path(temp_dir)
            database, job_id, _ = self._build_seek_job(root)
            adapter = FakeSeekAdapter(
                True,
                questionnaire_eligibility_available=True,
                questionnaire_salary_available=True,
                dynamic_questions=[
                    {
                        "name": "questionnaire.NZ_Q_13_V_2",
                        "field_type": "select",
                        "question_text": "How much notice are you required to give your current employer?",
                        "options": ["1 week", "2 weeks", "3 weeks"],
                        "option_payloads": [
                            {"label": "1 week", "value": "one_week"},
                            {"label": "2 weeks", "value": "two_weeks"},
                            {"label": "3 weeks", "value": "three_weeks"},
                        ],
                        "expected_answer": "2 weeks",
                    }
                ],
                validation_errors=[],
                questionnaire_continue_validation_sequence=[
                    ["How much notice are you required to give your current employer?"],
                    [],
                ],
            )
            result = _assist_seek_apply(
                job_id,
                database,
                visible=True,
                adapter_factory=lambda visible: adapter,
                input_func=lambda: "",
            )
            self.assertEqual(result.status, "prepared_for_manual_review")
            self.assertTrue(adapter.questionnaire_continue_clicked)
            self.assertEqual(adapter.get_questionnaire_validation_errors(), [])

    def test_dynamic_sql_queries_question_uses_skills_experience_years_profile_value(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            adapter = FakeSeekAdapter(
                True,
                dynamic_questions=[
                    {
                        "name": "questionnaire_sql_years",
                        "field_type": "select",
                        "question_text": "How many years' experience do you have using SQL queries?",
                        "options": ["Less than 1 year", "1 year", "2 years", "3 years", "4 years", "5 years", "More than 5 years"],
                        "expected_answer": "More than 5 years",
                    }
                ],
            )
            result = handle_dynamic_seek_questions(adapter, DEFAULT_APPLICANT_PROFILE, Path(temp_dir) / "debug", input_func=lambda: "")
            self.assertEqual(result.status, "dynamic_questions_completed")
            self.assertEqual(adapter.dynamic_answers["questionnaire_sql_years"], "More than 5 years")

    def test_dynamic_sql_queries_question_chooses_5_plus_style_option(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            adapter = FakeSeekAdapter(
                True,
                dynamic_questions=[
                    {
                        "name": "questionnaire_sql_years",
                        "field_type": "select",
                        "question_text": "How many years' experience do you have using SQL queries?",
                        "options": ["0-1", "1-2", "3-5", "5+"],
                        "expected_answer": "5+",
                    }
                ],
            )
            result = handle_dynamic_seek_questions(adapter, DEFAULT_APPLICANT_PROFILE, Path(temp_dir) / "debug", input_func=lambda: "")
            self.assertEqual(result.status, "dynamic_questions_completed")
            self.assertEqual(adapter.dynamic_answers["questionnaire_sql_years"], "5+")

    def test_payroll_free_text_question_uses_truthful_profile_answer(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            adapter = FakeSeekAdapter(
                True,
                dynamic_questions=[
                    {
                        "name": "questionnaire_payroll",
                        "field_type": "textarea",
                        "question_text": "Walk us through your experience processing payroll in a New Zealand environment. How many employees were you responsible for and how did you ensure accuracy and legislative compliance?",
                        "options": [],
                    }
                ],
            )
            result = handle_dynamic_seek_questions(adapter, DEFAULT_APPLICANT_PROFILE, Path(temp_dir) / "debug", input_func=lambda: "")
            self.assertEqual(result.status, "dynamic_questions_completed")
            answer = adapter.dynamic_answers["questionnaire_payroll"]
            self.assertIn("I do not have direct payroll processing experience", answer)
            self.assertNotEqual(answer, "More than 5 years")

    def test_psychometric_assessment_question_chooses_yes(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            adapter = FakeSeekAdapter(
                True,
                dynamic_questions=[
                    {
                        "name": "questionnaire_psychometric",
                        "field_type": "select",
                        "question_text": "As part of our selection process, shortlisted candidates will be asked to complete a psychometric assessment. Are you comfortable proceeding with this requirement?",
                        "options": ["Yes", "No"],
                    }
                ],
            )
            result = handle_dynamic_seek_questions(adapter, DEFAULT_APPLICANT_PROFILE, Path(temp_dir) / "debug", input_func=lambda: "")
            self.assertEqual(result.status, "dynamic_questions_completed")
            self.assertEqual(adapter.dynamic_answers["questionnaire_psychometric"], "Yes")

    def test_notice_period_question_chooses_two_weeks(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            adapter = FakeSeekAdapter(
                True,
                dynamic_questions=[
                    {
                        "name": "questionnaire_notice_period",
                        "field_type": "select",
                        "question_text": "How much notice are you required to give your current employer?",
                        "options": ["Immediately", "1 week", "2 weeks", "4 weeks", "6 or more weeks"],
                    }
                ],
            )
            result = handle_dynamic_seek_questions(adapter, DEFAULT_APPLICANT_PROFILE, Path(temp_dir) / "debug", input_func=lambda: "")
            self.assertEqual(result.status, "dynamic_questions_completed")
            self.assertEqual(adapter.dynamic_answers["questionnaire_notice_period"], "2 weeks")

    def test_notice_period_question_chooses_two_weeks_or_less(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            adapter = FakeSeekAdapter(
                True,
                dynamic_questions=[
                    {
                        "name": "questionnaire_notice_period",
                        "field_type": "select",
                        "question_text": "How much notice are you required to give your current employer?",
                        "options": ["Immediately", "Less than 2 weeks", "2 weeks or less", "4 weeks"],
                    }
                ],
            )
            result = handle_dynamic_seek_questions(adapter, DEFAULT_APPLICANT_PROFILE, Path(temp_dir) / "debug", input_func=lambda: "")
            self.assertEqual(result.status, "dynamic_questions_completed")
            self.assertEqual(adapter.dynamic_answers["questionnaire_notice_period"], "2 weeks or less")

    def test_notice_period_generic_resolver_does_not_choose_longer_notice_option(self) -> None:
        resolution = resolve_profile_backed_answer(
            question_text="How much notice are you required to give your current employer?",
            field_type="select",
            available_options=["2 weeks", "4 weeks", "6 or more weeks"],
            applicant_profile=DEFAULT_APPLICANT_PROFILE,
        )
        self.assertTrue(resolution.should_answer)
        self.assertEqual(resolution.matched_rule, "notice_period")
        self.assertEqual(resolution.chosen_answer, "2 weeks")

    def test_load_applicant_profile_deep_merges_defaults(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            data_dir = Path(temp_dir)
            (data_dir / "applicant_profile.json").write_text(
                json.dumps(
                    {
                        "highest_education_level": "Bachelor Degree",
                        "automation": {"seek_browser_channels": ["chrome"]},
                    }
                ),
                encoding="utf-8",
            )
            profile = load_applicant_profile(data_dir)
            self.assertEqual(profile["highest_education_level"], "Bachelor Degree")
            self.assertEqual(profile["automation"]["seek_browser_channels"], ["chrome"])
            self.assertTrue(profile["software_experience"]["xero"])
            self.assertTrue(profile["software_experience"]["microsoft excel"])

    def test_dynamic_question_validation_retry_reapplies_notice_period(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            adapter = FakeSeekAdapter(
                True,
                dynamic_questions=[
                    {
                        "name": "questionnaire_notice_period",
                        "field_type": "select",
                        "question_text": "How much notice are you required to give your current employer?",
                        "nearby_text_candidates": [
                            "How much notice are you required to give your current employer?",
                            "Immediately",
                            "1 week",
                            "2 weeks",
                            "4 weeks",
                        ],
                        "options": ["Immediately", "1 week", "2 weeks", "4 weeks"],
                        "option_payloads": [
                            {"label": "Immediately", "value": "0"},
                            {"label": "1 week", "value": "1"},
                            {"label": "2 weeks", "value": "2"},
                            {"label": "4 weeks", "value": "4"},
                        ],
                        "expected_answer": "2 weeks",
                    }
                ],
                validation_errors=["How much notice are you required to give your current employer?"],
            )
            result = handle_dynamic_seek_questions(
                adapter,
                DEFAULT_APPLICANT_PROFILE,
                Path(temp_dir) / "debug",
                input_func=lambda: "",
            )
            self.assertEqual(result.status, "dynamic_questions_completed")
            self.assertEqual(adapter.dynamic_answers["questionnaire_notice_period"], "2 weeks")
            self.assertEqual(adapter.get_questionnaire_validation_errors(), [])

    def test_questionnaire_validation_detector_ignores_non_invalid_filled_selects(self) -> None:
        class _ValidationPage:
            def locator(self, selector: str):
                if selector == "body":
                    class _Body:
                        def inner_text(self, timeout=5000):
                            return "How much notice are you required to give your current employer?\n2 weeks"
                    return _Body()
                raise AssertionError(selector)

            def evaluate(self, script: str):
                if '[role="alert"]' in script:
                    return []
                if 'empty_select_questions' in script or 'select[name*="questionnaire"]' in script:
                    return []
                return []

        adapter = _PlaywrightSeekAdapter.__new__(_PlaywrightSeekAdapter)
        adapter._page = _ValidationPage()
        self.assertEqual(adapter.get_questionnaire_validation_errors(), [])

    def test_cleanup_stale_seek_profile_locks_removes_known_lock_files(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            profile_dir = Path(temp_dir) / "browser_profiles" / "seek_apply_bulk"
            (profile_dir / "Default").mkdir(parents=True, exist_ok=True)
            lock_paths = [
                profile_dir / "SingletonLock",
                profile_dir / "SingletonCookie",
                profile_dir / "Default" / "LOCK",
            ]
            for lock_path in lock_paths:
                lock_path.write_text("locked", encoding="utf-8")
            removed = _cleanup_stale_seek_profile_locks(profile_dir)
            self.assertEqual({path.name for path in removed}, {"SingletonLock", "SingletonCookie", "LOCK"})
            for lock_path in lock_paths:
                self.assertFalse(lock_path.exists())

    def test_xero_yes_no_question_chooses_yes(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            adapter = FakeSeekAdapter(
                True,
                dynamic_questions=[
                    {
                        "name": "questionnaire_xero",
                        "field_type": "radio",
                        "question_text": "Do you have experience using Xero?",
                        "options": ["Yes", "No"],
                    }
                ],
            )
            result = handle_dynamic_seek_questions(adapter, DEFAULT_APPLICANT_PROFILE, Path(temp_dir) / "debug", input_func=lambda: "")
            self.assertEqual(result.status, "dynamic_questions_completed")
            self.assertEqual(adapter.dynamic_answers["questionnaire_xero"], "Yes")

    def test_excel_yes_no_question_chooses_yes(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            adapter = FakeSeekAdapter(
                True,
                dynamic_questions=[
                    {
                        "name": "questionnaire_excel",
                        "field_type": "radio",
                        "question_text": "Do you have experience using Microsoft Excel?",
                        "options": ["Yes", "No"],
                    }
                ],
            )
            result = handle_dynamic_seek_questions(adapter, DEFAULT_APPLICANT_PROFILE, Path(temp_dir) / "debug", input_func=lambda: "")
            self.assertEqual(result.status, "dynamic_questions_completed")
            self.assertEqual(adapter.dynamic_answers["questionnaire_excel"], "Yes")

    def test_full_working_rights_question_chooses_yes(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            adapter = FakeSeekAdapter(
                True,
                dynamic_questions=[
                    {
                        "name": "questionnaire_work_rights",
                        "field_type": "radio",
                        "question_text": "Do you have full working rights in New Zealand?",
                        "options": ["Yes", "No"],
                    }
                ],
            )
            result = handle_dynamic_seek_questions(adapter, DEFAULT_APPLICANT_PROFILE, Path(temp_dir) / "debug", input_func=lambda: "")
            self.assertEqual(result.status, "dynamic_questions_completed")
            self.assertEqual(adapter.dynamic_answers["questionnaire_work_rights"], "Yes")

    def test_generic_years_resolver_does_not_run_for_walk_us_through_textarea(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            adapter = FakeSeekAdapter(
                True,
                dynamic_questions=[
                    {
                        "name": "questionnaire_walkthrough",
                        "field_type": "textarea",
                        "question_text": "Walk us through your experience using SQL queries and explain the work you performed.",
                        "options": [],
                    }
                ],
            )
            result = handle_dynamic_seek_questions(adapter, DEFAULT_APPLICANT_PROFILE, Path(temp_dir) / "debug", input_func=lambda: "")
            self.assertEqual(result.status, "dynamic_questions_completed")
            self.assertNotIn("questionnaire_walkthrough", adapter.dynamic_answers)

    def test_financial_discrepancy_textarea_uses_discrepancy_answer_not_salary(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            adapter = FakeSeekAdapter(
                True,
                dynamic_questions=[
                    {
                        "name": "questionnaire_discrepancy",
                        "field_type": "textarea",
                        "question_text": "Describe a time you found a financial discrepancy or risk, what you found, how you handled it, and the outcome.",
                        "options": [],
                    }
                ],
            )
            result = handle_dynamic_seek_questions(adapter, DEFAULT_APPLICANT_PROFILE, Path(temp_dir) / "debug", input_func=lambda: "")
            self.assertEqual(result.status, "dynamic_questions_completed")
            answer = adapter.dynamic_answers["questionnaire_discrepancy"]
            self.assertIn("identified discrepancies", answer)
            self.assertNotEqual(answer, "90k")

    def test_salary_answer_only_used_for_salary_fields(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            answer, matched_rule, _ = resolve_free_text_question_answer(
                "Describe a time you found a financial discrepancy or risk, what you found, how you handled it, and the outcome.",
                DEFAULT_APPLICANT_PROFILE,
                {},
            )
            self.assertEqual(matched_rule, "financial_discrepancy_or_risk")
            self.assertNotEqual(answer, "90k")

    def test_explicit_salary_question_gets_salary_answer(self) -> None:
        resolution = resolve_profile_backed_answer(
            question_text="What are your salary expectations for this role?",
            field_type="textarea",
            available_options=[],
            applicant_profile=DEFAULT_APPLICANT_PROFILE,
        )
        self.assertTrue(resolution.should_answer)
        self.assertEqual(resolution.matched_rule, "salary_expectation")
        self.assertEqual(resolution.chosen_answer, "185k")

    def test_salary_select_question_maps_to_closest_option(self) -> None:
        profile = dict(DEFAULT_APPLICANT_PROFILE)
        profile["salary_expectation_text"] = "115k"
        resolution = resolve_profile_backed_answer(
            question_text="What's your expected annual base salary?",
            field_type="select",
            available_options=["$90k", "$100k", "$110k", "$120k", "$130k"],
            applicant_profile=profile,
        )
        self.assertTrue(resolution.should_answer)
        self.assertEqual(resolution.matched_rule, "salary_expectation_option_match")
        self.assertEqual(resolution.chosen_answer, "$110k")

    def test_contextual_skill_yes_no_question_uses_profile_experience(self) -> None:
        profile = dict(DEFAULT_APPLICANT_PROFILE)
        profile["skills_experience_years"] = dict(DEFAULT_APPLICANT_PROFILE["skills_experience_years"])
        profile["skills_experience_years"]["python"] = 2
        resolution = resolve_profile_backed_answer(
            question_text="Do you have experience testing web applications?",
            field_type="radio",
            available_options=["Yes", "No"],
            applicant_profile=profile,
        )
        self.assertTrue(resolution.should_answer)
        self.assertEqual(resolution.matched_rule, "generic_skill_experience_yes_no")
        self.assertEqual(resolution.chosen_answer, "Yes")
        self.assertEqual(resolution.inferred_question_type, "skill_experience_yes_no")
        self.assertEqual(resolution.inferred_skill, "web application testing")
        self.assertIn("related profile experience", resolution.reason)

    def test_lead_generation_yes_no_question_uses_contextual_experience(self) -> None:
        resolution = resolve_profile_backed_answer(
            question_text="Do you have lead generation experience?",
            field_type="radio",
            available_options=["Yes", "No"],
            applicant_profile=DEFAULT_APPLICANT_PROFILE,
        )
        self.assertTrue(resolution.should_answer)
        self.assertEqual(resolution.matched_rule, "generic_skill_experience_yes_no")
        self.assertEqual(resolution.chosen_answer, "Yes")
        self.assertEqual(resolution.inferred_skill, "lead generation")

    def test_hseq_yes_no_question_uses_conservative_no_when_profile_has_no_explicit_match(self) -> None:
        resolution = resolve_profile_backed_answer(
            question_text="Are you familiar with the Health, Safety, Environment and Quality (HSEQ) standards?",
            field_type="radio",
            available_options=["Yes", "No"],
            applicant_profile=DEFAULT_APPLICANT_PROFILE,
        )
        self.assertTrue(resolution.should_answer)
        self.assertEqual(resolution.matched_rule, "hseq_experience_conservative_no")
        self.assertEqual(resolution.chosen_answer, "No")

    def test_continuous_production_yes_no_question_uses_conservative_no_when_profile_has_no_explicit_match(self) -> None:
        resolution = resolve_profile_backed_answer(
            question_text="Do you have experience working in a continuous production environment?",
            field_type="radio",
            available_options=["Yes", "No"],
            applicant_profile=DEFAULT_APPLICANT_PROFILE,
        )
        self.assertTrue(resolution.should_answer)
        self.assertEqual(resolution.matched_rule, "continuous_production_experience_conservative_no")
        self.assertEqual(resolution.chosen_answer, "No")

    def test_management_qualification_question_uses_dont_have_option(self) -> None:
        resolution = resolve_profile_backed_answer(
            question_text="Have you completed a qualification in operations management?",
            field_type="select",
            available_options=[
                "Yes, Diploma Degree",
                "Yes, Bachelor Degree",
                "Yes, Masters Degree",
                "Yes, Doctoral Degree",
                "I have a qualification in operations management which isn't listed",
                "I don't have a qualification in operations management",
            ],
            applicant_profile=DEFAULT_APPLICANT_PROFILE,
        )
        self.assertTrue(resolution.should_answer)
        self.assertEqual(resolution.matched_rule, "management_qualification")
        self.assertEqual(resolution.chosen_answer, "I don't have a qualification in operations management")

    def test_additional_information_textarea_uses_not_applicable_response(self) -> None:
        resolution = resolve_profile_backed_answer(
            question_text="Please use this space to include any additional information",
            field_type="textarea",
            available_options=[],
            applicant_profile=DEFAULT_APPLICANT_PROFILE,
        )
        self.assertTrue(resolution.should_answer)
        self.assertEqual(resolution.matched_rule, "additional_information_not_applicable")
        self.assertEqual(resolution.chosen_answer, "N/A")

    def test_employee_referral_textarea_uses_not_applicable_response(self) -> None:
        resolution = resolve_profile_backed_answer(
            question_text="Have you been referred by someone working at FirstCape (Harbour, JBWere, or Consilium)? If yes, please include the name of the employee.",
            field_type="textarea",
            available_options=[],
            applicant_profile=DEFAULT_APPLICANT_PROFILE,
        )
        self.assertTrue(resolution.should_answer)
        self.assertEqual(resolution.matched_rule, "employee_referral_not_applicable")
        self.assertIn("not referred", str(resolution.chosen_answer))

    def test_notice_period_textarea_uses_two_weeks_response(self) -> None:
        resolution = resolve_profile_backed_answer(
            question_text="What is your current notice period?",
            field_type="textarea",
            available_options=[],
            applicant_profile=DEFAULT_APPLICANT_PROFILE,
        )
        self.assertTrue(resolution.should_answer)
        self.assertEqual(resolution.matched_rule, "notice_period_text")
        self.assertEqual(resolution.chosen_answer, "2 weeks")

    def test_targets_and_kpis_yes_no_defaults_to_yes(self) -> None:
        resolution = resolve_profile_backed_answer(
            question_text="Do you have experience working towards targets and KPIs?",
            field_type="radio",
            available_options=["Yes", "No"],
            applicant_profile=DEFAULT_APPLICANT_PROFILE,
        )
        self.assertTrue(resolution.should_answer)
        self.assertEqual(resolution.matched_rule, "generic_skill_experience_yes_default")
        self.assertEqual(resolution.chosen_answer, "Yes")

    def test_forklift_licence_checkbox_defaults_to_none(self) -> None:
        resolution = resolve_profile_backed_answer(
            question_text="Which of the following forklift licences do you have?",
            field_type="checkbox",
            available_options=["Forklift Operator Certificate", "F Endorsement", "None of these"],
            applicant_profile=DEFAULT_APPLICANT_PROFILE,
        )
        self.assertTrue(resolution.should_answer)
        self.assertEqual(resolution.matched_rule, "forklift_licence_none")
        self.assertEqual(resolution.chosen_answer, "None of these")

    def test_interest_in_opportunity_textarea_gets_built_in_answer(self) -> None:
        answer, matched_rule, answer_source = resolve_free_text_question_answer(
            "What interests you about this opportunity?",
            DEFAULT_APPLICANT_PROFILE,
            {"job_title": "CRM & Customer Insights Manager"},
        )
        self.assertEqual(matched_rule, "interest_in_opportunity")
        self.assertIn("CRM & Customer Insights Manager role", answer or "")
        self.assertEqual(answer_source, "built_in_truthful_interest_answer")

    def test_salesforce_textarea_gets_truthful_limit_answer(self) -> None:
        answer, matched_rule, answer_source = resolve_free_text_question_answer(
            "How many years experience do you have working with Salesforce Sales Cloud and Salesforce Data Cloud, and in what capacity have you used this?",
            DEFAULT_APPLICANT_PROFILE,
            {},
        )
        self.assertEqual(matched_rule, "salesforce_experience_truthful_limit")
        self.assertIn("do not have direct hands-on experience", answer or "")
        self.assertEqual(answer_source, "built_in_truthful_salesforce_limit_answer")

    def test_data_engineer_years_question_uses_two_year_override(self) -> None:
        resolution = resolve_profile_backed_answer(
            question_text="How many years' experience do you have as a Data Engineer?",
            field_type="select",
            available_options=["No experience", "Less than 1 year", "1 year", "2 years", "3 years", "More than 5 years"],
            applicant_profile=DEFAULT_APPLICANT_PROFILE,
        )
        self.assertTrue(resolution.should_answer)
        self.assertEqual(resolution.matched_rule, "data_engineer_years_override")
        self.assertEqual(resolution.chosen_answer, "2 years")
        self.assertEqual(resolution.profile_years_used, 2)

    def test_notice_period_question_uses_two_weeks_override(self) -> None:
        resolution = resolve_profile_backed_answer(
            question_text="How much notice are you required to give your current employer?",
            field_type="select",
            available_options=["None, I'm ready to go now", "1 week", "2 weeks", "3 weeks", "4 weeks"],
            applicant_profile=DEFAULT_APPLICANT_PROFILE,
        )
        self.assertTrue(resolution.should_answer)
        self.assertEqual(resolution.matched_rule, "notice_period")
        self.assertEqual(resolution.chosen_answer, "2 weeks")
        self.assertIn("2 weeks", resolution.reason)

    def test_heard_about_position_question_chooses_seek(self) -> None:
        resolution = resolve_profile_backed_answer(
            question_text="How did you hear about this position?",
            field_type="select",
            available_options=["LinkedIN", "SEEK", "Word of mouth"],
            applicant_profile=DEFAULT_APPLICANT_PROFILE,
        )
        self.assertTrue(resolution.should_answer)
        self.assertEqual(resolution.matched_rule, "heard_about_role_via_seek")
        self.assertEqual(resolution.chosen_answer, "SEEK")

    def test_right_to_work_radio_question_chooses_nz_citizen_option(self) -> None:
        resolution = resolve_profile_backed_answer(
            question_text="Are you legally entitled to work in NZ?",
            field_type="radio",
            available_options=["NZ Citizen", "NZ Permanent Resident", "Work Visa", "No"],
            applicant_profile=DEFAULT_APPLICANT_PROFILE,
        )
        self.assertTrue(resolution.should_answer)
        self.assertEqual(resolution.matched_rule, "right_to_work_nz_option_match")
        self.assertEqual(resolution.chosen_answer, "NZ Citizen")

    def test_drivers_licence_question_chooses_full_nz_licence(self) -> None:
        resolution = resolve_profile_backed_answer(
            question_text="Do you hold a current NZ drivers licence?",
            field_type="radio",
            available_options=[
                "Full NZ Drivers Licence",
                "Restricted NZ Driver Licence",
                "Learners NZ Drivers Licence",
                "None of the above",
            ],
            applicant_profile=DEFAULT_APPLICANT_PROFILE,
        )
        self.assertTrue(resolution.should_answer)
        self.assertEqual(resolution.matched_rule, "drivers_licence_profile_match")
        self.assertEqual(resolution.chosen_answer, "Full NZ Drivers Licence")

    def test_nil_or_na_instruction_textarea_chooses_nil(self) -> None:
        resolution = resolve_profile_backed_answer(
            question_text="If you are on a Work Visa - please provide the following details. If you answered NZ Citizen / NZ Permanent Resident / No, simply indicate Nil or NA",
            field_type="textarea",
            available_options=[],
            applicant_profile=DEFAULT_APPLICANT_PROFILE,
        )
        self.assertTrue(resolution.should_answer)
        self.assertEqual(resolution.matched_rule, "explicit_nil_or_na_instruction")
        self.assertEqual(resolution.chosen_answer, "Nil")

    def test_current_or_previous_target_employer_question_defaults_to_no(self) -> None:
        resolution = resolve_profile_backed_answer(
            question_text="Do you currently work or have previously worked for Fidelity Life?",
            field_type="radio",
            available_options=["Yes", "No"],
            applicant_profile=DEFAULT_APPLICANT_PROFILE,
        )
        self.assertTrue(resolution.should_answer)
        self.assertEqual(resolution.matched_rule, "worked_for_target_employer_before")
        self.assertEqual(resolution.chosen_answer, "No")

    def test_criminal_offence_question_defaults_to_no(self) -> None:
        resolution = resolve_profile_backed_answer(
            question_text="Have you ever been charged with a criminal offence?",
            field_type="radio",
            available_options=["Yes", "No"],
            applicant_profile=DEFAULT_APPLICANT_PROFILE,
        )
        self.assertTrue(resolution.should_answer)
        self.assertEqual(resolution.matched_rule, "criminal_offence_history")
        self.assertEqual(resolution.chosen_answer, "No")

    def test_disciplinary_follow_up_textarea_defaults_to_nil(self) -> None:
        resolution = resolve_profile_backed_answer(
            question_text="Have you ever been subject to any disciplinary action or investigation by a previous employer, or have you resigned as a result of a pending disciplinary process or investigation?",
            field_type="textarea",
            available_options=[],
            applicant_profile=DEFAULT_APPLICANT_PROFILE,
        )
        self.assertTrue(resolution.should_answer)
        self.assertEqual(resolution.matched_rule, "disciplinary_follow_up_nil")
        self.assertEqual(resolution.chosen_answer, "Nil")

    def test_nz_location_prompt_chooses_auckland(self) -> None:
        resolution = resolve_profile_backed_answer(
            question_text="The location for this role is flexible within NZ, whereabouts in New Zealand are you located?",
            field_type="textarea",
            available_options=[],
            applicant_profile=DEFAULT_APPLICANT_PROFILE,
        )
        self.assertTrue(resolution.should_answer)
        self.assertEqual(resolution.matched_rule, "nz_location_text")
        self.assertEqual(resolution.chosen_answer, "Auckland")

    def test_power_bi_experience_yes_no_chooses_yes(self) -> None:
        resolution = resolve_profile_backed_answer(
            question_text="Do you have PowerBI experience?",
            field_type="radio",
            available_options=["Yes", "No"],
            applicant_profile=DEFAULT_APPLICANT_PROFILE,
        )
        self.assertTrue(resolution.should_answer)
        self.assertEqual(resolution.matched_rule, "power_bi_experience_yes_no")
        self.assertEqual(resolution.chosen_answer, "Yes")

    def test_power_bi_experience_text_uses_profile_backed_answer(self) -> None:
        resolution = resolve_profile_backed_answer(
            question_text="Do you have PowerBI experience?",
            field_type="textarea",
            available_options=[],
            applicant_profile=DEFAULT_APPLICANT_PROFILE,
        )
        self.assertTrue(resolution.should_answer)
        self.assertEqual(resolution.matched_rule, "power_bi_experience_text")
        self.assertIn("Power BI experience", resolution.chosen_answer or "")

    def test_modern_data_platforms_text_uses_truthful_limit_answer(self) -> None:
        resolution = resolve_profile_backed_answer(
            question_text="Have you got experience in Microsoft Fabric, Databricks or Snowflake? If so how many years experience?",
            field_type="textarea",
            available_options=[],
            applicant_profile=DEFAULT_APPLICANT_PROFILE,
        )
        self.assertTrue(resolution.should_answer)
        self.assertEqual(resolution.matched_rule, "modern_data_platforms_truthful_limit")
        self.assertIn("do not want to overstate", resolution.chosen_answer or "")

    def test_playwright_adapter_select_answer_verifies_committed_value(self) -> None:
        class FakeSelectLocator:
            def __init__(self) -> None:
                self.selected_value = ""
                self.selected_label = ""
                self.focused = False
                self.tabbed = False
                self.first = self

            def count(self) -> int:
                return 1

            def select_option(self, *, value=None, label=None):
                if value == "opt-2":
                    self.selected_value = "opt-2"
                    self.selected_label = "2 weeks"
                elif label == "2 weeks":
                    self.selected_value = "opt-2"
                    self.selected_label = "2 weeks"

            def evaluate(self, script, payload=None):
                script_text = str(script)
                if "selectedOptions" in script_text:
                    return {"value": self.selected_value, "label": self.selected_label}
                desired_value = str((payload or {}).get("value") or "").strip()
                desired_label = str((payload or {}).get("label") or "").strip().lower()
                if desired_value == "opt-2" or desired_label == "2 weeks":
                    self.selected_value = "opt-2"
                    self.selected_label = "2 weeks"
                return None

            def focus(self) -> None:
                self.focused = True

            def press(self, key: str) -> None:
                if key == "Tab":
                    self.tabbed = True

        class FakeSelectPage:
            def __init__(self) -> None:
                self.locator_instance = FakeSelectLocator()

            def locator(self, selector: str):
                self.last_selector = selector
                return self.locator_instance

        adapter = object.__new__(_PlaywrightSeekAdapter)
        adapter._page = FakeSelectPage()
        success = adapter.answer_dynamic_question(
            {
                "field_type": "select",
                "name": "questionnaire.notice",
                "option_payloads": [
                    {"label": "1 week", "value": "opt-1"},
                    {"label": "2 weeks", "value": "opt-2"},
                ],
            },
            "2 weeks",
        )
        self.assertTrue(success)
        self.assertEqual(adapter._page.locator_instance.selected_value, "opt-2")
        self.assertEqual(adapter._page.locator_instance.selected_label, "2 weeks")

    def test_playwright_adapter_select_existing_resume_option_falls_back_to_matching_value(self) -> None:
        class FakeResumeSelectLocator:
            def __init__(self) -> None:
                self.first = self
                self.selected_value = ""
                self.selected_label = ""
                self.focused = False
                self.tabbed = False
                self.options = [
                    {"index": 0, "value": "", "label": "Please select a resumÃ©"},
                    {
                        "index": 1,
                        "value": "resume-2",
                        "label": "2/7/26 - Example_Candidate_CV_20260702_170131_591042.docx",
                    },
                ]

            def count(self) -> int:
                return 1

            def select_option(self, *, value=None, label=None, index=None):
                if value == "resume-2":
                    self.selected_value = "resume-2"
                    self.selected_label = self.options[1]["label"]
                    return
                if label == self.options[1]["label"]:
                    return
                if index == 1:
                    self.selected_value = "resume-2"
                    self.selected_label = self.options[1]["label"]

            def evaluate(self, script, payload=None):
                script_text = str(script)
                if "selectedOptions" in script_text:
                    return {"value": self.selected_value, "label": self.selected_label}
                if "Array.from(el.options" in script_text:
                    return list(self.options)
                desired_value = str((payload or {}).get("value") or "").strip()
                desired_label = str((payload or {}).get("label") or "").strip().lower()
                if desired_value == "resume-2" or "example_candidate_cv_20260702_170131_591042.docx" in desired_label:
                    self.selected_value = "resume-2"
                    self.selected_label = self.options[1]["label"]
                return None

            def focus(self) -> None:
                self.focused = True

            def press(self, key: str) -> None:
                if key == "Tab":
                    self.tabbed = True

        class FakeResumeSelectPage:
            def __init__(self) -> None:
                self.locator_instance = FakeResumeSelectLocator()

            def locator(self, selector: str):
                self.last_selector = selector
                return self.locator_instance

            def wait_for_timeout(self, timeout_ms: int) -> None:
                self.last_wait_timeout_ms = timeout_ms

        adapter = object.__new__(_PlaywrightSeekAdapter)
        adapter._page = FakeResumeSelectPage()

        success = adapter.select_existing_resume_option(
            "Example_Candidate_CV_20260702_170131_591042.docx"
        )

        self.assertTrue(success)
        self.assertEqual(adapter._page.locator_instance.selected_value, "resume-2")
        self.assertIn(
            "Example_Candidate_CV_20260702_170131_591042.docx",
            adapter._page.locator_instance.selected_label,
        )

    def test_required_blank_textarea_is_filled_on_validation_recovery(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            adapter = FakeSeekAdapter(
                True,
                dynamic_questions=[
                    {
                        "name": "questionnaire_payroll_recovery",
                        "field_type": "textarea",
                        "question_text": "Walk us through your experience processing payroll in a New Zealand environment. How many employees were you responsible for and how did you ensure accuracy and legislative compliance?",
                        "options": [],
                    }
                ],
                validation_errors=[
                    "Walk us through your experience processing payroll in a New Zealand environment. How many employees were you responsible for and how did you ensure accuracy and legislative compliance?"
                ],
            )
            result = handle_dynamic_seek_questions(adapter, DEFAULT_APPLICANT_PROFILE, Path(temp_dir) / "debug", input_func=lambda: "")
            self.assertEqual(result.status, "dynamic_questions_completed")
            self.assertIn("questionnaire_payroll_recovery", adapter.dynamic_answers)
            self.assertEqual(adapter.get_questionnaire_validation_errors(), [])

    def test_dynamic_years_question_does_not_select_eleven_plus_if_profile_years_is_ten(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            profile = dict(DEFAULT_APPLICANT_PROFILE)
            profile["skills_experience_years"] = dict(DEFAULT_APPLICANT_PROFILE["skills_experience_years"])
            profile["skills_experience_years"]["sql"] = 10
            adapter = FakeSeekAdapter(
                True,
                dynamic_questions=[
                    {
                        "name": "questionnaire_sql_years",
                        "field_type": "select",
                        "question_text": "How many years' experience do you have using SQL queries?",
                        "options": ["0-2 years", "3-5 years", "6-10 years", "11+ years"],
                        "expected_answer": "6-10 years",
                    }
                ],
            )
            result = handle_dynamic_seek_questions(adapter, profile, Path(temp_dir) / "debug", input_func=lambda: "")
            self.assertEqual(result.status, "dynamic_questions_completed")
            self.assertEqual(adapter.dynamic_answers["questionnaire_sql_years"], "6-10 years")

    def test_unknown_legal_question_does_not_guess(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            adapter = FakeSeekAdapter(
                True,
                dynamic_questions=[
                    {
                        "name": "questionnaire_criminal",
                        "field_type": "select",
                        "question_text": "Do you have any criminal convictions?",
                        "options": ["Yes", "No"],
                    }
                ],
            )
            result = handle_dynamic_seek_questions(adapter, DEFAULT_APPLICANT_PROFILE, Path(temp_dir) / "debug", input_func=lambda: (_ for _ in ()).throw(AssertionError("input() should not be called")))
            self.assertEqual(result.status, "dynamic_questions_completed")
            self.assertNotIn("questionnaire_criminal", adapter.dynamic_answers)

    def test_unknown_qualification_question_does_not_guess(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            adapter = FakeSeekAdapter(
                True,
                dynamic_questions=[
                    {
                        "name": "questionnaire_unknown_qualification",
                        "field_type": "select",
                        "question_text": "Have you completed a qualification in marine biology?",
                        "options": ["Yes", "No"],
                    }
                ],
            )
            result = handle_dynamic_seek_questions(adapter, DEFAULT_APPLICANT_PROFILE, Path(temp_dir) / "debug", input_func=lambda: (_ for _ in ()).throw(AssertionError("input() should not be called")))
            self.assertEqual(result.status, "dynamic_questions_completed")
            self.assertNotIn("questionnaire_unknown_qualification", adapter.dynamic_answers)

    def test_explicit_criminal_offence_rule_requires_profile_key_to_exist(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            profile = dict(DEFAULT_APPLICANT_PROFILE)
            disclosures = dict(profile.get("disclosures") or {})
            disclosures.pop("has_criminal_offence_history", None)
            profile["disclosures"] = disclosures
            adapter = FakeSeekAdapter(
                True,
                dynamic_questions=[
                    {
                        "name": "questionnaire_criminal_history",
                        "field_type": "select",
                        "question_text": "Have you ever been charged with a criminal offence?",
                        "options": ["Yes", "No"],
                    }
                ],
            )
            result = handle_dynamic_seek_questions(adapter, profile, Path(temp_dir) / "debug", input_func=lambda: (_ for _ in ()).throw(AssertionError("input() should not be called")))
            self.assertEqual(result.status, "dynamic_questions_completed")
            self.assertNotIn("questionnaire_criminal_history", adapter.dynamic_answers)

    def test_pause_on_unknown_questions_false_does_not_call_input(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            profile = dict(DEFAULT_APPLICANT_PROFILE)
            profile["automation"] = dict(DEFAULT_APPLICANT_PROFILE["automation"])
            profile["automation"]["pause_on_unknown_questions"] = False
            adapter = FakeSeekAdapter(
                True,
                dynamic_questions=[
                    {
                        "name": "questionnaire_unknown",
                        "field_type": "select",
                        "question_text": "What is your favourite colour?",
                        "options": ["Blue", "Green"],
                    }
                ],
            )
            result = handle_dynamic_seek_questions(adapter, profile, Path(temp_dir) / "debug", input_func=lambda: (_ for _ in ()).throw(AssertionError("input() should not be called")))
            self.assertEqual(result.status, "dynamic_questions_completed")

    def test_dynamic_ict_qualification_question_chooses_yes(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            adapter = FakeSeekAdapter(
                True,
                dynamic_questions=[
                    {
                        "name": "questionnaire_ict",
                        "field_type": "radio",
                        "question_text": "Have you completed a qualification in ICT?",
                        "options": ["Yes", "No"],
                        "expected_answer": "Yes",
                    }
                ],
            )
            result = handle_dynamic_seek_questions(adapter, DEFAULT_APPLICANT_PROFILE, Path(temp_dir) / "debug", input_func=lambda: "")
            self.assertEqual(result.status, "dynamic_questions_completed")
            self.assertEqual(adapter.dynamic_answers["questionnaire_ict"], "Yes")

    def test_dynamic_mathematics_qualification_question_chooses_bachelor_degree(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            adapter = FakeSeekAdapter(
                True,
                dynamic_questions=[
                    {
                        "name": "questionnaire_maths",
                        "field_type": "select",
                        "question_text": "Have you completed a qualification in mathematics / mathematical science?",
                        "options": [
                            "Yes, Diploma",
                            "Yes, Associate Degree",
                            "Yes, Bachelor Degree",
                            "Yes, Masters Degree",
                            "I have a qualification in mathematics which isn't listed",
                            "I don't have a qualification in mathematics",
                        ],
                    }
                ],
            )
            profile = dict(DEFAULT_APPLICANT_PROFILE)
            profile["highest_education_level"] = "Bachelor Degree"
            result = handle_dynamic_seek_questions(adapter, profile, Path(temp_dir) / "debug", input_func=lambda: "")
            self.assertEqual(result.status, "dynamic_questions_completed")
            self.assertEqual(adapter.dynamic_answers["questionnaire_maths"], "Yes, Bachelor Degree")

    def test_dynamic_highest_education_question_chooses_bachelor_degree(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            adapter = FakeSeekAdapter(
                True,
                dynamic_questions=[
                    {
                        "name": "questionnaire_education",
                        "field_type": "select",
                        "question_text": "What's your highest level of education?",
                        "options": [
                            "NCEA Level 3",
                            "Bachelor Degree",
                            "Graduate Diploma",
                            "Masters Degree",
                        ],
                    }
                ],
            )
            profile = dict(DEFAULT_APPLICANT_PROFILE)
            profile["highest_education_level"] = "Bachelor Degree"
            result = handle_dynamic_seek_questions(adapter, profile, Path(temp_dir) / "debug", input_func=lambda: "")
            self.assertEqual(result.status, "dynamic_questions_completed")
            self.assertEqual(adapter.dynamic_answers["questionnaire_education"], "Bachelor Degree")

    def test_is_review_page_false_for_role_requirements(self) -> None:
        page = FakeContinuePage(transition_mode="role_requirements")
        self.assertFalse(_is_review_page(page))

    def test_extract_question_text_handles_select_then_strong_ict_pattern(self) -> None:
        nearby_candidates = [
            "Yes, Diploma",
            "Yes, Advanced Diploma",
            "Yes, Associate Degree",
            "Yes, Bachelor Degree",
            "Yes, Bachelor Degree (Honours)",
            "Yes, Graduate Certificate",
            "Yes, Graduate Diploma",
            "Yes, Masters Degree",
            "Yes, Doctoral Degree",
            "I have a qualification in ICT which isn't listed",
            "I don't have a qualification in ICT",
            "Have you completed a qualification in ICT?",
        ]
        option_payloads = [{"label": candidate, "value": candidate} for candidate in nearby_candidates[:-1]]
        extracted = _extract_question_text_from_candidates(nearby_candidates, option_payloads)
        self.assertEqual(extracted, "Have you completed a qualification in ICT?")

    def test_extract_question_text_handles_select_then_strong_sql_data_analyst_pattern(self) -> None:
        nearby_candidates = [
            "No experience",
            "Less than 1 year",
            "1 year",
            "2 years",
            "3 years",
            "4 years",
            "5 years",
            "More than 5 years",
            "How many years' experience do you have as a SQL Data Analyst?",
        ]
        option_payloads = [{"label": candidate, "value": candidate} for candidate in nearby_candidates[:-1]]
        extracted = _extract_question_text_from_candidates(nearby_candidates, option_payloads)
        self.assertEqual(extracted, "How many years' experience do you have as a SQL Data Analyst?")

    def test_radio_group_prompt_above_yes_no_extracts_prompt_not_yes(self) -> None:
        nearby_candidates = [
            "As part of our selection process, shortlisted candidates will be asked to complete a psychometric assessment. Are you comfortable proceeding with this requirement?",
            "Yes",
            "No",
        ]
        option_payloads = [
            {"label": "Yes", "value": "yes"},
            {"label": "No", "value": "no"},
        ]
        details = _extract_question_text_details_from_candidates(nearby_candidates, option_payloads)
        self.assertEqual(
            details["text"],
            "As part of our selection process, shortlisted candidates will be asked to complete a psychometric assessment. Are you comfortable proceeding with this requirement?",
        )
        rejected_reasons = {entry["reason"] for entry in details["rejected_candidates"]}
        self.assertIn("matches option label", rejected_reasons)

    def test_radio_group_prompt_does_not_steal_neighboring_notice_question(self) -> None:
        nearby_candidates = [
            "As part of our selection process, shortlisted candidates will be asked to complete a psychometric assessment. Are you comfortable proceeding with this requirement?",
            "Yes",
            "No",
            "How much notice are you required to give your current employer?",
        ]
        option_payloads = [
            {"label": "Yes", "value": "yes"},
            {"label": "No", "value": "no"},
        ]
        details = _extract_question_text_details_from_candidates(nearby_candidates, option_payloads)
        self.assertEqual(
            details["text"],
            "As part of our selection process, shortlisted candidates will be asked to complete a psychometric assessment. Are you comfortable proceeding with this requirement?",
        )

    def test_local_question_extraction_keeps_each_fake_form_field_scoped(self) -> None:
        fake_form_fields = [
            {
                "field": "experience_select",
                "candidates": [
                    "How many years' experience do you have as a Payroll and Accounts Officer?",
                    "No experience",
                    "Less than 1 year",
                    "1 year",
                    "2 years",
                    "More than 5 years",
                ],
                "options": [
                    {"label": "No experience", "value": "none"},
                    {"label": "Less than 1 year", "value": "lt1"},
                    {"label": "1 year", "value": "1"},
                    {"label": "2 years", "value": "2"},
                    {"label": "More than 5 years", "value": "5plus"},
                ],
                "expected": "How many years' experience do you have as a Payroll and Accounts Officer?",
            },
            {
                "field": "notice_period_select",
                "candidates": [
                    "How much notice are you required to give your current employer?",
                    "Immediately",
                    "1 week",
                    "2 weeks",
                    "4 weeks",
                    "6 or more weeks",
                ],
                "options": [
                    {"label": "Immediately", "value": "0"},
                    {"label": "1 week", "value": "1"},
                    {"label": "2 weeks", "value": "2"},
                    {"label": "4 weeks", "value": "4"},
                    {"label": "6 or more weeks", "value": "6plus"},
                ],
                "expected": "How much notice are you required to give your current employer?",
            },
            {
                "field": "financial_discrepancy_textarea",
                "candidates": [
                    "Describe a time you identified a financial discrepancy or risk. What did you find, how did you handle it, and what was the outcome?",
                ],
                "options": [],
                "expected": "Describe a time you identified a financial discrepancy or risk. What did you find, how did you handle it, and what was the outcome?",
            },
            {
                "field": "payroll_textarea",
                "candidates": [
                    "Walk us through your experience processing payroll in a New Zealand environment. How many employees were you responsible for and how did you ensure accuracy and legislative compliance?",
                ],
                "options": [],
                "expected": "Walk us through your experience processing payroll in a New Zealand environment. How many employees were you responsible for and how did you ensure accuracy and legislative compliance?",
            },
        ]

        for field in fake_form_fields:
            with self.subTest(field=field["field"]):
                details = _extract_question_text_details_from_candidates(field["candidates"], field["options"])
                self.assertEqual(details["text"], field["expected"])
                self.assertTrue(details["source"])

    def test_local_question_extraction_rejects_option_labels_from_fake_notice_field(self) -> None:
        nearby_candidates = [
            "How much notice are you required to give your current employer?",
            "Immediately",
            "1 week",
            "2 weeks",
            "4 weeks",
            "6 or more weeks",
        ]
        option_payloads = [
            {"label": "Immediately", "value": "0"},
            {"label": "1 week", "value": "1"},
            {"label": "2 weeks", "value": "2"},
            {"label": "4 weeks", "value": "4"},
            {"label": "6 or more weeks", "value": "6plus"},
        ]
        details = _extract_question_text_details_from_candidates(nearby_candidates, option_payloads)
        self.assertEqual(details["text"], "How much notice are you required to give your current employer?")
        rejected_reasons = {entry["reason"] for entry in details["rejected_candidates"]}
        self.assertIn("matches option label", rejected_reasons)

    def test_dynamic_ict_select_prefers_bachelor_degree_when_available(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            adapter = FakeSeekAdapter(
                True,
                dynamic_questions=[
                    {
                        "name": "questionnaire_ict_select",
                        "field_type": "select",
                        "question_text": "Have you completed a qualification in ICT?",
                        "nearby_text_candidates": [
                            "Yes, Diploma",
                            "Yes, Bachelor Degree",
                            "I have a qualification in ICT which isn't listed",
                            "I don't have a qualification in ICT",
                            "Have you completed a qualification in ICT?",
                        ],
                        "options": [
                            "Yes, Diploma",
                            "Yes, Bachelor Degree",
                            "I have a qualification in ICT which isn't listed",
                            "I don't have a qualification in ICT",
                        ],
                        "option_payloads": [
                            {"label": "Yes, Diploma", "value": "diploma"},
                            {"label": "Yes, Bachelor Degree", "value": "bachelor"},
                            {"label": "I have a qualification in ICT which isn't listed", "value": "ict_other"},
                            {"label": "I don't have a qualification in ICT", "value": "no_ict"},
                        ],
                        "expected_answer": "Yes, Bachelor Degree",
                    }
                ],
            )
            result = handle_dynamic_seek_questions(adapter, DEFAULT_APPLICANT_PROFILE, Path(temp_dir) / "debug", input_func=lambda: "")
            self.assertEqual(result.status, "dynamic_questions_completed")
            self.assertEqual(adapter.dynamic_answers["questionnaire_ict_select"], "Yes, Bachelor Degree")

    def test_dynamic_sql_data_analyst_years_chooses_more_than_five_years(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            adapter = FakeSeekAdapter(
                True,
                dynamic_questions=[
                    {
                        "name": "questionnaire_sql_data_analyst",
                        "field_type": "select",
                        "question_text": "How many years' experience do you have as a SQL Data Analyst?",
                        "options": ["No experience", "Less than 1 year", "1 year", "2 years", "3 years", "4 years", "5 years", "More than 5 years"],
                        "expected_answer": "More than 5 years",
                    }
                ],
            )
            result = handle_dynamic_seek_questions(adapter, DEFAULT_APPLICANT_PROFILE, Path(temp_dir) / "debug", input_func=lambda: "")
            self.assertEqual(result.status, "dynamic_questions_completed")
            self.assertEqual(adapter.dynamic_answers["questionnaire_sql_data_analyst"], "More than 5 years")

    def test_dynamic_power_bi_analyst_years_chooses_more_than_five_years(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            adapter = FakeSeekAdapter(
                True,
                dynamic_questions=[
                    {
                        "name": "questionnaire_power_bi_analyst",
                        "field_type": "select",
                        "question_text": "How many years' experience do you have as a Power BI Analyst?",
                        "options": ["No experience", "Less than 1 year", "1 year", "2 years", "3 years", "4 years", "5 years", "More than 5 years"],
                        "expected_answer": "More than 5 years",
                    }
                ],
            )
            result = handle_dynamic_seek_questions(adapter, DEFAULT_APPLICANT_PROFILE, Path(temp_dir) / "debug", input_func=lambda: "")
            self.assertEqual(result.status, "dynamic_questions_completed")
            self.assertEqual(adapter.dynamic_answers["questionnaire_power_bi_analyst"], "More than 5 years")

    def test_dynamic_right_to_work_question_chooses_nz_citizen(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            adapter = FakeSeekAdapter(
                True,
                dynamic_questions=[
                    {
                        "name": "questionnaire_right_to_work",
                        "field_type": "select",
                        "question_text": "What is your right to work in New Zealand status?",
                        "options": ["Visa holder", "New Zealand citizen", "Resident"],
                        "expected_answer": "New Zealand citizen",
                    }
                ],
            )
            result = handle_dynamic_seek_questions(adapter, DEFAULT_APPLICANT_PROFILE, Path(temp_dir) / "debug", input_func=lambda: "")
            self.assertEqual(result.status, "dynamic_questions_completed")
            self.assertEqual(adapter.dynamic_answers["questionnaire_right_to_work"], "New Zealand citizen")

    def test_dynamic_nz_drivers_licence_question_chooses_yes(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            adapter = FakeSeekAdapter(
                True,
                dynamic_questions=[
                    {
                        "name": "questionnaire_drivers_licence",
                        "field_type": "select",
                        "question_text": "Do you have a current New Zealand driver's licence?",
                        "options": ["Yes", "No"],
                        "expected_answer": "Yes",
                    }
                ],
            )
            result = handle_dynamic_seek_questions(adapter, DEFAULT_APPLICANT_PROFILE, Path(temp_dir) / "debug", input_func=lambda: "")
            self.assertEqual(result.status, "dynamic_questions_completed")
            self.assertEqual(adapter.dynamic_answers["questionnaire_drivers_licence"], "Yes")

    def test_multiple_selects_are_all_processed_not_just_first(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            adapter = FakeSeekAdapter(
                True,
                dynamic_questions=[
                    {
                        "name": "questionnaire_ict",
                        "field_type": "select",
                        "question_text": "Have you completed a qualification in ICT?",
                        "options": ["Yes", "No"],
                        "expected_answer": "Yes",
                    },
                    {
                        "name": "questionnaire_sql_data_analyst",
                        "field_type": "select",
                        "question_text": "How many years' experience do you have as a SQL Data Analyst?",
                        "options": ["No experience", "1 year", "2 years", "3 years", "4 years", "5 years", "More than 5 years"],
                        "expected_answer": "More than 5 years",
                    },
                    {
                        "name": "questionnaire_power_bi_analyst",
                        "field_type": "select",
                        "question_text": "How many years' experience do you have as a Power BI Analyst?",
                        "options": ["No experience", "1 year", "2 years", "3 years", "4 years", "5 years", "More than 5 years"],
                        "expected_answer": "More than 5 years",
                    },
                    {
                        "name": "questionnaire_drivers_licence",
                        "field_type": "select",
                        "question_text": "Do you have a current New Zealand driver's licence?",
                        "options": ["Yes", "No"],
                        "expected_answer": "Yes",
                    },
                ],
            )
            result = handle_dynamic_seek_questions(adapter, DEFAULT_APPLICANT_PROFILE, Path(temp_dir) / "debug", input_func=lambda: "")
            self.assertEqual(result.status, "dynamic_questions_completed")
            self.assertEqual(result.answered_count, 4)
            self.assertEqual(adapter.dynamic_answers["questionnaire_ict"], "Yes")
            self.assertEqual(adapter.dynamic_answers["questionnaire_sql_data_analyst"], "More than 5 years")
            self.assertEqual(adapter.dynamic_answers["questionnaire_power_bi_analyst"], "More than 5 years")
            self.assertEqual(adapter.dynamic_answers["questionnaire_drivers_licence"], "Yes")

    def test_dynamic_question_scan_json_is_written_with_field_metadata(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            debug_dir = Path(temp_dir) / "debug"
            adapter = FakeSeekAdapter(
                True,
                dynamic_questions=[
                    {
                        "id": "questionnaire_sql",
                        "name": "questionnaire_sql_data_analyst",
                        "field_type": "select",
                        "question_text": "How many years' experience do you have as a SQL Data Analyst?",
                        "options": ["No experience", "More than 5 years"],
                        "option_payloads": [
                            {"label": "No experience", "value": "0"},
                            {"label": "More than 5 years", "value": "6_plus"},
                        ],
                        "expected_answer": "More than 5 years",
                    }
                ],
            )
            result = handle_dynamic_seek_questions(adapter, DEFAULT_APPLICANT_PROFILE, debug_dir, input_func=lambda: "")
            self.assertEqual(result.status, "dynamic_questions_completed")
            scan_path = debug_dir / "dynamic_question_scan.json"
            self.assertTrue(scan_path.exists())
            payload = __import__("json").loads(scan_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["fields_found"], 1)
            field = payload["fields"][0]
            self.assertEqual(field["field_name"], "questionnaire_sql_data_analyst")
            self.assertEqual(field["matched_rule"], "sql_data_analyst_years")
            self.assertEqual(field["chosen_answer"], "More than 5 years")
            self.assertEqual(field["chosen_value"], "6_plus")
            self.assertTrue(field["answer_success"])

    def test_unknown_dynamic_question_skips_without_pause_by_default(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            adapter = FakeSeekAdapter(
                True,
                dynamic_questions=[
                    {
                        "name": "questionnaire_unknown",
                        "field_type": "select",
                        "question_text": "What is your favourite colour?",
                        "options": ["Blue", "Green", "Red"],
                    }
                ],
            )
            result = handle_dynamic_seek_questions(adapter, DEFAULT_APPLICANT_PROFILE, Path(temp_dir) / "debug", input_func=_raise_eof)
            self.assertEqual(result.status, "dynamic_questions_completed")
            self.assertTrue(adapter.dynamic_unknown_saved)

    def test_required_unanswered_select_triggers_pause(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            adapter = FakeSeekAdapter(
                True,
                dynamic_questions=[],
                validation_errors=["Please make a selection"],
            )
            result = handle_dynamic_seek_questions(adapter, DEFAULT_APPLICANT_PROFILE, Path(temp_dir) / "debug", input_func=_raise_eof)
            self.assertEqual(result.status, "paused_for_manual_questionnaire")
            self.assertTrue(adapter.dynamic_unknown_saved)

    def test_seek_profile_dir_uses_persistent_data_browser_profile(self) -> None:
        db_path = Path(r"C:\temp\ai_job_apply_assistant\data\job_assistant.db")
        profile_dir = seek_profile_dir_for_db(db_path)
        self.assertEqual(profile_dir, Path(r"C:\temp\ai_job_apply_assistant\data\browser_profiles\seek_apply"))

    def test_seek_temp_profile_dir_uses_temp_browser_profile(self) -> None:
        db_path = Path(r"C:\temp\ai_job_apply_assistant\data\job_assistant.db")
        profile_dir = seek_temp_profile_dir_for_db(db_path)
        self.assertEqual(profile_dir, Path(r"C:\temp\ai_job_apply_assistant\data\browser_profiles\seek_apply_temp"))

    def test_configured_seek_browser_profile_dir_reads_single_profile_override(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            db_path = Path(temp_dir) / "data" / "job_assistant.db"
            db_path.parent.mkdir(parents=True, exist_ok=True)
            (db_path.parent / "applicant_profile.json").write_text(
                json.dumps({"automation": {"seek_browser_profile_path": r"C:\BrowserProfiles\SeekChrome"}}),
                encoding="utf-8",
            )
            self.assertEqual(
                configured_seek_browser_profile_dir(db_path),
                Path(r"C:\BrowserProfiles\SeekChrome"),
            )

    def test_configured_seek_browser_profile_dir_reads_bulk_profile_override(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            db_path = Path(temp_dir) / "data" / "job_assistant.db"
            db_path.parent.mkdir(parents=True, exist_ok=True)
            (db_path.parent / "applicant_profile.json").write_text(
                json.dumps({"automation": {"seek_bulk_browser_profile_path": r"C:\BrowserProfiles\SeekBulkChrome"}}),
                encoding="utf-8",
            )
            self.assertEqual(
                configured_seek_browser_profile_dir(db_path, use_bulk_profile=True),
                Path(r"C:\BrowserProfiles\SeekBulkChrome"),
            )

    def test_resolve_seek_browser_profile_launch_settings_splits_chrome_profile_path(self) -> None:
        user_data_dir, launch_args = resolve_seek_browser_profile_launch_settings(
            Path(r"C:\Users\ExampleUser\AppData\Local\Google\Chrome\User Data\Default")
        )
        self.assertEqual(user_data_dir, Path(r"C:\Users\ExampleUser\AppData\Local\Google\Chrome\User Data"))
        self.assertEqual(launch_args, ["--profile-directory=Default"])

    def test_seed_isolated_seek_browser_profile_from_configured_profile_copies_local_state_and_profile_dir(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            db_path = Path(temp_dir) / "data" / "job_assistant.db"
            db_path.parent.mkdir(parents=True, exist_ok=True)
            configured_root = Path(temp_dir) / "chrome-user-data"
            configured_root.mkdir(parents=True, exist_ok=True)
            (configured_root / "Local State").write_text("local-state", encoding="utf-8")
            profile_dir = configured_root / "Profile 2"
            profile_dir.mkdir(parents=True, exist_ok=True)
            (profile_dir / "Cookies").write_text("cookie-data", encoding="utf-8")
            (db_path.parent / "applicant_profile.json").write_text(
                json.dumps({"automation": {"seek_browser_profile_path": str(profile_dir)}}),
                encoding="utf-8",
            )

            seeded_dir, launch_args = seed_isolated_seek_browser_profile_from_configured_profile(db_path)

            self.assertEqual(launch_args, ["--profile-directory=Profile 2"])
            self.assertTrue((seeded_dir / "Local State").exists())
            self.assertEqual((seeded_dir / "Profile 2" / "Cookies").read_text(encoding="utf-8"), "cookie-data")

    def test_preferred_seek_browser_channels_defaults_to_chrome_then_edge_then_chromium(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            channels = preferred_seek_browser_channels(Path(temp_dir))
            self.assertEqual(channels[:3], ["chrome", "msedge", "chromium"])

    def test_preferred_seek_browser_channels_normalises_profile_setting(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            data_dir = Path(temp_dir)
            (data_dir / "applicant_profile.json").write_text(
                json.dumps({"automation": {"seek_browser_channels": ["edge", "chrome"]}}),
                encoding="utf-8",
            )
            channels = preferred_seek_browser_channels(data_dir)
            self.assertEqual(channels[:3], ["msedge", "chrome", "chromium"])

    def test_seed_temp_seek_profile_from_logged_in_profile_copies_seed_files(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            db_path = Path(temp_dir) / "data" / "job_assistant.db"
            seed_dir = seek_profile_dir_for_db(db_path)
            seed_dir.mkdir(parents=True, exist_ok=True)
            (seed_dir / "Local State").write_text("seed-state", encoding="utf-8")
            (seed_dir / "Default").mkdir(parents=True, exist_ok=True)
            (seed_dir / "Default" / "Cookies").write_text("cookie-data", encoding="utf-8")

            temp_profile_dir = seed_temp_seek_profile_from_logged_in_profile(db_path)

            self.assertTrue((temp_profile_dir / "Local State").exists())
            self.assertEqual((temp_profile_dir / "Default" / "Cookies").read_text(encoding="utf-8"), "cookie-data")

    def test_seed_temp_seek_profile_from_logged_in_profile_ignores_lock_files(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            db_path = Path(temp_dir) / "data" / "job_assistant.db"
            seed_dir = seek_profile_dir_for_db(db_path)
            seed_dir.mkdir(parents=True, exist_ok=True)
            (seed_dir / "SingletonLock").write_text("locked", encoding="utf-8")
            (seed_dir / "Default").mkdir(parents=True, exist_ok=True)
            (seed_dir / "Default" / "Preferences").write_text("prefs", encoding="utf-8")

            temp_profile_dir = seed_temp_seek_profile_from_logged_in_profile(db_path)

            self.assertFalse((temp_profile_dir / "SingletonLock").exists())
            self.assertTrue((temp_profile_dir / "Default" / "Preferences").exists())

    def test_profile_lock_detection_checks_common_lock_files(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            profile_dir = Path(temp_dir) / "browser_profiles" / "seek_apply"
            profile_dir.mkdir(parents=True, exist_ok=True)
            self.assertFalse(seek_profile_appears_locked(profile_dir))
            (profile_dir / "SingletonLock").write_text("locked", encoding="utf-8")
            self.assertTrue(seek_profile_appears_locked(profile_dir))

    def test_playwright_seek_adapter_detects_verifying_interstitial_as_seek_verification(self) -> None:
        adapter = _PlaywrightSeekAdapter.__new__(_PlaywrightSeekAdapter)
        adapter.current_url = lambda: "https://login.seek.com/code"
        adapter.page_text = lambda: "Verifying... Stuck? Troubleshoot"
        self.assertEqual(adapter.detect_pause_reason(), "paused_for_seek_verification")

    def test_get_seek_pause_diagnostics_captures_verification_token(self) -> None:
        adapter = FakeSeekAdapter(True, pause_reason="paused_for_seek_verification", page_text_value="Verifying... Stuck? Troubleshoot")
        adapter.url = "https://login.seek.com/code"
        diagnostics = _get_seek_pause_diagnostics(adapter)
        self.assertEqual(diagnostics["pause_reason"], "paused_for_seek_verification")
        self.assertEqual(diagnostics["matched_source"], "page_text")
        self.assertEqual(diagnostics["matched_token"], "verifying...")
        self.assertIn("Verifying", diagnostics["page_text_snippet"])

    def test_browser_profile_locked_error_returns_browser_profile_locked_status(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            root = Path(temp_dir)
            database, job_id, _ = self._build_seek_job(root)

            def failing_factory(visible: bool) -> FakeSeekAdapter:
                raise SeekBrowserProfileLockedError(PROFILE_LOCKED_MESSAGE)

            result = _assist_seek_apply(
                job_id,
                database,
                visible=True,
                adapter_factory=failing_factory,
                input_func=lambda: "",
            )
            self.assertEqual(result.status, "browser_profile_locked")
            self.assertIn("Close all SEEK/Chromium windows and retry", result.error)

    def test_quick_apply_login_redirect_returns_paused_for_seek_login(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            root = Path(temp_dir)
            database, job_id, _ = self._build_seek_job(root)

            class LoginAfterQuickApplyAdapter(FakeSeekAdapter):
                def __init__(self) -> None:
                    super().__init__(True, radio_available=True, has_file_input=True)
                    self._pause_checks = 0

                def detect_pause_reason(self) -> str | None:
                    self._pause_checks += 1
                    return "paused_for_seek_login" if self._pause_checks >= 2 else None

            adapter = LoginAfterQuickApplyAdapter()
            result = _assist_seek_apply(
                job_id,
                database,
                visible=True,
                adapter_factory=lambda visible: adapter,
                input_func=lambda: "",
            )
            self.assertEqual(result.status, "paused_for_seek_login")
            self.assertEqual(adapter.upload_attempts, 0)

    def test_initial_seek_verification_pause_preserves_paused_status(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            root = Path(temp_dir)
            database, job_id, _ = self._build_seek_job(root)
            adapter = FakeSeekAdapter(
                True,
                pause_reason="paused_for_seek_verification",
                page_text_value="Verifying... Stuck? Troubleshoot",
            )
            result = _assist_seek_apply(
                job_id,
                database,
                visible=True,
                adapter_factory=lambda visible: adapter,
                input_func=lambda: "",
            )
            self.assertEqual(result.status, "paused_for_seek_verification")
            self.assertIn("verification", result.error.lower())

    def test_closed_seek_job_is_skipped_before_quick_apply(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            root = Path(temp_dir)
            database, job_id, _ = self._build_seek_job(root)
            adapter = FakeSeekAdapter(
                True,
                page_text_value="This job is no longer advertised Jobs remain on SEEK for 30 days, unless the advertiser removes them sooner.",
            )
            result = _assist_seek_apply(
                job_id,
                database,
                visible=True,
                adapter_factory=lambda visible: adapter,
                input_func=lambda: "",
            )
            self.assertEqual(result.status, "skipped")
            self.assertIn("no longer advertised", result.error.lower())
            self.assertNotIn("clicked_quick_apply", result.steps_completed)

    def test_closed_seek_job_is_skipped_when_quick_apply_missing(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            root = Path(temp_dir)
            database, job_id, _ = self._build_seek_job(root)
            adapter = FakeSeekAdapter(
                True,
                page_text_value="This job is no longer advertised Jobs remain on SEEK for 30 days, unless the advertiser removes them sooner.",
                quick_apply_error="Could not find SEEK Quick apply button.",
            )
            result = _assist_seek_apply(
                job_id,
                database,
                visible=True,
                adapter_factory=lambda visible: adapter,
                input_func=lambda: "",
            )
            self.assertEqual(result.status, "skipped")
            self.assertIn("no longer advertised", result.error.lower())
            self.assertNotIn("clicked_quick_apply", result.steps_completed)

    def test_login_page_triggers_manual_pause_when_no_input_available(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            root = Path(temp_dir)
            database, job_id, _ = self._build_seek_job(root)
            adapter = FakeSeekAdapter(True, pause_reason="paused_for_seek_login", radio_available=True)
            result = _assist_seek_apply(
                job_id,
                database,
                visible=True,
                adapter_factory=lambda visible: adapter,
                input_func=_raise_eof,
            )
            self.assertEqual(result.status, "paused_for_seek_login")
            self.assertFalse(adapter.password_fill_attempted)

    def test_login_page_resumes_after_manual_confirmation(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            root = Path(temp_dir)
            database, job_id, _ = self._build_seek_job(root)
            adapter = FakeSeekAdapter(True, pause_reason="paused_for_seek_login")
            adapter._page = object()
            state = {"first": True}

            def detect_pause_reason() -> str | None:
                if state["first"]:
                    state["first"] = False
                    return "paused_for_seek_login"
                return None

            adapter.detect_pause_reason = detect_pause_reason
            result = _assist_seek_apply(
                job_id,
                database,
                visible=True,
                adapter_factory=lambda visible: adapter,
                input_func=lambda: "",
            )
            self.assertEqual(result.status, "prepared_for_manual_review")
            self.assertIn("resumed_after_manual_login", result.steps_completed)
            self.assertFalse(adapter.password_fill_attempted)
            self.assertTrue(adapter.close_called)

    def test_login_page_resumes_after_auto_email_code_login(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            root = Path(temp_dir)
            database, job_id, _ = self._build_seek_job(root)
            adapter = FakeSeekAdapter(True, pause_reason="paused_for_seek_login", radio_available=True)

            class DummyLoginPage:
                url = "https://www.seek.co.nz/job/12345678/apply"

                def title(self) -> str:
                    return "Choose documents | SEEK"

                def content(self) -> str:
                    return "<html></html>"

            adapter._page = DummyLoginPage()
            state = {"first": True}

            def detect_pause_reason() -> str | None:
                if state["first"]:
                    state["first"] = False
                    return "paused_for_seek_login"
                return None

            adapter.detect_pause_reason = detect_pause_reason
            with mock.patch(
                "src.seek_apply_assist._attempt_seek_email_code_login",
                return_value=(True, "SEEK bulk profile login completed automatically with Gmail sign-in code."),
            ):
                result = _assist_seek_apply(
                    job_id,
                    database,
                    visible=True,
                    adapter_factory=lambda visible: adapter,
                    input_func=_raise_eof,
                )
            self.assertNotEqual(result.status, "paused_for_seek_login")
            self.assertIn("resumed_after_manual_login", result.steps_completed)

    def test_unsupported_source_fails_safely(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            root = Path(temp_dir)
            database = Database(root / "data" / "job_assistant.db")
            importer = JobImporter(database)
            result = importer.import_manual_job(
                source="linkedin",
                url="https://www.linkedin.com/jobs/view/123",
                title="Data Analyst",
                company="Example Co",
                location="Auckland",
                salary="$100,000",
                description="SQL and reporting role.",
                import_channel="test",
            )
            database.update_job_career_assets(
                result["job_id"],
                selected_cv_path=str(root / "cv.docx"),
                cover_letter_preview="Example cover letter",
            )
            Path(root / "cv.docx").write_text("cv", encoding="utf-8")
            assist_result = _assist_seek_apply(result["job_id"], database, visible=True, adapter_factory=lambda visible: FakeSeekAdapter(visible))
            self.assertEqual(assist_result.status, "failed")
            self.assertIn("SEEK Apply Assist only supports SEEK jobs", assist_result.error)

    def _build_seek_job(self, root: Path, *, include_cover_letter: bool = True) -> tuple[Database, int, Path]:
        data_dir = root / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        database = Database(data_dir / "job_assistant.db")
        importer = JobImporter(database)
        result = importer.import_manual_job(
            source="seek",
            url="https://www.seek.co.nz/job/12345678",
            title="BI Analyst",
            company="Example Co",
            location="Auckland",
            salary="$100,000",
            description="Power BI, SQL, stakeholder reporting, and dashboard delivery role.",
            import_channel="test",
        )
        cv_path = root / "applications" / "1" / "selected_cv.docx"
        cv_path.parent.mkdir(parents=True, exist_ok=True)
        cv_path.write_text("fake cv", encoding="utf-8")
        upload_dir = root / "data" / "temp_uploads"
        upload_dir.mkdir(parents=True, exist_ok=True)
        upload_cv_path = upload_dir / "Example_Candidate_CV.docx"
        upload_cv_path.write_text("fake cv", encoding="utf-8")
        database.update_job_career_assets(
            result["job_id"],
            selected_cv_path=str(upload_cv_path),
            cover_letter_preview="Example cover letter tailored for SEEK." if include_cover_letter else None,
            role_family="business_intelligence",
        )
        return database, result["job_id"], cv_path

if __name__ == "__main__":
    unittest.main()
