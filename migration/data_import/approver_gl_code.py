from __future__ import annotations

_BATCH = 500


def _load_gl_code_lookup(conn, client_id: int) -> dict[str, int]:
    with conn.cursor() as cur:
        cur.execute("SELECT GLCodeId, GLCodeName FROM GLCode WHERE ClientId=%s", (client_id,))
        return {r["GLCodeName"].lower(): r["GLCodeId"] for r in cur.fetchall() if r["GLCodeName"]}


def _load_user_lookup(conn, client_id: int) -> dict[str, str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT UserId, UserName, Email FROM User WHERE ClientId=%s AND IsActive=1",
            (client_id,),
        )
        lookup: dict[str, str] = {}
        for r in cur.fetchall():
            uid = r["UserId"]
            if r["UserName"]:
                lookup[r["UserName"].lower()] = uid
            if r["Email"]:
                lookup[r["Email"].lower()] = uid
        return lookup


def _load_existing_pairs(conn, client_id: int) -> set[tuple]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT a.GLCodeId, a.UserId FROM ApproverGLCode a "
            "INNER JOIN GLCode g ON g.GLCodeId = a.GLCodeId WHERE g.ClientId=%s",
            (client_id,),
        )
        return {(r["GLCodeId"], r["UserId"]) for r in cur.fetchall()}


def _batch_insert(cur, records: list[tuple]) -> int:
    total = 0
    for i in range(0, len(records), _BATCH):
        chunk = records[i:i + _BATCH]
        placeholders = ",".join(["(%s,%s)"] * len(chunk))
        params = [v for pair in chunk for v in pair]
        cur.execute(
            f"INSERT IGNORE INTO ApproverGLCode (GLCodeId, UserId) VALUES {placeholders}",
            params,
        )
        total += cur.rowcount
    return total


def _batch_delete(cur, records: list[tuple]) -> int:
    total = 0
    for i in range(0, len(records), _BATCH):
        chunk = records[i:i + _BATCH]
        conditions = " OR ".join(["(GLCodeId=%s AND UserId=%s)"] * len(chunk))
        params = [v for pair in chunk for v in pair]
        cur.execute(f"DELETE FROM ApproverGLCode WHERE {conditions}", params)
        total += cur.rowcount
    return total


def import_approver_gl_codes(
    conn,
    rows: list[dict],
    client_id: int,
    mode: str = "process",
    dry_run: bool = False,
) -> tuple[dict, list[dict]]:
    """
    Maps GL codes to approver users.
    Expected row fields: user_email*, gl_code_name*, action (INSERT/DELETE, optional)
    mode: "process" – use action column per row
          "sync"    – desired state from Excel; insert new, delete removed
          "insert"  – insert-only, skip existing
    """
    summary = {"inserted": 0, "deleted": 0, "skipped": 0, "errors": []}
    log: list[dict] = []

    try:
        gl_lookup = _load_gl_code_lookup(conn, client_id)
        user_lookup = _load_user_lookup(conn, client_id)
        existing = _load_existing_pairs(conn, client_id)

        to_insert: list[tuple] = []
        to_delete: list[tuple] = []
        desired: set[tuple] = set()

        for row in rows:
            email = str(row.get("user_email") or "").strip().lower()
            gl_name = str(row.get("gl_code_name") or "").strip().lower()
            action = str(row.get("action") or "").strip().upper() or "INSERT"
            row_num = row.get("_row", "?")

            gl_id = gl_lookup.get(gl_name)
            user_id = user_lookup.get(email)

            if not gl_id:
                summary["errors"].append(f"Row {row_num}: GL code '{gl_name}' not found")
                log.append({"row": row_num, "action": "error", "email": email, "gl_code": gl_name, "detail": "GL code not found"})
                continue
            if not user_id:
                summary["errors"].append(f"Row {row_num}: user '{email}' not found")
                log.append({"row": row_num, "action": "error", "email": email, "gl_code": gl_name, "detail": "user not found"})
                continue

            pair = (gl_id, user_id)

            if mode == "process":
                if action == "DELETE":
                    if pair in existing:
                        to_delete.append(pair)
                    else:
                        summary["skipped"] += 1
                else:
                    if pair not in existing:
                        to_insert.append(pair)
                    else:
                        summary["skipped"] += 1

            elif mode == "sync":
                desired.add(pair)

            elif mode == "insert":
                if pair not in existing:
                    to_insert.append(pair)
                else:
                    summary["skipped"] += 1

        if mode == "sync":
            to_insert = list(desired - existing)
            to_delete = list(existing - desired)

        if not dry_run:
            with conn.cursor() as cur:
                if to_insert:
                    n = _batch_insert(cur, to_insert)
                    summary["inserted"] = n
                if to_delete:
                    n = _batch_delete(cur, to_delete)
                    summary["deleted"] = n
            conn.commit()
        else:
            summary["inserted"] = len(to_insert)
            summary["deleted"] = len(to_delete)

        for pair in to_insert[:200]:
            log.append({"action": "insert", "gl_code_id": pair[0], "user_id": pair[1], "detail": ""})
        for pair in to_delete[:200]:
            log.append({"action": "delete", "gl_code_id": pair[0], "user_id": pair[1], "detail": ""})

    except Exception as exc:
        conn.rollback()
        summary["errors"].append(f"Transaction rolled back: {exc}")

    return summary, log
