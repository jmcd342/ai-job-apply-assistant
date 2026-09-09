from __future__ import annotations

import gc
import shutil
import sys
import tempfile
from pathlib import Path

from docx import Document

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.database import Database
from src.document_generator import DocumentGenerator
from src.job_importer import JobImporter

PLACEHOLDER = "[Your Name]"


def main() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
        temp_root = Path(temp_dir)
        data_dir = temp_root / "data"
        applications_dir = temp_root / "applications"
        data_dir.mkdir(parents=True, exist_ok=True)
        applications_dir.mkdir(parents=True, exist_ok=True)

        for file_name in ["profile.md", "base_cv.txt", "personal_details.json", "cv_profiles.json"]:
            source = PROJECT_ROOT / "data" / file_name
            if source.exists():
                shutil.copy2(source, data_dir / file_name)
        templates_source = PROJECT_ROOT / "data" / "templates"
        if templates_source.exists():
            shutil.copytree(templates_source, data_dir / "templates", dirs_exist_ok=True)
        cv_variants_source = PROJECT_ROOT / "data" / "cv_variants"
        if cv_variants_source.exists():
            shutil.copytree(cv_variants_source, data_dir / "cv_variants", dirs_exist_ok=True)

        database = Database(data_dir / "job_assistant.db")
        importer = JobImporter(database)
        generator = DocumentGenerator(database, temp_root)

        result = importer.import_manual_job(
            source="manual",
            url="https://www.seek.co.nz/job/76543210",
            title="Store Operations Coordinator",
            company="Example Retail",
            location="Auckland",
            salary="$55,000",
            description=(
                "Support warehouse and retail operations in Auckland, coordinate store documentation, "
                "maintain records, and help the wider team stay organised."
            ),
            import_channel="smoke_test",
        )
        assert result["created"], "Expected test job to be imported"
        assert result["auto_draft_job_ids"] == [result["job_id"]], "Expected imported Auckland job to be queued for drafting"
        assert int(result["fit_score"]) < 80, "Expected the smoke-test role to be low fit"

        package = generator.generate_for_job(result["job_id"])
        job_dir = Path(package["output_dir"])
        upload_cv_dir = temp_root / "data" / "temp_uploads"
        upload_cv_matches = sorted(upload_cv_dir.glob("Example_Candidate_CV_*.docx"))
        assert upload_cv_matches, "Expected recruiter-facing upload CV copy to exist"
        upload_cv_path = upload_cv_matches[-1]

        required_files = [
            job_dir / "tailored_cv.docx",
            job_dir / "tailored_cv_preview.md",
            job_dir / "cover_letter.md",
            job_dir / "application_checklist.md",
            job_dir / "application_rationale.md",
            job_dir / "interview_prep.md",
            job_dir / "demo_recommendation.md",
            job_dir / "application_packet.md",
        ]
        for path in required_files:
            assert path.exists(), f"Expected generated file: {path}"
        assert upload_cv_path.exists(), "Expected recruiter-facing upload CV copy to exist"
        assert upload_cv_path.name.startswith("Example_Candidate_CV_"), "Expected recruiter-facing upload CV filename prefix"
        assert upload_cv_path.read_bytes() == (job_dir / "selected_cv.docx").read_bytes(), "Upload CV copy should match selected variant contents"

        upload_cv_path.write_text("stale upload cv", encoding="utf-8")
        generator.generate_for_job(result["job_id"])
        refreshed_upload_matches = sorted(upload_cv_dir.glob("Example_Candidate_CV_*.docx"))
        assert refreshed_upload_matches, "Expected recruiter-facing upload CV copy to exist after regeneration"
        refreshed_upload_cv_path = refreshed_upload_matches[-1]
        assert refreshed_upload_cv_path.name != upload_cv_path.name, "Expected a unique recruiter-facing upload CV filename for each generation run"
        assert refreshed_upload_cv_path.read_bytes() == (job_dir / "selected_cv.docx").read_bytes(), "Repeated generation should overwrite the recruiter-facing upload CV safely"

        cv_preview = (job_dir / "tailored_cv_preview.md").read_text(encoding="utf-8")
        cover_letter = (job_dir / "cover_letter.md").read_text(encoding="utf-8")
        rationale = (job_dir / "application_rationale.md").read_text(encoding="utf-8")
        selection_criteria = (job_dir / "selection_criteria.md").read_text(encoding="utf-8")
        recruiter_message = (job_dir / "recruiter_message.txt").read_text(encoding="utf-8")
        checklist = (job_dir / "application_checklist.md").read_text(encoding="utf-8")
        assert PLACEHOLDER not in cv_preview, "CV preview still contains placeholder text"
        assert PLACEHOLDER not in cover_letter, "Cover letter still contains placeholder text"
        assert PLACEHOLDER not in rationale, "Rationale still contains placeholder text"
        forbidden_tokens = ["[Your Name]", "[Email]", "[Phone]", "Replace this template", "Profile Summary Template"]
        all_text = "\n".join([cv_preview, cover_letter, rationale, selection_criteria, recruiter_message, checklist])
        for token in forbidden_tokens:
            assert token not in all_text, f"Generated text still contains forbidden token: {token}"
        assert "Example Candidate" in cover_letter, "Expected personal details to appear in generated cover letter"
        forbidden_cover_letter_phrases = [
            "cv profile",
            "fit scoring",
            "role family",
            "best matched",
            "aligns well with",
            "the main value here seems to be",
            "that is the kind of role i am aiming for",
            "ai job apply assistant",
            "application workflow",
            "automation pipeline",
            "internal logic",
            "document basis",
            "email snippet fallback",
            "role classification",
        ]
        lowered_cover_letter = cover_letter.lower()
        for phrase in forbidden_cover_letter_phrases:
            assert phrase not in lowered_cover_letter, f"Cover letter should not expose internal phrase: {phrase}"

        cv_doc = Document(job_dir / "tailored_cv.docx")
        doc_text = "\n".join(paragraph.text for paragraph in cv_doc.paragraphs)
        assert PLACEHOLDER not in doc_text, "DOCX CV still contains placeholder text"

        generated_docs = database.list_generated_documents(result["job_id"])
        doc_types = {item["doc_type"] for item in generated_docs}
        details = database.get_job_details(result["job_id"])
        assert "tailored_cv_preview" in doc_types, "Expected tailored_cv_preview to be registered in generated documents"
        assert "tailored_cv" in doc_types, "Expected tailored_cv to be registered in generated documents"
        assert "cover_letter" in doc_types, "Expected cover_letter to be registered in generated documents"
        assert "interview_prep" in doc_types, "Expected interview_prep to be registered in generated documents"
        assert "demo_recommendation" in doc_types, "Expected demo_recommendation to be registered in generated documents"
        assert details["selected_cv_profile"], "Expected selected CV profile to be stored"
        assert Path(details["selected_cv_path"]).name.startswith("Example_Candidate_CV_"), "Expected stored selected_cv_path to use recruiter-facing filename"
        assert details["cover_letter_basis"], "Expected cover letter basis to be stored"
        del cv_doc
        del generator
        del importer
        del database
        gc.collect()

        print("Smoke test passed.")
        print(f"Job imported: yes (fit_score={result['fit_score']})")
        print("Draft package generated: yes")
        print("Placeholder removed: yes")
        print("CV markdown preview exists: yes")


if __name__ == "__main__":
    main()
