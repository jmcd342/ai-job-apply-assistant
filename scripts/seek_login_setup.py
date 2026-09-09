from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.seek_apply_assist import setup_seek_login_session


def main() -> None:
    parser = argparse.ArgumentParser(description="Open a visible SEEK login session for the automation browser profile.")
    parser.add_argument("--bulk-profile", action="store_true")
    parser.add_argument("--auto-email-code", action="store_true")
    args = parser.parse_args()
    db_path = PROJECT_ROOT / "data" / "job_assistant.db"
    setup_seek_login_session(
        db_path,
        visible=True,
        use_bulk_profile=args.bulk_profile,
        auto_email_code=args.auto_email_code,
    )


if __name__ == "__main__":
    main()
