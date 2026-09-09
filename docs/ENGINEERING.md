# Engineering walkthrough

A short guide for discussing this project in an interview.

## 1. Follow an opportunity through the system

Run `python demo.py`, then read `src/job_importer.py`, `src/duplicate_checker.py`, and `src/database.py`. The demo imports each fictional role twice to demonstrate duplicate prevention.

## 2. Inspect a score

Read `src/fit_scorer.py` and `tests/test_fit_scorer.py`. Discuss why explicit weights make the result explainable, and where location and skill assumptions can mis-rank a role. There is no labelled evaluation set establishing ranking accuracy.

## 3. Follow document generation

Read `src/cv_selector.py` and `src/document_generator.py`. The generator chooses a profile, renders the cover letter, checks unwanted internal phrases, and stores generated artifacts. The public template is illustrative and must be adapted to a real candidate's verified experience.

## 4. Review failure handling

Read parser and browser-adapter tests. They capture cases such as incomplete job descriptions, duplicated links, review-state mismatches, and missing uploaded documents. Mocked tests make these cases repeatable; live website compatibility still needs separate verification.

## What this demonstrates

Data cleaning, relational persistence, modular Python, third-party adapters, regression testing, and a usable review interface. Be precise about your own contribution and any AI assistance when presenting the project. The repository does not make authorship or performance claims on your behalf.
