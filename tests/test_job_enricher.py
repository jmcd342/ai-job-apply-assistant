from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

from bs4 import BeautifulSoup

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.database import Database
from src.document_generator import DocumentGenerator
from src.job_enricher import JobEnricher, _console_safe_text, parse_relative_posted_date
from src.job_importer import JobImporter


ENRICHED_HTML = """
<html>
  <body>
    <main>
      <h1>Senior Data Analyst</h1>
      <div data-automation="advertiser-name">Insight Co</div>
      <div data-automation="job-detail-location">Auckland, New Zealand</div>
      <div data-automation="job-detail-salary">$120,000</div>
      <div>Posted 4 days ago</div>
      <section>
        <button>Quick Apply</button>
        <div>Salary: $120,000 - $130,000 p.a.</div>
      </section>
      <section data-automation="jobAdDetails">
        <h2>About the role</h2>
        <p>This role owns stakeholder reporting, Power BI dashboard delivery, SQL analysis, and Python-based data validation.</p>
        <p>You will partner with business teams, build trusted reporting, and improve analytics workflows.</p>
        <p>Similar jobs</p>
      </section>
    </main>
  </body>
</html>
"""

QUICK_APPLY_SPAN_HTML = """
<html>
  <body>
    <main>
      <span data-automation="job-detail-salary">$100,000 – $110,000 per year</span>
      <div><span>Quick apply</span></div>
    </main>
  </body>
</html>
"""


class ConsoleSafeTextTests(unittest.TestCase):
    def test_escapes_unicode_unsupported_by_windows_charmap(self) -> None:
        self.assertEqual(r"\u2060", _console_safe_text("\u2060", encoding="cp1252"))

    def test_preserves_unicode_supported_by_active_encoding(self) -> None:
        self.assertEqual("Māori", _console_safe_text("Māori", encoding="utf-8"))

APPLY_ONLY_HTML = """
<html>
  <body>
    <main>
      <div><span>Apply</span></div>
    </main>
  </body>
</html>
"""

APPLICATION_FALSE_POSITIVE_HTML = """
<html>
  <body>
    <main>
      <div>Read the application guide before applying.</div>
      <div>Applications close soon.</div>
    </main>
  </body>
</html>
"""

INVALID_SALARY_SELECTOR_HTML = """
<html>
  <body>
    <main>
      <div data-automation="job-detail-salary">Southern Cross Travel Insurance</div>
      <div><span>Apply</span></div>
    </main>
  </body>
</html>
"""


class FakeJobEnricher(JobEnricher):
    def _fetch_job_page(self, url: str, *, source: str):
        return type("FetchResult", (), {"html": ENRICHED_HTML, "text": "enriched page text", "clicked_show_more": False})()


