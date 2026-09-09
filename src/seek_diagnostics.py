from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Callable


def _save_resume_failure_json(debug_dir: Path, stem: str, payload: dict[str, Any]) -> Path:
    debug_dir.mkdir(parents=True, exist_ok=True)
    json_path = debug_dir / f"{stem}.json"
    try:
        json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    return json_path


def _safe_page_screenshot(page: Any, path: Path, *, timeout_ms: int = 3000) -> tuple[bool, str]:
    try:
        page.screenshot(path=str(path), full_page=True, timeout=timeout_ms)
        return True, ""
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def _safe_page_html_write(page: Any, path: Path) -> tuple[bool, str]:
    try:
        html = ""
        locator_method = getattr(page, "locator", None)
        if callable(locator_method):
            html = str(
                locator_method("html").first.evaluate(
                    "el => el.outerHTML",
                    timeout=2000,
                )
                or ""
            )
        if not html:
            content_method = getattr(page, "content", None)
            if callable(content_method):
                html = str(content_method() or "")
        path.write_text(html, encoding="utf-8")
        return True, ""
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def _save_verification_failure_artifacts(
    debug_dir: Path,
    page: Any,
    *,
    stem: str,
    payload: dict[str, Any],
) -> tuple[Path, Path, Path, dict[str, Any]]:
    debug_dir.mkdir(parents=True, exist_ok=True)
    screenshot_path = debug_dir / f"{stem}.png"
    html_path = debug_dir / f"{stem}.html"
    capture_errors: list[str] = []
    screenshot_saved, screenshot_error = _safe_page_screenshot(page, screenshot_path)
    if screenshot_error:
        capture_errors.append(f"screenshot: {screenshot_error}")
    html_saved, html_error = _safe_page_html_write(page, html_path)
    if html_error:
        capture_errors.append(f"html: {html_error}")
    capture_payload = dict(payload)
    capture_payload["evidence_capture_errors"] = capture_errors
    capture_payload["screenshot_saved"] = screenshot_saved
    capture_payload["html_saved"] = html_saved
    capture_payload["json_saved"] = True
    json_path = _save_resume_failure_json(debug_dir, stem, capture_payload)
    return screenshot_path, html_path, json_path, {
        "evidence_capture_errors": capture_errors,
        "screenshot_saved": screenshot_saved,
        "html_saved": html_saved,
        "json_saved": True,
    }


def _write_local_dom_codex_prompt(
    diagnostics_dir: Path,
    *,
    failure_type: str,
    root_failure_reason: str,
    local_state: dict[str, Any],
    artifact_paths: dict[str, str],
) -> Path:
    diagnostics_dir.mkdir(parents=True, exist_ok=True)
    prompt_path = diagnostics_dir / "codex_prompt.txt"
    spinner_details = list(local_state.get("local_spinner_details") or [])
    nearby_buttons = list(local_state.get("nearby_buttons") or [])
    nearby_inputs = list(local_state.get("nearby_inputs") or [])
    nearby_labels = list(local_state.get("nearby_labels") or [])
    lines = [
        f"SEEK local DOM diagnostics for failure_type={failure_type}",
        "",
        f"Root failure reason: {root_failure_reason}",
        "",
        "Observed local state:",
        f"- filename_visible: {local_state.get('filename_visible')}",
        f"- upload_radio_checked: {local_state.get('upload_radio_checked')}",
        f"- dont_include_checked: {local_state.get('dont_include_checked')}",
        f"- continue_enabled: {local_state.get('continue_enabled')}",
        f"- local_spinner_count: {local_state.get('local_spinner_count')}",
        f"- parent_container_selector_guess: {local_state.get('parent_container_selector_guess')}",
        "",
        "Local spinner/progress elements:",
        json.dumps(spinner_details, indent=2, ensure_ascii=False) if spinner_details else "(none)",
        "",
        "Nearby buttons:",
        json.dumps(nearby_buttons, indent=2, ensure_ascii=False) if nearby_buttons else "(none)",
        "",
        "Nearby inputs:",
        json.dumps(nearby_inputs, indent=2, ensure_ascii=False) if nearby_inputs else "(none)",
        "",
        "Nearby labels:",
        json.dumps(nearby_labels, indent=2, ensure_ascii=False) if nearby_labels else "(none)",
        "",
        "Artifacts:",
        *(f"- {name}: {path}" for name, path in artifact_paths.items()),
        "",
        "Task:",
        "Determine whether the local spinner/progress state represents a real upload in progress or a decorative/stale UI element, and propose a selector or state-machine fix that keeps SEEK Apply Assist safe.",
    ]
    prompt_path.write_text("\n".join(lines), encoding="utf-8")
    return prompt_path


