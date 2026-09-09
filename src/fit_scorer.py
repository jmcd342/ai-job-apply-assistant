from __future__ import annotations

import re
from dataclasses import dataclass

TARGET_ROLE_FAMILIES = {
    "data_engineering": ["data engineer", "etl developer", "analytics engineer", "data platform", "data pipeline"],
    "analytics_engineering": ["analytics engineer", "elt", "dbt", "semantic model", "metrics layer"],
    "business_intelligence": ["bi analyst", "business intelligence", "reporting analyst", "power bi", "dashboard"],
    "data_science": ["data scientist", "machine learning", "predictive", "modeling"],
    "ai_automation": ["ai", "automation", "llm", "agent", "streamlit", "workflow automation"],
    "product_analytics": ["product analyst", "growth analytics", "funnel", "product analytics"],
}

TARGET_TITLE_WEIGHTS = {
    "data analyst": 24,
    "bi analyst": 24,
    "business intelligence analyst": 24,
    "reporting analyst": 22,
    "insights analyst": 22,
    "data business analyst": 22,
    "analytics engineer": 24,
    "junior data engineer": 21,
    "data engineer": 20,
    "data scientist": 15,
    "automation": 15,
    "business analyst": 14,
}

TECH_SKILLS = {
    "power bi": 8,
    "sql": 8,
    "python": 7,
    "etl": 5,
    "elt": 5,
    "azure": 5,
    "fabric": 5,
    "databricks": 4,
    "spark": 4,
    "snowflake": 4,
    "dashboard": 4,
    "dashboards": 4,
    "data pipeline": 4,
    "data pipelines": 4,
    "automation": 4,
    "streamlit": 3,
    "sqlite": 2,
}

BUSINESS_SKILLS = {
    "stakeholder": 5,
    "stakeholder reporting": 5,
    "reporting": 4,
    "business analysis": 4,
    "requirements": 4,
    "dashboard delivery": 4,
    "operational analytics": 4,
    "asset": 3,
    "facilities": 3,
    "maintenance": 3,
}

CORE_PROFILE_SKILLS = [
    "power bi",
    "sql",
    "python",
    "azure",
    "fabric",
    "etl",
    "databricks",
    "spark",
    "snowflake",
    "dashboard",
    "stakeholder",
    "automation",
]

MISSING_RISK_SKILLS = ["databricks", "spark", "snowflake", "dbt", "kafka", "terraform"]
NEGATIVE_SIGNALS = {
    "internship": 12,
    "training program": 12,
    "sales": 12,
    "cold calling": 12,
    "accounts payable": 10,
    "accounts receivable": 10,
    "bookkeeping": 10,
    "finance-only": 8,
    "fixed term": 4,
    "fixed-term": 4,
    "3 month contract": 5,
    "6 month contract": 4,
}

CORE_SKILLS = [
    "sql",
    "power bi",
    "python",
    "fabric",
    "azure",
    "etl",
    "data pipeline",
    "data pipelines",
    "reporting",
    "stakeholder",
]


@dataclass
class ScoredJob:
    score: int
    enrichment: dict[str, object]


def extract_sections(description: str) -> tuple[str, str]:
    bullet_lines = [line.strip("-* \t") for line in str(description or "").splitlines() if line.strip()]
    responsibilities = [line for line in bullet_lines if any(word in line.lower() for word in ["respons", "manage", "build", "develop", "deliver", "support", "work with"])]
    requirements = [line for line in bullet_lines if any(word in line.lower() for word in ["require", "experience", "skill", "must", "ability", "knowledge"])]
    return "\n".join(responsibilities[:12]), "\n".join(requirements[:12])


def detect_seniority(text: str) -> str:
    lowered = text.lower()
    if any(marker in lowered for marker in ["intern", "internship", "graduate"]):
        return "entry_level"
    if "junior" in lowered:
        return "junior"
    if any(marker in lowered for marker in ["senior", "sr "]):
        return "senior"
    if any(marker in lowered for marker in ["lead", "principal", "manager", "head of"]):
        return "lead"
    return "mid"


