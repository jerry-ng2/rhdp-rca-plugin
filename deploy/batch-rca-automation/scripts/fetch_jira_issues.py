"""Fetch Jira board issues via REST API and JQL for batch RCA ticket matching."""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from normalize_jira_sprint_issues import normalize_data

CLOSED_STATUSES = ("Closed", "Done", "Resolved", "Cancelled")
ISSUE_FIELDS = ("summary", "status", "description")
PAGE_SIZE = 100


def _auth_header(email: str, api_token: str) -> str:
    credentials = base64.b64encode(f"{email}:{api_token}".encode()).decode()
    return f"Basic {credentials}"


def _request(
    method: str,
    url: str,
    email: str,
    api_token: str,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": _auth_header(email, api_token),
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        raise RuntimeError(f"Jira API {method} {url} failed ({exc.code}): {detail}") from exc


def build_status_jql() -> str:
    closed = ", ".join(f'"{status}"' for status in CLOSED_STATUSES)
    return f"status NOT IN ({closed})"


def build_project_jql(project_key: str) -> str:
    return f"project = {project_key} AND {build_status_jql()} ORDER BY updated DESC"


def fetch_active_sprints(base_url: str, board_id: str, email: str, api_token: str) -> list[dict[str, Any]]:
    query = urllib.parse.urlencode({"state": "active"})
    url = f"{base_url.rstrip('/')}/rest/agile/1.0/board/{board_id}/sprint?{query}"
    payload = _request("GET", url, email, api_token)
    sprints = []
    for sprint in payload.get("values", []):
        if isinstance(sprint, dict) and "id" in sprint:
            sprints.append({"id": sprint["id"], "name": sprint.get("name", "")})
    return sprints


def fetch_board_issues_agile(
    base_url: str,
    board_id: str,
    email: str,
    api_token: str,
) -> list[dict[str, Any]]:
    """Fetch board-scoped issues; JQL is AND-combined with the board filter."""
    jql = build_status_jql()
    issues: list[dict[str, Any]] = []
    start_at = 0

    while True:
        query = urllib.parse.urlencode(
            {
                "jql": jql,
                "startAt": start_at,
                "maxResults": PAGE_SIZE,
                "fields": ",".join(ISSUE_FIELDS),
            }
        )
        url = f"{base_url.rstrip('/')}/rest/agile/1.0/board/{board_id}/issue?{query}"
        payload = _request("GET", url, email, api_token)
        page_issues = payload.get("issues", [])
        issues.extend(page_issues)

        if not page_issues:
            break

        start_at += len(page_issues)
        total = payload.get("total", 0)
        if start_at >= total:
            break

    return issues


def fetch_issues_search_jql(
    base_url: str,
    jql: str,
    email: str,
    api_token: str,
) -> list[dict[str, Any]]:
    url = f"{base_url.rstrip('/')}/rest/api/3/search/jql"
    issues: list[dict[str, Any]] = []
    next_page_token: str | None = None

    while True:
        body: dict[str, Any] = {
            "jql": jql,
            "maxResults": PAGE_SIZE,
            "fields": list(ISSUE_FIELDS),
        }
        if next_page_token:
            body["nextPageToken"] = next_page_token

        payload = _request("POST", url, email, api_token, body)
        page_issues = payload.get("issues", [])
        issues.extend(page_issues)

        if payload.get("isLast", True):
            break

        next_page_token = payload.get("nextPageToken")
        if not next_page_token:
            break

    return issues


def fetch_board_issues(
    base_url: str,
    board_id: str,
    project_key: str,
    email: str,
    api_token: str,
) -> list[dict[str, Any]]:
    issues = fetch_board_issues_agile(base_url, board_id, email, api_token)
    if issues:
        print(
            f"Fetched {len(issues)} issue(s) from agile board API with JQL: {build_status_jql()}",
            file=sys.stderr,
        )
        return issues

    project_jql = build_project_jql(project_key)
    print(
        "Agile board issue API returned no issues; falling back to search/jql with "
        f"project scope: {project_jql}",
        file=sys.stderr,
    )
    issues = fetch_issues_search_jql(base_url, project_jql, email, api_token)
    print(f"search/jql returned {len(issues)} issue(s)", file=sys.stderr)
    return issues


def fetch_jira_data(
    base_url: str,
    board_id: str,
    project_key: str,
    email: str,
    api_token: str,
) -> dict[str, Any]:
    sprints = fetch_active_sprints(base_url, board_id, email, api_token)
    issues = fetch_board_issues(base_url, board_id, project_key, email, api_token)
    return normalize_data({"sprints": sprints, "issues": issues}, base_url)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch Jira board issues via REST API and JQL")
    parser.add_argument("--board-id", required=True, help="Jira board ID")
    parser.add_argument("--project-key", default="GPTEINFRA", help="Jira project key for search/jql fallback")
    parser.add_argument("--base-url", default="https://redhat.atlassian.net")
    parser.add_argument("--output", "-o", required=True, help="Output JSON path")
    args = parser.parse_args(argv)

    email = os.environ.get("JIRA_EMAIL", "").strip()
    api_token = os.environ.get("JIRA_API_TOKEN", "").strip()
    if not email or not api_token:
        print(
            "JIRA_EMAIL and JIRA_API_TOKEN must be set in the environment "
            "(e.g. via .claude/settings.json env)",
            file=sys.stderr,
        )
        return 1

    try:
        data = fetch_jira_data(args.base_url, args.board_id, args.project_key, email, api_token)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    with open(args.output, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")

    print(f"Fetched {len(data.get('issues', []))} issue(s)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
