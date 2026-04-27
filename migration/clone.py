# migration/clone.py — Copy a client's full dataset to a new ClientId within one environment

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Callable
import pymysql

from config import CLIENT_TABLE, CLIENT_ID_COLUMN
from db.operations import batch_insert

LogCallback = Callable[[str, str], None]


# ---------------------------------------------------------------------------
# Catalog entry — describes how to fetch and remap a table during clone
# ---------------------------------------------------------------------------

@dataclass
class CatalogEntry:
    table: str
    label: str
    client_id_col: str | None       # Column holding ClientId; None for indirect tables
    auto_pk: str | None = None      # Auto-inc PK to strip on insert and track for cascade
    parent_table: str | None = None     # Indirect tables: which catalog table is the parent
    parent_join_col: str | None = None  # Column in this table referencing parent PK
    # Hardcoded literal SQL appended to WHERE — never from user input
    filter_extra: str | None = None
    # {col: id_map_table_name} — extra FK columns to remap via id_map
    fk_remaps: dict | None = None
    default_enabled: bool = True
    is_root: bool = False
    group: str = "other"


# ---------------------------------------------------------------------------
# Clone catalog — ordered so parent tables always precede their children
# ---------------------------------------------------------------------------

CLONE_CATALOG: list[CatalogEntry] = [
    # ── Root ──────────────────────────────────────────────────────────────
    CatalogEntry(
        table="Client", label="Client (root record)",
        client_id_col="ClientId", is_root=True, default_enabled=True, group="core",
    ),

    # ── Core config — ClientCompany must come before tables referencing its PK ─
    CatalogEntry(
        table="ClientCompany", label="Client Companies",
        client_id_col="ClientId", auto_pk="Id",
        default_enabled=True, group="config",
    ),
    CatalogEntry(
        table="ClientWorkFlow", label="Workflow Config",
        client_id_col="ClientId",
        default_enabled=True, group="config",
    ),
    CatalogEntry(
        table="ClientSpecificConfig", label="Feature Flags / Config",
        client_id_col="ClientId",
        default_enabled=True, group="config",
    ),
    CatalogEntry(
        table="ClientInvoiceType", label="Invoice Types",
        client_id_col="ClientId",
        default_enabled=True, group="config",
    ),
    CatalogEntry(
        table="ClientInvoiceAttribute", label="Invoice Attributes",
        client_id_col="ClientID",           # capital D — actual column name in this table
        fk_remaps={"ClientCompanyId": "ClientCompany"},
        default_enabled=True, group="config",
    ),
    CatalogEntry(
        table="_x_ClientParameters", label="Client Parameters",
        client_id_col="ClientId",
        default_enabled=True, group="config",
    ),
    CatalogEntry(
        table="ClientWorkFlowFolder", label="Workflow Folders",
        client_id_col="ClientID",           # capital D — actual column name in this table
        default_enabled=True, group="config",
    ),
    CatalogEntry(
        table="EmailCaptureClients", label="Email Capture Config",
        client_id_col="ClientId", auto_pk="EmailCaptureClientId",
        default_enabled=True, group="config",
    ),

    # ── Approval workflow ──────────────────────────────────────────────────
    CatalogEntry(
        table="ApprovalStep", label="Approval Steps",
        client_id_col="ClientId", auto_pk="ApprovalStepId",
        fk_remaps={"ClientCompanyId": "ClientCompany"},
        default_enabled=True, group="approval",
    ),
    CatalogEntry(
        table="ApprovalSubStep", label="Approval Sub-Steps",
        client_id_col=None, auto_pk="ApprovalSubStepId",
        parent_table="ApprovalStep", parent_join_col="ApprovalStepId",
        default_enabled=True, group="approval",
    ),
    CatalogEntry(
        table="ApprovalSubStepUserFilter", label="Approval Sub-Step User Filters",
        client_id_col=None,
        parent_table="ApprovalSubStep", parent_join_col="ApprovalSubStepId",
        default_enabled=True, group="approval",
    ),

    # ── Users & access ─────────────────────────────────────────────────────
    CatalogEntry(
        table="User", label='Admin Users (UserName LIKE "Admin%")',
        client_id_col="ClientId", auto_pk="UserId",
        filter_extra="UserName LIKE 'Admin%' AND RoleId IS NOT NULL",
        default_enabled=True, group="users",
    ),
    CatalogEntry(
        table="UserRoles", label="User Roles",
        client_id_col=None,
        parent_table="User", parent_join_col="UserId",
        default_enabled=False, group="users",
    ),

    # ── Email capture children (off by default — rarely needed for new clients) ─
    CatalogEntry(
        table="EmailCaptureClientEmails", label="Email Capture Addresses",
        client_id_col=None, auto_pk="EmailCaptureClientEmailId",
        parent_table="EmailCaptureClients", parent_join_col="EmailCaptureClientId",
        default_enabled=False, group="email",
    ),
]

