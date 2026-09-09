from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.bulk_prepare_quick_apply import execute_single_quick_apply_job
from src.database import Database
from src.document_generator import DocumentGenerator


def _parse_bool(value: str) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a single SEEK Apply Assist job up to Review and submit without submitting.")
    parser.add_argument("--job-id", type=int, required=True)
    parser.add_argument("--db-path", type=Path, required=True)
    parser.add_argument("--run-folder", type=Path, required=True)
    parser.add_argument("--visible", default="false")
    parser.add_argument("--no-submit", action="store_true")
    parser.add_argument("--force-new-profile", action="store_true")
    parser.add_argument("--bulk-profile", action="store_true")
    parser.add_argument("--non-interactive", action="store_true")
    args = parser.parse_args()

    database = Database(args.db_path)
    document_generator = DocumentGenerator(database, PROJECT_ROOT)
    result = execute_single_quick_apply_job(
        database,
        document_generator,
        job_id=args.job_id,
        run_dir=args.run_folder,
        visible=_parse_bool(args.visible),
        force_new_profile=args.force_new_profile,
        use_bulk_profile=args.bulk_profile,
        non_interactive=args.non_interactive,
    )
    summary = {
        "job_id": result.job_id,
        "status": result.status,
        "root_failure_reason": result.root_failure_reason,
        "evidence_folder": result.evidence_folder,
    }
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return 0 if result.status == "ready_for_human_review" else 1


if __name__ == "__main__":
    raise SystemExit(main())
