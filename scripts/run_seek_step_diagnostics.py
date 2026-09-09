from __future__ import annotations

import argparse
import faulthandler
import json
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.document_generator import DocumentGenerator  # noqa: E402
from src.seek_apply_assist import (  # noqa: E402
    _build_playwright_adapter,
    _wait_for_post_quick_apply_ready,
    handle_resume_upload,
    prompt_for_seek_login,
)
from src.database import Database  # noqa: E402


def _now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _iso_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass
class StepResult:
    name: str
    status: str
    started_at: str
    finished_at: str
    elapsed_seconds: float
    current_url: str = ""
    page_title: str = ""
    pause_reason: str = ""
    note: str = ""
    trace_path: str = ""


class StepLogger:
    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = self.output_dir / "step_trace.log"
        self.results_path = self.output_dir / "step_results.json"
        self.lines: list[str] = []
        self.results: list[StepResult] = []

    def log(self, message: str) -> None:
        line = f"{_iso_now()} {message}"
        print(line, flush=True)
        self.lines.append(line)
        self.log_path.write_text("\n".join(self.lines) + "\n", encoding="utf-8")

    def add_result(self, result: StepResult) -> None:
        self.results.append(result)
        self.results_path.write_text(
            json.dumps([asdict(item) for item in self.results], indent=2, ensure_ascii=False),
            encoding="utf-8",
        )


def _safe(callable_obj: Any, default: Any = None) -> Any:
    try:
        return callable_obj()
    except Exception:
        return default


def _save_checkpoint(adapter: Any, output_dir: Path, name: str) -> None:
    page = getattr(adapter, "_page", None)
    if page is None:
        return
    screenshot_path = output_dir / f"{name}.png"
    html_path = output_dir / f"{name}.html"
    json_path = output_dir / f"{name}.json"
    try:
        page.screenshot(path=str(screenshot_path), full_page=True, timeout=5000)
    except Exception:
        pass
    try:
        html_path.write_text(str(page.content() or ""), encoding="utf-8")
    except Exception:
        pass
    payload = {
        "checkpoint": name,
        "captured_at": _iso_now(),
        "current_url": _safe(lambda: str(page.url or ""), ""),
        "page_title": _safe(lambda: str(page.title() or ""), ""),
        "pause_reason": _safe(lambda: str(adapter.detect_pause_reason() or ""), ""),
        "body_text_snippet": _safe(lambda: str(page.locator("body").inner_text(timeout=5000) or "")[:1500], ""),
        "file_input_count": _safe(lambda: int(page.locator('input[type="file"]').count()), 0),
    }
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def _timed_step(
    logger: StepLogger,
    adapter: Any | None,
    output_dir: Path,
    *,
    name: str,
    timeout_seconds: int,
    action: Any,
) -> Any:
    started_at = _iso_now()
    logger.log(f"[seek_step_diagnostics] step_started name={name} timeout_seconds={timeout_seconds}")
    start = time.monotonic()
    trace_path = output_dir / f"{name}_timeout_trace.txt"
    trace_file = trace_path.open("w", encoding="utf-8")
    faulthandler.dump_traceback_later(timeout_seconds, file=trace_file)
    try:
        value = action()
        status = "success"
        note = ""
        return value
    except Exception as exc:  # noqa: BLE001
        status = "error"
        note = str(exc)
        raise
    finally:
        faulthandler.cancel_dump_traceback_later()
        trace_file.flush()
        trace_file.close()
        elapsed = round(time.monotonic() - start, 2)
        finished_at = _iso_now()
        current_url = ""
        page_title = ""
        pause_reason = ""
        if adapter is not None:
            current_url = _safe(lambda: str(adapter.current_url() or ""), "")
            page_title = _safe(lambda: str(getattr(adapter, "_page").title() or ""), "")
            pause_reason = _safe(lambda: str(adapter.detect_pause_reason() or ""), "")
            _save_checkpoint(adapter, output_dir, name)
        logger.add_result(
            StepResult(
                name=name,
                status=status,
                started_at=started_at,
                finished_at=finished_at,
                elapsed_seconds=elapsed,
                current_url=current_url,
                page_title=page_title,
                pause_reason=pause_reason,
                note=note,
                trace_path=str(trace_path),
            )
        )
        logger.log(
            f"[seek_step_diagnostics] step_finished name={name} status={status} "
            f"elapsed_seconds={elapsed} pause_reason={pause_reason or '(none)'}"
        )


