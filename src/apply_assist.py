from __future__ import annotations

from typing import Any


def build_apply_assist_payload(job: dict[str, Any]) -> dict[str, Any]:
    return {
        "apply_url": str(job.get("url") or ""),
        "auto_submit": False,
        "warning": "Review all fields before submitting. This app does not submit applications automatically.",
        "cover_letter": str(job.get("cover_letter_preview") or ""),
        "profile_summary": str(job.get("cv_selection_reason") or ""),
        "salary_expectation": str(job.get("salary_expectation_text") or ""),
        "selected_cv_path": str(job.get("selected_cv_path") or ""),
    }


def answer_application_questions(job: dict[str, Any], questions_text: str) -> str:
    lines = [line.strip() for line in str(questions_text or "").splitlines() if line.strip()]
    if not lines:
        return ""
    basis = str(job.get("selected_cv_profile") or "selected CV profile")
    answers: list[str] = []
    for line in lines:
        answers.append(f"Question: {line}")
        answers.append(
            f"Suggested answer: Based on {basis}, Example Candidate should answer this truthfully using his strongest examples in Power BI, SQL, Python, stakeholder reporting, automation tooling, and recent project work. Manual verification recommended."
        )
        answers.append("")
    return "\n".join(answers).strip()
