---
name: rca-annotator
description: Structured annotation tool that walks users through reviewing and labeling root-cause-analysis outputs, with evidence traceability, difficulty calibration, and alternative diagnosis capture.
allowed-tools:
  - Read
  - Write
  - Bash
  - AskUserQuestion
---

# RCA Annotator

A structured annotation tool that presents the `root-cause-analysis` agent's diagnosis to the user and guides them through labeling it — capturing whether the diagnosis is correct, evidence quality, difficulty, and alternative hypotheses.

| | `root-cause-analysis` (Agent) | `rca-annotator` (Annotation tool) |
|---|---|---|
| **Purpose** | Diagnose failures for users | Capture human-labeled ground-truth data |
| **Reads** | Logs, Splunk, GitHub (live) | Step 1/3/4/5 output files (offline) |
| **Output** | Human-readable diagnosis | Structured `annotation.json` |

**Use when**: a `root-cause-analysis` run is complete and you want to annotate its output as correct, incorrect, or partially correct — for evaluation, benchmarking, or dataset building.

**Do NOT use** to perform initial RCA (use `root-cause-analysis`).

## Prerequisites

Verify root-cause-analysis has been completed.

- `JUMPBOX_URI` (optional) — SSH connection string (e.g. `"user@host -p 2222"`). If unset, uses local `.analysis/` only.
- SSH keys configured in `~/.ssh/config` if using jumpbox; `ssh` and `rsync` installed.
- **Required files** in `.analysis/<job_id>/`:
  - `step5_summary.json` — Agent's final diagnosis (primary input)
  - `step1_job_context.json` — Job metadata, failed tasks
  - `step3_correlation.json` — Timeline with AAP + Splunk events
  - `step4_github_fetch_history.json` — Configuration and code context

If missing, run `root-cause-analysis` skill first.

## Workflow

0. Download from jumpbox (if `JUMPBOX_URI` set) or verify local files
1. Read `step5_summary.json` — present the agent's diagnosis to the user
2. Walk through annotation questions interactively — the user labels each section
3. Write `annotation.json` with the user's labels
4. Upload to jumpbox (if `JUMPBOX_URI` set)

---

## Step 0: Locate Analysis Files

First, determine where the analysis files are:

1. Check if `step5_analysis_summary.json` exists in the current directory
2. If not, check `.analysis/<job_id>/`
3. If neither exists and `JUMPBOX_URI` is set, download files:

```bash
cd skills/rca-annotator
python scripts/cli.py download --job-id <job_id>
```

**For eval/headless mode**: Files are in the current directory (workspace root)
**For interactive mode**: Files are in `.analysis/<job_id>/` (relative to `skills/rca-annotator/`)

Once located, record the base path for reading step files in subsequent steps.

---

## Step 1: Read Agent Diagnosis

Read `step5_analysis_summary.json` from the base path determined in Step 0 and present the agent's diagnosis clearly to the user:

- Root cause category and summary
- Confidence level
- Key evidence cited
- Difficulty score (if present)
- Recommendations
- Alternative diagnoses (if any)

This is the starting point for annotation. The user is reviewing the agent's work.

---

## Step 2: Interactive Annotation

Walk through each question below with the user. Present the relevant section from `step5_summary.json` before asking each question.

**IMPORTANT**: Use the `AskUserQuestion` tool for each question to enable headless execution in evaluation harnesses. Present the question with appropriate options where applicable. For open-ended questions, use a single free-text option.

### 1. Root Cause Category

Present the agent's category and summary. Use AskUserQuestion to ask:

```
Question: "Is the root cause category correct?"
Options: 
- "Yes, <agent_category> is correct"
- "No, should be: <other_category>" (for each valid category different from agent's)
- "Other (specify)"
```

Valid categories: `configuration` | `infrastructure` | `application_bug` | `dependency` | `network` | `resource` | `cloud_api` | `credential` | `secrets` | `unknown`

### 2. Summary Accuracy

Present the agent's summary sentence. Use AskUserQuestion to ask:

```
Question: "Is the summary accurate and specific?"
Options:
- "Yes, accurate as-is"
- "Mostly accurate, minor refinement needed"
- "Needs correction (specify)"
```

### 3. Evidence

Present the evidence items the agent cited. Use AskUserQuestion to ask:

```
Question: "Is any evidence missing or wrong?"
Options:
- "All evidence complete and accurate"
- "Some evidence missing (specify)"
- "Some evidence incorrect (specify)"
```

If the user indicates issues, read step1/step3/step4 to help identify missing or incorrect evidence. This is reference material for validation — not a re-analysis.

**Evidence traceability format** (for any new or corrected evidence items the user provides):

```json
{
  "source": "step1 | step3 | step4",
  "source_file": ".analysis/<job_id>/step1_job_context.json",
  "json_path": "failed_tasks[0].duration",
  "exact_value": 917.565567,
  "exact_quote": "optional — literal text for code/config",
  "line_number": 5,
  "github_path": "owner/repo:path/to/file.yml:line",
  "message": "The relevant log line or config snippet.",
  "confidence": "high | medium | low",
  "is_root_cause": true
}
```

