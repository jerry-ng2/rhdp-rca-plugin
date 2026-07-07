"""Fetch job logs from remote server via SSH + rsync."""

import os
import shlex
import subprocess
from pathlib import Path

from .tracing import SpanType, trace

SSH_TIMEOUT = 30
RSYNC_TIMEOUT = 120


def spool_base() -> str:
    """Base AAP2 ETL spool directory on remote bastions."""
    remote_dir = os.environ.get("REMOTE_DIR", "/var/spool/aap2-etl/extract")
    if remote_dir.endswith("/extract"):
        return remote_dir[: -len("/extract")]
    return os.environ.get("REMOTE_SPOOL_BASE", "/var/spool/aap2-etl")


def remote_extract_dir(cluster_name: str) -> str:
    """Resolve the remote extract directory for a cluster.

    CNV/multi-tenant bastions store logs under /var/spool/aap2-etl/{prefix}/extract
    where prefix is the first segment of cluster_name (prod0, prod1, event0, ...).
    Legacy aap2-* clusters use the flat /var/spool/aap2-etl/extract path.
    """
    base = spool_base()
    prefix = cluster_name.split("-")[0]
    if prefix.startswith("aap2"):
        return f"{base}/extract"
    return f"{base}/{prefix}/extract"


def build_ssh_rsync_prefix(remote_host: str, ssh_port: str | int | None = None) -> list[str]:
    """Build rsync -e argument list for SSH with optional port override."""
    if ssh_port is not None:
        return ["-e", f"ssh -p {ssh_port}"]
    return []


def build_ssh_command(remote_host: str, ssh_port: str | int | None = None) -> list[str]:
    """Build ssh command prefix with optional port."""
    cmd = ["ssh"]
    if ssh_port is not None:
        cmd.extend(["-p", str(ssh_port)])
    cmd.append(remote_host)
    return cmd


def _list_files_in_dir(
    remote_host: str,
    remote_dir: str,
    normalized: str,
    ssh_port: str | int | None = None,
) -> list[str]:
    remote_cmd = (
        f"cd {shlex.quote(remote_dir)} && "
        f"find . -maxdepth 1 -name {shlex.quote(normalized + '*')} -exec basename {{}} \\;"
    )
    ssh_cmd = build_ssh_command(remote_host, ssh_port)
    result = subprocess.run(
        ssh_cmd + [remote_cmd],
        capture_output=True,
        text=True,
        check=False,
        timeout=SSH_TIMEOUT,
    )
    if result.returncode != 0:
        return []
    return [f.strip() for f in result.stdout.splitlines() if f.strip()]


def _search_spool_tree(
    remote_host: str,
    normalized: str,
    ssh_port: str | int | None = None,
) -> tuple[list[str], str | None]:
    """Search the full spool tree and return (filenames, remote_dir)."""
    base = spool_base()
    remote_cmd = (
        f"find {shlex.quote(base)} -name {shlex.quote(normalized + '*')} -type f 2>/dev/null | head -5"
    )
    ssh_cmd = build_ssh_command(remote_host, ssh_port)
    result = subprocess.run(
        ssh_cmd + [remote_cmd],
        capture_output=True,
        text=True,
        check=False,
        timeout=SSH_TIMEOUT,
    )
    if result.returncode != 0:
        return [], None

    paths = [p.strip() for p in result.stdout.splitlines() if p.strip()]
    if not paths:
        return [], None

    # Prefer extract/ paths over scratch/extract when multiple matches exist
    paths.sort(key=lambda p: ("/scratch/" in p, p))
    chosen = paths[0]
    remote_dir = str(Path(chosen).parent)
    return [Path(chosen).name], remote_dir


def _resolve_remote_location(
    remote_host: str,
    normalized: str,
    remote_dir: str,
    cluster_name: str | None,
    ssh_port: str | int | None = None,
) -> tuple[list[str], str]:
    dirs_to_try: list[str] = []
    if cluster_name:
        dirs_to_try.append(remote_extract_dir(cluster_name))
    if remote_dir not in dirs_to_try:
        dirs_to_try.append(remote_dir)

    for candidate_dir in dirs_to_try:
        files = _list_files_in_dir(remote_host, candidate_dir, normalized, ssh_port)
        if files:
            return files, candidate_dir

    files, found_dir = _search_spool_tree(remote_host, normalized, ssh_port)
    if files and found_dir:
        return files, found_dir

    tried = ", ".join(dirs_to_try)
    raise FileNotFoundError(
        f"No log files found on {remote_host} for {normalized} (searched: {tried}, then {spool_base()})"
    )


@trace(name="Fetch job log from remote", span_type=SpanType.RETRIEVER if SpanType else None)
def fetch_job_log(
    job_id: str,
    local_dir: Path,
    remote_host: str,
    remote_dir: str,
    ssh_port: str | int | None = None,
    cluster_name: str | None = None,
) -> list[str]:
    """
    Fetch log files for a single job from the remote server.

    Args:
        job_id: Job identifier (with or without 'job_' prefix)
        local_dir: Local directory to store fetched logs
        remote_host: SSH host alias (from ~/.ssh/config)
        remote_dir: Default remote directory containing log files
        ssh_port: Optional SSH port override (when not set in ~/.ssh/config)
        cluster_name: Optional cluster name for per-tenant extract path resolution

    Returns:
        List of filenames fetched

    Raises:
        FileNotFoundError: If no matching files found on remote
        subprocess.CalledProcessError: If SSH or rsync fails
    """
    normalized = job_id if job_id.startswith("job_") else f"job_{job_id}"
    local_dir.mkdir(parents=True, exist_ok=True)

    files, resolved_dir = _resolve_remote_location(
        remote_host, normalized, remote_dir, cluster_name, ssh_port
    )

    print(f"[Fetch] Found {len(files)} file(s) on remote ({resolved_dir}): {', '.join(files)}")

    rsync_cmd = [
        "rsync",
        "-avz",
        "--progress",
        "--files-from=-",
        *build_ssh_rsync_prefix(remote_host, ssh_port),
        f"{remote_host}:{resolved_dir}/",
        str(local_dir),
    ]
    subprocess.run(
        rsync_cmd,
        input="\n".join(files),
        text=True,
        check=True,
        timeout=RSYNC_TIMEOUT,
    )

    print(f"[Fetch] Files transferred to {local_dir}")
    return files
