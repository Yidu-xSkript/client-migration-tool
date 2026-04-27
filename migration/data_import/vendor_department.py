from __future__ import annotations
import pymysql


def _get_vendor_id(cur, vendor_no: str, vendor_name: str, client_id: int) -> int | None:
    cur.execute(
        "SELECT VendorId FROM Vendor WHERE VendorNo=%s AND VendorName=%s AND ClientId=%s LIMIT 1",
        (vendor_no, vendor_name, client_id),
    )
    row = cur.fetchone()
    return row["VendorId"] if row else None


def _get_dept_id(cur, dept_name: str, client_id: int) -> int | None:
    cur.execute(
        "SELECT Id FROM Department WHERE DepartmentName=%s AND ClientId=%s LIMIT 1",
        (dept_name, client_id),
    )
    row = cur.fetchone()
    return row["Id"] if row else None


def import_vendor_departments(
    conn,
    rows: list[dict],
    client_id: int,
    dry_run: bool = False,
) -> tuple[dict, list[dict]]:
    """
    Links vendors to departments.
    Expected row fields: vendor_no*, vendor_name*, dept_name*
    """
    summary = {"inserted": 0, "skipped": 0, "errors": []}
    log: list[dict] = []

    try:
        with conn.cursor() as cur:
            for row in rows:
                vendor_no = str(row.get("vendor_no") or "").strip()
                vendor_name = str(row.get("vendor_name") or "").strip()
                dept_name = str(row.get("dept_name") or "").strip()
                row_num = row.get("_row", "?")

                if not vendor_no or not dept_name:
                    continue

                try:
                    vendor_id = _get_vendor_id(cur, vendor_no, vendor_name, client_id)
                    if vendor_id is None:
                        summary["errors"].append(f"Row {row_num}: vendor '{vendor_no}' not found")
                        log.append({"row": row_num, "action": "error", "vendor_no": vendor_no, "dept_name": dept_name, "detail": "vendor not found"})
                        continue

                    dept_id = _get_dept_id(cur, dept_name, client_id)
                    if dept_id is None:
                        summary["errors"].append(f"Row {row_num}: department '{dept_name}' not found")
                        log.append({"row": row_num, "action": "error", "vendor_no": vendor_no, "dept_name": dept_name, "detail": "department not found"})
                        continue

                    if not dry_run:
                        cur.execute(
                            "INSERT IGNORE INTO VendorDepartment (VendorId, DepartmentId) VALUES (%s, %s)",
                            (vendor_id, dept_id),
                        )
                        affected = cur.rowcount
                    else:
                        affected = 1

                    if affected:
                        summary["inserted"] += 1
                        log.append({"row": row_num, "action": "inserted" if not dry_run else "would insert", "vendor_no": vendor_no, "dept_name": dept_name, "detail": ""})
                    else:
                        summary["skipped"] += 1
                        log.append({"row": row_num, "action": "skipped", "vendor_no": vendor_no, "dept_name": dept_name, "detail": "already linked"})

                except pymysql.err.IntegrityError as exc:
                    if exc.args[0] == 1062:
                        summary["skipped"] += 1
                        log.append({"row": row_num, "action": "skipped", "vendor_no": vendor_no, "dept_name": dept_name, "detail": "duplicate"})
                    else:
                        summary["errors"].append(f"Row {row_num}: {exc}")
                        log.append({"row": row_num, "action": "error", "vendor_no": vendor_no, "dept_name": dept_name, "detail": str(exc)})
                except Exception as exc:
                    summary["errors"].append(f"Row {row_num}: {exc}")
                    log.append({"row": row_num, "action": "error", "vendor_no": vendor_no, "dept_name": dept_name, "detail": str(exc)})

        if not dry_run:
            conn.commit()
    except Exception as exc:
        conn.rollback()
        summary["errors"].append(f"Transaction rolled back: {exc}")

    return summary, log