### 4. Difficulty Rating

Present the agent's difficulty score (or estimate one from the evidence). Present the calibration rubric to help the user score:

| Criterion | Points |
|---|---|
| Requires cross-source correlation (AAP + Splunk + GitHub) | +3 |
| Requires understanding code behavior | +2 |
| Error message is generic or misleading | +2 |
| Requires variable precedence/override knowledge | +1 |
| Requires domain knowledge (K8s, Ansible, cloud APIs) | +1 |
| Multiple plausible alternatives exist | +1 |
| Timing dependencies are critical | +1 |

Mapping: 0–3 = easy, 4–6 = medium, 7–10 = hard.

Use AskUserQuestion to ask:

```
Question: "Is the difficulty rating appropriate?"
Options:
- "Yes, score of <X>/10 is correct"
- "Too easy, should be <Y>/10"
- "Too hard, should be <Z>/10"
```

### 5. Alternative Diagnoses

Present any alternative diagnoses the agent identified. Use AskUserQuestion to ask:

```
Question: "Any alternative diagnoses to add or correct?"
Options:
- "No, alternatives are complete"
- "Add alternative diagnosis (specify)"
- "Correct existing alternative (specify)"
```

Alternative diagnosis format:

```json
{
  "category": "infrastructure",
  "summary": "A plausible but wrong diagnosis.",
  "why_wrong": "Why the evidence does not support this.",
  "plausibility": "high | medium | low",
  "supporting_evidence": ["long timeout", "destroy action"],
  "contradicting_evidence": ["test environment", "auth retry pattern"]
}
```

`plausibility`: `high` = shares many characteristics | `medium` = some evidence | `low` = superficial similarity

---

## Step 3: Write Annotation

After all questions are answered, verify before writing:

- Root cause category confirmed or corrected
- Exactly one evidence item has `is_root_cause: true`
- All evidence has traceability (source_file, json_path, exact_value/quote)
- Difficulty score calculated with justification
- Alternative diagnoses have plausibility levels

Write `annotation.json` to the base path from Step 0:
- **Eval/headless mode**: Write to `outputs/annotation.json` (create outputs directory first)
- **Interactive mode**: Write to `.analysis/<job_id>/annotation.json`

---

## Step 4: Upload Annotation

```bash
cd skills/rca-annotator
python scripts/cli.py upload --job-id <job_id>
```

Uploads `.analysis/<job_id>/annotation.json` to jumpbox if `JUMPBOX_URI` set. Local copy always preserved. If `JUMPBOX_URI` unset, file remains local only.

---

## Output Format

Save to `.analysis/<job_id>/annotation.json`:

```json
{
  "job_id": "1234567",
  "annotated_at": "2026-03-19T12:05:00Z",

  "category_correct": true,
  "category_comment": "Confirmed — matches the auth retry pattern.",

  "root_cause": {
    "category": "configuration | infrastructure | application_bug | dependency | network | resource | cloud_api | credential | secrets | unknown",
    "summary": "One sentence describing what failed and why.",
    "confidence": "high | medium | low"
  },

  "summary_accurate": true,
  "summary_comment": "Clear and specific.",

  "evidence": [
    {
      "source": "step1 | step3 | step4",
      "source_file": ".analysis/<job_id>/step1_job_context.json",
      "json_path": "failed_tasks[0].duration",
      "exact_value": 917.565567,
      "exact_quote": "optional — literal text for code/config",
      "line_number": 5,
      "github_path": "owner/repo:path/to/file.yml:line",
      "message": "The relevant log line or config snippet.",
      "confidence": "high | medium | low",
      "is_root_cause": true
    }
  ],

  "evidence_feedback": "Missing the kubeconfig 404 from step4 github_fetches.",

  "difficulty": "easy | medium | hard",
  "difficulty_score": 5,
  "difficulty_justification": "Requires correlating task code (+2) with missing configs and interpreting generic MODULE FAILURE (+2). Total: 5.",
  "difficulty_appropriate": false,
  "difficulty_comment": "Should be hard (8/10) — requires deep variable precedence knowledge.",

  "recommendations": [
    {
      "priority": "high | medium | low",
      "action": "What should be done to fix it.",
      "file": "path/to/file.yml"
    }
  ],

  "contributing_factors": [
    "Factor that made the failure more likely or harder to diagnose."
  ],

  "alternative_diagnoses": [
    {
      "category": "infrastructure",
      "summary": "A plausible but wrong diagnosis.",
      "why_wrong": "Why the evidence does not support this.",
      "plausibility": "high | medium | low",
      "supporting_evidence": ["long timeout", "destroy action"],
      "contradicting_evidence": ["test environment", "auth retry pattern"]
    }
  ]
}
```
