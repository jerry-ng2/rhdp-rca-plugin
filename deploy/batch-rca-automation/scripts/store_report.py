"""Store batch RCA JSON reports into a results table on the source database."""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from typing import Any

import psycopg2
import psycopg2.extras
import psycopg2.sql
from dotenv import load_dotenv


def load_config() -> dict[str, Any]:
    env_file = os.path.join(os.path.dirname(__file__), "..", ".env")
    if os.path.exists(env_file):
        load_dotenv(env_file)

    config = {
        "host": os.environ.get("SOURCE_DB_HOST", "localhost"),
        "port": int(os.environ.get("SOURCE_DB_PORT", "5432")),
        "name": os.environ.get("SOURCE_DB_NAME", ""),
        "user": os.environ.get("SOURCE_DB_USER", ""),
        "password": os.environ.get("SOURCE_DB_PASSWORD", ""),
        "results_table": os.environ.get("SOURCE_DB_RESULT_TABLE", ""),
    }

    errors = []
    required_keys = {
        "name": "SOURCE_DB_NAME",
        "user": "SOURCE_DB_USER",
        "password": "SOURCE_DB_PASSWORD",
        "results_table": "SOURCE_DB_RESULT_TABLE",
    }
    for key, env_var in required_keys.items():
        if not config[key]:
            errors.append(f"{env_var} is required")
    if errors:
        print("\n".join(errors), file=sys.stderr)
        raise SystemExit(1)

    return config


def connect_db(config: dict[str, Any]) -> Any:
    return psycopg2.connect(
        host=config["host"],
        port=config["port"],
        dbname=config["name"],
        user=config["user"],
        password=config["password"],
    )



def store_report(conn: Any, table: str, report: dict[str, Any], filename: str | None = None) -> bool:
    batch_id = report.get("batch_id")
    if not batch_id and filename:
        base = os.path.splitext(os.path.basename(filename))[0]
        batch_id = base
    if not batch_id:
        print("[ERROR] Cannot determine batch_id", file=sys.stderr)
        return False

    correlated_jobs: dict[str, set[str]] = {}
    descriptions: dict[str, list[str]] = {}

    for p in report.get("cross_job_patterns", []):
        pattern_jobs = [str(j) for j in p.get("jobs", [])]
        desc = p.get("description", "")
        for jid in pattern_jobs:
            correlated_jobs.setdefault(jid, set()).update(
                j for j in pattern_jobs if j != jid
            )
            descriptions.setdefault(jid, []).append(desc)

    for h in report.get("historical_correlations", []):
        current_ids = [str(j) for j in h.get("current_job_ids", [])]
        historical_ids = [str(j) for j in h.get("historical_job_ids", [])]
        desc = h.get("description", "")
        for jid in current_ids:
            correlated_jobs.setdefault(jid, set()).update(
                j for j in current_ids if j != jid
            )
            correlated_jobs[jid].update(historical_ids)
            descriptions.setdefault(jid, []).append(desc)

    jobs = report.get("job_results") or report.get("job_summaries") or report.get("jobs", [])
    with conn.cursor() as cur:
        for job in jobs:
            jid = str(job.get("job_id", ""))
            related = sorted(correlated_jobs.get(jid, set()))
            descs = descriptions.get(jid, [])
            cur.execute(
                psycopg2.sql.SQL(
                    """INSERT INTO {}
                       (batch_id, job_id, status, root_cause_category, root_cause_summary,
                        confidence, catalog_item, job_duration_seconds,
                        cross_job_pattern, cross_job_pattern_description, ticket_link, is_open)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT (batch_id, job_id) DO NOTHING"""
                ).format(psycopg2.sql.Identifier(table)),
                (
                    batch_id,
                    jid,
                    job.get("status"),
                    job.get("root_cause_category"),
                    job.get("root_cause_summary"),
                    job.get("confidence"),
                    job.get("catalog_item"),
                    job.get("job_duration_seconds"),
                    ",".join(related) if related else None,
                    " | ".join(descs) if descs else None,
                    job.get("ticket_link"),
                    job.get("is_open", False),
                ),
            )

    conn.commit()
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Store batch RCA reports in PostgreSQL")
    parser.add_argument(
        "report", nargs="?", help="Path to a batch report JSON file"
    )
    parser.add_argument(
        "--backfill", metavar="DIR",
        help="Load all batch_*.json files from DIR"
    )
    args = parser.parse_args(argv)

    if not args.report and not args.backfill:
        parser.error("Provide a report path or --backfill DIR")

    try:
        config = load_config()
    except SystemExit:
        return 1

    try:
        conn = connect_db(config)
    except psycopg2.OperationalError as e:
        print(f"Cannot connect to database: {e}", file=sys.stderr)
        return 1

    print(f"[INFO] Connected as user: {config['user']} on {config['results_table']}")
    results_table = config["results_table"]

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

        if store_report(conn, results_table, report, filename=path):
            bid = report.get("batch_id") or os.path.splitext(os.path.basename(path))[0]
            jobs = report.get("job_results") or report.get("job_summaries") or report.get("jobs", [])
            inserted += 1
            print(f"[OK] Stored {bid} ({len(jobs)} jobs)")

    conn.close()
    print(f"[DONE] {inserted}/{len(files)} report(s) stored in {results_table}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
