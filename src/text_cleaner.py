from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse


NOISE_PATTERNS = [
    r"\bview job:?\b",
    r"\bschool alumni\b",
    r"\bcompany alumni\b",
    r"\bactively hiring\b",
    r"\bpromoted\b",
    r"\bviewed job reminder\b",
    r"\bjob alert digest\b",
    r"\bsee who is hiring\b",
    r"\bclick to apply\b",
]

URL_PATTERN = re.compile(r"https?://[^\s)>]+", flags=re.IGNORECASE)
LONG_TOKEN_PATTERN = re.compile(r"\b[a-z0-9]{20,}\b", flags=re.IGNORECASE)


def extract_preferred_job_url(text: str, existing_url: str = "") -> str:
    candidates = []
    if existing_url.strip():
        candidates.append(existing_url.strip())
    candidates.extend(URL_PATTERN.findall(text or ""))
    if not candidates:
        return ""

    preferred_domains = ["linkedin.com", "seek.co.nz", "email.s.seek.co.nz", "trademe.co.nz", "trademejobs.co.nz"]
    for domain in preferred_domains:
        for candidate in candidates:
            if domain in candidate.lower():
                return _trim_tracking_suffix(candidate)
    return _trim_tracking_suffix(candidates[0])


def is_valid_job_url(url: str) -> bool:
    if not url.strip():
        return False
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"}:
        return False
    if not parsed.netloc:
        return False
    # Reserved example domain supports offline fixtures without real job links.
    if parsed.hostname == "example.com":
        return True
    return any(
        domain in parsed.netloc.lower()
        for domain in [
            "linkedin.com",
            "seek.co.nz",
            "email.s.seek.co.nz",
            "trademe.co.nz",
            "trademejobs.co.nz",
            "greenhouse.io",
            "lever.co",
            "workable.com",
            "smartrecruiters.com",
        ]
    )


def clean_job_description(
    text: str,
    *,
    title: str,
    company: str,
    location: str,
    source: str,
    existing_url: str = "",
) -> dict[str, Any]:
    raw_text = str(text or "").strip()
    job_url = extract_preferred_job_url(raw_text, existing_url=existing_url)

    cleaned = URL_PATTERN.sub(" ", raw_text)
    cleaned = re.sub(r"\?[^ \n]{12,}", " ", cleaned)
    cleaned = re.sub(r"&[a-z0-9_%=-]{8,}", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\b\d+\s+(school|company)\s+alumni\b", " ", cleaned, flags=re.IGNORECASE)
    for pattern in NOISE_PATTERNS:
        cleaned = re.sub(pattern, " ", cleaned, flags=re.IGNORECASE)
    cleaned = LONG_TOKEN_PATTERN.sub(" ", cleaned)
    cleaned = re.sub(r"\b(?:trk|tracking|ref|utm_[a-z_]+)\b[:=]?[^\s]*", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"[|â€¢Â·]+", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = re.sub(r"\s+([,.;:])", r"\1", cleaned)
    cleaned = cleaned.strip(" -,:;")

    if _description_is_weak(cleaned, title=title, company=company):
        source_label = source.replace("_", " ").title()
        cleaned = (
            f"{source_label} alert for {title} at {company} in {location or 'Auckland'}. "
            "Full description should be reviewed from the job URL."
        )

    return {
        "clean_description": cleaned,
        "job_url": job_url,
        "raw_text": raw_text,
        "was_cleaned": cleaned != raw_text or bool(job_url and job_url != existing_url.strip()),
    }


def _trim_tracking_suffix(url: str) -> str:
    trimmed = url.strip().rstrip(").,;")
    parsed = urlparse(trimmed)
    if not parsed.scheme or not parsed.netloc:
        return trimmed
    tracking_prefixes = ("utm_", "trk", "tracking", "ref", "li_", "mc_", "campaign")
    cleaned_query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=False)
        if not key.lower().startswith(tracking_prefixes)
    ]
    if "seek.co.nz" in parsed.netloc.lower():
        cleaned_query = []
    return urlunparse(parsed._replace(query=urlencode(cleaned_query), fragment=""))


def _description_is_weak(text: str, *, title: str, company: str) -> bool:
    candidate = re.sub(r"\s+", " ", text).strip()
    if len(candidate) < 45:
        return True
    words = re.findall(r"[A-Za-z]{3,}", candidate)
    if len(words) < 8:
        return True
    lowered = candidate.lower()
    useless_phrases = [
        title.lower().strip(),
        company.lower().strip(),
        f"{title.lower().strip()} at {company.lower().strip()}",
    ]
    return lowered in useless_phrases


def is_garbage_description(text: str) -> bool:
    candidate = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(candidate) < 35:
        return True
    words = re.findall(r"[A-Za-z]{3,}", candidate)
    if len(words) < 6:
        return True
    return False
