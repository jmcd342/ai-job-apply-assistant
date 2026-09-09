from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from src.autonomous_loop_support import (  # noqa: E402
    auth_blockers_from_components,
    build_component_result,
    classify_auth_blocker,
    classify_failure_category,
    cluster_failure_records,
    compute_health_score,
    render_auth_blocker_markdown,
    render_health_report_markdown,
    sanitize_filename,
    utc_now_iso,
)
from src.database import Database  # noqa: E402
from src.document_generator import DocumentGenerator  # noqa: E402
from src.gmail_followup import GmailFollowupManager  # noqa: E402
from src.gmail_ingestor import GmailIngestor  # noqa: E402
from src.job_importer import JobImporter  # noqa: E402


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the autonomous health verification suite.")
    parser.add_argument("--db-path", type=Path, default=PROJECT_ROOT / "data" / "job_assistant.db")
    parser.add_argument("--output-dir", type=Path, default=PROJECT_ROOT / "data" / "health_reports")
    parser.add_argument("--sample-size", type=int, default=3)
    parser.add_argument("--allow-submit", action="store_true", help="Reserved for dangerous/manual flows. Disabled by default.")
    return parser.parse_args()


def _run_command(label: str, args: list[str], cwd: Path) -> dict[str, Any]:
    started = time.perf_counter()
    completed = subprocess.run(
        args,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    duration = round(time.perf_counter() - started, 2)
    return {
        "label": label,
        "command": " ".join(args),
        "returncode": int(completed.returncode),
        "stdout": completed.stdout.strip(),
        "stderr": completed.stderr.strip(),
        "duration_seconds": duration,
    }


def _truncate(text: str, limit: int = 500) -> str:
    value = str(text or "").strip()
    if len(value) <= limit:
        return value
    return value[: limit - 3] + "..."


def _latest_json_file(root: Path, pattern: str) -> Path | None:
    if not root.exists():
        return None
    matches = sorted(root.glob(pattern), key=lambda item: item.stat().st_mtime, reverse=True)
    return matches[0] if matches else None


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _extract_json_payload(text: str) -> dict[str, Any]:
    raw = str(text or "").strip()
    if not raw:
        return {}
    for line in reversed(raw.splitlines()):
        candidate = line.strip()
        if not candidate.startswith("{"):
            continue
        try:
            return json.loads(candidate)
        except Exception:  # noqa: BLE001
            continue
    return {}


def _collect_recent_failure_records(database: Database) -> list[dict[str, Any]]:
    logs = database.fetch_run_logs(limit=200)
    if logs.empty:
        return []
    records: list[dict[str, Any]] = []
    for _, row in logs.iterrows():
        status = str(row.get("status") or "").strip().lower()
        if status not in {"error", "warning", "failed"}:
            continue
        message = f"{row.get('run_type', '')} {row.get('action', '')} {row.get('details', '')}".strip()
        records.append(
            {
                "source": str(row.get("run_type") or ""),
                "message": message,
                "timestamp": str(row.get("created_at") or ""),
                "category": classify_failure_category(message),
            }
        )
    return records


def _repo_import_component(commands_run: list[dict[str, Any]]) -> Any:
    commands = [
        _run_command(
            "repo_import_health",
            [sys.executable, "-c", "import app; print('import_ok')"],
            PROJECT_ROOT,
        ),
        _run_command(
            "full_unittest_suite",
            [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py"],
            PROJECT_ROOT,
        ),
    ]
    commands_run.extend(commands)
    failed = [item for item in commands if item["returncode"] != 0]
    passed = not failed and "import_ok" in commands[0]["stdout"]
    summary = "Application import and full unittest suite passed." if passed else _truncate(failed[0]["stderr"] or failed[0]["stdout"] or "Import/tests failed.")
    return build_component_result(
        "repo_import_health",
        passed=passed,
        summary=summary,
        command=" && ".join(item["command"] for item in commands),
        details={"commands": commands},
        fatal=not passed,
    )


def _core_db_component(database: Database, db_path: Path) -> Any:
    required_tables = [
        "jobs",
        "applications",
        "generated_documents",
        "run_logs",
        "processed_email_messages",
        "application_email_events",
    ]
    if not db_path.exists():
        return build_component_result(
            "core_db_health",
            passed=False,
            summary=f"Database file is missing: {db_path}",
            details={"db_path": str(db_path)},
            fatal=True,
        )
    try:
        snapshot = database.get_jobs_debug_snapshot()
        with sqlite3.connect(db_path) as connection:
            table_rows = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            ).fetchall()
        table_names = {str(row[0]) for row in table_rows}
        missing = [table for table in required_tables if table not in table_names]
        passed = not missing and bool(snapshot.get("db_exists"))
        summary = (
            f"Database healthy with {snapshot.get('total_jobs', 0)} jobs and {snapshot.get('total_applications', 0)} applications."
            if passed
            else f"Missing required tables: {', '.join(missing)}"
        )
        return build_component_result(
            "core_db_health",
            passed=passed,
            summary=summary,
            details={
                "snapshot": snapshot,
                "table_names": sorted(table_names),
                "missing_tables": missing,
            },
            fatal=not passed,
        )
    except Exception as exc:  # noqa: BLE001
        return build_component_result(
            "core_db_health",
            passed=False,
            summary=f"Database check failed: {exc}",
            details={"db_path": str(db_path)},
            fatal=True,
        )


def _gmail_component(database: Database) -> Any:
    importer = JobImporter(database)
    generator = DocumentGenerator(database, PROJECT_ROOT)
    gmail_ingestor = GmailIngestor(database, importer, generator, PROJECT_ROOT)
    status = gmail_ingestor.get_connection_status()
    passed = bool(status.get("configured")) and bool(status.get("token_exists")) and bool(status.get("authenticated"))
    summary = "Gmail credentials and token look healthy." if passed else str(status.get("error") or "Gmail is not authenticated.")
    auth_blocker = classify_auth_blocker(
        service="gmail",
        triggering_workflow="gmail_sync",
        text=summary,
        command_or_link="python app.py",
    )
    return build_component_result(
        "gmail_auth_connectivity",
        passed=passed,
        summary=summary,
        details=status,
        auth_blocker=auth_blocker,
    )


def _ingestion_component(commands_run: list[dict[str, Any]]) -> Any:
    commands = [
        _run_command(
            "ingestion_smoke_gmail_ingestor",
            [sys.executable, "tests/smoke_gmail_ingestor.py"],
            PROJECT_ROOT,
        ),
        _run_command(
            "ingestion_unit_tests",
            [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", "test_gmail_*.py"],
            PROJECT_ROOT,
        ),
    ]
    commands_run.extend(commands)
    failed = [item for item in commands if item["returncode"] != 0]
    passed = not failed
    summary = "Gmail ingestion smoke tests passed." if passed else _truncate((failed[0]["stderr"] or failed[0]["stdout"] or "Ingestion tests failed."))
    return build_component_result(
        "ingestion_pipeline",
        passed=passed,
        summary=summary,
        command=" && ".join(item["command"] for item in commands),
        details={"commands": commands},
    )


def _enrichment_component(commands_run: list[dict[str, Any]]) -> Any:
    result = _run_command(
        "enrichment_tests",
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", "test_job_enricher.py"],
        PROJECT_ROOT,
    )
    commands_run.append(result)
    passed = result["returncode"] == 0
    summary = "Job enrichment tests passed." if passed else _truncate(result["stderr"] or result["stdout"] or "Enrichment tests failed.")
    return build_component_result(
        "enrichment_tests",
        passed=passed,
        summary=summary,
        command=result["command"],
        details=result,
    )


def _document_generation_component(commands_run: list[dict[str, Any]]) -> Any:
    commands = [
        _run_command(
            "document_generation_unit_tests",
            [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", "test_cover_letter_generation.py"],
            PROJECT_ROOT,
        ),
        _run_command(
            "document_generation_smoke",
            [sys.executable, "tests/smoke_drafting_workflow.py"],
            PROJECT_ROOT,
        ),
    ]
    commands_run.extend(commands)
    failed = [item for item in commands if item["returncode"] != 0]
    passed = not failed
    summary = "Document generation checks passed." if passed else _truncate((failed[0]["stderr"] or failed[0]["stdout"] or "Document generation failed."))
    return build_component_result(
        "document_generation",
        passed=passed,
        summary=summary,
        command=" && ".join(item["command"] for item in commands),
        details={"commands": commands},
    )


def _seek_session_component(commands_run: list[dict[str, Any]], db_path: Path) -> Any:
    result = _run_command(
        "seek_session_health",
        [sys.executable, "scripts/run_validate_seek_session.py", "--db-path", str(db_path)],
        PROJECT_ROOT,
    )
    commands_run.append(result)
    payload = _extract_json_payload(result["stdout"])
    if not payload:
        payload = {"ok": False, "state": "unknown", "reason": result["stderr"] or result["stdout"]}
    passed = result["returncode"] == 0 and bool(payload.get("ok"))
    summary = "SEEK session valid." if passed else str(payload.get("reason") or payload.get("state") or "SEEK session validation failed.")
    auth_blocker = classify_auth_blocker(
        service="seek",
        triggering_workflow="seek_session_validation",
        text=f"{payload.get('state', '')} {summary}",
        command_or_link="python scripts/run_validate_seek_session.py --db-path data/job_assistant.db --visible true",
    )
    return build_component_result(
        "seek_session_health",
        passed=passed,
        summary=summary,
        command=result["command"],
        details={"command_result": result, "validation": payload},
        auth_blocker=auth_blocker,
    )


def _seek_prepare_component(commands_run: list[dict[str, Any]], db_path: Path, sample_size: int) -> Any:
    diagnostics_root = db_path.resolve().parent / "diagnostic_runs"
    before_latest = _latest_json_file(diagnostics_root, "*_prepare_diagnostics/summary.json")
    result = _run_command(
        "seek_prepare_canary",
        [
            sys.executable,
            "scripts/run_prepare_diagnostics.py",
            "--db-path",
            str(db_path),
            "--bucket",
            "mixed",
            "--sample-size",
            str(max(1, sample_size)),
        ],
        PROJECT_ROOT,
    )
    commands_run.append(result)
    after_latest = _latest_json_file(diagnostics_root, "*_prepare_diagnostics/summary.json")
    summary_payload: dict[str, Any] = {}
    if after_latest and after_latest != before_latest:
        try:
            summary_payload = _read_json(after_latest)
        except Exception:  # noqa: BLE001
            summary_payload = {}
    passed = False
    summary = "Prepare diagnostics did not produce a report."
    auth_blocker = None
    if result["returncode"] == 0 and summary_payload:
        categories = dict(summary_payload.get("category_counts") or {})
        status_counts = dict(summary_payload.get("status_counts") or {})
        auth_text = f"{summary_payload.get('results', [])} {categories}"
        auth_blocker = classify_auth_blocker(
            service="seek",
            triggering_workflow="seek_prepare_canary",
            text=auth_text,
            command_or_link="python scripts/run_prepare_diagnostics.py --db-path data/job_assistant.db --bucket mixed --sample-size 3",
        )
        if auth_blocker is None and int(categories.get("login", 0)) > 0:
            auth_blocker = classify_auth_blocker(
                service="seek",
                triggering_workflow="seek_prepare_canary",
                text="login required",
                command_or_link="python scripts/run_prepare_diagnostics.py --db-path data/job_assistant.db --bucket mixed --sample-size 3",
            )
        if auth_blocker is None and int(categories.get("seek_verification", 0)) > 0:
            auth_blocker = classify_auth_blocker(
                service="seek",
                triggering_workflow="seek_prepare_canary",
                text="verification required",
                command_or_link="python scripts/run_prepare_diagnostics.py --db-path data/job_assistant.db --bucket mixed --sample-size 3",
            )
        if auth_blocker is None and int(categories.get("captcha", 0)) > 0:
            auth_blocker = classify_auth_blocker(
                service="seek",
                triggering_workflow="seek_prepare_canary",
                text="captcha required",
                command_or_link="python scripts/run_prepare_diagnostics.py --db-path data/job_assistant.db --bucket mixed --sample-size 3",
            )
        blocking_categories = {"login", "seek_verification", "captcha"}
        blocking_detected = any(int(categories.get(key, 0)) > 0 for key in blocking_categories)
        passed = bool(status_counts.get("ready_for_human_review")) and not blocking_detected
        summary = (
            f"Prepare canary passed with {status_counts.get('ready_for_human_review', 0)} ready job(s)."
            if passed
            else f"Prepare canary categories: {categories}"
        )
    else:
        summary = _truncate(result["stderr"] or result["stdout"] or summary)
        auth_blocker = classify_auth_blocker(
            service="seek",
            triggering_workflow="seek_prepare_canary",
            text=summary,
            command_or_link="python scripts/run_prepare_diagnostics.py --db-path data/job_assistant.db --bucket mixed --sample-size 3",
        )
    return build_component_result(
        "seek_prepare_canary",
        passed=passed,
        summary=summary,
        command=result["command"],
        details={"command_result": result, "diagnostic_summary": summary_payload, "report_path": str(after_latest) if after_latest else ""},
        auth_blocker=auth_blocker,
    )


def _follow_up_component(database: Database) -> Any:
    manager = GmailFollowupManager(database, PROJECT_ROOT)
    status = {
        "credentials_exists": manager.credentials_path.exists(),
        "token_exists": manager.token_path.exists(),
        "recent_events": len(database.list_application_email_events(limit=25)),
    }
    passed = bool(status["credentials_exists"]) and bool(status["token_exists"])
    summary = (
        f"Follow-up pipeline configured with {status['recent_events']} tracked event(s)."
        if passed
        else "Follow-up Gmail credentials are missing."
    )
    auth_blocker = classify_auth_blocker(
        service="gmail",
        triggering_workflow="follow_up_pipeline",
        text=summary,
        command_or_link="python app.py",
    )
    return build_component_result(
        "follow_up_pipeline",
        passed=passed,
        summary=summary,
        details=status,
        auth_blocker=auth_blocker,
    )


def _write_reports(report: dict[str, Any], output_dir: Path) -> dict[str, str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    report_id = sanitize_filename(report["generated_at"].replace(":", "").replace("Z", ""))
    report_dir = output_dir / report_id
    report_dir.mkdir(parents=True, exist_ok=True)
    json_path = report_dir / "health_report.json"
    markdown_path = report_dir / "health_report.md"
    json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    markdown_path.write_text(render_health_report_markdown(report), encoding="utf-8")
    paths = {"json": str(json_path), "markdown": str(markdown_path)}
    blockers = report.get("auth_blockers", [])
    if blockers:
        auth_json_path = report_dir / "auth_required.json"
        auth_md_path = report_dir / "auth_required.md"
        auth_payload = {"authorization_needed": True, "blockers": blockers}
        auth_json_path.write_text(json.dumps(auth_payload, indent=2, ensure_ascii=False), encoding="utf-8")
        auth_md_path.write_text(render_auth_blocker_markdown(blockers), encoding="utf-8")
        paths["auth_json"] = str(auth_json_path)
        paths["auth_markdown"] = str(auth_md_path)
    return paths


def main() -> int:
    args = _parse_args()
    database = Database(args.db_path)
    commands_run: list[dict[str, Any]] = []
    components = [
        _repo_import_component(commands_run),
        _core_db_component(database, args.db_path),
        _gmail_component(database),
        _ingestion_component(commands_run),
        _enrichment_component(commands_run),
        _document_generation_component(commands_run),
        _seek_session_component(commands_run, args.db_path),
        _seek_prepare_component(commands_run, args.db_path, args.sample_size),
        _follow_up_component(database),
    ]
    failure_records = _collect_recent_failure_records(database)
    for component in components:
        if component.passed:
            continue
        failure_records.append(
            {
                "source": component.name,
                "message": component.summary,
                "timestamp": utc_now_iso(),
                "category": classify_failure_category(component.summary),
            }
        )
    failure_clusters = cluster_failure_records(failure_records)
    auth_blockers = auth_blockers_from_components(components)
    health_score = compute_health_score(components)
    state = "PASS" if health_score >= 95 and not auth_blockers else "AUTH_REQUIRED" if auth_blockers else "FAIL"
    report = {
        "generated_at": utc_now_iso(),
        "state": state,
        "health_score": health_score,
        "target_score": 95,
        "allow_submit": bool(args.allow_submit),
        "db_path": str(args.db_path),
        "components": [component.to_dict() for component in components],
        "commands_run": commands_run,
        "failure_clusters": failure_clusters,
        "auth_blockers": auth_blockers,
    }
    report["report_paths"] = _write_reports(report, args.output_dir)
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return 0 if state == "PASS" else 2 if state == "AUTH_REQUIRED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
