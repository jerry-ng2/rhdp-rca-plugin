"""Normalize Jira sprint issue JSON from MCP or REST API into batch report shape."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any


def _plain_description(description: Any) -> str:
    if description is None:
        return ""
    if isinstance(description, str):
        return description
    if isinstance(description, dict):
        parts: list[str] = []
        for block in description.get("content", []):
            for item in block.get("content", []):
                text = item.get("text")
                if text:
                    parts.append(text)
        return " ".join(parts)
    return str(description)


def normalize_issue(issue: dict[str, Any], base_url: str, sprint_name: str = "") -> dict[str, str]:
    if "fields" in issue:
        key = issue["key"]
        fields = issue["fields"]
        status = fields.get("status", {})
        status_name = status.get("name", "") if isinstance(status, dict) else str(status)
        return {
            "key": key,
            "summary": fields.get("summary", ""),
            "description": _plain_description(fields.get("description")),
            "status": status_name,
            "sprint_name": sprint_name,
            "ticket_url": f"{base_url.rstrip('/')}/browse/{key}",
        }

    key = issue.get("key", "")
    ticket_url = issue.get("ticket_url") or (
        f"{base_url.rstrip('/')}/browse/{key}" if key else ""
    )
    return {
        "key": key,
        "summary": issue.get("summary", ""),
        "description": issue.get("description", ""),
        "status": issue.get("status", ""),
        "sprint_name": issue.get("sprint_name", sprint_name),
        "ticket_url": ticket_url,
    }


def normalize_data(data: dict[str, Any], base_url: str) -> dict[str, Any]:
    sprints = []
    for sprint in data.get("sprints", []):
        if isinstance(sprint, dict) and "id" in sprint:
            sprints.append({"id": sprint["id"], "name": sprint.get("name", "")})

    issues = []
    seen: set[str] = set()
    for issue in data.get("issues", []):
        normalized = normalize_issue(issue, base_url)
        key = normalized.get("key", "")
        if not key or key in seen:
            continue
        seen.add(key)
        issues.append(normalized)

    return {"sprints": sprints, "issues": issues}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Normalize Jira sprint issue JSON")
    parser.add_argument("input", help="Path to raw Jira JSON")
    parser.add_argument("--base-url", default="https://redhat.atlassian.net")
    parser.add_argument("--output", "-o", help="Output path (default: stdout)")
    args = parser.parse_args(argv)

    with open(args.input) as f:
        data = json.load(f)

    normalized = normalize_data(data, args.base_url)
    output = json.dumps(normalized, indent=2) + "\n"

    if args.output:
        with open(args.output, "w") as f:
            f.write(output)
    else:
        print(output, end="")

    return 0


if __name__ == "__main__":
    sys.exit(main())
