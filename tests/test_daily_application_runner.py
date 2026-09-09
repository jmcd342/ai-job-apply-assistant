from __future__ import annotations

import unittest
import tempfile
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pandas as pd

from src.daily_application_runner import _latest_review_urls, run_daily_application_cycle


class FakeDatabase:
    def __init__(self, jobs: list[dict]) -> None:
        self.db_path = Path("test.db")
        self.jobs = {int(job["job_id"]): dict(job) for job in jobs}
        self.logs: list[tuple] = []
        self.updates: list[tuple] = []

    def search_jobs(self, **_: object) -> pd.DataFrame:
        return pd.DataFrame(self.jobs.values())

    def get_job_details(self, job_id: int) -> dict | None:
        return self.jobs.get(job_id)

    def update_application_state(self, job_id: int, status: str, note: str = "") -> None:
        self.updates.append((job_id, status, note))
        self.jobs[job_id]["application_status"] = status

    def log_run(self, *args: object) -> None:
        self.logs.append(args)


class DailyApplicationRunnerTests(unittest.TestCase):
    def test_latest_review_urls_reads_latest_ready_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            database = FakeDatabase([])
            database.db_path = root / "job_assistant.db"
            run_dir = root / "apply_runs" / "job_7"
            run_dir.mkdir(parents=True)
            (run_dir / "final_review_checklist.json").write_text(
                json.dumps({"job_id": 7, "status": "ready_for_human_review"}),
                encoding="utf-8",
            )
            (run_dir / "apply_run_log.txt").write_text(
                "status=ready_for_human_review\nfinal_url=https://seek.example/7/apply/review\n",
                encoding="utf-8",
            )

            self.assertEqual(_latest_review_urls(database), {7: "https://seek.example/7/apply/review"})

    def test_submits_ready_jobs_before_preparing_and_submitting_new_jobs(self) -> None:
        database = FakeDatabase(
            [
                {
                    "job_id": 1,
                    "title": "Ready role",
                    "source": "seek",
                    "application_status": "ready_to_apply",
                    "apply_type": "Quick Apply",
                    "url": "https://seek.example/1",
                },
                {
                    "job_id": 2,
                    "title": "Fresh role",
                    "source": "seek",
                    "application_status": "reviewed",
                    "apply_type": "Quick Apply",
                    "url": "https://seek.example/2",
                },
            ]
        )
        submitted_ids: list[int] = []

        def submit(job_id: int, *_: object, **__: object) -> SimpleNamespace:
            submitted_ids.append(job_id)
            return SimpleNamespace(status="applied", error="", final_url="https://seek.example/success")

        prepared = [
            SimpleNamespace(
                job_id=2,
                title="Fresh role",
                status="ready_for_human_review",
                failure_reason="",
                job_url="https://seek.example/2/apply/review",
                evidence_folder="missing-test-run-folder",
            )
        ]
        prepare = Mock(return_value=prepared)

        result = run_daily_application_cycle(
            database,
            object(),
            max_applications=2,
            submit_func=submit,
            prepare_func=prepare,
        )

        self.assertEqual(submitted_ids, [1, 2])
        self.assertEqual(result.prepared, 1)
        self.assertEqual(result.attempted, 2)
        self.assertEqual(result.submitted, 2)
        prepare.assert_called_once()
        self.assertEqual(prepare.call_args.kwargs["max_jobs"], 1)

    def test_blocking_submit_failure_stops_before_preparing_more_jobs(self) -> None:
        database = FakeDatabase(
            [
                {
                    "job_id": 1,
                    "title": "Ready role",
                    "source": "seek",
                    "application_status": "ready_to_apply",
                    "apply_type": "Quick Apply",
                    "url": "https://seek.example/1",
                }
            ]
        )
        prepare = Mock(return_value=[])

        result = run_daily_application_cycle(
            database,
            object(),
            max_applications=10,
            submit_func=lambda *args, **kwargs: SimpleNamespace(
                status="paused_for_captcha", error="CAPTCHA required", final_url=""
            ),
            prepare_func=prepare,
        )

        self.assertTrue(result.stopped_early)
        self.assertEqual(result.blocked, 1)
        self.assertEqual(result.submitted, 0)
        prepare.assert_not_called()


if __name__ == "__main__":
    unittest.main()