def _write_summary(
    output_dir: Path,
    *,
    logger: StepLogger,
    job_id: int,
    title: str,
    company: str,
    selected_cv_path: str,
    final_status: str,
    failure_reason: str,
) -> Path:
    summary = {
        "job_id": job_id,
        "title": title,
        "company": company,
        "selected_cv_path": selected_cv_path,
        "final_status": final_status,
        "failure_reason": failure_reason,
        "results": [asdict(item) for item in logger.results],
        "log_path": str(logger.log_path),
        "step_results_path": str(logger.results_path),
    }
    path = output_dir / "summary.json"
    path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    markdown_lines = [
        "# SEEK Step Diagnostics",
        "",
        f"- Job ID: `{job_id}`",
        f"- Title: `{title}`",
        f"- Company: `{company}`",
        f"- Selected CV: `{selected_cv_path}`",
        f"- Final status: `{final_status}`",
        f"- Failure reason: `{failure_reason or '(none)'}`",
        "",
        "## Steps",
        "",
    ]
    for item in logger.results:
        markdown_lines.append(
            f"- `{item.name}`: `{item.status}` in `{item.elapsed_seconds}s`"
            + (f" | pause=`{item.pause_reason}`" if item.pause_reason else "")
            + (f" | note=`{item.note}`" if item.note else "")
        )
    (output_dir / "summary.md").write_text("\n".join(markdown_lines) + "\n", encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Run step-level SEEK diagnostics for a single job.")
    parser.add_argument("--job-id", type=int, required=True)
    parser.add_argument("--db-path", type=Path, default=PROJECT_ROOT / "data" / "job_assistant.db")
    parser.add_argument("--visible", default="false")
    parser.add_argument("--slow-mo-ms", type=int, default=0)
    parser.add_argument("--step-timeout-seconds", type=int, default=45)
    parser.add_argument("--output-dir", default="")
    args = parser.parse_args()

    output_dir = (
        Path(args.output_dir)
        if args.output_dir
        else PROJECT_ROOT / "data" / "diagnostic_runs" / f"{_now_stamp()}_seek_step_diagnostics_{int(args.job_id)}"
    )
    logger = StepLogger(output_dir)

    database = Database(args.db_path)
    document_generator = DocumentGenerator(database, PROJECT_ROOT)
    details = database.get_job_details(int(args.job_id)) or {}
    title = str(details.get("title") or "")
    company = str(details.get("company") or "")
    job_url = str(details.get("url") or "")
    if not job_url:
        raise SystemExit(f"Job {args.job_id} does not have a URL.")

    adapter = None
    selected_cv_path = ""
    final_status = "failed"
    failure_reason = ""
    try:
        package = _timed_step(
            logger,
            None,
            output_dir,
            name="generate_documents",
            timeout_seconds=max(30, int(args.step_timeout_seconds)),
            action=lambda: document_generator.generate_for_job(int(args.job_id)),
        )
        selected_cv_path = str(package.get("selected_cv_path") or "")
        if not selected_cv_path:
            raise RuntimeError("Document generator did not return selected_cv_path.")

        adapter = _timed_step(
            logger,
            None,
            output_dir,
            name="build_adapter",
            timeout_seconds=max(30, int(args.step_timeout_seconds)),
            action=lambda: _build_playwright_adapter(
                str(args.visible).strip().lower() in {"1", "true", "yes", "on"},
                args.db_path,
                slow_mo_ms=max(0, int(args.slow_mo_ms)),
            ),
        )
        _timed_step(
            logger,
            adapter,
            output_dir,
            name="open_job",
            timeout_seconds=max(45, int(args.step_timeout_seconds)),
            action=lambda: adapter.open_job(job_url),
        )
        _timed_step(
            logger,
            adapter,
            output_dir,
            name="click_quick_apply",
            timeout_seconds=max(45, int(args.step_timeout_seconds)),
            action=lambda: adapter.click_quick_apply(),
        )
        immediate_pause_reason = _safe(lambda: str(adapter.detect_pause_reason() or ""), "")
        if immediate_pause_reason == "paused_for_seek_login":
            resumed = _timed_step(
                logger,
                adapter,
                output_dir,
                name="prompt_for_seek_login",
                timeout_seconds=max(45, int(args.step_timeout_seconds)),
                action=lambda: prompt_for_seek_login(
                    adapter,
                    db_path=args.db_path,
                    use_bulk_profile=False,
                    input_func=lambda: "",
                    allow_manual_prompt=False,
                ),
            )
            if not bool((resumed or (False, ""))[0]):
                raise RuntimeError(str((resumed or (False, ""))[1] or "SEEK login still required after Quick Apply."))
        elif immediate_pause_reason in {"paused_for_seek_verification", "paused_for_captcha"}:
            raise RuntimeError(f"Quick Apply paused immediately with {immediate_pause_reason}.")
        _timed_step(
            logger,
            adapter,
            output_dir,
            name="wait_for_application_form",
            timeout_seconds=max(45, int(args.step_timeout_seconds)),
            action=lambda: adapter.wait_for_application_form(),
        )
        post_quick_apply_state, _post_quick_apply_payload = _timed_step(
            logger,
            adapter,
            output_dir,
            name="wait_for_post_quick_apply_ready",
            timeout_seconds=max(45, int(args.step_timeout_seconds)),
            action=lambda: _wait_for_post_quick_apply_ready(adapter, timeout_ms=20000),
        )
        if post_quick_apply_state != "application_ready":
            raise RuntimeError(f"Post Quick Apply state was {post_quick_apply_state}.")
        resume_result = _timed_step(
            logger,
            adapter,
            output_dir,
            name="handle_resume_upload",
            timeout_seconds=max(90, int(args.step_timeout_seconds)),
            action=lambda: handle_resume_upload(adapter, Path(selected_cv_path), output_dir),
        )
        final_status = str(getattr(resume_result, "status", "") or "success")
        failure_reason = str(getattr(resume_result, "warning", "") or "")
    except Exception as exc:  # noqa: BLE001
        failure_reason = str(exc)
        final_status = "error"
        logger.log(f"[seek_step_diagnostics] run_failed error={failure_reason}")
    finally:
        if adapter is not None:
            try:
                adapter.close()
            except Exception:
                pass

    summary_path = _write_summary(
        output_dir,
        logger=logger,
        job_id=int(args.job_id),
        title=title,
        company=company,
        selected_cv_path=selected_cv_path,
        final_status=final_status,
        failure_reason=failure_reason,
    )
    print(str(summary_path), flush=True)
    return 0 if final_status == "resume_uploaded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
