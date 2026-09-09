from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.seek_apply_assist import (  # noqa: E402
    _build_playwright_adapter,
    _choose_resume_limit_delete_candidate,
    _resume_upload_diagnostics,
    wait_for_resume_filename_after_upload,
)


def _now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def default_debug_dir() -> Path:
    return PROJECT_ROOT / "data" / "debug_apply_assist" / "resume_debug_runs" / _now_stamp()


def _safe(callable_obj, default: Any = None) -> Any:
    try:
        return callable_obj()
    except Exception:
        return default


class HarnessLogger:
    def __init__(self, debug_dir: Path) -> None:
        self.debug_dir = debug_dir
        self.lines: list[str] = []

    def log(self, message: str) -> None:
        print(message, flush=True)
        self.lines.append(message)
        log_path = self.debug_dir / "harness.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("\n".join(self.lines), encoding="utf-8")


def _file_input_details(page: Any) -> list[dict[str, Any]]:
    details: list[dict[str, Any]] = []
    count = _safe(lambda: page.locator('input[type="file"]').count(), 0) or 0
    for index in range(count):
        locator = page.locator('input[type="file"]').nth(index)
        details.append(
            {
                "index": index,
                "visible": _safe(lambda: bool(locator.is_visible()), False),
                "attached": True,
                "disabled": _safe(lambda: bool(locator.is_disabled()), False),
                "accept": _safe(lambda: str(locator.get_attribute("accept") or ""), ""),
                "outer_html_snippet": _safe(lambda: str(locator.evaluate("el => el.outerHTML || ''") or "")[:240], ""),
            }
        )
    return details


def _visible_resume_filenames(page: Any) -> list[str]:
    body_text = _safe(lambda: str(page.locator("body").inner_text(timeout=5000) or ""), "")
    return re.findall(r"\b([A-Za-z0-9._ -]+\.(?:doc|docx|pdf))\b", body_text, re.I)


def _button_candidate_count(page: Any, text_pattern: str) -> int:
    pattern = re.compile(text_pattern, re.I)
    total = 0
    for factory in (
        lambda: page.get_by_role("button", name=pattern),
        lambda: page.get_by_text(pattern),
        lambda: page.locator(
            f'button:has-text("{text_pattern.split("|")[0]}"), '
            f'[role="button"]:has-text("{text_pattern.split("|")[0]}"), '
            f'a:has-text("{text_pattern.split("|")[0]}")'
        ),
    ):
        total += _safe(lambda: factory().count(), 0) or 0
    return total


def save_checkpoint(page: Any, debug_dir: Path, name: str, logger: HarnessLogger) -> None:
    debug_dir.mkdir(parents=True, exist_ok=True)
    screenshot_path = debug_dir / f"{name}.png"
    html_path = debug_dir / f"{name}.html"
    json_path = debug_dir / f"{name}.json"
    _safe(lambda: page.screenshot(path=str(screenshot_path), full_page=True))
    html_path.write_text(_safe(lambda: page.content(), "<html></html>"), encoding="utf-8")
    body_text = _safe(lambda: str(page.locator("body").inner_text(timeout=5000) or ""), "")
    state = {
        "checkpoint": name,
        "current_url": _safe(lambda: str(page.url or ""), ""),
        "page_title": _safe(lambda: str(page.title() or ""), ""),
        "visible_body_text_snippet": body_text[:1200],
        "selected_resume_method": _safe(
            lambda: page.evaluate(
                """() => {
                    const checked = document.querySelector('input[name="resume-method"]:checked');
                    return checked ? checked.value : '';
                }"""
            ),
            "",
        ),
        "upload_radio_checked": _safe(
            lambda: bool(page.locator('input[data-testid="resume-method-upload"]').first.is_checked()),
            False,
        ),
        "dont_include_radio_checked": _safe(
            lambda: bool(
                page.locator('input[name="resume-method"][value*="without"], input[name="resume-method"][value*="none"]').first.is_checked()
            ),
            False,
        ),
        "file_input_count": _safe(lambda: page.locator('input[type="file"]').count(), 0),
        "file_inputs": _file_input_details(page),
        "doc_limit_dropdown_exists": _safe(lambda: bool(page.locator("#docLimitExceededDropdown").count()), False),
        "dropdown_options": _safe(
            lambda: [
                str(page.locator("#docLimitExceededDropdown option").nth(index).inner_text(timeout=500) or "").strip()
                for index in range(page.locator("#docLimitExceededDropdown option").count())
            ],
            [],
        ),
        "visible_resume_filenames": _visible_resume_filenames(page),
        "upload_button_candidate_count": _button_candidate_count(page, "Upload|Upload resume|Upload a resume|Upload a resumé"),
        "delete_button_candidate_count": _button_candidate_count(page, "Delete|Confirm"),
        "continue_button_candidate_count": _button_candidate_count(page, "Continue"),
    }
    json_path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.log(f"[debug_seek_resume_upload] checkpoint_saved: {name} -> {json_path}")