def detect_role_family(text: str) -> str:
    lowered = text.lower()
    for family, signals in TARGET_ROLE_FAMILIES.items():
        if any(signal in lowered for signal in signals):
            return family
    return "unknown"


def detect_keywords(text: str) -> list[str]:
    all_signals = set(TARGET_TITLE_WEIGHTS) | set(TECH_SKILLS) | set(BUSINESS_SKILLS)
    return [signal for signal in all_signals if signal in text.lower()]


def score_job(title: str, location: str, salary: str, description: str) -> ScoredJob:
    title_text = str(title or "")
    location_text = str(location or "")
    salary_text = str(salary or "")
    description_text = str(description or "")
    combined = " ".join([title_text, location_text, salary_text, description_text]).lower()

    title_score, title_matches = _weighted_matches(title_text.lower(), TARGET_TITLE_WEIGHTS, cap=25)
    tech_score, tech_matches = _weighted_matches(combined, TECH_SKILLS, cap=25)
    business_score, business_matches = _weighted_matches(combined, BUSINESS_SKILLS, cap=15)
    pathway_score = _pathway_score(combined)
    location_score = 10 if "auckland" in combined else 5 if "new zealand" in combined or "remote" in combined else 0
    fit_adjustment, risk_flags = _fit_adjustments(title_text.lower(), combined, salary_text)
    if "data analyst" in title_text.lower() and "power bi" in combined and "sql" in combined:
        fit_adjustment += 11
    if any(title_signal in title_text.lower() for title_signal in ["bi analyst", "reporting analyst", "insights analyst"]) and "stakeholder" in combined:
        fit_adjustment += 5
    if "analytics engineer" in title_text.lower() and any(skill in combined for skill in ["etl", "elt", "fabric", "azure"]):
        fit_adjustment += 6

    raw_score = title_score + tech_score + business_score + pathway_score + location_score + fit_adjustment
    score = max(0, min(100, raw_score))

    matched_skills = sorted(set([skill for skill in CORE_PROFILE_SKILLS if skill in combined]))
    missing_skills = sorted(set([skill for skill in MISSING_RISK_SKILLS if skill in combined and skill not in matched_skills]))
    role_family = detect_role_family(f"{title_text} {description_text}")
    seniority_level = detect_seniority(f"{title_text} {description_text}")
    fit_category = _fit_category(score)
    opportunity_quality = _opportunity_quality(score, role_family, risk_flags)
    concise_reason = _concise_reason(title_matches, tech_matches, business_matches, role_family, risk_flags, fit_category)

    responsibilities, requirements = extract_sections(description_text)
    enrichment = {
        "keywords": detect_keywords(combined),
        "seniority": seniority_level,
        "seniority_level": seniority_level,
        "role_family": role_family,
        "fit_category": fit_category,
        "matched_skills": matched_skills,
        "missing_skills": missing_skills,
        "opportunity_quality": opportunity_quality,
        "risk_flags": risk_flags,
        "responsibilities": responsibilities,
        "requirements": requirements,
        "concise_reason": concise_reason,
        "debug_notes": {
            "matched_title_signals": title_matches,
            "matched_technical_skills": tech_matches,
            "matched_business_skills": business_matches,
            "pathway_value": {
                "score": pathway_score,
                "signals": [signal for signal in ["etl", "elt", "azure", "fabric", "databricks", "spark", "snowflake"] if signal in combined],
                "summary": _pathway_summary(pathway_score, role_family),
            },
            "location": {"score": location_score, "note": "Auckland-focused" if location_score == 10 else "NZ/remote compatible" if location_score else "No strong location signal"},
            "seniority_salary_contract_fit": {"score": fit_adjustment, "notes": risk_flags},
            "penalties": risk_flags,
        },
        "reasons": [
            f"Role title alignment: {title_score}/25",
            f"Technical skills match: {tech_score}/25",
            f"Business/stakeholder alignment: {business_score}/15",
            f"Data engineering pathway value: {pathway_score}/15",
            f"Location/work arrangement: {location_score}/10",
            f"Seniority/salary/contract fit: {fit_adjustment:+d}/10",
        ],
    }
    return ScoredJob(score=score, enrichment=enrichment)


