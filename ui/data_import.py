from __future__ import annotations
import os
import pandas as pd
import streamlit as st
from datetime import datetime

from db.connection import get_connection
from migration.data_import import read_excel_rows, backup_import_tables
from migration.data_import.vendor import import_vendors
from migration.data_import.vendor_department import import_vendor_departments
from migration.data_import.vendor_gl_default import import_vendor_gl_defaults
from migration.data_import.department import import_departments
from migration.data_import.gl_code import import_gl_codes
from migration.data_import.approval import (
    import_approval_substep_users,
    import_approval_user_vendors,
    import_approval_user_departments,
    import_approval_user_vendor_departments,
)
from migration.data_import.approver_gl_code import import_approver_gl_codes
from migration.data_import.approver_amount import import_approver_amounts
from migration.data_import.user_registration import import_users
from migration.data_import.invoice import lookup_invoices
from ui.components.column_mapper import render_column_mapper
from config import ENV_LABELS


# ── Shared UI helpers ────────────────────────────────────────────────────────

def _show_results(summary: dict, log: list[dict]) -> None:
    keys = [k for k in summary if k != "errors"]
    cols = st.columns(len(keys) + 1)
    for i, k in enumerate(keys):
        cols[i].metric(k.capitalize(), summary[k])
    n_err = len(summary.get("errors", []))
    cols[-1].metric("Errors", n_err, delta="⚠" if n_err else None,
                    delta_color="inverse" if n_err else "off")

    if n_err:
        with st.expander("Error details", expanded=True):
            for e in summary["errors"][:50]:
                st.error(e)

    if log:
        with st.expander(f"Row log ({min(len(log), 200)} of {len(log)} rows)", expanded=False):
            st.dataframe(pd.DataFrame(log[:200]), use_container_width=True, height=280)


def _get_conn(env: str):
    try:
        return get_connection(env)
    except RuntimeError as exc:
        st.error(str(exc))
        return None


def _do_backup(conn, specs: list[dict], client_id: int, env: str) -> bool:
    """Run backup and show inline results. Returns True if all succeeded."""
    with st.spinner("Creating backups…"):
        results = backup_import_tables(conn, specs, client_id, env)

    all_ok = True
    for file_path, row_count, error in results:
        if error:
            st.warning(f"Backup warning — {error}")
            all_ok = False
        else:
            fname = os.path.basename(file_path)
            st.info(f"Backed up {row_count} rows → `{fname}`", icon="💾")
    return all_ok


def _backup_checkbox(key: str) -> bool:
    return st.checkbox(
        "Backup affected tables before import",
        value=True,
        key=key,
        help="Snapshots current DB rows to a JSON file. "
             "Tables with a direct ClientId column can be restored via Settings → Backup Manager. "
             "Other tables are saved for reference.",
    )


# ── Backup spec builders ─────────────────────────────────────────────────────
# Each returns a list of spec dicts for backup_import_tables.
# client_id_column=None means the table has no direct ClientId — backup is
# created but auto-restore via Settings will not work for those entries.

def _specs_vendor(cid):
    return [
        {"table": "Vendor",  "query": "SELECT * FROM Vendor WHERE ClientId=%s", "params": (cid,), "client_id_column": "ClientId"},
        {"table": "Address", "query": "SELECT a.* FROM Address a INNER JOIN Vendor v ON v.AddressId=a.AddressId WHERE v.ClientId=%s", "params": (cid,), "client_id_column": None},
    ]

def _specs_dept(cid):
    return [{"table": "Department", "query": "SELECT * FROM Department WHERE ClientId=%s", "params": (cid,), "client_id_column": "ClientId"}]

def _specs_gl(cid):
    return [{"table": "GLCode", "query": "SELECT * FROM GLCode WHERE ClientId=%s", "params": (cid,), "client_id_column": "ClientId"}]

def _specs_vd(cid):
    return [{"table": "VendorDepartment", "query": "SELECT vd.* FROM VendorDepartment vd INNER JOIN Vendor v ON v.VendorId=vd.VendorId WHERE v.ClientId=%s", "params": (cid,), "client_id_column": None}]

