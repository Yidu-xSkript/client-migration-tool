# db/operations.py — Low-level database read/write operations

from __future__ import annotations
from datetime import datetime, timedelta
import pymysql

from config import (
    CLIENT_TABLE, CLIENT_ID_COLUMN, CLIENT_SEARCH_COLUMNS,
    BATCH_SIZE, DASHBOARD_CLIENT_LIMIT, FORBIDDEN_FILTER_KEYWORDS,
)


# ---------------------------------------------------------------------------
# Schema introspection helpers
# ---------------------------------------------------------------------------

def get_existing_tables(conn) -> set[str]:
    """Return the set of all table names that exist in the current database."""
    with conn.cursor() as cur:
        cur.execute("SHOW TABLES")
        return {list(row.values())[0] for row in cur.fetchall()}


def get_table_columns(table: str, conn) -> list[str]:
    """Return all column names for a table in ordinal order."""
    sql = """
        SELECT COLUMN_NAME
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s
        ORDER BY ORDINAL_POSITION
    """
    with conn.cursor() as cur:
        cur.execute(sql, (table,))
        return [r["COLUMN_NAME"] for r in cur.fetchall()]


def get_column_schema(table: str, conn) -> list[dict]:
    """Return column definitions for schema drift comparison."""
    sql = """
        SELECT COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH,
               NUMERIC_PRECISION, IS_NULLABLE, COLUMN_DEFAULT
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s
        ORDER BY ORDINAL_POSITION
    """
    with conn.cursor() as cur:
        cur.execute(sql, (table,))
        return cur.fetchall()


# ---------------------------------------------------------------------------
# Client read operations
# ---------------------------------------------------------------------------

def get_row_count(table: str, client_id_col: str, client_id: int, conn) -> int:
    """Return the number of rows for the given ClientId in a table."""
    sql = f"SELECT COUNT(*) AS cnt FROM `{table}` WHERE `{client_id_col}` = %s"
    with conn.cursor() as cur:
        cur.execute(sql, (client_id,))
        row = cur.fetchone()
    return int(row["cnt"]) if row else 0


def get_all_row_counts(tables: list, client_id: int, conn) -> dict[str, int]:
    """Return {table_name: row_count} for all given tables."""
    counts = {}
    for info in tables:
        try:
            counts[info.name] = get_row_count(info.name, info.client_id_column, client_id, conn)
        except pymysql.Error:
            counts[info.name] = -1
    return counts


def sample_client_data(
    table: str,
    client_id_col: str,
    client_id: int,
    conn,
    limit: int = 10,
) -> list[dict]:
    """Fetch up to `limit` rows for the given client — used for dry run previews."""
    sql = f"SELECT * FROM `{table}` WHERE `{client_id_col}` = %s LIMIT %s"
    with conn.cursor() as cur:
        cur.execute(sql, (client_id, limit))
        return cur.fetchall()


def read_client_data(
    table: str,
    client_id_col: str,
    client_id: int,
    conn,
    exclude_columns: list[str] | None = None,
    row_filter: str | None = None,
) -> list[dict]:
    """
    Fetch all rows for the given ClientId from a table.

    exclude_columns: column names to omit from the SELECT (e.g. cache fields).
    row_filter:      additional SQL WHERE expression (e.g. "is_deleted = 0").
                     Validated before use — never pass raw user input without
                     calling validation.validate_row_filter() first.
    """
    if exclude_columns:
        all_cols = get_table_columns(table, conn)
        included = [c for c in all_cols if c not in exclude_columns]
        if not included:
            return []
        col_list = ", ".join(f"`{c}`" for c in included)
        select_part = col_list
    else:
        select_part = "*"

    where = f"`{client_id_col}` = %s"
    if row_filter and row_filter.strip():
        where += f" AND ({row_filter})"

    sql = f"SELECT {select_part} FROM `{table}` WHERE {where}"
    with conn.cursor() as cur:
        cur.execute(sql, (client_id,))
        return cur.fetchall()


