from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.apply_assist import build_apply_assist_payload


class ApplyAssistTests(unittest.TestCase):
    def test_apply_assist_never_auto_submits(self) -> None:
        payload = build_apply_assist_payload(
            {
                "url": "https://www.seek.co.nz/job/12345678",
                "cover_letter_preview": "Example cover letter",
                "cv_selection_reason": "Selected BI profile",
            }
        )
        self.assertFalse(payload["auto_submit"])
        self.assertIn("does not submit applications automatically", payload["warning"].lower())


if __name__ == "__main__":
    unittest.main()
