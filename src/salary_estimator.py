from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SalaryEstimate:
    salary_expectation_text: str
    salary_low: int
    salary_high: int
    salary_midpoint: int
    confidence: str
    reasoning: list[str]


ROLE_BANDS: list[dict[str, Any]] = [
    {
        "label": "Senior Data Engineer",
        "patterns": [r"\bsenior data engineer\b"],
        "low": 130,
        "high": 140,
        "selection": "mid",
    },
    {
        "label": "Data Engineer",
        "patterns": [r"\bdata engineer\b"],
        "low": 140,
        "high": 170,
        "selection": "lower",
    },
    {
        "label": "Analytics Engineer",
        "patterns": [r"\banalytics engineer\b"],
        "low": 140,
        "high": 180,
        "selection": "lower",
    },
    {
        "label": "Senior Business Analyst",
        "patterns": [r"\bsenior business analyst\b", r"\bsenior ba\b"],
        "low": 130,
        "high": 160,
        "selection": "mid",
    },
    {
        "label": "Business Analyst",
        "patterns": [r"\bbusiness analyst\b", r"\bba\b"],
        "low": 100,
        "high": 130,
        "selection": "mid",
    },
    {
        "label": "BI Developer",
        "patterns": [r"\bbi developer\b", r"\bbusiness intelligence developer\b"],
        "low": 120,
        "high": 150,
        "selection": "mid",
    },
    {
        "label": "BI Analyst",
        "patterns": [r"\bbi analyst\b", r"\bbusiness intelligence analyst\b"],
        "low": 90,
        "high": 100,
        "selection": "mid",
    },
    {
        "label": "Data Analyst",
        "patterns": [r"\bdata analyst\b"],
        "low": 100,
        "high": 140,
        "selection": "mid",
    },
    {
        "label": "Data Architect",
        "patterns": [r"\bdata architect\b", r"\barchitect\b"],
        "low": 180,
        "high": 260,
        "selection": "mid",
    },
    {
        "label": "Principal Consultant",
        "patterns": [r"\bprincipal consultant\b"],
        "low": 140,
        "high": 150,
        "selection": "mid",
    },
]


def estimate_salary_expectation(job: dict[str, Any]) -> SalaryEstimate:
    title = str(job.get("enriched_title") or job.get("title") or "").strip()
    location = str(job.get("enriched_location") or job.get("location") or "").strip()
    description = str(job.get("full_description") or job.get("about_the_job") or job.get("description") or "").strip()
    title_lower = title.lower()
    location_lower = location.lower()
    description_lower = description.lower()
    matched = _match_role_band(title_lower)

    if matched is None:
        low, high, midpoint = 100, 130, 115
        reasoning = [
            "Fallback Auckland band used: Business Analyst ~$100k-130k.",
            "No stronger role match was found in the fixed Auckland salary table.",
        ]
        if _looks_like_analyst_hybrid(title_lower, description_lower):
            low, high = 90, 100
            midpoint = _midpoint(low, high)
            reasoning = [
                "Title is analyst/hybrid and aligns better with a mid-90s salary band.",
                "Used an analyst clamp instead of the broader Auckland fallback.",
            ]
        if _is_regional_location(location_lower):
            if not _looks_like_analyst_hybrid(title_lower, description_lower):
                low = max(85, low - 5)
                high = max(low, high - 5)
                midpoint = _midpoint(low, high)
            reasoning.append("Regional location lowers the salary expectation slightly versus Auckland.")
        return SalaryEstimate(
            salary_expectation_text=f"{midpoint}k",
            salary_low=low,
            salary_high=high,
            salary_midpoint=midpoint,
            confidence="medium",
            reasoning=reasoning,
        )

    low = int(matched["low"])
    high = int(matched["high"])
    selection = str(matched["selection"])
    midpoint = _midpoint(low, high)
    chosen = low if selection == "lower" else midpoint
    reasoning = [
        f"Matched fixed Auckland salary table role: {matched['label']} ~${low}k-{high}k.",
        "Engineer roles use the lower end; other matched roles use the middle.",
    ]
    return SalaryEstimate(
        salary_expectation_text=f"{chosen}k",
        salary_low=low,
        salary_high=high,
        salary_midpoint=midpoint,
        confidence="high",
        reasoning=reasoning,
    )


def _looks_like_analyst_hybrid(title_lower: str, description_lower: str) -> bool:
    if "analyst" not in title_lower:
        return False
    return any(
        token in description_lower
        for token in ("power bi", "sql", "python", "reporting", "dashboards", "analytics", "stakeholder")
    )


def _is_regional_location(location_lower: str) -> bool:
    if not location_lower:
        return False
    return "auckland" not in location_lower


def _match_role_band(title_lower: str) -> dict[str, Any] | None:
    for band in ROLE_BANDS:
        for pattern in band["patterns"]:
            if re.search(pattern, title_lower, re.I):
                return band
    return None


def _midpoint(low: int, high: int) -> int:
    return int(round(((low + high) / 2) / 5.0) * 5)
