from __future__ import annotations


def _load_states(conn) -> dict[str, int]:
    with conn.cursor() as cur:
        cur.execute("SELECT StateId, StateNameShort FROM State")
        return {r["StateNameShort"]: r["StateId"] for r in cur.fetchall() if r["StateNameShort"]}


def _get_vendor(conn, vendor_no: str, client_id: int, client_company_id: int) -> tuple[int, int] | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT VendorId, AddressId FROM Vendor "
            "WHERE VendorNo = %s AND ClientId = %s AND ClientCompanyId = %s LIMIT 1",
            (vendor_no, client_id, client_company_id),
        )
        row = cur.fetchone()
    return (row["VendorId"], row["AddressId"]) if row else None


def _insert_address(cur, row: dict, state_id: int | None) -> int:
    zipcode = str(row.get("zipcode") or "").strip()
    if zipcode.upper() == "#VALUE!":
        zipcode = ""
    cur.execute(
        "INSERT INTO Address (StateId, StreetName, Address1, CityName, ZipCode, PhoneNo, Address2) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (
            state_id,
            str(row.get("street_name") or "")[:150],
            str(row.get("street_name") or "")[:250],
            str(row.get("city") or "")[:50],
            zipcode[:20],
            str(row.get("phone") or "")[:50],
            str(row.get("address2") or "")[:250],
        ),
    )
    return cur.lastrowid


def _update_address(cur, address_id: int, row: dict, state_id: int | None) -> None:
    zipcode = str(row.get("zipcode") or "").strip()
    if zipcode.upper() == "#VALUE!":
        zipcode = ""
    cur.execute(
        "UPDATE Address SET StateId=%s, StreetName=%s, Address1=%s, CityName=%s, "
        "ZipCode=%s, PhoneNo=%s, ContactPerson=%s, Address2=%s "
        "WHERE AddressId=%s",
        (
            state_id,
            str(row.get("street_name") or "")[:150],
            str(row.get("street_name") or "")[:250],
            str(row.get("city") or "")[:50],
            zipcode[:20],
            str(row.get("phone") or "")[:50],
            str(row.get("contact") or "")[:50],
            str(row.get("address2") or "")[:250],
            address_id,
        ),
    )


def _insert_vendor(cur, row: dict, client_id: int, client_company_id: int, company_code: str, address_id: int) -> int:
    cur.execute(
        "INSERT INTO Vendor (ClientId, VendorNo, VendorName, AddressId, CompanyCode, "
        "ClientCompanyId, IsActive, VendorType, Email) "
        "VALUES (%s, %s, %s, %s, %s, %s, 1, %s, %s)",
        (
            client_id,
            str(row.get("vendor_no") or "")[:250],
            str(row.get("vendor_name") or "")[:250],
            address_id,
            company_code[:50] if company_code else "",
            client_company_id,
            str(row.get("vendor_type") or "")[:50] or None,
            str(row.get("email") or "")[:250] or None,
        ),
    )
    return cur.lastrowid


def import_vendors(
    conn,
    rows: list[dict],
    client_id: int,
    client_company_id: int,
    company_code: str,
    dry_run: bool = False,
) -> tuple[dict, list[dict]]:
    """
    Inserts new vendors + addresses or updates existing address records.
    Expected row fields: vendor_no*, vendor_name*, street_name, city, state_short,
                         zipcode, phone, contact, vendor_type, address2, email
    """
    summary = {"inserted": 0, "updated": 0, "skipped": 0, "errors": []}
    log: list[dict] = []

    try:
        states = _load_states(conn)
        with conn.cursor() as cur:
            for row in rows:
                vendor_no = str(row.get("vendor_no") or "").strip()
                vendor_name = str(row.get("vendor_name") or "").strip()
                row_num = row.get("_row", "?")

                if not vendor_no and not vendor_name:
                    continue

                state_short = str(row.get("state_short") or "").strip()
                state_id = states.get(state_short)

                try:
                    existing = _get_vendor(conn, vendor_no, client_id, client_company_id)

                    if existing is None:
                        if not dry_run:
                            addr_id = _insert_address(cur, row, state_id)
                            _insert_vendor(cur, row, client_id, client_company_id, company_code, addr_id)
                        summary["inserted"] += 1
                        log.append({"row": row_num, "action": "inserted" if not dry_run else "would insert", "vendor_no": vendor_no, "vendor_name": vendor_name, "detail": ""})
                    else:
                        vendor_id, address_id = existing
                        if not dry_run:
                            _update_address(cur, address_id, row, state_id)
                        summary["updated"] += 1
                        log.append({"row": row_num, "action": "updated" if not dry_run else "would update", "vendor_no": vendor_no, "vendor_name": vendor_name, "detail": f"VendorId={vendor_id}"})

                except Exception as exc:
                    summary["errors"].append(f"Row {row_num} ({vendor_no}): {exc}")
                    log.append({"row": row_num, "action": "error", "vendor_no": vendor_no, "vendor_name": vendor_name, "detail": str(exc)})

        if not dry_run:
            conn.commit()
    except Exception as exc:
        conn.rollback()
        summary["errors"].append(f"Transaction rolled back: {exc}")

    return summary, log
