"""Print Jira sprint issues in a human-readable format for batch logs."""

from __future__ import annotations

import json
import sys


def main(argv: list[str] | None = None) -> int:
    args = argv or sys.argv
    if len(args) < 2:
        print("Usage: print_jira_issues.py <jira_issues.json>", file=sys.stderr)
        return 1

    with open(args[1]) as f:
        data = json.load(f)

    sprints = data.get("sprints", [])
    issues = data.get("issues", [])

    if sprints:
        sprint_names = ", ".join(f"{s.get('name', '?')} (id={s.get('id', '?')})" for s in sprints)
        print(f"[JIRA] Active sprint(s): {sprint_names}")
    else:
        print("[JIRA] Active sprint(s): none")

    if not issues:
        print("[JIRA] Sprint issues: (none)")
        return 0

    print(f"[JIRA] Sprint issues ({len(issues)} total):")
    for issue in issues:
        key = issue.get("key", "?")
        status = issue.get("status", "?")
        summary = issue.get("summary", "")
        url = issue.get("ticket_url", "")
        print(f"  - {key} [{status}] {summary}")
        if url:
            print(f"    {url}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