def capture_local_dom_diagnostics(
    page: Any,
    locator: Any,
    failure_type: str,
    *,
    debug_dir: Path,
    adapter: Any | None = None,
    expected_filename: str = "",
    root_failure_reason: str = "",
    extra: dict[str, Any] | None = None,
    get_resume_method_state: Callable[[Any], dict[str, Any]] | None = None,
    verify_resume_filename: Callable[[Any], str] | None = None,
) -> dict[str, Any]:
    diagnostics_dir = debug_dir / "dom_diagnostics"
    diagnostics_dir.mkdir(parents=True, exist_ok=True)
    full_page_path = diagnostics_dir / "full_page.png"
    local_png_path = diagnostics_dir / "local_container.png"
    local_html_path = diagnostics_dir / "local_container.html"
    local_text_path = diagnostics_dir / "local_container.txt"
    local_json_path = diagnostics_dir / "local_state.json"
    capture_errors: list[str] = []

    full_page_saved, full_page_error = _safe_page_screenshot(page, full_page_path)
    if full_page_error:
        capture_errors.append(f"full_page_screenshot: {full_page_error}")

    local_state: dict[str, Any] = {
        "failure_type": failure_type,
        "filename_visible": False,
        "upload_radio_checked": False,
        "dont_include_checked": False,
        "continue_enabled": False,
        "local_spinner_count": 0,
        "local_spinner_details": [],
        "parent_container_selector_guess": "",
        "nearby_buttons": [],
        "nearby_inputs": [],
        "nearby_labels": [],
        "nearby_validation_text": [],
        "local_container_html": "",
        "local_container_text": "",
        "capture_errors": [],
    }
    if adapter is not None and get_resume_method_state is not None and verify_resume_filename is not None:
        method_state = get_resume_method_state(adapter)
        local_state["upload_radio_checked"] = bool(method_state.get("upload_radio_checked"))
        local_state["dont_include_checked"] = bool(method_state.get("dont_include_resume_radio_checked"))
        local_state["filename_visible"] = bool(verify_resume_filename(adapter))

    collector = getattr(adapter, "capture_local_dom_diagnostics", None) if adapter is not None else None
    if callable(collector):
        try:
            collected = dict(collector(failure_type, expected_filename) or {})
            local_state.update(collected)
        except Exception as exc:  # noqa: BLE001
            capture_errors.append(f"capture_local_dom_diagnostics: {exc}")

    local_html = str(local_state.get("local_container_html") or "")
    local_text = str(local_state.get("local_container_text") or "")
    try:
        local_html_path.write_text(local_html, encoding="utf-8")
        html_saved = True
    except Exception as exc:  # noqa: BLE001
        html_saved = False
        capture_errors.append(f"local_html: {exc}")
    try:
        local_text_path.write_text(local_text, encoding="utf-8")
        text_saved = True
    except Exception as exc:  # noqa: BLE001
        text_saved = False
        capture_errors.append(f"local_text: {exc}")

    cropped_saved = False
    screenshot_saver = getattr(adapter, "save_local_dom_screenshot", None) if adapter is not None else None
    if callable(screenshot_saver):
        try:
            cropped_saved, cropped_error = screenshot_saver(
                local_png_path,
                failure_type=failure_type,
                expected_filename=expected_filename,
            )
            if cropped_error:
                capture_errors.append(f"cropped_screenshot: {cropped_error}")
        except Exception as exc:  # noqa: BLE001
            capture_errors.append(f"cropped_screenshot: {exc}")
    elif locator is not None:
        try:
            locator.screenshot(path=str(local_png_path), timeout=3000)
            cropped_saved = True
        except Exception as exc:  # noqa: BLE001
            capture_errors.append(f"cropped_screenshot: {exc}")

    if extra:
        local_state.update(extra)
    local_state["capture_errors"] = capture_errors
    local_state["full_page_screenshot_saved"] = full_page_saved
    local_state["local_container_screenshot_saved"] = cropped_saved
    local_state["html_saved"] = html_saved
    local_state["text_saved"] = text_saved
    local_json_path.write_text(json.dumps(local_state, indent=2, ensure_ascii=False), encoding="utf-8")

    artifact_paths = {
        "full_page": str(full_page_path),
        "local_container_png": str(local_png_path),
        "local_container_html": str(local_html_path),
        "local_container_txt": str(local_text_path),
        "local_state_json": str(local_json_path),
    }
    prompt_path = _write_local_dom_codex_prompt(
        diagnostics_dir,
        failure_type=failure_type,
        root_failure_reason=root_failure_reason,
        local_state=local_state,
        artifact_paths=artifact_paths,
    )
    artifact_paths["codex_prompt"] = str(prompt_path)
    return {
        "diagnostics_dir": str(diagnostics_dir),
        "local_state_json": str(local_json_path),
        "local_container_html": str(local_html_path),
        "local_container_text": str(local_text_path),
        "local_container_png": str(local_png_path),
        "full_page_png": str(full_page_path),
        "codex_prompt": str(prompt_path),
        "evidence_capture_errors": capture_errors,
        "screenshot_saved": full_page_saved,
        "cropped_screenshot_saved": cropped_saved,
        "html_saved": html_saved,
        "text_saved": text_saved,
    }


