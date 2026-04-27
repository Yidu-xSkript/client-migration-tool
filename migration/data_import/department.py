from __future__ import annotations


def import_departments(
    conn,
    rows: list[dict],
    client_id: int,
    dry_run: bool = False,
) -> tuple[dict, list[dict]]:
    """
    Idempotently loads departments from Excel rows.
    Expected row field: dept_name
    Returns (summary, log).
    """
    summary = {"inserted": 0, "skipped": 0, "errors": []}
    log: list[dict] = []

    def _exists(cur, name: str) -> bool:
        cur.execute(
            "SELECT 1 FROM Department WHERE DepartmentName = %s AND ClientId = %s LIMIT 1",
            (name, client_id),
        )
        return cur.fetchone() is not None

    try:
        with conn.cursor() as cur:
            for row in rows:
                name = str(row.get("dept_name") or "").strip()
                if not name:
                    continue
                row_num = row.get("_row", "?")
                try:
                    if _exists(cur, name):
                        summary["skipped"] += 1
                        log.append({"row": row_num, "action": "skipped", "dept_name": name, "detail": "already exists"})
                        continue
                    if not dry_run:
                        cur.execute(
                            "INSERT INTO Department (DepartmentName, ClientId, IsActive) VALUES (%s, %s, 1)",
                            (name, client_id),
                        )
                    summary["inserted"] += 1
                    log.append({"row": row_num, "action": "inserted" if not dry_run else "would insert", "dept_name": name, "detail": ""})
                except Exception as exc:
                    summary["errors"].append(f"Row {row_num} ({name}): {exc}")
                    log.append({"row": row_num, "action": "error", "dept_name": name, "detail": str(exc)})

        if not dry_run:
            conn.commit()
    except Exception as exc:
        conn.rollback()
        summary["errors"].append(f"Transaction rolled back: {exc}")

    return summary, log
