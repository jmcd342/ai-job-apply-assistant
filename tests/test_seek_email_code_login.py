from __future__ import annotations

import json
import tempfile
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.seek_email_code_login import extract_seek_code_from_text, load_seek_login_email


class SeekEmailCodeLoginTests(unittest.TestCase):
    def test_extract_seek_code_from_subject(self) -> None:
        code = extract_seek_code_from_text("557827 is your code for SEEK")
        self.assertEqual(code, "557827")

    def test_extract_seek_code_from_body_fallback(self) -> None:
        code = extract_seek_code_from_text("", "Verification code Use this code to login to SEEK 812345")
        self.assertEqual(code, "812345")

    def test_load_seek_login_email_accepts_data_dir_path(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            root = Path(temp_dir)
            data_dir = root / "data"
            data_dir.mkdir(parents=True, exist_ok=True)
            (data_dir / "personal_details.json").write_text(
                json.dumps({"email": "candidate@example.com"}),
                encoding="utf-8",
            )
            self.assertEqual(load_seek_login_email(data_dir), "candidate@example.com")


if __name__ == "__main__":
    unittest.main()
