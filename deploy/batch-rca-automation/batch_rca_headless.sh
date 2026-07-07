#!/bin/bash
set -euo pipefail

#############################################
# Batch RCA Analysis - Claude Headless Mode
#############################################
#
# This script:
# 1. Queries source PostgreSQL table for unanalyzed job IDs (ai_processed = FALSE)
# 1a. Pre-fetches logs from per-cluster bastions (via jumpbox ProxyJump)
# 2. Invokes Claude in headless mode to run parallel RCA on those jobs
#
# Requires SOURCE_DB_* env vars (HOST, PORT, NAME, USER, PASSWORD, TABLE)
# set in .claude/settings.json under "env".
#
# Usage:
#   ./batch_rca_headless.sh [--since 'YYYY-MM-DD HH:MM:SS'] [--limit N]
#
# Schedule via cron (every 30 min — each run analyzes the previous 30-min window):
#   7,37 * * * * /path/to/batch_rca_headless.sh >> /tmp/batch_rca.log 2>&1
#

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPORT_DIR="$SCRIPT_DIR/reports"
SCHEMA_FILE="$SCRIPT_DIR/schemas/batch_report.schema.json"
TIMESTAMP=$(date -u +%Y%m%d_%H%M%S)
BATCH_ID="batch_${TIMESTAMP}"

# Load environment variables from Claude settings.json
SETTINGS_FILE="$SCRIPT_DIR/.claude/settings.json"
if [ ! -f "$SETTINGS_FILE" ]; then
  echo "[ERROR] Claude settings.json not found at: $SETTINGS_FILE"
  echo "[ERROR] Please ensure .claude/settings.json exists with env variables configured"
  exit 1
fi

# Extract env vars from JSON using python
eval "$(python3 -c "
import json, sys
try:
    with open('$SETTINGS_FILE') as f:
        settings = json.load(f)
    for key, value in settings.get('env', {}).items():
        print(f'export {key}=\"{value}\"')
except Exception as e:
    print(f'echo \"[ERROR] Failed to load settings.json: {e}\"', file=sys.stderr)
    sys.exit(1)
")"

echo "[INFO] Environment variables loaded from settings.json"

# Default: look back 30 minutes (matches the cron interval)
SINCE=""
LIMIT=""

# Parse arguments
while [[ $# -gt 0 ]]; do
  case $1 in
    --since)
      SINCE="$2"
      shift 2
      ;;
    --limit)
      LIMIT="$2"
      shift 2
      ;;
    *)
      echo "Unknown option: $1"
      exit 1
      ;;
  esac
done

# If --since not provided, default to 30 minutes ago
if [ -z "$SINCE" ]; then
  if date -v-1d > /dev/null 2>&1; then
    SINCE=$(date -u -v-30M "+%Y-%m-%d %H:%M:%S")
  else
    SINCE=$(date -u -d "30 minutes ago" "+%Y-%m-%d %H:%M:%S")
  fi
fi

echo "[INFO] Batch RCA Analysis - $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
echo "[INFO] Analyzing events since: $SINCE"

#############################################
# Step 1: Query source DB for unanalyzed job IDs
#############################################
echo "[STEP 1] Querying source database for unanalyzed jobs..."

QUERY_ARGS=(--since "$SINCE")
if [ -n "$LIMIT" ]; then
  QUERY_ARGS+=(--limit "$LIMIT")
fi

JOBS_JSON=$(python3 "$SCRIPT_DIR/scripts/query_source_db.py" "${QUERY_ARGS[@]}" --json)

if [ $? -ne 0 ]; then
  echo "[ERROR] Source DB query failed"
  exit 1
fi

if [ -z "$JOBS_JSON" ] || [ "$JOBS_JSON" = "[]" ]; then
  echo "[INFO] No unanalyzed jobs found"
  echo "[SUCCESS] Batch RCA completed at $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
  exit 0
fi

JOB_IDS=$(echo "$JOBS_JSON" | python3 -c "import sys,json; jobs=json.load(sys.stdin); print('\n'.join(str(j['job_id']) for j in jobs))")
JOB_COUNT=$(echo "$JOB_IDS" | wc -l | tr -d ' ')
JOBS_LIST=$(echo "$JOB_IDS" | tr '\n' ' ' | sed 's/ $//')
echo "[INFO] Found $JOB_COUNT job(s) to analyze: $JOBS_LIST"

#############################################
# Step 1a: Pre-fetch logs from per-cluster bastions
#############################################
echo "[STEP 1a] Pre-fetching logs from per-cluster bastions..."
echo "$JOBS_JSON" | python3 "$SCRIPT_DIR/scripts/prefetch_job_logs.py" || {
  echo "[WARN] Log prefetch had failures (continuing with available logs)"
}

#############################################
# Step 1b: Query historical open issues
#############################################
echo "[STEP 1b] Querying historical open issues..."
OPEN_ISSUES=$(python3 "$SCRIPT_DIR/scripts/query_open_issues.py" --limit 30 2>/dev/null || echo "[]")
OPEN_COUNT=$(echo "$OPEN_ISSUES" | python3 -c "import sys,json; print(len(json.load(sys.stdin)))" 2>/dev/null || echo "0")
echo "[INFO] Found $OPEN_COUNT historical open issue group(s)"

#############################################
# Step 2: Build Dynamic Claude Prompt
#############################################
echo "[STEP 2] Building Claude prompt for parallel RCA..."

if [ ! -f "$SCHEMA_FILE" ]; then
  echo "[ERROR] Batch report schema not found at: $SCHEMA_FILE"
  exit 1
fi

