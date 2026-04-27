from __future__ import annotations


# ── Shared lookup helpers ────────────────────────────────────────────────────

def _get_user_id(cur, identifier: str, client_id: int) -> str | None:
    cur.execute(
        "SELECT UserId FROM User "
        "WHERE (UserName=%s OR Email=%s) AND ClientId=%s AND IsActive=1 LIMIT 1",
        (identifier, identifier, client_id),
    )
    row = cur.fetchone()
    return row["UserId"] if row else None


def _get_vendor_id(cur, vendor_no: str, client_id: int) -> int | None:
    cur.execute(
        "SELECT VendorId FROM Vendor WHERE VendorNo=%s AND ClientId=%s LIMIT 1",
        (vendor_no, client_id),
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


def _get_approval_substep_id(cur, client_id: int, company_code: str, substep_name: str) -> int | None:
    cur.execute(
        """
        SELECT ass.ApprovalSubStepId
        FROM ApprovalSubStep ass
        INNER JOIN ApprovalStep ap ON ap.ApprovalStepId = ass.ApprovalStepId
        WHERE ap.ClientId = %s AND ass.SubStepName = %s
        LIMIT 1
        """,
        (client_id, substep_name),
    )
    row = cur.fetchone()
    return row["ApprovalSubStepId"] if row else None


# ── Processor 1: ApprovalSubStepUser ────────────────────────────────────────

def import_approval_substep_users(
    conn,
    rows: list[dict],
    client_id: int,
    company_code: str,
    substep_name: str,
    dry_run: bool = False,
) -> tuple[dict, list[dict]]:
    """
    Links users to an approval sub-step.
    Expected row fields: approval_one_email*, approval_two_email, vendor_no
    """
    summary = {"inserted": 0, "skipped": 0, "errors": []}
    log: list[dict] = []

    try:
        with conn.cursor() as cur:
            substep_id = _get_approval_substep_id(cur, client_id, company_code, substep_name)
            if substep_id is None:
                summary["errors"].append(
                    f"ApprovalSubStep '{substep_name}' not found for client {client_id}"
                )
                return summary, log

            for row in rows:
                row_num = row.get("_row", "?")
                emails = [
                    str(row.get("approval_one_email") or "").strip(),
                    str(row.get("approval_two_email") or "").strip(),
                ]
                for email in filter(None, emails):
                    try:
                        user_id = _get_user_id(cur, email, client_id)
                        if user_id is None:
                            summary["errors"].append(f"Row {row_num}: user '{email}' not found")
                            log.append({"row": row_num, "action": "error", "email": email, "detail": "user not found"})
                            continue
                        cur.execute(
                            "SELECT 1 FROM ApprovalSubStepUser WHERE ApprovalSubStepId=%s AND UserId=%s LIMIT 1",
                            (substep_id, user_id),
                        )
                        if cur.fetchone():
                            summary["skipped"] += 1
                            log.append({"row": row_num, "action": "skipped", "email": email, "detail": "already linked"})
                            continue
                        if not dry_run:
                            cur.execute(
                                "INSERT INTO ApprovalSubStepUser (ApprovalSubStepId, UserId) VALUES (%s, %s)",
                                (substep_id, user_id),
                            )
                        summary["inserted"] += 1
                        log.append({"row": row_num, "action": "inserted" if not dry_run else "would insert", "email": email, "detail": f"substep={substep_id}"})
                    except Exception as exc:
                        summary["errors"].append(f"Row {row_num} ({email}): {exc}")
                        log.append({"row": row_num, "action": "error", "email": email, "detail": str(exc)})

        if not dry_run:
            conn.commit()
    except Exception as exc:
        conn.rollback()
        summary["errors"].append(f"Transaction rolled back: {exc}")

    return summary, log


# ── Processor 2: ApprovalSubStepUserVendor ───────────────────────────────────

def import_approval_user_vendors(
    conn,
    rows: list[dict],
    client_id: int,
    approval_substep_id: int,
    dry_run: bool = False,
) -> tuple[dict, list[dict]]:
    """
    Links approval-user pairs to specific vendors.
    Expected row fields: vendor_no*, user_email*
    """
    summary = {"inserted": 0, "skipped": 0, "errors": []}
    log: list[dict] = []

    try:
        with conn.cursor() as cur:
            for row in rows:
                vendor_no = str(row.get("vendor_no") or "").strip()
                email = str(row.get("user_email") or "").strip()
                row_num = row.get("_row", "?")
                if not vendor_no or not email:
                    continue
                try:
                    user_id = _get_user_id(cur, email, client_id)
                    if not user_id:
                        summary["errors"].append(f"Row {row_num}: user '{email}' not found")
                        log.append({"row": row_num, "action": "error", "email": email, "detail": "user not found"})
                        continue
                    vendor_id = _get_vendor_id(cur, vendor_no, client_id)
                    if vendor_id is None:
                        summary["errors"].append(f"Row {row_num}: vendor '{vendor_no}' not found")
                        log.append({"row": row_num, "action": "error", "email": email, "detail": f"vendor {vendor_no} not found"})
                        continue
                    cur.execute(
                        "SELECT 1 FROM ApprovalSubStepUserVendor "
                        "WHERE ApprovalSubStepId=%s AND UserId=%s AND VendorId=%s LIMIT 1",
                        (approval_substep_id, user_id, vendor_id),
                    )
                    if cur.fetchone():
                        summary["skipped"] += 1
                        log.append({"row": row_num, "action": "skipped", "email": email, "detail": "already linked"})
                        continue
                    if not dry_run:
                        cur.execute(
                            "INSERT INTO ApprovalSubStepUserVendor (ApprovalSubStepId, UserId, VendorId) "
                            "VALUES (%s, %s, %s)",
                            (approval_substep_id, user_id, vendor_id),
                        )
                    summary["inserted"] += 1
                    log.append({"row": row_num, "action": "inserted" if not dry_run else "would insert", "email": email, "detail": f"vendor={vendor_no}"})
                except Exception as exc:
                    summary["errors"].append(f"Row {row_num}: {exc}")
                    log.append({"row": row_num, "action": "error", "email": email, "detail": str(exc)})

        if not dry_run:
            conn.commit()
    except Exception as exc:
        conn.rollback()
        summary["errors"].append(f"Transaction rolled back: {exc}")

    return summary, log


# ── Processor 3: ApprovalSubStepUserDepartment ───────────────────────────────

def import_approval_user_departments(
    conn,
    rows: list[dict],
    client_id: int,
    approval_substep_id: int,
    dry_run: bool = False,
) -> tuple[dict, list[dict]]:
    """
    Links approval-user pairs to departments.
    Expected row fields: dept_name*, user_email*
    """
    summary = {"inserted": 0, "skipped": 0, "errors": []}
    log: list[dict] = []

    try:
        with conn.cursor() as cur:
            for row in rows:
                dept_name = str(row.get("dept_name") or "").strip()
                email = str(row.get("user_email") or "").strip()
                row_num = row.get("_row", "?")
                if not dept_name or not email:
                    continue
                try:
                    user_id = _get_user_id(cur, email, client_id)
                    if not user_id:
                        summary["errors"].append(f"Row {row_num}: user '{email}' not found")
                        log.append({"row": row_num, "action": "error", "email": email, "detail": "user not found"})
                        continue
                    dept_id = _get_dept_id(cur, dept_name, client_id)
                    if dept_id is None:
                        summary["errors"].append(f"Row {row_num}: department '{dept_name}' not found")
                        log.append({"row": row_num, "action": "error", "email": email, "detail": f"dept {dept_name} not found"})
                        continue
                    cur.execute(
                        "SELECT 1 FROM ApprovalSubStepUserDepartment "
                        "WHERE ApprovalSubStepId=%s AND UserId=%s AND DepartmentId=%s LIMIT 1",
                        (approval_substep_id, user_id, dept_id),
                    )
                    if cur.fetchone():
                        summary["skipped"] += 1
                        log.append({"row": row_num, "action": "skipped", "email": email, "detail": "already linked"})
                        continue
                    if not dry_run:
                        cur.execute(
                            "INSERT INTO ApprovalSubStepUserDepartment (ApprovalSubStepId, UserId, DepartmentId) "
                            "VALUES (%s, %s, %s)",
                            (approval_substep_id, user_id, dept_id),
                        )
                    summary["inserted"] += 1
                    log.append({"row": row_num, "action": "inserted" if not dry_run else "would insert", "email": email, "detail": f"dept={dept_name}"})
                except Exception as exc:
                    summary["errors"].append(f"Row {row_num}: {exc}")
                    log.append({"row": row_num, "action": "error", "email": email, "detail": str(exc)})

        if not dry_run:
            conn.commit()
    except Exception as exc:
        conn.rollback()
        summary["errors"].append(f"Transaction rolled back: {exc}")

    return summary, log


# ── Processor 4: ApprovalSubStepUserVendorDepartment (4-way) ─────────────────

def import_approval_user_vendor_departments(
    conn,
    rows: list[dict],
    client_id: int,
    dry_run: bool = False,
) -> tuple[dict, list[dict]]:
    """
    Inserts 4-way approval mappings (substep + user + vendor + department).
    Expected row fields: user_id* (UUID string), approval_substep_id*, department_id*, vendor_id*
    """
    summary = {"inserted": 0, "skipped": 0, "errors": []}
    log: list[dict] = []

    try:
        with conn.cursor() as cur:
            for row in rows:
                row_num = row.get("_row", "?")
                try:
                    raw_user_id = str(row.get("user_id") or "").strip()
                    raw_substep = row.get("approval_substep_id")
                    raw_dept = row.get("department_id")
                    raw_vendor = row.get("vendor_id")

                    if not all([raw_user_id, raw_substep, raw_dept, raw_vendor]):
                        summary["skipped"] += 1
                        continue

                    substep_id = int(raw_substep)
                    dept_id = int(raw_dept)
                    vendor_id = int(raw_vendor)

                    cur.execute(
                        "SELECT 1 FROM ApprovalSubStepUserVendorDepartment "
                        "WHERE ApprovalSubStepId=%s AND UserId=%s AND VendorId=%s AND DepartmentId=%s LIMIT 1",
                        (substep_id, raw_user_id, vendor_id, dept_id),
                    )
                    if cur.fetchone():
                        summary["skipped"] += 1
                        log.append({"row": row_num, "action": "skipped", "user_id": raw_user_id, "detail": "already exists"})
                        continue

                    if not dry_run:
                        cur.execute(
                            "INSERT INTO ApprovalSubStepUserVendorDepartment "
                            "(ApprovalSubStepId, UserId, VendorId, DepartmentId) VALUES (%s, %s, %s, %s)",
                            (substep_id, raw_user_id, vendor_id, dept_id),
                        )
                    summary["inserted"] += 1
                    log.append({"row": row_num, "action": "inserted" if not dry_run else "would insert", "user_id": raw_user_id, "detail": f"substep={substep_id} vendor={vendor_id} dept={dept_id}"})

                except (ValueError, TypeError) as exc:
                    summary["errors"].append(f"Row {row_num}: invalid ID value — {exc}")
                    log.append({"row": row_num, "action": "error", "user_id": "", "detail": str(exc)})
                except Exception as exc:
                    summary["errors"].append(f"Row {row_num}: {exc}")
                    log.append({"row": row_num, "action": "error", "user_id": "", "detail": str(exc)})

        if not dry_run:
            conn.commit()
    except Exception as exc:
        conn.rollback()
        summary["errors"].append(f"Transaction rolled back: {exc}")

    return summary, log