CATALOG_BY_TABLE: dict[str, CatalogEntry] = {e.table: e for e in CLONE_CATALOG}
CATALOG_TABLE_NAMES: frozenset[str] = frozenset(e.table for e in CLONE_CATALOG)

# Human-readable group labels for the UI
CATALOG_GROUP_LABELS: dict[str, str] = {
    "config":   "Core Configuration",
    "approval": "Approval Workflow",
    "users":    "Users & Access",
    "email":    "Email Capture",
}


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class CloneResult:
    src_client_id:   int
    new_client_id:   int | None
    tables_copied:   list[str] = field(default_factory=list)
    rows_copied:     dict[str, int] = field(default_factory=dict)
    fk_cloned:       dict[str, int] = field(default_factory=dict)
    excluded_tables: list[str] = field(default_factory=list)
    success:         bool = False
    error:           str = ""


@dataclass
class DeleteResult:
    client_id:          int
    tables_deleted:     list[str] = field(default_factory=list)
    rows_deleted:       dict[str, int] = field(default_factory=dict)
    fk_records_deleted: dict[str, int] = field(default_factory=dict)
    success:            bool = False
    error:              str = ""


# ---------------------------------------------------------------------------
# FK introspection helpers (used for Client row's shared FK records)
# ---------------------------------------------------------------------------

def get_outgoing_fks(table: str, conn) -> list[dict]:
    sql = """
        SELECT
            kcu.COLUMN_NAME           AS col,
            kcu.REFERENCED_TABLE_NAME  AS ref_table,
            kcu.REFERENCED_COLUMN_NAME AS ref_col
        FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu
        WHERE kcu.TABLE_SCHEMA           = DATABASE()
          AND kcu.TABLE_NAME             = %s
          AND kcu.REFERENCED_TABLE_NAME IS NOT NULL
    """
    with conn.cursor() as cur:
        cur.execute(sql, (table,))
        return cur.fetchall()


def get_pk_column(table: str, conn) -> str | None:
    sql = """
        SELECT COLUMN_NAME
        FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE
        WHERE TABLE_SCHEMA    = DATABASE()
          AND TABLE_NAME      = %s
          AND CONSTRAINT_NAME = 'PRIMARY'
        ORDER BY ORDINAL_POSITION
        LIMIT 1
    """
    with conn.cursor() as cur:
        cur.execute(sql, (table,))
        row = cur.fetchone()
    return row["COLUMN_NAME"] if row else None


def get_fk_columns_set(table: str, conn) -> set[str]:
    return {fk["col"] for fk in get_outgoing_fks(table, conn)}


# ---------------------------------------------------------------------------
# Shared-entity record cloner (for Client table's own FK references, e.g. Address)
# ---------------------------------------------------------------------------

