"""Assign ticket_link on every job summary using pre-fetched Jira sprint issues."""

from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Any


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9][a-z0-9_-]{2,}", text.lower())}


def _issue_text(issue: dict[str, Any]) -> str:
    parts = [
        issue.get("key", ""),
        issue.get("summary", ""),
        issue.get("description", ""),
        issue.get("status", ""),
        issue.get("sprint_name", ""),
    ]
    return " ".join(str(p) for p in parts if p)


def _job_text(job: dict[str, Any]) -> str:
    parts = [
        job.get("job_id", ""),
        job.get("root_cause_summary", ""),
        job.get("root_cause_category", ""),
        job.get("catalog_item", ""),
        job.get("cluster", ""),
        job.get("platform", ""),
        job.get("guid", ""),
        job.get("failing_role", ""),
        job.get("failing_github_path", ""),
    ]
    return " ".join(str(p) for p in parts if p)


def score_match(job: dict[str, Any], issue: dict[str, Any]) -> int:
    job_blob = _job_text(job).lower()
    issue_blob = _issue_text(issue).lower()
    score = 0

    guid = str(job.get("guid", "")).lower()
    if guid and guid in issue_blob:
        score += 50

    job_id = str(job.get("job_id", ""))
    if job_id and job_id in issue_blob:
        score += 40

    catalog = str(job.get("catalog_item", "")).lower()
    if catalog and catalog in issue_blob:
        score += 25

    platform = str(job.get("platform", "")).lower()
    if platform and platform in issue_blob:
        score += 10

    role = str(job.get("failing_role", "")).lower()
    if role and role in issue_blob:
        score += 15

    path = str(job.get("failing_github_path", "")).lower()
    if path:
        filename = path.rsplit(":", 1)[0].rsplit("/", 1)[-1]
        if filename and filename in issue_blob:
            score += 12

    job_tokens = _tokens(job_blob)
    issue_tokens = _tokens(issue_blob)
    score += len(job_tokens & issue_tokens)

    return score


def pick_issue(job: dict[str, Any], issues: list[dict[str, Any]]) -> dict[str, Any]:
    if not issues:
        raise ValueError("No Jira issues available for ticket matching")

    ranked = sorted(issues, key=lambda issue: score_match(job, issue), reverse=True)
    return ranked[0]


def assign_links(report: dict[str, Any], jira_data: dict[str, Any]) -> dict[str, Any]:
    issues = jira_data.get("issues", [])
    if not issues:
        raise ValueError("Jira issue list is empty; cannot assign ticket_link")

    report["jira_sprint_tickets"] = jira_data

    for job in report.get("job_summaries", []):
        best = pick_issue(job, issues)
        job["ticket_link"] = best["ticket_url"]

    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Assign ticket_link in batch reports")
    parser.add_argument("report", help="Path to batch report JSON")
    parser.add_argument("--jira-issues", required=True, help="Path to jira sprint issues JSON")
    parser.add_argument("--in-place", action="store_true", help="Overwrite the report file")
    args = parser.parse_args(argv)

    with open(args.report) as f:
        report = json.load(f)
    with open(args.jira_issues) as f:
        jira_data = json.load(f)

    try:
        report = assign_links(report, jira_data)
    except ValueError as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        return 1

    output_path = args.report if args.in_place else args.report + ".with_tickets.json"
    with open(output_path, "w") as f:
        json.dump(report, f, indent=2)
        f.write("\n")

    assigned = sum(1 for j in report.get("job_summaries", []) if j.get("ticket_link"))
    print(f"[OK] Assigned ticket_link for {assigned} job(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
