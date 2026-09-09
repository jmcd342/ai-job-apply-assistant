# Local CV Variants

Store your real CV source files locally in this directory.

These files are used by the static CV-selection workflow through paths configured in `data/cv_profiles.json`.

Recommended setup:

1. Keep your real `.docx` CV files in this directory on your local machine.
2. Create or update your local `data/cv_profiles.json` so each profile points to the correct local file path in `data/cv_variants/`.
3. Do not commit the real CV files to Git.

Example filenames:

- `analytics_engineer_bi_cv.docx`
- `data_engineer_cv.docx`
- `ai_automation_data_science_cv.docx`

This `README.md` may remain tracked as repository guidance, but the actual CV files in this folder must stay private and local only.