class JobEnricherTests(unittest.TestCase):
    def test_parse_relative_posted_date_variants(self) -> None:
        today = date(2026, 5, 10)
        self.assertEqual(date(2026, 5, 6), parse_relative_posted_date("4 days ago", today))
        self.assertEqual(date(2026, 5, 9), parse_relative_posted_date("yesterday", today))
        self.assertEqual(date(2026, 5, 10), parse_relative_posted_date("today", today))
        self.assertEqual(date(2026, 4, 10), parse_relative_posted_date("30+ days ago", today))

    def test_enrichment_updates_full_description_and_documents_use_it(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            root = Path(temp_dir)
            data_dir = root / "data"
            applications_dir = root / "applications"
            data_dir.mkdir(parents=True, exist_ok=True)
            applications_dir.mkdir(parents=True, exist_ok=True)
            for file_name in ["profile.md", "base_cv.txt", "personal_details.json", "cv_profiles.json"]:
                source = PROJECT_ROOT / "data" / file_name
                if source.exists():
                    shutil.copy2(source, data_dir / file_name)
            cv_variants_source = PROJECT_ROOT / "data" / "cv_variants"
            if cv_variants_source.exists():
                shutil.copytree(cv_variants_source, data_dir / "cv_variants", dirs_exist_ok=True)
            templates_source = PROJECT_ROOT / "data" / "templates"
            if templates_source.exists():
                shutil.copytree(templates_source, data_dir / "templates", dirs_exist_ok=True)

            database = Database(data_dir / "job_assistant.db")
            importer = JobImporter(database)
            generator = DocumentGenerator(database, root)
            enricher = FakeJobEnricher(database, root)

            imported = importer.import_manual_job(
                source="seek",
                url="https://www.seek.co.nz/job/12345678",
                title="Senior Data Analyst",
                company="Insight Co",
                location="Auckland",
                salary="",
                description="Short email snippet only.",
                import_channel="manual",
            )
            job_id = imported["job_id"]

            enrichment_result = enricher.enrich_job(job_id, force=True)
            self.assertEqual("success", enrichment_result["status"])

            details = database.get_job_details(job_id)
            self.assertIn("stakeholder reporting", str(details.get("about_the_job") or ""))
            self.assertIn("Power BI dashboard delivery", str(details.get("full_description") or ""))
            self.assertEqual("success", details.get("enrichment_status"))
            expected_posted = (datetime.now().date() - timedelta(days=4)).isoformat()
            self.assertEqual(expected_posted, details.get("date_posted"))
            self.assertEqual("Posted 4 days ago", details.get("date_posted_text"))
            self.assertEqual("Quick Apply", details.get("apply_type"))
            self.assertEqual(0, details.get("has_apply_button"))
            self.assertEqual(1, details.get("has_quick_apply_button"))
            self.assertEqual("$120,000", str(details.get("apply_area_salary_text") or ""))
            self.assertNotIn("Similar jobs", str(details.get("full_description") or ""))

            generation_result = generator.generate_for_job(job_id)
            self.assertEqual("about_the_job", generation_result["document_basis"])
            cover_letter_path = root / "applications" / str(job_id) / "cover_letter.md"
            cover_letter = cover_letter_path.read_text(encoding="utf-8")
            self.assertIn("power bi", cover_letter.lower())
            self.assertEqual("about_the_job", database.get_job_details(job_id).get("document_basis"))

    def test_cleaning_removes_linkedin_noise(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            root = Path(temp_dir)
            enricher = JobEnricher(Database(root / "data" / "job_assistant.db"), root)
            cleaned = enricher._clean_visible_text(  # noqa: SLF001
                "About the job\nBuild dashboards\nSimilar jobs\nPeople also viewed\nSign in\nCreate job alert\nTrusted reporting"
            )
            self.assertIn("Build dashboards", cleaned)
            self.assertIn("Trusted reporting", cleaned)
            self.assertNotIn("Similar jobs", cleaned)
            self.assertNotIn("People also viewed", cleaned)
            self.assertNotIn("Sign in", cleaned)

    def test_seek_quick_apply_detected_inside_nested_span(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            root = Path(temp_dir)
            enricher = JobEnricher(Database(root / "data" / "job_assistant.db"), root)
            details = enricher._extract_seek_apply_details(BeautifulSoup(QUICK_APPLY_SPAN_HTML, "html.parser"))  # noqa: SLF001
            self.assertEqual("Quick Apply", details["apply_type"])
            self.assertTrue(details["has_quick_apply_button"])
            self.assertFalse(details["has_apply_button"])

    def test_seek_salary_selector_extracts_job_detail_salary_text(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            root = Path(temp_dir)
            enricher = JobEnricher(Database(root / "data" / "job_assistant.db"), root)
            details = enricher._extract_seek_apply_details(BeautifulSoup(QUICK_APPLY_SPAN_HTML, "html.parser"))  # noqa: SLF001
            self.assertEqual("$100,000 – $110,000 per year", details["apply_area_salary_text"])

    def test_seek_salary_selector_rejects_company_name(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            root = Path(temp_dir)
            enricher = JobEnricher(Database(root / "data" / "job_assistant.db"), root)
            soup = BeautifulSoup(INVALID_SALARY_SELECTOR_HTML, "html.parser")
            details = enricher._extract_seek_apply_details(soup)  # noqa: SLF001
            parsed = enricher._finalise_parsed_payload(enricher._parse_seek_page(soup))  # noqa: SLF001
            self.assertEqual("", details["apply_area_salary_text"])
            self.assertEqual("", parsed["salary"])

    def test_seek_normal_apply_detection_still_works(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            root = Path(temp_dir)
            enricher = JobEnricher(Database(root / "data" / "job_assistant.db"), root)
            details = enricher._extract_seek_apply_details(BeautifulSoup(APPLY_ONLY_HTML, "html.parser"))  # noqa: SLF001
            self.assertEqual("Apply", details["apply_type"])
            self.assertTrue(details["has_apply_button"])
            self.assertFalse(details["has_quick_apply_button"])

    def test_seek_apply_detection_avoids_application_false_positive(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            root = Path(temp_dir)
            enricher = JobEnricher(Database(root / "data" / "job_assistant.db"), root)
            details = enricher._extract_seek_apply_details(BeautifulSoup(APPLICATION_FALSE_POSITIVE_HTML, "html.parser"))  # noqa: SLF001
            self.assertEqual("unknown", details["apply_type"])
            self.assertFalse(details["has_apply_button"])
            self.assertFalse(details["has_quick_apply_button"])


if __name__ == "__main__":
    unittest.main()
