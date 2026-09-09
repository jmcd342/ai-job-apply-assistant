# Full application setup

Use the offline demo in the README first. The full dashboard includes actions that contact external services.

1. Create a Python 3.12 virtual environment and install `requirements.txt`.
2. Copy `data/personal_details.example.json` to `data/personal_details.json` and `data/profile.example.md` to `data/profile.md`.
3. Copy `data/cv_profiles.example.json` to `data/cv_profiles.json`; supply your own CV files at its configured paths.
4. Add your own `data/base_cv.txt` and adapt `data/templates/master_cover_letter.txt` to truthful experience.
5. Review every field of `data/applicant_profile.example.json` before creating `data/applicant_profile.json`. Example eligibility and experience values must not be submitted as facts.
6. Start `python -m streamlit run app.py`.

## Optional Gmail

Create a desktop OAuth client with the Gmail API enabled and save its downloaded credentials locally as `data/gmail/credentials.json`. Copy `.env.example` to `.env` if custom paths are needed. Run `python gmail_ingestor.py` to complete normal OAuth sign-in. Ingestion may label messages and mark them read; inspect its settings before using your mailbox.

## Optional browser assistance

Install Chromium with `python -m playwright install chromium` and run `python scripts/seek_login_setup.py` for manual sign-in. Browser profiles contain session data and must remain private. Third-party login challenges require user interaction.

## Daily processing

```sh
python run_daily.py --prepare-only
```

This stops before application submission but may still import Gmail messages and fetch job pages. Omitting `--prepare-only` enables the existing submission runner. This is not part of the portfolio demo or CI checks.
