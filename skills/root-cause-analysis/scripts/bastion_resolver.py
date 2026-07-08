"""Resolve per-job bastion SSH targets and manage SSH config for log fetch."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Config


@dataclass(frozen=True)
class BastionTarget:
    """SSH target for fetching a job log from a cluster bastion."""

    remote_host: str
    remote_log_dir: str | None = None
    cluster_name: str | None = None
    bastion_hostname: str | None = None
    bastion_ssh_port: int | None = None


def ssh_config_path() -> Path:
    return Path(os.environ.get("SSH_CONFIG", Path.home() / ".ssh" / "config"))


def ssh_host_exists(alias: str, config_path: Path | None = None) -> bool:
    path = config_path or ssh_config_path()
    if not path.exists():
        return False
    try:
        content = path.read_text()
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
        print(f"[SSH] Host alias '{alias}' already in {config_path}")
        return

    config_path.parent.mkdir(parents=True, exist_ok=True)
    block = "\n".join([f"Host {alias}"] + [f"  {line}" for line in lines]) + "\n"
    with open(config_path, "a") as f:
        f.write("\n" + block)
    try:
        config_path.chmod(0o600)
    except OSError:
        pass
    print(f"[SSH] Added Host '{alias}' to {config_path}")


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


def resolve_bastion_user(config: Config) -> str:
    if config.bastion_ssh_user:
        return config.bastion_ssh_user
    if config.jumpbox_uri:
        user, _, _ = parse_jumpbox_uri(config.jumpbox_uri)
        return user
    raise ValueError("BASTION_SSH_USER or JUMPBOX_URI is required for bastion SSH user")


def ensure_jumpbox_alias(config: Config, identity_file: str | None = None) -> None:
    jumpbox_alias = config.ssh_jumpbox_alias
    config_path = ssh_config_path()
    if ssh_host_exists(jumpbox_alias, config_path):
        return

    if not config.jumpbox_uri:
        raise ValueError(
            f"Jumpbox alias '{jumpbox_alias}' not in SSH config and JUMPBOX_URI is unset"
        )

    identity = identity_file or resolve_identity_file()
    user, hostname, port = parse_jumpbox_uri(config.jumpbox_uri)
    lines = [
        f"HostName {hostname}",
        f"User {user}",
        *common_ssh_options(identity),
    ]
    if port:
        lines.insert(1, f"Port {port}")

    append_ssh_host_block(config_path, jumpbox_alias, lines)


def ensure_bastion_host(
    target: BastionTarget,
    config: Config,
    identity_file: str | None = None,
) -> None:
    if not target.cluster_name or not target.bastion_hostname or target.bastion_ssh_port is None:
        raise ValueError("Bastion target is missing cluster_name, bastion_hostname, or port")

    alias = target.remote_host
    config_path = ssh_config_path()
    if ssh_host_exists(alias, config_path):
        return

    identity = identity_file or resolve_identity_file()
    bastion_user = resolve_bastion_user(config)
    lines = [
        f"HostName {target.bastion_hostname}",
        f"Port {target.bastion_ssh_port}",
        f"User {bastion_user}",
        f"ProxyJump {config.ssh_jumpbox_alias}",
        *common_ssh_options(identity),
    ]
    append_ssh_host_block(config_path, alias, lines)


def lookup_job_bastion(config: Config, job_id: str) -> BastionTarget | None:
    """Look up cluster and bastion mapping for a job from the source database."""
    if not config.has_source_db():
        return None

    try:
        import psycopg2
        import psycopg2.extras
        import psycopg2.sql
    except ImportError as exc:
        raise RuntimeError(
            "SOURCE_DB is configured but psycopg2 is not installed. "
            "Install psycopg2-binary or use REMOTE_HOST for single-host fetch."
        ) from exc

    query = psycopg2.sql.SQL(
        "SELECT e.job_id, e.cluster_name, u.bastion_hostname, u.bastion_ssh_port, "
        "u.instance_base_path "
        "FROM {} e "
        "LEFT JOIN {} u ON e.cluster_name = u.cluster_name "
        "WHERE e.job_id = %s "
        "ORDER BY e.job_started DESC "
        "LIMIT 1"
    ).format(
        psycopg2.sql.Identifier(config.source_db_table),
        psycopg2.sql.Identifier(config.source_db_bastion_table),
    )

    conn = psycopg2.connect(
        host=config.source_db_host,
        port=config.source_db_port,
        dbname=config.source_db_name,
        user=config.source_db_user,
        password=config.source_db_password,
        cursor_factory=psycopg2.extras.RealDictCursor,
    )
    try:
        with conn.cursor() as cur:
            cur.execute(query, (job_id,))
            row = cur.fetchone()
    finally:
        conn.close()

    if not row:
        return None

    cluster_name = row.get("cluster_name")
    bastion_hostname = row.get("bastion_hostname")
    bastion_ssh_port = row.get("bastion_ssh_port")
    instance_base_path = (row.get("instance_base_path") or "").strip() or None
    if not cluster_name or not bastion_hostname or bastion_ssh_port is None:
        return None

    return BastionTarget(
        remote_host=bastion_alias(cluster_name),
        remote_log_dir=instance_base_path,
        cluster_name=cluster_name,
        bastion_hostname=bastion_hostname,
        bastion_ssh_port=int(bastion_ssh_port),
    )


def resolve_bastion_for_job(config: Config, job_id: str) -> BastionTarget:
    """Resolve the SSH host alias to use when fetching a job log."""
    target = lookup_job_bastion(config, job_id)
    if target:
        return target

    if config.remote_host and config.remote_log_dir:
        return BastionTarget(
            remote_host=config.remote_host,
            remote_log_dir=config.remote_log_dir,
        )

    raise ValueError(
        "Cannot resolve bastion for job fetch. Configure SOURCE_DB_* + JUMPBOX_URI for "
        "per-cluster bastions, or REMOTE_HOST + REMOTE_DIR for a single log server."
    )


def resolve_remote_log_dir(target: BastionTarget, config: Config) -> str:
    """Resolve the remote directory for fetching job logs."""
    if (target.remote_log_dir or "").strip():
        # instance_base_path is the ETL instance root; job logs live in its extract/ subdir
        return target.remote_log_dir.rstrip("/") + "/extract"
    remote_dir = config.remote_log_dir
    if not remote_dir:
        raise ValueError(
            "No remote log directory resolved. Populate instance_base_path in "
            f"{config.source_db_bastion_table} or set REMOTE_DIR."
        )
    return remote_dir


def prepare_bastion_for_fetch(config: Config, target: BastionTarget) -> None:
    """Ensure SSH config entries exist for the resolved bastion target."""
    if target.bastion_hostname and target.bastion_ssh_port is not None:
        ensure_jumpbox_alias(config)
        ensure_bastion_host(target, config)
        return

    if not ssh_host_exists(target.remote_host):
        raise ValueError(f"REMOTE_HOST alias '{target.remote_host}' not found in SSH config")