def _clone_shared_record(
    ref_table: str,
    ref_col:   str,
    old_id,
    conn,
    fk_map:    dict,
    log:       LogCallback,
) -> object:
    cache_key = (ref_table, str(old_id))
    if cache_key in fk_map:
        return fk_map[cache_key]

    with conn.cursor() as cur:
        cur.execute(f"SELECT * FROM `{ref_table}` WHERE `{ref_col}` = %s", (old_id,))
        row = cur.fetchone()

    if not row:
        log(f"  [{ref_table}] FK source row {ref_col}={old_id} not found — keeping original value.", "warning")
        return old_id

    new_row = {k: v for k, v in row.items() if k != ref_col}
    if not new_row:
        return old_id

    cols = list(new_row.keys())
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO `{ref_table}` ({', '.join(f'`{c}`' for c in cols)}) "
            f"VALUES ({', '.join(['%s'] * len(cols))})",
            [new_row[c] for c in cols],
        )
        new_id = cur.lastrowid

    if new_id == 0:
        log(f"  [{ref_table}] No AUTO_INCREMENT PK — reusing original {ref_col}={old_id}.", "warning")
        return old_id

    fk_map[cache_key] = new_id
    log(f"  Cloned {ref_table} row {old_id} -> {new_id}")
    return new_id


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def clone_client(
    src_client_id:          int,
    new_client_overrides:   dict,
    enabled_catalog_tables: set[str],   # table names from catalog that are checked ON
    extra_table_infos:      list,       # TableInfo objects for discovered non-catalog tables
    enabled_extra_tables:   set[str],   # which extra tables are checked ON
    conn,
    batch_size:             int = 500,
    progress_callback:      LogCallback | None = None,
) -> CloneResult:
    """
    Catalog-driven client clone.

    Steps:
      1. Clone the Client row and its FK-referenced shared records (e.g. Address).
      2. Process each enabled catalog entry in declaration order — parents always
         precede children, so cascade id_map remapping works correctly.
      3. Process any enabled extra (dynamically-discovered) tables that have a
         direct ClientId column.
    """
    result = CloneResult(src_client_id=src_client_id, new_client_id=None)
    # id_map[table][str(old_pk)] = new_pk — cascade FK remapping across catalog entries
    id_map: dict[str, dict[str, object]] = {}

    def log(msg: str, level: str = "info"):
        if progress_callback:
            progress_callback(msg, level)

    try:
        with conn.cursor() as cur:
            cur.execute("SET FOREIGN_KEY_CHECKS = 0")

        # ── 1. Read source Client row ────────────────────────────────────────
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT * FROM `{CLIENT_TABLE}` WHERE `{CLIENT_ID_COLUMN}` = %s",
                (src_client_id,),
            )
            src_row = cur.fetchone()

        if not src_row:
            raise ValueError(f"Source client {src_client_id} not found in {CLIENT_TABLE}.")

        # ── 2. Clone FK-referenced shared records on the Client row ──────────
        client_fks  = get_outgoing_fks(CLIENT_TABLE, conn)
        new_client_row = {k: v for k, v in src_row.items() if k != CLIENT_ID_COLUMN}
        shared_cache: dict[tuple, object] = {}

        for fk in client_fks:
            col, ref_table, ref_col = fk["col"], fk["ref_table"], fk["ref_col"]
            if col not in new_client_row or new_client_row[col] is None:
                continue
            new_id = _clone_shared_record(ref_table, ref_col, new_client_row[col], conn, shared_cache, log)
            new_client_row[col] = new_id
            result.fk_cloned[ref_table] = result.fk_cloned.get(ref_table, 0) + 1

        # ── 3. Create the new Client row ─────────────────────────────────────
        new_client_row.update(new_client_overrides)
        cols_c = list(new_client_row.keys())
        with conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO `{CLIENT_TABLE}` "
                f"({', '.join(f'`{c}`' for c in cols_c)}) "
                f"VALUES ({', '.join(['%s'] * len(cols_c))})",
                [new_client_row[c] for c in cols_c],
            )
            new_client_id = cur.lastrowid

        if not new_client_id:
            raise RuntimeError("Failed to create new client — no LAST_INSERT_ID returned.")

        result.new_client_id = new_client_id
        id_map[CLIENT_TABLE] = {str(src_client_id): new_client_id}
        log(f"Created new client: {CLIENT_ID_COLUMN} = {new_client_id}")

        # ── 4. Process catalog entries ───────────────────────────────────────
        for entry in CLONE_CATALOG:
            if entry.is_root:
                continue
            if entry.table not in enabled_catalog_tables:
                log(f"[{entry.table}] Skipped (disabled)")
                result.excluded_tables.append(entry.table)
                continue
            _process_catalog_entry(
                entry, src_client_id, new_client_id, id_map, conn, batch_size, result, log
            )

        # ── 5. Process extra discovered tables ───────────────────────────────
        for info in extra_table_infos:
            tbl = info.name
            if tbl not in enabled_extra_tables:
                result.excluded_tables.append(tbl)
                continue

            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT * FROM `{tbl}` WHERE `{info.client_id_column}` = %s",
                    (src_client_id,),
                )
                rows = cur.fetchall()

            if not rows:
                log(f"[{tbl}] No rows — skipped.")
                continue

            new_rows = [{**dict(r), info.client_id_column: new_client_id} for r in rows]
            n = batch_insert(tbl, new_rows, conn, batch_size)
            result.rows_copied[tbl] = n
            result.tables_copied.append(tbl)
            log(f"[{tbl}] Copied {n} row(s).")

        conn.commit()
        result.success = True
        log(f"Clone complete. New client ID: {new_client_id}")

    except Exception as e:
        conn.rollback()
        result.success = False
        result.error = str(e)
        log(f"Clone failed — rolled back. Error: {e}", "error")

    finally:
        try:
            with conn.cursor() as cur:
                cur.execute("SET FOREIGN_KEY_CHECKS = 1")
            conn.commit()
        except Exception:
            pass

    return result


