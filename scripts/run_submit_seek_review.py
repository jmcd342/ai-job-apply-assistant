from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.seek_apply_assist import _submit_seek_review_inprocess


def _parse_bool(value: str) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def main() -> int:
    parser = argparse.ArgumentParser(description="Replay a prepared SEEK review page and submit the application.")
    parser.add_argument("--job-id", type=int, required=True)
    parser.add_argument("--db-path", type=Path, required=True)
    parser.add_argument("--review-url", default="")
    parser.add_argument("--visible", default="false")
    parser.add_argument("--bulk-profile", action="store_true")
    args = parser.parse_args()

    result = _submit_seek_review_inprocess(
        args.job_id,
        args.db_path,
        review_url=str(args.review_url or ""),
        visible=_parse_bool(args.visible),
        use_bulk_profile=args.bulk_profile,
    )
    print(json.dumps(dataclasses.asdict(result), ensure_ascii=False), flush=True)
    return 0 if str(result.status) == "applied" else 1


if __name__ == "__main__":
    raise SystemExit(main())
