# migration/batch.py — Batch migration: process multiple clients sequentially

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Callable, Generator

from db.connection import get_connection
from db.discovery import discover_related_tables
from migration.engine import run_migration, MigrationResult
from migration.backup import create_backups
from migration import audit


@dataclass
class BatchClientResult:
    client_id: int
    success: bool
    rows_migrated: int = 0
    tables_migrated: int = 0
    error: str = ""
    migration_result: MigrationResult | None = None


@dataclass
class BatchResult:
    src_env: str
    dst_env: str
    total: int = 0
    succeeded: int = 0
    failed: int = 0
    results: list[BatchClientResult] = field(default_factory=list)

    @property
    def total_rows(self) -> int:
        return sum(r.rows_migrated for r in self.results)


ProgressCallback = Callable[[int, int, int, str], None]
# Args: (current_index, total, client_id, status_message)


def run_batch(
    client_ids: list[int],
    src_env: str,
    dst_env: str,
    conflict_mode: str = "replace",
    delta_mode: bool = False,
    do_backup: bool = True,
    excluded_columns: dict | None = None,
    row_filters: dict | None = None,
    pre_check_names: list[str] | None = None,
    ticket: str = "",
    progress_callback: ProgressCallback | None = None,
) -> Generator[BatchClientResult, None, BatchResult]:
    """
    Migrate a list of clients one by one.

    Yields a BatchClientResult after each client completes (for live UI updates).
    Returns the full BatchResult when the generator is exhausted.

    Failures for individual clients do not stop the batch — they are recorded
    and the next client is attempted immediately.
    """
    batch = BatchResult(src_env=src_env, dst_env=dst_env, total=len(client_ids))

    # Discover tables once using the source env (all clients share the same schema)
    try:
        src_conn_disc = get_connection(src_env)
        tables_all = discover_related_tables(src_conn_disc)
        src_conn_disc.close()
    except Exception as e:
        # If discovery fails, we can't migrate anything
        for cid in client_ids:
            r = BatchClientResult(client_id=cid, success=False, error=f"Discovery failed: {e}")
            batch.results.append(r)
            batch.failed += 1
            yield r
        return batch

    for idx, client_id in enumerate(client_ids):
        if progress_callback:
            progress_callback(idx, len(client_ids), client_id, "Starting…")

        client_result = BatchClientResult(client_id=client_id, success=False)

        try:
            src_conn = get_connection(src_env)
            dst_conn = get_connection(dst_env)

            backup_tables: list[str] = []
            if do_backup:
                backup_tables = create_backups(client_id, tables_all, dst_conn)

            result = run_migration(
                client_id=client_id,
                tables=tables_all,
                src_conn=src_conn,
                dst_conn=dst_conn,
                source_env=src_env,
                target_env=dst_env,
                conflict_mode=conflict_mode,
                delta_mode=delta_mode,
                excluded_columns=excluded_columns or {},
                row_filters=row_filters or {},
            )

            src_conn.close()
            dst_conn.close()

            client_result.success = result.success
            client_result.rows_migrated = result.total_inserted
            client_result.tables_migrated = len(result.tables)
            client_result.migration_result = result
            if not result.success:
                client_result.error = result.error_message

            # Audit
            row_counts = {t.table: t.inserted for t in result.tables}
            audit.log_attempt(audit.make_entry(
                source_env=src_env,
                target_env=dst_env,
                client_id=client_id,
                tables=tables_all,
                row_counts=row_counts,
                status="success" if result.success else "failure",
                error_message=result.error_message,
                ticket_number=ticket,
                backup_tables=backup_tables,
            ))

        except Exception as e:
            client_result.success = False
            client_result.error = str(e)
            audit.log_attempt(audit.make_entry(
                source_env=src_env,
                target_env=dst_env,
                client_id=client_id,
                tables=tables_all,
                row_counts={},
                status="failure",
                error_message=str(e),
                ticket_number=ticket,
            ))

        batch.results.append(client_result)
        if client_result.success:
            batch.succeeded += 1
        else:
            batch.failed += 1

        if progress_callback:
            status = "Done" if client_result.success else f"Failed: {client_result.error[:60]}"
            progress_callback(idx + 1, len(client_ids), client_id, status)

        yield client_result

    return batch
