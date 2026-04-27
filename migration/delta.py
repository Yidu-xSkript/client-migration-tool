# migration/delta.py — Incremental (delta) sync: only migrate changed rows
#
# Instead of DELETE + full INSERT, computes a diff by comparing MD5 checksums
# of each row between source and destination, then only transfers what changed.
# Requires tables to have a PRIMARY KEY.

from __future__ import annotations
from dataclasses import dataclass, field
import pymysql

from db.operations import batch_insert, upsert_batch, skip_existing_insert
from config import BATCH_SIZE, DELTA_CHECKSUM_BATCH


@dataclass
class TableDelta:
    table: str
    pk_cols: list[str]
    to_insert: list[dict]   # Rows in src, not in dst
    to_update: list[dict]   # Rows in both but checksum differs
    to_delete: list[tuple]  # PK tuples in dst, not in src
    src_total: int = 0
    dst_total: int = 0


@dataclass
class DeltaApplyResult:
    table: str
    inserted: int = 0
    updated: int = 0
    deleted: int = 0
    status: str = "ok"
    error: str = ""


# ---------------------------------------------------------------------------
# Primary key discovery
# ---------------------------------------------------------------------------

def get_primary_keys(table: str, conn) -> list[str]:
    """Return the ordered list of PRIMARY KEY columns for a table."""
    sql = """
        SELECT COLUMN_NAME
        FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE
        WHERE TABLE_SCHEMA    = DATABASE()
          AND TABLE_NAME      = %s
          AND CONSTRAINT_NAME = 'PRIMARY'
        ORDER BY ORDINAL_POSITION
    """
    with conn.cursor() as cur:
        cur.execute(sql, (table,))
        rows = cur.fetchall()
    return [r["COLUMN_NAME"] for r in rows]


# ---------------------------------------------------------------------------
# Checksum computation
# ---------------------------------------------------------------------------

def _compute_checksums(
    table: str,
    pk_cols: list[str],
    client_id_col: str,
    client_id: int,
    conn,
) -> dict[tuple, str]:
    """
    Return {pk_tuple: md5_checksum} for all rows belonging to client_id.
    Uses MySQL's MD5(CONCAT_WS) for a stable row fingerprint.
    """
    if not pk_cols:
        return {}

    pk_select  = ", ".join(f"`{c}`" for c in pk_cols)
    # Coerce everything to CHAR so NULL/type differences don't break CONCAT_WS
    all_cols_sql = f"""
        SELECT COLUMN_NAME
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s
        ORDER BY ORDINAL_POSITION
    """
    with conn.cursor() as cur:
        cur.execute(all_cols_sql, (table,))
        all_cols = [r["COLUMN_NAME"] for r in cur.fetchall()]

    concat_parts = ", ".join(f"CAST(`{c}` AS CHAR)" for c in all_cols)
    checksum_expr = f"MD5(CONCAT_WS('||', {concat_parts}))"

    sql = (
        f"SELECT {pk_select}, {checksum_expr} AS _chk "
        f"FROM `{table}` WHERE `{client_id_col}` = %s"
    )

    result: dict[tuple, str] = {}
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (client_id,))
            for row in cur.fetchall():
                pk_vals = tuple(row[c] for c in pk_cols)
                result[pk_vals] = row["_chk"] or ""
    except pymysql.Error:
        pass

    return result


# ---------------------------------------------------------------------------
# Full row fetch (for building insert/update payloads)
# ---------------------------------------------------------------------------

