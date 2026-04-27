# migration/validation.py — Pre- and post-migration validation checks

from __future__ import annotations
from dataclasses import dataclass, field
import pymysql

from config import CLIENT_TABLE, CLIENT_ID_COLUMN, FORBIDDEN_FILTER_KEYWORDS


# ---------------------------------------------------------------------------
# Available check names (used in profiles and UI)
# ---------------------------------------------------------------------------
PRE_CHECKS = {
    "source_has_data":    "Source client exists in the Client table",
    "row_count_positive": "All selected tables have ≥ 1 row in source",
    "no_orphaned_refs":   "Source rows all reference a valid ClientId",
    "destination_clean":  "Destination has no rows for this client (clean target)",
}

POST_CHECKS = {
    "row_counts_match":       "Row counts match between source and destination",
    "referential_integrity":  "No orphaned FK references in destination",
}


@dataclass
class CheckResult:
    name: str
    passed: bool
    message: str


@dataclass
class ValidationResult:
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)

    @property
    def failures(self) -> list[CheckResult]:
        return [c for c in self.checks if not c.passed]

    @property
    def warnings(self) -> list[CheckResult]:
        return []  # Reserved for future soft-failure checks


# ---------------------------------------------------------------------------
# Row filter validation (security gate — no dangerous keywords)
# ---------------------------------------------------------------------------

def validate_row_filter(expr: str) -> tuple[bool, str]:
    """
    Ensure a user-provided SQL WHERE expression doesn't contain forbidden keywords.
    Returns (ok, error_message).
    """
    if not expr or not expr.strip():
        return True, ""
    upper = expr.upper()
    for kw in FORBIDDEN_FILTER_KEYWORDS:
        if kw in upper:
            return False, f"Forbidden keyword in filter expression: '{kw}'"
    if ";" in expr:
        return False, "Semicolons are not allowed in filter expressions."
    return True, ""


# ---------------------------------------------------------------------------
# Pre-migration checks
# ---------------------------------------------------------------------------

def run_pre_checks(
    client_id: int,
    tables: list,        # list[TableInfo]
    src_conn,
    dst_conn,
    check_names: list[str],
    custom_sql: str = "",
) -> ValidationResult:
    result = ValidationResult()

    for name in check_names:
        if name == "source_has_data":
            result.checks.append(_check_source_has_data(client_id, src_conn))

        elif name == "row_count_positive":
            result.checks.append(_check_row_count_positive(client_id, tables, src_conn))

        elif name == "no_orphaned_refs":
            result.checks.append(_check_no_orphaned_refs(client_id, tables, src_conn))

        elif name == "destination_clean":
            result.checks.append(_check_destination_clean(client_id, tables, dst_conn))

    if custom_sql and custom_sql.strip():
        result.checks.append(_check_custom_sql(custom_sql, client_id, src_conn))

    return result


def _check_source_has_data(client_id: int, src_conn) -> CheckResult:
    try:
        sql = f"SELECT COUNT(*) AS cnt FROM `{CLIENT_TABLE}` WHERE `{CLIENT_ID_COLUMN}` = %s"
        with src_conn.cursor() as cur:
            cur.execute(sql, (client_id,))
            row = cur.fetchone()
        cnt = int(row["cnt"]) if row else 0
        if cnt > 0:
            return CheckResult("source_has_data", True, f"Client {client_id} found in source.")
        return CheckResult("source_has_data", False, f"Client {client_id} does not exist in source.")
    except pymysql.Error as e:
        return CheckResult("source_has_data", False, f"Query error: {e}")


def _check_row_count_positive(client_id: int, tables: list, src_conn) -> CheckResult:
    empty = []
    try:
        for info in tables:
            sql = f"SELECT COUNT(*) AS cnt FROM `{info.name}` WHERE `{info.client_id_column}` = %s"
            with src_conn.cursor() as cur:
                cur.execute(sql, (client_id,))
                row = cur.fetchone()
            if not row or int(row["cnt"]) == 0:
                empty.append(info.name)
    except pymysql.Error as e:
        return CheckResult("row_count_positive", False, f"Query error: {e}")

    if not empty:
        return CheckResult("row_count_positive", True, "All tables have data in source.")
    return CheckResult(
        "row_count_positive", False,
        f"These tables are empty in source: {', '.join(empty)}"
    )


def _check_no_orphaned_refs(client_id: int, tables: list, src_conn) -> CheckResult:
    """Check that every child-table ClientId actually exists in the Client table."""
    bad = []
    client_table_names = {t.name for t in tables if t.name == CLIENT_TABLE}
    if not client_table_names:
        return CheckResult("no_orphaned_refs", True, "Client table not in migration set — skipped.")

    try:
        for info in tables:
            if info.name == CLIENT_TABLE:
                continue
            sql = f"""
                SELECT COUNT(*) AS cnt
                FROM `{info.name}` child
                LEFT JOIN `{CLIENT_TABLE}` parent
                    ON child.`{info.client_id_column}` = parent.`{CLIENT_ID_COLUMN}`
                WHERE child.`{info.client_id_column}` = %s
                  AND parent.`{CLIENT_ID_COLUMN}` IS NULL
            """
            with src_conn.cursor() as cur:
                cur.execute(sql, (client_id,))
                row = cur.fetchone()
            if row and int(row["cnt"]) > 0:
                bad.append(info.name)
    except pymysql.Error as e:
        return CheckResult("no_orphaned_refs", False, f"Query error: {e}")

    if not bad:
        return CheckResult("no_orphaned_refs", True, "No orphaned references found in source.")
    return CheckResult(
        "no_orphaned_refs", False,
        f"Orphaned references found in source tables: {', '.join(bad)}"
    )


