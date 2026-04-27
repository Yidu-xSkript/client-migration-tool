# migration/engine.py — Core migration orchestrator

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Callable
import pymysql

from db.operations import (
    read_client_data,
    sample_client_data,
    delete_client_data,
    get_row_count,
    get_existing_tables,
    batch_insert,
    upsert_batch,
    skip_existing_insert,
)
from config import BATCH_SIZE, PREVIEW_ROW_SAMPLE
from migration.delta import compute_table_delta, apply_table_delta
from migration.validation import run_post_checks, ValidationResult


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class TableDryRunInfo:
    table: str
    src_rows: int           # Total rows in source for this client
    dst_rows: int           # Total rows in destination for this client
    action: str             # "replace" | "skip" | "update" | "delta" | "N/A"
    delta_insert: int = 0
    delta_update: int = 0
    delta_delete: int = 0
    missing_in: str = ""    # "" = exists in both | "dst" | "src" | "both"
    # Actual sampled row data for rich preview display
    src_sample: list[dict] = field(default_factory=list)   # Rows from source (what will be written)
    dst_sample: list[dict] = field(default_factory=list)   # Rows from dest (current state / what gets removed)
    # For delta mode: the specific rows in each bucket
    delta_insert_rows: list[dict] = field(default_factory=list)
    delta_update_rows: list[dict] = field(default_factory=list)  # (src_row, dst_row) pairs
    delta_delete_rows: list[dict] = field(default_factory=list)
    sample_limit: int = PREVIEW_ROW_SAMPLE
    # Set to True after a per-table migration runs — used to update card labels
    migrated: bool = False


@dataclass
class DryRunResult:
    client_id: int
    source_env: str
    target_env: str
    tables: list[TableDryRunInfo] = field(default_factory=list)
    conflict_mode: str = "replace"
    delta_mode: bool = False


@dataclass
class TableMigrationResult:
    table: str
    deleted: int = 0
    inserted: int = 0
    updated: int = 0
    status: str = "ok"     # "ok" | "skipped" | "missing" | "error"
    error: str = ""
    mode: str = "full"     # "full" | "delta"
    missing_in: str = ""   # "" | "dst" | "src"


@dataclass
class MigrationResult:
    client_id: int
    source_env: str
    target_env: str
    tables: list[TableMigrationResult] = field(default_factory=list)
    success: bool = True
    error_message: str = ""
    post_validation: ValidationResult | None = None

    @property
    def total_inserted(self) -> int:
        return sum(t.inserted for t in self.tables)

    @property
    def total_deleted(self) -> int:
        return sum(t.deleted for t in self.tables)

    @property
    def total_updated(self) -> int:
        return sum(t.updated for t in self.tables)


LogCallback        = Callable[[str, str], None]           # (message, level)
TableStartCallback = Callable[[str, int, int], None]      # (table_name, index, total)


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------

def dry_run(
    client_id: int,
    tables: list,
    src_conn,
    dst_conn,
    source_env: str,
    target_env: str,
    conflict_mode: str = "replace",
    delta_mode: bool = False,
) -> DryRunResult:
    """
    Simulate a migration without writing anything.
    In delta mode, computes the actual diff (slower but accurate preview).
    """
    result = DryRunResult(
        client_id=client_id,
        source_env=source_env,
        target_env=target_env,
        conflict_mode=conflict_mode,
        delta_mode=delta_mode,
    )

    src_existing = get_existing_tables(src_conn)
    dst_existing = get_existing_tables(dst_conn)

    for info in tables:
        in_src = info.name in src_existing
        in_dst = info.name in dst_existing

        if not in_src or not in_dst:
            missing_in = ("both" if not in_src and not in_dst
                          else "src" if not in_src else "dst")
            result.tables.append(TableDryRunInfo(
                table=info.name,
                src_rows=0,
                dst_rows=0,
                action="N/A",
                missing_in=missing_in,
            ))
            continue

        src_count = get_row_count(info.name, info.client_id_column, client_id, src_conn)
        dst_count = get_row_count(info.name, info.client_id_column, client_id, dst_conn)

        entry = TableDryRunInfo(
            table=info.name,
            src_rows=src_count,
            dst_rows=dst_count,
            action="delta" if delta_mode else conflict_mode,
        )

        # Fetch actual row samples for the preview UI
        entry.src_sample = sample_client_data(
            info.name, info.client_id_column, client_id, src_conn, PREVIEW_ROW_SAMPLE
        )
        entry.dst_sample = sample_client_data(
            info.name, info.client_id_column, client_id, dst_conn, PREVIEW_ROW_SAMPLE
        )

        if delta_mode:
            delta = compute_table_delta(info, client_id, src_conn, dst_conn)
            if delta:
                entry.delta_insert = len(delta.to_insert)
                entry.delta_update = len(delta.to_update)
                entry.delta_delete = len(delta.to_delete)
                entry.delta_insert_rows = delta.to_insert[:PREVIEW_ROW_SAMPLE]
                entry.delta_update_rows = delta.to_update[:PREVIEW_ROW_SAMPLE]
                entry.delta_delete_rows = delta.to_delete[:PREVIEW_ROW_SAMPLE]

        result.tables.append(entry)

    return result