# ---------------------------------------------------------------------------
# Catalog entry processor
# ---------------------------------------------------------------------------

def _process_catalog_entry(
    entry:          CatalogEntry,
    src_client_id:  int,
    new_client_id:  int,
    id_map:         dict[str, dict[str, object]],
    conn,
    batch_size:     int,
    result:         CloneResult,
    log:            LogCallback,
) -> None:
    tbl = entry.table

    # ── Fetch source rows ──────────────────────────────────────────────────
    if entry.client_id_col:
        # Direct table: filter by ClientId
        sql = f"SELECT * FROM `{tbl}` WHERE `{entry.client_id_col}` = %s"
        if entry.filter_extra:
            sql += f" AND {entry.filter_extra}"
        with conn.cursor() as cur:
            cur.execute(sql, (src_client_id,))
            rows = cur.fetchall()
    else:
        # Indirect table: filter by old parent PKs from id_map
        old_parent_ids = list(id_map.get(entry.parent_table, {}).keys())
        if not old_parent_ids:
            log(f"[{tbl}] No parent IDs mapped for {entry.parent_table} — skipped.", "warning")
            return
        placeholders = ", ".join(["%s"] * len(old_parent_ids))
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT * FROM `{tbl}` WHERE `{entry.parent_join_col}` IN ({placeholders})",
                old_parent_ids,
            )
            rows = cur.fetchall()

    if not rows:
        log(f"[{tbl}] No rows for client {src_client_id} — skipped.")
        return

    # ── Build new rows: remap all FK columns ──────────────────────────────
    old_auto_pks: list[object] = []
    new_rows: list[dict] = []

    for row in rows:
        new_row = dict(row)

        # Remap direct ClientId column
        if entry.client_id_col and entry.client_id_col in new_row:
            new_row[entry.client_id_col] = new_client_id

        # Remap parent join column for indirect tables
        if entry.parent_join_col and entry.parent_join_col in new_row:
            old_val = new_row[entry.parent_join_col]
            remapped = id_map.get(entry.parent_table, {}).get(str(old_val))
            if remapped is not None:
                new_row[entry.parent_join_col] = remapped

        # Remap additional FK columns (e.g. ClientCompanyId -> new ClientCompany.Id)
        if entry.fk_remaps:
            for col, ref_tbl in entry.fk_remaps.items():
                if col in new_row and new_row[col] is not None:
                    remapped = id_map.get(ref_tbl, {}).get(str(new_row[col]))
                    if remapped is not None:
                        new_row[col] = remapped

        # Strip auto-inc PK (record old value for cascade children)
        if entry.auto_pk and entry.auto_pk in new_row:
            old_auto_pks.append(new_row.pop(entry.auto_pk))

        new_rows.append(new_row)

    # ── Insert ────────────────────────────────────────────────────────────
    if entry.auto_pk:
        # Insert one row at a time to capture each new auto-inc PK
        new_pk_list: list[object] = []
        for new_row in new_rows:
            cols = list(new_row.keys())
            with conn.cursor() as cur:
                cur.execute(
                    f"INSERT INTO `{tbl}` ({', '.join(f'`{c}`' for c in cols)}) "
                    f"VALUES ({', '.join(['%s'] * len(cols))})",
                    [new_row[c] for c in cols],
                )
                new_pk_list.append(cur.lastrowid)
        id_map[tbl] = {str(old): new for old, new in zip(old_auto_pks, new_pk_list)}
        n = len(new_rows)
    else:
        n = batch_insert(tbl, new_rows, conn, batch_size)

    result.rows_copied[tbl] = n
    result.tables_copied.append(tbl)
    log(f"[{tbl}] Copied {n} row(s).")


