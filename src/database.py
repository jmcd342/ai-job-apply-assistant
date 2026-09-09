from __future__ import annotations

import builtins
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

import pandas as pd


def print(*args: object, **kwargs: object) -> None:  # type: ignore[override]
    try:
        builtins.print(*args, **kwargs)
    except OSError:
        pass

VALID_STATUSES = {
    "discovered",
    "enriched",
    "reviewed",
    "ready_to_apply",
    "shortlisted",
    "drafted",
    "needs_user_action",
    "applied",
    "rejected",
    "interview",
    "archived",
    "ignored",
    "closed",
}

EMPTY_JOBS_COLUMNS = [
    "job_id",
    "title",
    "company",
    "source",
    "location",
    "fit_score",
    "fit_category",
    "role_family",
    "opportunity_quality",
    "reason_for_rating",
    "status",
    "application_status",
    "job_url",
    "imported_at",
    "url",
    "salary",
    "description",
    "about_the_job",
    "full_description",
    "source_page_text",
    "date_found",
    "date_posted",
    "date_posted_text",
    "apply_type",
    "has_apply_button",
    "has_quick_apply_button",
    "apply_area_salary_text",
    "email_received_at",
    "age_in_days",
    "import_channel",
    "enrichment_status",
    "enriched_at",
    "enrichment_error",
    "enriched_title",
    "enriched_company",
    "enriched_location",
    "enriched_salary",
    "employment_type",
    "document_basis",
    "selected_cv_profile",
    "selected_cv_path",
    "cv_selection_reason",
    "salary_expectation_text",
    "salary_estimate_low",
    "salary_estimate_high",
    "salary_estimate_reasoning_json",
    "matched_skills_json",
    "missing_skills_json",
    "seniority_level",
    "risk_flags_json",
    "cover_letter_path",
    "cover_letter_preview",
    "cover_letter_basis",
    "interview_prep_md",
    "interview_prep_path",
    "demo_recommendation_md",
    "demo_recommendation_path",
    "application_packet_path",
    "ready_to_apply_at",
    "applied_at",
    "submitted_at",
    "rejected_at",
    "last_response_type",
    "last_response_at",
    "closed_reason",
    "closed_at",
    "follow_up_feedback_summary",
    "follow_up_feedback_tags_json",
    "follow_up_reply_last_action",
    "follow_up_reply_last_at",
    "rationale_path",
    "ready_to_apply",
]


