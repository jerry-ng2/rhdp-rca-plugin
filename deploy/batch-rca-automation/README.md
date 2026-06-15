# Batch RCA Automation

Automated root cause analysis system for Red Hat Demo Platform (RHDP) infrastructure failures, deployed as an OpenShift CronJob.

## Overview

This system automatically:
- Fetches failed job logs every 30 minutes from remote bastion
- Spawns parallel Claude Code agents for analysis (up to 15 jobs simultaneously)
- Generates aggregated batch reports with root cause breakdowns
- Tracks analyzed jobs to prevent duplicate processing
- Persists analysis results to PVC for long-term storage

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│ OpenShift CronJob (every 30 minutes)                       │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  Init Container: Setup SSH, Claude settings, skills        │
│  Main Container: Run batch_rca_headless.sh                 │
│                                                             │
└─────────────────────────────────────────────────────────────┘
              │
              ▼
    ┌──────────────────────┐
    │ PersistentVolumeClaim│
    │  - reports/          │
    │  - analysis results  │
    │  - state tracking    │
    └──────────────────────┘
```

## How It Works

The orchestration script (`batch_rca_headless.sh`):
1. Loads environment variables from Claude settings
2. Fetches recent failed job logs via SSH
3. Filters out already-analyzed jobs
4. Spawns parallel Claude Code agents in background mode run rca skill for each of the jobs in parallel.
5. Generates aggregated batch reports
6. Updates state tracking to prevent re-processing

## Performance

| Metric | Value |
|--------|-------|
| **Jobs analyzed** | 50+ jobs/day |
| **Success rate** | ~95% | 
| **Init time** | 14 seconds |
| **Analysis time** | 2-3 minutes for 5-7 jobs (parallel) |

## Output

**Batch Reports:** `/workspace/reports/batch_YYYYMMDD_HHMMSS.json`

Reports must conform to `schemas/batch_report.schema.json`. The orchestration script passes this schema path to Claude during aggregation.

Required top-level fields: `batch_id`, `generated_at`, `total_jobs_requested`, `total_jobs_analyzed`, `total_jobs_failed`, `timing`, `root_cause_category_breakdown`, `confidence_breakdown`, `high_priority_recommendations`, `job_summaries`, `failed_analyses`, `cross_job_patterns`.

**Individual Analysis:** `/workspace/.claude/skills/root-cause-analysis/.analysis/{job_id}/`
- Session metadata, job context, Splunk logs, correlation analysis, GitHub history, final RCA report
