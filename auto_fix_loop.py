from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
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
    append_jsonl,
    cluster_failure_records,
    has_repeated_unsuccessful_attempt,
    infer_failure_records_from_health_report,
    ledger_attempt_signature,
    read_jsonl,
    utc_now_iso,
)
from src.database import Database  # noqa: E402

DEFAULT_STATE_ORDER = [
    "NORMAL_VERIFY",
    "FIX_ATTEMPT",
    "REVERIFY",
    "AUTH_REQUIRED",
    "WAITING_FOR_USER",
    "RESUME_AFTER_AUTH",
    "PASS",
    "FAIL_LIMIT_REACHED",
]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Autonomous verify -> repair -> reverify loop.")
    parser.add_argument("--db-path", type=Path, default=PROJECT_ROOT / "data" / "job_assistant.db")
    parser.add_argument("--max-hours", type=float, default=12.0)
    parser.add_argument("--target-score", type=int, default=95)
    parser.add_argument("--max-attempts", type=int, default=50)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--allow-code-changes", action="store_true")
    parser.add_argument("--allow-submit", action="store_true")
    parser.add_argument("--output-dir", type=Path, default=PROJECT_ROOT / "data" / "auto_fix_runs")
    parser.add_argument("--sample-size", type=int, default=3)
    return parser.parse_args()


