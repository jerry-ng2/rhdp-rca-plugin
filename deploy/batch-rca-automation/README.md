# Batch RCA Automation

Automated root cause analysis system for Red Hat Demo Platform (RHDP) infrastructure failures, deployed as an OpenShift CronJob.

## Overview

This system automatically:
- Resolves per-cluster bastion targets from `aap2_user_url` and pre-fetches job logs via jumpbox ProxyJump
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
2. Queries `aap2_events` joined with `aap2_user_url` on `cluster_name` for unanalyzed jobs
3. Pre-fetches logs from each job's bastion (`bastion_hostname` / `bastion_ssh_port`) via jumpbox ProxyJump into `JOB_LOGS_DIR`
4. Spawns parallel Claude Code agents in background mode to run the RCA skill for each job
5. Generates aggregated batch reports
6. Updates state tracking to prevent re-processing

### Multi-bastion log routing

Jobs may run on different AAP clusters, each with its own bastion. The batch pipeline resolves the correct bastion per job:

```
aap2_events.cluster_name  →  aap2_user_url  →  bastion_hostname + bastion_ssh_port
                                                      ↓
                                              SSH via ProxyJump (JUMPBOX_URI)
                                                      ↓
                                              JOB_LOGS_DIR (local cache)
```

Required env vars for bastion routing:
- `SOURCE_DB_BASTION_TABLE` — bastion lookup table (default: `aap2_user_url`)
- `JUMPBOX_URI` — jumpbox connection for ProxyJump (e.g. `user@host -p 30361`)
- `SSH_JUMPBOX_ALIAS` — SSH config alias for jumpbox (default: `rca-jumpbox`; use `ci-jumpbox` on OpenShift)
- `BASTION_SSH_USER` — SSH username for bastion hosts (defaults to user from `JUMPBOX_URI`)
- `REMOTE_DIR` — default remote log directory on bastions (`/var/spool/aap2-etl/extract` for legacy aap2 clusters)
- Per-cluster CNV bastions use tenant subdirectories: `/var/spool/aap2-etl/{prefix}/extract` where `prefix` is the first segment of `cluster_name` (e.g. `prod0`, `prod1`, `event0`). The prefetch script resolves this automatically from `cluster_name`.

## Performance

| Metric | Value |
|--------|-------|
| **Jobs analyzed** | 50+ jobs/day |
| **Success rate** | ~95% | 
| **Init time** | 14 seconds |
| **Analysis time** | 2-3 minutes for 5-7 jobs (parallel) |

## Output

**Batch Reports:** `/workspace/reports/batch_YYYYMMDD_HHMMSS.json`

```json
{
  "batch_id": "batch_YYYYMMDD_HHMMSS",
  "total_jobs_requested": 4,
  "total_jobs_completed": 4,
  "root_cause_category_breakdown": {
    "infrastructure": 3,
    "configuration": 1
  },
  "job_summaries": [...]
}
```

**Individual Analysis:** `/workspace/.claude/skills/root-cause-analysis/.analysis/{job_id}/`
- Session metadata, job context, Splunk logs, correlation analysis, GitHub history, final RCA report
