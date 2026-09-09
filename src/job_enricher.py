from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup

from src.database import Database
from src.fit_scorer import score_job

NOISE_PATTERNS = [
    r"\bapply\b",
    r"\bsave\b",
    r"\bshare\b",
    r"similar jobs",
    r"people also viewed",
    r"recommended jobs",
    r"sign in",
    r"create job alert",
    r"skip to main content",
    r"terms and conditions",
    r"privacy policy",
    r"cookie",
    r"linkedin home",
]


@dataclass
class EnrichmentFetchResult:
    html: str
    text: str
    used_playwright: bool
    clicked_show_more: bool


def _console_safe_text(value: Any, *, encoding: str | None = None) -> str:
    """Return diagnostic text that can be written by the active console."""
    selected_encoding = encoding or getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        return str(value).encode(selected_encoding, errors="backslashreplace").decode(selected_encoding)
    except LookupError:
        return str(value).encode("utf-8", errors="backslashreplace").decode("utf-8")


def parse_relative_posted_date(text: str, today: date) -> date | None:
    cleaned = (text or "").strip().lower()
    if not cleaned:
        return None
    cleaned = cleaned.replace("posted ", "").replace("listed ", "").strip()
    if cleaned in {"today", "posted today"}:
        return today
    if "yesterday" in cleaned:
        return today - timedelta(days=1)
    if re.search(r"\b\d+\s+hours?\s+ago\b", cleaned):
        return today
    match = re.search(r"\b(\d+)\s+days?\s+ago\b", cleaned)
    if match:
        return today - timedelta(days=int(match.group(1)))
    match = re.search(r"\b(\d+)\+\s+days?\s+ago\b", cleaned)
    if match:
        return today - timedelta(days=int(match.group(1)))
    for fmt in ("%d %B %Y", "%d %b %Y", "%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(text.strip(), fmt).date()
        except ValueError:
            continue
    return None


class JobEnricher:
    def __init__(self, database: Database, app_root: Path) -> None:
        self.database = database
        self.app_root = Path(app_root)
        self.debug_dir = self.app_root / "data" / "debug_job_pages"
        self.debug_dir.mkdir(parents=True, exist_ok=True)

    def enrich_job(self, job_id: int, *, force: bool = False) -> dict[str, Any]:
        job = self.database.get_job_details(job_id)
        if job is None:
            raise ValueError(f"Job {job_id} not found")

        if not force and str(job.get("enrichment_status") or "").lower() == "success" and str(job.get("full_description") or "").strip():
            return {"job_id": job_id, "status": "skipped", "reason": "already_enriched"}

        url = str(job.get("url") or "").strip()
        source = self._detect_source(url)
        if not url:
            self.database.update_job_enrichment_result(
                job_id,
                full_description=None,
                about_the_job=None,
                source_page_text=None,
                enrichment_status="failed",
                enrichment_error="missing_job_url",
                enriched_at=datetime.now().isoformat(timespec="seconds"),
            )
            return {"job_id": job_id, "status": "failed", "error": "missing_job_url"}

        clicked_show_more = False
        try:
            fetched = self._fetch_job_page(url, source=source)
            clicked_show_more = fetched.clicked_show_more
            parsed = self._parse_source_page(url, fetched.html)
            parsed = self._finalise_parsed_payload(parsed)
            description_for_scoring = parsed["about_the_job"] or parsed["full_description"] or parsed["source_page_text"]
            if not description_for_scoring.strip():
                raise ValueError("no_visible_description_found")

            scored = score_job(
                title=parsed.get("title") or str(job.get("title") or ""),
                location=parsed.get("location") or str(job.get("location") or ""),
                salary=parsed.get("salary") or str(job.get("salary") or ""),
                description=description_for_scoring,
            )
            enriched_at = datetime.now().isoformat(timespec="seconds")
            self.database.update_job_enrichment_result(
                job_id,
                full_description=parsed.get("full_description") or None,
                about_the_job=parsed.get("about_the_job") or None,
                source_page_text=parsed.get("source_page_text") or None,
                enrichment_status="success",
                enrichment_error=None,
                enriched_at=enriched_at,
                date_posted=parsed.get("date_posted"),
                date_posted_text=parsed.get("date_posted_text"),
                title=(parsed.get("title") or None),
                company=(parsed.get("company") or None),
                location=(parsed.get("location") or None),
                salary=(parsed.get("salary") or None),
                employment_type=(parsed.get("employment_type") or None),
                apply_type=(parsed.get("apply_type") or None),
                has_apply_button=parsed.get("has_apply_button"),
                has_quick_apply_button=parsed.get("has_quick_apply_button"),
                apply_area_salary_text=(parsed.get("apply_area_salary_text") or None),
                fit_score=scored.score,
                seniority=str(scored.enrichment.get("seniority") or ""),
                extracted_keywords=list(scored.enrichment.get("keywords") or []),
                responsibilities=str(scored.enrichment.get("responsibilities") or ""),
                requirements=str(scored.enrichment.get("requirements") or ""),
            )
            self.database.log_run(
                "job_enrichment",
                "enriched",
                "success",
                self._build_log_details(
                    job_id=job_id,
                    source=source,
                    title_found=bool(parsed.get("title")),
                    date_posted_found=bool(parsed.get("date_posted")),
                    description_chars=len(description_for_scoring),
                    clicked_show_more=clicked_show_more,
                    enrichment_status="success",
                ),
            )
            return {"job_id": job_id, "status": "success", "url": url}
        except Exception as exc:  # noqa: BLE001
            debug_path = self._save_failed_debug(job_id, url, locals().get("fetched", EnrichmentFetchResult("", "", False, False)).html, str(exc))
            self.database.update_job_enrichment_result(
                job_id,
                full_description=None,
                about_the_job=None,
                source_page_text=locals().get("fetched", EnrichmentFetchResult("", "", False, False)).text,
                enrichment_status="failed",
                enrichment_error=str(exc),
                enriched_at=datetime.now().isoformat(timespec="seconds"),
            )
            self.database.log_run(
                "job_enrichment",
                "enrichment_failed",
                "error",
                self._build_log_details(
                    job_id=job_id,
                    source=source,
                    title_found=False,
                    date_posted_found=False,
                    description_chars=0,
                    clicked_show_more=clicked_show_more,
                    enrichment_status="failed",
                    error=str(exc),
                    debug_path=debug_path,
                ),
            )
            return {"job_id": job_id, "status": "failed", "url": url, "error": str(exc), "debug_path": debug_path}

    def enrich_jobs(self, job_ids: list[int], *, force: bool = False) -> dict[str, Any]:
        summary = {"attempted": 0, "success": 0, "failed": 0, "skipped": 0, "results": []}
        for job_id in job_ids:
            summary["attempted"] += 1
            result = self.enrich_job(job_id, force=force)
            summary["results"].append(result)
            summary[result["status"]] += 1
        return summary

    def _fetch_job_page(self, url: str, *, source: str) -> EnrichmentFetchResult:
        if source == "linkedin":
            playwright_result = self._fetch_with_playwright(url)
            if playwright_result is not None:
                return playwright_result
        html = self._fetch_with_requests(url)
        return EnrichmentFetchResult(html=html, text=self._html_to_text(html), used_playwright=False, clicked_show_more=False)

    def _fetch_with_playwright(self, url: str) -> EnrichmentFetchResult | None:
        try:
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
            from playwright.sync_api import sync_playwright
        except Exception:  # noqa: BLE001
            return None

        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                page = browser.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=20000)
                page.wait_for_timeout(1500)
                clicked = False
                for label in ["Show more", "See more", "more"]:
                    locator = page.get_by_text(label, exact=False)
                    if locator.count() > 0:
                        try:
                            locator.first.click(timeout=2000)
                            page.wait_for_timeout(800)
                            clicked = True
                            break
                        except Exception:  # noqa: BLE001
                            continue
                html = page.content()
                text = page.locator("body").inner_text(timeout=3000)
                browser.close()
                blocked_text = text.lower()
                if any(token in blocked_text for token in ["sign in", "join linkedin", "captcha", "log in"]):
                    raise ValueError("linkedin_login_or_blocked")
                return EnrichmentFetchResult(html=html, text=text, used_playwright=True, clicked_show_more=clicked)
        except PlaywrightTimeoutError as exc:
            raise ValueError(f"playwright_timeout: {exc}") from exc

    def _fetch_with_requests(self, url: str) -> str:
        response = requests.get(
            url,
            timeout=20,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
                "Accept-Language": "en-NZ,en;q=0.9",
            },
        )
        response.raise_for_status()
        return response.text

    def _parse_source_page(self, url: str, html: str) -> dict[str, str]:
        source = self._detect_source(url)
        soup = BeautifulSoup(html, "html.parser")
        if source == "seek":
            return self._parse_seek_page(soup)
        if source == "linkedin":
            return self._parse_linkedin_page(soup)
        if source == "trademe":
            return self._parse_trademe_page(soup)
        return self._generic_parse_page(soup)

    def _parse_seek_page(self, soup: BeautifulSoup) -> dict[str, str]:
        main_text = self._description_text(soup, ["main", "body"])
        full_description = self._description_text(
            soup,
            ["[data-automation='jobAdDetails']", "[data-testid='job-details']", "main"],
        )
        apply_details = self._extract_seek_apply_details(soup)
        return {
            "title": self._first_text(soup, ["h1", "[data-automation='job-detail-title']"]),
            "company": self._first_text(soup, ["[data-automation='advertiser-name']", "[data-automation='company-link']"]),
            "location": self._first_text(soup, ["[data-automation='job-detail-location']", "[data-automation='job-location']"]),
            "salary": self._first_text(soup, ["[data-automation='job-detail-salary']", "[data-automation='salary']"]),
            "employment_type": self._find_text_near_labels(main_text, ["work type", "job type", "employment type"]),
            "date_posted_text": self._find_posted_text(main_text),
            "date_posted": "",
            "about_the_job": self._extract_section_by_headings(main_text, ["about the role", "about you", "responsibilities", "requirements", "skills"]),
            "full_description": full_description,
            "source_page_text": main_text,
            "apply_type": apply_details["apply_type"],
            "has_apply_button": apply_details["has_apply_button"],
            "has_quick_apply_button": apply_details["has_quick_apply_button"],
            "apply_area_salary_text": apply_details["apply_area_salary_text"],
        }

    def _parse_linkedin_page(self, soup: BeautifulSoup) -> dict[str, str]:
        main_text = self._description_text(soup, ["main", "body"])
        about_section = self._description_text(
            soup,
            [".show-more-less-html__markup", ".description__text", ".jobs-description-content__text", "main"],
        )
        return {
            "title": self._first_text(soup, ["h1", ".top-card-layout__title"]),
            "company": self._first_text(soup, [".topcard__org-name-link", ".topcard__flavor"]),
            "location": self._first_text(soup, [".topcard__flavor--bullet", ".topcard__flavor.topcard__flavor--bullet"]),
            "salary": "",
            "employment_type": self._find_text_near_labels(main_text, ["employment type", "job type"]),
            "date_posted_text": self._find_posted_text(main_text),
            "date_posted": "",
            "about_the_job": self._extract_section_by_headings(about_section or main_text, ["about the job", "job description"]),
            "full_description": about_section,
            "source_page_text": main_text,
        }

    def _parse_trademe_page(self, soup: BeautifulSoup) -> dict[str, str]:
        main_text = self._description_text(soup, ["main", "body"])
        full_description = self._description_text(
            soup,
            ["[data-testid='listing-description']", ".tm-marketplace-listing-description", "main"],
        )
        return {
            "title": self._first_text(soup, ["h1", "[data-testid='listing-title']"]),
            "company": self._first_text(soup, ["[data-testid='listing-seller']", ".tm-marketplace-seller"]),
            "location": self._first_text(soup, ["[data-testid='location']", ".tm-marketplace-buyer-options__shipping"]),
            "salary": self._first_text(soup, ["[data-testid='salary']", ".tm-marketplace-buyer-options__price"]),
            "employment_type": self._find_text_near_labels(main_text, ["job type", "employment type", "work type"]),
            "date_posted_text": self._find_posted_text(main_text),
            "date_posted": "",
            "about_the_job": self._extract_section_by_headings(main_text, ["about the role", "what you'll be doing", "about you"]),
            "full_description": full_description,
            "source_page_text": main_text,
        }

    def _generic_parse_page(self, soup: BeautifulSoup) -> dict[str, str]:
        page_text = self._description_text(soup, ["main", "article", "body"])
        return {
            "title": self._first_text(soup, ["h1", "title"]),
            "company": "",
            "location": "",
            "salary": "",
            "employment_type": self._find_text_near_labels(page_text, ["job type", "employment type"]),
            "date_posted_text": self._find_posted_text(page_text),
            "date_posted": "",
            "about_the_job": self._extract_section_by_headings(page_text, ["about the job", "job description"]),
            "full_description": page_text,
            "source_page_text": page_text,
        }

    def _finalise_parsed_payload(self, parsed: dict[str, str]) -> dict[str, str]:
        today = datetime.now().date()
        date_posted_text = self._clean_visible_text(parsed.get("date_posted_text") or "")
        date_posted = parse_relative_posted_date(date_posted_text, today)
        about_the_job = self._clean_visible_text(parsed.get("about_the_job") or "")
        full_description = self._clean_visible_text(parsed.get("full_description") or "")
        source_page_text = self._clean_visible_text(parsed.get("source_page_text") or "")
        if about_the_job and len(about_the_job) < 120 and full_description:
            about_the_job = ""
        salary = self._validated_salary_text(parsed.get("salary") or "")
        apply_area_salary_text = self._validated_salary_text(parsed.get("apply_area_salary_text") or "")
        return {
            "title": self._clean_visible_text(parsed.get("title") or "")[:200],
            "company": self._clean_visible_text(parsed.get("company") or "")[:200],
            "location": self._clean_visible_text(parsed.get("location") or "")[:200],
            "salary": salary,
            "employment_type": self._clean_visible_text(parsed.get("employment_type") or "")[:200],
            "date_posted_text": date_posted_text[:100],
            "date_posted": date_posted.isoformat() if date_posted else "",
            "about_the_job": about_the_job,
            "full_description": full_description,
            "source_page_text": source_page_text,
            "apply_type": self._normalise_compact_text(parsed.get("apply_type") or "")[:40],
            "has_apply_button": bool(parsed.get("has_apply_button")),
            "has_quick_apply_button": bool(parsed.get("has_quick_apply_button")),
            "apply_area_salary_text": apply_area_salary_text,
        }

    def _description_text(self, soup: BeautifulSoup, selectors: list[str]) -> str:
        for selector in selectors:
            element = soup.select_one(selector)
            if element:
                text = self._clean_visible_text(element.get_text("\n", strip=True))
                if len(text) >= 80:
                    return text
        return ""

    def _first_text(self, soup: BeautifulSoup, selectors: list[str]) -> str:
        for selector in selectors:
            element = soup.select_one(selector)
            if element:
                text = self._clean_visible_text(element.get_text(" ", strip=True))
                if text:
                    return text[:200]
        return ""

    def _html_to_text(self, html: str) -> str:
        soup = BeautifulSoup(html, "html.parser")
        return self._clean_visible_text(soup.get_text("\n", strip=True))

    def _clean_visible_text(self, text: str) -> str:
        cleaned_lines: list[str] = []
        for raw_line in str(text or "").splitlines():
            line = re.sub(r"\s+", " ", raw_line).strip(" -\t\r\n")
            if not line:
                continue
            if any(re.search(pattern, line, flags=re.IGNORECASE) for pattern in NOISE_PATTERNS):
                continue
            cleaned_lines.append(line)
        cleaned = "\n".join(cleaned_lines)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
        return cleaned.strip()

    def _extract_section_by_headings(self, text: str, headings: list[str]) -> str:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        for index, line in enumerate(lines):
            if any(heading in line.lower() for heading in headings):
                collected: list[str] = []
                for candidate in lines[index + 1 :]:
                    lowered = candidate.lower()
                    if any(
                        stop in lowered
                        for stop in ["similar jobs", "people also viewed", "recommended jobs", "footer", "sign in", "create job alert"]
                    ):
                        break
                    collected.append(candidate)
                section = self._clean_visible_text("\n".join(collected))
                if len(section) >= 120:
                    return section
        return ""

    def _find_posted_text(self, text: str) -> str:
        patterns = [
            r"(Posted\s+\d+\+?\s+days?\s+ago)",
            r"(Listed\s+\d+\+?\s+days?\s+ago)",
            r"(\d+\+?\s+days?\s+ago)",
            r"(Posted yesterday)",
            r"(yesterday)",
            r"(Posted today)",
            r"(\btoday\b)",
            r"(\d+\s+hours?\s+ago)",
        ]
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match:
                return match.group(1)
        return ""

    def _find_text_near_labels(self, text: str, labels: list[str]) -> str:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        for idx, line in enumerate(lines):
            lowered = line.lower()
            for label in labels:
                if label in lowered:
                    if ":" in line:
                        return line.split(":", 1)[1].strip()
                    if idx + 1 < len(lines):
                        return lines[idx + 1]
        return ""

    def _detect_source(self, url: str) -> str:
        lowered = url.lower()
        if "seek.co.nz" in lowered or "seek.com.au" in lowered or "email.s.seek.co.nz" in lowered:
            return "seek"
        if "linkedin.com" in lowered:
            return "linkedin"
        if "trademe.co.nz" in lowered:
            return "trademe"
        return "generic"

    def _extract_seek_apply_details(self, soup: BeautifulSoup) -> dict[str, Any]:
        body_text = self._normalise_compact_text(soup.get_text("\n", strip=True))
        html_text = str(soup)
        has_quick_apply_button = False
        has_apply_button = False
        apply_type = "unknown"
        apply_area_salary_text = ""

        quick_apply_candidates = self._find_seek_action_text_candidates(soup, action_text="Quick apply")
        apply_candidates = self._find_seek_action_text_candidates(soup, action_text="Apply", exclude_quick_apply=True)
        candidate_texts = quick_apply_candidates + apply_candidates

        has_quick_apply_button = bool(quick_apply_candidates) or bool(re.search(r"\bQuick apply\b", body_text, flags=re.IGNORECASE)) or bool(
            re.search(r"\bQuick apply\b", html_text, flags=re.IGNORECASE)
        )
        print(f"[job_enricher] seek_quick_apply_detected={has_quick_apply_button}", flush=True)
        if quick_apply_candidates:
            candidate_text = _console_safe_text(" | ".join(quick_apply_candidates[:5]))
            print(f"[job_enricher] seek_apply_text_detected=Quick apply via candidates: {candidate_text}", flush=True)

        body_without_quick_apply = re.sub(r"\bQuick apply\b", " ", body_text, flags=re.IGNORECASE)
        html_without_quick_apply = re.sub(r"\bQuick apply\b", " ", html_text, flags=re.IGNORECASE)
        has_plain_apply_text = bool(apply_candidates) or bool(re.search(r"\bApply\b", body_without_quick_apply, flags=re.IGNORECASE)) or bool(
            re.search(r"\bApply\b", html_without_quick_apply, flags=re.IGNORECASE)
        )
        has_apply_button = bool(has_plain_apply_text and not has_quick_apply_button)
        if has_quick_apply_button and has_plain_apply_text:
            has_apply_button = True
        print(f"[job_enricher] seek_apply_detected={has_apply_button}", flush=True)
        if apply_candidates:
            candidate_text = _console_safe_text(" | ".join(apply_candidates[:5]))
            print(f"[job_enricher] seek_apply_text_detected=Apply via candidates: {candidate_text}", flush=True)

        salary_selector = soup.select_one('[data-automation="job-detail-salary"]')
        if salary_selector:
            selector_text = self._normalise_compact_text(salary_selector.get_text(" ", strip=True))
            apply_area_salary_text = self._validated_salary_text(selector_text)
            print(f"[job_enricher] seek_salary_selector_detected={bool(apply_area_salary_text)}", flush=True)
        else:
            print("[job_enricher] seek_salary_selector_detected=False", flush=True)
        if not apply_area_salary_text:
            for candidate in soup.find_all(lambda tag: tag.name in {"a", "button", "span", "div"} and re.search(r"\bquick apply\b|\bapply\b", tag.get_text(" ", strip=True), re.I))[:20]:
                container = candidate
                for _ in range(4):
                    if container is None:
                        break
                    container_text = self._normalise_compact_text(container.get_text("\n", strip=True))
                    salary_match = self._extract_salary_text(container_text)
                    if salary_match:
                        apply_area_salary_text = salary_match
                        break
                    container = container.parent if getattr(container, "parent", None) is not None else None
                if apply_area_salary_text:
                    break
            if not apply_area_salary_text:
                apply_area_salary_text = self._extract_salary_text(body_text)

        if has_quick_apply_button:
            apply_type = "Quick Apply"
        elif has_apply_button:
            apply_type = "Apply"

        salary_text = _console_safe_text(apply_area_salary_text or "(none)")
        print(f"[job_enricher] seek_salary_text_result={salary_text}", flush=True)
        print(f"[job_enricher] seek_apply_type_result={apply_type}", flush=True)
        if apply_type == "unknown":
            print("[job_enricher] seek_apply_type_unknown_warning=No Apply or Quick Apply text was detected.", flush=True)

        return {
            "apply_type": apply_type,
            "has_apply_button": has_apply_button,
            "has_quick_apply_button": has_quick_apply_button,
            "apply_area_salary_text": apply_area_salary_text,
            "apply_candidate_texts": candidate_texts,
        }

    def _find_seek_action_text_candidates(self, soup: BeautifulSoup, *, action_text: str, exclude_quick_apply: bool = False) -> list[str]:
        action_pattern = re.compile(rf"\b{re.escape(action_text)}\b", flags=re.IGNORECASE)
        disallowed_context = re.compile(r"\bapplication\b|\bapplied\b|\bapply by\b", flags=re.IGNORECASE)
        matches: list[str] = []
        for tag in soup.find_all(["a", "button", "span", "div"]):
            text = self._normalise_compact_text(tag.get_text(" ", strip=True))
            if not text or disallowed_context.search(text):
                continue
            if exclude_quick_apply and re.search(r"\bQuick apply\b", text, flags=re.IGNORECASE):
                continue
            if action_pattern.search(text):
                matches.append(text)
        return matches

    def _extract_salary_text(self, text: str) -> str:
        patterns = [
            r"(\$\s?\d[\d,]*(?:\s?-\s?\$\s?\d[\d,]*)?(?:\s?(?:per year|p\.a\.|pa|annual))?)",
            r"(\d[\d,]*\s?-\s?\d[\d,]*\s?(?:k|K)(?:\s?(?:per year|p\.a\.|pa|annual))?)",
            r"((?:salary|pay)\s*[:\-]?\s*[^\n]{0,80})",
        ]
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match:
                return self._clean_visible_text(match.group(1))[:200]
        return ""

    def _validated_salary_text(self, text: str) -> str:
        cleaned = self._clean_visible_text(text)[:200]
        if not cleaned:
            return ""
        lowered = cleaned.lower()
        monetary = re.search(
            r"\$\s?\d|\b\d{2,3}(?:,\d{3})?\s?k\b|\b\d[\d,]*(?:\.\d+)?\s*(?:per\s+)?(?:hour|year|annum)\b",
            lowered,
        )
        qualitative = any(
            token in lowered
            for token in [
                "salary",
                "remuneration",
                "competitive pay",
                "competitive package",
                "negotiable",
                "market rate",
                "pay range",
                "hourly rate",
                "per annum",
                "per year",
                "per hour",
                "kiwisaver",
            ]
        )
        return cleaned if monetary or qualitative else ""

    def _normalise_compact_text(self, text: str) -> str:
        return re.sub(r"\s+", " ", str(text or "")).strip()

    def _build_log_details(
        self,
        *,
        job_id: int,
        source: str,
        title_found: bool,
        date_posted_found: bool,
        description_chars: int,
        clicked_show_more: bool,
        enrichment_status: str,
        error: str | None = None,
        debug_path: str | None = None,
    ) -> str:
        details = [
            f"job_id={job_id}",
            f"source={source}",
            f"title_found={'yes' if title_found else 'no'}",
            f"date_posted_found={'yes' if date_posted_found else 'no'}",
            f"description_chars={description_chars}",
            f"clicked_show_more={'yes' if clicked_show_more else 'no'}",
            f"enrichment_status={enrichment_status}",
        ]
        if error:
            details.append(f"error={error}")
        if debug_path:
            details.append(f"debug={debug_path}")
        return "; ".join(details)

    def _save_failed_debug(self, job_id: int, url: str, html: str, error: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", url.lower()).strip("-")[:50] or f"job-{job_id}"
        html_path = self.debug_dir / f"{job_id}-{slug}.html"
        txt_path = self.debug_dir / f"{job_id}-{slug}.txt"
        html_path.write_text(html or "", encoding="utf-8")
        txt_path.write_text(f"URL: {url}\nError: {error}\n", encoding="utf-8")
        return str(html_path)
