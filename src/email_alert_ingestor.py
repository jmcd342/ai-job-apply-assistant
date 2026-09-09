from __future__ import annotations

import codecs
import re
from email import policy
from email.parser import BytesParser
from pathlib import Path
from typing import Any

from src.database import Database
from src.document_generator import DocumentGenerator
from src.email_job_parser import ParsedEmailJobs, parse_email_html_jobs
from src.job_importer import JobImporter


class EmailAlertIngestor:
    def __init__(
        self,
        database: Database,
        importer: JobImporter,
        document_generator: DocumentGenerator | None,
        data_dir: Path,
    ) -> None:
        self.database = database
        self.importer = importer
        self.document_generator = document_generator
        self.data_dir = Path(data_dir)
        self.alerts_dir = self.data_dir / "email_alerts"
        self.processed_dir = self.alerts_dir / "processed"
        self.failed_dir = self.alerts_dir / "failed"
        self.debug_email_dir = self.data_dir / "debug_emails"
        self.ensure_directories()

    def ensure_directories(self) -> None:
        self.alerts_dir.mkdir(parents=True, exist_ok=True)
        self.processed_dir.mkdir(parents=True, exist_ok=True)
        self.failed_dir.mkdir(parents=True, exist_ok=True)
        self.debug_email_dir.mkdir(parents=True, exist_ok=True)

    def ingest_pending_files(self, run_type: str = "email_alert_file_import") -> dict[str, Any]:
        self.ensure_directories()
        summary = self._empty_summary()
        files = sorted(
            [
                path
                for path in self.alerts_dir.iterdir()
                if path.is_file() and path.suffix.lower() in {".txt", ".eml"}
            ]
        )

        for file_path in files:
            file_summary = self.ingest_file(file_path, run_type=run_type, move_after=True)
            self._merge_summary(summary, file_summary)

        self.database.log_run(run_type, "folder_ingest_complete", "success", str(summary))
        return summary

    def ingest_uploaded_file(
        self,
        file_name: str,
        file_bytes: bytes,
        run_type: str = "email_alert_upload_import",
    ) -> dict[str, Any]:
        suffix = Path(file_name).suffix.lower()
        if suffix not in {".txt", ".eml"}:
            raise ValueError("Only .txt and .eml email alert files are supported")

        if suffix == ".eml":
            text, detected_source, detected_date, parsed_jobs = self._extract_from_eml_bytes(file_bytes)
        else:
            text = file_bytes.decode("utf-8-sig", errors="ignore").lstrip("\ufeff")
            detected_source = self.importer._detect_source_from_text_or_url("", text)  # noqa: SLF001
            detected_date = None
            parsed_jobs = None

        summary = self._import_text_payload(
            text=text,
            file_label=file_name,
            detected_source=detected_source,
            detected_date=detected_date,
            run_type=run_type,
            parsed_jobs=parsed_jobs,
        )
        return summary

    def ingest_file(self, file_path: Path, run_type: str = "email_alert_file_import", move_after: bool = True) -> dict[str, Any]:
        self.ensure_directories()
        try:
            suffix = file_path.suffix.lower()
            if suffix == ".eml":
                text, detected_source, detected_date, parsed_jobs = self._extract_from_eml_file(file_path)
            elif suffix == ".txt":
                text = file_path.read_text(encoding="utf-8-sig", errors="ignore").lstrip("\ufeff")
                detected_source = self.importer._detect_source_from_text_or_url("", text)  # noqa: SLF001
                detected_date = None
                parsed_jobs = None
            else:
                raise ValueError(f"Unsupported file type: {suffix}")

            summary = self._import_text_payload(
                text=text,
                file_label=file_path.name,
                detected_source=detected_source,
                detected_date=detected_date,
                run_type=run_type,
                parsed_jobs=parsed_jobs,
            )
            if move_after:
                destination = self.processed_dir / file_path.name
                self._safe_replace(file_path, destination)
            return summary
        except Exception as exc:  # noqa: BLE001
            if move_after and file_path.exists():
                destination = self.failed_dir / file_path.name
                self._safe_replace(file_path, destination)
            failure = self._empty_summary()
            failure["files_failed"] = 1
            failure["failed"] = 1
            failure["errors"].append({"file": file_path.name, "error": str(exc)})
            self.database.log_run(run_type, "email_alert_file_failed", "error", f"{file_path.name}: {exc}")
            return failure

    def _import_text_payload(
        self,
        text: str,
        file_label: str,
        detected_source: str,
        detected_date: str | None,
        run_type: str,
        parsed_jobs: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        if not text.strip():
            raise ValueError("Email alert file did not contain readable text")

        result = self.importer.import_email_alert_text(
            text,
            run_type=run_type,
            source_hint=detected_source,
            date_found=detected_date,
            parsed_jobs=parsed_jobs,
        )
        auto_drafted = 0
        if self.document_generator is not None:
            for job_id in result.get("auto_draft_job_ids", []):
                self.document_generator.generate_for_job(job_id)
                auto_drafted += 1

        summary = self._empty_summary()
        summary["files_processed"] = 1
        summary["created"] = result["created_count"]
        summary["duplicates"] = result["duplicate_count"]
        summary["failed"] = len(result["errors"])
        summary["auto_drafted"] = auto_drafted
        summary["auckland_filtered_out"] = result.get("filtered_out_count", 0)
        summary["errors"].extend({"file": file_label, "error": item["error"]} for item in result["errors"])
        self.database.log_run(
            run_type,
            "email_alert_file_imported",
            "success",
            f"{file_label}: created={summary['created']}, duplicates={summary['duplicates']}, auto_drafted={auto_drafted}",
        )
        return summary

    def _extract_from_eml_file(self, file_path: Path) -> tuple[str, str, str | None, list[dict[str, Any]] | None]:
        return self._extract_from_eml_bytes(file_path.read_bytes())

    def _extract_from_eml_bytes(self, payload: bytes) -> tuple[str, str, str | None, list[dict[str, Any]] | None]:
        cleaned_payload = payload.lstrip(codecs.BOM_UTF8)
        message = BytesParser(policy=policy.default).parsebytes(cleaned_payload)
        subject = message.get("subject", "")
        from_header = message.get("from", "")
        date_header = message.get("date")
        detected_date = None
        if date_header:
            try:
                parsed = message["date"].datetime
                if parsed is not None:
                    detected_date = parsed.date().isoformat()
            except Exception:  # noqa: BLE001
                detected_date = None

        text_parts: list[str] = []
        html_parts: list[str] = []
        if message.is_multipart():
            for part in message.walk():
                content_type = part.get_content_type()
                if content_type == "text/plain":
                    text_parts.append(part.get_content())
                elif content_type == "text/html":
                    html_parts.append(part.get_content())
                    if not text_parts:
                        text_parts.append(self._html_to_text(part.get_content()))
        else:
            content_type = message.get_content_type()
            if content_type == "text/html":
                html_parts.append(message.get_content())
                text_parts.append(self._html_to_text(message.get_content()))
            else:
                text_parts.append(message.get_content())

        text = "\n".join(part for part in text_parts if part).strip().lstrip("\ufeff")
        detected_source = self.importer._detect_source_from_text_or_url(subject + " " + from_header, text)  # noqa: SLF001
        parsed_jobs = None
        if html_parts:
            parsed_result = self._extract_jobs_from_html(
                "\n".join(html_parts),
                detected_source,
                subject=subject,
                message_id=message.get("message-id"),
            )
            if isinstance(parsed_result, ParsedEmailJobs):
                parsed_jobs = parsed_result.jobs
            else:
                parsed_jobs = parsed_result
        return text, detected_source, detected_date, parsed_jobs

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
            if "trademe" not in href.lower() and "/a/jobs/" not in href.lower():
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
            jobs.append(
                {
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
            )
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

            block_lines: list[str] = []
            if anchor is not None:
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
            jobs.append(
                {
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
            )
        return jobs

    def _html_to_text(self, html: str) -> str:
        without_scripts = re.sub(r"<(script|style).*?>.*?</\1>", " ", html, flags=re.IGNORECASE | re.DOTALL)
        with_breaks = re.sub(r"</?(div|p|br|tr|td|li|table|h1|h2|h3)[^>]*>", "\n", without_scripts, flags=re.IGNORECASE)
        text = re.sub(r"<[^>]+>", " ", with_breaks)
        text = re.sub(r"&nbsp;|&#160;", " ", text)
        text = re.sub(r"&amp;", "&", text)
        text = re.sub(r"\n\s*\n+", "\n", text)
        text = re.sub(r"[ \t]+", " ", text)
        return text.strip()

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
        ]
        return any(phrase in lowered for phrase in phrases)

    def _is_trademe_noise_line(self, line: str) -> bool:
        lowered = line.lower().strip()
        phrases = [
            "trade me jobs",
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

    def _safe_replace(self, source: Path, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            destination.unlink()
        source.replace(destination)

    def _empty_summary(self) -> dict[str, Any]:
        return {
            "files_processed": 0,
            "files_failed": 0,
            "created": 0,
            "duplicates": 0,
            "failed": 0,
            "auto_drafted": 0,
            "auckland_filtered_out": 0,
            "errors": [],
        }

    def _merge_summary(self, target: dict[str, Any], incoming: dict[str, Any]) -> None:
        for key in ["files_processed", "files_failed", "created", "duplicates", "failed", "auto_drafted", "auckland_filtered_out"]:
            target[key] += incoming.get(key, 0)
        target["errors"].extend(incoming.get("errors", []))
