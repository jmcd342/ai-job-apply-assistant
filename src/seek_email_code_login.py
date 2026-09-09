from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
SEEK_CODE_PATTERNS = [
    re.compile(r"\b(\d{6})\b"),
    re.compile(r"\b(\d{5,8})\b"),
]


def resolve_app_root(path: Path) -> Path:
    candidate = Path(path)
    if candidate.name.lower() == "data":
        return candidate.parent
    if candidate.is_file():
        return candidate.parent.parent if candidate.parent.name.lower() == "data" else candidate.parent
    return candidate


def load_seek_login_email(app_root: Path) -> str:
    app_root = resolve_app_root(app_root)
    profile_path = app_root / "data" / "applicant_profile.json"
    personal_path = app_root / "data" / "personal_details.json"
    for path in (profile_path, personal_path):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if path == profile_path:
            email = str(payload.get("seek_login_email") or payload.get("email") or "").strip()
        else:
            email = str(payload.get("email") or "").strip()
        if email:
            return email
    return ""


def extract_seek_code_from_text(*parts: str) -> str:
    for part in parts:
        text = str(part or "")
        for pattern in SEEK_CODE_PATTERNS:
            match = pattern.search(text)
            if match:
                return match.group(1)
    return ""


class SeekEmailCodeFetcher:
    def __init__(self, app_root: Path) -> None:
        self.app_root = resolve_app_root(app_root)
        load_dotenv(self.app_root / ".env")
        default_credentials = self.app_root / "data" / "gmail" / "credentials.json"
        default_token = self.app_root / "data" / "gmail" / "token.json"
        self.credentials_path = Path(os.getenv("GMAIL_CREDENTIALS_PATH") or default_credentials)
        self.token_path = Path(os.getenv("GMAIL_TOKEN_PATH") or default_token)

    def _load_credentials(self, interactive: bool = False) -> Credentials | None:
        creds = None
        if self.token_path.exists():
            creds = Credentials.from_authorized_user_file(str(self.token_path), GMAIL_SCOPES)
        if creds and creds.valid:
            return creds
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            self.token_path.write_text(creds.to_json(), encoding="utf-8")
            return creds
        if not interactive:
            return creds
        flow = InstalledAppFlow.from_client_secrets_file(str(self.credentials_path), GMAIL_SCOPES)
        creds = flow.run_local_server(port=0)
        self.token_path.write_text(creds.to_json(), encoding="utf-8")
        return creds

    def _build_service(self) -> Any:
        creds = self._load_credentials(interactive=False)
        if creds is None or not creds.valid:
            raise RuntimeError("Gmail API credentials are unavailable or invalid.")
        return build("gmail", "v1", credentials=creds)

    def fetch_latest_seek_code(
        self,
        *,
        issued_after: datetime,
        login_email: str = "",
        timeout_seconds: int = 90,
        poll_interval_seconds: int = 5,
    ) -> str:
        service = self._build_service()
        deadline = time.monotonic() + timeout_seconds
        issued_after_epoch_ms = int(issued_after.astimezone(timezone.utc).timestamp() * 1000)
        query = '(from:(seek OR no-reply@seek.co.nz OR no-reply@updates.seek.com) OR subject:(SEEK)) newer_than:2d'
        login_email_lower = str(login_email or "").strip().lower()
        while time.monotonic() < deadline:
            response = service.users().messages().list(userId="me", q=query, maxResults=25).execute()
            messages = list(response.get("messages") or [])
            for message in messages:
                full = service.users().messages().get(userId="me", id=message["id"], format="full").execute()
                internal_date_ms = int(str(full.get("internalDate") or "0") or "0")
                if internal_date_ms and internal_date_ms + 1000 < issued_after_epoch_ms:
                    continue
                payload = full.get("payload", {})
                headers = {header["name"].lower(): header["value"] for header in payload.get("headers", [])}
                subject = headers.get("subject", "")
                to_header = str(headers.get("to") or "")
                from_header = str(headers.get("from") or "")
                lowered_combined_headers = f"{subject}\n{to_header}\n{from_header}".lower()
                if "seek" not in lowered_combined_headers:
                    continue
                if login_email_lower and login_email_lower not in to_header.lower():
                    continue
                snippet = str(full.get("snippet") or "")
                body_text = self._extract_payload_text(payload)
                code = extract_seek_code_from_text(subject, snippet, body_text)
                if code:
                    return code
            time.sleep(max(1, poll_interval_seconds))
        return ""

    def _extract_payload_text(self, payload: dict[str, Any]) -> str:
        texts: list[str] = []
        body_data = payload.get("body", {}).get("data")
        mime_type = str(payload.get("mimeType") or "")
        if body_data and mime_type in {"text/plain", "text/html"}:
            texts.append(self._decode_body(body_data))
        for part in payload.get("parts") or []:
            text = self._extract_payload_text(part)
            if text:
                texts.append(text)
        return "\n".join(part for part in texts if part).strip()

    @staticmethod
    def _decode_body(body_data: str) -> str:
        import base64

        padded = body_data + "=" * (-len(body_data) % 4)
        decoded = base64.urlsafe_b64decode(padded.encode("utf-8"))
        return decoded.decode("utf-8", errors="ignore")
