from __future__ import annotations


def _get_gl_code_id(cur, client_id: int, gl_code_name: str, client_company_id: int) -> int | None:
    cur.execute(
        "SELECT GLCodeId FROM GLCode WHERE ClientId=%s AND GLCodeName=%s AND ClientCompanyId=%s LIMIT 1",
        (client_id, gl_code_name, client_company_id),
    )
    row = cur.fetchone()
    return row["GLCodeId"] if row else None


def _insert_gl_code(cur, client_id: int, name: str, desc: str, is_active: int, client_company_id: int,
                    source_gl_code_id: int | None = None, percentage: float | None = None) -> int:
    cur.execute(
        "INSERT INTO GLCode (ClientId, GLCodeName, Desccription, IsActive, ClientCompanyId, "
        "SourceGLCodeId, Percentage) VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (client_id, name[:250], (desc or "")[:500], is_active, client_company_id,
         source_gl_code_id, percentage),
    )
    return cur.lastrowid


def _update_gl_code(cur, gl_code_id: int, client_id: int, name: str, desc: str, is_active: int,
                    client_company_id: int, source_gl_code_id: int | None = None,
                    percentage: float | None = None) -> None:
    cur.execute(
        "UPDATE GLCode SET GLCodeName=%s, Desccription=%s, IsActive=%s, ClientCompanyId=%s, "
        "SourceGLCodeId=%s, Percentage=%s WHERE GLCodeId=%s AND ClientId=%s",
        (name[:250], (desc or "")[:500], is_active, client_company_id,
         source_gl_code_id, percentage, gl_code_id, client_id),
    )


def import_gl_codes(
    conn,
    rows: list[dict],
    client_id: int,
    client_company_id: int,
    mode: str = "flat",
    dry_run: bool = False,
) -> tuple[dict, list[dict]]:
    """
    Loads GL codes from Excel rows.
    mode="flat": fields gl_code_name, description
    mode="split": fields parent_gl_name, description, child_gl_name, percentage
      Parent rows have parent_gl_name set + child_gl_name empty.
      Child rows have child_gl_name set + parent_gl_name empty.

    Note: GLCode table uses 'Desccription' (two c's).
    """
    summary = {"inserted": 0, "updated": 0, "skipped": 0, "errors": []}
    log: list[dict] = []

    try:
        with conn.cursor() as cur:
            if mode == "flat":
                for row in rows:
                    name = str(row.get("gl_code_name") or "").strip()
                    desc = str(row.get("description") or "").strip()
                    row_num = row.get("_row", "?")
                    if not name:
                        continue
                    try:
                        existing_id = _get_gl_code_id(cur, client_id, name, client_company_id)
                        if existing_id:
                            if not dry_run:
                                _update_gl_code(cur, existing_id, client_id, name, desc, 1, client_company_id)
                            summary["updated"] += 1
                            log.append({"row": row_num, "action": "updated" if not dry_run else "would update", "name": name, "detail": f"GLCodeId={existing_id}"})
                        else:
                            if not dry_run:
                                new_id = _insert_gl_code(cur, client_id, name, desc, 1, client_company_id)
                            summary["inserted"] += 1
                            log.append({"row": row_num, "action": "inserted" if not dry_run else "would insert", "name": name, "detail": ""})
                    except Exception as exc:
                        summary["errors"].append(f"Row {row_num} ({name}): {exc}")
                        log.append({"row": row_num, "action": "error", "name": name, "detail": str(exc)})

            else:  # split mode
                current_parent_id: int | None = None
                current_parent_name: str = ""

                for row in rows:
                    parent_name = str(row.get("parent_gl_name") or "").strip()
                    child_name = str(row.get("child_gl_name") or "").strip()
                    desc = str(row.get("description") or "").strip()
                    row_num = row.get("_row", "?")

                    if parent_name:
                        # Parent row
                        try:
                            existing_id = _get_gl_code_id(cur, client_id, parent_name, client_company_id)
                            if existing_id:
                                if not dry_run:
                                    _update_gl_code(cur, existing_id, client_id, parent_name, desc, 1, client_company_id)
                                current_parent_id = existing_id
                                summary["updated"] += 1
                                log.append({"row": row_num, "action": "updated" if not dry_run else "would update", "name": parent_name, "detail": "parent"})
                            else:
                                if not dry_run:
                                    current_parent_id = _insert_gl_code(cur, client_id, parent_name, desc, 1, client_company_id)
                                else:
                                    current_parent_id = -1  # placeholder for dry run
                                current_parent_name = parent_name
                                summary["inserted"] += 1
                                log.append({"row": row_num, "action": "inserted" if not dry_run else "would insert", "name": parent_name, "detail": "parent"})
                        except Exception as exc:
                            summary["errors"].append(f"Row {row_num} parent ({parent_name}): {exc}")
                            log.append({"row": row_num, "action": "error", "name": parent_name, "detail": str(exc)})

                    elif child_name:
                        # Child row
                        if current_parent_id is None:
                            summary["errors"].append(f"Row {row_num}: child '{child_name}' has no preceding parent row")
                            log.append({"row": row_num, "action": "error", "name": child_name, "detail": "no parent"})
                            continue
                        raw_pct = row.get("percentage")
                        try:
                            percentage = float(raw_pct) / 100.0 if raw_pct not in (None, "") else None
                        except (ValueError, TypeError):
                            percentage = None
                        try:
                            # Check for existing child with same parent link
                            cur.execute(
                                "SELECT GLCodeId FROM GLCode WHERE ClientId=%s AND GLCodeName=%s "
                                "AND ClientCompanyId=%s AND SourceGLCodeId=%s LIMIT 1",
                                (client_id, child_name, client_company_id, current_parent_id),
                            )
                            existing = cur.fetchone()
                            if existing:
                                if not dry_run:
                                    _update_gl_code(cur, existing["GLCodeId"], client_id, child_name, desc, 1,
                                                    client_company_id, current_parent_id, percentage)
                                summary["updated"] += 1
                                log.append({"row": row_num, "action": "updated" if not dry_run else "would update", "name": child_name, "detail": f"child of {current_parent_name}"})
                            else:
                                if not dry_run:
                                    _insert_gl_code(cur, client_id, child_name, desc, 1, client_company_id,
                                                    current_parent_id, percentage)
                                summary["inserted"] += 1
                                log.append({"row": row_num, "action": "inserted" if not dry_run else "would insert", "name": child_name, "detail": f"child of {current_parent_name}"})
                        except Exception as exc:
                            summary["errors"].append(f"Row {row_num} child ({child_name}): {exc}")
                            log.append({"row": row_num, "action": "error", "name": child_name, "detail": str(exc)})

        if not dry_run:
            conn.commit()
    except Exception as exc:
        conn.rollback()
        summary["errors"].append(f"Transaction rolled back: {exc}")

    return summary, log
