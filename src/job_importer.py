from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

import pandas as pd

from src.database import Database
from src.duplicate_checker import DuplicateChecker, canonicalize_url
from src.fit_scorer import score_job
from src.text_cleaner import clean_job_description, is_garbage_description, is_valid_job_url

SOURCE_TAGS = {"linkedin", "seek", "trademe", "manual", "email_alert"}


class JobImporter:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.duplicate_checker = DuplicateChecker(database)

    def import_manual_job(
        self,
        source: str,
        url: str,
        title: str,
        company: str,
        location: str,
        salary: str,
        description: str,
        source_job_id: str | None = None,
        run_type: str = "manual_import",
        date_found: str | None = None,
        import_channel: str | None = None,
        metadata: dict[str, Any] | None = None,
        email_received_at: str | None = None,
    ) -> dict[str, Any]:
        normalised_source = self._normalise_source(source)
        cleaned_payload = clean_job_description(
            description,
            title=title.strip(),
            company=company.strip(),
            location=location.strip(),
            source=normalised_source,
            existing_url=url,
        )
        clean_description = cleaned_payload["clean_description"]
        clean_url = cleaned_payload["job_url"] or url.strip()
        metadata_payload = dict(metadata or {})
        if cleaned_payload["raw_text"]:
            metadata_payload["raw_imported_text"] = cleaned_payload["raw_text"]
        metadata_payload["clean_description_generated"] = cleaned_payload["clean_description"] != str(description or "").strip()
        if email_received_at:
            metadata_payload["email_received_at"] = email_received_at

        if not title.strip() or not company.strip():
            raise ValueError("title and company are required")
        if not is_valid_job_url(clean_url):
            raise ValueError("missing or unusable job URL")
        if is_garbage_description(clean_description):
            raise ValueError("description is too short or unusable after cleaning")

        canonical_url = canonicalize_url(clean_url)
        duplicate_id = self.duplicate_checker.find_duplicate(
            source=normalised_source,
            source_job_id=source_job_id,
            canonical_url=canonical_url,
            title=title,
            company=company,
            location=location or "",
        )

        if duplicate_id:
            self.database.log_run(run_type, "duplicate_prevented", "skipped", f"Matched existing job {duplicate_id}")
            return {
                "created": False,
                "job_id": duplicate_id,
                "fit_score": None,
                "should_auto_draft": False,
                "created_job_ids": [],
                "auto_draft_job_ids": [],
            }

        scored = score_job(title=title, location=location, salary=salary, description=clean_description)
        imported_at = datetime.now().isoformat(timespec="seconds")
        effective_date_found = date_found or date.today().isoformat()
        age_in_days = self._calculate_age_in_days(email_received_at=email_received_at, date_found=effective_date_found)
        metadata_payload["age_in_days"] = age_in_days
        job_id = self.database.insert_job(
            {
                "source": normalised_source,
                "source_job_id": source_job_id,
                "canonical_url": canonical_url,
                "url": clean_url or None,
                "title": title.strip(),
                "company": company.strip(),
                "location": location.strip(),
                "salary": salary.strip(),
                "description": clean_description.strip(),
                "date_found": effective_date_found,
                "email_received_at": email_received_at,
                "age_in_days": age_in_days,
                "fit_score": scored.score,
                "seniority": scored.enrichment["seniority"],
                "extracted_keywords": scored.enrichment["keywords"],
                "responsibilities": scored.enrichment["responsibilities"],
                "requirements": scored.enrichment["requirements"],
                "import_channel": import_channel or normalised_source,
                "imported_at": imported_at,
                "application_status": "discovered",
                "fit_category": scored.enrichment.get("fit_category"),
                "matched_skills": scored.enrichment.get("matched_skills", []),
                "missing_skills": scored.enrichment.get("missing_skills", []),
                "seniority_level": scored.enrichment.get("seniority_level"),
                "role_family": scored.enrichment.get("role_family"),
                "opportunity_quality": scored.enrichment.get("opportunity_quality"),
                "risk_flags": scored.enrichment.get("risk_flags", []),
                "metadata": metadata_payload or {"mvp_import_mode": "manual"},
            }
        )
        self.database.log_run(run_type, "job_imported", "success", f"Imported job {job_id} with fit {scored.score}")
        return {
            "created": True,
            "job_id": job_id,
            "fit_score": scored.score,
            "should_auto_draft": True,
            "created_job_ids": [job_id],
            "auto_draft_job_ids": [job_id],
        }

    def import_csv(self, frame: pd.DataFrame, run_type: str = "csv_import") -> dict[str, Any]:
        rows = frame.fillna("").to_dict(orient="records")
        return self._import_rows(
            rows,
            run_type=run_type,
            import_channel="csv",
            default_source="manual",
        )

    def import_job_rows(
        self,
        rows: list[dict[str, Any]],
        *,
        run_type: str,
        import_channel: str,
        default_source: str,
        auckland_only: bool = False,
        log_filtered_out: bool = True,
    ) -> dict[str, Any]:
        return self._import_rows(
            rows,
            run_type=run_type,
            import_channel=import_channel,
            default_source=default_source,
            auckland_only=auckland_only,
            default_location_if_missing=not auckland_only,
            log_filtered_out=log_filtered_out,
        )

    def import_email_alert_text(
        self,
        text: str,
        run_type: str = "email_alert_import",
        source_hint: str | None = None,
        date_found: str | None = None,
        email_received_at: str | None = None,
        log_filtered_out: bool = True,
        parsed_jobs: list[dict[str, Any]] | None = None,
        auckland_only: bool = True,
        skip_clearly_irrelevant_locations: bool = False,
    ) -> dict[str, Any]:
        jobs = parsed_jobs if parsed_jobs is not None else self._parse_email_alert_jobs(text, source_hint=source_hint)
        if source_hint:
            for job in jobs:
                if not job.get("source") or job.get("source") == "manual":
                    job["source"] = source_hint
        if date_found:
            for job in jobs:
                job["date_found"] = date_found
        if email_received_at:
            for job in jobs:
                job["email_received_at"] = email_received_at
        return self._import_rows(
            jobs,
            run_type=run_type,
            import_channel="email_alert",
            default_source="email_alert",
            auckland_only=auckland_only,
            skip_clearly_irrelevant_locations=skip_clearly_irrelevant_locations,
            default_location_if_missing=False,
            log_filtered_out=log_filtered_out,
        )

    def import_saved_search_text(
        self,
        search_url: str,
        page_text: str,
        run_type: str = "saved_search_import",
    ) -> dict[str, Any]:
        source = self._detect_source_from_text_or_url(search_url, page_text)
        jobs = self._parse_saved_search_jobs(page_text, source, search_url)
        return self._import_rows(
            jobs,
            run_type=run_type,
            import_channel="saved_search",
            default_source=source,
            default_location_if_missing=True,
        )

    def _import_rows(
        self,
        rows: list[dict[str, Any]],
        run_type: str,
        import_channel: str,
        default_source: str,
        auckland_only: bool = False,
        skip_clearly_irrelevant_locations: bool = False,
        default_location_if_missing: bool = True,
        log_filtered_out: bool = True,
    ) -> dict[str, Any]:
        created_count = 0
        duplicate_count = 0
        filtered_out_count = 0
        auto_draft_job_ids: list[int] = []
        created_job_ids: list[int] = []
        imported_job_titles: list[str] = []
        duplicate_job_titles: list[str] = []
        errors: list[dict[str, Any]] = []

        for index, row in enumerate(rows):
            try:
                title = str(row.get("title", "")).strip()
                company = str(row.get("company", "")).strip()
                description = str(row.get("description", "")).strip()
                if not title or not company or not description:
                    raise ValueError("title, company, and description are required")

                location = str(row.get("location", "")).strip()
                if not location and default_location_if_missing and not auckland_only:
                    location = "Auckland"
                if skip_clearly_irrelevant_locations and self._is_clearly_irrelevant_location(location, description, title):
                    filtered_out_count += 1
                    if log_filtered_out:
                        self.database.log_run(run_type, "clearly_irrelevant_location_filtered", "skipped", f"Skipped {title} at {company}")
                    continue
                if auckland_only and not self._is_auckland_job(location, description, title):
                    filtered_out_count += 1
                    if log_filtered_out:
                        self.database.log_run(run_type, "non_auckland_filtered", "skipped", f"Skipped {title} at {company}")
                    continue

                result = self.import_manual_job(
                    source=str(row.get("source", default_source)).strip() or default_source,
                    url=str(row.get("url", "")).strip(),
                    title=title,
                    company=company,
                    location=location,
                    salary=str(row.get("salary", "")).strip(),
                    description=description,
                    source_job_id=self._optional_text(row.get("source_job_id")),
                    run_type=run_type,
                    date_found=self._optional_text(row.get("date_found")),
                    import_channel=import_channel,
                    metadata={
                        key: value
                        for key, value in row.items()
                        if key
                        not in {"title", "company", "location", "url", "source", "description", "salary", "date_found", "source_job_id"}
                    }
                    | ({"raw_imported_text": str(row.get("description", "")).strip()} if str(row.get("description", "")).strip() else {}),
                    email_received_at=self._optional_text(row.get("email_received_at")),
                )
                if result["created"]:
                    created_count += 1
                    created_job_ids.append(result["job_id"])
                    imported_job_titles.append(title)
                    if result["should_auto_draft"]:
                        auto_draft_job_ids.append(result["job_id"])
                else:
                    duplicate_count += 1
                    duplicate_job_titles.append(title)
            except Exception as exc:  # noqa: BLE001
                errors.append({"row": index + 1, "error": str(exc)})
                self.database.log_run(run_type, "import_row_error", "error", f"Row {index + 1}: {exc}")

        return {
            "total_rows": len(rows),
            "created_count": created_count,
            "duplicate_count": duplicate_count,
            "filtered_out_count": filtered_out_count,
            "non_auckland_filtered": filtered_out_count,
            "errors": errors,
            "created_job_ids": created_job_ids,
            "auto_draft_job_ids": auto_draft_job_ids,
            "import_channel": import_channel,
            "imported_job_titles": imported_job_titles,
            "duplicate_job_titles": duplicate_job_titles,
        }

    def _optional_text(self, value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        if not text or text.lower() == "none":
            return None
        return text

    def _normalise_source(self, value: str) -> str:
        lowered = value.strip().lower()
        if "linkedin" in lowered:
            return "linkedin"
        if "seek" in lowered:
            return "seek"
        if "trade me" in lowered or "trademe" in lowered:
            return "trademe"
        if "email" in lowered:
            return "email_alert"
        if lowered in SOURCE_TAGS:
            return lowered
        return "manual"

    def _detect_source_from_text_or_url(self, search_url: str, text: str) -> str:
        combined = f"{search_url} {text}".lower()
        if "linkedin" in combined:
            return "linkedin"
        if "seek" in combined:
            return "seek"
        if "trade me" in combined or "trademe" in combined:
            return "trademe"
        return "manual"

    def _parse_email_alert_jobs(self, text: str, source_hint: str | None = None) -> list[dict[str, Any]]:
        source = source_hint or self._detect_source_from_text_or_url("", text)
        if source == "linkedin":
            linkedin_jobs = self._parse_linkedin_email_alert_jobs(text)
            if linkedin_jobs:
                return linkedin_jobs
        jobs: list[dict[str, Any]] = []
        blocks = re.split(r"\n\s*\n", text.strip())
        url_pattern = re.compile(r"https?://\S+")
        for block in blocks:
            lines = [line.strip() for line in block.splitlines() if line.strip()]
            if len(lines) < 2:
                continue
            urls = url_pattern.findall(block)
            source = self._detect_source_from_text_or_url(" ".join(urls), block)
            title, company, location = self._extract_title_company_location(lines, source)
            if not title or not company or self._looks_like_email_header(title, company):
                continue
            description = " ".join(lines[2:8]) if len(lines) > 2 else block
            jobs.append(
                {
                    "title": title,
                    "company": company,
                    "location": location,
                    "url": urls[0] if urls else "",
                    "source": source,
                    "description": description[:2000],
                    "salary": self._extract_salary(block),
                    "source_job_id": self._extract_source_job_id(urls[0]) if urls else None,
                    "email_received_at": None,
                }
            )
        return jobs

    def _parse_linkedin_email_alert_jobs(self, text: str) -> list[dict[str, Any]]:
        lines = [line.strip(" -*\t\r") for line in text.splitlines() if line.strip()]
        jobs: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str]] = set()
        consumed_indexes: set[int] = set()
        url_pattern = re.compile(r"https?://\S+")

        for index, line in enumerate(lines):
            if index in consumed_indexes:
                continue
            title = line.strip()
            if not self._looks_like_linkedin_title(title):
                continue

            company = ""
            location = ""
            description_bits: list[str] = []
            job_url = ""
            last_probe_used = index

            for probe in range(index + 1, min(index + 7, len(lines))):
                candidate = lines[probe].strip()
                if not candidate:
                    continue
                last_probe_used = probe
                if not job_url:
                    urls = url_pattern.findall(candidate)
                    if urls:
                        job_url = urls[0]
                        continue
                if self._is_linkedin_noise_line(candidate):
                    continue
                if not company and not self._looks_like_location_line(candidate) and not url_pattern.search(candidate):
                    company = candidate
                    continue
                if not location and self._looks_like_location_line(candidate):
                    location = candidate
                    continue
                if candidate != title and candidate != company and candidate != location and not url_pattern.search(candidate):
                    description_bits.append(candidate)

            if not company:
                continue

            key = (title.lower(), company.lower(), job_url.lower())
            if key in seen:
                continue
            seen.add(key)
            consumed_indexes.update(range(index, last_probe_used + 1))
            jobs.append(
                {
                    "title": title[:200],
                    "company": company[:200],
                    "location": location[:200],
                    "url": job_url,
                    "source": "linkedin",
                    "description": " ".join(description_bits[:4]) or f"LinkedIn alert for {title} at {company} in {location or 'Auckland'}.",
                    "salary": "",
                    "source_job_id": self._extract_source_job_id(job_url) if job_url else None,
                    "email_received_at": None,
                }
            )
        return jobs

    def _parse_saved_search_jobs(self, text: str, source: str, search_url: str) -> list[dict[str, Any]]:
        jobs: list[dict[str, Any]] = []
        url_pattern = re.compile(r"https?://\S+")
        blocks = re.split(r"\n\s*\n|(?=https?://)", text.strip())
        for block in blocks:
            raw = block.strip()
            if len(raw) < 20:
                continue
            lines = [line.strip(" -*\t") for line in raw.splitlines() if line.strip()]
            urls = url_pattern.findall(raw)
            title, company, location = self._extract_title_company_location(lines, source)
            if not title or not company or self._looks_like_email_header(title, company):
                continue
            description = " ".join(lines[2:10]) if len(lines) > 2 else raw
            jobs.append(
                {
                    "title": title,
                    "company": company,
                    "location": location,
                    "url": urls[0] if urls else search_url,
                    "source": source,
                    "description": description[:2000],
                    "salary": self._extract_salary(raw),
                    "source_job_id": self._extract_source_job_id(urls[0]) if urls else None,
                    "email_received_at": None,
                }
            )
        return jobs

    def _extract_title_company_location(self, lines: list[str], source: str) -> tuple[str, str, str]:
        title = lines[0].lstrip("\ufeff").strip() if lines else ""
        company = ""
        location = ""

        for line in lines[1:6]:
            lowered = line.lower()
            if not company and "auckland" not in lowered and "new zealand" not in lowered and not re.search(r"\$\d|salary", lowered):
                company = line
                continue
            if not location and ("auckland" in lowered or "new zealand" in lowered or lowered == "nz"):
                location = line

        if source == "linkedin" and " at " in title.lower():
            parts = re.split(r"\s+at\s+", title, maxsplit=1, flags=re.IGNORECASE)
            if len(parts) == 2:
                title = parts[0].strip()
                company = company or parts[1].strip()

        if source == "seek" and " - " in title and not company:
            parts = title.split(" - ", 1)
            if len(parts) == 2:
                title, company = parts[0].strip(), parts[1].strip()

        return title[:200], company[:200], location[:200]

    def _extract_salary(self, text: str) -> str:
        match = re.search(r"(\$[\d,]+(?:\s*-\s*\$?[\d,]+)?(?:\s*(?:per year|a year|annual|hour))?)", text, flags=re.IGNORECASE)
        return match.group(1) if match else ""

    def _extract_source_job_id(self, url: str) -> str | None:
        if not url:
            return None
        match = re.search(r"/jobs/view/(\d+)|job/([^/?#]+)|listing/([^/?#]+)", url)
        if not match:
            return None
        return next((group for group in match.groups() if group), None)

    def _is_auckland_job(self, location: str, description: str, title: str) -> bool:
        combined = " ".join([location, description, title]).lower()
        auckland_markers = ["auckland", "north shore", "manukau", "waitakere", "mt wellington", "penrose", "albany"]
        return any(marker in combined for marker in auckland_markers)

    def _is_clearly_irrelevant_location(self, location: str, description: str, title: str) -> bool:
        combined = " ".join([location, description, title]).lower()
        nz_patterns = [
            r"\bnew zealand\b",
            r"\bnz\b",
            r"\bauckland\b",
            r"\bnorth shore\b",
            r"\bmanukau\b",
            r"\bwaitakere\b",
            r"\bmt wellington\b",
            r"\bpenrose\b",
            r"\balbany\b",
            r"\bwellington\b",
            r"\bchristchurch\b",
            r"\bhamilton\b",
            r"\btauranga\b",
            r"\bremote nz\b",
        ]
        if any(re.search(pattern, combined) for pattern in nz_patterns):
            return False
        non_nz_patterns = [
            r"\baustralia\b",
            r"\bsydney\b",
            r"\bmelbourne\b",
            r"\bbrisbane\b",
            r"\bperth\b",
            r"\badelaide\b",
            r"\bunited kingdom\b",
            r"\buk\b",
            r"\blondon\b",
            r"\bmanchester\b",
            r"\bunited states\b",
            r"\busa\b",
            r"\bnew york\b",
            r"\bcalifornia\b",
            r"\bcanada\b",
            r"\btoronto\b",
            r"\bvancouver\b",
            r"\bsingapore\b",
            r"\bindia\b",
            r"\bphilippines\b",
            r"\beurope\b",
            r"\bgermany\b",
            r"\bberlin\b",
            r"\bdubai\b",
            r"\buae\b",
        ]
        return any(re.search(pattern, combined) for pattern in non_nz_patterns)

    def _looks_like_email_header(self, title: str, company: str) -> bool:
        header_prefixes = ("from:", "subject:", "date:", "content-type:", "to:")
        title_lower = title.lower()
        company_lower = company.lower()
        return title_lower.startswith(header_prefixes) or company_lower.startswith(header_prefixes)

    def _looks_like_linkedin_title(self, line: str) -> bool:
        lowered = line.lower().strip()
        if not lowered or len(lowered) < 5 or len(lowered) > 140:
            return False
        if self._is_linkedin_noise_line(lowered):
            return False
        if self._looks_like_location_line(lowered):
            return False
        if re.search(r"https?://", lowered):
            return False
        if lowered.startswith(("your job alert", "job alert", "view all", "show more", "unsubscribe")):
            return False
        return len(re.findall(r"[A-Za-z]{2,}", line)) >= 2

    def _is_linkedin_noise_line(self, line: str) -> bool:
        lowered = line.lower().strip()
        noise_phrases = [
            "your job alert has been created",
            "your job alert",
            "job alert has been created",
            "view job",
            "apply",
            "save",
            "show more",
            "see all jobs",
            "unsubscribe",
            "linkedin",
            "school alumni",
            "company alumni",
            "actively hiring",
            "promoted",
            "job alert digest",
            "viewed job reminder",
        ]
        return any(phrase in lowered for phrase in noise_phrases)

    def _looks_like_location_line(self, line: str) -> bool:
        lowered = line.lower()
        location_markers = [
            "auckland",
            "new zealand",
            "nz",
            "remote",
            "hybrid",
            "on-site",
            "onsite",
            "wellington",
            "christchurch",
        ]
        return any(marker in lowered for marker in location_markers)

    def _calculate_age_in_days(self, email_received_at: str | None, date_found: str) -> int:
        reference_value = email_received_at or date_found
        if not reference_value:
            return 0
        try:
            parsed = datetime.fromisoformat(reference_value.replace("Z", "+00:00"))
            reference_date = parsed.date()
        except ValueError:
            try:
                reference_date = date.fromisoformat(reference_value[:10])
            except ValueError:
                return 0
        return max(0, (date.today() - reference_date).days)