def _check_destination_clean(client_id: int, tables: list, dst_conn) -> CheckResult:
    """Check that destination has no existing rows for this client."""
    existing = []
    try:
        for info in tables:
            sql = f"SELECT COUNT(*) AS cnt FROM `{info.name}` WHERE `{info.client_id_column}` = %s"
            with dst_conn.cursor() as cur:
                cur.execute(sql, (client_id,))
                row = cur.fetchone()
            if row and int(row["cnt"]) > 0:
                existing.append(f"{info.name} ({row['cnt']} rows)")
    except pymysql.Error as e:
        return CheckResult("destination_clean", False, f"Query error: {e}")

    if not existing:
        return CheckResult("destination_clean", True, "Destination is clean for this client.")
    return CheckResult(
        "destination_clean", False,
        f"Destination already has data: {', '.join(existing)}"
    )


def _check_custom_sql(sql: str, client_id: int, conn) -> CheckResult:
    """
    Run a user-provided SQL query. The query should return COUNT(*) = 0 to pass.
    Use %s as a placeholder for client_id if needed.
    """
    ok, err = validate_row_filter(sql)
    if not ok:
        return CheckResult("custom_sql", False, f"Invalid SQL: {err}")
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (client_id,))
            row = cur.fetchone()
        count = int(list(row.values())[0]) if row else 0
        if count == 0:
            return CheckResult("custom_sql", True, "Custom SQL check passed (returned 0 rows).")
        return CheckResult("custom_sql", False, f"Custom SQL check failed (returned {count}, expected 0).")
    except pymysql.Error as e:
        return CheckResult("custom_sql", False, f"Custom SQL error: {e}")


# ---------------------------------------------------------------------------
# Post-migration checks
# ---------------------------------------------------------------------------

def run_post_checks(
    client_id: int,
    tables: list,   # list[TableInfo]
    src_conn,
    dst_conn,
) -> ValidationResult:
    result = ValidationResult()
    result.checks.append(_post_row_counts_match(client_id, tables, src_conn, dst_conn))
    result.checks.append(_post_referential_integrity(client_id, tables, dst_conn))
    return result


def _post_row_counts_match(client_id: int, tables: list, src_conn, dst_conn) -> CheckResult:
    mismatches = []
    try:
        for info in tables:
            sql = f"SELECT COUNT(*) AS cnt FROM `{info.name}` WHERE `{info.client_id_column}` = %s"
            with src_conn.cursor() as cur:
                cur.execute(sql, (client_id,))
                src_row = cur.fetchone()
            with dst_conn.cursor() as cur:
                cur.execute(sql, (client_id,))
                dst_row = cur.fetchone()
            src_cnt = int(src_row["cnt"]) if src_row else 0
            dst_cnt = int(dst_row["cnt"]) if dst_row else 0
            if src_cnt != dst_cnt:
                mismatches.append(f"{info.name} (src={src_cnt}, dst={dst_cnt})")
    except pymysql.Error as e:
        return CheckResult("row_counts_match", False, f"Query error: {e}")

    if not mismatches:
        return CheckResult("row_counts_match", True, "All row counts match between source and destination.")
    return CheckResult(
        "row_counts_match", False,
        f"Row count mismatches: {', '.join(mismatches)}"
    )


def _post_referential_integrity(client_id: int, tables: list, dst_conn) -> CheckResult:
    """Check for orphaned FK references in the destination after migration."""
    bad = []
    client_in_set = any(t.name == CLIENT_TABLE for t in tables)
    if not client_in_set:
        return CheckResult("referential_integrity", True, "Client table not in set — skipped.")

    try:
        for info in tables:
            if info.name == CLIENT_TABLE:
                continue
            if not info.fk_parents:
                continue
            sql = f"""
                SELECT COUNT(*) AS cnt
                FROM `{info.name}` child
                LEFT JOIN `{CLIENT_TABLE}` parent
                    ON child.`{info.client_id_column}` = parent.`{CLIENT_ID_COLUMN}`
                WHERE child.`{info.client_id_column}` = %s
                  AND parent.`{CLIENT_ID_COLUMN}` IS NULL
            """
            with dst_conn.cursor() as cur:
                cur.execute(sql, (client_id,))
                row = cur.fetchone()
            if row and int(row["cnt"]) > 0:
                bad.append(info.name)
    except pymysql.Error as e:
        return CheckResult("referential_integrity", False, f"Query error: {e}")

    if not bad:
        return CheckResult("referential_integrity", True, "No referential integrity issues in destination.")
    return CheckResult(
        "referential_integrity", False,
        f"Orphaned FK references found in destination: {', '.join(bad)}"
    )
