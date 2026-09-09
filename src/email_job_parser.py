from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from bs4 import BeautifulSoup


CANDIDATE_DOMAINS = {
    "seek": ["seek.co.nz", "seek.com.au", "email.s.seek.co.nz"],
    "linkedin": ["linkedin.com"],
    "trademe": ["trademe.co.nz"],
}

URL_PATTERNS = {
    "seek": ["/job/", "/uni/ss/c/"],
    "linkedin": ["/jobs/", "/jobs/view/", "/comm/jobs/view/"],
    "trademe": ["/a/jobs/", "/jobs/", "trademe.co.nz/"],
}

LINKEDIN_REJECT_TOKENS = ["company_logo", "unsubscribe", "preferences", "login", "signup"]
LINKEDIN_WRAPPER_TOKENS = ["job_posting"]


@dataclass
class ParsedEmailJobs:
    jobs: list[dict[str, Any]]
    raw_html_saved: str | None = None
    card_counts: dict[str, int] | None = None


def parse_email_html_jobs(
    html: str,
    *,
    subject: str,
    message_id: str | None = None,
    debug_dir: Path | None = None,
) -> ParsedEmailJobs:
    soup = BeautifulSoup(html, "html.parser")
    card_counts = {
        "seek_cards_found": 0,
        "linkedin_jobcard_body_links_found": 0,
        "trademe_responsiveTitle_links_found": 0,
    }

    jobs: list[dict[str, Any]] = []
    jobs.extend(_extract_seek_jobs(soup, subject=subject, message_id=message_id, card_counts=card_counts))
    jobs.extend(_extract_linkedin_jobs(soup, subject=subject, message_id=message_id, card_counts=card_counts))
    jobs.extend(_extract_trademe_jobs(soup, subject=subject, message_id=message_id, card_counts=card_counts))

    jobs_by_key: dict[str, dict[str, Any]] = {}
    for job in jobs:
        jobs_by_key.setdefault(_job_dedupe_key(job), job)

    raw_html_saved = None
    if not jobs_by_key and debug_dir is not None:
        raw_html_saved = _save_debug_email_html(debug_dir, html, subject, message_id)
    return ParsedEmailJobs(
        jobs=list(jobs_by_key.values()),
        raw_html_saved=raw_html_saved,
        card_counts=card_counts,
    )


def normalise_candidate_job_url(url: str) -> str:
    if not url:
        return ""
    candidate = _extract_redirect_target(url.strip()) or url.strip()
    parsed = urlparse(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    source = detect_source_from_url(candidate)
    if not source:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}".rstrip("/")


def detect_source_from_url(url: str) -> str:
    lowered = url.lower()
    for source, domains in CANDIDATE_DOMAINS.items():
        if any(domain in lowered for domain in domains) and any(pattern in lowered for pattern in URL_PATTERNS[source]):
            return source
    return ""


def _extract_seek_jobs(soup: BeautifulSoup, *, subject: str, message_id: str | None, card_counts: dict[str, int]) -> list[dict[str, Any]]:
    jobs: list[dict[str, Any]] = []
    for anchor in soup.find_all("a", href=True):
        if not _looks_like_seek_card_anchor(anchor):
            continue
        card_counts["seek_cards_found"] += 1
        href = _first_valid_href(anchor)
        url = _normalise_tracking_url(href)
        if not url:
            continue
        title = _extract_title_from_anchor(anchor)
        container = _pick_card_container(anchor)
        lines = _extract_card_lines(anchor, container)
        job = _build_job_from_lines(
            title=title,
            url=url,
            source="seek",
            lines=lines,
            subject=subject,
            message_id=message_id,
        )
        if job:
            jobs.append(job)
    return jobs