class Database:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        print(f"[database] DB_PATH={self.db_path.resolve()}", flush=True)
        self._initialise()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA journal_mode=WAL")
        except Exception:  # noqa: BLE001
            pass
        try:
            connection.execute("PRAGMA synchronous=NORMAL")
        except Exception:  # noqa: BLE001
            pass
        try:
            connection.execute("PRAGMA busy_timeout = 30000")
        except Exception:  # noqa: BLE001
            pass
        return connection

    @contextmanager
    def _managed_connection(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialise(self) -> None:
        with self._managed_connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT NOT NULL,
                    source_job_id TEXT,
                    canonical_url TEXT,
                    url TEXT,
                    title TEXT NOT NULL,
                    company TEXT NOT NULL,
                    location TEXT,
                    salary TEXT,
                    description TEXT NOT NULL,
                    date_found TEXT NOT NULL,
                    email_received_at TEXT,
                    age_in_days INTEGER DEFAULT 0,
                    fit_score INTEGER DEFAULT 0,
                    seniority TEXT,
                    extracted_keywords TEXT,
                    responsibilities TEXT,
                    requirements TEXT,
                    import_channel TEXT,
                    imported_at TEXT,
                    metadata_json TEXT DEFAULT '{}',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_source_job_id
                ON jobs(source, source_job_id)
                WHERE source_job_id IS NOT NULL;

                CREATE INDEX IF NOT EXISTS idx_jobs_canonical_url ON jobs(canonical_url);
                CREATE INDEX IF NOT EXISTS idx_jobs_company_title ON jobs(company, title, location);

                CREATE TABLE IF NOT EXISTS applications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id INTEGER NOT NULL UNIQUE,
                    status TEXT NOT NULL DEFAULT 'discovered',
                    duplicate_of_job_id INTEGER,
                    ready_to_apply INTEGER NOT NULL DEFAULT 0,
                    user_approved INTEGER NOT NULL DEFAULT 0,
                    notes TEXT,
                    submitted_at TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(job_id) REFERENCES jobs(id),
                    FOREIGN KEY(duplicate_of_job_id) REFERENCES jobs(id)
                );

                CREATE TABLE IF NOT EXISTS generated_documents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id INTEGER NOT NULL,
                    application_id INTEGER,
                    doc_type TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    content_preview TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(job_id) REFERENCES jobs(id),
                    FOREIGN KEY(application_id) REFERENCES applications(id)
                );

                CREATE TABLE IF NOT EXISTS run_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_type TEXT NOT NULL,
                    action TEXT NOT NULL,
                    status TEXT NOT NULL,
                    details TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS processed_email_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    provider TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    source TEXT,
                    imported_jobs INTEGER NOT NULL DEFAULT 0,
                    duplicate_jobs INTEGER NOT NULL DEFAULT 0,
                    filtered_jobs INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    processed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    details TEXT,
                    UNIQUE(provider, message_id)
                );

                CREATE TABLE IF NOT EXISTS app_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS application_email_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id INTEGER NOT NULL,
                    provider TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    thread_id TEXT,
                    subject TEXT,
                    sender TEXT,
                    sender_email TEXT,
                    received_at TEXT,
                    classification TEXT NOT NULL,
                    match_score REAL DEFAULT 0,
                    raw_snippet TEXT,
                    feedback_summary TEXT,
                    feedback_tags_json TEXT DEFAULT '[]',
                    reply_action TEXT,
                    reply_subject TEXT,
                    reply_body TEXT,
                    reply_reference_message_id TEXT,
                    processed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(provider, message_id),
                    FOREIGN KEY(job_id) REFERENCES jobs(id)
                );
                """
            )
            self._ensure_columns(
                connection,
                "jobs",
                {
                    "import_channel": "TEXT",
                    "imported_at": "TEXT",
                    "email_received_at": "TEXT",
                    "age_in_days": "INTEGER DEFAULT 0",
                    "date_posted": "TEXT",
                    "date_posted_text": "TEXT",
                    "about_the_job": "TEXT",
                    "full_description": "TEXT",
                    "enriched_title": "TEXT",
                    "enriched_company": "TEXT",
                    "enriched_location": "TEXT",
                    "enriched_salary": "TEXT",
                    "employment_type": "TEXT",
                    "apply_type": "TEXT",
                    "has_apply_button": "INTEGER DEFAULT 0",
                    "has_quick_apply_button": "INTEGER DEFAULT 0",
                    "apply_area_salary_text": "TEXT",
                    "enriched_at": "TEXT",
                    "enrichment_status": "TEXT",
                    "enrichment_error": "TEXT",
                    "source_page_text": "TEXT",
                    "document_basis": "TEXT",
                    "selected_cv_profile": "TEXT",
                    "selected_cv_path": "TEXT",
                    "cv_selection_reason": "TEXT",
                    "salary_expectation_text": "TEXT",
                    "salary_estimate_low": "INTEGER",
                    "salary_estimate_high": "INTEGER",
                    "salary_estimate_reasoning_json": "TEXT",
                    "fit_category": "TEXT",
                    "matched_skills_json": "TEXT",
                    "missing_skills_json": "TEXT",
                    "seniority_level": "TEXT",
                    "role_family": "TEXT",
                    "opportunity_quality": "TEXT",
                    "risk_flags_json": "TEXT",
                    "cover_letter_path": "TEXT",
                    "cover_letter_preview": "TEXT",
                    "cover_letter_basis": "TEXT",
                    "interview_prep_md": "TEXT",
                    "interview_prep_path": "TEXT",
                    "demo_recommendation_md": "TEXT",
                    "demo_recommendation_path": "TEXT",
                    "application_packet_path": "TEXT",
                    "application_status": "TEXT",
                    "ready_to_apply_at": "TEXT",
                    "applied_at": "TEXT",
                    "rejected_at": "TEXT",
                    "last_response_type": "TEXT",
                    "last_response_at": "TEXT",
                    "closed_reason": "TEXT",
                    "closed_at": "TEXT",
                    "follow_up_feedback_summary": "TEXT",
                    "follow_up_feedback_tags_json": "TEXT",
                    "follow_up_reply_last_action": "TEXT",
                    "follow_up_reply_last_at": "TEXT",
                },
            )
            connection.execute(
                """
                UPDATE jobs
                SET application_status = 'discovered'
                WHERE application_status IS NULL OR trim(application_status) = ''
                """
            )
            connection.commit()

    def _ensure_columns(self, connection: sqlite3.Connection, table_name: str, columns: dict[str, str]) -> None:
        existing = {row["name"] for row in connection.execute(f"PRAGMA table_info({table_name})").fetchall()}
        for column_name, column_type in columns.items():
            if column_name not in existing:
                connection.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}")
        connection.commit()

    def execute(self, query: str, params: tuple[Any, ...] = ()) -> None:
        with self._managed_connection() as connection:
            connection.execute(query, params)
            connection.commit()

    def execute_many(self, query: str, params_seq: list[tuple[Any, ...]]) -> None:
        if not params_seq:
            return
        with self._managed_connection() as connection:
            connection.executemany(query, params_seq)
            connection.commit()

    def fetch_one(self, query: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        with self._managed_connection() as connection:
            return connection.execute(query, params).fetchone()

    def fetch_all(self, query: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self._managed_connection() as connection:
            return connection.execute(query, params).fetchall()

    def insert_job(self, payload: dict[str, Any]) -> int:
        with self._managed_connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO jobs (
                    source, source_job_id, canonical_url, url, title, company, location, salary,
                    description, date_found, email_received_at, age_in_days, fit_score, seniority, extracted_keywords,
                    responsibilities, requirements, import_channel, imported_at, metadata_json,
                    application_status, fit_category, matched_skills_json, missing_skills_json, seniority_level,
                    role_family, opportunity_quality, risk_flags_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload["source"],
                    payload.get("source_job_id"),
                    payload.get("canonical_url"),
                    payload.get("url"),
                    payload["title"],
                    payload["company"],
                    payload.get("location"),
                    payload.get("salary"),
                    payload["description"],
                    payload["date_found"],
                    payload.get("email_received_at"),
                    payload.get("age_in_days", 0),
                    payload.get("fit_score", 0),
                    payload.get("seniority"),
                    json.dumps(payload.get("extracted_keywords", [])),
                    payload.get("responsibilities"),
                    payload.get("requirements"),
                    payload.get("import_channel"),
                    payload.get("imported_at"),
                    json.dumps(payload.get("metadata", {})),
                    payload.get("application_status", "discovered"),
                    payload.get("fit_category"),
                    json.dumps(payload.get("matched_skills", [])),
                    json.dumps(payload.get("missing_skills", [])),
                    payload.get("seniority_level"),
                    payload.get("role_family"),
                    payload.get("opportunity_quality"),
                    json.dumps(payload.get("risk_flags", [])),
                ),
            )
            job_id = int(cursor.lastrowid)
            connection.execute("INSERT INTO applications (job_id, status) VALUES (?, 'discovered')", (job_id,))
            connection.commit()
            return job_id

    def update_job_enrichment(self, job_id: int, enrichment: dict[str, Any], fit_score: int) -> None:
        self.execute(
            """
            UPDATE jobs
            SET fit_score = ?, seniority = ?, extracted_keywords = ?, responsibilities = ?, requirements = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (
                fit_score,
                enrichment.get("seniority"),
                json.dumps(enrichment.get("keywords", [])),
                enrichment.get("responsibilities"),
                enrichment.get("requirements"),
                job_id,
            ),
        )

    def update_job_content(
        self,
        job_id: int,
        *,
        description: str,
        url: str | None,
        canonical_url: str | None,
        metadata: dict[str, Any] | None = None,
        email_received_at: str | None = None,
        age_in_days: int | None = None,
    ) -> None:
        self.execute(
            """
            UPDATE jobs
            SET description = ?, url = ?, canonical_url = ?, metadata_json = ?, email_received_at = ?, age_in_days = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (
                description,
                url,
                canonical_url,
                json.dumps(metadata or {}),
                email_received_at,
                age_in_days if age_in_days is not None else 0,
                job_id,
            ),
        )

    def search_jobs(
        self,
        query: str = "",
        source: str | None = None,
        status: str | None = None,
        min_fit_score: int = 0,
        company: str = "",
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> pd.DataFrame:
        sql = """
            SELECT
                jobs.id AS job_id,
                jobs.title,
                jobs.company,
                jobs.location,
                jobs.source,
                jobs.url,
                jobs.salary,
                jobs.description,
                jobs.about_the_job,
                jobs.full_description,
                jobs.source_page_text,
                jobs.date_found,
                jobs.date_posted,
                jobs.date_posted_text,
                jobs.apply_type,
                jobs.has_apply_button,
                jobs.has_quick_apply_button,
                jobs.apply_area_salary_text,
                jobs.email_received_at,
                jobs.age_in_days,
                jobs.fit_score,
                jobs.fit_category,
                jobs.role_family,
                jobs.opportunity_quality,
                jobs.import_channel,
                jobs.imported_at,
                jobs.enriched_at,
                jobs.enrichment_status,
                jobs.enrichment_error,
                jobs.enriched_title,
                jobs.enriched_company,
                jobs.enriched_location,
                jobs.enriched_salary,
                jobs.employment_type,
                jobs.document_basis,
                jobs.selected_cv_profile,
                jobs.selected_cv_path,
                jobs.cv_selection_reason,
                jobs.salary_expectation_text,
                jobs.matched_skills_json,
                jobs.missing_skills_json,
                jobs.seniority_level,
                jobs.risk_flags_json,
                jobs.cover_letter_path,
                jobs.cover_letter_preview,
                jobs.cover_letter_basis,
                jobs.interview_prep_md,
                jobs.interview_prep_path,
                jobs.demo_recommendation_md,
                jobs.demo_recommendation_path,
                jobs.application_packet_path,
                jobs.application_status,
                jobs.ready_to_apply_at,
                jobs.applied_at,
                jobs.rejected_at,
                jobs.last_response_type,
                jobs.last_response_at,
                jobs.closed_reason,
                jobs.closed_at,
                jobs.follow_up_feedback_summary,
                jobs.follow_up_feedback_tags_json,
                jobs.follow_up_reply_last_action,
                jobs.follow_up_reply_last_at,
                (
                    SELECT file_path
                    FROM generated_documents
                    WHERE generated_documents.job_id = jobs.id
                      AND generated_documents.doc_type = 'application_rationale'
                    ORDER BY generated_documents.id DESC
                    LIMIT 1
                ) AS rationale_path,
                applications.status,
                applications.ready_to_apply,
                applications.submitted_at
            FROM jobs
            JOIN applications ON applications.job_id = jobs.id
            WHERE jobs.fit_score >= ?
        """
        params: list[Any] = [min_fit_score]

        if source and source != "All":
            sql += " AND jobs.source = ?"
            params.append(source)
        if status and status != "All":
            sql += " AND COALESCE(NULLIF(trim(jobs.application_status), ''), applications.status) = ?"
            params.append(status)
        if company:
            sql += " AND lower(jobs.company) LIKE ?"
            params.append(f"%{company.lower()}%")
        if date_from:
            sql += " AND jobs.date_found >= ?"
            params.append(date_from)
        if date_to:
            sql += " AND jobs.date_found <= ?"
            params.append(date_to)
        if query:
            sql += """
                AND (
                    lower(jobs.title) LIKE ?
                    OR lower(jobs.company) LIKE ?
                    OR lower(jobs.description) LIKE ?
                    OR lower(COALESCE(jobs.about_the_job, '')) LIKE ?
                    OR lower(COALESCE(jobs.full_description, '')) LIKE ?
                    OR lower(COALESCE(jobs.source_page_text, '')) LIKE ?
                    OR lower(jobs.extracted_keywords) LIKE ?
                )
            """
            term = f"%{query.lower()}%"
            params.extend([term, term, term, term, term, term, term])

        sql += " ORDER BY jobs.fit_score DESC, jobs.date_found DESC, jobs.id DESC"
        rows = [dict(row) for row in self.fetch_all(sql, tuple(params))]
        if not rows:
            return pd.DataFrame(columns=EMPTY_JOBS_COLUMNS)

        frame = pd.DataFrame(rows)
        if "url" in frame.columns and "job_url" not in frame.columns:
            frame["job_url"] = frame["url"]
        if "reason_for_rating" not in frame.columns:
            frame["reason_for_rating"] = ""
        for column in EMPTY_JOBS_COLUMNS:
            if column not in frame.columns:
                frame[column] = ""
        return frame

    def get_jobs_debug_snapshot(self) -> dict[str, Any]:
        db_exists = self.db_path.exists()
        file_size = self.db_path.stat().st_size if db_exists else 0
        total_jobs = int(self.fetch_one("SELECT COUNT(*) AS count FROM jobs")["count"]) if db_exists else 0
        total_applications = int(self.fetch_one("SELECT COUNT(*) AS count FROM applications")["count"]) if db_exists else 0
        latest_job = self.fetch_one(
            """
            SELECT id, title, imported_at, created_at
            FROM jobs
            ORDER BY COALESCE(imported_at, created_at) DESC, id DESC
            LIMIT 1
            """
        ) if db_exists else None
        application_status_counts = [
            dict(row)
            for row in self.fetch_all(
                """
                SELECT COALESCE(NULLIF(trim(application_status), ''), 'NULL') AS status, COUNT(*) AS count
                FROM jobs
                GROUP BY COALESCE(NULLIF(trim(application_status), ''), 'NULL')
                ORDER BY count DESC, status ASC
                """
            )
        ] if db_exists else []
        enrichment_status_counts = [
            dict(row)
            for row in self.fetch_all(
                """
                SELECT COALESCE(NULLIF(trim(enrichment_status), ''), 'NULL') AS status, COUNT(*) AS count
                FROM jobs
                GROUP BY COALESCE(NULLIF(trim(enrichment_status), ''), 'NULL')
                ORDER BY count DESC, status ASC
                """
            )
        ] if db_exists else []
        latest_jobs = [
            dict(row)
            for row in self.fetch_all(
                """
                SELECT
                    id,
                    title,
                    source,
                    application_status,
                    enrichment_status,
                    closed_reason,
                    closed_at,
                    imported_at,
                    created_at
                FROM jobs
                ORDER BY COALESCE(imported_at, created_at) DESC, id DESC
                LIMIT 10
                """
            )
        ] if db_exists else []
        return {
            "db_path": str(self.db_path.resolve()),
            "db_exists": db_exists,
            "file_size": file_size,
            "total_jobs": total_jobs,
            "total_applications": total_applications,
            "latest_job": dict(latest_job) if latest_job else None,
            "application_status_counts": application_status_counts,
            "enrichment_status_counts": enrichment_status_counts,
            "latest_jobs": latest_jobs,
        }

    def get_job_details(self, job_id: int) -> dict[str, Any] | None:
        row = self.fetch_one(
            """
            SELECT
                jobs.id AS job_id,
                jobs.*,
                applications.status,
                applications.ready_to_apply,
                applications.submitted_at,
                applications.user_approved,
                applications.notes
            FROM jobs
            JOIN applications ON applications.job_id = jobs.id
            WHERE jobs.id = ?
            """,
            (job_id,),
        )
        if not row:
            return None
        payload = dict(row)
        try:
            payload["metadata"] = json.loads(payload.get("metadata_json") or "{}")
        except Exception:  # noqa: BLE001
            payload["metadata"] = {}
        return payload

    def update_job_enrichment_result(
        self,
        job_id: int,
        *,
        full_description: str | None,
        about_the_job: str | None,
        source_page_text: str | None,
        enrichment_status: str,
        enrichment_error: str | None,
        enriched_at: str | None,
        date_posted: str | None = None,
        date_posted_text: str | None = None,
        title: str | None = None,
        company: str | None = None,
        location: str | None = None,
        salary: str | None = None,
        employment_type: str | None = None,
        apply_type: str | None = None,
        has_apply_button: bool | None = None,
        has_quick_apply_button: bool | None = None,
        apply_area_salary_text: str | None = None,
        fit_score: int | None = None,
        seniority: str | None = None,
        extracted_keywords: list[str] | None = None,
        responsibilities: str | None = None,
        requirements: str | None = None,
    ) -> None:
        self.execute(
            """
            UPDATE jobs
            SET
                full_description = COALESCE(?, full_description),
                about_the_job = COALESCE(?, about_the_job),
                source_page_text = COALESCE(?, source_page_text),
                enrichment_status = ?,
                enrichment_error = ?,
                enriched_at = ?,
                date_posted = COALESCE(?, date_posted),
                date_posted_text = COALESCE(?, date_posted_text),
                title = COALESCE(?, title),
                company = COALESCE(?, company),
                location = COALESCE(?, location),
                salary = COALESCE(?, salary),
                enriched_title = COALESCE(?, enriched_title),
                enriched_company = COALESCE(?, enriched_company),
                enriched_location = COALESCE(?, enriched_location),
                enriched_salary = COALESCE(?, enriched_salary),
                employment_type = COALESCE(?, employment_type),
                apply_type = COALESCE(?, apply_type),
                has_apply_button = COALESCE(?, has_apply_button),
                has_quick_apply_button = COALESCE(?, has_quick_apply_button),
                apply_area_salary_text = COALESCE(?, apply_area_salary_text),
                fit_score = COALESCE(?, fit_score),
                seniority = COALESCE(?, seniority),
                extracted_keywords = COALESCE(?, extracted_keywords),
                responsibilities = COALESCE(?, responsibilities),
                requirements = COALESCE(?, requirements),
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (
                full_description,
                about_the_job,
                source_page_text,
                enrichment_status,
                enrichment_error,
                enriched_at,
                date_posted,
                date_posted_text,
                title,
                company,
                location,
                salary,
                title,
                company,
                location,
                salary,
                employment_type,
                apply_type,
                (1 if has_apply_button else 0) if has_apply_button is not None else None,
                (1 if has_quick_apply_button else 0) if has_quick_apply_button is not None else None,
                apply_area_salary_text,
                fit_score,
                seniority,
                json.dumps(extracted_keywords) if extracted_keywords is not None else None,
                responsibilities,
                requirements,
                job_id,
            ),
        )

    def fetch_jobs_missing_enrichment(self) -> list[int]:
        rows = self.fetch_all(
            """
            SELECT id
            FROM jobs
            WHERE url IS NOT NULL
              AND trim(url) != ''
              AND COALESCE(enrichment_status, '') NOT IN ('success')
            ORDER BY id DESC
            """
        )
        return [int(row["id"]) for row in rows]

    def set_job_document_basis(self, job_id: int, document_basis: str) -> None:
        self.execute(
            """
            UPDATE jobs
            SET document_basis = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (document_basis, job_id),
        )

    def update_job_career_assets(
        self,
        job_id: int,
        *,
        selected_cv_profile: str | None = None,
        selected_cv_path: str | None = None,
        cv_selection_reason: str | None = None,
        salary_expectation_text: str | None = None,
        salary_estimate_low: int | None = None,
        salary_estimate_high: int | None = None,
        salary_estimate_reasoning_json: str | None = None,
        fit_category: str | None = None,
        matched_skills_json: str | None = None,
        missing_skills_json: str | None = None,
        seniority_level: str | None = None,
        role_family: str | None = None,
        opportunity_quality: str | None = None,
        risk_flags_json: str | None = None,
        cover_letter_path: str | None = None,
        cover_letter_preview: str | None = None,
        cover_letter_basis: str | None = None,
        interview_prep_md: str | None = None,
        interview_prep_path: str | None = None,
        demo_recommendation_md: str | None = None,
        demo_recommendation_path: str | None = None,
        application_packet_path: str | None = None,
        document_basis: str | None = None,
    ) -> None:
        self.execute(
            """
            UPDATE jobs
            SET
                selected_cv_profile = COALESCE(?, selected_cv_profile),
                selected_cv_path = COALESCE(?, selected_cv_path),
                cv_selection_reason = COALESCE(?, cv_selection_reason),
                salary_expectation_text = COALESCE(?, salary_expectation_text),
                salary_estimate_low = COALESCE(?, salary_estimate_low),
                salary_estimate_high = COALESCE(?, salary_estimate_high),
                salary_estimate_reasoning_json = COALESCE(?, salary_estimate_reasoning_json),
                fit_category = COALESCE(?, fit_category),
                matched_skills_json = COALESCE(?, matched_skills_json),
                missing_skills_json = COALESCE(?, missing_skills_json),
                seniority_level = COALESCE(?, seniority_level),
                role_family = COALESCE(?, role_family),
                opportunity_quality = COALESCE(?, opportunity_quality),
                risk_flags_json = COALESCE(?, risk_flags_json),
                cover_letter_path = COALESCE(?, cover_letter_path),
                cover_letter_preview = COALESCE(?, cover_letter_preview),
                cover_letter_basis = COALESCE(?, cover_letter_basis),
                interview_prep_md = COALESCE(?, interview_prep_md),
                interview_prep_path = COALESCE(?, interview_prep_path),
                demo_recommendation_md = COALESCE(?, demo_recommendation_md),
                demo_recommendation_path = COALESCE(?, demo_recommendation_path),
                application_packet_path = COALESCE(?, application_packet_path),
                document_basis = COALESCE(?, document_basis),
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (
                selected_cv_profile,
                selected_cv_path,
                cv_selection_reason,
                salary_expectation_text,
                salary_estimate_low,
                salary_estimate_high,
                salary_estimate_reasoning_json,
                fit_category,
                matched_skills_json,
                missing_skills_json,
                seniority_level,
                role_family,
                opportunity_quality,
                risk_flags_json,
                cover_letter_path,
                cover_letter_preview,
                cover_letter_basis,
                interview_prep_md,
                interview_prep_path,
                demo_recommendation_md,
                demo_recommendation_path,
                application_packet_path,
                document_basis,
                job_id,
            ),
        )

    def update_application_state(self, job_id: int, status: str, note: str | None = None) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        ready_to_apply_at = now if status == "ready_to_apply" else None
        applied_at = now if status == "applied" else None
        rejected_at = now if status in {"rejected", "ignored"} else None
        closed_at = now if status == "closed" else None
        self.execute(
            """
            UPDATE jobs
            SET
                application_status = ?,
                ready_to_apply_at = COALESCE(?, ready_to_apply_at),
                applied_at = COALESCE(?, applied_at),
                rejected_at = COALESCE(?, rejected_at),
                closed_at = COALESCE(?, closed_at),
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (status, ready_to_apply_at, applied_at, rejected_at, closed_at, job_id),
        )
        self.update_application_status(job_id, status, note)

    def update_application_status(self, job_id: int, status: str, note: str | None = None) -> None:
        if status not in VALID_STATUSES:
            raise ValueError(f"Invalid status: {status}")
        submitted_at = datetime.now().isoformat(timespec="seconds") if status == "applied" else None
        ready_to_apply = 1 if status == "ready_to_apply" else 0
        self.execute(
            """
            UPDATE applications
            SET
                status = ?,
                ready_to_apply = ?,
                notes = COALESCE(?, notes),
                submitted_at = COALESCE(?, submitted_at),
                updated_at = CURRENT_TIMESTAMP
            WHERE job_id = ?
            """,
            (status, ready_to_apply, note, submitted_at, job_id),
        )

    def set_ready_to_apply(self, job_id: int, ready_to_apply: bool, status: str, note: str | None = None) -> None:
        if status not in VALID_STATUSES:
            raise ValueError(f"Invalid status: {status}")
        self.execute(
            """
            UPDATE applications
            SET ready_to_apply = ?, status = ?, notes = COALESCE(?, notes), updated_at = CURRENT_TIMESTAMP
            WHERE job_id = ?
            """,
            (1 if ready_to_apply else 0, status, note, job_id),
        )

    def insert_generated_document(
        self,
        job_id: int,
        doc_type: str,
        file_path: Path,
        content_preview: str,
    ) -> None:
        application = self.fetch_one("SELECT id FROM applications WHERE job_id = ?", (job_id,))
        application_id = int(application["id"]) if application else None
        with self._managed_connection() as connection:
            connection.execute(
                "DELETE FROM generated_documents WHERE job_id = ? AND doc_type = ?",
                (job_id, doc_type),
            )
            connection.execute(
                """
                INSERT INTO generated_documents (job_id, application_id, doc_type, file_path, content_preview)
                VALUES (?, ?, ?, ?, ?)
                """,
                (job_id, application_id, doc_type, str(file_path), content_preview[:1200]),
            )
            connection.commit()

    def list_generated_documents(self, job_id: int) -> list[dict[str, Any]]:
        rows = self.fetch_all(
            "SELECT * FROM generated_documents WHERE job_id = ? ORDER BY created_at DESC, id DESC",
            (job_id,),
        )
        return [dict(row) for row in rows]

    def has_processed_email_message(self, provider: str, message_id: str) -> bool:
        row = self.fetch_one(
            "SELECT 1 FROM processed_email_messages WHERE provider = ? AND message_id = ?",
            (provider, message_id),
        )
        return row is not None

    def record_processed_email_message(
        self,
        provider: str,
        message_id: str,
        source: str,
        imported_jobs: int,
        duplicate_jobs: int,
        filtered_jobs: int,
        status: str,
        details: str,
    ) -> None:
        with self._managed_connection() as connection:
            connection.execute(
                """
                INSERT INTO processed_email_messages (
                    provider, message_id, source, imported_jobs, duplicate_jobs,
                    filtered_jobs, status, details
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider, message_id) DO UPDATE SET
                    source = excluded.source,
                    imported_jobs = excluded.imported_jobs,
                    duplicate_jobs = excluded.duplicate_jobs,
                    filtered_jobs = excluded.filtered_jobs,
                    status = excluded.status,
                    details = excluded.details,
                    processed_at = CURRENT_TIMESTAMP
                """,
                (
                    provider,
                    message_id,
                    source,
                    imported_jobs,
                    duplicate_jobs,
                    filtered_jobs,
                    status,
                    details,
                ),
            )
            connection.commit()

    def clear_processed_email_messages(self) -> None:
        self.execute("DELETE FROM processed_email_messages")

    def update_job_follow_up(
        self,
        job_id: int,
        *,
        last_response_type: str | None = None,
        last_response_at: str | None = None,
        closed_reason: str | None = None,
        closed_at: str | None = None,
        follow_up_feedback_summary: str | None = None,
        follow_up_feedback_tags_json: str | None = None,
        follow_up_reply_last_action: str | None = None,
        follow_up_reply_last_at: str | None = None,
    ) -> None:
        self.execute(
            """
            UPDATE jobs
            SET
                last_response_type = COALESCE(?, last_response_type),
                last_response_at = COALESCE(?, last_response_at),
                closed_reason = COALESCE(?, closed_reason),
                closed_at = COALESCE(?, closed_at),
                follow_up_feedback_summary = COALESCE(?, follow_up_feedback_summary),
                follow_up_feedback_tags_json = COALESCE(?, follow_up_feedback_tags_json),
                follow_up_reply_last_action = COALESCE(?, follow_up_reply_last_action),
                follow_up_reply_last_at = COALESCE(?, follow_up_reply_last_at),
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (
                last_response_type,
                last_response_at,
                closed_reason,
                closed_at,
                follow_up_feedback_summary,
                follow_up_feedback_tags_json,
                follow_up_reply_last_action,
                follow_up_reply_last_at,
                job_id,
            ),
        )

    def close_application(self, job_id: int, reason: str, note: str | None = None) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        self.update_job_follow_up(
            job_id,
            closed_reason=reason,
            closed_at=now,
        )
        self.update_application_state(job_id, "closed", note or reason)

    def close_stale_applied_jobs(self, days: int = 30) -> int:
        threshold = datetime.now() - pd.Timedelta(days=max(1, int(days)))
        rows = self.fetch_all(
            """
            SELECT id
            FROM jobs
            WHERE application_status = 'applied'
              AND COALESCE(trim(applied_at), '') != ''
              AND datetime(applied_at) <= datetime(?)
              AND COALESCE(trim(last_response_at), '') = ''
              AND COALESCE(trim(closed_at), '') = ''
            """,
            (threshold.isoformat(timespec="seconds"),),
        )
        closed = 0
        for row in rows:
            self.close_application(int(row["id"]), "closed_no_response", note="Auto-closed after 30 days with no recruiter response.")
            closed += 1
        return closed

    def record_application_email_event(
        self,
        *,
        job_id: int,
        provider: str,
        message_id: str,
        thread_id: str | None,
        subject: str,
        sender: str,
        sender_email: str,
        received_at: str | None,
        classification: str,
        match_score: float,
        raw_snippet: str,
        feedback_summary: str = "",
        feedback_tags_json: str = "[]",
        reply_action: str = "",
        reply_subject: str = "",
        reply_body: str = "",
        reply_reference_message_id: str = "",
    ) -> None:
        with self._managed_connection() as connection:
            connection.execute(
                """
                INSERT INTO application_email_events (
                    job_id, provider, message_id, thread_id, subject, sender, sender_email,
                    received_at, classification, match_score, raw_snippet, feedback_summary,
                    feedback_tags_json, reply_action, reply_subject, reply_body, reply_reference_message_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider, message_id) DO UPDATE SET
                    job_id = excluded.job_id,
                    thread_id = excluded.thread_id,
                    subject = excluded.subject,
                    sender = excluded.sender,
                    sender_email = excluded.sender_email,
                    received_at = excluded.received_at,
                    classification = excluded.classification,
                    match_score = excluded.match_score,
                    raw_snippet = excluded.raw_snippet,
                    feedback_summary = excluded.feedback_summary,
                    feedback_tags_json = excluded.feedback_tags_json,
                    reply_action = excluded.reply_action,
                    reply_subject = excluded.reply_subject,
                    reply_body = excluded.reply_body,
                    reply_reference_message_id = excluded.reply_reference_message_id,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    job_id,
                    provider,
                    message_id,
                    thread_id,
                    subject,
                    sender,
                    sender_email,
                    received_at,
                    classification,
                    match_score,
                    raw_snippet,
                    feedback_summary,
                    feedback_tags_json,
                    reply_action,
                    reply_subject,
                    reply_body,
                    reply_reference_message_id,
                ),
            )
            connection.commit()

    def list_application_email_events(self, limit: int = 200, job_id: int | None = None) -> pd.DataFrame:
        sql = """
            SELECT
                application_email_events.*,
                jobs.title,
                jobs.company,
                jobs.application_status,
                jobs.closed_reason
            FROM application_email_events
            JOIN jobs ON jobs.id = application_email_events.job_id
        """
        params: list[Any] = []
        if job_id is not None:
            sql += " WHERE application_email_events.job_id = ?"
            params.append(int(job_id))
        sql += " ORDER BY COALESCE(application_email_events.received_at, application_email_events.created_at) DESC, application_email_events.id DESC LIMIT ?"
        params.append(int(limit))
        rows = self.fetch_all(sql, tuple(params))
        return pd.DataFrame([dict(row) for row in rows])

    def fetch_jobs_for_drafting(self) -> list[int]:
        rows = self.fetch_all(
            """
            SELECT jobs.id
            FROM jobs
            JOIN applications ON applications.job_id = jobs.id
            WHERE applications.status IN ('discovered', 'shortlisted')
              AND applications.ready_to_apply = 0
            ORDER BY jobs.fit_score DESC, jobs.id DESC
            """
        )
        return [int(row["id"]) for row in rows]

    def get_setting(self, key: str, default: str) -> str:
        row = self.fetch_one("SELECT value FROM app_settings WHERE key = ?", (key,))
        return str(row["value"]) if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self._managed_connection() as connection:
            connection.execute(
                """
                INSERT INTO app_settings (key, value)
                VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (key, value),
            )
            connection.commit()

    def log_run(self, run_type: str, action: str, status: str, details: str) -> None:
        self.execute(
            "INSERT INTO run_logs (run_type, action, status, details) VALUES (?, ?, ?, ?)",
            (run_type, action, status, details),
        )

    def log_runs(self, entries: list[tuple[str, str, str, str]]) -> None:
        self.execute_many(
            "INSERT INTO run_logs (run_type, action, status, details) VALUES (?, ?, ?, ?)",
            entries,
        )

    def fetch_run_logs(self, limit: int = 50) -> pd.DataFrame:
        rows = self.fetch_all(
            "SELECT * FROM run_logs ORDER BY created_at DESC, id DESC LIMIT ?",
            (limit,),
        )
        return pd.DataFrame([dict(row) for row in rows])

    def fetch_run_logs_by_type(self, run_type: str, limit: int = 5000) -> pd.DataFrame:
        rows = self.fetch_all(
            "SELECT * FROM run_logs WHERE run_type = ? ORDER BY created_at DESC, id DESC LIMIT ?",
            (run_type, limit),
        )
        return pd.DataFrame([dict(row) for row in rows])

    def fetch_latest_run_log(self, run_type: str, action: str | None = None) -> dict[str, Any] | None:
        sql = "SELECT * FROM run_logs WHERE run_type = ?"
        params: list[Any] = [run_type]
        if action is not None:
            sql += " AND action = ?"
            params.append(action)
        sql += " ORDER BY created_at DESC, id DESC LIMIT 1"
        row = self.fetch_one(sql, tuple(params))
        return dict(row) if row else None