# ---------------------------------------------------------------------------
# Live migration
# ---------------------------------------------------------------------------

def run_migration(
    client_id: int,
    tables: list,
    src_conn,
    dst_conn,
    source_env: str,
    target_env: str,
    conflict_mode: str = "replace",
    delta_mode: bool = False,
    batch_size: int = BATCH_SIZE,
    excluded_columns: dict[str, list[str]] | None = None,
    row_filters: dict[str, str] | None = None,
    post_validate: bool = True,
    progress_callback: LogCallback | None = None,
    on_table_start: TableStartCallback | None = None,
) -> MigrationResult:
    """
    Execute a migration for a single client across all given tables.

    delta_mode:       Only transfer rows that differ between src and dst.
    excluded_columns: {table_name: [col1, col2, ...]} — omit these from SELECT.
    row_filters:      {table_name: "SQL WHERE expression"} — extra filter on source.
    post_validate:    Run integrity checks after commit and attach to result.
    """
    result = MigrationResult(
        client_id=client_id,
        source_env=source_env,
        target_env=target_env,
    )

    def log(msg: str, level: str = "info"):
        if progress_callback:
            progress_callback(msg, level)

    try:
        with dst_conn.cursor() as cur:
            cur.execute("SET FOREIGN_KEY_CHECKS = 0")

        src_existing = get_existing_tables(src_conn)
        dst_existing = get_existing_tables(dst_conn)
        total_tables = len(tables)

        for tbl_idx, info in enumerate(tables):
            if on_table_start:
                on_table_start(info.name, tbl_idx, total_tables)

            # Skip tables that don't exist in source or destination
            if info.name not in src_existing:
                result.tables.append(TableMigrationResult(
                    table=info.name, status="missing", missing_in="src",
                    error="Table does not exist in source",
                ))
                log(f"[{info.name}] Skipped — not found in source.", "warning")
                continue
            if info.name not in dst_existing:
                result.tables.append(TableMigrationResult(
                    table=info.name, status="missing", missing_in="dst",
                    error="Table does not exist in destination",
                ))
                log(f"[{info.name}] Skipped — not found in destination.", "warning")
                continue
            excl = (excluded_columns or {}).get(info.name, [])
            row_filter = (row_filters or {}).get(info.name, None)

            if delta_mode:
                tbl_result = _migrate_table_delta(
                    client_id=client_id,
                    info=info,
                    src_conn=src_conn,
                    dst_conn=dst_conn,
                    exclude_columns=excl,
                    row_filter=row_filter,
                    batch_size=batch_size,
                    log=log,
                )
            else:
                tbl_result = _migrate_table_full(
                    client_id=client_id,
                    info=info,
                    src_conn=src_conn,
                    dst_conn=dst_conn,
                    conflict_mode=conflict_mode,
                    exclude_columns=excl,
                    row_filter=row_filter,
                    batch_size=batch_size,
                    log=log,
                )

            result.tables.append(tbl_result)

            if tbl_result.status == "error":
                raise RuntimeError(f"Table `{info.name}` failed: {tbl_result.error}")

        dst_conn.commit()
        log("Transaction committed successfully.", "info")

    except Exception as e:
        dst_conn.rollback()
        result.success = False
        result.error_message = str(e)
        log(f"Migration rolled back. Error: {e}", "error")

    finally:
        try:
            with dst_conn.cursor() as cur:
                cur.execute("SET FOREIGN_KEY_CHECKS = 1")
            dst_conn.commit()
        except Exception:
            pass

    # Post-migration validation (non-blocking — attaches to result, doesn't raise)
    if result.success and post_validate:
        try:
            result.post_validation = run_post_checks(client_id, tables, src_conn, dst_conn)
            if not result.post_validation.passed:
                failures = "; ".join(c.message for c in result.post_validation.failures)
                log(f"Post-migration checks found issues: {failures}", "warning")
        except Exception as e:
            log(f"Post-migration validation error: {e}", "warning")

    return result


