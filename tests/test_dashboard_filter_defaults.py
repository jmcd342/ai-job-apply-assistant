from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app import DESIRED_DEFAULT_STATUSES, STATUS_OPTIONS


class DashboardFilterDefaultsTests(unittest.TestCase):
    def test_default_statuses_are_subset_of_status_options(self) -> None:
        default_statuses = [status for status in DESIRED_DEFAULT_STATUSES if status in STATUS_OPTIONS]
        self.assertEqual(default_statuses, DESIRED_DEFAULT_STATUSES)


if __name__ == "__main__":
    unittest.main()
