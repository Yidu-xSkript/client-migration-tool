from __future__ import annotations
import pandas as pd
from decimal import Decimal, InvalidOperation


def lookup_invoices(
    conn,
    rows: list[dict],
    client_id: int,
    year: int,
) -> pd.DataFrame:
    """
    Finds invoices matching criteria in each Excel row.
    Expected row fields: invoice_no*, vendor_no*, invoice_total
    Returns a DataFrame of all matched invoices (deduped by InvoiceId).
    """
    all_results: list[dict] = []
    seen_ids: set[int] = set()

    with conn.cursor() as cur:
        for row in rows:
            invoice_no = str(row.get("invoice_no") or "").strip()
            vendor_no = str(row.get("vendor_no") or "").strip()
            if not invoice_no:
                continue

            cur.execute(
                """
                SELECT i.InvoiceId, i.InvoiceNo, i.InvoiceTotal, i.ScannedDate,
                       v.VendorName, v.VendorNo
                FROM Invoice i
                INNER JOIN Vendor v ON v.VendorId = i.VendorId
                WHERE i.ClientId = %s
                  AND i.InvoiceNo = %s
                  AND YEAR(i.ScannedDate) = %s
                  AND i.InvoiceType = 'Invoice'
                  AND v.VendorNo = %s
                LIMIT 50
                """,
                (client_id, invoice_no, year, vendor_no),
            )
            for r in cur.fetchall():
                if r["InvoiceId"] not in seen_ids:
                    seen_ids.add(r["InvoiceId"])
                    all_results.append(r)

    if not all_results:
        return pd.DataFrame(columns=["InvoiceId", "InvoiceNo", "InvoiceTotal", "ScannedDate", "VendorName", "VendorNo"])

    return pd.DataFrame(all_results)