def _run_health_check(args: argparse.Namespace, run_dir: Path) -> dict[str, Any]:
    command = [
        sys.executable,
        "health_check.py",
        "--db-path",
        str(args.db_path),
        "--output-dir",
        str(run_dir / "health_reports"),
        "--sample-size",
        str(max(1, int(args.sample_size))),
    ]
    if args.allow_submit:
        command.append("--allow-submit")
    completed = subprocess.run(
        command,
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    payload: dict[str, Any] = {}
    raw_stdout = str(completed.stdout or "").strip()
    for line in reversed(raw_stdout.splitlines()):
        candidate = line.strip()
        if not candidate.startswith("{"):
            continue
        try:
            payload = json.loads(candidate)
            break
        except Exception:  # noqa: BLE001
            continue
    if not payload:
        payload = {
            "generated_at": utc_now_iso(),
            "state": "FAIL",
            "health_score": 0,
            "target_score": int(args.target_score),
            "components": [],
            "commands_run": [],
            "failure_clusters": [
                {
                    "category": "import_runtime",
                    "count": 1,
                    "examples": [completed.stderr.strip() or completed.stdout.strip() or "health_check.py did not return JSON"],
                    "sources": ["health_check.py"],
                    "latest_timestamp": utc_now_iso(),
                }
            ],
            "auth_blockers": [],
            "raw_stdout": completed.stdout,
            "raw_stderr": completed.stderr,
        }
    payload["health_check_returncode"] = int(completed.returncode)
    payload["health_check_command"] = " ".join(command)
    return payload


def _dominant_cluster(report: dict[str, Any]) -> str:
    clusters = list(report.get("failure_clusters") or [])
    if clusters:
        return str(clusters[0].get("category") or "unknown")
    inferred = cluster_failure_records(infer_failure_records_from_health_report(report))
    return str(inferred[0].get("category") or "unknown") if inferred else "unknown"


def _write_loop_summary(run_dir: Path, payload: dict[str, Any]) -> None:
    (run_dir / "summary.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    lines = ["# Auto Fix Loop Summary", ""]
    lines.append(f"- State: `{payload.get('state', '')}`")
    lines.append(f"- Attempts used: `{payload.get('attempts_used', 0)}`")
    lines.append(f"- Final health score: `{payload.get('final_health_score', 0)}` / `{payload.get('target_score', 0)}`")
    lines.append(f"- Started: `{payload.get('started_at', '')}`")
    lines.append(f"- Finished: `{payload.get('finished_at', '')}`")
    lines.append("")
    blockers = payload.get("remaining_auth_blockers", [])
    if blockers:
        lines.append("## Authorization Blockers")
        lines.append("")
        for blocker in blockers:
            lines.append(
                f"- `{blocker.get('service')}` via `{blocker.get('triggering_workflow')}`: {blocker.get('required_user_action')}"
            )
        lines.append("")
    lines.append("## Remaining Failure Clusters")
    lines.append("")
    for cluster in payload.get("remaining_failure_clusters", []):
        lines.append(f"- `{cluster.get('category')}`: {cluster.get('count', 0)}")
    lines.append("")
    lines.append("## Next Action")
    lines.append("")
    lines.append(f"{payload.get('next_action', '')}")
    (run_dir / "summary.md").write_text("\n".join(lines).strip() + "\n", encoding="utf-8")


def _repair_clear_flags(args: argparse.Namespace, *, dry_run: bool = False) -> dict[str, Any]:
    changed: list[str] = []
    data_dir = args.db_path.resolve().parent
    for file_name in ("cancel_processing.flag", "pause_daily_run.flag"):
        path = data_dir / file_name
        if path.exists():
            if not dry_run:
                path.unlink()
            changed.append(str(path))
    return {
        "name": "clear_stale_flags",
        "command_run": "remove stale pause/cancel flags",
        "files_changed": changed,
        "outcome": "dry_run" if dry_run and changed else "success" if changed else "noop",
        "notes": "Cleared stale processing flags that can block queue progress.",
    }


def _repair_refresh_seek_state(args: argparse.Namespace, *, dry_run: bool = False) -> dict[str, Any]:
    source_path = args.db_path.resolve().parent / "seek_session_state.json"
    changed: list[str] = []
    if source_path.exists():
        backup_dir = args.output_dir / "seek_state_backups"
        if not dry_run:
            backup_dir.mkdir(parents=True, exist_ok=True)
            backup_path = backup_dir / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_seek_session_state.json"
            shutil.copy2(source_path, backup_path)
            changed.append(str(backup_path))
    command = [
        sys.executable,
        "scripts/run_validate_seek_session.py",
        "--db-path",
        str(args.db_path),
    ]
    if dry_run:
        completed_stdout = "Dry run only."
        completed_returncode = 0
    else:
        completed = subprocess.run(
            command,
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        completed_stdout = completed.stdout.strip() or completed.stderr.strip()
        completed_returncode = completed.returncode
    return {
        "name": "refresh_seek_session_state",
        "command_run": " ".join(command),
        "files_changed": changed,
        "outcome": "dry_run" if dry_run else "success" if completed_returncode == 0 else "warning",
        "notes": completed_stdout,
    }


def _repair_db_consistency(args: argparse.Namespace, *, dry_run: bool = False) -> dict[str, Any]:
    database = Database(args.db_path)
    changed_rows = 0
    rows = database.fetch_all(
        """
        SELECT jobs.id, jobs.application_status, applications.status
        FROM jobs
        JOIN applications ON applications.job_id = jobs.id
        WHERE COALESCE(trim(jobs.application_status), '') = ''
        """
    )
    for row in rows:
        status = str(row["status"] or "").strip() or "discovered"
        if not dry_run:
            database.update_application_state(int(row["id"]), status)
        changed_rows += 1
    return {
        "name": "db_queue_consistency",
        "command_run": "normalize blank jobs.application_status from applications.status",
        "files_changed": [str(args.db_path)] if changed_rows else [],
        "outcome": "dry_run" if dry_run and changed_rows else "success" if changed_rows else "noop",
        "notes": f"Updated {changed_rows} rows.",
    }


def _repair_gmail_report_state(args: argparse.Namespace, *, dry_run: bool = False) -> dict[str, Any]:
    database = Database(args.db_path)
    latest = database.fetch_latest_run_log("gmail_ingestion", "auth")
    note = str(latest.get("details") or "") if latest else "No Gmail auth log entry found."
    return {
        "name": "gmail_status_report_only",
        "command_run": "inspect latest gmail_ingestion auth log",
        "files_changed": [],
        "outcome": "noop",
        "notes": note,
    }


def _repair_seek_canary_diagnostics(args: argparse.Namespace, cluster: str, *, dry_run: bool = False) -> dict[str, Any]:
    database = Database(args.db_path)
    jobs = database.search_jobs(status="All", source="seek", min_fit_score=0)
    if jobs.empty:
        return {
            "name": "seek_canary_diagnostics",
            "command_run": "no-op",
            "files_changed": [],
            "outcome": "noop",
            "notes": "No SEEK jobs available for diagnostics.",
        }
    candidate_job_id = int(jobs.iloc[0]["job_id"])
    command = [
        sys.executable,
        "scripts/run_seek_step_diagnostics.py",
        "--job-id",
        str(candidate_job_id),
        "--db-path",
        str(args.db_path),
    ]
    if dry_run:
        completed_stdout = "Dry run only."
        completed_returncode = 0
    else:
        completed = subprocess.run(
            command,
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        completed_stdout = completed.stdout.strip() or completed.stderr.strip()
        completed_returncode = completed.returncode
    return {
        "name": "seek_canary_diagnostics",
        "command_run": " ".join(command),
        "files_changed": [],
        "outcome": "dry_run" if dry_run else "success" if completed_returncode == 0 else "warning",
        "notes": f"{cluster}: {completed_stdout}",
    }


def _repair_import_runtime_report(args: argparse.Namespace, *, dry_run: bool = False) -> dict[str, Any]:
    command = [sys.executable, "-c", "import app; print('import_ok')"]
    if dry_run:
        output = "Dry run only."
        code = 0
    else:
        completed = subprocess.run(
            command,
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        output = completed.stdout.strip() or completed.stderr.strip()
        code = completed.returncode
    return {
        "name": "import_runtime_probe",
        "command_run": " ".join(command),
        "files_changed": [],
        "outcome": "dry_run" if dry_run else "success" if code == 0 else "warning",
        "notes": output,
    }


def _pick_repair_action(cluster: str):
    if cluster in {"import_runtime", "tests"}:
        return _repair_import_runtime_report
    if cluster == "db":
        return _repair_db_consistency
    if cluster == "gmail_pipeline":
        return _repair_gmail_report_state
    if cluster == "auth_seek":
        return _repair_refresh_seek_state
    if cluster.startswith("seek_prepare"):
        return lambda args, dry_run=False: _repair_seek_canary_diagnostics(args, cluster, dry_run=dry_run)
    return _repair_clear_flags


def main() -> int:
    args = _parse_args()
    started_at = utc_now_iso()
    deadline = datetime.now(UTC) + timedelta(hours=max(0.1, float(args.max_hours)))
    run_dir = args.output_dir / datetime.now().strftime("%Y%m%d_%H%M%S_auto_fix_loop")
    run_dir.mkdir(parents=True, exist_ok=True)
    ledger_path = run_dir / "fix_attempt_ledger.jsonl"
    state_history: list[str] = []
    attempts_used = 0

    while True:
        state_history.append("NORMAL_VERIFY" if not state_history else "REVERIFY")
        report = _run_health_check(args, run_dir)
        if int(report.get("health_score", 0)) >= int(args.target_score) and not report.get("auth_blockers"):
            final_payload = {
                "state": "PASS",
                "started_at": started_at,
                "finished_at": utc_now_iso(),
                "attempts_used": attempts_used,
                "target_score": int(args.target_score),
                "final_health_score": int(report.get("health_score", 0)),
                "remaining_failure_clusters": report.get("failure_clusters", []),
                "remaining_auth_blockers": [],
                "next_action": f"System is healthy. Re-run with `{sys.executable} auto_fix_loop.py --max-hours {args.max_hours} --target-score {args.target_score}` when you want another maintenance pass.",
                "state_history": state_history,
                "latest_health_report": report.get("report_paths", {}),
            }
            _write_loop_summary(run_dir, final_payload)
            print(json.dumps(final_payload, indent=2, ensure_ascii=False), flush=True)
            return 0

        if report.get("auth_blockers"):
            state_history.extend(["AUTH_REQUIRED", "WAITING_FOR_USER"])
            final_payload = {
                "state": "AUTH_REQUIRED",
                "started_at": started_at,
                "finished_at": utc_now_iso(),
                "attempts_used": attempts_used,
                "target_score": int(args.target_score),
                "final_health_score": int(report.get("health_score", 0)),
                "remaining_failure_clusters": report.get("failure_clusters", []),
                "remaining_auth_blockers": report.get("auth_blockers", []),
                "next_action": "Complete the authorization step in the generated auth_required report, then rerun this loop. It will resume verification from the blocked area first.",
                "state_history": state_history,
                "latest_health_report": report.get("report_paths", {}),
            }
            _write_loop_summary(run_dir, final_payload)
            print(json.dumps(final_payload, indent=2, ensure_ascii=False), flush=True)
            return 2

        if attempts_used >= int(args.max_attempts) or datetime.now(UTC) >= deadline:
            state_history.append("FAIL_LIMIT_REACHED")
            final_payload = {
                "state": "FAIL_LIMIT_REACHED",
                "started_at": started_at,
                "finished_at": utc_now_iso(),
                "attempts_used": attempts_used,
                "target_score": int(args.target_score),
                "final_health_score": int(report.get("health_score", 0)),
                "remaining_failure_clusters": report.get("failure_clusters", []),
                "remaining_auth_blockers": [],
                "next_action": "Review the remaining failure clusters in the latest health report and decide whether a manual code fix is required.",
                "state_history": state_history,
                "latest_health_report": report.get("report_paths", {}),
            }
            _write_loop_summary(run_dir, final_payload)
            print(json.dumps(final_payload, indent=2, ensure_ascii=False), flush=True)
            return 1

        attempts_used += 1
        state_history.append("FIX_ATTEMPT")
        dominant_cluster = _dominant_cluster(report)
        repair_action = _pick_repair_action(dominant_cluster)
        ledger_rows = read_jsonl(ledger_path)
        preview = repair_action(args, dry_run=bool(args.dry_run))
        repeated = has_repeated_unsuccessful_attempt(
            ledger_rows,
            failure_cluster=dominant_cluster,
            command_run=str(preview.get("command_run") or ""),
            files_changed=list(preview.get("files_changed") or []),
        )
        if repeated:
            final_payload = {
                "state": "FAIL_LIMIT_REACHED",
                "started_at": started_at,
                "finished_at": utc_now_iso(),
                "attempts_used": attempts_used - 1,
                "target_score": int(args.target_score),
                "final_health_score": int(report.get("health_score", 0)),
                "remaining_failure_clusters": report.get("failure_clusters", []),
                "remaining_auth_blockers": [],
                "next_action": f"Stopped to avoid repeating the same unsuccessful fix loop for `{dominant_cluster}`.",
                "state_history": state_history,
                "latest_health_report": report.get("report_paths", {}),
            }
            _write_loop_summary(run_dir, final_payload)
            print(json.dumps(final_payload, indent=2, ensure_ascii=False), flush=True)
            return 1

        if args.dry_run:
            repair_result = preview
        else:
            repair_result = preview

        reverify_report = _run_health_check(args, run_dir)
        signature = ledger_attempt_signature(
            dominant_cluster,
            str(repair_result.get("command_run") or ""),
            list(repair_result.get("files_changed") or []),
        )
        ledger_entry = {
            "timestamp": utc_now_iso(),
            "attempt_number": attempts_used,
            "failure_cluster": dominant_cluster,
            "command_run": str(repair_result.get("command_run") or ""),
            "files_changed": list(repair_result.get("files_changed") or []),
            "health_before": int(report.get("health_score", 0)),
            "health_after": int(reverify_report.get("health_score", 0)),
            "outcome": str(repair_result.get("outcome") or "unknown"),
            "notes": str(repair_result.get("notes") or ""),
            "signature": signature,
            "dry_run": bool(args.dry_run),
        }
        append_jsonl(ledger_path, ledger_entry)


if __name__ == "__main__":
    raise SystemExit(main())
