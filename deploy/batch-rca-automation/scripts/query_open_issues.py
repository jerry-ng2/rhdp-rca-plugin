"""Query open issues from the local SQLite job_results table."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys


def query_open_issues(db_path: str, limit: int = 50) -> list[dict]:
    if not os.path.exists(db_path):
        return []

    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        """SELECT job_id, root_cause_category, root_cause_summary,
                  confidence, catalog_item, cluster, batch_id
           FROM job_results
           ORDER BY batch_id DESC"""
    ).fetchall()
    conn.close()

    if not rows:
        return []

    groups: dict[tuple, dict] = {}
    for row in rows:
        key = (row["root_cause_category"], row["catalog_item"], row["cluster"])
        if key not in groups:
            groups[key] = {
                "root_cause_category": row["root_cause_category"],
                "catalog_item": row["catalog_item"],
                "cluster": row["cluster"],
                "root_cause_summary": row["root_cause_summary"],
                "confidence": row["confidence"],
                "occurrence_count": 0,
                "job_ids": [],
                "last_seen_batch": row["batch_id"],
            }
        groups[key]["occurrence_count"] += 1
        groups[key]["job_ids"].append(row["job_id"])

    result = sorted(groups.values(), key=lambda g: g["occurrence_count"], reverse=True)
    return result[:limit]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Query open issues from job_results")
    parser.add_argument(
        "--db", default=None,
        help="SQLite database path (default: reports/batch_rca.db relative to script)"
    )
    parser.add_argument(
        "--limit", type=int, default=50,
        help="Maximum number of grouped issues to return (default: 50)"
    )
    args = parser.parse_args(argv)

    db_path = args.db
    if not db_path:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        db_path = os.path.join(script_dir, "..", "reports", "batch_rca.db")

    result = query_open_issues(db_path, limit=args.limit)
    json.dump(result, sys.stdout, indent=2)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