def _extract_linkedin_jobs(soup: BeautifulSoup, *, subject: str, message_id: str | None, card_counts: dict[str, int]) -> list[dict[str, Any]]:
    jobs: list[dict[str, Any]] = []
    for anchor in soup.find_all("a", href=True):
        if not _looks_like_linkedin_card_anchor(anchor):
            continue
        card_counts["linkedin_jobcard_body_links_found"] += 1
        href = _first_valid_href(anchor)
        url = normalise_candidate_job_url(href) or _normalise_tracking_url(href)
        title = _clean_text(anchor.get_text(" ", strip=True))
        if not _looks_like_title(title):
            continue
        company, location = _extract_linkedin_company_location(anchor)
        if not company:
            continue
        jobs.append(
            {
                "title": title[:200],
                "company": company[:200],
                "location": location[:200],
                "url": url or href,
                "source": "linkedin",
                "description": f"LinkedIn alert card for {title} at {company} in {location or 'the listed location'}. Full details should be reviewed from the job URL.",
                "salary": "",
                "source_job_id": _extract_source_job_id(url or href),
                "raw_email_message_id": message_id or "",
                "raw_email_subject": subject[:500],
            }
        )
    return jobs


def _extract_trademe_jobs(soup: BeautifulSoup, *, subject: str, message_id: str | None, card_counts: dict[str, int]) -> list[dict[str, Any]]:
    jobs: list[dict[str, Any]] = []
    for anchor in soup.find_all("a", href=True):
        if not _looks_like_trademe_card_anchor(anchor):
            continue
        card_counts["trademe_responsiveTitle_links_found"] += 1
        href = _first_valid_href(anchor)
        url = _strip_tracking_query(href)
        if not url:
            continue
        title = _clean_text(anchor.get_text(" ", strip=True))
        if not _looks_like_title(title):
            continue
        container = _pick_card_container(anchor)
        lines = _extract_card_lines(anchor, container)
        job = _build_job_from_lines(
            title=title,
            url=url,
            source="trademe",
            lines=lines,
            subject=subject,
            message_id=message_id,
        )
        if job:
            jobs.append(job)
    return jobs


def _anchor_href_candidates(anchor: Any) -> list[str]:
    href = str(anchor.get("href") or "").strip()
    safe_redirect = str(anchor.get("data-saferedirecturl") or "").strip()
    candidates: list[str] = []
    for candidate in [href, safe_redirect]:
        if candidate and candidate not in candidates:
            candidates.append(candidate)
    return candidates


def _first_valid_href(anchor: Any) -> str:
    for candidate in _anchor_href_candidates(anchor):
        if candidate:
            return candidate
    return str(anchor.get("href") or "").strip()


def _normalise_tracking_url(url: str) -> str:
    if not url:
        return ""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}".rstrip("/")


def _extract_redirect_target(url: str) -> str:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    for key in ["url", "u", "redirect", "target", "dest", "q"]:
        if key in query and query[key]:
            return unquote(query[key][0])
    return url


def _pick_card_container(anchor: Any) -> Any:
    container = anchor
    best = anchor
    for _ in range(8):
        parent = container.parent
        if parent is None:
            break
        child_links = parent.find_all("a", href=True)
        if len(child_links) > 3:
            break
        text = parent.get_text("\n", strip=True)
        if len(text) < 20:
            break
        best = parent
        container = parent
    return best


def _extract_card_lines(anchor: Any, container: Any) -> list[str]:
    anchor_lines = _unique_lines(anchor.get_text("\n", strip=True).splitlines())
    if len(anchor_lines) >= 2:
        return anchor_lines
    return _unique_lines(container.get_text("\n", strip=True).splitlines())


def _extract_title_from_anchor(anchor: Any) -> str:
    underlined = anchor.find(style=re.compile(r"text-decoration\s*:\s*underline", re.I))
    if underlined:
        title = _clean_text(underlined.get_text(" ", strip=True))
        if title:
            return title
    return _clean_text(anchor.get_text(" ", strip=True))


