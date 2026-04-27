from __future__ import annotations
from decimal import Decimal, InvalidOperation


def _get_user_id(cur, identifier: str, client_id: int) -> str | None:
    cur.execute(
        "SELECT UserId FROM User WHERE (UserName=%s OR Email=%s) AND ClientId=%s AND IsActive=1 LIMIT 1",
        (identifier, identifier, client_id),
    )
    row = cur.fetchone()
    return row["UserId"] if row else None


def _record_exists(cur, user_id: str, client_company_id: int) -> bool:
    cur.execute(
        "SELECT 1 FROM ApproverByAmount WHERE UserId=%s AND ClientCompanyId=%s LIMIT 1",
        (user_id, client_company_id),
    )
    return cur.fetchone() is not None


def import_approver_amounts(
    conn,
    rows: list[dict],
    client_id: int,
    client_company_id: int,
    dry_run: bool = False,
) -> tuple[dict, list[dict]]:
    """
    Creates amount-based approval limits.
    Expected row fields: first_approver_email*, second_approver_email*, max_amount*
    """
    summary = {"inserted": 0, "skipped": 0, "errors": []}
    log: list[dict] = []

    try:
        with conn.cursor() as cur:
            for row in rows:
                first_email = str(row.get("first_approver_email") or "").strip()
                second_email = str(row.get("second_approver_email") or "").strip()
                raw_amount = row.get("max_amount")
                row_num = row.get("_row", "?")

                if not first_email:
                    continue

                try:
                    try:
                        max_amount = Decimal(str(raw_amount)) if raw_amount not in (None, "") else None
                    except InvalidOperation:
                        max_amount = None

                    user_id = _get_user_id(cur, first_email, client_id)
                    if user_id is None:
                        summary["errors"].append(f"Row {row_num}: user '{first_email}' not found")
                        log.append({"row": row_num, "action": "error", "email": first_email, "detail": "user not found"})
                        continue

                    second_user_id = _get_user_id(cur, second_email, client_id) if second_email else None

                    if _record_exists(cur, user_id, client_company_id):
                        summary["skipped"] += 1
                        log.append({"row": row_num, "action": "skipped", "email": first_email, "detail": "record already exists"})
                        continue

                    if not dry_run:
                        cur.execute(
                            "INSERT INTO ApproverByAmount (UserId, SecondApproverId, MaximumAllowedAmount, ClientCompanyId) "
                            "VALUES (%s, %s, %s, %s)",
                            (user_id, second_user_id, max_amount, client_company_id),
                        )
                    summary["inserted"] += 1
                    log.append({"row": row_num, "action": "inserted" if not dry_run else "would insert", "email": first_email, "detail": f"max={max_amount}"})

                except Exception as exc:
                    summary["errors"].append(f"Row {row_num}: {exc}")
                    log.append({"row": row_num, "action": "error", "email": first_email, "detail": str(exc)})

        if not dry_run:
            conn.commit()
    except Exception as exc:
        conn.rollback()
        summary["errors"].append(f"Transaction rolled back: {exc}")

    return summary, log
