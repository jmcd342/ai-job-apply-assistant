from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.cv_selector import select_cv_profile


class CVSelectorTests(unittest.TestCase):
    def test_selects_bi_profile_for_power_bi_reporting_role(self) -> None:
        selection = select_cv_profile(
            config_path=PROJECT_ROOT / "data" / "cv_profiles.example.json",
            title="BI Analyst",
            description="Power BI, SQL, dashboard delivery, and stakeholder reporting.",
            role_family="business_intelligence",
            matched_skills=["power bi", "sql", "dashboard", "stakeholder"],
        )
        self.assertEqual("analytics_engineer_bi_cv", selection.profile_name)


if __name__ == "__main__":
    unittest.main()