# Build the orchestration prompt
read -r -d '' CLAUDE_PROMPT <<EOF || true
You are running in headless mode to analyze failed jobs in parallel.

**Job IDs to analyze:** $JOBS_LIST
**Batch ID:** $BATCH_ID
**Jobs requested:** $JOB_COUNT

**Historical Issues (from previous batches):**
The following root cause patterns have been seen in prior batch analyses.
After analyzing the current batch, compare results against these historical patterns.
Only correlate a current failure with a historical pattern when their root_cause_category values
match. Then check for additional overlap (same catalog_item, same cluster, or similar
root_cause_summary). Include matches in the historical_correlations array in the batch report.

$OPEN_ISSUES

**Instructions:**

1. **Spawn parallel agents** - For EACH job ID above, spawn a background agent in a SINGLE message with multiple Agent tool calls:

   Agent({
     description: "RCA for job {JOB_ID}",
     prompt: "Invoke the 'root-cause-analysis' skill for job {JOB_ID}. Use: Skill({skill: 'root-cause-analysis', args: '{JOB_ID}'}). Follow all skill instructions including Step 5 analysis. Report completion status.",
     run_in_background: true
   })

   **CRITICAL:** All agents must be in ONE response for true parallelism.
   Record agent_spawn as the ISO 8601 UTC timestamp when agents are launched.

2. **Wait for completion** - You'll receive task-notification for each agent when done.
   Record per-job duration_ms and status (completed|failed|timeout) in timing.agent_completion.

3. **Aggregate results** - After all agents complete:
   - Read each job's step5_analysis_summary.json from:
     .claude/skills/root-cause-analysis/.analysis/{job_id}/step5_analysis_summary.json
   - Also read step1_job_context.json from the same .analysis/{job_id}/ directory for guid,
     catalog_item, cluster/platform, and job_duration_seconds
   - Detect cross-job patterns: first group jobs by root_cause_category, then within each
     group look for shared signals (same failing file, same missing resource, similar summary)
   - Build the batch report JSON that conforms EXACTLY to the schema at:
     $SCHEMA_FILE
   - Read the schema file before writing the report; every required field must be present
   - Use these fixed values:
     * batch_id: "$BATCH_ID"
     * total_jobs_requested: $JOB_COUNT
     * total_jobs_analyzed: count of jobs with a valid step5_analysis_summary.json
     * total_jobs_failed: count of jobs whose agent failed or lack step5 output
     * confidence_breakdown: tally high/medium/low from each job's root_cause.confidence
     * high_priority_recommendations: top 5 across all jobs, ranked 1-5, deduplicated where possible
     * failed_analyses: one entry per failed job (empty array when none failed)
     * cross_job_patterns: shared patterns across 2+ jobs in this batch that share the same
       root_cause_category and have additional overlap (empty array when none)
     * historical_correlations: matches between current batch failures and the historical
       issues listed above. First filter by matching root_cause_category, then confirm with
       catalog_item, cluster, or root_cause_summary similarity. Each entry needs
       current_job_ids, historical_job_ids, pattern, description, and root_cause_category.
       Empty array when no matches found.
     * analysis_path for each job: ".analysis/{job_id}/step5_analysis_summary.json"

4. **Save report** - Write ONLY valid JSON (no markdown, no comments) to:
   $REPORT_DIR/${BATCH_ID}.json

5. **Output completion summary** - Print to stdout:
   - Number of jobs analyzed successfully
   - Number of failures (if any)
   - Report location

**Note:** The root-cause-analysis skill handles Steps 1-5 automatically, including Claude's analysis in Step 5.
EOF

#############################################
# Step 3: Setup MLflow
#############################################
MLFLOW_VENV="$SCRIPT_DIR/.mlflow-venv"
if grep -q "MLFLOW_CLAUDE_TRACING_ENABLED.*true" "$SETTINGS_FILE" 2>/dev/null; then
  echo "[STEP 3] Setting up MLflow tracing..."

  if [ ! -d "$MLFLOW_VENV" ]; then
    echo "[INFO] Creating MLflow venv (first run)..."
    python3 -m venv "$MLFLOW_VENV"
    "$MLFLOW_VENV/bin/pip" install -q mlflow
    echo "[INFO] MLflow installed"
  fi

  echo "[INFO] MLflow tracing enabled"
else
  echo "[STEP 3] MLflow tracing disabled (skipping)"
fi

#############################################
# Step 4: Execute Claude Headless
#############################################
echo "[STEP 4] Executing Claude in headless mode..."

mkdir -p "$REPORT_DIR"

# Run claude in non-interactive mode with permissions bypass for testing
# Note: -p/--print flag for non-interactive output
# Using --dangerously-skip-permissions for testing only
# Run from repo root to pick up .claude/settings.json (MLflow hooks, env vars)

cd "$SCRIPT_DIR" || exit 1

claude -p --dangerously-skip-permissions "$CLAUDE_PROMPT" || {
  echo "[ERROR] Claude execution failed"
  exit 1
}

#############################################
# Step 5: Store report in local DB
#############################################
echo "[STEP 5] Storing report in local database..."

REPORT_FILE="$REPORT_DIR/batch_${TIMESTAMP}.json"
if [ -f "$REPORT_FILE" ]; then
  python3 "$SCRIPT_DIR/scripts/store_report.py" "$REPORT_FILE" || {
    echo "[WARN] Failed to store report in database (non-fatal)"
  }
else
  echo "[WARN] Report file not found at $REPORT_FILE, skipping DB store"
fi

echo "[SUCCESS] Batch RCA completed at $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
echo "[INFO] Report: $REPORT_DIR/${BATCH_ID}.json"
