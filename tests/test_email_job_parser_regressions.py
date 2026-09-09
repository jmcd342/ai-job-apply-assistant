from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.email_job_parser import parse_email_html_jobs


SEEK_INSURANCE_COMPANY_HTML = """
<html>
  <body>
    <h1>20 new jobs for data</h1>
    <a style="display:block" href="https://email.s.seek.co.nz/uni/ss/c/southern-cross-token">
      <div style="text-decoration:underline">Data Analyst</div>
      <div>Strong applicant</div>
      <div>Southern Cross Travel Insurance</div>
      <div>Auckland CBD, Auckland</div>
    </a>
  </body>
</html>
"""


class EmailJobParserRegressionTests(unittest.TestCase):
    def test_seek_applicant_badge_is_ignored_and_insurance_company_is_preserved(self) -> None:
        parsed = parse_email_html_jobs(
            SEEK_INSURANCE_COMPANY_HTML,
            subject="20 new jobs for data",
            message_id="seek-msg-insurance",
        )
        self.assertEqual(1, len(parsed.jobs))
        self.assertEqual("Data Analyst", parsed.jobs[0]["title"])
        self.assertEqual("Southern Cross Travel Insurance", parsed.jobs[0]["company"])
        self.assertEqual("Auckland CBD, Auckland", parsed.jobs[0]["location"])
        self.assertEqual("", parsed.jobs[0]["salary"])


if __name__ == "__main__":
    unittest.main()
