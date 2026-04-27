from __future__ import annotations
import uuid


def _email_exists(cur, email: str, client_id: int) -> bool:
    cur.execute(
        "SELECT 1 FROM User WHERE Email=%s AND ClientId=%s LIMIT 1",
        (email, client_id),
    )
    return cur.fetchone() is not None


def import_users(
    conn,
    rows: list[dict],
    client_id: int,
    default_address_id: int | None = None,
    dry_run: bool = False,
) -> tuple[dict, list[dict]]:
    """
    Creates user accounts from Excel rows.
    Expected row fields: first_name*, last_name*, email*
    Password is left blank — user must set it after creation.
    """
    summary = {"inserted": 0, "skipped": 0, "errors": []}
    log: list[dict] = []

    try:
        with conn.cursor() as cur:
            for row in rows:
                first_name = str(row.get("first_name") or "").strip()
                last_name = str(row.get("last_name") or "").strip()
                email = str(row.get("email") or "").strip()
                row_num = row.get("_row", "?")

                if not email:
                    continue

                try:
                    if _email_exists(cur, email, client_id):
                        summary["skipped"] += 1
                        log.append({"row": row_num, "action": "skipped", "email": email, "detail": "email already registered"})
                        continue

                    if not dry_run:
                        user_id = str(uuid.uuid4())
                        cur.execute(
                            """INSERT INTO User (
                                UserId, ClientId, FirstName, LastName, Email, UserName,
                                Password, RoleId, IsActive, AddressId,
                                EnableEscalation, CanViewConfidentialInvoice,
                                IsTelephoneConfirmed, TelephoneNotification,
                                NotifyOnUrgentPOApproval, IsDelegateOnlyUser,
                                DisableCPayNotification, IsCloudxUser, ViewCapEx
                            ) VALUES (
                                %s, %s, %s, %s, %s, %s,
                                '', 1, 1, %s,
                                0, 0, 0, 0, 0, 0, 0, 0, 0
                            )""",
                            (
                                user_id, client_id, first_name[:50], last_name[:50],
                                email[:100], email[:50],
                                default_address_id,
                            ),
                        )
                        cur.execute(
                            "INSERT IGNORE INTO UserRoles (UserId, RoleId) VALUES (%s, 1), (%s, 5)",
                            (user_id, user_id),
                        )

                    summary["inserted"] += 1
                    log.append({"row": row_num, "action": "inserted" if not dry_run else "would insert", "email": email, "detail": f"{first_name} {last_name}"})

                except Exception as exc:
                    summary["errors"].append(f"Row {row_num} ({email}): {exc}")
                    log.append({"row": row_num, "action": "error", "email": email, "detail": str(exc)})

        if not dry_run:
            conn.commit()
    except Exception as exc:
        conn.rollback()
        summary["errors"].append(f"Transaction rolled back: {exc}")

    return summary, log