# ---------------------------------------------------------------------------
# Delete a cloned client (undo)
# ---------------------------------------------------------------------------

def delete_cloned_client(
    client_id:        int,
    extra_table_infos: list,      # TableInfo objects from discover_related_tables
    conn,
    log_callback:     LogCallback | None = None,
) -> DeleteResult:
    """
    Permanently delete all data for a client from the Dev database.

    Phase 1: indirect catalog tables (deepest children first, via subqueries)
    Phase 2: direct catalog tables + extra discovered tables
    Phase 3: Client root row + shared FK records cloned for this client (e.g. Address)

    Wrapped in a single transaction — rolled back entirely on any failure.
    """
    result = DeleteResult(client_id=client_id)

    def log(msg: str, level: str = "info"):
        if log_callback:
            log_callback(msg, level)

    def _exec_delete(sql: str, params: tuple) -> int:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.rowcount

    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT * FROM `{CLIENT_TABLE}` WHERE `{CLIENT_ID_COLUMN}` = %s",
                (client_id,),
            )
            client_row = cur.fetchone()

        if not client_row:
            raise ValueError(f"Client {client_id} not found in {CLIENT_TABLE}.")

        # Capture FK-referenced shared record IDs before any rows are deleted
        client_fks = get_outgoing_fks(CLIENT_TABLE, conn)
        fk_refs: list[tuple[str, str, object]] = []
        for fk in client_fks:
            val = client_row.get(fk["col"])
            if val is not None:
                fk_refs.append((fk["ref_table"], fk["ref_col"], val))

        with conn.cursor() as cur:
            cur.execute("SET FOREIGN_KEY_CHECKS = 0")

        # Phase 1: indirect catalog tables — children first via subqueries
        indirect_entries = [
            e for e in reversed(CLONE_CATALOG)
            if not e.is_root and e.client_id_col is None
        ]
        for entry in indirect_entries:
            sql, params = _build_indirect_delete_sql(entry, client_id)
            n = _exec_delete(sql, params)
            if n:
                result.rows_deleted[entry.table] = n
                result.tables_deleted.append(entry.table)
                log(f"[{entry.table}] Deleted {n} row(s).")

        # Phase 2: direct catalog tables (reverse declaration order)
        direct_entries = [
            e for e in reversed(CLONE_CATALOG)
            if not e.is_root and e.client_id_col is not None
        ]
        for entry in direct_entries:
            n = _exec_delete(
                f"DELETE FROM `{entry.table}` WHERE `{entry.client_id_col}` = %s",
                (client_id,),
            )
            if n:
                result.rows_deleted[entry.table] = n
                result.tables_deleted.append(entry.table)
                log(f"[{entry.table}] Deleted {n} row(s).")

        # Phase 2b: extra discovered tables
        for info in extra_table_infos:
            n = _exec_delete(
                f"DELETE FROM `{info.name}` WHERE `{info.client_id_column}` = %s",
                (client_id,),
            )
            if n:
                result.rows_deleted[info.name] = n
                result.tables_deleted.append(info.name)
                log(f"[{info.name}] Deleted {n} row(s) (extra table).")

        # Phase 3: Client root row
        _exec_delete(
            f"DELETE FROM `{CLIENT_TABLE}` WHERE `{CLIENT_ID_COLUMN}` = %s",
            (client_id,),
        )
        log(f"Deleted Client row ({CLIENT_ID_COLUMN} = {client_id}).")

        # Phase 3b: shared FK records cloned exclusively for this client
        for ref_table, ref_col, val in fk_refs:
            try:
                n = _exec_delete(
                    f"DELETE FROM `{ref_table}` WHERE `{ref_col}` = %s", (val,)
                )
                if n:
                    result.fk_records_deleted[ref_table] = n
                    log(f"[{ref_table}] Removed {n} cloned shared record(s).")
            except Exception as e:
                log(f"[{ref_table}] Could not remove shared record: {e}", "warning")

        with conn.cursor() as cur:
            cur.execute("SET FOREIGN_KEY_CHECKS = 1")
        conn.commit()

        result.success = True
        log(f"Client {client_id} deleted successfully.")

    except Exception as e:
        conn.rollback()
        result.success = False
        result.error = str(e)
        log(f"Delete failed — rolled back. Error: {e}", "error")
        try:
            with conn.cursor() as cur:
                cur.execute("SET FOREIGN_KEY_CHECKS = 1")
            conn.commit()
        except Exception:
            pass

    return result


