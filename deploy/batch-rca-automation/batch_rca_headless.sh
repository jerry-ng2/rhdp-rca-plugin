#!/bin/bash
set -euo pipefail

#############################################
# Batch RCA Analysis - Claude Headless Mode
#############################################
#
# This script:
# 1. Queries source PostgreSQL table for unanalyzed job IDs (ai_proccessed = FALSE)
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
TIMESTAMP=$(date -u +%Y%m%d_%H%M%S)

# Load environment variables from Claude settings.json
SETTINGS_FILE="$SCRIPT_DIR/../../.claude/settings.json"
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

JOB_IDS=$(python3 "$SCRIPT_DIR/scripts/query_source_db.py" "${QUERY_ARGS[@]}")

if [ $? -ne 0 ]; then
  echo "[ERROR] Source DB query failed"
  exit 1
fi

if [ -z "$JOB_IDS" ]; then
  echo "[INFO] No unanalyzed jobs found"
  echo "[SUCCESS] Batch RCA completed at $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
  exit 0
fi

JOB_COUNT=$(echo "$JOB_IDS" | wc -l | tr -d ' ')
JOBS_LIST=$(echo "$JOB_IDS" | tr '\n' ' ' | sed 's/ $//')
echo "[INFO] Found $JOB_COUNT job(s) to analyze: $JOBS_LIST"

#############################################
# Step 2: Build Dynamic Claude Prompt
#############################################
echo "[STEP 2] Building Claude prompt for parallel RCA..."

# Build the orchestration prompt
read -r -d '' CLAUDE_PROMPT <<EOF || true
You are running in headless mode to analyze failed jobs in parallel.

**Job IDs to analyze:** $JOBS_LIST

**Instructions:**

1. **Spawn parallel agents** - For EACH job ID above, spawn a background agent in a SINGLE message with multiple Agent tool calls:

   Agent({
     description: "RCA for job {JOB_ID}",
     prompt: "Invoke the 'root-cause-analysis' skill for job {JOB_ID}. Use: Skill({skill: 'root-cause-analysis', args: '{JOB_ID}'}). Follow all skill instructions including Step 5 analysis. Report completion status.",
     run_in_background: true
   })

   **CRITICAL:** All agents must be in ONE response for true parallelism.

2. **Wait for completion** - You'll receive task-notification for each agent when done.

3. **Aggregate results** - After all agents complete:
   - Read each job's step5_analysis_summary.json from the .analysis/{job_id}/ directory
   - Create aggregated report with:
     * Total jobs analyzed
     * Root cause category breakdown (count by category)
     * High-priority recommendations (collect top 5 from all jobs)
     * Any failed analyses
   - Report time taken for each step (agent spawn, wait, aggregation)

4. **Save report** - Write to: $REPORT_DIR/batch_${TIMESTAMP}.json

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
REPO_ROOT="$SCRIPT_DIR/../.."
cd "$REPO_ROOT" || exit 1

claude -p --dangerously-skip-permissions "$CLAUDE_PROMPT" || {
  echo "[ERROR] Claude execution failed"
  exit 1
}

echo "[SUCCESS] Batch RCA completed at $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
echo "[INFO] Report: $REPORT_DIR/batch_${TIMESTAMP}.json"
