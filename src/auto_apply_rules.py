from __future__ import annotations

from typing import Any

AUTO_APPLY_BLOCKED_COMPANY_PATTERNS: tuple[str, ...] = (
    "bank of china (new zealand)",
    "bank of china new zealand",
)


def is_auto_apply_blocked(job: dict[str, Any] | None) -> bool:
    if not job:
        return False
    company_values = (
        str(job.get("company") or "").strip().lower(),
        str(job.get("enriched_company") or "").strip().lower(),
    )
    if any("industrial and commercial bank of china" in company_value for company_value in company_values if company_value):
        return False
    return any(
        pattern in company_value
        for company_value in company_values
        for pattern in AUTO_APPLY_BLOCKED_COMPANY_PATTERNS
        if company_value
    )


def auto_apply_block_reason(job: dict[str, Any] | None) -> str:
    if not is_auto_apply_blocked(job):
        return ""
    company = str((job or {}).get("enriched_company") or (job or {}).get("company") or "").strip() or "this employer"
    return f"Automatic apply is blocked for {company}. Keep this role for manual review/submission."