def _specs_vgl(cid):
    return [
        {"table": "VendorGlDefault", "query": "SELECT * FROM VendorGlDefault WHERE ClientId=%s", "params": (cid,), "client_id_column": "ClientId"},
        {"table": "Vendor",          "query": "SELECT * FROM Vendor WHERE ClientId=%s",          "params": (cid,), "client_id_column": "ClientId"},
    ]

def _specs_approval_substep(cid):
    q = ("SELECT asu.* FROM ApprovalSubStepUser asu "
         "INNER JOIN ApprovalSubStep ass ON ass.ApprovalSubStepId=asu.ApprovalSubStepId "
         "INNER JOIN ApprovalStep ap ON ap.ApprovalStepId=ass.ApprovalStepId "
         "WHERE ap.ClientId=%s")
    return [{"table": "ApprovalSubStepUser", "query": q, "params": (cid,), "client_id_column": None}]

def _specs_approval_vendor(cid):
    q = ("SELECT auv.* FROM ApprovalSubStepUserVendor auv "
         "INNER JOIN Vendor v ON v.VendorId=auv.VendorId WHERE v.ClientId=%s")
    return [{"table": "ApprovalSubStepUserVendor", "query": q, "params": (cid,), "client_id_column": None}]

def _specs_approval_dept(cid):
    q = ("SELECT aud.* FROM ApprovalSubStepUserDepartment aud "
         "INNER JOIN Department d ON d.Id=aud.DepartmentId WHERE d.ClientId=%s")
    return [{"table": "ApprovalSubStepUserDepartment", "query": q, "params": (cid,), "client_id_column": None}]

def _specs_approval_4way(cid):
    q = ("SELECT auvd.* FROM ApprovalSubStepUserVendorDepartment auvd "
         "INNER JOIN Vendor v ON v.VendorId=auvd.VendorId WHERE v.ClientId=%s")
    return [{"table": "ApprovalSubStepUserVendorDepartment", "query": q, "params": (cid,), "client_id_column": None}]

def _specs_approver_gl(cid):
    q = ("SELECT ag.* FROM ApproverGLCode ag "
         "INNER JOIN GLCode g ON g.GLCodeId=ag.GLCodeId WHERE g.ClientId=%s")
    return [{"table": "ApproverGLCode", "query": q, "params": (cid,), "client_id_column": None}]

def _specs_approver_amount(cid):
    q = ("SELECT ab.* FROM ApproverByAmount ab "
         "INNER JOIN User u ON u.UserId=ab.UserId WHERE u.ClientId=%s")
    return [{"table": "ApproverByAmount", "query": q, "params": (cid,), "client_id_column": None}]

def _specs_users(cid):
    return [
        {"table": "User",      "query": "SELECT * FROM User WHERE ClientId=%s",                                                    "params": (cid,), "client_id_column": "ClientId"},
        {"table": "UserRoles", "query": "SELECT ur.* FROM UserRoles ur INNER JOIN User u ON u.UserId=ur.UserId WHERE u.ClientId=%s", "params": (cid,), "client_id_column": None},
    ]


# ── Main render ───────────────────────────────────────────────────────────────

