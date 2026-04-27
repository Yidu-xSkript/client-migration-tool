# migration/backup.py — File-based backup creation, restore, and lifecycle management
#
# Backups are written as JSON files under BACKUP_DIR/{env}/ so nothing is
# left behind in the database.

from __future__ import annotations
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

from config import BACKUP_DIR, BACKUP_RETENTION_DAYS
from db.operations import batch_insert


# ---------------------------------------------------------------------------
# Directory helpers
# ---------------------------------------------------------------------------

def _env_dir(env: str) -> str:
    """Return (and create if needed) the backup subdirectory for an environment."""
    path = os.path.join(BACKUP_DIR, env or "unknown")
    os.makedirs(path, exist_ok=True)
    return path


# ---------------------------------------------------------------------------
# Naming
# ---------------------------------------------------------------------------

def _backup_filename(client_id: int, table: str) -> str:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    # Keep total length well under filesystem limits
    max_tbl = max(8, 50 - len(str(client_id)))
    safe = table[:max_tbl]
    return f"clt_bkp_{client_id}_{safe}_{ts}.json"


# ---------------------------------------------------------------------------
# BackupInfo
# ---------------------------------------------------------------------------

@dataclass
class BackupInfo:
    file_path:      str       # Absolute path to the JSON file
    backup_name:    str       # Filename stem — used as unique display / action ID
    original_table: str
    client_id:      int
    env:            str       # "dev" | "qa" | "prod"
    created_at:     datetime
    age_days:       int
    row_count:      int = 0


def parse_backup_file(file_path: str) -> BackupInfo | None:
    """Read a backup file header and return a BackupInfo, or None if unreadable."""
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        meta = data.get("metadata", {})
        created_at = datetime.fromisoformat(meta["created_at"])
        return BackupInfo(
            file_path      = file_path,
            backup_name    = os.path.splitext(os.path.basename(file_path))[0],
            original_table = meta["original_table"],
            client_id      = int(meta["client_id"]),
            env            = meta.get("env", ""),
            created_at     = created_at,
            age_days       = (datetime.now() - created_at).days,
            row_count      = len(data.get("rows", [])),
        )
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------

def create_backups(
    client_id:    int,
    tables:       list,           # list[TableInfo]
    conn,
    env:          str = "",
    log_callback: Callable | None = None,
) -> list[str]:
    """
    Snapshot current destination rows into JSON files on disk.
    One file per table, stored under BACKUP_DIR/{env}/.
    Returns the list of file paths created.
    Failures are logged as warnings — they never abort the migration.
    """
    env_dir = _env_dir(env)
    created: list[str] = []

    for info in tables:
        filename  = _backup_filename(client_id, info.name)
        file_path = os.path.join(env_dir, filename)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT * FROM `{info.name}` WHERE `{info.client_id_column}` = %s",
                    (client_id,),
                )
                rows = cur.fetchall()

            payload = {
                "metadata": {
                    "client_id":        client_id,
                    "original_table":   info.name,
                    "client_id_column": info.client_id_column,
                    "env":              env,
                    "created_at":       datetime.now().isoformat(),
                },
                "rows": rows,
            }

            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, default=str, indent=2)

            created.append(file_path)
            if log_callback:
                log_callback(f"Backup saved: {filename}", "info")

        except Exception as e:
            if log_callback:
                log_callback(f"Warning: could not back up `{info.name}`: {e}", "warning")

    return created


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------

def list_backups(
    env:       str | None = None,
    client_id: int | None = None,
) -> list[BackupInfo]:
    """
    List all backup files under BACKUP_DIR, optionally filtered by env and client_id.
    Returns BackupInfo objects sorted newest-first.
    """
    os.makedirs(BACKUP_DIR, exist_ok=True)

    # Determine which env subfolders to scan
    if env:
        envs = [env]
    else:
        try:
            envs = [d for d in os.listdir(BACKUP_DIR)
                    if os.path.isdir(os.path.join(BACKUP_DIR, d))]
        except OSError:
            envs = []

    infos: list[BackupInfo] = []
    for e in envs:
        env_path = os.path.join(BACKUP_DIR, e)
        if not os.path.isdir(env_path):
            continue
        for fname in sorted(os.listdir(env_path), reverse=True):
            if not fname.endswith(".json"):
                continue
            info = parse_backup_file(os.path.join(env_path, fname))
            if info is None:
                continue
            if client_id is not None and info.client_id != client_id:
                continue
            infos.append(info)

    return sorted(infos, key=lambda b: b.created_at, reverse=True)


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------

def restore_backup(
    backup_info:  BackupInfo,
    conn,
    log_callback: Callable | None = None,
) -> int:
    """
    Restore a backup: delete current rows for the client in the original table,
    then re-insert from the JSON file.  Returns the number of rows restored.
    """
    with open(backup_info.file_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    meta    = data.get("metadata", {})
    rows    = data.get("rows", [])
    table   = backup_info.original_table
    cid_col = meta.get("client_id_column", "ClientId")
    cid     = backup_info.client_id

    try:
        with conn.cursor() as cur:
            cur.execute("SET FOREIGN_KEY_CHECKS = 0")
            cur.execute(f"DELETE FROM `{table}` WHERE `{cid_col}` = %s", (cid,))
            deleted = cur.rowcount

        inserted = batch_insert(table, rows, conn, batch_size=500)

        with conn.cursor() as cur:
            cur.execute("SET FOREIGN_KEY_CHECKS = 1")
        conn.commit()

        if log_callback:
            log_callback(
                f"Restored {inserted} rows into `{table}` (removed {deleted} existing rows).",
                "info",
            )
        return inserted

    except Exception as e:
        conn.rollback()
        if log_callback:
            log_callback(f"Restore failed for `{table}`: {e}", "error")
        raise


# ---------------------------------------------------------------------------
# Delete & retention
# ---------------------------------------------------------------------------

def delete_backup(backup_info: BackupInfo) -> None:
    """Delete a single backup file from disk."""
    os.remove(backup_info.file_path)


def cleanup_old_backups(
    days:         int = BACKUP_RETENTION_DAYS,
    env:          str | None = None,
    log_callback: Callable | None = None,
) -> list[str]:
    """
    Delete all backup files older than `days` days.
    Returns the list of backup_names that were deleted.
    """
    cutoff  = datetime.now() - timedelta(days=days)
    removed: list[str] = []

    for info in list_backups(env=env):
        if info.created_at < cutoff:
            try:
                os.remove(info.file_path)
                removed.append(info.backup_name)
                if log_callback:
                    log_callback(f"Deleted old backup: {info.backup_name}", "info")
            except Exception as e:
                if log_callback:
                    log_callback(f"Could not delete `{info.backup_name}`: {e}", "warning")

    return removed
