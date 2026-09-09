"""Recruiter-facing offline demo. Does not load live integration modules."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import streamlit as st

from demo import build_demo
from src.document_generator import DocumentGenerator

st.set_page_config(page_title="Job Apply Assistant | Portfolio", page_icon="📋", layout="wide")
st.caption("PYTHON  /  SQLITE  /  STREAMLIT")
st.title("From job lead to application packet")
st.write("Explore an explainable application workflow, using fictional roles and the real project pipeline.")
st.info("Portfolio demo · Fictional candidate and employers · No sign-in or application submission")

if "portfolio_demo" not in st.session_state:
    temporary = tempfile.TemporaryDirectory(prefix="job-portfolio-ui-")
    root = Path(temporary.name)
    database, ids = build_demo(root)
    st.session_state.portfolio_demo = (temporary, root, database, ids)
_, root, database, ids = st.session_state.portfolio_demo
jobs = [database.get_job_details(job_id) for job_id in ids]

left, middle, right = st.columns(3)
left.metric("Example opportunities", len(jobs))
middle.metric("Duplicates prevented", len(jobs))
right.metric("External connections", 0)
st.divider()

selected = st.selectbox("Explore an opportunity", ids, format_func=lambda value: next(j["title"] for j in jobs if j["id"] == value))
job = database.get_job_details(selected)
summary, detail = st.columns([1, 2])
with summary:
    st.subheader(job["title"])
    st.write(job["company"])
    st.caption(job["location"])
    st.metric("Rule-based fit score", f"{job['fit_score']}/100")
    st.caption("A prioritisation heuristic, not a hiring probability.")
    st.write("**Salary in fictional listing**")
    st.text(job["salary"])
with detail:
    st.subheader("What the pipeline sees")
    st.write(job["description"])
    matched = job.get("matched_skills_json") or "[]"
    if isinstance(matched, str):
        matched = json.loads(matched)
    st.write("**Matched signals:** " + (", ".join(matched) or "See score details in the core module."))
    st.write("**Role family:** " + str(job.get("role_family") or "Unclassified").replace("_", " ").title())
    st.caption("Cleaning → duplicate check → SQLite → scoring → CV selection → document packet")

st.divider()
st.subheader("Build a review packet")
st.write("Generate a sample cover letter, interview notes, and application rationale. The sample CV is a fallback profile summary because private CV files are excluded.")
if st.button("Generate fictional application packet", type="primary"):
    with st.spinner("Building packet with the project document generator…"):
        DocumentGenerator(database, root).generate_for_job(selected)
packet_dir = root / "applications" / str(selected)
if (packet_dir / "application_packet.md").exists():
    for label, filename in [("Cover letter", "cover_letter.md"), ("Interview preparation", "interview_prep.md"), ("Application rationale", "application_rationale.md")]:
        with st.expander(label, expanded=label == "Cover letter"):
            st.markdown((packet_dir / filename).read_text(encoding="utf-8"))
    st.download_button("Download sample packet", (packet_dir / "application_packet.md").read_text(encoding="utf-8"), file_name="fictional-application-packet.md")
st.caption("All demo data stays in a temporary directory. The full project also contains optional Gmail and browser adapters; this demo does not invoke them.")