def render_data_import() -> None:
    st.header("Data Import")
    st.caption("Load data from Excel files into any environment. Map columns interactively before running.")

    c1, c2, c3 = st.columns([1, 1, 1])
    with c1:
        env = st.selectbox(
            "Target Environment",
            options=["dev", "qa", "prod"],
            format_func=lambda e: ENV_LABELS[e],
            key="di_env",
        )
    with c2:
        client_id = int(st.number_input("Client ID", min_value=1, step=1, key="di_client_id"))
    with c3:
        company_id = int(st.number_input("Client Company ID", min_value=1, step=1, key="di_company_id"))

    if env == "prod":
        st.warning("You are targeting **Production**. All imports are live.")

    conns = st.session_state.get("connections", {})
    if env not in conns or not conns[env]:
        st.warning(f"Not connected to **{ENV_LABELS[env]}**. Configure the connection in the sidebar first.")
        return

    tab_vendors, tab_depts, tab_approvals, tab_users = st.tabs([
        "📦 Vendors",
        "🏢 Departments & GL Codes",
        "✅ Approvals",
        "👤 Users & Invoices",
    ])

    # ════════════════════════════════════════════════════════════════
    # TAB 1 — Vendors
    # ════════════════════════════════════════════════════════════════
    with tab_vendors:

        with st.expander("Vendor Address Import", expanded=False):
            st.caption("Insert new vendors with addresses or update existing addresses.")
            uploaded = st.file_uploader("Excel file", type=["xlsx"], key="di_vendor_file")
            if uploaded:
                file_bytes = uploaded.read()
                fields = [
                    {"key": "vendor_no",   "label": "Vendor Number",   "required": True,  "default_col": 1},
                    {"key": "vendor_name", "label": "Vendor Name",     "required": True,  "default_col": 2},
                    {"key": "street_name", "label": "Street/Address",  "required": False, "default_col": 4},
                    {"key": "city",        "label": "City",            "required": False, "default_col": 5},
                    {"key": "state_short", "label": "State (2-char)",  "required": False, "default_col": 7},
                    {"key": "zipcode",     "label": "Zip Code",        "required": False, "default_col": 8},
                    {"key": "phone",       "label": "Phone",           "required": False, "default_col": 9},
                    {"key": "contact",     "label": "Contact Person",  "required": False, "default_col": 12},
                    {"key": "vendor_type", "label": "Vendor Type",     "required": False, "default_col": 0},
                    {"key": "address2",    "label": "Address 2",       "required": False, "default_col": 0},
                    {"key": "email",       "label": "Email",           "required": False, "default_col": 0},
                ]
                col_map = render_column_mapper(file_bytes, fields, "di_vendor", default_start_row=2)
                if col_map:
                    company_code = st.text_input("Company Code", key="di_vendor_cc", placeholder="e.g. Toyota")
                    dry_run = st.checkbox("Dry Run", key="di_vendor_dry")
                    backup = _backup_checkbox("di_vendor_backup")
                    if st.button("▶ Run Vendor Import", key="di_vendor_run"):
                        conn = _get_conn(env)
                        if conn:
                            if backup and not dry_run:
                                _do_backup(conn, _specs_vendor(client_id), client_id, env)
                            rows = read_excel_rows(file_bytes, col_map, ["vendor_no", "vendor_name"])
                            with st.spinner(f"Importing {len(rows)} rows…"):
                                summary, log = import_vendors(conn, rows, client_id, company_id, company_code, dry_run)
                            _show_results(summary, log)

        with st.expander("Vendor Department Links", expanded=False):
            st.caption("Link existing vendors to departments by name.")
            uploaded = st.file_uploader("Excel file", type=["xlsx"], key="di_vd_file")
            if uploaded:
                file_bytes = uploaded.read()
                fields = [
                    {"key": "vendor_no",   "label": "Vendor Number",   "required": True, "default_col": 1},
                    {"key": "vendor_name", "label": "Vendor Name",     "required": True, "default_col": 2},
                    {"key": "dept_name",   "label": "Department Name", "required": True, "default_col": 3},
                ]
                col_map = render_column_mapper(file_bytes, fields, "di_vd", default_start_row=4)
                if col_map:
                    dry_run = st.checkbox("Dry Run", key="di_vd_dry")
                    backup = _backup_checkbox("di_vd_backup")
                    if st.button("▶ Run Vendor-Department Link", key="di_vd_run"):
                        conn = _get_conn(env)
                        if conn:
                            if backup and not dry_run:
                                _do_backup(conn, _specs_vd(client_id), client_id, env)
                            rows = read_excel_rows(file_bytes, col_map, ["vendor_no", "dept_name"])
                            with st.spinner(f"Linking {len(rows)} rows…"):
                                summary, log = import_vendor_departments(conn, rows, client_id, dry_run)
                            _show_results(summary, log)

    # ════════════════════════════════════════════════════════════════
    # TAB 2 — Departments & GL Codes
    # ════════════════════════════════════════════════════════════════
    with tab_depts:

        with st.expander("Department Import", expanded=False):
            st.caption("Bulk-load department names. Skips duplicates.")
            uploaded = st.file_uploader("Excel file", type=["xlsx"], key="di_dept_file")
            if uploaded:
                file_bytes = uploaded.read()
                fields = [
                    {"key": "dept_name", "label": "Department Name", "required": True, "default_col": 3},
                ]
                col_map = render_column_mapper(file_bytes, fields, "di_dept", default_start_row=4)
                if col_map:
                    dry_run = st.checkbox("Dry Run", key="di_dept_dry")
                    backup = _backup_checkbox("di_dept_backup")
                    if st.button("▶ Run Department Import", key="di_dept_run"):
                        conn = _get_conn(env)
                        if conn:
                            if backup and not dry_run:
                                _do_backup(conn, _specs_dept(client_id), client_id, env)
                            rows = read_excel_rows(file_bytes, col_map, ["dept_name"])
                            with st.spinner(f"Importing {len(rows)} departments…"):
                                summary, log = import_departments(conn, rows, client_id, dry_run)
                            _show_results(summary, log)

        with st.expander("GL Code Import — Flat", expanded=False):
            st.caption("Insert or update GL codes (no parent-child hierarchy).")
            uploaded = st.file_uploader("Excel file", type=["xlsx"], key="di_gl_file")
            if uploaded:
                file_bytes = uploaded.read()
                fields = [
                    {"key": "gl_code_name", "label": "GL Code Name", "required": True,  "default_col": 1},
                    {"key": "description",  "label": "Description",  "required": False, "default_col": 2},
                ]
                col_map = render_column_mapper(file_bytes, fields, "di_gl", default_start_row=2)
                if col_map:
                    dry_run = st.checkbox("Dry Run", key="di_gl_dry")
                    backup = _backup_checkbox("di_gl_backup")
                    if st.button("▶ Run GL Code Import", key="di_gl_run"):
                        conn = _get_conn(env)
                        if conn:
                            if backup and not dry_run:
                                _do_backup(conn, _specs_gl(client_id), client_id, env)
                            rows = read_excel_rows(file_bytes, col_map, ["gl_code_name"])
                            with st.spinner(f"Importing {len(rows)} GL codes…"):
                                summary, log = import_gl_codes(conn, rows, client_id, company_id, mode="flat", dry_run=dry_run)
                            _show_results(summary, log)

        with st.expander("GL Code Import — Split / Hierarchical", expanded=False):
            st.caption(
                "Parent rows have a value in the Parent GL Name column; "
                "child rows have a value in the Child GL Name column. "
                "Percentage is 0–100 (stored as a decimal fraction)."
            )
            uploaded = st.file_uploader("Excel file", type=["xlsx"], key="di_sgl_file")
            if uploaded:
                file_bytes = uploaded.read()
                fields = [
                    {"key": "parent_gl_name", "label": "Parent GL Code Name", "required": False, "default_col": 1},
                    {"key": "description",    "label": "Description",         "required": False, "default_col": 2},
                    {"key": "child_gl_name",  "label": "Child GL Code Name",  "required": False, "default_col": 3},
                    {"key": "percentage",     "label": "Percentage (0–100)",  "required": False, "default_col": 4},
                ]
                col_map = render_column_mapper(file_bytes, fields, "di_sgl", default_start_row=2)
                if col_map:
                    dry_run = st.checkbox("Dry Run", key="di_sgl_dry")
                    backup = _backup_checkbox("di_sgl_backup")
                    if st.button("▶ Run Split GL Code Import", key="di_sgl_run"):
                        conn = _get_conn(env)
                        if conn:
                            if backup and not dry_run:
                                _do_backup(conn, _specs_gl(client_id), client_id, env)
                            rows = read_excel_rows(file_bytes, col_map, [])
                            with st.spinner(f"Importing {len(rows)} GL code rows…"):
                                summary, log = import_gl_codes(conn, rows, client_id, company_id, mode="split", dry_run=dry_run)
                            _show_results(summary, log)

        with st.expander("Vendor GL Defaults", expanded=False):
            st.caption("Set the default GL code per vendor and create VendorGlDefault records.")
            uploaded = st.file_uploader("Excel file", type=["xlsx"], key="di_vgl_file")
            if uploaded:
                file_bytes = uploaded.read()
                fields = [
                    {"key": "vendor_no",    "label": "Vendor Number", "required": True, "default_col": 1},
                    {"key": "vendor_name",  "label": "Vendor Name",   "required": True, "default_col": 2},
                    {"key": "gl_code_name", "label": "GL Code Name",  "required": True, "default_col": 3},
                ]
                col_map = render_column_mapper(file_bytes, fields, "di_vgl", default_start_row=4)
                if col_map:
                    dry_run = st.checkbox("Dry Run", key="di_vgl_dry")
                    backup = _backup_checkbox("di_vgl_backup")
                    if st.button("▶ Run Vendor GL Default Import", key="di_vgl_run"):
                        conn = _get_conn(env)
                        if conn:
                            if backup and not dry_run:
                                _do_backup(conn, _specs_vgl(client_id), client_id, env)
                            rows = read_excel_rows(file_bytes, col_map, ["vendor_no", "gl_code_name"])
                            with st.spinner(f"Processing {len(rows)} rows…"):
                                summary, log = import_vendor_gl_defaults(conn, rows, client_id, dry_run)
                            _show_results(summary, log)

    # ════════════════════════════════════════════════════════════════
    # TAB 3 — Approvals
    # ════════════════════════════════════════════════════════════════
    with tab_approvals:

        with st.expander("Approval Sub-Step Users", expanded=False):
            st.caption("Link users to an approval sub-step workflow.")
            uploaded = st.file_uploader("Excel file", type=["xlsx"], key="di_asu_file")
            if uploaded:
                file_bytes = uploaded.read()
                fields = [
                    {"key": "vendor_no",          "label": "Vendor Number",       "required": False, "default_col": 2},
                    {"key": "approval_one_email", "label": "Approver 1 Email",    "required": True,  "default_col": 6},
                    {"key": "approval_two_email", "label": "Approver 2 Email",    "required": False, "default_col": 9},
                    {"key": "max_amount",         "label": "Max Allowed Amount",  "required": False, "default_col": 26},
                ]
                col_map = render_column_mapper(file_bytes, fields, "di_asu", default_start_row=5, default_sheet_index=1)
                if col_map:
                    ca, cb = st.columns(2)
                    with ca:
                        company_code = st.text_input("Company Code", key="di_asu_cc", placeholder="e.g. Tulkoff")
                    with cb:
                        substep_name = st.text_input("Sub-Step Name", key="di_asu_sn", value="Sub Step 1 ( PO)")
                    also_amounts = st.checkbox("Also import Approver By Amount (uses max_amount column)", key="di_asu_amounts")
                    dry_run = st.checkbox("Dry Run", key="di_asu_dry")
                    backup = _backup_checkbox("di_asu_backup")
                    if st.button("▶ Run Approval User Import", key="di_asu_run"):
                        conn = _get_conn(env)
                        if conn:
                            if backup and not dry_run:
                                specs = _specs_approval_substep(client_id)
                                if also_amounts:
                                    specs += _specs_approver_amount(client_id)
                                _do_backup(conn, specs, client_id, env)
                            rows = read_excel_rows(file_bytes, col_map, ["approval_one_email"])
                            with st.spinner(f"Processing {len(rows)} rows…"):
                                summary, log = import_approval_substep_users(
                                    conn, rows, client_id, company_code, substep_name, dry_run
                                )
                            _show_results(summary, log)
                            if also_amounts:
                                amt_map = {
                                    "sheet_index": col_map.get("sheet_index", 0),
                                    "start_row":   col_map.get("start_row", 5),
                                    "first_approver_email":  col_map.get("approval_one_email", 6),
                                    "second_approver_email": col_map.get("approval_two_email", 9),
                                    "max_amount":            col_map.get("max_amount", 26),
                                }
                                amt_rows = read_excel_rows(file_bytes, amt_map, ["first_approver_email"])
                                with st.spinner("Importing approver amounts…"):
                                    amt_sum, amt_log = import_approver_amounts(conn, amt_rows, client_id, company_id, dry_run)
                                st.markdown("**Approver By Amount results:**")
                                _show_results(amt_sum, amt_log)

        with st.expander("Approval User → Vendor", expanded=False):
            st.caption("Link specific users + approval sub-step to individual vendors.")
            uploaded = st.file_uploader("Excel file", type=["xlsx"], key="di_auv_file")
            if uploaded:
                file_bytes = uploaded.read()
                fields = [
                    {"key": "vendor_no",  "label": "Vendor Number", "required": True, "default_col": 1},
                    {"key": "user_email", "label": "User Email",    "required": True, "default_col": 2},
                ]
                col_map = render_column_mapper(file_bytes, fields, "di_auv", default_start_row=2)
                if col_map:
                    substep_id = int(st.number_input("Approval Sub-Step ID", min_value=1, step=1, key="di_auv_sid"))
                    dry_run = st.checkbox("Dry Run", key="di_auv_dry")
                    backup = _backup_checkbox("di_auv_backup")
                    if st.button("▶ Run", key="di_auv_run"):
                        conn = _get_conn(env)
                        if conn:
                            if backup and not dry_run:
                                _do_backup(conn, _specs_approval_vendor(client_id), client_id, env)
                            rows = read_excel_rows(file_bytes, col_map, ["vendor_no", "user_email"])
                            with st.spinner(f"Processing {len(rows)} rows…"):
                                summary, log = import_approval_user_vendors(conn, rows, client_id, substep_id, dry_run)
                            _show_results(summary, log)

        with st.expander("Approval User → Department", expanded=False):
            st.caption("Link specific users + approval sub-step to departments.")
            uploaded = st.file_uploader("Excel file", type=["xlsx"], key="di_aud_file")
            if uploaded:
                file_bytes = uploaded.read()
                fields = [
                    {"key": "dept_name",  "label": "Department Name", "required": True, "default_col": 1},
                    {"key": "user_email", "label": "User Email",      "required": True, "default_col": 2},
                ]
                col_map = render_column_mapper(file_bytes, fields, "di_aud", default_start_row=2)
                if col_map:
                    substep_id = int(st.number_input("Approval Sub-Step ID", min_value=1, step=1, key="di_aud_sid"))
                    dry_run = st.checkbox("Dry Run", key="di_aud_dry")
                    backup = _backup_checkbox("di_aud_backup")
                    if st.button("▶ Run", key="di_aud_run"):
                        conn = _get_conn(env)
                        if conn:
                            if backup and not dry_run:
                                _do_backup(conn, _specs_approval_dept(client_id), client_id, env)
                            rows = read_excel_rows(file_bytes, col_map, ["dept_name", "user_email"])
                            with st.spinner(f"Processing {len(rows)} rows…"):
                                summary, log = import_approval_user_departments(conn, rows, client_id, substep_id, dry_run)
                            _show_results(summary, log)

        with st.expander("Approval User → Vendor + Department (4-way)", expanded=False):
            st.caption("Insert 4-way mappings directly using raw IDs from the Excel.")
            uploaded = st.file_uploader("Excel file", type=["xlsx"], key="di_auvd_file")
            if uploaded:
                file_bytes = uploaded.read()
                fields = [
                    {"key": "user_id",             "label": "User ID (UUID)",        "required": True, "default_col": 1},
                    {"key": "approval_substep_id", "label": "Approval Sub-Step ID",  "required": True, "default_col": 2},
                    {"key": "department_id",       "label": "Department ID",         "required": True, "default_col": 3},
                    {"key": "vendor_id",           "label": "Vendor ID",             "required": True, "default_col": 4},
                ]
                col_map = render_column_mapper(file_bytes, fields, "di_auvd", default_start_row=2)
                if col_map:
                    dry_run = st.checkbox("Dry Run", key="di_auvd_dry")
                    backup = _backup_checkbox("di_auvd_backup")
                    if st.button("▶ Run 4-Way Import", key="di_auvd_run"):
                        conn = _get_conn(env)
                        if conn:
                            if backup and not dry_run:
                                _do_backup(conn, _specs_approval_4way(client_id), client_id, env)
                            rows = read_excel_rows(file_bytes, col_map, ["user_id", "approval_substep_id"])
                            with st.spinner(f"Processing {len(rows)} rows…"):
                                summary, log = import_approval_user_vendor_departments(conn, rows, client_id, dry_run)
                            _show_results(summary, log)

        with st.expander("Approver GL Codes", expanded=False):
            st.caption("Map GL codes to approver users. Supports process, sync, and insert-only modes.")
            uploaded = st.file_uploader("Excel file", type=["xlsx"], key="di_agl_file")
            if uploaded:
                file_bytes = uploaded.read()
                fields = [
                    {"key": "user_email",   "label": "User Email / Username",  "required": True,  "default_col": 2},
                    {"key": "gl_code_name", "label": "GL Code Name",           "required": True,  "default_col": 3},
                    {"key": "action",       "label": "Action (INSERT/DELETE)", "required": False, "default_col": 5},
                ]
                col_map = render_column_mapper(file_bytes, fields, "di_agl", default_start_row=2)
                if col_map:
                    mode = st.radio(
                        "Mode",
                        options=["process", "sync", "insert"],
                        format_func=lambda m: {
                            "process": "Process — use Action column per row",
                            "sync":    "Sync — match Excel exactly (deletes removed rows)",
                            "insert":  "Insert only — skip existing",
                        }[m],
                        key="di_agl_mode",
                        horizontal=True,
                    )
                    if mode == "sync":
                        st.warning("Sync mode will **delete** ApproverGLCode records not present in the file.")
                    dry_run = st.checkbox("Dry Run", key="di_agl_dry")
                    backup = _backup_checkbox("di_agl_backup")
                    if st.button("▶ Run Approver GL Code Import", key="di_agl_run"):
                        conn = _get_conn(env)
                        if conn:
                            if backup and not dry_run:
                                _do_backup(conn, _specs_approver_gl(client_id), client_id, env)
                            rows = read_excel_rows(file_bytes, col_map, ["user_email", "gl_code_name"])
                            with st.spinner(f"Processing {len(rows)} rows (mode={mode})…"):
                                summary, log = import_approver_gl_codes(conn, rows, client_id, mode, dry_run)
                            _show_results(summary, log)

        with st.expander("Approver By Amount", expanded=False):
            st.caption("Set amount-based approval thresholds.")
            uploaded = st.file_uploader("Excel file", type=["xlsx"], key="di_aba_file")
            if uploaded:
                file_bytes = uploaded.read()
                fields = [
                    {"key": "first_approver_email",  "label": "First Approver Email",  "required": True,  "default_col": 6},
                    {"key": "second_approver_email", "label": "Second Approver Email", "required": False, "default_col": 9},
                    {"key": "max_amount",            "label": "Max Allowed Amount",    "required": False, "default_col": 26},
                ]
                col_map = render_column_mapper(file_bytes, fields, "di_aba", default_start_row=2)
                if col_map:
                    dry_run = st.checkbox("Dry Run", key="di_aba_dry")
                    backup = _backup_checkbox("di_aba_backup")
                    if st.button("▶ Run Approver Amount Import", key="di_aba_run"):
                        conn = _get_conn(env)
                        if conn:
                            if backup and not dry_run:
                                _do_backup(conn, _specs_approver_amount(client_id), client_id, env)
                            rows = read_excel_rows(file_bytes, col_map, ["first_approver_email"])
                            with st.spinner(f"Processing {len(rows)} rows…"):
                                summary, log = import_approver_amounts(conn, rows, client_id, company_id, dry_run)
                            _show_results(summary, log)

    # ════════════════════════════════════════════════════════════════
    # TAB 4 — Users & Invoices
    # ════════════════════════════════════════════════════════════════
    with tab_users:

        with st.expander("User Registration", expanded=False):
            st.caption(
                "Bulk-create user accounts. Password is left blank — users set it after creation. "
                "Skips emails that already exist."
            )
            uploaded = st.file_uploader("Excel file", type=["xlsx"], key="di_usr_file")
            if uploaded:
                file_bytes = uploaded.read()
                fields = [
                    {"key": "first_name", "label": "First Name", "required": True, "default_col": 4},
                    {"key": "last_name",  "label": "Last Name",  "required": True, "default_col": 5},
                    {"key": "email",      "label": "Email",      "required": True, "default_col": 6},
                ]
                col_map = render_column_mapper(file_bytes, fields, "di_usr", default_start_row=5)
                if col_map:
                    addr_str = st.text_input("Default Address ID (optional)", key="di_usr_addr", placeholder="Leave blank for none")
                    default_addr = int(addr_str) if addr_str.strip().isdigit() else None
                    dry_run = st.checkbox("Dry Run", key="di_usr_dry")
                    backup = _backup_checkbox("di_usr_backup")
                    if st.button("▶ Run User Registration", key="di_usr_run"):
                        conn = _get_conn(env)
                        if conn:
                            if backup and not dry_run:
                                _do_backup(conn, _specs_users(client_id), client_id, env)
                            rows = read_excel_rows(file_bytes, col_map, ["email"])
                            with st.spinner(f"Registering {len(rows)} users…"):
                                summary, log = import_users(conn, rows, client_id, default_addr, dry_run)
                            _show_results(summary, log)

        with st.expander("Invoice Lookup", expanded=False):
            st.caption("Find invoices by number + vendor. Read-only — results downloadable as CSV.")
            uploaded = st.file_uploader("Excel file", type=["xlsx"], key="di_inv_file")
            if uploaded:
                file_bytes = uploaded.read()
                fields = [
                    {"key": "invoice_no",    "label": "Invoice Number", "required": True,  "default_col": 3},
                    {"key": "vendor_no",     "label": "Vendor Number",  "required": True,  "default_col": 5},
                    {"key": "invoice_total", "label": "Invoice Total",  "required": False, "default_col": 8},
                ]
                col_map = render_column_mapper(file_bytes, fields, "di_inv", default_start_row=2)
                if col_map:
                    year = int(st.number_input(
                        "Year filter (ScannedDate)",
                        min_value=2000, max_value=datetime.now().year + 1,
                        value=datetime.now().year, step=1, key="di_inv_year",
                    ))
                    if st.button("🔍 Run Invoice Lookup", key="di_inv_run"):
                        conn = _get_conn(env)
                        if conn:
                            rows = read_excel_rows(file_bytes, col_map, ["invoice_no", "vendor_no"])
                            with st.spinner(f"Looking up {len(rows)} invoices…"):
                                df = lookup_invoices(conn, rows, client_id, year)
                            if df.empty:
                                st.info("No matching invoices found.")
                            else:
                                st.success(f"Found {len(df)} invoice(s).")
                                st.dataframe(df, use_container_width=True, height=320)
                                st.download_button(
                                    "⬇ Download CSV",
                                    data=df.to_csv(index=False).encode(),
                                    file_name=f"invoices_client{client_id}_{year}.csv",
                                    mime="text/csv",
                                    key="di_inv_dl",
                                )
