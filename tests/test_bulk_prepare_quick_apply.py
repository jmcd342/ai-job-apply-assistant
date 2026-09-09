from __future__ import annotations

import sys
import tempfile
import unittest
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.bulk_prepare_quick_apply import _build_bulk_result, _build_timeout_error_message, _write_apply_run_artifacts, find_quick_apply_jobs, run_bulk_quick_apply
from src.bulk_prepare_quick_apply import execute_single_quick_apply_job


class FakeBulkDatabase:
    def __init__(self, db_path: Path, search_frame: pd.DataFrame, details_by_job_id: dict[int, dict]) -> None:
        self.db_path = db_path
        self._search_frame = search_frame
        self._details_by_job_id = details_by_job_id
        self.logged_runs: list[tuple[object, ...]] = []
        self.updated_states: list[tuple[object, ...]] = []

    def search_jobs(self, **_: object) -> pd.DataFrame:
        return self._search_frame.copy()

    def get_job_details(self, job_id: int) -> dict | None:
        return self._details_by_job_id.get(job_id)

    def log_run(self, *args: object) -> None:
        self.logged_runs.append(args)

    def update_application_state(self, *args: object, **kwargs: object) -> None:
        note = kwargs.get("note")
        self.updated_states.append((*args, note))


class FakeDocumentGenerator:
    def ensure_current_cover_letter(self, job_id: int, force_regenerate: bool = False) -> dict:
        return {"job_id": job_id, "force_regenerate": force_regenerate}


class RaisingDocumentGenerator:
    def ensure_current_cover_letter(self, job_id: int, force_regenerate: bool = False) -> dict:
        raise AssertionError("Bulk dashboard runner should not regenerate documents in-process.")


