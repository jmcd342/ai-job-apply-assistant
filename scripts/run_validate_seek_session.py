from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.seek_apply_assist import validate_seek_session


def _parse_bool(value: str) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate the current SEEK session in a standalone subprocess.")
    parser.add_argument("--db-path", type=Path, required=True)
    parser.add_argument("--visible", default="false")
    parser.add_argument("--bulk-profile", action="store_true")
    args = parser.parse_args()

    result = validate_seek_session(
        args.db_path,
        use_bulk_profile=args.bulk_profile,
        visible=_parse_bool(args.visible),
    )
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0 if bool(result.get("ok")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