def run_debug_seek_resume_upload(
    *,
    job_url: str,
    cv_path: str | Path,
    visible: bool = True,
    stop_after_resume: bool = False,
    slow_mo_ms: int = 500,
    debug_dir: str | Path | None = None,
) -> dict[str, Any]:
    resolved_debug_dir = Path(debug_dir) if debug_dir else default_debug_dir()
    resolved_debug_dir.mkdir(parents=True, exist_ok=True)
    logger = HarnessLogger(resolved_debug_dir)
    summary_path = resolved_debug_dir / "final_summary.json"

    adapter = _build_playwright_adapter(
        bool(visible),
        PROJECT_ROOT / "data" / "job_assistant.db",
        slow_mo_ms=max(0, int(slow_mo_ms)),
    )
    page = getattr(adapter, "_page", None)
    if page is None:
        raise RuntimeError("Playwright page was not available on the SEEK adapter.")

    final_status = "failed"
    failure_reason = ""
    final_checkpoint = ""
    exit_code = 1
    try:
        adapter.open_job(str(job_url))
        save_checkpoint(page, resolved_debug_dir, "01_job_opened", logger)

        adapter.click_quick_apply()
        save_checkpoint(page, resolved_debug_dir, "02_quick_apply_clicked", logger)

        adapter.wait_for_application_form()
        adapter.wait_for_resume_section(timeout_ms=10000)
        save_checkpoint(page, resolved_debug_dir, "03_choose_documents_loaded", logger)

        if not adapter.click_resume_upload_radio():
            raise RuntimeError("Could not select Upload a resume.")
        save_checkpoint(page, resolved_debug_dir, "04_upload_radio_selected", logger)

        save_checkpoint(page, resolved_debug_dir, "05_before_upload_click", logger)
        chooser_uploaded = adapter.try_resume_upload_via_file_chooser(Path(cv_path), timeout_ms=5000)
        save_checkpoint(page, resolved_debug_dir, "06_after_upload_click", logger)

        if adapter.resume_limit_ui_present():
            save_checkpoint(page, resolved_debug_dir, "07_resume_limit_detected", logger)
            options = adapter.get_resume_limit_dropdown_options()
            candidate = _choose_resume_limit_delete_candidate(options, protected_filename=Path(cv_path).name)
            if candidate:
                adapter.select_resume_limit_dropdown_option(candidate)
                save_checkpoint(page, resolved_debug_dir, "08_resume_limit_dropdown_selected", logger)
            if adapter.click_resume_limit_delete():
                save_checkpoint(page, resolved_debug_dir, "09_resume_limit_delete_clicked", logger)
            adapter.confirm_resume_limit_delete()
            save_checkpoint(page, resolved_debug_dir, "10_resume_limit_delete_confirmed", logger)
            adapter.wait_for_resume_limit_change(options, timeout_ms=5000)
            save_checkpoint(page, resolved_debug_dir, "11_after_resume_limit_closed", logger)
            if not adapter.click_resume_upload_radio():
                logger.log("[debug_seek_resume_upload] upload radio reselect did not succeed after delete.")
            chooser_uploaded = adapter.try_resume_upload_via_file_chooser(Path(cv_path), timeout_ms=5000)
            save_checkpoint(page, resolved_debug_dir, "12_retry_upload_clicked", logger)

        if not chooser_uploaded:
            if not adapter.wait_for_resume_file_input(timeout_ms=5000):
                raise RuntimeError("No file input appeared after Upload click.")
            save_checkpoint(page, resolved_debug_dir, "13_file_input_detected", logger)
            adapter.upload_resume(Path(cv_path))
            save_checkpoint(page, resolved_debug_dir, "14_after_set_input_files", logger)
        else:
            save_checkpoint(page, resolved_debug_dir, "14_after_set_input_files", logger)

        filename = wait_for_resume_filename_after_upload(adapter, resolved_debug_dir, timeout_ms=10000)
        if not filename:
            raise RuntimeError("Uploaded filename never became visible.")
        save_checkpoint(page, resolved_debug_dir, "15_filename_visible", logger)

        if stop_after_resume:
            final_status = "success"
            final_checkpoint = "15_filename_visible"
            exit_code = 0
        else:
            save_checkpoint(page, resolved_debug_dir, "16_before_continue", logger)
            _safe(lambda: page.get_by_role("button", name=re.compile("Continue", re.I)).first.click())
            _safe(lambda: page.wait_for_timeout(2000))
            save_checkpoint(page, resolved_debug_dir, "17_after_continue", logger)
            final_status = "success"
            final_checkpoint = "17_after_continue"
            exit_code = 0
    except Exception as exc:  # noqa: BLE001
        failure_reason = str(exc)
        checkpoint_name = f"failure_{re.sub(r'[^A-Za-z0-9_]+', '_', failure_reason)[:60] or 'unknown'}"
        save_checkpoint(page, resolved_debug_dir, checkpoint_name, logger)
        final_checkpoint = checkpoint_name
        logger.log(f"[debug_seek_resume_upload] failure: {failure_reason}")
    finally:
        summary = {
            "status": final_status,
            "failure_reason": failure_reason,
            "debug_dir": str(resolved_debug_dir),
            "final_checkpoint": final_checkpoint,
            "resume_upload_diagnostics": _resume_upload_diagnostics(adapter),
            "log_lines": logger.lines[-20:],
            "exit_code": exit_code,
        }
        summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
        adapter.close()
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Debug SEEK resume upload flow only.")
    parser.add_argument("--job-url", required=True)
    parser.add_argument("--cv-path", required=True)
    parser.add_argument("--visible", action="store_true")
    parser.add_argument("--stop-after-resume", action="store_true")
    parser.add_argument("--slow-mo-ms", type=int, default=500)
    parser.add_argument("--debug-dir", default="")
    args = parser.parse_args()
    summary = run_debug_seek_resume_upload(
        job_url=args.job_url,
        cv_path=args.cv_path,
        visible=bool(args.visible),
        stop_after_resume=bool(args.stop_after_resume),
        slow_mo_ms=int(args.slow_mo_ms),
        debug_dir=args.debug_dir or None,
    )
    return int(summary.get("exit_code", 1))


if __name__ == "__main__":
    raise SystemExit(main())
