"""Offline walkthrough using fictional fixtures and the real core pipeline."""
from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

from src.database import Database
from src.document_generator import DocumentGenerator
from src.job_importer import JobImporter

PROJECT_ROOT = Path(__file__).resolve().parent


def build_demo(root: Path) -> tuple[Database, list[int]]:
    """Build an isolated demo; callers own the temporary directory lifecycle."""
    data = root / "data"
    data.mkdir(parents=True, exist_ok=True)
    (data / "templates").mkdir(exist_ok=True)
    for source, destination in [
        ("personal_details.example.json", "personal_details.json"),
        ("profile.example.md", "profile.md"),
        ("cv_profiles.example.json", "cv_profiles.json"),
        ("templates/master_cover_letter.txt", "templates/master_cover_letter.txt"),
    ]:
        shutil.copyfile(PROJECT_ROOT / "data" / source, data / destination)
    database = Database(data / "demo.db")
    importer = JobImporter(database)
    jobs = json.loads((PROJECT_ROOT / "examples" / "jobs.json").read_text(encoding="utf-8"))
    ids = []
    for job in jobs:
        result = importer.import_manual_job(**job, import_channel="fictional_demo")
        if not result.get("created"):
            raise RuntimeError("Expected a new fictional job")
        ids.append(result["job_id"])
        duplicate = importer.import_manual_job(**job, import_channel="fictional_demo")
        if duplicate.get("created"):
            raise RuntimeError("Duplicate prevention failed")
    return database, ids


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="job-portfolio-") as temporary:
        root = Path(temporary)
        database, ids = build_demo(root)
        generator = DocumentGenerator(database, root)
        for job_id in ids:
            job = database.get_job_details(job_id)
            print(f"{job['title']} | fit score: {job['fit_score']}")
            packet = generator.generate_for_job(job_id)
            if not (Path(packet["output_dir"]) / "application_packet.md").is_file():
                raise RuntimeError("Document packet was not generated")
        print(f"PASS: {len(ids)} fictional jobs imported, duplicates rejected, packets generated.")
        print("No mailbox, external job page, or application submission was used.")


if __name__ == "__main__":
    main()