def fetch_cloned_table_rows(entry: CatalogEntry, new_client_id: int, conn) -> list[dict]:
    """Fetch all rows that belong to new_client_id in a catalog table."""
    if entry.client_id_col:
        sql = f"SELECT * FROM `{entry.table}` WHERE `{entry.client_id_col}` = %s"
        if entry.filter_extra:
            sql += f" AND {entry.filter_extra}"
        with conn.cursor() as cur:
            cur.execute(sql, (new_client_id,))
            return cur.fetchall()
    # Indirect table — chase parent chain via subqueries
    sql, params = _build_indirect_select_sql(entry, new_client_id)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def _build_indirect_select_sql(entry: CatalogEntry, client_id: int) -> tuple[str, tuple]:
    """Build parameterized SELECT * for an indirect catalog table via subquery chain."""
    chain: list[CatalogEntry] = [entry]
    while chain[-1].client_id_col is None:
        chain.append(CATALOG_BY_TABLE[chain[-1].parent_table])
    direct = chain[-1]
    inner_sql = (
        f"SELECT `{direct.auto_pk}` FROM `{direct.table}` "
        f"WHERE `{direct.client_id_col}` = %s"
    )
    for level in range(len(chain) - 2, 0, -1):
        mid = chain[level]
        inner_sql = (
            f"SELECT `{mid.auto_pk}` FROM `{mid.table}` "
            f"WHERE `{mid.parent_join_col}` IN ({inner_sql})"
        )
    sql = (
        f"SELECT * FROM `{entry.table}` "
        f"WHERE `{entry.parent_join_col}` IN ({inner_sql})"
    )
    return sql, (client_id,)


def _build_indirect_delete_sql(entry: CatalogEntry, client_id: int) -> tuple[str, tuple]:
    """
    Build a parameterized DELETE for an indirect catalog table (no direct ClientId column).
    Walks up the parent chain until reaching a table with a direct client_id_col,
    then generates nested subqueries to identify the rows to remove.
    """
    chain: list[CatalogEntry] = [entry]
    while chain[-1].client_id_col is None:
        chain.append(CATALOG_BY_TABLE[chain[-1].parent_table])
    # chain[-1] is the first ancestor with a direct ClientId column

    direct = chain[-1]
    inner_sql = (
        f"SELECT `{direct.auto_pk}` FROM `{direct.table}` "
        f"WHERE `{direct.client_id_col}` = %s"
    )

    # Wrap each intermediate level (if any) from inside out
    for level in range(len(chain) - 2, 0, -1):
        mid = chain[level]
        inner_sql = (
            f"SELECT `{mid.auto_pk}` FROM `{mid.table}` "
            f"WHERE `{mid.parent_join_col}` IN ({inner_sql})"
        )

    sql = (
        f"DELETE FROM `{entry.table}` "
        f"WHERE `{entry.parent_join_col}` IN ({inner_sql})"
    )
    return sql, (client_id,)
