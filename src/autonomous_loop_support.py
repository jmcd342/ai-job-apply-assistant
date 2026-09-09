from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

HEALTH_COMPONENT_WEIGHTS: dict[str, int] = {
    "repo_import_health": 10,
    "core_db_health": 10,
    "gmail_auth_connectivity": 15,
    "ingestion_pipeline": 10,
    "enrichment_tests": 10,
    "document_generation": 10,
    "seek_session_health": 15,
    "seek_prepare_canary": 15,
    "follow_up_pipeline": 5,
}


@dataclass(slots=True)
class AuthBlocker:
    service: str
    triggering_workflow: str
    required_user_action: str
    command_or_link: str
    safe_to_resume_after_completion: bool = True
    evidence: str = ""
    authorization_needed: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ComponentResult:
    name: str
    weight: int
    passed: bool
    summary: str
    command: str = ""
    details: dict[str, Any] = field(default_factory=dict)
    auth_blocker: AuthBlocker | None = None
    fatal: bool = False
    score: int = 0

    def __post_init__(self) -> None:
        self.score = self.weight if self.passed else 0

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        if self.auth_blocker is not None:
            payload["auth_blocker"] = self.auth_blocker.to_dict()
        return payload


def utc_now_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def build_component_result(
    name: str,
    *,
    passed: bool,
    summary: str,
    command: str = "",
    details: dict[str, Any] | None = None,
    auth_blocker: AuthBlocker | None = None,
    fatal: bool = False,
) -> ComponentResult:
    return ComponentResult(
        name=name,
        weight=HEALTH_COMPONENT_WEIGHTS[name],
        passed=passed,
        summary=summary,
        command=command,
        details=dict(details or {}),
        auth_blocker=auth_blocker,
        fatal=fatal,
    )


def compute_health_score(components: list[ComponentResult]) -> int:
    return max(0, min(100, sum(component.score for component in components)))


def classify_auth_blocker(
    *,
    service: str,
    triggering_workflow: str,
    text: str,
    command_or_link: str = "",
) -> AuthBlocker | None:
    lowered = str(text or "").strip().lower()
    if not lowered:
        return None

    if service.lower() == "gmail":
        if any(
            token in lowered
            for token in (
                "oauth token expired",
                "token expired",
                "token revoked",
                "reconnect gmail",
                "invalid_grant",
                "consent",
                "interactive authentication",
            )
        ):
            return AuthBlocker(
                service="Gmail",
                triggering_workflow=triggering_workflow,
                required_user_action="Reconnect Gmail OAuth and complete the consent/login flow in the browser.",
                command_or_link=command_or_link or "python app.py",
                evidence=text.strip(),
            )

    if service.lower() == "seek":
        if "captcha" in lowered:
            return AuthBlocker(
                service="SEEK",
                triggering_workflow=triggering_workflow,
                required_user_action="Open the SEEK browser session and complete the CAPTCHA or human verification prompt.",
                command_or_link=command_or_link or "python scripts/run_validate_seek_session.py --db-path data/job_assistant.db --visible true",
                evidence=text.strip(),
            )
        if any(token in lowered for token in ("verification required", "seek verification", "verification code", "2fa", "mfa")):
            return AuthBlocker(
                service="SEEK",
                triggering_workflow=triggering_workflow,
                required_user_action="Open SEEK, enter the verification code or complete the MFA step, then rerun validation.",
                command_or_link=command_or_link or "python scripts/run_validate_seek_session.py --db-path data/job_assistant.db --visible true",
                evidence=text.strip(),
            )
        if any(token in lowered for token in ("login required", "seek login", "not logged in", "session expired")):
            return AuthBlocker(
                service="SEEK",
                triggering_workflow=triggering_workflow,
                required_user_action="Sign in to SEEK in the shared browser profile and validate the session again.",
                command_or_link=command_or_link or "python scripts/run_validate_seek_session.py --db-path data/job_assistant.db --visible true",
                evidence=text.strip(),
            )
        if any(token in lowered for token in ("browser profile locked", "profile is locked", "another chromium")):
            return AuthBlocker(
                service="SEEK",
                triggering_workflow=triggering_workflow,
                required_user_action="Close the other Chrome/Chromium windows using the SEEK automation profile, then rerun validation.",
                command_or_link=command_or_link or "python scripts/run_validate_seek_session.py --db-path data/job_assistant.db --visible true",
                evidence=text.strip(),
            )

    return None


def classify_failure_category(text: str) -> str:
    lowered = str(text or "").lower()
    if not lowered:
        return "unknown"
    if "oauth" in lowered or "token expired" in lowered or "reconnect gmail" in lowered:
        return "auth_gmail"
    if any(token in lowered for token in ("seek login", "verification required", "captcha", "browser profile locked", "mfa")):
        return "auth_seek"
    if "importerror" in lowered or "modulenotfounderror" in lowered or "nameerror" in lowered:
        return "import_runtime"
    if "assert" in lowered or "unittest" in lowered or "failed (" in lowered or "traceback" in lowered:
        return "tests"
    if "database" in lowered or "sqlite" in lowered or "no such table" in lowered:
        return "db"
    if "resume" in lowered or "documents" in lowered:
        return "seek_prepare_resume"
    if "questionnaire" in lowered or "employer questions" in lowered or "role-requirements" in lowered:
        return "seek_prepare_questionnaire"
    if "quick apply" in lowered and "could not find" in lowered:
        return "seek_prepare_quick_apply"
    if "timed out" in lowered or "timeout" in lowered:
        return "seek_prepare_timeout"
    if "gmail" in lowered or "ingestion" in lowered:
        return "gmail_pipeline"
    if "followup" in lowered or "follow-up" in lowered:
        return "follow_up"
    if "seek" in lowered:
        return "seek_prepare_other"
    return "other"


