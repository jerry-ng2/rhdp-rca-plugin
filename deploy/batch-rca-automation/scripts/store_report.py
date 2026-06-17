"""Store batch RCA JSON reports into a local SQLite database."""

from __future__ import annotations

import argparse
import glob
import json
import os
import sqlite3
import sys
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS job_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL,
    job_id TEXT NOT NULL,
    status TEXT,
    root_cause_category TEXT,
    root_cause_summary TEXT,
    confidence TEXT,
    cluster TEXT,
    catalog_item TEXT,
    guid TEXT,
    job_duration_seconds REAL,
    cross_job_pattern TEXT,
    cross_job_pattern_description TEXT,
    ticket_link TEXT,
    is_open INTEGER,
    UNIQUE(batch_id, job_id)
);
"""


MIGRATIONS = [
    "ALTER TABLE job_results ADD COLUMN cross_job_pattern TEXT",
    "ALTER TABLE job_results ADD COLUMN cross_job_pattern_description TEXT",
    "ALTER TABLE job_results ADD COLUMN ticket_link TEXT",
    "ALTER TABLE job_results ADD COLUMN is_open INTEGER",
    "UPDATE job_results SET is_open = 1 WHERE is_open IS NULL",
]


def init_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    for stmt in MIGRATIONS:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            pass  # column already exists
    return conn


def store_report(conn: sqlite3.Connection, report: dict[str, Any], filename: str | None = None) -> bool:
    batch_id = report.get("batch_id")
    if not batch_id and filename:
        base = os.path.splitext(os.path.basename(filename))[0]
        batch_id = base
    if not batch_id:
        print("[ERROR] Cannot determine batch_id", file=sys.stderr)
        return False

    # Build a lookup from job_id -> pattern info for cross_job_patterns
    pattern_by_job: dict[str, dict[str, str | None]] = {}
    for p in report.get("cross_job_patterns", []):
        for jid in p.get("jobs", []):
            pattern_by_job[str(jid)] = {
                "pattern": p.get("pattern"),
                "description": p.get("description"),
            }

    jobs = report.get("job_results") or report.get("job_summaries") or report.get("jobs", [])
    for job in jobs:
        jid = str(job.get("job_id", ""))
        pinfo = pattern_by_job.get(jid, {})
        conn.execute(
            """INSERT OR IGNORE INTO job_results
               (batch_id, job_id, status, root_cause_category, root_cause_summary,
                confidence, cluster, catalog_item, guid, job_duration_seconds,
                cross_job_pattern, cross_job_pattern_description, is_open)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                batch_id,
                jid,
                job.get("status"),
                job.get("root_cause_category"),
                job.get("root_cause_summary"),
                job.get("confidence"),
                job.get("cluster"),
                job.get("catalog_item"),
                job.get("guid"),
                job.get("job_duration_seconds"),
                pinfo.get("pattern"),
                pinfo.get("description"),
                1,
            ),
        )

    conn.commit()
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Store batch RCA reports in SQLite")
    parser.add_argument(
        "report", nargs="?", help="Path to a batch report JSON file"
    )
    parser.add_argument(
        "--db", default=None,
        help="SQLite database path (default: reports/batch_rca.db relative to script)"
    )
    parser.add_argument(
        "--backfill", metavar="DIR",
        help="Load all batch_*.json files from DIR"
    )
    args = parser.parse_args(argv)

    if not args.report and not args.backfill:
        parser.error("Provide a report path or --backfill DIR")

    db_path = args.db
    if not db_path:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        db_path = os.path.join(script_dir, "..", "reports", "batch_rca.db")

    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    conn = init_db(db_path)

    files: list[str] = []
    if args.backfill:
        files = sorted(glob.glob(os.path.join(args.backfill, "batch_*.json")))
        if not files:
            print(f"[WARN] No batch_*.json files found in {args.backfill}", file=sys.stderr)
            conn.close()
            return 0
    elif args.report:
        files = [args.report]

    inserted = 0
    for path in files:
        try:
            with open(path) as f:
                report = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            print(f"[ERROR] Failed to read {path}: {e}", file=sys.stderr)
            continue

        if store_report(conn, report, filename=path):
            bid = report.get("batch_id") or os.path.splitext(os.path.basename(path))[0]
            jobs = report.get("job_results") or report.get("job_summaries") or report.get("jobs", [])
            inserted += 1
            print(f"[OK] Stored {bid} ({len(jobs)} jobs)")

    conn.close()
    print(f"[DONE] {inserted}/{len(files)} report(s) stored in {db_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
