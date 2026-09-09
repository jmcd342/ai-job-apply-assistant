from __future__ import annotations

import sys
import unittest
from datetime import date, timedelta
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import Mock, patch

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app import (
    _bucket_quick_apply_queue_item,
    _categorize_problem_job,
    _applied_timestamp_series,
    _build_queue_summary,
    _persist_submit_result_state,
    build_dashboard_stats,
    build_job_display_summary,
    render_shared_queue_summary,
    visible_job_actions,
)


class DashboardSimplifiedUiTests(unittest.TestCase):
    def test_bucket_quick_apply_queue_item_marks_ready_status_as_ready(self) -> None:
        bucket = _bucket_quick_apply_queue_item(
            latest_run={"payload": {"status": "ready_for_human_review"}},
            latest_run_status="ready_for_human_review",
            latest_submit_status="",
        )
        self.assertEqual(bucket, "ready")

    def test_bucket_quick_apply_queue_item_marks_prepared_manual_review_as_ready(self) -> None:
        bucket = _bucket_quick_apply_queue_item(
            latest_run={"payload": {"status": "prepared_for_manual_review"}},
            latest_run_status="prepared_for_manual_review",
            latest_submit_status="",
        )
        self.assertEqual(bucket, "ready")

    def test_bucket_quick_apply_queue_item_marks_failed_status_as_problem(self) -> None:
        bucket = _bucket_quick_apply_queue_item(
            latest_run={"payload": {"status": "failed_needs_fix"}},
            latest_run_status="failed_needs_fix",
            latest_submit_status="",
        )
        self.assertEqual(bucket, "problems")

    def test_bucket_quick_apply_queue_item_marks_incomplete_resume_upload_as_problem(self) -> None:
        bucket = _bucket_quick_apply_queue_item(
            latest_run={"payload": {"status": "paused_for_resume_upload_incomplete"}},
            latest_run_status="paused_for_resume_upload_incomplete",
            latest_submit_status="",
        )
        self.assertEqual(bucket, "problems")

    def test_bucket_quick_apply_queue_item_keeps_unknown_processed_status_unprocessed(self) -> None:
        bucket = _bucket_quick_apply_queue_item(
            latest_run={"payload": {"status": "completed_without_bucket"}},
            latest_run_status="completed_without_bucket",
            latest_submit_status="",
        )
        self.assertEqual(bucket, "unprocessed")

    def test_bucket_quick_apply_queue_item_marks_submit_failure_as_problem(self) -> None:
        bucket = _bucket_quick_apply_queue_item(
            latest_run={"payload": {"status": "ready_for_human_review"}},
            latest_run_status="ready_for_human_review",
            latest_submit_status="failed",
        )
        self.assertEqual(bucket, "problems")

    def test_categorize_problem_job_treats_close_all_chromium_message_as_login_verification(self) -> None:
        key, label = _categorize_problem_job(
            latest_run_status="failed_needs_fix",
            issue_text="Close all SEEK/Chromium windows and retry. If it still fails, delete browser_profiles\\seek_seeded and run scripts/seek_login_setup.py again.",
        )
        self.assertEqual(key, "login_verification")
        self.assertEqual(label, "Login / Verification")

    def test_persist_submit_result_state_closes_stale_job(self) -> None:
        tracker = SimpleNamespace(database=Mock())
        result = SimpleNamespace(status="skipped", error="This SEEK job is no longer advertised.", final_url="")

        _persist_submit_result_state(tracker, 55, result, source_note="unused")

        tracker.database.close_application.assert_called_once_with(
            55,
            "job_no_longer_advertised",
            note="Closed automatically because SEEK reported the job was no longer advertised during final submit.",
        )
        tracker.database.update_application_state.assert_not_called()

    def test_persist_submit_result_state_moves_missing_quick_apply_job_out_of_ready_queue(self) -> None:
        tracker = SimpleNamespace(database=Mock())
        result = SimpleNamespace(status="failed", error="Could not find SEEK Quick apply button.", final_url="")

        _persist_submit_result_state(tracker, 56, result, source_note="unused")

        tracker.database.update_application_state.assert_called_once_with(
            56,
            "reviewed",
            note="Moved out of ready queue because SEEK no longer showed a Quick Apply button during final submit.",
        )

    def test_persist_submit_result_state_moves_missing_cv_job_out_of_ready_queue(self) -> None:
        tracker = SimpleNamespace(database=Mock())
        result = SimpleNamespace(
            status="failed",
            error="Selected CV file not found: C:\\temp\\missing.docx",
            final_url="",
        )

        _persist_submit_result_state(tracker, 57, result, source_note="unused")

        tracker.database.update_application_state.assert_called_once_with(
            57,
            "reviewed",
            note="Moved out of ready queue because the selected CV file was missing and needs regeneration.",
        )

    def test_persist_submit_result_state_moves_verification_blocked_job_out_of_ready_queue(self) -> None:
        tracker = SimpleNamespace(database=Mock())
        result = SimpleNamespace(
            status="paused_for_seek_verification",
            error="SEEK verification is required before automation can continue.",
            final_url="",
        )

        _persist_submit_result_state(tracker, 58, result, source_note="unused")

        tracker.database.update_application_state.assert_called_once_with(
            58,
            "reviewed",
            note="SEEK verification is required before automation can continue.",
        )

    def test_applied_timestamp_series_falls_back_to_submitted_at(self) -> None:
        jobs = pd.DataFrame(
            [
                {"applied_at": "", "submitted_at": "2026-06-26T09:30:00"},
                {"applied_at": "2026-06-25T14:00:00", "submitted_at": ""},
                {"applied_at": None, "submitted_at": None},
            ]
        )

        applied_series = _applied_timestamp_series(jobs)

        self.assertEqual(list(applied_series), ["2026-06-26T09:30:00", "2026-06-25T14:00:00", ""])

    def test_visible_actions_only_include_kept_actions(self) -> None:
        seek_details = {"source": "seek"}
        actions = visible_job_actions(seek_details)
        self.assertIn("Regenerate cover letter", actions)
        self.assertIn("Interview prep", actions)
        self.assertIn("Apply Assist - SEEK", actions)
        for removed in [
            "Select CV",
            "Cover letter",
            "Ready",
            "Reject",
            "Ignore",
            "Retry enrichment",
            "Mark ready to apply",
        ]:
            self.assertNotIn(removed, actions)

    def test_build_job_display_summary_exposes_apply_and_salary_fields(self) -> None:
        details = {
            "title": "Digital Data & Innovation Analyst",
            "company": "Barker Fruit Processors",
            "location": "Geraldine, Canterbury",
            "date_posted": "2026-05-11",
            "apply_type": "Quick Apply",
            "has_apply_button": 1,
            "has_quick_apply_button": 1,
            "apply_area_salary_text": "$90,000 - $100,000",
            "salary_expectation_text": "95k",
            "fit_score": 72,
            "selected_cv_profile": "analytics_engineer_bi_cv",
            "description": "Power BI, SQL, reporting, and analytics delivery.",
        }
        summary = build_job_display_summary(details)
        self.assertEqual(summary["date_posted"], "2026-05-11")
        self.assertEqual(summary["apply_type"], "Quick Apply")
        self.assertTrue(summary["has_apply_button"])
        self.assertTrue(summary["has_quick_apply_button"])
        self.assertEqual(summary["page_salary"], "$90,000 - $100,000")
        self.assertEqual(summary["calculated_salary"], "95k")
        self.assertIn("BI / Analytics CV", summary["fit_and_cv"])

    def test_build_dashboard_stats_counts_apply_types_salary_and_cv_profiles(self) -> None:
        jobs = pd.DataFrame(
            [
                {
                    "fit_score": 85,
                    "has_quick_apply_button": 1,
                    "has_apply_button": 1,
                    "apply_area_salary_text": "$100,000",
                    "salary_expectation_text": "100k",
                    "selected_cv_profile": "data_engineer_cv",
                },
                {
                    "fit_score": 65,
                    "has_quick_apply_button": 0,
                    "has_apply_button": 1,
                    "apply_area_salary_text": "",
                    "salary_expectation_text": "95k",
                    "selected_cv_profile": "analytics_engineer_bi_cv",
                },
                {
                    "fit_score": 35,
                    "has_quick_apply_button": 0,
                    "has_apply_button": 0,
                    "apply_area_salary_text": "",
                    "salary_expectation_text": "",
                    "selected_cv_profile": "",
                },
            ]
        )
        stats = build_dashboard_stats(jobs)
        self.assertEqual(stats["total_jobs"], 3)
        self.assertEqual(stats["jobs_with_quick_apply"], 1)
        self.assertEqual(stats["jobs_with_normal_apply"], 1)
        self.assertEqual(stats["jobs_with_salary_extracted"], 1)
        self.assertEqual(stats["jobs_missing_salary"], 2)
        self.assertEqual(stats["average_calculated_salary"], "100k")
        fit_counts = {row["fit_level"]: row["count"] for row in stats["fit_level_counts"]}
        self.assertEqual(fit_counts["High"], 1)
        self.assertEqual(fit_counts["Medium"], 1)
        self.assertEqual(fit_counts["Low"], 1)
        chosen_counts = {row["cv_profile"]: row["count"] for row in stats["chosen_cv_counts"]}
        self.assertEqual(chosen_counts["Data Engineer CV"], 1)
        self.assertEqual(chosen_counts["BI / Analytics CV"], 1)
        self.assertEqual(chosen_counts["Pending"], 1)

    def test_build_queue_summary_uses_bucketed_run_statuses(self) -> None:
        tracker = SimpleNamespace(
            database=Mock(),
            search_jobs=Mock(
                side_effect=[
                    pd.DataFrame(
                        [
                            {
                                "job_id": 1,
                                "apply_type": "Quick Apply",
                                "has_quick_apply_button": 1,
                                "location": "Auckland",
                                "application_status": "reviewed",
                            },
                            {
                                "job_id": 2,
                                "apply_type": "Quick Apply",
                                "has_quick_apply_button": 1,
                                "location": "Auckland",
                                "application_status": "reviewed",
                            },
                            {
                                "job_id": 3,
                                "apply_type": "Quick Apply",
                                "has_quick_apply_button": 1,
                                "location": "Auckland",
                                "application_status": "reviewed",
                            },
                        ]
                    ),
                    pd.DataFrame(
                        [
                            {
                                "job_id": 9,
                                "apply_type": "Quick Apply",
                                "has_quick_apply_button": 1,
                                "location": "Auckland",
                                "application_status": "applied",
                                "applied_at": f"{date.today().isoformat()}T10:00:00",
                                "submitted_at": "",
                            },
                            {
                                "job_id": 10,
                                "apply_type": "Quick Apply",
                                "has_quick_apply_button": 1,
                                "location": "Auckland",
                                "application_status": "ignored",
                                "applied_at": "",
                                "submitted_at": "",
                            },
                        ]
                    ),
                ]
            ),
        )

        with (
            patch(
                "app._latest_apply_runs_by_job",
                return_value={
                    2: {"payload": {"status": "ready_for_human_review"}},
                    3: {"payload": {"status": "failed_needs_fix"}},
                },
            ),
            patch("app._load_timing_history", return_value=pd.DataFrame()),
        ):
            summary = _build_queue_summary(tracker)

        self.assertEqual(summary["unprocessed_count"], 1)
        self.assertEqual(summary["ready_count"], 1)
        self.assertEqual(summary["problem_count"], 1)
        self.assertEqual(summary["applied_today_count"], 1)
        self.assertEqual(summary["applied_all_time_count"], 1)
        self.assertEqual(summary["removed_count"], 1)

    def test_app_source_no_longer_contains_acknowledgement_checkbox_or_question_helper(self) -> None:
        app_source = (PROJECT_ROOT / "app.py").read_text(encoding="utf-8")
        self.assertNotIn("I understand this app will not submit the application automatically.", app_source)
        self.assertNotIn("Application Question Helper", app_source)
        self.assertNotIn("Debug resume upload mode", app_source)
        self.assertNotIn("Continue after resume debug", app_source)
        self.assertNotIn("final_summary.json", app_source)
        self.assertIn("Apply Assist - SEEK", app_source)
        self.assertIn("Prepare In Background - SEEK", app_source)
        self.assertIn("Prepare all Quick Apply jobs", app_source)
        self.assertIn("Dry run preview", app_source)
        self.assertIn("Max jobs", app_source)
        self.assertIn("Latest failure:", app_source)
        self.assertIn("Failure summary", app_source)
        self.assertIn("Skipped jobs", app_source)
        self.assertIn("paused_for_seek_verification", app_source)
        self.assertIn("SEEK Session", app_source)
        self.assertIn("Verification required", app_source)
        self.assertIn("Open SEEK Login In Chrome", app_source)
        self.assertIn("use SEEK email sign-in in normal Chrome or Edge, not Google sign-in", app_source)
        self.assertIn("Regenerate cover letter", app_source)
        self.assertIn("Interview prep", app_source)

    def test_applied_today_count_uses_real_current_date(self) -> None:
        tracker = SimpleNamespace(
            database=Mock(),
            search_jobs=Mock(
                side_effect=[
                    pd.DataFrame(columns=["job_id", "apply_type", "has_quick_apply_button", "location", "application_status"]),
                    pd.DataFrame(
                        [
                            {
                                "job_id": 91,
                                "apply_type": "Quick Apply",
                                "has_quick_apply_button": 1,
                                "location": "Auckland",
                                "application_status": "applied",
                                "applied_at": f"{date.today().isoformat()}T09:00:00",
                                "submitted_at": "",
                            },
                            {
                                "job_id": 92,
                                "apply_type": "Quick Apply",
                                "has_quick_apply_button": 1,
                                "location": "Auckland",
                                "application_status": "applied",
                                "applied_at": f"{(date.today() - timedelta(days=1)).isoformat()}T09:00:00",
                                "submitted_at": "",
                            },
                        ]
                    ),
                ]
            ),
        )

        with (
            patch("app._latest_apply_runs_by_job", return_value={}),
            patch("app._load_timing_history", return_value=pd.DataFrame()),
        ):
            summary = _build_queue_summary(tracker)

        self.assertEqual(summary["applied_today_count"], 1)

    def test_shared_queue_summary_prominently_reports_required_actions(self) -> None:
        tracker = SimpleNamespace()
        summary = {
            "unprocessed_count": 7,
            "ready_count": 3,
            "problem_count": 2,
            "applied_today_count": 1,
            "applied_all_time_count": 10,
            "estimated_prepare_total": "14m",
            "estimated_submit_total": "3m",
        }
        metric_columns = [Mock(), Mock(), Mock(), Mock()]

        with (
            patch("app._build_queue_summary", return_value=summary),
            patch("app.st.columns", return_value=metric_columns),
            patch("app.st.caption") as caption,
            patch("app.st.error") as error,
            patch("app.st.warning") as warning,
            patch("app.st.success") as success,
        ):
            render_shared_queue_summary(tracker)

        metric_columns[0].metric.assert_called_once_with("Actually submitted today", 1)
        metric_columns[1].metric.assert_called_once_with("Ready but NOT submitted", 3, delta="Est. 3m")
        metric_columns[2].metric.assert_called_once_with("Blocked — needs you", 2)
        metric_columns[3].metric.assert_called_once_with("Not prepared in SEEK", 7, delta="Est. 14m")
        caption.assert_called_once_with("Actually submitted all time: 10")
        error.assert_called_once()
        self.assertEqual(warning.call_count, 2)
        success.assert_not_called()


if __name__ == "__main__":
    unittest.main()