def _fetch_rows_by_pks(
    table: str,
    pk_cols: list[str],
    pk_tuples: set[tuple],
    client_id_col: str,
    client_id: int,
    conn,
    batch_size: int = DELTA_CHECKSUM_BATCH,
) -> list[dict]:
    """Fetch full row data for a specific set of PK values."""
    if not pk_tuples:
        return []

    all_rows: list[dict] = []
    pk_list = list(pk_tuples)

    for i in range(0, len(pk_list), batch_size):
        batch = pk_list[i : i + batch_size]
        if len(pk_cols) == 1:
            placeholders = ", ".join(["%s"] * len(batch))
            sql = (
                f"SELECT * FROM `{table}` "
                f"WHERE `{client_id_col}` = %s "
                f"AND `{pk_cols[0]}` IN ({placeholders})"
            )
            params = [client_id] + [pk[0] for pk in batch]
        else:
            # Composite PK: use OR of AND conditions
            conditions = " OR ".join(
                "(" + " AND ".join(f"`{c}` = %s" for c in pk_cols) + ")"
                for _ in batch
            )
            sql = f"SELECT * FROM `{table}` WHERE `{client_id_col}` = %s AND ({conditions})"
            params = [client_id]
            for pk in batch:
                params.extend(pk)

        with conn.cursor() as cur:
            cur.execute(sql, params)
            all_rows.extend(cur.fetchall())

    return all_rows


# ---------------------------------------------------------------------------
# Delta computation
# ---------------------------------------------------------------------------

def compute_table_delta(
    info,           # TableInfo
    client_id: int,
    src_conn,
    dst_conn,
) -> TableDelta | None:
    """
    Compute the diff between source and destination for one table.
    Returns None if the table has no primary key (falls back to full replace).
    """
    pk_cols = get_primary_keys(info.name, src_conn)
    if not pk_cols:
        return None  # Cannot delta-sync without a PK

    src_checksums = _compute_checksums(info.name, pk_cols, info.client_id_column, client_id, src_conn)
    dst_checksums = _compute_checksums(info.name, pk_cols, info.client_id_column, client_id, dst_conn)

    src_keys = set(src_checksums.keys())
    dst_keys = set(dst_checksums.keys())

    new_keys     = src_keys - dst_keys
    deleted_keys = dst_keys - src_keys
    common_keys  = src_keys & dst_keys
    changed_keys = {k for k in common_keys if src_checksums[k] != dst_checksums[k]}

    # Fetch actual row data for inserts and updates
    rows_to_insert = _fetch_rows_by_pks(
        info.name, pk_cols, new_keys, info.client_id_column, client_id, src_conn
    )
    rows_to_update = _fetch_rows_by_pks(
        info.name, pk_cols, changed_keys, info.client_id_column, client_id, src_conn
    )

    return TableDelta(
        table=info.name,
        pk_cols=pk_cols,
        to_insert=rows_to_insert,
        to_update=rows_to_update,
        to_delete=list(deleted_keys),
        src_total=len(src_keys),
        dst_total=len(dst_keys),
    )


# ---------------------------------------------------------------------------
# Delta application (within an open transaction)
# ---------------------------------------------------------------------------

def apply_table_delta(
    delta: TableDelta,
    pk_cols: list[str],
    dst_conn,
    batch_size: int = BATCH_SIZE,
) -> DeltaApplyResult:
    result = DeltaApplyResult(table=delta.table)

    try:
        # Inserts
        if delta.to_insert:
            result.inserted = batch_insert(delta.table, delta.to_insert, dst_conn, batch_size)

        # Updates — use upsert so we don't need separate UPDATE statements
        if delta.to_update:
            result.updated = upsert_batch(delta.table, delta.to_update, dst_conn, batch_size)

        # Deletes — only remove specific PKs, not all client rows
        if delta.to_delete:
            result.deleted = _delete_by_pks(delta.table, pk_cols, delta.to_delete, dst_conn)

    except pymysql.Error as e:
        result.status = "error"
        result.error = str(e)

    return result


def _delete_by_pks(table: str, pk_cols: list[str], pk_tuples: list[tuple], conn) -> int:
    """Delete specific rows by primary key."""
    if not pk_tuples:
        return 0

    total = 0
    for pk in pk_tuples:
        conditions = " AND ".join(f"`{c}` = %s" for c in pk_cols)
        sql = f"DELETE FROM `{table}` WHERE {conditions}"
        with conn.cursor() as cur:
            cur.execute(sql, pk)
            total += cur.rowcount
    return total
