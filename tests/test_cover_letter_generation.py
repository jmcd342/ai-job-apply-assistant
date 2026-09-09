from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.database import Database
from src.apply_assist import build_apply_assist_payload
from src.document_generator import COVER_LETTER_FORBIDDEN_PHRASES, DocumentGenerator
from src.job_importer import JobImporter


class CoverLetterGenerationTests(unittest.TestCase):
    def test_cover_letter_uses_master_template_and_replaces_placeholders(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            root = Path(temp_dir)
            self._seed_data(root)
            database = Database(root / "data" / "job_assistant.db")
            importer = JobImporter(database)
            generator = DocumentGenerator(database, root)

            result = importer.import_manual_job(
                source="seek",
                url="https://www.seek.co.nz/job/999999",
                title="Senior Data Analyst",
                company="Datacom",
                location="Auckland",
                salary="$110,000",
                description=(
                    "Senior Data Analyst role focused on Power BI, SQL, stakeholder reporting, cloud reporting uplift, "
                    "and collaboration with business teams to improve decision-making."
                ),
                import_channel="test",
            )
            package = generator.generate_for_job(result["job_id"])
            cover_letter = (Path(package["output_dir"]) / "cover_letter.md").read_text(encoding="utf-8")

            self.assertRegex(cover_letter, r"\d{1,2} [A-Za-z]+ \d{4}")
            self.assertIn("Application for Senior Data Analyst Role", cover_letter)
            self.assertIn("Datacom", cover_letter)
            self.assertIn("Example Candidate", cover_letter)
            self.assertNotIn("{company}", cover_letter)
            self.assertNotIn("{job_title}", cover_letter)
            self.assertNotIn("{date}", cover_letter)
            self.assertNotIn("{location}", cover_letter)
            self.assertIn("Dear Hiring Team,", cover_letter)
            self.assertIn("Kind regards,", cover_letter)

    def test_cover_letter_excludes_internal_logic_phrases(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            root = Path(temp_dir)
            self._seed_data(root)
            database = Database(root / "data" / "job_assistant.db")
            generator = DocumentGenerator(database, root)

            job = {
                "title": "Analytics Engineer",
                "company": "Example Platform Co",
                "location": "Auckland",
                "document_description": "Analytics engineering role using SQL, Python, ETL, Azure, and stakeholder engagement.",
                "source_page_text": "",
            }
            cover_letter, mode, sanitized, banned_phrase = generator._build_cover_letter(job, generator._load_personal_details(), generator._load_profile())
            lowered = cover_letter.lower()
            for phrase in COVER_LETTER_FORBIDDEN_PHRASES:
                self.assertNotIn(phrase, lowered)
            self.assertEqual(mode, "template_contextualized")
            self.assertFalse(sanitized)
            self.assertIsNone(banned_phrase)
            self.assertNotIn("ai job apply assistant", lowered)
            self.assertNotIn("cv profile", lowered)

    def test_company_and_role_are_inserted_correctly(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            root = Path(temp_dir)
            self._seed_data(root)
            database = Database(root / "data" / "job_assistant.db")
            generator = DocumentGenerator(database, root)

            job = {
                "title": "Data Business Analyst",
                "company": "Halter",
                "location": "Auckland",
                "document_description": "Business-facing analytics and reporting role with SQL, dashboards, and stakeholder workshops.",
                "source_page_text": "",
            }
            cover_letter, _, _, _ = generator._build_cover_letter(job, generator._load_personal_details(), generator._load_profile())
            self.assertIn("Data Business Analyst", cover_letter)
            self.assertIn("Halter", cover_letter)
            self.assertNotIn("cv profile", cover_letter.lower())

    def test_sanitizer_flags_bad_phrases(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            root = Path(temp_dir)
            self._seed_data(root)
            database = Database(root / "data" / "job_assistant.db")
            generator = DocumentGenerator(database, root)

            bad_text = "This aligns well with the data engineer cv profile I am using for this application."
            cleaned, banned = generator.sanitize_cover_letter(bad_text)
            self.assertTrue(banned)
            self.assertIn("aligns well with", cleaned.lower())

    def test_fallback_template_is_used_when_context_is_unsafe(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            root = Path(temp_dir)
            self._seed_data(root)
            database = Database(root / "data" / "job_assistant.db")
            generator = DocumentGenerator(database, root)

            def unsafe_context(job: dict[str, str], profile_text: str) -> dict[str, str]:
                return {
                    "date": "7 May 2026",
                    "company": "Unsafe Co",
                    "location": "Auckland, New Zealand",
                    "job_title": "Data Engineer",
                    "company_context": "a team where the main value here seems to be fit scoring",
                    "company_motivation": "the opportunity to work on an automation pipeline",
                    "relevant_skills": "Power BI, SQL, and best matched workflow logic",
                    "growth_area": "role family scoring",
                }

            generator.generate_cover_letter_context = unsafe_context  # type: ignore[method-assign]
            cover_letter, mode, sanitized, banned_phrase = generator._build_cover_letter(
                {"title": "Data Engineer", "company": "Unsafe Co", "location": "Auckland", "document_description": ""},
                generator._load_personal_details(),
                generator._load_profile(),
            )

            self.assertEqual(mode, "template_fallback_sanitized")
            self.assertTrue(sanitized)
            self.assertIn(banned_phrase, {"fit score", "fit scoring", "the main value here seems to be"})
            lowered = cover_letter.lower()
            for phrase in COVER_LETTER_FORBIDDEN_PHRASES:
                self.assertNotIn(phrase, lowered)
            self.assertIn("Unsafe Co", cover_letter)
            self.assertIn("Application for Data Engineer Role", cover_letter)

    def test_stale_cover_letter_is_replaced_and_apply_assist_uses_new_content(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            root = Path(temp_dir)
            self._seed_data(root)
            database = Database(root / "data" / "job_assistant.db")
            importer = JobImporter(database)
            generator = DocumentGenerator(database, root)

            result = importer.import_manual_job(
                source="seek",
                url="https://www.seek.co.nz/job/777777",
                title="BI Analyst",
                company="Example Co",
                location="Auckland",
                salary="$100,000",
                description="Power BI, SQL, and reporting role.",
                import_channel="test",
            )
            job_id = result["job_id"]
            application_dir = root / "applications" / str(job_id)
            application_dir.mkdir(parents=True, exist_ok=True)
            stale_cover_letter = (
                "Two examples that best reflect how I work are the AI Job Apply Assistant and the Trade Me property scraper. "
                "That is the kind of role I am aiming for, and it aligns well with the data engineer cv profile I am using for this application."
            )
            (application_dir / "cover_letter.md").write_text(stale_cover_letter, encoding="utf-8")
            database.update_job_career_assets(
                job_id,
                cover_letter_preview=stale_cover_letter,
                selected_cv_path=str(application_dir / "selected_cv.docx"),
            )
            (application_dir / "selected_cv.docx").write_text("fake cv", encoding="utf-8")

            refreshed = generator.ensure_current_cover_letter(job_id, force_regenerate=False)
            payload = build_apply_assist_payload(refreshed)
            lowered = payload["cover_letter"].lower()

            self.assertNotIn("best reflect how i work", lowered)
            self.assertNotIn("ai job apply assistant", lowered)
            self.assertNotIn("cv profile", lowered)
            self.assertIn("Application for BI Analyst Role", payload["cover_letter"])
            self.assertIn("Example Co", payload["cover_letter"])

    def test_invalidate_stale_cover_letters_regenerates_existing_rows(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            root = Path(temp_dir)
            self._seed_data(root)
            database = Database(root / "data" / "job_assistant.db")
            importer = JobImporter(database)
            generator = DocumentGenerator(database, root)

            result = importer.import_manual_job(
                source="seek",
                url="https://www.seek.co.nz/job/666666",
                title="Reporting Analyst",
                company="Example Co",
                location="Auckland",
                salary="$95,000",
                description="Reporting, SQL, and Power BI role.",
                import_channel="test",
            )
            job_id = result["job_id"]
            app_dir = root / "applications" / str(job_id)
            app_dir.mkdir(parents=True, exist_ok=True)
            stale_text = "The main value here seems to be workflow scoring and it aligns well with the data engineer cv profile."
            (app_dir / "cover_letter.md").write_text(stale_text, encoding="utf-8")
            (app_dir / "selected_cv.docx").write_text("fake cv", encoding="utf-8")
            database.update_job_career_assets(
                job_id,
                cover_letter_preview=stale_text,
                selected_cv_path=str(app_dir / "selected_cv.docx"),
            )

            stats = generator.invalidate_stale_cover_letters()
            refreshed = database.get_job_details(job_id)
            self.assertEqual(stats["regenerated"], 1)
            self.assertIsNotNone(refreshed)
            self.assertNotIn("aligns well with", str(refreshed["cover_letter_preview"]).lower())
            self.assertIn(str(refreshed["cover_letter_basis"]), {"template_contextualized", "template_fallback_sanitized"})

    def _seed_data(self, root: Path) -> None:
        data_dir = root / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        templates_dir = data_dir / "templates"
        templates_dir.mkdir(parents=True, exist_ok=True)

        for file_name in ["profile.md", "base_cv.txt", "personal_details.json", "cv_profiles.json"]:
            source = PROJECT_ROOT / "data" / file_name
            if source.exists():
                (data_dir / file_name).write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
        template_source = PROJECT_ROOT / "data" / "templates" / "master_cover_letter.txt"
        (templates_dir / "master_cover_letter.txt").write_text(template_source.read_text(encoding="utf-8"), encoding="utf-8")

        cv_variants_source = PROJECT_ROOT / "data" / "cv_variants"
        if cv_variants_source.exists():
            import shutil

            shutil.copytree(cv_variants_source, data_dir / "cv_variants", dirs_exist_ok=True)


if __name__ == "__main__":
    unittest.main()
