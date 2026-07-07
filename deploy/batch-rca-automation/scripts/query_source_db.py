"""Query source PostgreSQL table for unanalyzed job IDs.

Reads events where ai_processed = FALSE and outputs distinct job IDs,
one per line. Joins aap2_user_url to resolve per-cluster bastion targets.
The batch script passes these to Claude agents for RCA.
"""

from __future__ import annotations

import argparse
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
        "table": os.environ.get("SOURCE_DB_TABLE", ""),
        "bastion_table": os.environ.get("SOURCE_DB_BASTION_TABLE", "aap2_user_url"),
    }

    errors = []
    for key in ("name", "user", "password", "table"):
        if not config[key]:
            env_var = f"SOURCE_DB_{key.upper()}"
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
        cursor_factory=psycopg2.extras.RealDictCursor,
    )


def query_jobs(
    conn: Any,
    events_table: str,
    bastion_table: str,
    since: str | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    conditions = ["(e.ai_processed IS NULL OR e.ai_processed = FALSE)"]
    params: list[Any] = []

    if since:
        conditions.append("e.job_started >= %s")
        params.append(since)

    suffix = ""
    if limit is not None:
        suffix = " LIMIT %s"
        params.append(limit)

    query = psycopg2.sql.SQL(
        "SELECT DISTINCT ON (e.job_id) "
        "e.job_id, e.cluster_name, "
        "u.bastion_hostname, u.bastion_ssh_port "
        "FROM {} e "
        "LEFT JOIN {} u ON e.cluster_name = u.cluster_name "
        "WHERE " + " AND ".join(conditions) + " "
        "ORDER BY e.job_id DESC, e.job_started DESC" + suffix
    ).format(
        psycopg2.sql.Identifier(events_table),
        psycopg2.sql.Identifier(bastion_table),
    )

    with conn.cursor() as cur:
        cur.execute(query, params)
        rows = cur.fetchall()

    jobs: list[dict[str, Any]] = []
    for row in rows:
        job = {
            "job_id": row["job_id"],
            "cluster_name": row["cluster_name"],
            "bastion_hostname": row["bastion_hostname"],
            "bastion_ssh_port": row["bastion_ssh_port"],
        }
        if not row["bastion_hostname"]:
            print(
                f"[WARN] No bastion mapping in {bastion_table} for job "
                f"{row['job_id']} (cluster_name={row['cluster_name']!r})",
                file=sys.stderr,
            )
        jobs.append(job)

    return jobs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Query source DB for unanalyzed job IDs"
    )
    parser.add_argument(
        "--since", type=str, default=None,
        help="Only include events after this timestamp (e.g. '2024-06-01 08:00:00')"
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Maximum number of job IDs to return"
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Output JSON array with job_id, cluster_name, and bastion fields"
    )
    parser.add_argument(
        "--output", "-o", type=str, default=None,
        help="Save output to this file (default: print to stdout)"
    )
    args = parser.parse_args(argv)

    try:
        config = load_config()
    except SystemExit:
        return 1

    conn = None
    try:
        conn = connect_db(config)
        jobs = query_jobs(
            conn,
            config["table"],
            config["bastion_table"],
            args.since,
            args.limit,
        )
    except psycopg2.OperationalError as e:
        print(f"Cannot connect to source database: {e}", file=sys.stderr)
        return 1
    except psycopg2.Error as e:
        print(f"Query failed: {e}", file=sys.stderr)
        return 1
    finally:
        if conn:
            conn.close()

    if args.json:
        output = json.dumps(jobs, default=str)
    else:
        output = "\n".join(str(job["job_id"]) for job in jobs)

    if args.output:
        with open(args.output, "w") as f:
            f.write(output + "\n" if output else "")
        print(f"Saved {len(jobs)} job(s) to {args.output}", file=sys.stderr)
    elif output:
        print(output)

    return 0


if __name__ == "__main__":
    sys.exit(main())