def _write_final_review_checklist(debug_dir: Path, checklist: Any, extra: dict[str, Any] | None = None) -> Path:
    debug_dir.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = checklist.as_dict()
    payload["all_checks_passed"] = checklist.all_true()
    if extra:
        payload.update(extra)
    path = debug_dir / "final_review_checklist.json"
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[seek_apply_assist] final_review_checklist_json: {path}", flush=True)
    return path


def _save_resume_section_dom_snapshot(
    debug_dir: Path,
    checkpoint: str,
    adapter: Any,
    *,
    expected_filename: str = "",
    extra: dict[str, Any] | None = None,
    snapshots_enabled: Callable[[Path], bool] | None = None,
    collect_resume_section_state: Callable[[Any, str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if snapshots_enabled is not None and not snapshots_enabled(debug_dir):
        return {}
    snapshot_dir = debug_dir / f"resume_section_{checkpoint}"
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    screenshot_path = snapshot_dir / "resume_section_screenshot.png"
    html_path = snapshot_dir / "resume_section.html"
    text_path = snapshot_dir / "resume_section_text.txt"
    json_path = snapshot_dir / "resume_section_state.json"
    state = collect_resume_section_state(adapter, expected_filename) if collect_resume_section_state is not None else {}
    if extra:
        state.update(extra)
    capture_errors = list(state.get("snapshot_capture_errors") or [])
    screenshot_saved = False
    screenshot_saver = getattr(adapter, "save_resume_section_screenshot", None)
    if callable(screenshot_saver):
        try:
            screenshot_saved, screenshot_error = screenshot_saver(screenshot_path, expected_filename=expected_filename)
            if screenshot_error:
                capture_errors.append(f"screenshot: {screenshot_error}")
        except Exception as exc:  # noqa: BLE001
            capture_errors.append(f"screenshot: {exc}")
    else:
        page = getattr(adapter, "_page", None)
        if page is not None:
            screenshot_saved, screenshot_error = _safe_page_screenshot(page, screenshot_path)
            if screenshot_error:
                capture_errors.append(f"screenshot: {screenshot_error}")
    html_saved = False
    try:
        html_path.write_text(str(state.get("resume_section_html") or ""), encoding="utf-8")
        html_saved = True
    except Exception as exc:  # noqa: BLE001
        capture_errors.append(f"html: {exc}")
    text_saved = False
    try:
        text_path.write_text(str(state.get("resume_section_text") or ""), encoding="utf-8")
        text_saved = True
    except Exception as exc:  # noqa: BLE001
        capture_errors.append(f"text: {exc}")
    state["snapshot_capture_errors"] = capture_errors
    state["screenshot_saved"] = screenshot_saved
    state["html_saved"] = html_saved
    state["text_saved"] = text_saved
    _save_resume_failure_json(snapshot_dir, "resume_section_state", state)
    print(
        f"[seek_apply_assist] resume_section_snapshot_saved: checkpoint={checkpoint} json={json_path}",
        flush=True,
    )
    return {
        "snapshot_dir": str(snapshot_dir),
        "json_path": str(json_path),
        "html_path": str(html_path),
        "text_path": str(text_path),
        "screenshot_path": str(screenshot_path),
        "screenshot_saved": screenshot_saved,
        "html_saved": html_saved,
        "text_saved": text_saved,
        "snapshot_capture_errors": capture_errors,
    }


def _save_upload_click_diagnostics(
    adapter: Any,
    debug_dir: Path,
    payload: dict[str, Any],
    *,
    resume_upload_diagnostics: Callable[[Any], dict[str, Any]] | None = None,
) -> tuple[Path, Path, Path]:
    saver = getattr(adapter, "save_named_resume_artifacts", None)
    if callable(saver):
        try:
            screenshot_path, html_path = saver(debug_dir, "upload_click_diagnostics")
        except Exception:  # noqa: BLE001
            screenshot_path, html_path = adapter.save_resume_debug_artifacts(debug_dir)
    else:
        screenshot_path, html_path = adapter.save_resume_debug_artifacts(debug_dir)
    diagnostics = {
        **payload,
        "resume_upload_diagnostics": resume_upload_diagnostics(adapter) if resume_upload_diagnostics is not None else {},
        "resume_limit_present": bool(adapter.resume_limit_ui_present()),
        "body_text_snippet": str(adapter.page_text() or "")[:1200],
    }
    json_path = _save_resume_failure_json(debug_dir, "upload_click_diagnostics", diagnostics)
    return screenshot_path, html_path, json_path


def _write_dynamic_question_scan(debug_dir: Path, scan_report: list[dict[str, Any]], validation_errors: list[str]) -> None:
    debug_dir.mkdir(parents=True, exist_ok=True)
    scan_path = debug_dir / "dynamic_question_scan.json"
    payload = {
        "fields_found": len(scan_report),
        "validation_errors": validation_errors,
        "fields": scan_report,
    }
    scan_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[seek_apply_assist] dynamic_question_scan_json: {scan_path}", flush=True)


def _log_continue_click_failure(page: Any, candidate_name: str, strategy_name: str) -> None:
    current_url = ""
    page_title = ""
    step_text = ""
    try:
        current_url = str(page.url or "")
    except Exception:  # noqa: BLE001
        pass
    try:
        page_title = str(page.title() or "")
    except Exception:  # noqa: BLE001
        pass
    try:
        step_text = str(
            page.evaluate(
                """() => {
                    const bodyText = (document.body?.innerText || '');
                    const lines = bodyText.split(/\\n+/).map(line => line.trim()).filter(Boolean);
                    return lines.find(line => /Answer employer questions|Update SEEK Profile|Review and submit|Submit|Review/i.test(line)) || '';
                }"""
            )
            or ""
        )
    except Exception:  # noqa: BLE001
        pass
    print(f"[seek_apply_assist] click_strategy_failed: candidate={candidate_name} strategy={strategy_name}", flush=True)
    print(f"[seek_apply_assist] current_url: {current_url or '(unknown)'}", flush=True)
    print(f"[seek_apply_assist] page_title: {page_title or '(unknown)'}", flush=True)
    print(f"[seek_apply_assist] step_text_visible: {step_text or '(none)'}", flush=True)


def _log_seek_step_diagnostics(
    page: Any,
    *,
    get_questionnaire_field_counts: Callable[[Any], dict[str, int]],
) -> tuple[str, str, dict[str, int]]:
    current_url = ""
    page_title = ""
    try:
        current_url = str(page.url or "")
    except Exception:  # noqa: BLE001
        current_url = ""
    try:
        page_title = str(page.title() or "")
    except Exception:  # noqa: BLE001
        page_title = ""
    counts = get_questionnaire_field_counts(page)
    print(f"[seek_apply_assist] questionnaire_page_url: {current_url or '(unknown)'}", flush=True)
    print(f"[seek_apply_assist] questionnaire_page_title: {page_title or '(unknown)'}", flush=True)
    print(f"[seek_apply_assist] questionnaire_select_count: {counts['select']}", flush=True)
    print(f"[seek_apply_assist] questionnaire_radio_count: {counts['radio']}", flush=True)
    print(f"[seek_apply_assist] questionnaire_textarea_count: {counts['textarea']}", flush=True)
    print(f"[seek_apply_assist] questionnaire_input_count: {counts['input']}", flush=True)
    return current_url, page_title, counts


def _log_resume_state(
    label: str,
    page: Any,
    adapter: Any,
    *,
    resume_state_snapshot: Callable[[Any, Any], dict[str, Any]],
) -> dict[str, Any]:
    snapshot = resume_state_snapshot(page, adapter)
    print(
        f"[seek_apply_assist] {label}: "
        f"filename_visible={snapshot['resume_filename_visible']} "
        f"filename={snapshot['resume_filename'] or '(none)'} "
        f"no_resume_included={snapshot['no_resume_included']} "
        f"upload_radio_selected={snapshot['upload_radio_selected']} "
        f"dont_include_selected={snapshot['dont_include_selected']}",
        flush=True,
    )
    return snapshot


def _save_resume_state_lost_artifacts(
    debug_dir: Path,
    page: Any,
    adapter: Any,
    checkpoint: str,
    *,
    resume_state_snapshot: Callable[[Any, Any], dict[str, Any]],
) -> tuple[Path, Path, Path]:
    debug_dir.mkdir(parents=True, exist_ok=True)
    screenshot_path = debug_dir / "resume_state_lost.png"
    html_path = debug_dir / "resume_state_lost.html"
    _safe_page_screenshot(page, screenshot_path)
    _safe_page_html_write(page, html_path)
    json_path = _save_resume_failure_json(
        debug_dir,
        "resume_state_lost",
        {
            "checkpoint": checkpoint,
            **resume_state_snapshot(page, adapter),
        },
    )
    return screenshot_path, html_path, json_path


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
    debug_dir.mkdir(parents=True, exist_ok=True)
    snapshot = snapshot or {}
    prompt_path = debug_dir / "codex_prompt_resume_failure.txt"
    lines = [
        "SEEK resume upload/apply flow failure",
        "",
        f"Failure reason: {failure_reason}",
        f"Latest checkpoint: {latest_checkpoint}",
        f"Screenshot: {screenshot_path or '(not saved)'}",
        f"HTML: {html_path or '(not saved)'}",
        f"JSON: {json_path or '(not saved)'}",
        "",
        "Key page text:",
        str(snapshot.get("body_text_snippet") or "(none)"),
        "",
        "Suggested next fix:",
        suggested_next_fix or "Inspect the latest checkpoint artifacts and update the SEEK resume state handling accordingly.",
        "",
        "Do not auto-edit source code from inside the app. Use these artifacts to prepare the next Codex change.",
    ]
    prompt_path.write_text("\n".join(lines), encoding="utf-8")
    return prompt_path


def _record_resume_checkpoint(
    debug_dir: Path,
    checkpoint: str,
    *,
    page: Any | None,
    adapter: Any | None,
    expected_filename: str = "",
    extra: dict[str, Any] | None = None,
    resume_state_snapshot: Callable[[Any, Any], dict[str, Any]] | None = None,
    resume_upload_diagnostics: Callable[[Any], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "checkpoint": checkpoint,
        "expected_filename": str(expected_filename or ""),
        "timestamp": datetime.now().isoformat(timespec="seconds"),
    }
    if page is None or adapter is None or resume_state_snapshot is None or resume_upload_diagnostics is None:
        payload["page_available"] = False
        return payload

    debug_dir.mkdir(parents=True, exist_ok=True)
    screenshot_path = debug_dir / f"{checkpoint}.png"
    html_path = debug_dir / f"{checkpoint}.html"
    json_path = debug_dir / f"{checkpoint}.json"
    snapshot = resume_state_snapshot(page, adapter)
    diagnostics = resume_upload_diagnostics(adapter)
    payload.update(snapshot)
    payload["resume_limit_modal_exists"] = bool(adapter.resume_limit_ui_present())
    payload["file_input_diagnostics"] = diagnostics.get("file_inputs", [])
    payload["upload_button_diagnostics"] = {
        "counts": diagnostics.get("upload_counts", {}),
        "candidates": diagnostics.get("upload_text_candidates", []),
    }
    if extra:
        payload.update(extra)
    try:
        _safe_page_screenshot(page, screenshot_path, timeout_ms=3000)
    except Exception:  # noqa: BLE001
        pass
    try:
        _safe_page_html_write(page, html_path)
    except Exception:  # noqa: BLE001
        pass
    try:
        json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    print(
        f"[seek_apply_assist] checkpoint={checkpoint} "
        f"url={payload.get('page_url', '')} "
        f"title={payload.get('page_title', '')} "
        f"filename={payload.get('resume_filename') or '(none)'} "
        f"no_resume_included={payload.get('no_resume_included')} "
        f"resume_limit_modal_exists={payload.get('resume_limit_modal_exists')}",
        flush=True,
    )
    payload["screenshot_path"] = str(screenshot_path)
    payload["html_path"] = str(html_path)
    payload["json_path"] = str(json_path)
    return payload
