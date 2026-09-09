from __future__ import annotations

import base64
import inspect
import os
import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import Resource, build
from googleapiclient.errors import HttpError

from src.database import Database
from src.document_generator import DocumentGenerator
from src.email_job_parser import ParsedEmailJobs, parse_email_html_jobs
from src.job_importer import JobImporter

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
PROCESSED_LABEL_NAME = "AI Job Processed"
DEFAULT_GMAIL_LOOKBACK_DAYS = 30
KNOWN_ALERT_DOMAINS = [
    "linkedin.com",
    "linkedinmail.com",
    "seek.co.nz",
    "trademe.co.nz",
    "trademejobs.co.nz",
    "greenhouse.io",
    "lever.co",
    "workablemail.com",
    "smartrecruiters.com",
]


class GmailIngestor:
    def __init__(
        self,
        database: Database,
        importer: JobImporter,
        document_generator: DocumentGenerator | None,
        app_root: Path,
        job_enricher: Any | None = None,
    ) -> None:
        self.database = database
        self.importer = importer
        self.document_generator = document_generator
        self.job_enricher = job_enricher
        self.app_root = Path(app_root)
        load_dotenv(self.app_root / ".env")
        default_credentials = self.app_root / "data" / "gmail" / "credentials.json"
        default_token = self.app_root / "data" / "gmail" / "token.json"
        self.credentials_path = Path(os.getenv("GMAIL_CREDENTIALS_PATH") or default_credentials)
        self.token_path = Path(os.getenv("GMAIL_TOKEN_PATH") or default_token)
        self.trust_saved_search_location = self._env_flag("TRUST_SAVED_SEARCH_LOCATION", default=True)
        self.debug_email_dir = self.app_root / "data" / "debug_emails"
        self.debug_job_pages_dir = self.app_root / "data" / "debug_job_pages"
        self.credentials_path.parent.mkdir(parents=True, exist_ok=True)
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        self.debug_email_dir.mkdir(parents=True, exist_ok=True)
        self.debug_job_pages_dir.mkdir(parents=True, exist_ok=True)

    def get_connection_status(self) -> dict[str, Any]:
        configured = self.credentials_path.exists()
        token_exists = self.token_path.exists()
        authenticated = False
        error: str | None = None
        try:
            creds = self._load_credentials(interactive=False)
            authenticated = creds is not None and creds.valid
        except RefreshError:
            error = "OAuth token expired or revoked. Reconnect Gmail."
        except Exception as exc:  # noqa: BLE001
            error = str(exc)

        latest_sync = self.database.fetch_latest_run_log("gmail_ingestion", "sync")
        imported_jobs = 0
        if latest_sync and latest_sync.get("details"):
            match = re.search(r"'created': (\d+)", str(latest_sync["details"]))
            if match:
                imported_jobs = int(match.group(1))

        return {
            "configured": configured,
            "token_exists": token_exists,
            "authenticated": authenticated,
            "credentials_path": str(self.credentials_path),
            "token_path": str(self.token_path),
            "last_sync_timestamp": latest_sync["created_at"] if latest_sync else None,
            "last_sync_imported_jobs": imported_jobs,
            "error": error,
        }

    def sync_messages(
        self,
        unread_only: bool = True,
        jobs_label_only: bool = False,
        max_results: int = 10,
        lookback_days: int = DEFAULT_GMAIL_LOOKBACK_DAYS,
        mark_as_read: bool = True,
        apply_processed_label: bool = True,
        interactive_auth: bool = True,
        ignore_processed_tracking: bool = False,
        progress_callback: Any | None = None,
    ) -> dict[str, Any]:
        try:
            service = self._build_service(interactive=interactive_auth)
        except RefreshError as exc:
            self.database.log_run("gmail_ingestion", "auth", "error", f"Expired token: {exc}")
            return self._summary_with_error("Gmail OAuth token expired or was revoked.")
        except FileNotFoundError as exc:
            self.database.log_run("gmail_ingestion", "auth", "error", str(exc))
            return self._summary_with_error(str(exc))
        except Exception as exc:  # noqa: BLE001
            self.database.log_run("gmail_ingestion", "auth", "error", str(exc))
            return self._summary_with_error(str(exc))

        processed_label_id = None
        jobs_label_id = None
        try:
            if apply_processed_label:
                processed_label_id = self._get_or_create_label(service, PROCESSED_LABEL_NAME)
            if jobs_label_only:
                jobs_label_id = self._get_label_id(service, "Jobs")
            query = self._build_search_query(
                jobs_label_only=jobs_label_only,
                unread_only=unread_only,
                lookback_days=lookback_days,
            )
            self._emit_progress(
                progress_callback,
                {
                    "status": "starting_sync",
                    "query": query,
                    "max_results": max_results,
                    "lookback_days": lookback_days,
                    "jobs_label_only": jobs_label_only,
                    "unread_only": unread_only,
                    "ignore_processed_tracking": ignore_processed_tracking,
                    "trust_saved_search_location": self.trust_saved_search_location,
                },
            )
            response = service.users().messages().list(
                userId="me",
                q=query,
                labelIds=[jobs_label_id] if jobs_label_id else None,
                maxResults=max_results,
            ).execute()
        except HttpError as exc:
            self.database.log_run("gmail_ingestion", "list_messages", "error", str(exc))
            return self._summary_with_error(f"Gmail API error while listing messages: {exc}")

        summary = self._empty_summary()
        messages = response.get("messages", [])
        total_messages = len(messages)
        summary["messages_considered"] = total_messages
        self._emit_progress(
            progress_callback,
            {
                "status": "messages_found",
                "total": total_messages,
                "query": query,
                "unread_only": unread_only,
                "lookback_days": lookback_days,
            },
        )
        if total_messages == 0:
            self._emit_progress(
                progress_callback,
                {
                    "status": "no_messages_found",
                    "query": query,
                    "unread_only": unread_only,
                    "lookback_days": lookback_days,
                },
            )
        for index, item in enumerate(messages, start=1):
            message_id = item["id"]
            if not ignore_processed_tracking and self.database.has_processed_email_message("gmail", message_id):
                summary["duplicate_emails"] += 1
                self._emit_progress(
                    progress_callback,
                    {
                        "index": index,
                        "total": total_messages,
                        "message_id": message_id,
                        "status": "duplicate_email",
                        "subject": "",
                        "imported": 0,
                        "duplicates": 0,
                        "filtered_out": 0,
                        "auto_drafted": 0,
                    },
                )
                continue
            if ignore_processed_tracking:
                self._emit_progress(
                    progress_callback,
                    {
                        "index": index,
                        "total": total_messages,
                        "message_id": message_id,
                        "status": "reprocessing_email",
                    },
                )

            try:
                message = service.users().messages().get(userId="me", id=message_id, format="full").execute()
                parsed = self._parse_gmail_message(message)
                self._emit_progress(
                    progress_callback,
                    {
                        "index": index,
                        "total": total_messages,
                        "message_id": message_id,
                        "status": "processing_email",
                        "subject": parsed["subject"],
                        "source": parsed["source"],
                        "sender": parsed["from"],
                    },
                )
                if self._is_older_than_days(parsed["received_at"], lookback_days):
                    summary["filtered_out"] += 1
                    summary["non_auckland_filtered"] += 0
                    self._emit_progress(
                        progress_callback,
                        {
                            "index": index,
                            "total": total_messages,
                            "message_id": message_id,
                            "status": "skipped_old",
                            "subject": parsed["subject"],
                            "imported": 0,
                            "duplicates": 0,
                            "filtered_out": 0,
                            "auto_drafted": 0,
                        },
                    )
                    continue
                parsed_jobs = parsed.get("parsed_jobs")
                extracted_jobs_count = len(parsed_jobs) if isinstance(parsed_jobs, list) else None
                card_counts = parsed.get("card_counts") or {}
                if extracted_jobs_count is not None:
                    summary["jobs_found_in_email"] += extracted_jobs_count
                    self._emit_progress(
                        progress_callback,
                        {
                            "index": index,
                            "total": total_messages,
                            "message_id": message_id,
                            "status": "jobs_extracted",
                            "subject": parsed["subject"],
                            "source": parsed["source"],
                            "extracted_jobs_count": extracted_jobs_count,
                            "card_counts": card_counts,
                            "seek_cards_parsed": extracted_jobs_count if parsed["source"] == "seek" else 0,
                        },
                    )
                    missing_url_count = sum(1 for job in parsed_jobs if not str(job.get("url", "")).strip())
                    if missing_url_count:
                        self._emit_progress(
                            progress_callback,
                            {
                                "index": index,
                                "total": total_messages,
                                "message_id": message_id,
                                "status": "missing_job_url",
                                "subject": parsed["subject"],
                                "source": parsed["source"],
                                "missing_url_count": missing_url_count,
                            },
                        )
                if parsed["source"] in {"linkedin", "seek", "trademe"} and parsed.get("parsed_jobs") == []:
                    reason = "no_trademe_job_cards_found" if parsed["source"] == "trademe" else "no_job_cards_found"
                    status = "error" if parsed["source"] == "trademe" else "skipped"
                    action = reason
                    details = f"subject={parsed['subject']}; sender={parsed['from']}; reason={reason}"
                    if parsed["source"] != "trademe" and (apply_processed_label or mark_as_read):
                        self._mark_processed(
                            service,
                            message_id=message_id,
                            processed_label_id=processed_label_id,
                            mark_as_read=mark_as_read,
                        )
                    if parsed["source"] != "trademe":
                        self.database.record_processed_email_message(
                            provider="gmail",
                            message_id=message_id,
                            source=parsed["source"],
                            imported_jobs=0,
                            duplicate_jobs=0,
                            filtered_jobs=0,
                            status=status,
                            details=details,
                        )
                    self.database.log_run("gmail_ingestion", action, status, details)
                    self._emit_progress(
                        progress_callback,
                        {
                            "index": index,
                            "total": total_messages,
                            "message_id": message_id,
                            "status": reason,
                            "subject": parsed["subject"],
                            "error": reason,
                        },
                    )
                    continue
                result = self.importer.import_email_alert_text(
                    parsed["text"],
                    run_type="gmail_ingestion",
                    source_hint=parsed["source"],
                    date_found=parsed["received_date"],
                    email_received_at=parsed["received_at"],
                    log_filtered_out=False,
                    parsed_jobs=parsed.get("parsed_jobs"),
                    auckland_only=not self.trust_saved_search_location,
                    skip_clearly_irrelevant_locations=self.trust_saved_search_location,
                )
                enrichment_summary = {"success": 0, "failed": 0, "skipped": 0}
                if self.job_enricher is not None and result.get("created_job_ids"):
                    enrichment_summary = self.job_enricher.enrich_jobs(result["created_job_ids"])
                auto_drafted = 0
                if self.document_generator is not None:
                    for job_id in result.get("auto_draft_job_ids", []):
                        self.document_generator.generate_for_job(job_id)
                        auto_drafted += 1
                        self._emit_progress(
                            progress_callback,
                            {
                                "index": index,
                                "total": total_messages,
                                "message_id": message_id,
                                "status": "documents_generated",
                                "subject": parsed["subject"],
                                "job_id": job_id,
                            },
                        )

                if apply_processed_label or mark_as_read:
                    self._mark_processed(
                        service,
                        message_id=message_id,
                        processed_label_id=processed_label_id,
                        mark_as_read=mark_as_read,
                    )

                self.database.record_processed_email_message(
                    provider="gmail",
                    message_id=message_id,
                    source=parsed["source"],
                    imported_jobs=result["created_count"],
                    duplicate_jobs=result["duplicate_count"],
                    filtered_jobs=result.get("filtered_out_count", 0),
                    status="success",
                    details=f"subject={parsed['subject']}",
                )
                summary["emails_processed"] += 1
                summary["created"] += result["created_count"]
                summary["created_job_ids"].extend(result.get("created_job_ids", []))
                summary["duplicates"] += result["duplicate_count"]
                summary["filtered_out"] += result.get("filtered_out_count", 0)
                summary["non_auckland_filtered"] += result.get("filtered_out_count", 0)
                summary["failed"] += len(result["errors"])
                summary["auto_drafted"] += auto_drafted
                summary["jobs_enriched"] += enrichment_summary.get("success", 0)
                summary["enrichment_failed"] += enrichment_summary.get("failed", 0)
                summary["imported_job_titles"].extend(result.get("imported_job_titles", []))
                summary["duplicate_job_titles"].extend(result.get("duplicate_job_titles", []))
                self._emit_progress(
                    progress_callback,
                    {
                        "index": index,
                        "total": total_messages,
                        "message_id": message_id,
                        "status": "processed",
                        "subject": parsed["subject"],
                        "imported": result["created_count"],
                        "duplicates": result["duplicate_count"],
                        "jobs_imported": result["created_count"],
                        "jobs_skipped_duplicate": result["duplicate_count"],
                        "filtered_out": result.get("filtered_out_count", 0),
                        "auto_drafted": auto_drafted,
                        "jobs_enriched": enrichment_summary.get("success", 0),
                        "enrichment_failed": enrichment_summary.get("failed", 0),
                        "imported_job_titles": result.get("imported_job_titles", []),
                    },
                )
            except HttpError as exc:
                summary["failed"] += 1
                summary["errors"].append({"message_id": message_id, "error": f"Gmail API error: {exc}"})
                self.database.record_processed_email_message(
                    provider="gmail",
                    message_id=message_id,
                    source="gmail",
                    imported_jobs=0,
                    duplicate_jobs=0,
                    filtered_jobs=0,
                    status="error",
                    details=str(exc),
                )
                self._emit_progress(
                    progress_callback,
                    {
                        "index": index,
                        "total": total_messages,
                        "message_id": message_id,
                        "status": "failed",
                        "subject": "",
                        "error": f"Gmail API error: {exc}",
                    },
                )
            except Exception as exc:  # noqa: BLE001
                summary["failed"] += 1
                summary["errors"].append({"message_id": message_id, "error": str(exc)})
                self.database.record_processed_email_message(
                    provider="gmail",
                    message_id=message_id,
                    source="gmail",
                    imported_jobs=0,
                    duplicate_jobs=0,
                    filtered_jobs=0,
                    status="error",
                    details=str(exc),
                )
                self._emit_progress(
                    progress_callback,
                    {
                        "index": index,
                        "total": total_messages,
                        "message_id": message_id,
                        "status": "failed",
                        "subject": "",
                        "error": str(exc),
                    },
                )

        self.database.log_run("gmail_ingestion", "sync", "success", str(summary))
        self._emit_progress(
            progress_callback,
            {
                "status": "sync_complete",
                "summary": summary,
            },
        )
        return summary

    def _build_service(self, interactive: bool) -> Resource:
        creds = self._load_credentials(interactive=interactive)
        if creds is None or not creds.valid:
            raise RuntimeError("Unable to create Gmail API credentials.")
        return build("gmail", "v1", credentials=creds)

    def _load_credentials(self, interactive: bool) -> Credentials | None:
        creds = None
        if self.token_path.exists():
            creds = Credentials.from_authorized_user_file(str(self.token_path), GMAIL_SCOPES)
        if creds and creds.valid:
            return creds
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                self.token_path.write_text(creds.to_json(), encoding="utf-8")
                return creds
            except RefreshError:
                if not interactive:
                    raise
                self.token_path.unlink(missing_ok=True)
                creds = None
        if not interactive:
            return creds
        if not self.credentials_path.exists():
            raise FileNotFoundError(
                f"Gmail credentials file not found at {self.credentials_path}. "
                "Create an OAuth desktop client in Google Cloud and set GMAIL_CREDENTIALS_PATH."
            )
        flow = InstalledAppFlow.from_client_secrets_file(str(self.credentials_path), GMAIL_SCOPES)
        creds = flow.run_local_server(port=0)
        self.token_path.write_text(creds.to_json(), encoding="utf-8")
        return creds

    def _build_search_query(
        self,
        jobs_label_only: bool,
        *,
        unread_only: bool = True,
        lookback_days: int = DEFAULT_GMAIL_LOOKBACK_DAYS,
    ) -> str:
        domain_query = " OR ".join(f"from:{domain}" for domain in KNOWN_ALERT_DOMAINS)
        subject_query = 'subject:"job alert" OR subject:"jobs for you" OR subject:"new jobs" OR subject:"recruiter"'
        terms = [
            f"({subject_query})",
            f"({domain_query})",
            f'-label:"{PROCESSED_LABEL_NAME}"',
            f"newer_than:{max(1, int(lookback_days))}d",
        ]
        if unread_only:
            terms.append("is:unread")
        if jobs_label_only:
            terms.append("label:Jobs")
        return " ".join(terms)

    def _parse_gmail_message(self, message: dict[str, Any]) -> dict[str, Any]:
        payload = message.get("payload", {})
        headers = {header["name"].lower(): header["value"] for header in payload.get("headers", [])}
        subject = headers.get("subject", "")
        from_header = headers.get("from", "")
        text, html = self._extract_payload_parts(payload)
        text = text.strip()
        if not text:
            text = message.get("snippet", "")
        source = self.importer._detect_source_from_text_or_url(subject + " " + from_header, f"{text} {html}")  # noqa: SLF001
        received_at = self._received_timestamp_from_message(message, headers)
        received_date = received_at[:10] if received_at else None
        parsed_jobs = None
        raw_html_saved = None
        card_counts = None
        if html:
            parsed_result = self._extract_jobs_from_html(html, source, subject=subject, message_id=message.get("id"))
            if isinstance(parsed_result, ParsedEmailJobs):
                parsed_jobs = parsed_result.jobs
                raw_html_saved = parsed_result.raw_html_saved
                card_counts = parsed_result.card_counts
            else:
                parsed_jobs = parsed_result
        if html and source in {"linkedin", "seek", "trademe"} and parsed_jobs is None:
            parsed_jobs = []
        return {
            "subject": subject,
            "from": from_header,
            "text": text,
            "source": source,
            "received_date": received_date,
            "received_at": received_at,
            "parsed_jobs": parsed_jobs,
            "raw_html_saved": raw_html_saved,
            "card_counts": card_counts,
        }

    def _extract_payload_parts(self, payload: dict[str, Any]) -> tuple[str, str]:
        mime_type = payload.get("mimeType", "")
        body_data = payload.get("body", {}).get("data")
        plain_parts: list[str] = []
        html_parts: list[str] = []
        if mime_type == "text/plain" and body_data:
            plain_parts.append(self._decode_gmail_body(body_data))
        elif mime_type == "text/html" and body_data:
            html_parts.append(self._decode_gmail_body(body_data))
        if mime_type == "text/html" and body_data:
            pass
        for part in payload.get("parts", []) or []:
            plain_text, html_text = self._extract_payload_parts(part)
            if plain_text:
                plain_parts.append(plain_text)
            if html_text:
                html_parts.append(html_text)
        if not plain_parts and body_data and mime_type not in {"text/plain", "text/html"}:
            plain_parts.append(self._decode_gmail_body(body_data))
        plain_text = "\n".join(part for part in plain_parts if part).strip()
        html_text = "\n".join(part for part in html_parts if part).strip()
        if not plain_text and html_text:
            plain_text = self._html_to_text(html_text)
        return plain_text, html_text

    def _decode_gmail_body(self, body_data: str) -> str:
        padded = body_data + "=" * (-len(body_data) % 4)
        decoded = base64.urlsafe_b64decode(padded.encode("utf-8"))
        return decoded.decode("utf-8", errors="ignore")

    def _html_to_text(self, html: str) -> str:
        without_scripts = re.sub(r"<(script|style).*?>.*?</\1>", " ", html, flags=re.IGNORECASE | re.DOTALL)
        with_breaks = re.sub(r"</?(div|p|br|tr|td|li|table|h1|h2|h3)[^>]*>", "\n", without_scripts, flags=re.IGNORECASE)
        text = re.sub(r"<[^>]+>", " ", with_breaks)
        text = re.sub(r"&nbsp;|&#160;", " ", text)
        text = re.sub(r"&amp;", "&", text)
        text = re.sub(r"\n\s*\n+", "\n", text)
        text = re.sub(r"[ \t]+", " ", text)
        return text.strip()

    def _received_timestamp_from_message(self, message: dict[str, Any], headers: dict[str, str]) -> str | None:
        internal_date = message.get("internalDate")
        if internal_date:
            try:
                return datetime.fromtimestamp(int(internal_date) / 1000, tz=timezone.utc).isoformat(timespec="seconds")
            except Exception:  # noqa: BLE001
                pass
        date_header = headers.get("date")
        if date_header:
            try:
                return parsedate_to_datetime(date_header).astimezone(timezone.utc).isoformat(timespec="seconds")
            except Exception:  # noqa: BLE001
                return None
        return None

    def _is_older_than_days(self, received_at: str | None, days: int) -> bool:
        if not received_at:
            return False
        try:
            parsed = datetime.fromisoformat(received_at.replace("Z", "+00:00"))
        except ValueError:
            return False
        # Gmail search already applies the primary lookback window, so keep this
        # secondary guard intentionally loose to avoid false negatives from old
        # test fixtures, API clock skew, or delayed message timestamps.
        effective_days = max(int(days), 90)
        return parsed < datetime.now(timezone.utc) - timedelta(days=effective_days)

    def _extract_linkedin_jobs_from_html(self, html: str) -> list[dict[str, Any]] | None:
        try:
            from bs4 import BeautifulSoup
        except ImportError:
            return self._extract_linkedin_jobs_from_html_fallback(html)

        soup = BeautifulSoup(html, "html.parser")
        jobs = self._extract_linkedin_jobs_from_anchor_blocks(
            [
                (str(anchor.get("href") or "").strip(), anchor.get_text(" ", strip=True), anchor)
                for anchor in soup.find_all("a", href=True)
            ]
        )
        return jobs or None

    def _extract_jobs_from_html(
        self,
        html: str,
        source: str,
        *,
        subject: str,
        message_id: str | None,
    ) -> ParsedEmailJobs | list[dict[str, Any]] | None:
        parsed = parse_email_html_jobs(
            html,
            subject=subject,
            message_id=message_id,
            debug_dir=self.debug_email_dir,
        )
        if parsed.jobs:
            if source:
                for job in parsed.jobs:
                    job["source"] = source
            return parsed
        if source == "linkedin":
            return self._extract_linkedin_jobs_from_html(html)
        if source == "seek":
            return self._extract_seek_jobs_from_html(html)
        if source == "trademe":
            return self._extract_trademe_jobs_from_html(html)
        return parsed

    def _extract_seek_jobs_from_html(self, html: str) -> list[dict[str, Any]] | None:
        try:
            from bs4 import BeautifulSoup
        except ImportError:
            return self._extract_seek_jobs_from_html_fallback(html)

        soup = BeautifulSoup(html, "html.parser")
        anchors = []
        for anchor in soup.find_all("a", href=True):
            href = str(anchor.get("href") or "").strip()
            title = anchor.get_text(" ", strip=True)
            anchors.append((href, title, anchor))
        jobs = self._extract_seek_jobs_from_anchor_blocks(anchors)
        return jobs or None

    def _extract_trademe_jobs_from_html(self, html: str) -> list[dict[str, Any]] | None:
        try:
            from bs4 import BeautifulSoup
        except ImportError:
            return self._extract_trademe_jobs_from_html_fallback(html)

        soup = BeautifulSoup(html, "html.parser")
        anchors = []
        for anchor in soup.find_all("a", href=True):
            href = str(anchor.get("href") or "").strip()
            title = anchor.get_text(" ", strip=True)
            anchors.append((href, title, anchor))
        jobs = self._extract_trademe_jobs_from_anchor_blocks(anchors)
        return jobs or None

    def _extract_seek_jobs_from_html_fallback(self, html: str) -> list[dict[str, Any]] | None:
        jobs: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str]] = set()
        block_pattern = re.compile(r"<td[^>]*>(.*?)</td>|<tr[^>]*>(.*?)</tr>|<div[^>]*>(.*?)</div>", flags=re.IGNORECASE | re.DOTALL)
        anchor_pattern = re.compile(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', flags=re.IGNORECASE | re.DOTALL)
        for match in block_pattern.findall(html):
            block_html = next((part for part in match if part), "")
            if "seek.co.nz" not in block_html.lower() and "/job/" not in block_html.lower():
                continue
            anchor_match = anchor_pattern.search(block_html)
            if not anchor_match:
                continue
            href = anchor_match.group(1)
            title = re.sub(r"<[^>]+>", " ", anchor_match.group(2))
            title = re.sub(r"\s+", " ", title).strip()
            if not self._looks_like_seek_title(title):
                continue
            block_text = self._html_to_text(block_html)
            block_lines = [line.strip(" -*\t\r") for line in re.split(r"\s{2,}|\n", block_text) if line.strip()]
            job = self._build_seek_job_from_lines(title, href, block_lines)
            if not job:
                continue
            key = (job["title"].lower(), job["company"].lower(), job["url"].lower())
            if key in seen:
                continue
            seen.add(key)
            jobs.append(job)
        return jobs or None

    def _extract_trademe_jobs_from_html_fallback(self, html: str) -> list[dict[str, Any]] | None:
        jobs: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str]] = set()
        block_pattern = re.compile(r"<td[^>]*>(.*?)</td>|<tr[^>]*>(.*?)</tr>|<div[^>]*>(.*?)</div>", flags=re.IGNORECASE | re.DOTALL)
        anchor_pattern = re.compile(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', flags=re.IGNORECASE | re.DOTALL)
        for match in block_pattern.findall(html):
            block_html = next((part for part in match if part), "")
            if "trademe" not in block_html.lower() and "/a/jobs/" not in block_html.lower():
                continue
            anchor_match = anchor_pattern.search(block_html)
            if not anchor_match:
                continue
            href = anchor_match.group(1)
            title = re.sub(r"<[^>]+>", " ", anchor_match.group(2))
            title = re.sub(r"\s+", " ", title).strip()
            if not self._looks_like_trademe_title(title):
                continue
            block_text = self._html_to_text(block_html)
            block_lines = [line.strip(" -*\t\r") for line in re.split(r"\s{2,}|\n", block_text) if line.strip()]
            job = self._build_trademe_job_from_lines(title, href, block_lines)
            if not job:
                continue
            key = (job["title"].lower(), job["company"].lower(), job["url"].lower())
            if key in seen:
                continue
            seen.add(key)
            jobs.append(job)
        return jobs or None

    def _extract_seek_jobs_from_anchor_blocks(self, anchors: list[tuple[str, str, Any]]) -> list[dict[str, Any]]:
        jobs: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str]] = set()
        for href, raw_title, anchor in anchors:
            if "seek.co.nz" not in href.lower() and "/job/" not in href.lower():
                continue
            title = re.sub(r"\s+", " ", raw_title).strip()
            if not self._looks_like_seek_title(title):
                continue
            container = self._pick_card_container(anchor, domain_hint="seek.co.nz")
            block_lines = [line.strip(" -*\t\r") for line in container.get_text("\n", strip=True).splitlines() if line.strip()]
            job = self._build_seek_job_from_lines(title, href, block_lines)
            if not job:
                continue
            key = (job["title"].lower(), job["company"].lower(), job["url"].lower())
            if key in seen:
                continue
            seen.add(key)
            jobs.append(job)
        return jobs

    def _extract_trademe_jobs_from_anchor_blocks(self, anchors: list[tuple[str, str, Any]]) -> list[dict[str, Any]]:
        jobs: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str]] = set()
        for href, raw_title, anchor in anchors:
            lowered_href = href.lower()
            if "trademe" not in lowered_href and "/a/jobs/" not in lowered_href:
                continue
            title = re.sub(r"\s+", " ", raw_title).strip()
            if not self._looks_like_trademe_title(title):
                continue
            container = self._pick_card_container(anchor, domain_hint="trademe")
            block_lines = [line.strip(" -*\t\r") for line in container.get_text("\n", strip=True).splitlines() if line.strip()]
            job = self._build_trademe_job_from_lines(title, href, block_lines)
            if not job:
                continue
            key = (job["title"].lower(), job["company"].lower(), job["url"].lower())
            if key in seen:
                continue
            seen.add(key)
            jobs.append(job)
        return jobs

    def _extract_linkedin_jobs_from_html_fallback(self, html: str) -> list[dict[str, Any]] | None:
        jobs: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str]] = set()
        block_pattern = re.compile(r"<td[^>]*>(.*?)</td>|<tr[^>]*>(.*?)</tr>", flags=re.IGNORECASE | re.DOTALL)
        anchor_pattern = re.compile(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', flags=re.IGNORECASE | re.DOTALL)
        for match in block_pattern.findall(html):
            block_html = next((part for part in match if part), "")
            if "/jobs/" not in block_html.lower():
                continue
            anchor_match = anchor_pattern.search(block_html)
            if not anchor_match:
                continue
            href = anchor_match.group(1)
            title = re.sub(r"<[^>]+>", " ", anchor_match.group(2))
            title = re.sub(r"\s+", " ", title).strip()
            if not self.importer._looks_like_linkedin_title(title):  # noqa: SLF001
                continue
            block_text = self._html_to_text(block_html)
            block_lines = [line.strip(" -*\t\r") for line in re.split(r"\s{2,}|\n", block_text) if line.strip()]
            company = ""
            location = ""
            description_bits: list[str] = []
            for line in block_lines:
                if line == title or self.importer._is_linkedin_noise_line(line):  # noqa: SLF001
                    continue
                if not company and not self.importer._looks_like_location_line(line):  # noqa: SLF001
                    company = line
                    continue
                if not location and self.importer._looks_like_location_line(line):  # noqa: SLF001
                    location = line
                    continue
                if line not in {title, company, location}:
                    description_bits.append(line)
            if not company:
                continue
            key = (title.lower(), company.lower(), href.lower())
            if key in seen:
                continue
            seen.add(key)
            jobs.append(self._build_linkedin_job_payload(title, company, location, href, description_bits))
        return jobs or None

    def _extract_linkedin_jobs_from_anchor_blocks(self, anchors: list[tuple[str, str, Any]]) -> list[dict[str, Any]]:
        jobs: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str]] = set()
        for href, raw_title, anchor in anchors:
            if "linkedin.com" not in href.lower() or "/jobs/" not in href.lower():
                continue
            title = re.sub(r"\s+", " ", raw_title).strip()
            if not self.importer._looks_like_linkedin_title(title):  # noqa: SLF001
                continue
            container = self._pick_card_container(anchor, domain_hint="linkedin.com")
            block_lines = [line.strip(" -*\t\r") for line in container.get_text("\n", strip=True).splitlines() if line.strip()]
            company = ""
            location = ""
            description_bits: list[str] = []
            for line in block_lines:
                if line == title or self.importer._is_linkedin_noise_line(line):  # noqa: SLF001
                    continue
                if not company and not self.importer._looks_like_location_line(line):  # noqa: SLF001
                    company = line
                    continue
                if not location and self.importer._looks_like_location_line(line):  # noqa: SLF001
                    location = line
                    continue
                if line not in {title, company, location}:
                    description_bits.append(line)
            if not company:
                continue
            key = (title.lower(), company.lower(), href.lower())
            if key in seen:
                continue
            seen.add(key)
            jobs.append(self._build_linkedin_job_payload(title, company, location, href, description_bits))
        return jobs

    def _build_linkedin_job_payload(self, title: str, company: str, location: str, href: str, description_bits: list[str]) -> dict[str, Any]:
        return {
            "title": title[:200],
            "company": company[:200],
            "location": location[:200],
            "url": href,
            "source": "linkedin",
            "description": " ".join(description_bits[:4]) or f"LinkedIn alert for {title} at {company} in {location or 'Auckland'}.",
            "salary": "",
            "source_job_id": self.importer._extract_source_job_id(href),  # noqa: SLF001
            "email_received_at": None,
        }

    def _build_seek_job_from_lines(self, title: str, href: str, block_lines: list[str]) -> dict[str, Any] | None:
        company = ""
        location = ""
        description_bits: list[str] = []
        for line in block_lines:
            if line == title or self._is_seek_noise_line(line):
                continue
            if not company and not self.importer._looks_like_location_line(line):  # noqa: SLF001
                company = line
                continue
            if not location and self.importer._looks_like_location_line(line):  # noqa: SLF001
                location = line
                continue
            if line not in {title, company, location}:
                description_bits.append(line)
        if not company:
            return None
        return {
            "title": title[:200],
            "company": company[:200],
            "location": location[:200],
            "url": href,
            "source": "seek",
            "description": " ".join(description_bits[:4]) or f"SEEK alert card for {title} at {company} in {location or 'Auckland'}. Full details should be reviewed from the job URL.",
            "salary": self.importer._extract_salary(" ".join(block_lines)),  # noqa: SLF001
            "source_job_id": self.importer._extract_source_job_id(href),  # noqa: SLF001
            "email_received_at": None,
        }

    def _build_trademe_job_from_lines(self, title: str, href: str, block_lines: list[str]) -> dict[str, Any] | None:
        company = ""
        location = ""
        description_bits: list[str] = []
        for line in block_lines:
            if line == title or self._is_trademe_noise_line(line):
                continue
            if not company and not self.importer._looks_like_location_line(line):  # noqa: SLF001
                company = line
                continue
            if not location and self.importer._looks_like_location_line(line):  # noqa: SLF001
                location = line
                continue
            if line not in {title, company, location}:
                description_bits.append(line)
        if not company:
            return None
        return {
            "title": title[:200],
            "company": company[:200],
            "location": location[:200],
            "url": href,
            "source": "trademe",
            "description": " ".join(description_bits[:4]) or f"Trade Me alert card for {title} at {company} in {location or 'Auckland'}. Full description should be reviewed from the job URL.",
            "salary": self.importer._extract_salary(" ".join(block_lines)),  # noqa: SLF001
            "source_job_id": self.importer._extract_source_job_id(href),  # noqa: SLF001
            "email_received_at": None,
        }

    def _looks_like_seek_title(self, line: str) -> bool:
        lowered = line.lower().strip()
        if not lowered or len(lowered) < 5 or len(lowered) > 140:
            return False
        if self._is_seek_noise_line(lowered):
            return False
        if self.importer._looks_like_location_line(lowered):  # noqa: SLF001
            return False
        if re.search(r"https?://", lowered):
            return False
        return len(re.findall(r"[A-Za-z]{2,}", line)) >= 2

    def _looks_like_trademe_title(self, line: str) -> bool:
        lowered = line.lower().strip()
        if not lowered or len(lowered) < 5 or len(lowered) > 160:
            return False
        if self._is_trademe_noise_line(lowered):
            return False
        if self.importer._looks_like_location_line(lowered):  # noqa: SLF001
            return False
        if re.search(r"https?://", lowered):
            return False
        return len(re.findall(r"[A-Za-z]{2,}", line)) >= 2

    def _is_seek_noise_line(self, line: str) -> bool:
        lowered = line.lower().strip()
        phrases = [
            "new jobs for",
            "view all jobs",
            "saved searches",
            "unsubscribe",
            "seek",
            "job mail",
            "view job",
            "apply now",
            "strong applicant",
            "early applicant",
            "hirer responsiveness",
            "application strength",
        ]
        return any(phrase in lowered for phrase in phrases)

    def _is_trademe_noise_line(self, line: str) -> bool:
        lowered = line.lower().strip()
        phrases = [
            "trade me jobs",
            "trade me",
            "view listing",
            "view job",
            "apply now",
            "unsubscribe",
            "job alert",
            "site.trademe.co.nz",
        ]
        return any(phrase in lowered for phrase in phrases)

    def _pick_card_container(self, anchor: Any, domain_hint: str) -> Any:
        container = anchor
        best = anchor
        for _ in range(6):
            parent = container.parent
            if parent is None:
                break
            matching_anchors = 0
            for child_anchor in parent.find_all("a", href=True):
                href = str(child_anchor.get("href") or "").lower()
                if domain_hint in href or "/job/" in href or "/jobs/" in href:
                    matching_anchors += 1
            if matching_anchors > 1:
                break
            block_text = parent.get_text("\n", strip=True)
            if len(block_text) < 20:
                break
            best = parent
            container = parent
        return best

    def _get_label_id(self, service: Resource, label_name: str) -> str | None:
        labels = service.users().labels().list(userId="me").execute().get("labels", [])
        for label in labels:
            if label.get("name") == label_name:
                return label["id"]
        return None

    def _get_or_create_label(self, service: Resource, label_name: str) -> str:
        existing = self._get_label_id(service, label_name)
        if existing:
            return existing
        created = service.users().labels().create(
            userId="me",
            body={
                "name": label_name,
                "labelListVisibility": "labelShow",
                "messageListVisibility": "show",
            },
        ).execute()
        return created["id"]

    def _mark_processed(self, service: Resource, message_id: str, processed_label_id: str | None, mark_as_read: bool) -> None:
        body: dict[str, Any] = {"addLabelIds": [], "removeLabelIds": []}
        if processed_label_id:
            body["addLabelIds"].append(processed_label_id)
        if mark_as_read:
            body["removeLabelIds"].append("UNREAD")
        service.users().messages().modify(userId="me", id=message_id, body=body).execute()

    def _summary_with_error(self, error: str) -> dict[str, Any]:
        summary = self._empty_summary()
        summary["errors"].append({"message_id": "", "error": error})
        summary["failed"] = 1
        return summary

    def _env_flag(self, key: str, *, default: bool) -> bool:
        raw_value = os.getenv(key)
        if raw_value is None:
            return default
        return raw_value.strip().lower() not in {"0", "false", "no", "off"}

    def _empty_summary(self) -> dict[str, Any]:
        return {
            "messages_considered": 0,
            "emails_processed": 0,
            "duplicate_emails": 0,
            "jobs_found_in_email": 0,
            "created": 0,
            "created_job_ids": [],
            "duplicates": 0,
            "filtered_out": 0,
            "non_auckland_filtered": 0,
            "failed": 0,
            "auto_drafted": 0,
            "jobs_enriched": 0,
            "enrichment_failed": 0,
            "imported_job_titles": [],
            "duplicate_job_titles": [],
            "errors": [],
        }

    def _emit_progress(self, progress_callback: Any | None, payload: dict[str, Any]) -> None:
        if progress_callback is None:
            return
        try:
            result = progress_callback(payload)
            if inspect.isawaitable(result):
                return
        except Exception as exc:  # noqa: BLE001
            self.database.log_run("gmail_ingestion", "progress_callback", "error", str(exc))
