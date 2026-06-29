#!/usr/bin/env python3
"""Fetch Jira issues via JQL."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Any
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv
from requests.auth import HTTPBasicAuth

PAGE_SIZE = 100
FIELDS = ["summary", "status", "description", "created", "assignee", "priority", "issuetype"]


def search_sprint_issues(
    base_url: str,
    auth: HTTPBasicAuth,
    board_id: int,
    sprint_id: int,
    jql: str,
) -> list[dict]:
    issues: list[dict] = []
    start_at = 0

    while True:
        response = requests.get(
            f"{base_url}/rest/agile/1.0/board/{board_id}/sprint/{sprint_id}/issue",
            params={
                "jql": jql,
                "startAt": start_at,
                "maxResults": PAGE_SIZE,
                "fields": ",".join(FIELDS),
            },
            auth=auth,
            headers={"Accept": "application/json"},
            timeout=60,
        )
        response.raise_for_status()
        data = response.json()

        batch = data.get("issues", [])
        issues.extend(batch)

        total = data.get("total", 0)
        if start_at + len(batch) >= total or not batch:
            break

        start_at += len(batch)

    return issues


def get_active_sprints(
    base_url: str, auth: HTTPBasicAuth, board_id: int
) -> list[dict]:
    response = requests.get(
        f"{base_url}/rest/agile/1.0/board/{board_id}/sprint",
        params={"state": "active"},
        auth=auth,
        headers={"Accept": "application/json"},
        timeout=60,
    )
    response.raise_for_status()
    return response.json().get("values", [])


def build_jql(
    project_id: str, *, active_sprint: bool = False, include_done: bool = False
) -> str:
    filters = [f'project = "{project_id}"']
    if not include_done:
        filters.append("statusCategory != Done")
    if active_sprint:
        filters.append("sprint in openSprints()")
    return f'{" AND ".join(filters)} ORDER BY created DESC'


def parse_board_url(board_url: str) -> tuple[str, str, int]:
    """Return (base_url, project_id, board_id) from a Jira board URL."""
    parsed = urlparse(board_url.strip())
    if not parsed.scheme or not parsed.netloc:
        raise ValueError(f"Invalid Jira board URL: {board_url}")
    base_url = f"{parsed.scheme}://{parsed.netloc}"
    match = re.search(r"/projects/([^/]+)/boards/(\d+)", parsed.path)
    if not match:
        raise ValueError(
            f"Cannot parse project key and board ID from Jira board URL: {board_url}"
        )
    return base_url, match.group(1), int(match.group(2))


def get_client(
    board_url: str | None = None,
) -> tuple[str, HTTPBasicAuth, str, int | None]:
    load_dotenv()

    board_id: int | None = None
    if board_url:
        base_url, project_id, board_id = parse_board_url(board_url)
    else:
        base_url = os.environ.get("JIRA_URL", "").rstrip("/")
        project_id = os.environ.get("JIRA_PROJECT_ID", "").strip()

    email = os.environ.get("JIRA_EMAIL", "")
    token = os.environ.get("JIRA_API_TOKEN", "")

    missing = [
        name
        for name, value in [
            ("JIRA_URL", base_url),
            ("JIRA_EMAIL", email),
            ("JIRA_API_TOKEN", token),
            ("JIRA_PROJECT_ID", project_id),
        ]
        if not value
    ]
    if missing:
        print(
            f"Missing required environment variable(s): {', '.join(missing)}",
            file=sys.stderr,
        )
        print(
            "Set JIRA_EMAIL and JIRA_API_TOKEN in the environment. "
            "Provide --board-url or set JIRA_URL and JIRA_PROJECT_ID.",
            file=sys.stderr,
        )
        sys.exit(1)

    return base_url, HTTPBasicAuth(email, token), project_id, board_id


def search_issues(base_url: str, auth: HTTPBasicAuth, jql: str) -> list[dict]:
    issues: list[dict] = []
    next_page_token: str | None = None

    while True:
        body: dict = {
            "jql": jql,
            "maxResults": PAGE_SIZE,
            "fields": FIELDS,
        }
        if next_page_token:
            body["nextPageToken"] = next_page_token

        response = requests.post(
            f"{base_url}/rest/api/3/search/jql",
            json=body,
            auth=auth,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            timeout=60,
        )
        response.raise_for_status()
        data = response.json()

        batch = data.get("issues", [])
        issues.extend(batch)

        if data.get("isLast", True) or not batch:
            break

        next_page_token = data.get("nextPageToken")
        if not next_page_token:
            break

    return issues


def search_board_issues(
    base_url: str, auth: HTTPBasicAuth, board_id: int, jql: str
) -> list[dict]:
    issues: list[dict] = []
    start_at = 0

    while True:
        response = requests.get(
            f"{base_url}/rest/agile/1.0/board/{board_id}/issue",
            params={
                "jql": jql,
                "startAt": start_at,
                "maxResults": PAGE_SIZE,
                "fields": ",".join(FIELDS),
            },
            auth=auth,
            headers={"Accept": "application/json"},
            timeout=60,
        )
        response.raise_for_status()
        data = response.json()

        batch = data.get("issues", [])
        issues.extend(batch)

        total = data.get("total", 0)
        if start_at + len(batch) >= total or not batch:
            break

        start_at += len(batch)

    return issues


def display_name(user: dict | None) -> str:
    if not user:
        return "-"
    return user.get("displayName") or user.get("emailAddress") or "-"


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


def format_issue(issue: dict, base_url: str) -> dict:
    fields = issue.get("fields", {})
    status = fields.get("status") or {}
    assignee = fields.get("assignee")
    key = issue.get("key")
    status_name = status.get("name")
    status_category = (status.get("statusCategory") or {}).get("name")
    is_open = status_category != "Done" if status_category else True
    return {
        "key": key,
        "summary": fields.get("summary"),
        "status": status_name,
        "status_category": status_category,
        "type": (fields.get("issuetype") or {}).get("name"),
        "priority": (fields.get("priority") or {}).get("name"),
        "assignee": display_name(assignee) if assignee else None,
        "created": fields.get("created"),
        "ticket_url": f"{base_url.rstrip('/')}/browse/{key}" if key else "",
        "is_open": is_open,
    }


def format_batch_issue(
    issue: dict, base_url: str, sprint_name: str = ""
) -> dict:
    fields = issue.get("fields", {})
    status = fields.get("status") or {}
    key = issue.get("key")
    status_name = status.get("name")
    status_category = (status.get("statusCategory") or {}).get("name")
    is_open = status_category != "Done" if status_category else True
    return {
        "key": key,
        "summary": fields.get("summary", "") or "",
        "description": _plain_description(fields.get("description")),
        "status": status_name,
        "sprint_name": sprint_name,
        "ticket_url": f"{base_url.rstrip('/')}/browse/{key}" if key else "",
        "is_open": is_open,
    }


def prepare_batch_output(sprints: list[dict], issues: list[dict]) -> dict:
    """Deduplicate and shape issues for batch RCA ticket matching."""
    normalized_sprints = []
    for sprint in sprints:
        if isinstance(sprint, dict) and "id" in sprint:
            normalized_sprints.append({"id": sprint["id"], "name": sprint.get("name", "")})

    batch_issues: list[dict] = []
    seen: set[str] = set()
    for issue in issues:
        key = str(issue.get("key", ""))
        if not key or key in seen:
            continue
        seen.add(key)
        batch_issues.append(issue)

    return {"sprints": normalized_sprints, "issues": batch_issues}


def print_results(result: dict) -> None:
    print(json.dumps(result, indent=2))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="List Jira issues for a project."
    )
    parser.add_argument(
        "--board-id",
        type=int,
        metavar="ID",
        help="Filter to issues on this board (e.g. 1290 from .../boards/1290)",
    )
    parser.add_argument(
        "--active-sprint",
        action="store_true",
        help="Only include issues in open (active) sprints",
    )
    parser.add_argument(
        "--include-done",
        action="store_true",
        help="Include issues in the Done status category (excluded by default)",
    )
    parser.add_argument(
        "--board-url",
        metavar="URL",
        help="Jira board URL (derives base URL, project key, and board ID)",
    )
    parser.add_argument(
        "--output",
        "-o",
        metavar="PATH",
        help="Write batch-ready JSON (sprints + issues) to this file",
    )
    return parser.parse_args()


def fetch_board_issues(
    base_url: str,
    auth: HTTPBasicAuth,
    project_id: str,
    *,
    board_id: int | None = None,
    active_sprint: bool = False,
    include_done: bool = False,
) -> dict:
    jql = build_jql(
        project_id,
        active_sprint=active_sprint and not board_id,
        include_done=include_done,
    )

    sprints: list[dict] = []
    raw_issues: list[dict] = []
    batch_issues: list[dict] = []
    if board_id and active_sprint:
        active_sprints = get_active_sprints(base_url, auth, board_id)
        if not active_sprints:
            print("No active sprint found on this board.", file=sys.stderr)
            sys.exit(1)
        for sprint in active_sprints:
            print(
                f"Active sprint: {sprint['name']} (id={sprint['id']})",
                file=sys.stderr,
            )
            sprints.append({"id": sprint["id"], "name": sprint["name"]})
            sprint_name = sprint.get("name", "")
            for issue in search_sprint_issues(
                base_url, auth, board_id, sprint["id"], jql
            ):
                raw_issues.append(issue)
                batch_issues.append(
                    format_batch_issue(issue, base_url, sprint_name)
                )
    elif board_id:
        raw_issues = search_board_issues(base_url, auth, board_id, jql)
        batch_issues = [format_batch_issue(issue, base_url) for issue in raw_issues]
    else:
        raw_issues = search_issues(base_url, auth, jql)
        batch_issues = [format_batch_issue(issue, base_url) for issue in raw_issues]

    return {
        "project": project_id,
        "board_id": board_id,
        "active_sprint": active_sprint,
        "include_done": include_done,
        "jql": jql,
        "sprints": sprints or None,
        "count": len(raw_issues),
        "issues": [format_issue(issue, base_url) for issue in raw_issues],
        "batch_issues": batch_issues,
    }


def main() -> None:
    args = parse_args()
    try:
        base_url, auth, project_id, parsed_board_id = get_client(args.board_url)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)

    board_id = args.board_id if args.board_id is not None else parsed_board_id

    scope_parts = []
    if board_id:
        scope_parts.append(f"board {board_id}")
    else:
        scope_parts.append(f"project {project_id}")
    if args.active_sprint:
        scope_parts.append("active sprint only")
    if args.include_done:
        scope_parts.append("including Done")
    else:
        scope_parts.append("excluding Done")
    print(f"Searching {', '.join(scope_parts)}...", file=sys.stderr)

    result = fetch_board_issues(
        base_url,
        auth,
        project_id,
        board_id=board_id,
        active_sprint=args.active_sprint,
        include_done=args.include_done,
    )

    if args.output:
        batch_data = prepare_batch_output(
            result.get("sprints") or [],
            result["batch_issues"],
        )
        with open(args.output, "w") as f:
            json.dump(batch_data, f, indent=2)
            f.write("\n")
        print(
            f"Wrote {len(batch_data.get('issues', []))} issue(s) to {args.output}",
            file=sys.stderr,
        )
    else:
        print_results(result)


if __name__ == "__main__":
    main()