def search_clients(query: str, conn) -> list[dict]:
    """Search the Client table by name, email, or company. Returns ≤50 rows."""
    existing_cols = _get_existing_search_columns(conn)
    if not existing_cols:
        return []

    like_val = f"%{query}%"
    conditions = " OR ".join(f"`{col}` LIKE %s" for col in existing_cols)
    params = [like_val] * len(existing_cols)
    sql = f"SELECT * FROM `{CLIENT_TABLE}` WHERE {conditions} LIMIT 50"
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def get_client_by_id(client_id: int, conn) -> dict | None:
    """Fetch a single client row by primary key."""
    sql = f"SELECT * FROM `{CLIENT_TABLE}` WHERE `{CLIENT_ID_COLUMN}` = %s"
    with conn.cursor() as cur:
        cur.execute(sql, (client_id,))
        return cur.fetchone()


def get_all_clients(conn, limit: int = DASHBOARD_CLIENT_LIMIT) -> list[dict]:
    """Return basic info for all clients (used by the Drift Dashboard)."""
    sql = f"SELECT * FROM `{CLIENT_TABLE}` LIMIT %s"
    with conn.cursor() as cur:
        cur.execute(sql, (limit,))
        return cur.fetchall()


def get_client_totals_all(tables: list, conn) -> dict[int, int]:
    """
    Return {client_id: total_row_count_across_all_tables} in a single UNION query.
    Used by the Drift Dashboard for efficient cross-env comparison.
    """
    if not tables:
        return {}

    unions = []
    for info in tables:
        unions.append(
            f"SELECT `{info.client_id_column}` AS cid, COUNT(*) AS cnt "
            f"FROM `{info.name}` GROUP BY `{info.client_id_column}`"
        )

    sql = (
        f"SELECT cid, SUM(cnt) AS total "
        f"FROM ({' UNION ALL '.join(unions)}) t "
        f"GROUP BY cid"
    )
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            return {int(row["cid"]): int(row["total"]) for row in cur.fetchall() if row["cid"]}
    except pymysql.Error:
        return {}


def _get_existing_search_columns(conn) -> list[str]:
    sql = """
        SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s
    """
    with conn.cursor() as cur:
        cur.execute(sql, (CLIENT_TABLE,))
        existing = {r["COLUMN_NAME"] for r in cur.fetchall()}
    return [col for col in CLIENT_SEARCH_COLUMNS if col in existing]


# ---------------------------------------------------------------------------
# Write operations (call within an explicit transaction)
# ---------------------------------------------------------------------------

def update_rows(table: str, rows: list[dict], pk_cols: list[str], conn) -> int:
    """
    UPDATE specific rows in a table.  Each dict in `rows` must contain all PK
    columns so the correct row can be located.  Non-PK columns are SET.
    Returns the total number of rows affected.
    """
    if not rows or not pk_cols:
        return 0
    updated = 0
    for row in rows:
        set_cols = [c for c in row if c not in pk_cols]
        if not set_cols:
            continue
        set_clause   = ", ".join(f"`{c}` = %s" for c in set_cols)
        where_clause = " AND ".join(f"`{c}` = %s" for c in pk_cols)
        sql = f"UPDATE `{table}` SET {set_clause} WHERE {where_clause}"
        values = [row[c] for c in set_cols] + [row[c] for c in pk_cols]
        with conn.cursor() as cur:
            cur.execute(sql, values)
            updated += cur.rowcount
    conn.commit()
    return updated


def delete_client_data(table: str, client_id_col: str, client_id: int, conn) -> int:
    sql = f"DELETE FROM `{table}` WHERE `{client_id_col}` = %s"
    with conn.cursor() as cur:
        cur.execute(sql, (client_id,))
        return cur.rowcount


