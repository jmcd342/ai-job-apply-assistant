from __future__ import annotations

import base64
import json
import os
import re
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path
from typing import Any

import pandas as pd
from dotenv import load_dotenv
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import Resource, build

from src.database import Database

FOLLOWUP_PROCESSED_LABEL = "AI Job Followup Processed"
GMAIL_REPLY_SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.compose",
]
FEEDBACK_REPLY_TEMPLATE = """Thanks for letting me know, and thank you for the time your team invested in the process.

If you're open to sharing it, even a small piece of feedback on where I fell short would go a long way for me. I'm always looking to improve, and thoughtful criticism is genuinely valuable.

I appreciate the opportunity and wish you and the team all the best.
"""


class GmailFollowupManager:
    def __init__(self, database: Database, app_root: Path) -> None:
        self.database = database
        self.app_root = Path(app_root)
        load_dotenv(self.app_root / ".env")
        default_credentials = self.app_root / "data" / "gmail" / "credentials.json"
        default_token = self.app_root / "data" / "gmail" / "token.json"
        self.credentials_path = Path(os.getenv("GMAIL_CREDENTIALS_PATH") or default_credentials)
        self.token_path = Path(os.getenv("GMAIL_TOKEN_PATH") or default_token)
        self.credentials_path.parent.mkdir(parents=True, exist_ok=True)
        self.token_path.parent.mkdir(parents=True, exist_ok=True)

    def sync_application_followups(
        self,
        *,
        lookback_days: int = 45,
        max_results: int = 100,
        unread_only: bool = False,
        interactive_auth: bool = True,
        auto_send_feedback_replies: bool = False,
        auto_send_rejection_replies: bool | None = None,
        create_drafts_for_rejections: bool = True,
        close_stale_days: int = 30,
    ) -> dict[str, Any]:
        if auto_send_rejection_replies is not None:
            auto_send_feedback_replies = bool(auto_send_rejection_replies)
        service = self._build_service(interactive=interactive_auth)
        processed_label_id = self._get_or_create_label(service, FOLLOWUP_PROCESSED_LABEL)
        query = self._build_followup_query(lookback_days=lookback_days, unread_only=unread_only)
        response = service.users().messages().list(userId="me", q=query, maxResults=max_results).execute()
        messages = response.get("messages", []) or []
        applied_jobs = self._applied_and_closed_jobs()
        summary = {
            "messages_considered": len(messages),
            "processed_messages": 0,
            "messages_matched": 0,
            "matched_jobs": 0,
            "confirmations": 0,
            "rejections": 0,
            "interviews": 0,
            "feedback": 0,
            "other": 0,
            "closed_no_response": 0,
            "closed_jobs": 0,
            "drafts_created": 0,
            "drafted_replies": 0,
            "replies_sent": 0,
            "sent_replies": 0,
            "reply_skipped": 0,
            "events": [],
        }

        for item in messages:
            message_id = str(item.get("id") or "").strip()
            if not message_id or self.database.has_processed_email_message("gmail_followup", message_id):
                continue
            message = service.users().messages().get(userId="me", id=message_id, format="full").execute()
            parsed = self._parse_message(message)
            match = self._match_job(parsed, applied_jobs)
            if not match:
                summary["processed_messages"] += 1
                self.database.record_processed_email_message(
                    "gmail_followup",
                    message_id,
                    "gmail_followup",
                    0,
                    0,
                    0,
                    "unmatched",
                    parsed["subject"][:500],
                )
                self._mark_processed(service, message_id, processed_label_id)
                continue

            summary["processed_messages"] += 1
            summary["messages_matched"] += 1
            summary["matched_jobs"] += 1
            classification = self._classify_followup(parsed)
            job_id = int(match["job_id"])
            received_at = parsed.get("received_at")
            reply_action = "none"
            reply_subject = ""
            reply_body = ""
            feedback_summary = ""
            feedback_tags: list[str] = []

            self.database.update_job_follow_up(
                job_id,
                last_response_type=classification,
                last_response_at=received_at,
            )

            if classification == "confirmation":
                summary["confirmations"] += 1
            elif classification == "interview":
                summary["interviews"] += 1
            elif classification == "feedback":
                summary["feedback"] += 1
                feedback_summary = self._summarise_feedback(parsed)
                feedback_tags = self._tag_feedback(parsed)
                self.database.update_job_follow_up(
                    job_id,
                    follow_up_feedback_summary=feedback_summary,
                    follow_up_feedback_tags_json=json.dumps(feedback_tags),
                )
                if str(match.get("application_status") or "").strip().lower() != "closed":
                    self.database.close_application(job_id, "closed_feedback_received", note="Closed after recruiter feedback was received.")
                    summary["closed_jobs"] += 1
            elif classification == "rejection":
                summary["rejections"] += 1
                if str(match.get("application_status") or "").strip().lower() != "closed":
                    self.database.close_application(job_id, "closed_rejected", note="Closed after recruiter rejection email.")
                    summary["closed_jobs"] += 1
                sender_email = str(parsed.get("sender_email") or "")
                if self._can_reply_to_sender(sender_email):
                    reply_subject = self._build_reply_subject(parsed["subject"])
                    reply_body = FEEDBACK_REPLY_TEMPLATE
                    if auto_send_feedback_replies:
                        self._send_reply(
                            service,
                            parsed=parsed,
                            reply_subject=reply_subject,
                            reply_body=reply_body,
                        )
                        reply_action = "sent"
                        summary["replies_sent"] += 1
                        summary["sent_replies"] += 1
                    elif create_drafts_for_rejections:
                        self._create_reply_draft(
                            service,
                            parsed=parsed,
                            reply_subject=reply_subject,
                            reply_body=reply_body,
                        )
                        reply_action = "drafted"
                        summary["drafts_created"] += 1
                        summary["drafted_replies"] += 1
                    else:
                        reply_action = "skipped"
                        summary["reply_skipped"] += 1
                else:
                    reply_action = "skipped_no_reply_address"
                    summary["reply_skipped"] += 1
            else:
                summary["other"] += 1

            self.database.update_job_follow_up(
                job_id,
                follow_up_reply_last_action=reply_action if reply_action != "none" else None,
                follow_up_reply_last_at=datetime.now().isoformat(timespec="seconds") if reply_action not in {"", "none"} else None,
            )
            self.database.record_application_email_event(
                job_id=job_id,
                provider="gmail_followup",
                message_id=message_id,
                thread_id=str(parsed.get("thread_id") or ""),
                subject=str(parsed.get("subject") or ""),
                sender=str(parsed.get("from") or ""),
                sender_email=str(parsed.get("sender_email") or ""),
                received_at=str(received_at or ""),
                classification=classification,
                match_score=float(match.get("score") or 0.0),
                raw_snippet=str(parsed.get("text") or "")[:1200],
                feedback_summary=feedback_summary,
                feedback_tags_json=json.dumps(feedback_tags),
                reply_action=reply_action,
                reply_subject=reply_subject,
                reply_body=reply_body,
                reply_reference_message_id=str(parsed.get("message_id_header") or ""),
            )
            self.database.record_processed_email_message(
                "gmail_followup",
                message_id,
                "gmail_followup",
                0,
                0,
                0,
                classification,
                f"job_id={job_id} reply_action={reply_action}",
            )
            self._mark_processed(service, message_id, processed_label_id)
            summary["events"].append(
                {
                    "job_id": job_id,
                    "title": str(match.get("title") or ""),
                    "company": str(match.get("company") or ""),
                    "classification": classification,
                    "reply_action": reply_action,
                    "sender": str(parsed.get("from") or ""),
                    "subject": str(parsed.get("subject") or ""),
                }
            )

        summary["closed_no_response"] = self.database.close_stale_applied_jobs(days=close_stale_days)
        summary["auto_closed_stale"] = summary["closed_no_response"]
        summary["closed_jobs"] += int(summary["closed_no_response"])
        return summary

    def list_followup_events(self, limit: int = 200) -> pd.DataFrame:
        return self.database.list_application_email_events(limit=limit)

    def _applied_and_closed_jobs(self) -> pd.DataFrame:
        jobs = self.database.search_jobs(status="All", min_fit_score=0)
        if jobs.empty:
            return jobs
        effective_status = jobs.get("application_status", pd.Series([""] * len(jobs), index=jobs.index)).fillna("").astype(str).str.strip().str.lower()
        return jobs[effective_status.isin(["applied", "closed"])].copy()

    def _build_service(self, interactive: bool) -> Resource:
        creds = self._load_credentials(interactive=interactive)
        if creds is None or not creds.valid:
            raise RuntimeError("Unable to create Gmail follow-up credentials.")
        return build("gmail", "v1", credentials=creds)

    def _load_credentials(self, interactive: bool) -> Credentials | None:
        creds = None
        if self.token_path.exists():
            creds = Credentials.from_authorized_user_file(str(self.token_path), GMAIL_REPLY_SCOPES)
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
            raise FileNotFoundError(f"Gmail credentials file not found at {self.credentials_path}.")
        flow = InstalledAppFlow.from_client_secrets_file(str(self.credentials_path), GMAIL_REPLY_SCOPES)
        creds = flow.run_local_server(port=0)
        self.token_path.write_text(creds.to_json(), encoding="utf-8")
        return creds

    def _build_followup_query(self, *, lookback_days: int, unread_only: bool) -> str:
        terms = [
            f"newer_than:{max(1, int(lookback_days))}d",
            "-from:me",
            f'-label:"{FOLLOWUP_PROCESSED_LABEL}"',
        ]
        if unread_only:
            terms.append("is:unread")
        return " ".join(terms)

    def _parse_message(self, message: dict[str, Any]) -> dict[str, Any]:
        payload = dict(message.get("payload") or {})
        headers = {str(header.get("name") or "").lower(): str(header.get("value") or "") for header in payload.get("headers", [])}
        text, html = self._extract_payload_parts(payload)
        combined_text = text.strip() or self._html_to_text(html)
        from_header = headers.get("from", "")
        sender_name, sender_email = parseaddr(from_header)
        received_at = self._received_timestamp_from_message(message, headers)
        return {
            "id": str(message.get("id") or ""),
            "thread_id": str(message.get("threadId") or ""),
            "subject": headers.get("subject", ""),
            "from": from_header,
            "sender_name": sender_name,
            "sender_email": sender_email,
            "text": combined_text,
            "html": html,
            "received_at": received_at,
            "message_id_header": headers.get("message-id", ""),
            "references": headers.get("references", ""),
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
        for part in payload.get("parts", []) or []:
            plain_text, html_text = self._extract_payload_parts(part)
            if plain_text:
                plain_parts.append(plain_text)
            if html_text:
                html_parts.append(html_text)
        plain_text = "\n".join(part for part in plain_parts if part).strip()
        html_text = "\n".join(part for part in html_parts if part).strip()
        if not plain_text and body_data and mime_type not in {"text/plain", "text/html"}:
            plain_text = self._decode_gmail_body(body_data)
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

    def _normalise_text(self, value: str) -> str:
        text = re.sub(r"[^a-z0-9]+", " ", str(value or "").lower())
        return " ".join(text.split())

    def _match_job(self, parsed: dict[str, Any], jobs: pd.DataFrame) -> dict[str, Any] | None:
        if jobs.empty:
            return None
        subject_text = self._normalise_text(str(parsed.get("subject") or ""))
        body_text = self._normalise_text(str(parsed.get("text") or ""))
        combined = f"{subject_text} {body_text}".strip()
        best: dict[str, Any] | None = None
        best_score = 0.0
        for _, row in jobs.iterrows():
            title = str(row.get("title") or "").strip()
            company = str(row.get("company") or "").strip()
            title_text = self._normalise_text(title)
            company_text = self._normalise_text(company)
            if not title_text:
                continue
            score = 0.0
            if title_text and title_text in subject_text:
                score += 14.0
            elif title_text and title_text in body_text:
                score += 10.0
            else:
                title_tokens = [token for token in title_text.split() if len(token) > 2]
                if title_tokens:
                    token_hits = sum(1 for token in title_tokens if token in combined)
                    score += min(8.0, token_hits * 2.0)
            if company_text and company_text in combined:
                score += 6.0
            company_tokens = [token for token in company_text.split() if len(token) > 2]
            if company_tokens:
                score += min(3.0, sum(1 for token in company_tokens if token in combined))
            if score > best_score:
                best_score = score
                best = {
                    "job_id": int(row["job_id"]),
                    "title": title,
                    "company": company,
                    "application_status": str(row.get("application_status") or ""),
                    "score": score,
                }
        if best is None or best_score < 8.0:
            return None
        return best

    def _classify_followup(self, parsed: dict[str, Any]) -> str:
        subject = self._normalise_text(str(parsed.get("subject") or ""))
        text = self._normalise_text(str(parsed.get("text") or ""))
        combined = f"{subject} {text}".strip()
        if any(needle in combined for needle in ["thanks for applying", "application received", "we have received your application", "your application has been received", "application has been submitted", "application confirmation"]):
            return "confirmation"
        if any(needle in combined for needle in ["interview", "phone screen", "schedule a call", "next step", "availability for", "meet with", "teams interview"]):
            return "interview"
        if any(needle in combined for needle in ["feedback", "fell short", "more experience", "stronger candidate", "looking for someone", "lacked experience", "missing experience", "not enough experience", "improve"]):
            return "feedback"
        if any(needle in combined for needle in ["unfortunately", "unsuccessful", "not proceed", "not moving forward", "another candidate", "position has been filled", "role has been filled", "regret to inform", "not selected"]):
            return "rejection"
        return "other"

    def _summarise_feedback(self, parsed: dict[str, Any]) -> str:
        text = " ".join(str(parsed.get("text") or "").split())
        if not text:
            return ""
        sentences = re.split(r"(?<=[.!?])\s+", text)
        trimmed = [sentence.strip() for sentence in sentences if sentence.strip()]
        return " ".join(trimmed[:2])[:500]

    def _tag_feedback(self, parsed: dict[str, Any]) -> list[str]:
        text = self._normalise_text(str(parsed.get("text") or ""))
        tags: list[str] = []
        rules = {
            "skills_gap": ["skill", "technical", "tooling", "sql", "python", "power bi", "azure", "snowflake", "dbt"],
            "experience_gap": ["experience", "background", "exposure"],
            "seniority_gap": ["senior", "leadership", "manager", "people leadership"],
            "domain_knowledge": ["industry", "domain", "sector", "commercial"],
            "communication": ["communication", "stakeholder", "presentation"],
        }
        for tag, needles in rules.items():
            if any(needle in text for needle in needles):
                tags.append(tag)
        return tags or ["other"]

    def _can_reply_to_sender(self, sender_email: str) -> bool:
        lowered = str(sender_email or "").strip().lower()
        return bool(lowered) and all(token not in lowered for token in ["no-reply", "noreply", "do-not-reply", "donotreply"])

    def _build_reply_subject(self, subject: str) -> str:
        cleaned = str(subject or "").strip()
        if cleaned.lower().startswith("re:"):
            return cleaned
        return f"Re: {cleaned or 'Application update'}"

    def _build_raw_message(
        self,
        *,
        parsed: dict[str, Any],
        reply_subject: str,
        reply_body: str,
    ) -> str:
        message = EmailMessage()
        message["To"] = str(parsed.get("sender_email") or "")
        message["Subject"] = reply_subject
        message["In-Reply-To"] = str(parsed.get("message_id_header") or "")
        references = str(parsed.get("references") or "").strip()
        if references:
            message["References"] = f"{references} {str(parsed.get('message_id_header') or '').strip()}".strip()
        elif parsed.get("message_id_header"):
            message["References"] = str(parsed.get("message_id_header") or "")
        message.set_content(reply_body.strip() + "\n")
        encoded = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")
        return encoded

    def _create_reply_draft(self, service: Resource, *, parsed: dict[str, Any], reply_subject: str, reply_body: str) -> None:
        raw = self._build_raw_message(parsed=parsed, reply_subject=reply_subject, reply_body=reply_body)
        service.users().drafts().create(
            userId="me",
            body={"message": {"raw": raw, "threadId": str(parsed.get("thread_id") or "")}},
        ).execute()

    def _send_reply(self, service: Resource, *, parsed: dict[str, Any], reply_subject: str, reply_body: str) -> None:
        raw = self._build_raw_message(parsed=parsed, reply_subject=reply_subject, reply_body=reply_body)
        service.users().messages().send(
            userId="me",
            body={"raw": raw, "threadId": str(parsed.get("thread_id") or "")},
        ).execute()

    def _get_label_id(self, service: Resource, label_name: str) -> str | None:
        labels = service.users().labels().list(userId="me").execute().get("labels", [])
        for label in labels:
            if label.get("name") == label_name:
                return str(label["id"])
        return None

    def _get_or_create_label(self, service: Resource, label_name: str) -> str:
        existing = self._get_label_id(service, label_name)
        if existing:
            return existing
        created = service.users().labels().create(
            userId="me",
            body={"name": label_name, "labelListVisibility": "labelShow", "messageListVisibility": "show"},
        ).execute()
        return str(created["id"])

    def _mark_processed(self, service: Resource, message_id: str, processed_label_id: str) -> None:
        service.users().messages().modify(
            userId="me",
            id=message_id,
            body={"addLabelIds": [processed_label_id]},
        ).execute()
