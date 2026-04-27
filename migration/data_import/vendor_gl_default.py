from __future__ import annotations


def _get_gl_code_id(cur, gl_code_name: str, client_id: int) -> int | None:
    cur.execute(
        "SELECT GLCodeId FROM GLCode WHERE GLCodeName=%s AND ClientId=%s LIMIT 1",
        (gl_code_name, client_id),
    )
    row = cur.fetchone()
    return row["GLCodeId"] if row else None


def _get_vendor_id(cur, vendor_no: str, vendor_name: str, client_id: int) -> int | None:
    cur.execute(
        "SELECT VendorId FROM Vendor WHERE VendorNo=%s AND VendorName=%s AND ClientId=%s LIMIT 1",
        (vendor_no, vendor_name, client_id),
    )
    row = cur.fetchone()
    return row["VendorId"] if row else None


def _default_exists(cur, client_id: int, vendor_id: int, gl_code_id: int) -> bool:
    cur.execute(
        "SELECT 1 FROM VendorGlDefault WHERE ClientId=%s AND VendorId=%s AND GlCodeId=%s LIMIT 1",
        (client_id, vendor_id, gl_code_id),
    )
    return cur.fetchone() is not None


def import_vendor_gl_defaults(
    conn,
    rows: list[dict],
    client_id: int,
    dry_run: bool = False,
) -> tuple[dict, list[dict]]:
    """
    Sets the default GL code for each vendor.
    Expected row fields: vendor_no*, vendor_name*, gl_code_name*
    """
    summary = {"inserted": 0, "updated": 0, "skipped": 0, "errors": []}
    log: list[dict] = []

    try:
        with conn.cursor() as cur:
            for row in rows:
                vendor_no = str(row.get("vendor_no") or "").strip()
                vendor_name = str(row.get("vendor_name") or "").strip()
                gl_name = str(row.get("gl_code_name") or "").strip()
                row_num = row.get("_row", "?")

                if not vendor_no or not gl_name:
                    continue

                try:
                    gl_code_id = _get_gl_code_id(cur, gl_name, client_id)
                    if gl_code_id is None:
                        summary["errors"].append(f"Row {row_num}: GL code '{gl_name}' not found")
                        log.append({"row": row_num, "action": "error", "vendor_no": vendor_no, "gl_code": gl_name, "detail": "GL code not found"})
                        continue

                    vendor_id = _get_vendor_id(cur, vendor_no, vendor_name, client_id)
                    if vendor_id is None:
                        summary["errors"].append(f"Row {row_num}: vendor '{vendor_no}' not found")
                        log.append({"row": row_num, "action": "error", "vendor_no": vendor_no, "gl_code": gl_name, "detail": "vendor not found"})
                        continue

                    if not dry_run:
                        if not _default_exists(cur, client_id, vendor_id, gl_code_id):
                            cur.execute(
                                "INSERT INTO VendorGlDefault (GlCodeId, ClientId, VendorId) VALUES (%s, %s, %s)",
                                (gl_code_id, client_id, vendor_id),
                            )
                            summary["inserted"] += 1
                            log.append({"row": row_num, "action": "inserted", "vendor_no": vendor_no, "gl_code": gl_name, "detail": ""})
                        else:
                            summary["skipped"] += 1
                            log.append({"row": row_num, "action": "skipped", "vendor_no": vendor_no, "gl_code": gl_name, "detail": "default already set"})

                        cur.execute(
                            "UPDATE Vendor SET DefaultGlCodeId=%s WHERE VendorId=%s AND ClientId=%s",
                            (gl_code_id, vendor_id, client_id),
                        )
                        summary["updated"] += 1
                    else:
                        summary["inserted"] += 1
                        log.append({"row": row_num, "action": "would insert", "vendor_no": vendor_no, "gl_code": gl_name, "detail": ""})

                except Exception as exc:
                    summary["errors"].append(f"Row {row_num}: {exc}")
                    log.append({"row": row_num, "action": "error", "vendor_no": vendor_no, "gl_code": gl_name, "detail": str(exc)})

        if not dry_run:
            conn.commit()
    except Exception as exc:
        conn.rollback()
        summary["errors"].append(f"Transaction rolled back: {exc}")

    return summary, log
