#!/usr/bin/env python3
"""
CLI entrypoint for CV evaluation — runs the full agent pipeline without HTTP.

Usage:
    python cli.py --cv path/to/cv.pdf \
                  --job-description "Senior Python Engineer..." \
                  --requirements "5+ years Python, AWS..." \
                  --responsibilities "Lead backend services..."

    # JSON output (pipe-friendly)
    python cli.py --cv cv.pdf --job-description "..." \
                  --requirements "..." --responsibilities "..." \
                  --output json
"""
import argparse
import json
import sys
from pathlib import Path

from app import CVEvaluationOutput, build_agent
from prompts.CVEvaluationPrompt import CVEvaluationPrompt


def evaluate(cv_path: Path, job_description: str, job_requirements: str, job_responsibilities: str) -> CVEvaluationOutput:
    raw_bytes = cv_path.read_bytes()
    try:
        candidate_cv = raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        candidate_cv = raw_bytes.decode("latin-1")

    user_prompt = CVEvaluationPrompt.USER.format(
        job_description=job_description,
        job_requirements=job_requirements,
        job_responsibilities=job_responsibilities,
        candidate_cv=candidate_cv,
    )

    agent = build_agent()
    response = agent(user_prompt)
    return response.structured_output


def main():
    parser = argparse.ArgumentParser(
        description="Evaluate a CV against a job posting using the Strands agent."
    )
    parser.add_argument(
        "--cv",
        required=True,
        metavar="FILE",
        help="Path to the candidate CV file (PDF, TXT, DOCX, etc.)",
    )
    parser.add_argument("--job-description", required=True, metavar="TEXT")
    parser.add_argument("--requirements", required=True, metavar="TEXT")
    parser.add_argument("--responsibilities", required=True, metavar="TEXT")
    parser.add_argument(
        "--output",
        choices=["json", "pretty"],
        default="pretty",
        help="Output format: 'pretty' (default) or 'json'",
    )
    args = parser.parse_args()

    cv_path = Path(args.cv)
    if not cv_path.exists():
        print(f"Error: CV file not found: {cv_path}", file=sys.stderr)
        sys.exit(1)

    print("Running CV evaluation...", file=sys.stderr)

    try:
        result = evaluate(
            cv_path=cv_path,
            job_description=args.job_description,
            job_requirements=args.requirements,
            job_responsibilities=args.responsibilities,
        )
    except Exception as exc:
        print(f"Evaluation failed: {exc}", file=sys.stderr)
        sys.exit(1)

    if args.output == "json":
        print(json.dumps(result.model_dump(mode="json"), indent=2))
    else:
        print(f"\nClassification : {result.classification}")
        print(f"Score          : {result.score}/100")
        print(f"\nSummary:\n{result.summary}")
        print(f"\nStrengths:")
        for s in result.strengths:
            print(f"  - {s}")
        print(f"\nGaps:")
        for g in result.gaps:
            print(f"  - {g}")
        print(f"\nReasoning:\n{result.reasoning}")


if __name__ == "__main__":
    main()
