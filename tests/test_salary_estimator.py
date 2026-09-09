from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.salary_estimator import estimate_salary_expectation


class SalaryEstimatorTests(unittest.TestCase):
    def test_digital_data_innovation_analyst_regional_role_returns_mid_90s(self) -> None:
        estimate = estimate_salary_expectation(
            {
                "title": "Digital Data & Innovation Analyst",
                "location": "Geraldine, Canterbury",
                "description": "Power BI, SQL, Python, reporting, dashboards, and stakeholder engagement.",
            }
        )
        self.assertEqual(estimate.salary_expectation_text, "95k")
        self.assertTrue(any("Title is analyst/hybrid" in reason for reason in estimate.reasoning))
        self.assertTrue(any("Regional location lowers" in reason for reason in estimate.reasoning))

    def test_senior_data_engineer_in_auckland_returns_mid_130s(self) -> None:
        estimate = estimate_salary_expectation(
            {
                "title": "Senior Data Engineer",
                "location": "Auckland",
                "description": "SQL, Python, ETL pipelines, Azure, Databricks, cloud data platform ownership.",
            }
        )
        self.assertEqual(estimate.salary_expectation_text, "135k")

    def test_bi_analyst_in_auckland_returns_mid_90s(self) -> None:
        estimate = estimate_salary_expectation(
            {
                "title": "BI Analyst",
                "location": "Auckland",
                "description": "Power BI, SQL reporting, dashboards, stakeholder communication.",
            }
        )
        self.assertEqual(estimate.salary_expectation_text, "95k")

    def test_principal_consultant_returns_higher_band(self) -> None:
        estimate = estimate_salary_expectation(
            {
                "title": "Principal Consultant",
                "location": "Auckland",
                "description": "Leadership, architecture, mentoring, client delivery, modern data platform strategy.",
            }
        )
        self.assertGreaterEqual(estimate.salary_midpoint, 145)

    def test_analyst_clamp_prevents_115k_for_regional_non_senior_role(self) -> None:
        estimate = estimate_salary_expectation(
            {
                "title": "Digital Data & Innovation Analyst",
                "location": "Geraldine, Canterbury",
                "description": "SQL, Power BI, Python, Azure, Fabric, ETL, dashboards, analytics, reporting.",
            }
        )
        self.assertLessEqual(estimate.salary_midpoint, 100)


if __name__ == "__main__":
    unittest.main()
