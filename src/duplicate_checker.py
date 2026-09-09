from __future__ import annotations

import re
from difflib import SequenceMatcher
from urllib.parse import parse_qsl, urlparse, urlunparse

from src.database import Database


TRACKING_PARAMS = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "trk", "ref"}
LOCATION_REGION_MARKERS = (
    "auckland",
    "wellington",
    "canterbury",
    "christchurch",
    "otago",
    "queenstown",
    "waikato",
    "hamilton",
    "bay of plenty",
    "tauranga",
    "northland",
    "manawatu",
    "palmerston north",
    "taranaki",
    "nelson",
    "marlborough",
    "southland",
)


def canonicalize_url(url: str | None) -> str | None:
    if not url:
        return None
    parsed = urlparse(url.strip())
    query_items = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=False) if key not in TRACKING_PARAMS]
    cleaned = parsed._replace(query="&".join(f"{key}={value}" for key, value in query_items), fragment="")
    normalised = urlunparse(cleaned).rstrip("/")
    return normalised or None


def fuzzy_similarity(left: str, right: str) -> float:
    return SequenceMatcher(None, left.lower().strip(), right.lower().strip()).ratio()


def normalise_location(location: str | None) -> str:
    text = str(location or "").lower()
    text = re.sub(r"\b(?:hybrid|remote|on[\s-]?site)\b", " ", text)
    text = re.sub(r"[\(\)\[\],/|]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def locations_are_compatible(left: str | None, right: str | None) -> bool:
    left_normalised = normalise_location(left)
    right_normalised = normalise_location(right)
    if not left_normalised or not right_normalised:
        return True
    if left_normalised == right_normalised:
        return True
    if fuzzy_similarity(left_normalised, right_normalised) >= 0.8:
        return True
    return any(marker in left_normalised and marker in right_normalised for marker in LOCATION_REGION_MARKERS)


class DuplicateChecker:
    def __init__(self, database: Database) -> None:
        self.database = database

    def find_duplicate(
        self,
        source: str,
        source_job_id: str | None,
        canonical_url: str | None,
        title: str,
        company: str,
        location: str,
    ) -> int | None:
        if source_job_id:
            row = self.database.fetch_one(
                "SELECT id FROM jobs WHERE source = ? AND source_job_id = ?",
                (source, source_job_id),
            )
            if row:
                return int(row["id"])

        if canonical_url:
            row = self.database.fetch_one("SELECT id FROM jobs WHERE canonical_url = ?", (canonical_url,))
            if row:
                return int(row["id"])

        possible = self.database.fetch_all(
            "SELECT id, title, company, location FROM jobs WHERE lower(company) = lower(?)",
            (company,),
        )
        for row in possible:
            title_match = fuzzy_similarity(title, row["title"])
            if title_match >= 0.88 and locations_are_compatible(location, row["location"]):
                return int(row["id"])

        return None
