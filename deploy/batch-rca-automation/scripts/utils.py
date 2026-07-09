"""Shared database utilities for batch RCA automation scripts."""

from __future__ import annotations

import os
import sys
from typing import Any

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

ALL_CONFIG_KEYS = {
    "host": ("SOURCE_DB_HOST", "localhost"),
    "port": ("SOURCE_DB_PORT", "5432"),
    "name": ("SOURCE_DB_NAME", ""),
    "user": ("SOURCE_DB_USER", ""),
    "password": ("SOURCE_DB_PASSWORD", ""),
    "source_table": ("SOURCE_DB_TABLE", ""),
    "results_table": ("SOURCE_DB_RESULT_TABLE", ""),
}


def load_config(required: tuple[str, ...] = ("name", "user", "password")) -> dict[str, Any]:
    env_file = os.path.join(os.path.dirname(__file__), "..", ".env")
    if os.path.exists(env_file):
        load_dotenv(env_file)

    config: dict[str, Any] = {}
    for key, (env_var, default) in ALL_CONFIG_KEYS.items():
        val = os.environ.get(env_var, default)
        config[key] = int(val) if key == "port" else val

    errors = [f"{ALL_CONFIG_KEYS[k][0]} is required" for k in required if not config.get(k)]
    if errors:
        print("\n".join(errors), file=sys.stderr)
        raise SystemExit(1)

    return config


def connect_db(config: dict[str, Any], *, use_dict_cursor: bool = False) -> Any:
    kwargs: dict[str, Any] = {
        "host": config["host"],
        "port": config["port"],
        "dbname": config["name"],
        "user": config["user"],
        "password": config["password"],
    }
    if use_dict_cursor:
        kwargs["cursor_factory"] = psycopg2.extras.RealDictCursor
    return psycopg2.connect(**kwargs)
