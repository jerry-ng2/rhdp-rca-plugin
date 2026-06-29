"""Semantic Jira ticket matching via Claude for jobs below the rule-based threshold."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from assign_ticket_links import DEFAULT_MIN_SCORE, pick_issue  # noqa: E402

DEFAULT_SEMANTIC_MIN_CONFIDENCE = "medium"
PROMPT_TEMPLATE = """You are matching failed RHDP/AAP jobs to open Jira sprint tickets.

For each job below, pick at most ONE ticket from the Jira list, or use null when no ticket fits.

Matching rules (priority order):
1. Explicit references in ticket text (job_id, guid, catalog_item)
2. Catalog item to workshop name mapping
   - Example: zt-rhel-bu-lab-developer-cnv maps to OpenShift Virtualization / CNV workshops
   - tests.test-empty-config and similar test catalogs usually have no sprint ticket
3. Platform (openshift_cnv, rosa, etc.) vs ticket summary keywords
4. Root cause category vs ticket scope (do not match SSL/DNS maintenance tickets unless the failure is clearly for that workshop)

Do NOT match solely on generic words (OpenShift, SSL, DNS) without catalog/workshop alignment.
Return confidence:
- high: strong catalog/workshop alignment or explicit reference
- medium: reasonable domain alignment with some uncertainty
- low: weak or generic overlap only
- none: no suitable ticket

Return ONLY valid JSON (no markdown fences) with this shape:
{{
  "matches": [
    {{
      "job_id": "2594913",
      "ticket_key": "GPTEINFRA-16879",
      "confidence": "high",
      "rationale": "One sentence explaining the match"
    }}
  ]
}}

Use ticket_key null and confidence none when no ticket fits.

## Jobs needing semantic match

{jobs_json}

## Open Jira tickets