def explain_score_job(title: str, location: str, salary: str, description: str) -> ScoredJob:
    return score_job(title=title, location=location, salary=salary, description=description)


def _weighted_matches(text: str, weights: dict[str, int], *, cap: int) -> tuple[int, list[str]]:
    total = 0
    matches: list[str] = []
    for signal, weight in weights.items():
        if signal in text:
            matches.append(signal)
            total += weight
    return min(cap, total), matches[:8]


def _pathway_score(text: str) -> int:
    score = 0
    for signal, points in {
        "etl": 4,
        "elt": 4,
        "azure": 4,
        "fabric": 4,
        "databricks": 4,
        "spark": 4,
        "snowflake": 3,
        "data pipeline": 4,
        "data pipelines": 4,
        "analytics engineer": 5,
        "data engineer": 5,
    }.items():
        if signal in text:
            score += points
    return min(15, score)


def _fit_adjustments(title: str, text: str, salary: str) -> tuple[int, list[str]]:
    adjustment = 0
    risks: list[str] = []
    for signal, penalty in NEGATIVE_SIGNALS.items():
        if signal in title or signal in text:
            adjustment -= penalty
            risks.append(signal)
    if "senior" in title and not any(skill in text for skill in ["power bi", "sql", "python", "stakeholder"]):
        adjustment -= 4
        risks.append("seniority mismatch")
    if "lead" in title or "principal" in title:
        adjustment -= 4
        risks.append("leadership-heavy role")
    if salary:
        nums = [int(match.replace(",", "")) for match in re.findall(r"\d[\d,]{3,}", salary)]
        if nums and max(nums) < 65000:
            adjustment -= 3
            risks.append("low salary band")
    return max(-10, min(10, adjustment)), risks[:8]


def _fit_category(score: int) -> str:
    if score >= 80:
        return "strong_fit"
    if score >= 65:
        return "good_fit"
    if score >= 45:
        return "stretch"
    return "weak_fit"


def _opportunity_quality(score: int, role_family: str, risks: list[str]) -> str:
    if score >= 75 and role_family in {"data_engineering", "analytics_engineering", "business_intelligence"} and not risks:
        return "high"
    if score >= 50:
        return "medium"
    return "low"


def _pathway_summary(score: int, role_family: str) -> str:
    if score >= 10:
        return f"Strong pathway toward {role_family.replace('_', ' ')}."
    if score >= 5:
        return f"Useful adjacent pathway toward {role_family.replace('_', ' ')}."
    return "Limited pathway value."


def _concise_reason(
    title_matches: list[str],
    tech_matches: list[str],
    business_matches: list[str],
    role_family: str,
    risks: list[str],
    fit_category: str,
) -> str:
    lead = {
        "strong_fit": "Strong fit:",
        "good_fit": "Good fit:",
        "stretch": "Stretch fit:",
        "weak_fit": "Weak fit:",
    }[fit_category]
    parts = [lead]
    if title_matches:
        parts.append(f"{title_matches[0].title()} alignment.")
    if tech_matches:
        parts.append(f"Technical overlap on {', '.join(tech_matches[:3])}.")
    if business_matches:
        parts.append(f"Business-facing fit through {', '.join(business_matches[:2])}.")
    if role_family != "unknown":
        parts.append(f"Best matched to {role_family.replace('_', ' ')} work.")
    if risks and fit_category != "strong_fit":
        parts.append(f"Watch-outs: {', '.join(risks[:2])}.")
    return " ".join(parts[:5])
