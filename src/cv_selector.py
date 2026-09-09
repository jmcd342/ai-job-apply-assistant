from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass
class CVSelection:
    profile_name: str
    file_path: str
    reason: str
    score: int


def load_cv_profiles(config_path: Path) -> list[dict]:
    if not config_path.exists():
        return []
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    return list(payload.get("profiles", []))


def select_cv_profile(
    *,
    config_path: Path,
    title: str,
    description: str,
    role_family: str,
    matched_skills: list[str],
) -> CVSelection:
    profiles = load_cv_profiles(config_path)
    combined = f"{title}\n{description}".lower()
    best: CVSelection | None = None
    for profile in profiles:
        score = 0
        target_roles = [str(item).lower() for item in profile.get("target_roles", [])]
        keywords = [str(item).lower() for item in profile.get("keywords", [])]
        if any(role in combined for role in target_roles):
            score += 15
        if role_family and role_family.replace("_", " ") in " ".join(target_roles):
            score += 10
        score += sum(2 for skill in matched_skills if skill.lower() in keywords)
        if score <= 0:
            continue
        reason = (
            f"Selected {profile.get('profile_name')} because it aligns with "
            f"{role_family.replace('_', ' ') if role_family else 'the role'} and signals like "
            f"{', '.join([skill for skill in matched_skills[:3]]) or ', '.join(keywords[:3])}."
        )
        selection = CVSelection(
            profile_name=str(profile.get("profile_name") or "Unknown"),
            file_path=str(profile.get("file_path") or ""),
            reason=reason,
            score=score,
        )
        if best is None or selection.score > best.score:
            best = selection

    if best:
        return best

    fallback = profiles[0] if profiles else {}
    return CVSelection(
        profile_name=str(fallback.get("profile_name") or "analytics_engineer_bi_cv"),
        file_path=str(fallback.get("file_path") or ""),
        reason="Defaulted to the broadest analytics and BI profile because no stronger profile signal was detected.",
        score=0,
    )