def cluster_failure_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for record in records:
        category = str(record.get("category") or classify_failure_category(str(record.get("message") or ""))).strip() or "unknown"
        bucket = grouped.setdefault(
            category,
            {
                "category": category,
                "count": 0,
                "examples": [],
                "sources": set(),
                "latest_timestamp": "",
            },
        )
        bucket["count"] += 1
        message = str(record.get("message") or "").strip()
        source = str(record.get("source") or "").strip()
        timestamp = str(record.get("timestamp") or "").strip()
        if message and message not in bucket["examples"] and len(bucket["examples"]) < 5:
            bucket["examples"].append(message)
        if source:
            bucket["sources"].add(source)
        if timestamp and timestamp > bucket["latest_timestamp"]:
            bucket["latest_timestamp"] = timestamp
    results: list[dict[str, Any]] = []
    for item in grouped.values():
        results.append(
            {
                "category": item["category"],
                "count": item["count"],
                "examples": item["examples"],
                "sources": sorted(item["sources"]),
                "latest_timestamp": item["latest_timestamp"],
            }
        )
    results.sort(key=lambda row: (-int(row["count"]), str(row["category"])))
    return results


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rows.append(json.loads(line))
    return rows


def ledger_attempt_signature(failure_cluster: str, command_run: str, files_changed: list[str]) -> str:
    payload = json.dumps(
        {
            "failure_cluster": failure_cluster,
            "command_run": command_run,
            "files_changed": sorted(files_changed),
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def has_repeated_unsuccessful_attempt(
    ledger_rows: list[dict[str, Any]],
    *,
    failure_cluster: str,
    command_run: str,
    files_changed: list[str],
) -> bool:
    signature = ledger_attempt_signature(failure_cluster, command_run, files_changed)
    for row in reversed(ledger_rows):
        if row.get("signature") != signature:
            continue
        health_before = int(row.get("health_before") or 0)
        health_after = int(row.get("health_after") or 0)
        if health_after <= health_before:
            return True
    return False


def auth_blockers_from_components(components: list[ComponentResult]) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    for component in components:
        if component.auth_blocker is not None:
            blockers.append(component.auth_blocker.to_dict())
    return blockers


def infer_failure_records_from_health_report(report: dict[str, Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for component in report.get("components", []):
        if component.get("passed"):
            continue
        summary = str(component.get("summary") or "").strip()
        name = str(component.get("name") or "").strip()
        records.append(
            {
                "source": name,
                "message": summary,
                "timestamp": report.get("generated_at", ""),
                "category": classify_failure_category(summary),
            }
        )
    for cluster in report.get("failure_clusters", []):
        for example in cluster.get("examples", []):
            records.append(
                {
                    "source": ",".join(cluster.get("sources", [])),
                    "message": str(example),
                    "timestamp": str(cluster.get("latest_timestamp") or report.get("generated_at") or ""),
                    "category": str(cluster.get("category") or ""),
                }
            )
    return records


def render_auth_blocker_markdown(blockers: list[dict[str, Any]]) -> str:
    lines = ["# Authorization Required", ""]
    for blocker in blockers:
        lines.append(f"## {blocker['service']}")
        lines.append("")
        lines.append(f"- authorization_needed: `{str(blocker.get('authorization_needed', True)).lower()}`")
        lines.append(f"- service: `{blocker['service']}`")
        lines.append(f"- triggering_workflow: `{blocker['triggering_workflow']}`")
        lines.append(f"- required_user_action: {blocker['required_user_action']}")
        lines.append(f"- command_or_link: `{blocker['command_or_link']}`")
        lines.append(f"- safe_to_resume_after_completion: `{str(bool(blocker.get('safe_to_resume_after_completion', True))).lower()}`")
        if blocker.get("evidence"):
            lines.append(f"- evidence: `{blocker['evidence']}`")
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def render_health_report_markdown(report: dict[str, Any]) -> str:
    lines = ["# Autonomous Health Report", ""]
    lines.append(f"- Generated: `{report.get('generated_at', '')}`")
    lines.append(f"- State: `{report.get('state', '')}`")
    lines.append(f"- Health score: `{report.get('health_score', 0)}` / `{report.get('target_score', 0)}`")
    lines.append("")
    lines.append("## Components")
    lines.append("")
    for component in report.get("components", []):
        marker = "PASS" if component.get("passed") else "FAIL"
        lines.append(
            f"- `{component.get('name')}` [{marker}] score `{component.get('score', 0)}/{component.get('weight', 0)}`: "
            f"{component.get('summary', '')}"
        )
        if component.get("command"):
            lines.append(f"  Command: `{component['command']}`")
    lines.append("")
    lines.append("## Failure Clusters")
    lines.append("")
    for cluster in report.get("failure_clusters", []):
        lines.append(f"- `{cluster.get('category')}`: {cluster.get('count', 0)}")
        for example in cluster.get("examples", []):
            lines.append(f"  Example: {example}")
    blockers = report.get("auth_blockers", [])
    if blockers:
        lines.append("")
        lines.append("## Authorization Blockers")
        lines.append("")
        for blocker in blockers:
            lines.append(
                f"- `{blocker.get('service')}` via `{blocker.get('triggering_workflow')}`: "
                f"{blocker.get('required_user_action')}"
            )
    lines.append("")
    lines.append("## Commands Run")
    lines.append("")
    for command_row in report.get("commands_run", []):
        lines.append(
            f"- `{command_row.get('label', '')}` exit={command_row.get('returncode')} "
            f"duration={command_row.get('duration_seconds', 0)}s"
        )
    return "\n".join(lines).strip() + "\n"


def sanitize_filename(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "").strip())
    return cleaned.strip("._") or "report"
