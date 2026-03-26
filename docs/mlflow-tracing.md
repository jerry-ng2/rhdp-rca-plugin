# MLflow Tracing Setup (Manual Fallback)

Tracing is optional. Use this guide only if preflight setup does not configure tracing automatically in your environment.

## 1) Create and activate `.venv`

```bash
python3 -m venv .venv
source .venv/bin/activate
```

## 2) Install MLflow

```bash
pip install "mlflow[genai]>=3.4"
```

## 3) Configure settings from template

Use `.claude/settings.example.json` and copy the MLflow-related keys/hook blocks into your active settings file:

- Project-level: `.claude/settings.local.json`
- Global: `~/.claude/settings.json`

Required pieces:
- `MLFLOW_CLAUDE_TRACING_ENABLED`, `MLFLOW_PORT`, `MLFLOW_EXPERIMENT_NAME`
- `SessionStart` and `Stop` hooks

## 4) Enable autologging and verify

```bash
mlflow autolog claude -u "http://127.0.0.1:$MLFLOW_PORT" -n "$MLFLOW_EXPERIMENT_NAME"
```

Then run Claude and execute an RCA prompt. If configured correctly, traces appear in your MLflow UI.
