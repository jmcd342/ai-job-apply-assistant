from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the SEEK resume debug harness and print a Codex-ready failure summary.")
    parser.add_argument("--job-url", required=True)
    parser.add_argument("--cv-path", required=True)
    parser.add_argument("--visible", action="store_true")
    parser.add_argument("--stop-after-resume", action="store_true")
    parser.add_argument("--slow-mo-ms", type=int, default=500)
    args = parser.parse_args()

    debug_root = PROJECT_ROOT / "data" / "debug_apply_assist" / "resume_debug_runs"
    before = {path.name for path in debug_root.iterdir()} if debug_root.exists() else set()
    command = [
        sys.executable,
        str(PROJECT_ROOT / "scripts" / "debug_seek_resume_upload.py"),
        "--job-url",
        args.job_url,
        "--cv-path",
        args.cv_path,
        "--slow-mo-ms",
        str(args.slow_mo_ms),
    ]
    if args.visible:
        command.append("--visible")
    if args.stop_after_resume:
        command.append("--stop-after-resume")

    completed = subprocess.run(command, cwd=str(PROJECT_ROOT))
    after = {path.name for path in debug_root.iterdir()} if debug_root.exists() else set()
    new_dirs = sorted(after - before)
    debug_dir = debug_root / new_dirs[-1] if new_dirs else debug_root
    summary_path = debug_dir / "final_summary.json"
    summary = {}
    if summary_path.exists():
        summary = json.loads(summary_path.read_text(encoding="utf-8"))

    if completed.returncode != 0:
        print(f"Debug folder: {debug_dir}", flush=True)
        print(f"Final checkpoint summary: {json.dumps(summary, indent=2, ensure_ascii=False)}", flush=True)
        prompt = (
            "Paste this into Codex:\n\n"
            f"SEEK resume debug harness failed.\n"
            f"Failure reason: {summary.get('failure_reason', '(unknown)')}\n"
            f"Final checkpoint: {summary.get('final_checkpoint', '(unknown)')}\n"
            f"Artifacts path: {debug_dir}\n"
            f"Relevant log lines:\n" + "\n".join(summary.get("log_lines", [])) + "\n\n"
            "Please inspect the screenshot, HTML, and JSON artifacts in that folder and suggest the next fix. "
            "Do not automatically modify code without user approval."
        )
        print(prompt, flush=True)
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
