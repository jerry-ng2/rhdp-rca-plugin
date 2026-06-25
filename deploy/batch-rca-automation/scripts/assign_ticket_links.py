"""Assign ticket_link on job summaries using rule-based and semantic Jira matching."""

from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Any

DEFAULT_MIN_SCORE = 15
DEFAULT_SEMANTIC_MIN_CONFIDENCE = "medium"
ACCEPTED_SEMANTIC_CONFIDENCES = frozenset({"high", "medium", "low", "none"})
SEMANTIC_CONFIDENCE_RANK = {"high": 3, "medium": 2, "low": 1, "none": 0}

_STOPWORDS = frozenset({
    "for", "the", "and", "with", "from", "that", "this", "when", "not",
    "are", "was", "has", "have", "new", "progress",
})


def _tokens(text: str) -> set[str]:
    return {
        t
        for t in re.findall(r"[a-z0-9][a-z0-9_-]{2,}", text.lower())
        if t not in _STOPWORDS
    }


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


def pick_issue(
    job: dict[str, Any], issues: list[dict[str, Any]]
) -> tuple[dict[str, Any], int]:
    if not issues:
        raise ValueError("No Jira issues available for ticket matching")

    ranked = sorted(issues, key=lambda issue: score_match(job, issue), reverse=True)
    best = ranked[0]
    return best, score_match(job, best)


def _issue_by_key(issues: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(issue.get("key", "")): issue for issue in issues if issue.get("key")}


def _clear_ticket_fields(job: dict[str, Any]) -> None:
    job["ticket_link"] = None
    job["is_open"] = None
    job["ticket_match_method"] = None
    job["ticket_match_rationale"] = None
    job["ticket_match_confidence"] = None


def _confidence_meets_minimum(confidence: str, minimum: str) -> bool:
    return SEMANTIC_CONFIDENCE_RANK.get(confidence, 0) >= SEMANTIC_CONFIDENCE_RANK.get(
        minimum, 0
    )


def apply_semantic_matches(
    jobs: list[dict[str, Any]],
    semantic_data: dict[str, Any],
    issues: list[dict[str, Any]],
    min_confidence: str = DEFAULT_SEMANTIC_MIN_CONFIDENCE,
) -> int:
    """Apply Claude semantic matches to jobs without a rule-based ticket_link."""
    issue_map = _issue_by_key(issues)
    matches_by_job = {
        str(entry.get("job_id", "")): entry
        for entry in semantic_data.get("matches", [])
        if entry.get("job_id")
    }
    applied = 0

    for job in jobs:
        if job.get("ticket_link"):
            continue

        job_id = str(job.get("job_id", ""))
        entry = matches_by_job.get(job_id)
        if not entry:
            continue

        confidence = str(entry.get("confidence", "none")).lower()
        if confidence not in ACCEPTED_SEMANTIC_CONFIDENCES:
            confidence = "none"
        if not _confidence_meets_minimum(confidence, min_confidence):
            continue

        ticket_key = str(entry.get("ticket_key") or "").strip()
        if not ticket_key or ticket_key.lower() == "null":
            continue

        issue = issue_map.get(ticket_key)
        if not issue:
            print(
                f"[WARN] Semantic match for job {job_id} references unknown ticket {ticket_key}",
                file=sys.stderr,
            )
            continue

        job["ticket_link"] = issue["ticket_url"]
        job["is_open"] = issue.get("is_open", True)
        job["ticket_match_method"] = "semantic"
        job["ticket_match_confidence"] = confidence
        job["ticket_match_rationale"] = str(entry.get("rationale") or "").strip() or None
        applied += 1

    return applied


def assign_links(
    report: dict[str, Any],
    jira_data: dict[str, Any],
    min_score: int = DEFAULT_MIN_SCORE,
    semantic_matches: dict[str, Any] | None = None,
    semantic_min_confidence: str = DEFAULT_SEMANTIC_MIN_CONFIDENCE,
) -> dict[str, Any]:
    issues = jira_data.get("issues", [])
    if not issues:
        raise ValueError("Jira issue list is empty; cannot assign ticket_link")

    report["jira_sprint_tickets"] = jira_data
    jobs = report.get("job_summaries", [])

    for job in jobs:
        best, score = pick_issue(job, issues)
        job["ticket_match_score"] = score
        if score >= min_score:
            job["ticket_link"] = best["ticket_url"]
            job["is_open"] = best.get("is_open", True)
            job["ticket_match_method"] = "rule"
            job["ticket_match_confidence"] = None
            job["ticket_match_rationale"] = None
        else:
            _clear_ticket_fields(job)

    if semantic_matches:
        apply_semantic_matches(jobs, semantic_matches, issues, semantic_min_confidence)

    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Assign ticket_link in batch reports")
    parser.add_argument("report", help="Path to batch report JSON")
    parser.add_argument("--jira-issues", required=True, help="Path to jira sprint issues JSON")
    parser.add_argument("--in-place", action="store_true", help="Overwrite the report file")
    parser.add_argument(
        "--min-score",
        type=int,
        default=DEFAULT_MIN_SCORE,
        help=f"Minimum rule match score to assign ticket_link (default: {DEFAULT_MIN_SCORE})",
    )
    parser.add_argument(
        "--semantic-matches",
        help="Path to semantic match JSON produced by semantic_match_tickets.py",
    )
    parser.add_argument(
        "--semantic-min-confidence",
        default=DEFAULT_SEMANTIC_MIN_CONFIDENCE,
        choices=sorted(ACCEPTED_SEMANTIC_CONFIDENCES - {"none"}),
        help=(
            "Minimum Claude confidence to accept a semantic match "
            f"(default: {DEFAULT_SEMANTIC_MIN_CONFIDENCE})"
        ),
    )
    args = parser.parse_args(argv)

    with open(args.report) as f:
        report = json.load(f)
    with open(args.jira_issues) as f:
        jira_data = json.load(f)

    semantic_data = None
    if args.semantic_matches:
        with open(args.semantic_matches) as f:
            semantic_data = json.load(f)

    try:
        report = assign_links(
            report,
            jira_data,
            min_score=args.min_score,
            semantic_matches=semantic_data,
            semantic_min_confidence=args.semantic_min_confidence,
        )
    except ValueError as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        return 1

    output_path = args.report if args.in_place else args.report + ".with_tickets.json"
    with open(output_path, "w") as f:
        json.dump(report, f, indent=2)
        f.write("\n")

    jobs = report.get("job_summaries", [])
    rule_assigned = sum(1 for j in jobs if j.get("ticket_match_method") == "rule")
    semantic_assigned = sum(1 for j in jobs if j.get("ticket_match_method") == "semantic")
    unmatched = len(jobs) - rule_assigned - semantic_assigned
    print(
        f"[OK] Assigned ticket_link for {rule_assigned + semantic_assigned} job(s) "
        f"(rule={rule_assigned}, semantic={semantic_assigned}, unmatched={unmatched})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
