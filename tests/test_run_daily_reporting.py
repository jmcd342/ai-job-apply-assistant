from __future__ import annotations

import unittest

from src.daily_application_runner import application_outcome_messages


class DailyApplicationOutcomeReportingTests(unittest.TestCase):
    def test_prepared_but_unsubmitted_applications_require_action(self) -> None:
        outcome, action = application_outcome_messages(
            prepared=11,
            submitted_by_daily_run=0,
            submitted_today=2,
        )

        self.assertEqual(
            outcome,
            "APPLICATION OUTCOME: applications_submitted_by_daily_run=0 "
            "applications_submitted_today=2 packages_prepared=11",
        )
        self.assertIn("ACTION REQUIRED", action)
        self.assertIn("11 prepared application(s) were NOT submitted", action)
        self.assertIn("Ready to submit", action)
        self.assertIn("Problems", action)

    def test_fully_submitted_batch_reports_no_action(self) -> None:
        outcome, action = application_outcome_messages(
            prepared=2,
            submitted_by_daily_run=2,
            submitted_today=2,
        )

        self.assertEqual(
            outcome,
            "APPLICATION OUTCOME: applications_submitted_by_daily_run=2 "
            "applications_submitted_today=2 packages_prepared=2",
        )
        self.assertEqual(action, "ACTION REQUIRED: none for applications prepared during this run.")


if __name__ == "__main__":
    unittest.main()
