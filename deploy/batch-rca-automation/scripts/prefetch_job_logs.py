"""Pre-fetch job logs from per-cluster bastions via jumpbox ProxyJump.

Reads a JSON job list (from query_source_db.py --json) and fetches logs
into JOB_LOGS_DIR using SSH host aliases generated from aap2_user_url data.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent.parent
RCA_SCRIPTS = REPO_ROOT / "skills" / "root-cause-analysis"
sys.path.insert(0, str(RCA_SCRIPTS))

from scripts.log_fetcher import fetch_job_log, remote_extract_dir  # noqa: E402


def load_env() -> None:
    env_file = SCRIPT_DIR.parent / ".env"
    if env_file.exists():
        load_dotenv(env_file)


def parse_jumpbox_uri(jumpbox_uri: str) -> tuple[str, str, str | None]:
    """Parse JUMPBOX_URI into (user, hostname, port)."""
    if not jumpbox_uri:
        raise ValueError("JUMPBOX_URI is empty")

    parts = jumpbox_uri.split()
    user_host = parts[0]
    if "@" not in user_host:
        raise ValueError(f"Invalid JUMPBOX_URI format (expected user@host): {jumpbox_uri!r}")

    user, hostname = user_host.split("@", 1)
    port: str | None = None
    if "-p" in parts:
        port_idx = parts.index("-p")
        if port_idx + 1 < len(parts):
            port = parts[port_idx + 1]

    return user, hostname, port


def ssh_config_path() -> Path:
    return Path(os.environ.get("SSH_CONFIG", Path.home() / ".ssh" / "config"))


def ssh_host_exists(alias: str, config_path: Path) -> bool:
    if not config_path.exists():
        return False
    try:
        content = config_path.read_text()
    except OSError:
        return False

    for line in content.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("host "):
            aliases = stripped.split()[1:]
            if alias in aliases:
                return True
    return False


def append_ssh_host_block(config_path: Path, alias: str, lines: list[str]) -> None:
    if ssh_host_exists(alias, config_path):
        print(f"[SSH] Host alias '{alias}' already in {config_path}", file=sys.stderr)
        return

    config_path.parent.mkdir(parents=True, exist_ok=True)
    block = "\n".join([f"Host {alias}"] + [f"  {line}" for line in lines]) + "\n"
    with open(config_path, "a") as f:
        f.write("\n" + block)
    try:
        config_path.chmod(0o600)
    except OSError:
        pass
    print(f"[SSH] Added Host '{alias}' to {config_path}", file=sys.stderr)


def resolve_identity_file() -> str:
    for candidate in (
        os.environ.get("SSH_IDENTITY_FILE", ""),
        str(Path.home() / ".ssh" / "id_ed25519"),
        str(Path.home() / ".ssh" / "id_rsa"),
    ):
        if candidate and Path(candidate).exists():
            return candidate
    return str(Path.home() / ".ssh" / "id_ed25519")


def bastion_alias(cluster_name: str) -> str:
    sanitized = re.sub(r"[^a-zA-Z0-9-]", "-", cluster_name.lower()).strip("-")
    return f"bastion-{sanitized}"


def common_ssh_options(identity_file: str) -> list[str]:
    return [
        f"IdentityFile {identity_file}",
        "StrictHostKeyChecking no",
        "UserKnownHostsFile /dev/null",
    ]


def ensure_jumpbox_alias(jumpbox_alias: str, identity_file: str) -> None:
    config_path = ssh_config_path()
    if ssh_host_exists(jumpbox_alias, config_path):
        return

    jumpbox_uri = os.environ.get("JUMPBOX_URI", "")
    if not jumpbox_uri:
        print(
            f"[ERROR] Jumpbox alias '{jumpbox_alias}' not in SSH config and JUMPBOX_URI is unset",
            file=sys.stderr,
        )
        raise SystemExit(1)

    user, hostname, port = parse_jumpbox_uri(jumpbox_uri)
    lines = [
        f"HostName {hostname}",
        f"User {user}",
        *common_ssh_options(identity_file),
    ]
    if port:
        lines.insert(1, f"Port {port}")

    append_ssh_host_block(config_path, jumpbox_alias, lines)


def ensure_bastion_hosts(
    jobs: list[dict[str, Any]],
    jumpbox_alias: str,
    bastion_user: str,
    identity_file: str,
) -> dict[str, str]:
    """Write SSH config for each cluster bastion. Returns cluster_name -> alias."""
    cluster_aliases: dict[str, str] = {}

    for job in jobs:
        cluster_name = job.get("cluster_name")
        hostname = job.get("bastion_hostname")
        port = job.get("bastion_ssh_port")
        if not cluster_name or not hostname or port is None:
            continue

        alias = bastion_alias(cluster_name)
        cluster_aliases[cluster_name] = alias

        config_path = ssh_config_path()
        if ssh_host_exists(alias, config_path):
            continue

        lines = [
            f"HostName {hostname}",
            f"Port {port}",
            f"User {bastion_user}",
            f"ProxyJump {jumpbox_alias}",
            *common_ssh_options(identity_file),
        ]
        append_ssh_host_block(config_path, alias, lines)

    return cluster_aliases


def job_log_exists(job_logs_dir: Path, job_id: str | int) -> bool:
    jid = str(job_id)
    patterns = [
        f"job_{jid}.json",
        f"job_{jid}.json.gz",
        f"job_{jid}.json.gz.transform-processed",
        f"job_{jid}.json.transform-processed",
    ]
    for pattern in patterns:
        if (job_logs_dir / pattern).exists():
            return True
    return bool(list(job_logs_dir.glob(f"job_{jid}.*")))


def resolve_bastion_user() -> str:
    bastion_user = os.environ.get("BASTION_SSH_USER", "")
    if bastion_user:
        return bastion_user

    jumpbox_uri = os.environ.get("JUMPBOX_URI", "")
    if jumpbox_uri:
        user, _, _ = parse_jumpbox_uri(jumpbox_uri)
        return user

    print("[ERROR] BASTION_SSH_USER or JUMPBOX_URI is required for bastion SSH user", file=sys.stderr)
    raise SystemExit(1)


def prefetch_jobs(jobs: list[dict[str, Any]]) -> dict[str, int]:
    jumpbox_alias = os.environ.get("SSH_JUMPBOX_ALIAS", "rca-jumpbox")
    jumpbox_uri = os.environ.get("JUMPBOX_URI", "")

    if not jumpbox_uri and not ssh_host_exists(jumpbox_alias, ssh_config_path()):
        print(
            "[ERROR] JUMPBOX_URI or SSH_JUMPBOX_ALIAS (with existing SSH config entry) is required",
            file=sys.stderr,
        )
        raise SystemExit(1)

    job_logs_dir_str = os.environ.get("JOB_LOGS_DIR", "")
    remote_dir = os.environ.get("REMOTE_DIR", "")
    if not job_logs_dir_str or not remote_dir:
        print("[ERROR] JOB_LOGS_DIR and REMOTE_DIR are required", file=sys.stderr)
        raise SystemExit(1)

    job_logs_dir = Path(job_logs_dir_str)
    identity_file = resolve_identity_file()
    bastion_user = resolve_bastion_user()

    ensure_jumpbox_alias(jumpbox_alias, identity_file)
    cluster_aliases = ensure_bastion_hosts(jobs, jumpbox_alias, bastion_user, identity_file)

    stats = {"fetched": 0, "skipped": 0, "failed": 0, "no_bastion": 0}

    for job in jobs:
        job_id = job.get("job_id")
        cluster_name = job.get("cluster_name")
        if not job_id:
            continue

        if not job.get("bastion_hostname") or job.get("bastion_ssh_port") is None:
            print(
                f"[WARN] Skipping prefetch for job {job_id}: no bastion mapping "
                f"(cluster_name={cluster_name!r})",
                file=sys.stderr,
            )
            stats["no_bastion"] += 1
            continue

        if job_log_exists(job_logs_dir, job_id):
            print(f"[SKIP] Job {job_id}: log already in {job_logs_dir}", file=sys.stderr)
            stats["skipped"] += 1
            continue

        alias = cluster_aliases.get(cluster_name) or bastion_alias(cluster_name)
        job_remote_dir = remote_extract_dir(cluster_name) if cluster_name else remote_dir
        print(
            f"[FETCH] Job {job_id} via {alias} "
            f"({job['bastion_hostname']}:{job['bastion_ssh_port']}) "
            f"dir={job_remote_dir}",
            file=sys.stderr,
        )
        try:
            fetch_job_log(
                str(job_id),
                job_logs_dir,
                alias,
                remote_dir,
                cluster_name=cluster_name,
            )
            stats["fetched"] += 1
        except (
            FileNotFoundError,
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
        ) as exc:
            print(f"[ERROR] Job {job_id}: {exc}", file=sys.stderr)
            stats["failed"] += 1

    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Pre-fetch job logs from per-cluster bastions")
    parser.add_argument(
        "--input", "-i", type=str, default=None,
        help="JSON job list file (default: read from stdin)",
    )
    args = parser.parse_args(argv)

    load_env()

    if args.input:
        with open(args.input) as f:
            jobs = json.load(f)
    else:
        raw = sys.stdin.read().strip()
        if not raw:
            print("[INFO] No jobs to prefetch", file=sys.stderr)
            return 0
        jobs = json.loads(raw)

    if not isinstance(jobs, list):
        print("[ERROR] Expected JSON array of job objects", file=sys.stderr)
        return 1

    if not jobs:
        print("[INFO] No jobs to prefetch", file=sys.stderr)
        return 0

    stats = prefetch_jobs(jobs)
    attempted = stats["fetched"] + stats["failed"]
    print(
        f"[DONE] Prefetch complete: fetched={stats['fetched']} skipped={stats['skipped']} "
        f"failed={stats['failed']} no_bastion={stats['no_bastion']}",
        file=sys.stderr,
    )

    if attempted > 0 and stats["fetched"] == 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
