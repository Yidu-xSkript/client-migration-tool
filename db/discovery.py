# db/discovery.py — Table discovery and dependency ordering

from __future__ import annotations
from dataclasses import dataclass, field
import pymysql
from config import CLIENT_TABLE, CLIENT_ID_COLUMN


@dataclass
class TableInfo:
    name: str                          # Table name
    client_id_column: str              # Column name that holds the ClientId
    fk_parents: list[str] = field(default_factory=list)  # Tables this one depends on
    row_count: int = 0                 # Row count for display (filled in later)


# ---------------------------------------------------------------------------
# Main discovery entry point
# ---------------------------------------------------------------------------

def discover_related_tables(conn: pymysql.connections.Connection) -> list[TableInfo]:
    """
    Discover all tables related to the Client table in the given database.

    Strategy:
      1. Find all FK relationships in the schema (full graph).
      2. Find all tables with a ClientId column as fallback.
      3. Build the set of tables that directly or transitively reference Client.
      4. Return them in topological order (parents before children).

    Always includes the Client table itself as the first entry.
    """
    db_name = _get_current_db(conn)
    fk_graph = _build_fk_graph(conn, db_name)           # {table: [parent_table, ...]}
    client_id_tables = _find_client_id_column_tables(conn, db_name)

    # Seed: tables that directly reference Client via FK
    direct_fk_refs = {
        tbl for tbl, parents in fk_graph.items()
        if CLIENT_TABLE in parents
    }

    # Union with tables that simply have a ClientId column
    all_related = direct_fk_refs | client_id_tables
    # Always include the Client table itself
    all_related.discard(CLIENT_TABLE)

    # Determine the ClientId column name per table
    client_id_col_map = _get_client_id_column_map(conn, db_name, all_related)

    # Build TableInfo list
    table_infos: dict[str, TableInfo] = {}

    # Client table always first
    table_infos[CLIENT_TABLE] = TableInfo(
        name=CLIENT_TABLE,
        client_id_column=CLIENT_ID_COLUMN,
        fk_parents=[],
    )

    for tbl in all_related:
        col = client_id_col_map.get(tbl, CLIENT_ID_COLUMN)
        parents = fk_graph.get(tbl, [])
        # Only keep parents that are in our related set (or Client itself)
        relevant_parents = [p for p in parents if p in all_related or p == CLIENT_TABLE]
        table_infos[tbl] = TableInfo(
            name=tbl,
            client_id_column=col,
            fk_parents=relevant_parents,
        )

    # Topological sort
    ordered = _topological_sort(table_infos)
    return ordered


# ---------------------------------------------------------------------------
# FK graph helpers
# ---------------------------------------------------------------------------

def _build_fk_graph(conn, db_name: str) -> dict[str, list[str]]:
    """
    Return a dict mapping each table to the list of tables it has FK references to.
    Uses INFORMATION_SCHEMA for full accuracy.
    """
    sql = """
        SELECT
            kcu.TABLE_NAME,
            kcu.REFERENCED_TABLE_NAME
        FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE AS kcu
        JOIN INFORMATION_SCHEMA.REFERENTIAL_CONSTRAINTS AS rc
            ON  kcu.CONSTRAINT_NAME   = rc.CONSTRAINT_NAME
            AND kcu.TABLE_SCHEMA      = rc.CONSTRAINT_SCHEMA
        WHERE kcu.TABLE_SCHEMA            = %s
          AND kcu.REFERENCED_TABLE_NAME   IS NOT NULL
    """
    graph: dict[str, list[str]] = {}
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (db_name,))
            for row in cur.fetchall():
                child = row["TABLE_NAME"]
                parent = row["REFERENCED_TABLE_NAME"]
                graph.setdefault(child, [])
                if parent not in graph[child]:
                    graph[child].append(parent)
    except pymysql.Error:
        pass  # If INFORMATION_SCHEMA is unavailable, fall back to column scan only
    return graph


def _find_client_id_column_tables(conn, db_name: str) -> set[str]:
    """
    Find all tables (excluding Client itself) that have a column named ClientId.
    This is the fallback discovery method for tables without formal FK constraints.
    """
    sql = """
        SELECT TABLE_NAME
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE COLUMN_NAME   = %s
          AND TABLE_SCHEMA  = %s
          AND TABLE_NAME   != %s
    """
    tables: set[str] = set()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (CLIENT_ID_COLUMN, db_name, CLIENT_TABLE))
            for row in cur.fetchall():
                tables.add(row["TABLE_NAME"])
    except pymysql.Error:
        pass
    return tables


def _get_client_id_column_map(conn, db_name: str, tables: set[str]) -> dict[str, str]:
    """
    For each table, return the actual column name used to link to ClientId.
    Prefers a column literally named 'ClientId'; otherwise uses the FK source column.
    """
    if not tables:
        return {}

    placeholders = ", ".join(["%s"] * len(tables))
    sql = f"""
        SELECT TABLE_NAME, COLUMN_NAME
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = %s
          AND TABLE_NAME IN ({placeholders})
          AND COLUMN_NAME = %s
    """
    col_map: dict[str, str] = {}
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (db_name, *tables, CLIENT_ID_COLUMN))
            for row in cur.fetchall():
                col_map[row["TABLE_NAME"]] = row["COLUMN_NAME"]
    except pymysql.Error:
        pass

    # Default to CLIENT_ID_COLUMN for any not found
    for tbl in tables:
        col_map.setdefault(tbl, CLIENT_ID_COLUMN)
    return col_map


def _get_current_db(conn) -> str:
    with conn.cursor() as cur:
        cur.execute("SELECT DATABASE() AS db")
        row = cur.fetchone()
    return row["db"] if row else ""


# ---------------------------------------------------------------------------
# Topological sort
# ---------------------------------------------------------------------------

def _topological_sort(table_infos: dict[str, TableInfo]) -> list[TableInfo]:
    """
    Return TableInfo list ordered so parents always come before children.
    Uses Kahn's algorithm (BFS-based topological sort).
    """
    in_degree = {t: 0 for t in table_infos}
    adjacency: dict[str, list[str]] = {t: [] for t in table_infos}

    for tbl, info in table_infos.items():
        for parent in info.fk_parents:
            if parent in table_infos:
                adjacency[parent].append(tbl)
                in_degree[tbl] += 1

    # Start with nodes that have no dependencies
    queue = [t for t, deg in in_degree.items() if deg == 0]
    # Ensure Client table is processed first if it has degree 0
    if CLIENT_TABLE in queue:
        queue.remove(CLIENT_TABLE)
        queue.insert(0, CLIENT_TABLE)

    ordered: list[TableInfo] = []
    while queue:
        node = queue.pop(0)
        ordered.append(table_infos[node])
        for child in adjacency[node]:
            in_degree[child] -= 1
            if in_degree[child] == 0:
                queue.append(child)

    # Append any remaining (handles cycles gracefully)
    seen = {t.name for t in ordered}
    for tbl, info in table_infos.items():
        if tbl not in seen:
            ordered.append(info)

    return ordered
