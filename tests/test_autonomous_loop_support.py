from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.autonomous_loop_support import (
    append_jsonl,
    build_component_result,
    classify_auth_blocker,
    compute_health_score,
    has_repeated_unsuccessful_attempt,
    ledger_attempt_signature,
    read_jsonl,
)


class HealthScoreTests(unittest.TestCase):
    def test_compute_health_score_sums_weights_for_passing_components(self) -> None:
        components = [
            build_component_result("repo_import_health", passed=True, summary="ok"),
            build_component_result("core_db_health", passed=False, summary="broken"),
            build_component_result("gmail_auth_connectivity", passed=True, summary="ok"),
        ]
        self.assertEqual(25, compute_health_score(components))


class AuthClassificationTests(unittest.TestCase):
    def test_classifies_gmail_oauth_expiry(self) -> None:
        blocker = classify_auth_blocker(
            service="gmail",
            triggering_workflow="gmail_sync",
            text="OAuth token expired or revoked. Reconnect Gmail.",
            command_or_link="python app.py",
        )
        self.assertIsNotNone(blocker)
        assert blocker is not None
        self.assertEqual("Gmail", blocker.service)
        self.assertTrue(blocker.safe_to_resume_after_completion)

    def test_classifies_seek_login_required(self) -> None:
        blocker = classify_auth_blocker(
            service="seek",
            triggering_workflow="seek_session_validation",
            text="login required - session expired",
            command_or_link="python scripts/run_validate_seek_session.py --db-path data/job_assistant.db --visible true",
        )
        self.assertIsNotNone(blocker)
        assert blocker is not None
        self.assertEqual("SEEK", blocker.service)
        self.assertIn("Sign in to SEEK", blocker.required_user_action)

    def test_non_auth_runtime_error_returns_none(self) -> None:
        blocker = classify_auth_blocker(
            service="seek",
            triggering_workflow="seek_prepare_canary",
            text="NameError: tracker is not defined",
        )
        self.assertIsNone(blocker)


class LedgerTests(unittest.TestCase):
    def test_append_and_read_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "ledger.jsonl"
            append_jsonl(path, {"attempt": 1, "outcome": "success"})
            append_jsonl(path, {"attempt": 2, "outcome": "noop"})
            rows = read_jsonl(path)
            self.assertEqual(2, len(rows))
            self.assertEqual("noop", rows[1]["outcome"])

    def test_repeated_unsuccessful_attempt_detection(self) -> None:
        files_changed = ["data/job_assistant.db"]
        signature = ledger_attempt_signature("db", "normalize blank jobs.application_status from applications.status", files_changed)
        rows = [
            {
                "signature": signature,
                "health_before": 60,
                "health_after": 60,
            }
        ]
        self.assertTrue(
            has_repeated_unsuccessful_attempt(
                rows,
                failure_cluster="db",
                command_run="normalize blank jobs.application_status from applications.status",
                files_changed=files_changed,
            )
        )


if __name__ == "__main__":
    unittest.main()