class BulkPrepareQuickApplyTests(unittest.TestCase):
    def test_build_timeout_error_message_classifies_resume_stage(self) -> None:
        stdout_text = "\n".join(
            [
                "[seek_apply_assist] clicked_quick_apply: Quick apply opened.",
                "[seek_apply_assist] handle_resume_upload_started",
                "[seek_apply_assist] resume_page_title: Choose documents | SEEK",
            ]
        )
        message = _build_timeout_error_message(stdout_text, "", 120)
        self.assertIn("120 seconds", message)
        self.assertIn("Choose documents / resume handling", message)

    def test_find_quick_apply_jobs_includes_apply_type_or_button_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "job_assistant.db"
            search_frame = pd.DataFrame(
                [
                    {"job_id": 3, "apply_type": "Quick Apply", "has_quick_apply_button": 0, "imported_at": "2026-05-13T10:00:00"},
                    {"job_id": 2, "apply_type": "", "has_quick_apply_button": 1, "imported_at": "2026-05-13T09:00:00"},
                    {"job_id": 1, "apply_type": "Apply", "has_quick_apply_button": 0, "imported_at": "2026-05-13T08:00:00"},
                ]
            )
            details = {
                3: {"job_id": 3, "title": "Quick Role", "company": "Example A", "url": "https://seek.example/3"},
                2: {"job_id": 2, "title": "Flagged Role", "company": "Example B", "url": "https://seek.example/2"},
                1: {"job_id": 1, "title": "Normal Apply", "company": "Example C", "url": "https://seek.example/1"},
            }
            database = FakeBulkDatabase(db_path, search_frame, details)

            jobs = find_quick_apply_jobs(database, max_jobs=5)

            self.assertEqual([job["job_id"] for job in jobs], [3, 2])

    def test_find_quick_apply_jobs_excludes_previously_attempted_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            db_path = root / "job_assistant.db"
            runs_root = root / "apply_runs" / "20260608_222019_285"
            runs_root.mkdir(parents=True, exist_ok=True)
            (runs_root / "final_review_checklist.json").write_text(
                json.dumps({"job_id": 3, "status": "failed_needs_fix"}),
                encoding="utf-8",
            )
            search_frame = pd.DataFrame(
                [
                    {"job_id": 3, "apply_type": "Quick Apply", "has_quick_apply_button": 1, "imported_at": "2026-05-13T10:00:00"},
                    {"job_id": 2, "apply_type": "Quick Apply", "has_quick_apply_button": 1, "imported_at": "2026-05-13T09:00:00"},
                ]
            )
            details = {
                3: {"job_id": 3, "title": "Previously Attempted", "company": "Example A", "url": "https://seek.example/3"},
                2: {"job_id": 2, "title": "Fresh Job", "company": "Example B", "url": "https://seek.example/2"},
            }
            database = FakeBulkDatabase(db_path, search_frame, details)

            jobs = find_quick_apply_jobs(database, max_jobs=5)

            self.assertEqual([job["job_id"] for job in jobs], [2])

    def test_find_quick_apply_jobs_can_include_previously_attempted_jobs_for_explicit_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            db_path = root / "job_assistant.db"
            runs_root = root / "apply_runs" / "20260608_222019_285"
            runs_root.mkdir(parents=True, exist_ok=True)
            (runs_root / "final_review_checklist.json").write_text(
                json.dumps({"job_id": 3, "status": "failed_needs_fix"}),
                encoding="utf-8",
            )
            search_frame = pd.DataFrame(
                [
                    {"job_id": 3, "apply_type": "Quick Apply", "has_quick_apply_button": 1, "imported_at": "2026-05-13T10:00:00"},
                    {"job_id": 2, "apply_type": "Quick Apply", "has_quick_apply_button": 1, "imported_at": "2026-05-13T09:00:00"},
                ]
            )
            details = {
                3: {"job_id": 3, "title": "Previously Attempted", "company": "Example A", "url": "https://seek.example/3"},
                2: {"job_id": 2, "title": "Fresh Job", "company": "Example B", "url": "https://seek.example/2"},
            }
            database = FakeBulkDatabase(db_path, search_frame, details)

            jobs = find_quick_apply_jobs(
                database,
                max_jobs=5,
                selected_job_ids=[3],
                include_attempted=True,
            )

            self.assertEqual([job["job_id"] for job in jobs], [3])

    def test_find_quick_apply_jobs_excludes_auto_apply_blocked_companies(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "job_assistant.db"
            search_frame = pd.DataFrame(
                [
                    {"job_id": 9, "apply_type": "Quick Apply", "has_quick_apply_button": 1, "imported_at": "2026-05-13T11:00:00"},
                    {"job_id": 8, "apply_type": "Quick Apply", "has_quick_apply_button": 1, "imported_at": "2026-05-13T10:00:00"},
                ]
            )
            details = {
                9: {"job_id": 9, "title": "IT Specialist", "company": "Bank of China (New Zealand) Limited", "url": "https://seek.example/9"},
                8: {"job_id": 8, "title": "Data Engineer", "company": "Example B", "url": "https://seek.example/8"},
            }
            database = FakeBulkDatabase(db_path, search_frame, details)

            jobs = find_quick_apply_jobs(database, max_jobs=5)

            self.assertEqual([job["job_id"] for job in jobs], [8])

    def test_run_bulk_quick_apply_dry_run_marks_jobs_as_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "job_assistant.db"
            search_frame = pd.DataFrame(
                [
                    {"job_id": 8, "apply_type": "Quick Apply", "has_quick_apply_button": 0, "imported_at": "2026-05-13T10:00:00"},
                    {"job_id": 7, "apply_type": "", "has_quick_apply_button": 1, "imported_at": "2026-05-13T09:00:00"},
                ]
            )
            details = {
                8: {"job_id": 8, "title": "Analytics Engineer", "company": "Example A", "url": "https://seek.example/8"},
                7: {"job_id": 7, "title": "BI Analyst", "company": "Example B", "url": "https://seek.example/7"},
            }
            database = FakeBulkDatabase(db_path, search_frame, details)

            results = run_bulk_quick_apply(
                database,
                FakeDocumentGenerator(),
                max_jobs=5,
                dry_run=True,
            )

            self.assertEqual(len(results), 2)
            self.assertEqual(results[0].status, "skipped")
            self.assertEqual(results[0].failure_reason, "Dry run preview only.")
            self.assertIn("apply_runs", results[0].evidence_folder)
            self.assertEqual([result.job_id for result in results], [8, 7])

    def test_build_bulk_result_preserves_root_failure_reason_when_evidence_capture_has_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            debug_dir = root / "debug"
            debug_dir.mkdir(parents=True, exist_ok=True)
            (debug_dir / "resume_upload_blocked_before_continue.json").write_text(
                '{"evidence_capture_errors": ["screenshot: timeout"]}',
                encoding="utf-8",
            )
            assist_result = SimpleNamespace(
                status="paused_for_resume_upload_incomplete",
                error="CV filename appeared, but SEEK was still processing the upload. Stopped before Continue.",
                root_failure_reason="CV filename appeared, but SEEK was still processing the upload. Stopped before Continue.",
                final_url="https://nz.seek.com/job/123/apply",
                debug_dir=str(debug_dir),
                steps_completed=["opened_job", "clicked_quick_apply"],
                evidence_capture_errors=["html: write failed"],
            )
            job = {
                "job_id": 74,
                "title": "Accounts & Payroll Officer",
                "company": "Vending Direct Ltd",
                "url": "https://seek.example/74",
            }

            result = _build_bulk_result(job, assist_result, root / "apply_run")
            _write_apply_run_artifacts(root / "apply_run", result, assist_result)
            payload = (root / "apply_run" / "final_review_checklist.json").read_text(encoding="utf-8")

            self.assertEqual(result.status, "failed_needs_fix")
            self.assertEqual(result.root_failure_reason, assist_result.root_failure_reason)
            self.assertIn('"root_failure_reason": "CV filename appeared, but SEEK was still processing the upload. Stopped before Continue."', payload)
            self.assertIn('"evidence_capture_errors": [', payload)

    def test_build_bulk_result_respects_explicit_failed_final_checklist(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            debug_dir = root / "debug"
            debug_dir.mkdir(parents=True, exist_ok=True)
            (debug_dir / "final_review_checklist.json").write_text(
                json.dumps(
                    {
                        "resume_included": True,
                        "cover_letter_included": True,
                        "questions_answered": True,
                        "no_validation_errors": True,
                        "final_submit_not_clicked": True,
                        "all_checks_passed": False,
                    }
                ),
                encoding="utf-8",
            )
            assist_result = SimpleNamespace(
                status="prepared_for_manual_review",
                error="",
                root_failure_reason="Final SEEK review checklist failed.",
                final_url="https://nz.seek.com/job/123/apply/review",
                debug_dir=str(debug_dir),
                steps_completed=[],
                evidence_capture_errors=[],
            )
            job = {"job_id": 1, "title": "Role", "company": "Co", "url": "https://seek.example/1"}

            result = _build_bulk_result(job, assist_result, root / "apply_run")

            self.assertEqual(result.status, "failed_needs_fix")

    def test_build_bulk_result_preserves_skipped_closed_job_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            assist_result = SimpleNamespace(
                status="skipped",
                error="This SEEK job is no longer advertised.",
                root_failure_reason="This SEEK job is no longer advertised.",
                final_url="https://seek.example/closed",
                debug_dir="",
                steps_completed=["opened_job"],
                evidence_capture_errors=[],
            )
            job = {
                "job_id": 65,
                "title": "Data & Analytics Senior Consultant (Fabric/Snowflake)",
                "company": "Datacom",
                "url": "https://seek.example/65",
            }

            result = _build_bulk_result(job, assist_result, root / "apply_run")

            self.assertEqual(result.status, "skipped")
            self.assertIn("no longer advertised", result.root_failure_reason.lower())

    def test_build_bulk_result_preserves_paused_verification_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            assist_result = SimpleNamespace(
                status="paused_for_seek_verification",
                error="SEEK verification is required before automation can continue.",
                root_failure_reason="SEEK verification is required before automation can continue.",
                final_url="https://seek.example/verify",
                debug_dir="",
                steps_completed=["opened_job"],
                evidence_capture_errors=[],
            )
            job = {
                "job_id": 111,
                "title": "Data Engineer",
                "company": "BrightSpark Recruitment",
                "url": "https://seek.example/111",
            }

            result = _build_bulk_result(job, assist_result, root / "apply_run")

            self.assertEqual(result.status, "paused_for_seek_verification")
            self.assertIn("verification", result.root_failure_reason.lower())

    def test_run_bulk_quick_apply_uses_subprocess_and_continues_after_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "job_assistant.db"
            search_frame = pd.DataFrame(
                [
                    {"job_id": 8, "apply_type": "Quick Apply", "has_quick_apply_button": 0, "imported_at": "2026-05-13T10:00:00"},
                    {"job_id": 7, "apply_type": "Quick Apply", "has_quick_apply_button": 0, "imported_at": "2026-05-13T09:00:00"},
                ]
            )
            details = {
                8: {"job_id": 8, "title": "Analytics Engineer", "company": "Example A", "url": "https://seek.example/8"},
                7: {"job_id": 7, "title": "BI Analyst", "company": "Example B", "url": "https://seek.example/7"},
            }
            database = FakeBulkDatabase(db_path, search_frame, details)
            calls: list[list[str]] = []

            def fake_subprocess_runner(command: list[str], capture_output: bool, text: bool, timeout: int, **kwargs: object) -> SimpleNamespace:
                self.assertTrue(capture_output)
                self.assertTrue(text)
                self.assertEqual(timeout, 120)
                self.assertEqual(kwargs.get("encoding"), "utf-8")
                self.assertEqual(kwargs.get("errors"), "replace")
                self.assertIsNotNone(kwargs.get("env"))
                calls.append(command)
                run_folder = Path(command[command.index("--run-folder") + 1])
                job_id = int(command[command.index("--job-id") + 1])
                payload = {
                    "job_id": job_id,
                    "title": details[job_id]["title"],
                    "company": details[job_id]["company"],
                    "status": "failed_needs_fix" if job_id == 8 else "ready_for_human_review",
                    "resume_included": job_id != 8,
                    "cover_letter_included": job_id != 8,
                    "questions_answered": job_id != 8,
                    "failure_reason": "resume stuck" if job_id == 8 else "",
                    "root_failure_reason": "resume stuck" if job_id == 8 else "",
                    "evidence_folder": str(run_folder),
                    "job_url": details[job_id]["url"],
                    "reached_review_page": job_id != 8,
                    "resume_text_seen": "Example_Candidate_CV.docx" if job_id != 8 else "",
                    "cover_letter_text_seen": "Example cover letter" if job_id != 8 else "",
                    "question_answer_count_text": "You answered 4 out of 4" if job_id != 8 else "",
                    "no_validation_errors": True,
                    "final_submit_not_clicked": True,
                    "screenshot_path": str(run_folder / "final_review.png"),
                    "html_path": str(run_folder / "final_review.html"),
                    "evidence_capture_errors": [],
                    "screenshot_saved": False,
                    "html_saved": False,
                    "json_saved": True,
                }
                run_folder.mkdir(parents=True, exist_ok=True)
                (run_folder / "final_review_checklist.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
                return SimpleNamespace(returncode=0 if job_id != 8 else 1, stdout=f"job {job_id} complete", stderr="")

            results = run_bulk_quick_apply(
                database,
                RaisingDocumentGenerator(),
                max_jobs=5,
                dry_run=False,
                subprocess_runner=fake_subprocess_runner,
                pause_between_jobs_seconds=0.0,
            )

            self.assertEqual(len(results), 2)
            self.assertEqual(len(calls), 2)
            self.assertTrue(all("run_single_apply_assist.py" in " ".join(call) for call in calls))
            self.assertTrue(all("--non-interactive" in call for call in calls))
            self.assertTrue(all("--force-new-profile" not in call for call in calls))
            self.assertEqual(results[0].status, "failed_needs_fix")
            self.assertEqual(results[1].status, "ready_for_human_review")
            self.assertEqual(results[0].root_failure_reason, "resume stuck")
            self.assertEqual(results[1].subprocess_exit_code, 0)

    def test_execute_single_quick_apply_job_retries_after_auto_bulk_login(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            db_path = root / "job_assistant.db"
            search_frame = pd.DataFrame([])
            details = {
                74: {"job_id": 74, "title": "Accounts & Payroll Officer", "company": "Vending Direct Ltd", "url": "https://seek.example/74"}
            }
            database = FakeBulkDatabase(db_path, search_frame, details)
            run_dir = root / "apply_run"
            assist_results = [
                SimpleNamespace(
                    status="paused_for_seek_login",
                    error="SEEK login is required before Quick Apply can continue.",
                    root_failure_reason="SEEK login is required before Quick Apply can continue.",
                    final_url="https://login.seek.com",
                    debug_dir="",
                    steps_completed=["opened_job", "clicked_quick_apply"],
                    evidence_capture_errors=[],
                ),
                SimpleNamespace(
                    status="ready_for_human_review",
                    error="",
                    root_failure_reason="",
                    final_url="https://nz.seek.com/job/74/apply/review",
                    debug_dir="",
                    steps_completed=["opened_job", "clicked_quick_apply", "uploaded_cv"],
                    evidence_capture_errors=[],
                ),
            ]

            with (
                patch("src.bulk_prepare_quick_apply.assist_seek_apply", side_effect=assist_results) as assist_mock,
                patch("src.bulk_prepare_quick_apply.refresh_seek_session_in_browser", return_value=("chrome", "profile-dir")) as refresh_mock,
                patch("src.bulk_prepare_quick_apply.validate_seek_session", return_value={"ok": True, "state": "session_valid", "reason": "validated"}) as validate_mock,
            ):
                result = execute_single_quick_apply_job(
                    database,
                    FakeDocumentGenerator(),
                    job_id=74,
                    run_dir=run_dir,
                    visible=False,
                    use_bulk_profile=True,
                    non_interactive=True,
                )

            self.assertEqual(assist_mock.call_count, 2)
            refresh_mock.assert_called_once()
            validate_mock.assert_called_once()
            self.assertEqual(result.status, "failed_needs_fix")


if __name__ == "__main__":
    unittest.main()