# ---------------------------------------------------------------------------
# Full-replace table migration
# ---------------------------------------------------------------------------

def _migrate_table_full(
    client_id: int,
    info,
    src_conn,
    dst_conn,
    conflict_mode: str,
    exclude_columns: list[str],
    row_filter: str | None,
    batch_size: int,
    log: LogCallback,
) -> TableMigrationResult:
    tbl = info.name
    col = info.client_id_column
    result = TableMigrationResult(table=tbl, mode="full")

    try:
        rows = read_client_data(tbl, col, client_id, src_conn, exclude_columns, row_filter)
        log(f"[{tbl}] Read {len(rows)} rows from source.", "info")

        if conflict_mode == "replace":
            result.deleted = delete_client_data(tbl, col, client_id, dst_conn)
            log(f"[{tbl}] Deleted {result.deleted} rows from destination.", "info")
            result.inserted = batch_insert(tbl, rows, dst_conn, batch_size)

        elif conflict_mode == "skip":
            result.inserted = skip_existing_insert(tbl, rows, dst_conn, batch_size)

        elif conflict_mode == "update":
            result.inserted = upsert_batch(tbl, rows, dst_conn, batch_size)

        else:
            raise ValueError(f"Unknown conflict mode: {conflict_mode}")

        result.status = "ok"
        log(f"[{tbl}] Inserted/updated {result.inserted} rows.", "info")

    except pymysql.Error as e:
        result.status = "error"
        result.error = str(e)
        log(f"[{tbl}] ERROR: {e}", "error")

    return result


# ---------------------------------------------------------------------------
# Delta table migration
# ---------------------------------------------------------------------------

def _migrate_table_delta(
    client_id: int,
    info,
    src_conn,
    dst_conn,
    exclude_columns: list[str],
    row_filter: str | None,
    batch_size: int,
    log: LogCallback,
) -> TableMigrationResult:
    tbl = info.name
    result = TableMigrationResult(table=tbl, mode="delta")

    try:
        delta = compute_table_delta(info, client_id, src_conn, dst_conn)

        if delta is None:
            # No PK — fall back to full replace for this table
            log(f"[{tbl}] No primary key found — falling back to full replace.", "warning")
            return _migrate_table_full(
                client_id=client_id,
                info=info,
                src_conn=src_conn,
                dst_conn=dst_conn,
                conflict_mode="replace",
                exclude_columns=exclude_columns,
                row_filter=row_filter,
                batch_size=batch_size,
                log=log,
            )

        log(
            f"[{tbl}] Delta: +{len(delta.to_insert)} insert, "
            f"~{len(delta.to_update)} update, "
            f"-{len(delta.to_delete)} delete.",
            "info",
        )

        if not (delta.to_insert or delta.to_update or delta.to_delete):
            log(f"[{tbl}] No changes — skipping.", "info")
            result.status = "ok"
            return result

        apply_result = apply_table_delta(delta, delta.pk_cols, dst_conn, batch_size)
        result.inserted = apply_result.inserted
        result.updated = apply_result.updated
        result.deleted = apply_result.deleted

        if apply_result.status == "error":
            result.status = "error"
            result.error = apply_result.error
            log(f"[{tbl}] Delta apply ERROR: {apply_result.error}", "error")
        else:
            result.status = "ok"
            log(
                f"[{tbl}] Delta applied: {result.inserted} inserted, "
                f"{result.updated} updated, {result.deleted} deleted.",
                "info",
            )

    except Exception as e:
        result.status = "error"
        result.error = str(e)
        log(f"[{tbl}] ERROR: {e}", "error")

    return result