def _build_job_from_lines(
    *,
    title: str,
    url: str,
    source: str,
    lines: list[str],
    subject: str,
    message_id: str | None,
) -> dict[str, Any] | None:
    clean_title = _clean_text(title)
    if not _looks_like_title(clean_title):
        clean_title = _first_title_line(lines)
    if not clean_title:
        return None

    company = ""
    location = ""
    snippet_bits: list[str] = []
    salary = ""
    seen_title = False
    for line in lines:
        clean_line = _clean_text(line)
        if not clean_line or _is_noise_line(clean_line, source):
            continue
        if clean_line == clean_title:
            seen_title = True
            continue
        if not seen_title and _looks_like_title(clean_line):
            continue
        if not company and not _looks_like_location(clean_line) and not _looks_like_salary(clean_line):
            company = clean_line
            continue
        if not location and _looks_like_location(clean_line):
            location = clean_line
            continue
        if not salary and _looks_like_salary(clean_line):
            salary = clean_line
            continue
        if clean_line not in {clean_title, company, location, salary}:
            snippet_bits.append(clean_line)

    if not company:
        return None

    snippet = " ".join(snippet_bits[:5]).strip()
    if not snippet:
        source_label = "LinkedIn" if source == "linkedin" else "SEEK" if source == "seek" else "Trade Me"
        snippet = f"{source_label} alert card for {clean_title} at {company} in {location or 'the listed location'}. Full details should be reviewed from the job URL."

    return {
        "title": clean_title[:200],
        "company": company[:200],
        "location": location[:200],
        "url": url,
        "source": source,
        "description": snippet[:4000],
        "salary": salary[:200],
        "source_job_id": _extract_source_job_id(url),
        "raw_email_message_id": message_id or "",
        "raw_email_subject": subject[:500],
    }


def _extract_source_job_id(url: str) -> str | None:
    match = re.search(
        r"/comm/jobs/view/(\d+)|/jobs/view/(\d+)|/job/([^/?#]+)|/listing/([^/?#]+)|/a/jobs/[^/]+/([^/?#]+)|trademe\.co\.nz/(\d+)",
        url,
    )
    if not match:
        return None
    return next((group for group in match.groups() if group), None)


def _extract_linkedin_company_location(anchor: Any) -> tuple[str, str]:
    containers = [anchor, _pick_card_container(anchor)]
    parent = anchor.parent
    for _ in range(4):
        if parent is None:
            break
        containers.append(parent)
        parent = parent.parent
    for container in containers:
        for paragraph in getattr(container, "find_all", lambda *_args, **_kwargs: [])("p"):
            text = _clean_text(paragraph.get_text(" ", strip=True))
            if "·" not in text:
                continue
            company, location = [part.strip() for part in text.split("·", 1)]
            if company and location:
                return company, location
    return "", ""


def _job_dedupe_key(job: dict[str, Any]) -> str:
    source = str(job.get("source") or "").lower()
    source_job_id = str(job.get("source_job_id") or "").strip()
    if source_job_id:
        return f"{source}:{source_job_id}"
    url = str(job.get("url") or "").strip().lower()
    if url:
        return f"url:{url}"
    title = str(job.get("title") or "").strip().lower()
    company = str(job.get("company") or "").strip().lower()
    location = str(job.get("location") or "").strip().lower()
    return f"{source}:{title}:{company}:{location}"


def _first_title_line(lines: list[str]) -> str:
    for line in lines:
        cleaned = _clean_text(line)
        if _looks_like_title(cleaned):
            return cleaned
    return ""


def _looks_like_title(text: str) -> bool:
    lowered = text.lower()
    if not lowered or len(lowered) < 4 or len(lowered) > 180:
        return False
    if "http://" in lowered or "https://" in lowered:
        return False
    if _looks_like_location(lowered) or _looks_like_salary(lowered):
        return False
    return len(re.findall(r"[A-Za-z]{2,}", text)) >= 2


def _looks_like_seek_card_anchor(anchor: Any) -> bool:
    style = str(anchor.get("style") or "").lower()
    inner_html = str(anchor).lower()
    lines = _unique_lines(anchor.get_text("\n", strip=True).splitlines())
    has_card_shape = (
        "display:block" in style
        or "text-decoration:underline" in inner_html
        or _visible_lines_look_like_job_card(lines)
    )
    if not has_card_shape:
        return False
    title = _extract_title_from_anchor(anchor)
    if not _looks_like_title(title):
        return False
    remaining = [line for line in lines if _clean_text(line) and _clean_text(line) != title]
    has_company = any(not _looks_like_location(line) and not _looks_like_salary(line) for line in remaining[:4])
    has_location = any(_looks_like_location(line) for line in remaining[:6])
    return has_company and has_location


