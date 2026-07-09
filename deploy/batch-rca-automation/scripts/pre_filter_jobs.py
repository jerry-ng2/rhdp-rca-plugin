"""Pre-filter jobs against recent known results before spawning Claude agents.

Matches on catalog_item + error_message similarity. A job is only pre-matched when:
  1. Its catalog_item matches a recent high-confidence result unambiguously
     (only one distinct root_cause_category for that catalog_item in the window)
  2. Its error_message is similar enough to the original job's error_message
     (SequenceMatcher ratio >= MATCH_THRESHOLD)

If either side is missing an error_message the job proceeds to full RCA.
"""

from __future__ import annotations

import argparse
import difflib
import json
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg2
import psycopg2.sql
from utils import connect_db, load_config

MATCH_THRESHOLD = 0.75


def extract_catalog_item(job_name: str) -> str | None:
    """Extract catalog_item from job_name.

    Handles the RHPDS format: 'RHPDS {platform}.{catalog_item}.{env}-{guid}-{action} ...'
    """
    name = job_name.removeprefix("RHPDS ").strip()
    if " " in name:
        name = name.split(maxsplit=1)[0]
    parts = name.split(".")
    if len(parts) >= 3:
        return ".".join(parts[1:-1])
    if len(parts) == 2:
        return parts[1].split("-")[0] if "-" in parts[1] else parts[1]
    return None


_MAX_CMP_LEN = 5000


def error_similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a[:_MAX_CMP_LEN], b[:_MAX_CMP_LEN]).ratio()


def fetch_filter_context(
    conn: Any,
    results_table: str,
    source_table: str,
    job_ids: list[int],
    lookback_hours: int = 4,
) -> tuple[list[dict[str, Any]], dict[int, dict[str, Any]]]:
    """Fetch recent known results and incoming job metadata in one pass.

    Returns (recent_results, job_metadata) where:
      - recent_results: high-confidence analyzed results with error_message from the source table
      - job_metadata: {job_id: {job_name, error_message}} for the incoming jobs
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    cutoff_batch_id = f"batch_{cutoff.strftime('%Y%m%d_%H%M%S')}"

    with conn.cursor() as cur:
        cur.execute(
            psycopg2.sql.SQL(
                """SELECT r.id, r.catalog_item, r.root_cause_category,
                          r.root_cause_summary, e.error_message
                   FROM {results} r
                   LEFT JOIN {source} e ON r.job_id::text = e.job_id::text
                   WHERE r.confidence = 'high'
                     AND r.batch_id >= %s
                     AND r.status = 'analyzed'
                   ORDER BY r.batch_id DESC"""
            ).format(
                results=psycopg2.sql.Identifier(results_table),
                source=psycopg2.sql.Identifier(source_table),
            ),
            (cutoff_batch_id,),
        )
        recent_results = [dict(row) for row in cur.fetchall()]

        cur.execute(
            psycopg2.sql.SQL(
                """SELECT job_id, job_name, error_message
                   FROM {} WHERE job_id = ANY(%s) AND job_finished >= %s"""
            ).format(psycopg2.sql.Identifier(source_table)),
            (job_ids, cutoff),
        )
        job_metadata = {
            row["job_id"]: {
                "job_name": row["job_name"],
                "error_message": row["error_message"],
            }
            for row in cur.fetchall()
            if row["job_name"]
        }

    return recent_results, job_metadata


def build_catalog_index(recent_results: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Build catalog_item -> result index, excluding ambiguous items.

    A catalog_item is excluded when it has more than one distinct root_cause_category
    in the lookback window — the same item is failing for different reasons so we
    cannot safely pre-match new jobs against it.
    """
    grouped: dict[str, list[dict[str, Any]]] = {}
    for result in recent_results:
        ci = result["catalog_item"]
        if not ci:
            continue
        grouped.setdefault(ci, []).append(result)

    return {
        ci: results[0]  # most recent (results ordered by batch_id DESC)
        for ci, results in grouped.items()
        if len({r["root_cause_category"] for r in results}) == 1
    }


def pre_filter(
    job_metadata: dict[int, dict[str, Any]],
    catalog_index: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    analyze = []
    pre_matched = []

    for job_id, meta in job_metadata.items():
        extracted = extract_catalog_item(meta["job_name"])
        matched_result = catalog_index.get(extracted) if extracted else None

        if matched_result:
            new_err = meta.get("error_message") or ""
            known_err = matched_result.get("error_message") or ""

            if not new_err or not known_err:
                matched_result = None  # can't compare — send to full RCA
            elif error_similarity(new_err, known_err) < MATCH_THRESHOLD:
                matched_result = None  # different error — send to full RCA

        if matched_result:
            pre_matched.append(
                {
                    "job_id": job_id,
                    "matched_result_id": matched_result["id"],
                    "catalog_item": matched_result["catalog_item"],
                    "root_cause_category": matched_result["root_cause_category"],
                    "match_reason": "pre_filter_catalog_item+error_message",
                    "recent_result_summary": matched_result["root_cause_summary"][:200],
                }
            )
        else:
            analyze.append(job_id)

    return {"analyze": analyze, "pre_matched": pre_matched}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Pre-filter jobs against known results")
    parser.add_argument(
        "--input",
        type=str,
        default=None,
        help="File with newline-separated job IDs (default: stdin)",
    )
    parser.add_argument(
        "--lookback-hours", type=int, default=4, help="Lookback window (default: 4)"
    )
    args = parser.parse_args(argv)

    if args.input:
        with open(args.input) as f:
            raw = f.read()
    else:
        raw = sys.stdin.read()

    job_ids = [int(line.strip()) for line in raw.strip().splitlines() if line.strip()]
    if not job_ids:
        print(json.dumps({"analyze": [], "pre_matched": []}))
        return 0

    try:
        config = load_config(required=("name", "user", "password", "source_table", "results_table"))
    except SystemExit:
        return 1

    try:
        conn = connect_db(config, use_dict_cursor=True)
    except psycopg2.OperationalError as e:
        print(f"Cannot connect to database: {e}", file=sys.stderr)
        return 1

    try:
        recent_results, job_metadata = fetch_filter_context(
            conn,
            config["results_table"],
            config["source_table"],
            job_ids,
            args.lookback_hours,
        )
        if not recent_results:
            print(json.dumps({"analyze": job_ids, "pre_matched": []}))
            return 0

        catalog_index = build_catalog_index(recent_results)

        unknown_ids = [jid for jid in job_ids if jid not in job_metadata]
        result = pre_filter(job_metadata, catalog_index)
        result["analyze"].extend(unknown_ids)

    except psycopg2.Error as e:
        print(f"Query failed: {e}", file=sys.stderr)
        return 1
    finally:
        conn.close()

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