{issues_json}
"""


def _compact_job(job: dict[str, Any]) -> dict[str, Any]:
    fields = (
        "job_id",
        "status",
        "root_cause_category",
        "root_cause_summary",
        "guid",
        "catalog_item",
        "cluster",
        "platform",
        "failing_role",
        "failing_github_path",
        "ticket_match_score",
    )
    return {field: job.get(field) for field in fields if job.get(field) is not None}


def _compact_issue(issue: dict[str, Any]) -> dict[str, Any]:
    fields = ("key", "summary", "description", "status", "ticket_url", "is_open")
    compact = {field: issue.get(field) for field in fields if issue.get(field) is not None}
    if compact.get("description"):
        compact["description"] = str(compact["description"])[:500]
    return compact


def jobs_needing_semantic_match(
    jobs: list[dict[str, Any]],
    issues: list[dict[str, Any]],
    min_score: int,
) -> list[dict[str, Any]]:
    unmatched: list[dict[str, Any]] = []
    for job in jobs:
        _, score = pick_issue(job, issues)
        if score < min_score:
            compact = _compact_job(job)
            compact["ticket_match_score"] = score
            unmatched.append(compact)
    return unmatched


def build_prompt(jobs: list[dict[str, Any]], issues: list[dict[str, Any]]) -> str:
    jobs_json = json.dumps(jobs, indent=2)
    issues_json = json.dumps([_compact_issue(issue) for issue in issues], indent=2)
    return PROMPT_TEMPLATE.format(jobs_json=jobs_json, issues_json=issues_json)


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    if not text:
        raise ValueError("Claude returned empty output")

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence_match:
        return json.loads(fence_match.group(1))

    brace_match = re.search(r"(\{.*\})", text, re.DOTALL)
    if brace_match:
        return json.loads(brace_match.group(1))

    raise ValueError("Could not parse JSON from Claude output")


def validate_matches(
    data: dict[str, Any],
    expected_job_ids: set[str],
    valid_ticket_keys: set[str],
) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError("Semantic match output must be a JSON object")

    raw_matches = data.get("matches", [])
    if not isinstance(raw_matches, list):
        raise ValueError("Semantic match output must include a matches array")

    validated: list[dict[str, Any]] = []
    for entry in raw_matches:
        if not isinstance(entry, dict):
            continue
        job_id = str(entry.get("job_id", "")).strip()
        if job_id not in expected_job_ids:
            print(f"[WARN] Ignoring semantic match for unexpected job_id {job_id}", file=sys.stderr)
            continue

        ticket_key = entry.get("ticket_key")
        confidence = str(entry.get("confidence", "none")).lower()
        rationale = str(entry.get("rationale") or "").strip()

        if ticket_key is None or str(ticket_key).lower() == "null" or confidence == "none":
            validated.append(
                {
                    "job_id": job_id,
                    "ticket_key": None,
                    "confidence": "none",
                    "rationale": rationale or "No suitable Jira ticket found",
                }
            )
            continue

        ticket_key = str(ticket_key).strip()
        if ticket_key not in valid_ticket_keys:
            print(
                f"[WARN] Ignoring semantic match for job {job_id}: unknown ticket {ticket_key}",
                file=sys.stderr,
            )
            continue

        validated.append(
            {
                "job_id": job_id,
                "ticket_key": ticket_key,
                "confidence": confidence,
                "rationale": rationale,
            }
        )

    return {"matches": validated}


def run_claude(prompt: str, repo_root: Path | None = None) -> str:
    cmd = ["claude", "-p", "--dangerously-skip-permissions"]
    kwargs: dict[str, Any] = {
        "input": prompt,
        "text": True,
        "check": False,
        "capture_output": True,
    }
    if repo_root is not None:
        kwargs["cwd"] = repo_root

    result = subprocess.run(cmd, **kwargs)
    if result.returncode != 0:
        stderr = result.stderr.strip() if result.stderr else "unknown error"
        raise RuntimeError(f"claude invocation failed: {stderr}")
    return result.stdout


def semantic_match(
    report: dict[str, Any],
    jira_data: dict[str, Any],
    min_score: int = DEFAULT_MIN_SCORE,
    repo_root: Path | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    issues = jira_data.get("issues", [])
    if not issues:
        raise ValueError("Jira issue list is empty; cannot run semantic matching")

    jobs = report.get("job_summaries", [])
    unmatched = jobs_needing_semantic_match(jobs, issues, min_score=min_score)
    if not unmatched:
        return {"matches": []}

    expected_job_ids = {str(job["job_id"]) for job in unmatched}
    valid_ticket_keys = {str(issue.get("key", "")) for issue in issues if issue.get("key")}

    if dry_run:
        return {
            "matches": [
                {
                    "job_id": job["job_id"],
                    "ticket_key": None,
                    "confidence": "none",
                    "rationale": "dry-run",
                }
                for job in unmatched
            ]
        }

    prompt = build_prompt(unmatched, issues)
    raw_output = run_claude(prompt, repo_root=repo_root)
    parsed = _extract_json(raw_output)
    return validate_matches(parsed, expected_job_ids, valid_ticket_keys)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run Claude semantic Jira matching for jobs below the rule threshold"
    )
    parser.add_argument("report", help="Path to batch report JSON")
    parser.add_argument("--jira-issues", required=True, help="Path to jira sprint issues JSON")
    parser.add_argument("--output", required=True, help="Path to write semantic matches JSON")
    parser.add_argument(
        "--min-rule-score",
        type=int,
        default=DEFAULT_MIN_SCORE,
        help=f"Only semantically match jobs with rule score below this (default: {DEFAULT_MIN_SCORE})",
    )
    parser.add_argument(
        "--repo-root",
        help="Repository root for claude invocation (defaults to two levels above scripts/)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Skip Claude and write placeholder none matches for unmatched jobs",
    )
    args = parser.parse_args(argv)

    with open(args.report) as f:
        report = json.load(f)
    with open(args.jira_issues) as f:
        jira_data = json.load(f)

    repo_root = Path(args.repo_root) if args.repo_root else Path(__file__).resolve().parents[2]

    try:
        result = semantic_match(
            report,
            jira_data,
            min_score=args.min_rule_score,
            repo_root=repo_root,
            dry_run=args.dry_run,
        )
    except (ValueError, RuntimeError, json.JSONDecodeError) as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        return 1

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w") as f:
        json.dump(result, f, indent=2)
        f.write("\n")

    matched = sum(
        1
        for entry in result.get("matches", [])
        if entry.get("ticket_key") and entry.get("confidence") not in (None, "none")
    )
    print(
        f"[OK] Semantic matches written to {output_path} "
        f"({matched} candidate match(es), {len(result.get('matches', []))} job(s) evaluated)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