def _looks_like_linkedin_card_anchor(anchor: Any) -> bool:
    href_blob = " ".join(_anchor_href_candidates(anchor)).lower()
    if "linkedin.com/comm/jobs/view/" not in href_blob or "jobcard_body" not in href_blob:
        return False
    if any(token in href_blob for token in LINKEDIN_REJECT_TOKENS):
        return False
    if any(token in href_blob for token in LINKEDIN_WRAPPER_TOKENS) and not _looks_like_title(_clean_text(anchor.get_text(" ", strip=True))):
        return False
    return _looks_like_title(_clean_text(anchor.get_text(" ", strip=True)))


def _looks_like_trademe_card_anchor(anchor: Any) -> bool:
    classes = [str(value).lower() for value in anchor.get("class", [])]
    href_blob = " ".join(_anchor_href_candidates(anchor)).lower()
    return any("responsivetitle" in value for value in classes) and "trademe.co.nz/" in href_blob


def _visible_lines_look_like_job_card(lines: list[str]) -> bool:
    if len(lines) < 3:
        return False
    title = _clean_text(lines[0])
    if not _looks_like_title(title):
        return False
    company_candidates = [line for line in lines[1:4] if not _looks_like_location(line) and not _looks_like_salary(line)]
    return bool(company_candidates) and any(_looks_like_location(line) for line in lines[1:6])


def _looks_like_location(text: str) -> bool:
    lowered = text.lower()
    markers = [
        "auckland",
        "new zealand",
        "wellington",
        "christchurch",
        "canterbury",
        "geraldine",
        "hamilton",
        "tauranga",
        "dunedin",
        "queenstown",
        "otago",
        "waikato",
        "bay of plenty",
        "city",
        "remote",
        "hybrid",
        "onsite",
        "on-site",
        "australia",
        "sydney",
        "melbourne",
    ]
    if any(marker in lowered for marker in markers):
        return True
    if "," in text and len(re.findall(r"[A-Za-z]{2,}", text)) >= 2 and not _looks_like_salary(text):
        return True
    return False


def _looks_like_salary(text: str) -> bool:
    lowered = text.lower()
    return bool(
        re.search(r"\$[\d,]+|\b\d{2,3}\s?k\b", lowered)
        or any(token in lowered for token in ["salary", "per year", "annual", "per hour", "remuneration", "benefit"])
    )


def _is_noise_line(text: str, source: str) -> bool:
    lowered = text.lower()
    generic_noise = [
        "view job",
        "apply now",
        "unsubscribe",
        "job alert",
        "new jobs for",
        "view all jobs",
        "saved searches",
        "see more",
        "show more",
        "promoted",
    ]
    source_noise = {
        "linkedin": ["linkedin", "school alumni", "company alumni", "actively hiring"],
        "seek": [
            "seek",
            "job mail",
            "strong applicant",
            "early applicant",
            "hirer responsiveness",
            "application strength",
        ],
        "trademe": ["trade me jobs", "trade me", "view listing"],
    }
    return any(token in lowered for token in generic_noise + source_noise.get(source, []))


def _clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip(" -\t\r\n")


def _unique_lines(lines: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for line in lines:
        cleaned = _clean_text(line)
        if not cleaned:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        ordered.append(cleaned)
    return ordered


def _strip_tracking_query(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}".rstrip("/")


def _save_debug_email_html(debug_dir: Path, html: str, subject: str, message_id: str | None) -> str:
    debug_dir.mkdir(parents=True, exist_ok=True)
    slug_bits = re.sub(r"[^a-z0-9]+", "-", (subject or "email").lower()).strip("-")[:40] or "email"
    file_name = f"{message_id or 'unknown'}-{slug_bits}.html"
    path = debug_dir / file_name
    path.write_text(html, encoding="utf-8")
    return str(path)
