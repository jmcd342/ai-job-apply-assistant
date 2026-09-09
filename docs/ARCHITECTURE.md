# Architecture

## Core pipeline

`JobImporter` cleans descriptions and normalises source identifiers before `DuplicateChecker` checks existing records. `Database` persists the job and related application state in SQLite. Its managed connection context handles commit/rollback and closing connections; WAL mode helps with reader/writer access on a single machine.

`fit_scorer.py` uses explicit weights and role-family rules. It records matched skills, missing skills, and risk flags alongside the score so a person can inspect the result. These are rule outputs, not calibrated probabilities.

`cv_selector.py` chooses a static profile. `DocumentGenerator` renders a contextual template and creates a packet containing a cover letter, interview preparation, rationale, and checklist. The source CV is preserved; an upload copy is prepared separately.

## Interfaces and boundaries

- `demo_app.py` and `demo.py` exercise core modules with fictional data in temporary directories.
- `app.py` exposes the full local workflow through Streamlit.
- Gmail ingestion and job-page enrichment are optional external adapters.
- SEEK assistance uses a persistent local browser profile and explicit state handling. A browser profile can contain sensitive session cookies even though the application does not store a password.

## Tradeoffs

SQLite and Streamlit reduce deployment complexity for a single-user tool. This design does not provide multi-user isolation, distributed workers, or a service API. Rule-based scoring is inspectable and inexpensive but depends on hand-maintained assumptions. Template drafting is predictable but less flexible than a model-based approach.

## Public data boundary

Only source, tests, fictional examples, and documentation belong in Git. Runtime databases, OAuth files, browser sessions, CVs, generated packets, and diagnostic logs stay local. The public repository starts from a clean history because removing a file from the latest commit would not remove it from earlier commits.