def batch_insert(table: str, rows: list[dict], conn, batch_size: int = BATCH_SIZE) -> int:
    if not rows:
        return 0
    columns = list(rows[0].keys())
    col_list = ", ".join(f"`{c}`" for c in columns)
    placeholders = ", ".join(["%s"] * len(columns))
    sql = f"INSERT INTO `{table}` ({col_list}) VALUES ({placeholders})"
    total = 0
    for i in range(0, len(rows), batch_size):
        batch = rows[i : i + batch_size]
        values = [tuple(row[c] for c in columns) for row in batch]
        with conn.cursor() as cur:
            cur.executemany(sql, values)
            total += cur.rowcount
    return total


def upsert_batch(table: str, rows: list[dict], conn, batch_size: int = BATCH_SIZE) -> int:
    if not rows:
        return 0
    columns = list(rows[0].keys())
    col_list = ", ".join(f"`{c}`" for c in columns)
    placeholders = ", ".join(["%s"] * len(columns))
    update_clause = ", ".join(f"`{c}` = VALUES(`{c}`)" for c in columns)
    sql = (
        f"INSERT INTO `{table}` ({col_list}) VALUES ({placeholders}) "
        f"ON DUPLICATE KEY UPDATE {update_clause}"
    )
    total = 0
    for i in range(0, len(rows), batch_size):
        batch = rows[i : i + batch_size]
        values = [tuple(row[c] for c in columns) for row in batch]
        with conn.cursor() as cur:
            cur.executemany(sql, values)
            total += cur.rowcount
    return total


def skip_existing_insert(table: str, rows: list[dict], conn, batch_size: int = BATCH_SIZE) -> int:
    if not rows:
        return 0
    columns = list(rows[0].keys())
    col_list = ", ".join(f"`{c}`" for c in columns)
    placeholders = ", ".join(["%s"] * len(columns))
    sql = f"INSERT IGNORE INTO `{table}` ({col_list}) VALUES ({placeholders})"
    total = 0
    for i in range(0, len(rows), batch_size):
        batch = rows[i : i + batch_size]
        values = [tuple(row[c] for c in columns) for row in batch]
        with conn.cursor() as cur:
            cur.executemany(sql, values)
            total += cur.rowcount
    return total


# ---------------------------------------------------------------------------
# Backup table helpers
# ---------------------------------------------------------------------------

def list_backup_tables(conn) -> list[str]:
    """Return all backup table names matching the clt_bkp_ prefix."""
    with conn.cursor() as cur:
        cur.execute("SHOW TABLES LIKE 'clt\\_bkp\\_%'")
        rows = cur.fetchall()
    return [list(r.values())[0] for r in rows]


def restore_from_backup(backup_table: str, target_table: str, client_id_col: str, client_id: int, conn) -> int:
    """
    Restore client data from a backup table back into the original table.
    Deletes existing data first, then re-inserts from backup.
    Must be called within an open transaction.
    """
    with conn.cursor() as cur:
        cur.execute(f"DELETE FROM `{target_table}` WHERE `{client_id_col}` = %s", (client_id,))
        deleted = cur.rowcount
        cur.execute(f"INSERT INTO `{target_table}` SELECT * FROM `{backup_table}`")
        inserted = cur.rowcount
    return inserted


def drop_table(table_name: str, conn) -> None:
    """Drop a table. Use with care — intended for backup cleanup only."""
    with conn.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS `{table_name}`")
    conn.commit()


def get_table_size_mb(table: str, conn) -> float:
    """Return approximate table size in MB (from INFORMATION_SCHEMA)."""
    sql = """
        SELECT ROUND((DATA_LENGTH + INDEX_LENGTH) / 1024 / 1024, 2) AS size_mb
        FROM INFORMATION_SCHEMA.TABLES
        WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s
    """
    with conn.cursor() as cur:
        cur.execute(sql, (table,))
        row = cur.fetchone()
    return float(row["size_mb"]) if row and row["size_mb"] else 0.0
