from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.bulk_prepare_quick_apply import find_quick_apply_jobs, run_bulk_quick_apply
from src.database import Database
from src.document_generator import DocumentGenerator

DB_PATH = PROJECT_ROOT / "data" / "job_assistant.db"


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare SEEK Quick Apply jobs up to Review and submit without submitting.")
    parser.add_argument("--max-jobs", type=int, default=10)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    database = Database(DB_PATH)
    document_generator = DocumentGenerator(database, PROJECT_ROOT)

    if args.dry_run:
        jobs = find_quick_apply_jobs(database, max_jobs=args.max_jobs)
        _safe_print_json(jobs)
        return 0

    results = run_bulk_quick_apply(
        database,
        document_generator,
        max_jobs=args.max_jobs,
        dry_run=False,
    )
    _safe_print_json([result.__dict__ for result in results])
    return 0


def _safe_print_json(payload: object) -> None:
    text = json.dumps(payload, indent=2, ensure_ascii=False, default=str)
    try:
        sys.stdout.buffer.write(text.encode("utf-8", errors="replace"))
        sys.stdout.buffer.write(b"\n")
    except Exception:
        print(text.encode("ascii", errors="replace").decode("ascii"))


if __name__ == "__main__":
    raise SystemExit(main())
