from __future__ import annotations

from pathlib import Path

import pandas as pd

from src.database import Database, EMPTY_JOBS_COLUMNS


class ApplicationTracker:
    def __init__(self, database: Database, app_root: Path) -> None:
        self.database = database
        self.app_root = Path(app_root)

    def search_jobs(
        self,
        query: str = "",
        source: str = "All",
        status: str = "All",
        min_fit_score: int = 0,
        company: str = "",
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> pd.DataFrame:
        jobs = self.database.search_jobs(
            query=query,
            source=source,
            status=status,
            min_fit_score=min_fit_score,
            company=company,
            date_from=date_from,
            date_to=date_to,
        )
        if jobs.empty:
            return pd.DataFrame(columns=EMPTY_JOBS_COLUMNS)
        return jobs

    def get_job_details(self, job_id: int) -> dict | None:
        return self.database.get_job_details(job_id)

    def update_application_status(self, job_id: int, status: str, note: str | None = None) -> None:
        self.database.update_application_state(job_id, status, note)
        self.database.log_run("manual_update", "status_changed", "success", f"Job {job_id} -> {status}")

    def list_generated_documents(self, job_id: int) -> list[dict]:
        return self.database.list_generated_documents(job_id)

    def jobs_for_daily_drafting(self) -> list[int]:
        return self.database.fetch_jobs_for_drafting()

    def approval_queue(self) -> pd.DataFrame:
        jobs = self.database.search_jobs(status="All")
        if jobs.empty:
            return jobs
        queue = jobs[(jobs["status"].isin(["drafted", "needs_user_action"])) | (jobs["ready_to_apply"] == 1)]
        return queue.sort_values(by=["fit_score", "date_found"], ascending=[False, False], kind="stable")

    def latest_drafted_applications(self, limit: int = 5) -> pd.DataFrame:
        jobs = self.database.search_jobs(status="All")
        if jobs.empty:
            return jobs
        drafted = jobs[jobs["status"].isin(["drafted", "needs_user_action"])]
        drafted = drafted.sort_values(by=["imported_at", "date_found"], ascending=[False, False], kind="stable")
        return drafted.head(limit)
