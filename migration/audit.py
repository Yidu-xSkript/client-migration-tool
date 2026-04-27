# migration/audit.py — Audit log writer and reader

from __future__ import annotations
import json
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime
from config import AUDIT_LOG_PATH, EDIT_LOG_PATH, AUDIT_LOG_DISPLAY_LIMIT


@dataclass
class AuditEntry:
    timestamp: str
    source_env: str
    target_env: str
    client_id: int
    tables_migrated: list[str]
    row_counts: dict[str, int]      # {table: rows_migrated}
    status: str                      # "success" | "failure" | "dry_run"
    error_message: str = ""
    ticket_number: str = ""
    backup_tables: list[str] = None

    def __post_init__(self):
        if self.backup_tables is None:
            self.backup_tables = []


def log_attempt(entry: AuditEntry) -> None:
    """Append an audit entry as a JSON line to the audit log file."""
    line = json.dumps(asdict(entry)) + "\n"
    try:
        with open(AUDIT_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass  # Audit logging failure is non-fatal


def read_recent(n: int = AUDIT_LOG_DISPLAY_LIMIT) -> list[AuditEntry]:
    """
    Read the last N audit entries from the log file.
    Returns entries in reverse-chronological order (newest first).
    """
    if not os.path.exists(AUDIT_LOG_PATH):
        return []

    entries: list[AuditEntry] = []
    try:
        with open(AUDIT_LOG_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                entries.append(AuditEntry(**data))
            except (json.JSONDecodeError, TypeError):
                continue
            if len(entries) >= n:
                break
    except OSError:
        pass

    return entries


# ---------------------------------------------------------------------------
# Edit audit (inline cell edits via the data editor)
# ---------------------------------------------------------------------------

@dataclass
class EditAuditEntry:
    """One Save action from the inline data editor."""
    timestamp:    str
    env:          str            # destination environment key ("dev"/"qa"/"prod")
    table:        str
    client_id:    int
    rows_changed: int
    backup_table: str            # backup created before the save, or "" if none
    changes: list[dict] = field(default_factory=list)
    # Each element: {"pk": {col: val, ...}, "before": {col: val, ...}, "after": {col: val, ...}}


def log_edit(
    env:          str,
    table:        str,
    client_id:    int,
    changes:      list[dict],   # list of {"pk", "before", "after"} dicts
    backup_table: str = "",
) -> None:
    """Append an edit audit entry to edit_audit.log."""
    entry = EditAuditEntry(
        timestamp    = datetime.now().isoformat(timespec="seconds"),
        env          = env,
        table        = table,
        client_id    = client_id,
        rows_changed = len(changes),
        backup_table = backup_table,
        changes      = changes,
    )
    line = json.dumps(asdict(entry)) + "\n"
    try:
        with open(EDIT_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass


def read_edit_logs(n: int = AUDIT_LOG_DISPLAY_LIMIT) -> list[EditAuditEntry]:
    """
    Read the last N edit audit entries, newest first.
    Skips malformed lines silently.
    """
    if not os.path.exists(EDIT_LOG_PATH):
        return []

    entries: list[EditAuditEntry] = []
    try:
        with open(EDIT_LOG_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                entries.append(EditAuditEntry(**data))
            except (json.JSONDecodeError, TypeError, KeyError):
                continue
            if len(entries) >= n:
                break
    except OSError:
        pass

    return entries


def make_entry(
    source_env: str,
    target_env: str,
    client_id: int,
    tables: list,           # list[TableInfo]
    row_counts: dict[str, int],
    status: str,
    error_message: str = "",
    ticket_number: str = "",
    backup_tables: list[str] = None,
    dry_run: bool = False,
) -> AuditEntry:
    """Convenience constructor for AuditEntry."""
    if dry_run:
        status = "dry_run"
    return AuditEntry(
        timestamp=datetime.now().isoformat(timespec="seconds"),
        source_env=source_env,
        target_env=target_env,
        client_id=client_id,
        tables_migrated=[t.name for t in tables],
        row_counts=row_counts,
        status=status,
        error_message=error_message,
        ticket_number=ticket_number,
        backup_tables=backup_tables or [],
    )
