from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.fit_scorer import score_job


class FitScorerTests(unittest.TestCase):
    def test_senior_data_analyst_scores_high(self) -> None:
        scored = score_job(
            title="Senior Data Analyst",
            location="Auckland",
            salary="$110,000",
            description=(
                "Lead Power BI dashboard delivery, SQL analysis, stakeholder reporting, operational reporting, "
                "requirements gathering, and business-facing analytics delivery."
            ),
        )
        self.assertGreaterEqual(scored.score, 80)

    def test_analytics_engineer_scores_high(self) -> None:
        scored = score_job(
            title="Analytics Engineer",
            location="Auckland",
            salary="$125,000",
            description=(
                "Build SQL and Python transformations, ETL pipelines, Azure/Fabric models, stakeholder reporting, "
                "and modern analytics engineering workflows."
            ),
        )
        self.assertGreaterEqual(scored.score, 80)

    def test_internship_scores_low(self) -> None:
        scored = score_job(
            title="Data Internship Program",
            location="Auckland",
            salary="$50,000",
            description="Structured training program for graduates and interns with basic reporting support.",
        )
        self.assertLess(scored.score, 40)

    def test_generic_analyst_scores_low_or_medium(self) -> None:
        scored = score_job(
            title="Analyst",
            location="Auckland",
            salary="$78,000",
            description="Support team administration, documentation, coordination, and general business tasks.",
        )
        self.assertLess(scored.score, 60)

    def test_auckland_data_business_analyst_scores_medium_high(self) -> None:
        scored = score_job(
            title="Data Business Analyst",
            location="Auckland",
            salary="$95,000",
            description=(
                "Gather requirements, validate data, support stakeholder reporting, Power BI dashboards, "
                "SQL analysis, and operational analytics for business teams."
            ),
        )
        self.assertGreaterEqual(scored.score, 60)


if __name__ == "__main__":
    unittest.main()
