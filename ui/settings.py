# ui/settings.py — Settings tab: profiles, backup manager, audit log, health checks, export

from __future__ import annotations
import json
import os
import pandas as pd
import streamlit as st

from db.connection import test_connection, get_connection
from db.discovery import discover_related_tables
from db.operations import read_client_data
from migration import audit
from migration.backup import (
    list_backups, restore_backup, delete_backup, cleanup_old_backups,
)
from migration.profiles import (
    MigrationProfile, save_profile, load_all_profiles, delete_profile,
)
from migration.validation import PRE_CHECKS
from config import (
    ENV_LABELS, ENV_ORDER, AUDIT_LOG_PATH, EDIT_LOG_PATH,
    AUDIT_LOG_DISPLAY_LIMIT, BACKUP_RETENTION_DAYS,
)


def render_settings() -> None:
    st.header("Settings & Advanced")

    tabs = st.tabs([
        "Connection Health",
        "Migration Profiles",
        "Backup Manager",
        "Migration History",
        "Edit History",
        "Export Client Data",
    ])

    with tabs[0]:
        _render_health_checks()
    with tabs[1]:
        _render_profiles()
    with tabs[2]:
        _render_backup_manager()
    with tabs[3]:
        _render_audit_log()
    with tabs[4]:
        _render_edit_history()
    with tabs[5]:
        _render_export()


# ---------------------------------------------------------------------------
# Health checks
# ---------------------------------------------------------------------------

def _render_health_checks() -> None:
    st.subheader("Connection Health Check")
    cols = st.columns(len(ENV_ORDER))
    for col, env in zip(cols, ENV_ORDER):
        label = ENV_LABELS[env]
        col.markdown(f"**{label}**")
        if col.button(f"Test {label}", key=f"health_{env}", use_container_width=True):
            with st.spinner(f"Testing {label}…"):
                ok, msg = test_connection(env)
            if ok:
                col.success(msg)
            else:
                col.error(msg)


# ---------------------------------------------------------------------------
# Migration profiles
# ---------------------------------------------------------------------------

def _render_profiles() -> None:
    st.subheader("Migration Profiles")
    st.caption(
        "Save named migration configurations (client lists, route, options). "
        "Profiles can be loaded in the Batch tab or run from the CLI."
    )

    profiles = load_all_profiles()

    # ---- Existing profiles ----
    if profiles:
        for p in profiles:
            with st.expander(f"**{p.name}** — {ENV_LABELS.get(p.src_env, p.src_env)} → {ENV_LABELS.get(p.dst_env, p.dst_env)}", expanded=False):
                col_info, col_del = st.columns([4, 1])
                col_info.markdown(
                    f"- **Clients:** {', '.join(str(i) for i in p.client_ids) or 'prompt at runtime'}\n"
                    f"- **Conflict:** `{p.conflict_mode}` | **Delta:** {'✓' if p.delta_mode else '✗'} | **Backup:** {'✓' if p.do_backup else '✗'}\n"
                    f"- **Pre-checks:** {', '.join(p.pre_checks) or 'none'}\n"
                    + (f"- **Description:** {p.description}" if p.description else "")
                )
                if col_del.button("Delete", key=f"del_profile_{p.name}", type="secondary"):
                    delete_profile(p.name)
                    st.rerun()
    else:
        st.info("No profiles saved yet.")

    st.divider()

    # ---- Create new profile ----
    st.markdown("**Create New Profile**")
    with st.form("new_profile_form"):
        p_name = st.text_input("Profile name", placeholder="Nightly Dev→QA")
        p_desc = st.text_input("Description (optional)")

        col_src, col_dst = st.columns(2)
        p_src = col_src.selectbox("Source", ENV_ORDER, format_func=lambda e: ENV_LABELS[e])
        p_dst = col_dst.selectbox("Destination", ENV_ORDER, format_func=lambda e: ENV_LABELS[e], index=1)

        p_ids_raw = st.text_area(
            "Client IDs (one per line, or leave blank to prompt at runtime)",
            height=80,
        )
        p_ids = [int(x.strip()) for x in p_ids_raw.replace(",", "\n").split("\n")
                 if x.strip().isdigit()]

        col_o1, col_o2, col_o3 = st.columns(3)
        p_conflict = col_o1.selectbox("Conflict mode", ["replace", "skip", "update"])
        p_delta    = col_o2.checkbox("Delta mode", value=False)
        p_backup   = col_o3.checkbox("Create backup", value=True)

        p_checks = st.multiselect(
            "Pre-migration checks",
            options=list(PRE_CHECKS.keys()),
            default=["source_has_data", "row_count_positive"],
            format_func=lambda k: PRE_CHECKS[k],
        )

        p_excl_raw = st.text_area(
            "Column exclusions (JSON: {\"Table\": [\"col\"]})",
            height=60,
        )
        p_filter_raw = st.text_area(
            "Row filters (JSON: {\"Table\": \"condition\"})",
            height=60,
        )

        submitted = st.form_submit_button("Save Profile", type="primary")

    if submitted:
        if not p_name.strip():
            st.error("Profile name is required.")
        elif p_src == p_dst:
            st.error("Source and destination must be different environments.")
        else:
            excl   = _safe_json(p_excl_raw, {})
            filters = _safe_json(p_filter_raw, {})
            profile = MigrationProfile(
                name=p_name.strip(),
                description=p_desc.strip(),
                src_env=p_src,
                dst_env=p_dst,
                client_ids=p_ids,
                conflict_mode=p_conflict,
                delta_mode=p_delta,
                do_backup=p_backup,
                excluded_columns=excl,
                row_filters=filters,
                pre_checks=p_checks,
            )
            save_profile(profile)
            st.success(f"Profile '{p_name}' saved.")
            st.rerun()


# ---------------------------------------------------------------------------
# Backup manager
# ---------------------------------------------------------------------------

def _render_backup_manager() -> None:
    st.subheader("Backup Manager")
    st.caption(
        "Backups are JSON files stored on disk under the `backups/` folder "
        "(one subfolder per environment). Nothing is written to the database."
    )

    col_env, col_cid, col_refresh = st.columns([2, 2, 1])
    env_filter = col_env.selectbox(
        "Filter by environment (blank = all)",
        ["(all)"] + ENV_ORDER,
        format_func=lambda e: "All environments" if e == "(all)" else ENV_LABELS.get(e, e),
        key="bkp_env",
    )
    filter_cid_raw = col_cid.text_input("Filter by Client ID (optional)", key="bkp_filter_cid")
    col_refresh.button("🔄 Refresh", use_container_width=True, key="bkp_load")

    # Auto-load / reload when filters change or on first visit
    env_arg     = None if env_filter == "(all)" else env_filter
    filter_cid  = int(filter_cid_raw) if filter_cid_raw.strip().isdigit() else None
    auto_key    = f"bkp_auto_{env_arg}_{filter_cid}"
    needs_load  = (
        "bkp_list" not in st.session_state
        or st.session_state.get("bkp_auto_last_key") != auto_key
        or st.session_state.get("bkp_load")   # refresh button
    )
    if needs_load:
        backups = list_backups(env=env_arg, client_id=filter_cid)
        st.session_state["bkp_list"]         = backups
        st.session_state["bkp_auto_last_key"] = auto_key

    backups = st.session_state.get("bkp_list", [])

    if not backups:
        st.info("No backup files found.")
    else:
        rows = [
            {
                "Backup File":     b.backup_name,
                "Environment":     ENV_LABELS.get(b.env, b.env),
                "Original Table":  b.original_table,
                "Client ID":       b.client_id,
                "Rows":            b.row_count,
                "Created":         b.created_at.strftime("%Y-%m-%d %H:%M:%S"),
                "Age (days)":      b.age_days,
            }
            for b in backups
        ]
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

        backup_labels = [
            f"{b.backup_name}  [{ENV_LABELS.get(b.env, b.env)}]"
            for b in backups
        ]

        # ---- Restore ----
        st.markdown("**Restore a Backup**")
        st.caption(
            "Restoring deletes the client's current rows in the target table "
            "and re-inserts the snapshotted rows. Make sure you are connected to "
            "the correct environment in the sidebar."
        )
        restore_idx = st.selectbox(
            "Select backup to restore", range(len(backup_labels)),
            format_func=lambda i: backup_labels[i], key="bkp_restore_sel",
        )
        restore_env = backups[restore_idx].env if backups else None
        if restore_env:
            st.caption(f"This backup belongs to **{ENV_LABELS.get(restore_env, restore_env)}**.")

        if st.button("Restore Selected Backup", type="primary", key="bkp_restore_btn"):
            chosen_info = backups[restore_idx]
            try:
                conn = get_connection(chosen_info.env)
                restored = restore_backup(chosen_info, conn)
                conn.close()
                st.success(
                    f"Restored {restored} rows into `{chosen_info.original_table}` "
                    f"in {ENV_LABELS.get(chosen_info.env, chosen_info.env)}."
                )
                del st.session_state["bkp_list"]
            except Exception as e:
                st.error(f"Restore failed: {e}")

        # ---- Delete individual ----
        st.markdown("**Delete a Backup**")
        del_idx = st.selectbox(
            "Select backup to delete", range(len(backup_labels)),
            format_func=lambda i: backup_labels[i], key="bkp_del_sel",
        )
        if st.button("Delete Selected Backup", key="bkp_del_btn"):
            try:
                delete_backup(backups[del_idx])
                st.success(f"Deleted `{backups[del_idx].backup_name}`.")
                del st.session_state["bkp_list"]
                st.rerun()
            except Exception as e:
                st.error(f"Delete failed: {e}")

    # ---- Retention cleanup ----
    st.divider()
    st.markdown("**Retention Policy — Bulk Cleanup**")
    col_days, col_cleanup = st.columns([2, 1])
    ret_days = col_days.number_input(
        "Delete backups older than (days)",
        min_value=1, max_value=365,
        value=BACKUP_RETENTION_DAYS,
        key="bkp_ret_days",
    )
    if col_cleanup.button("Run Cleanup", key="bkp_cleanup_btn", use_container_width=True):
        try:
            removed = cleanup_old_backups(
                days=int(ret_days),
                env=None if env_filter == "(all)" else env_filter,
            )
            st.success(f"Deleted {len(removed)} backup file(s) older than {ret_days} days.")
            if "bkp_list" in st.session_state:
                del st.session_state["bkp_list"]
        except Exception as e:
            st.error(f"Cleanup failed: {e}")


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------

def _render_audit_log() -> None:
    st.subheader("Migration History")

    if not os.path.exists(AUDIT_LOG_PATH):
        st.info("No migration history yet.")
        return

    entries = audit.read_recent(AUDIT_LOG_DISPLAY_LIMIT)
    if not entries:
        st.info("Audit log is empty.")
        return

    rows = []
    for e in entries:
        rows.append({
            "Timestamp": e.timestamp,
            "Route":     f"{ENV_LABELS.get(e.source_env, e.source_env)} → {ENV_LABELS.get(e.target_env, e.target_env)}",
            "Client ID": e.client_id,
            "Tables":    len(e.tables_migrated),
            "Rows":      sum(e.row_counts.values()) if e.row_counts else 0,
            "Status":    e.status.upper(),
            "Ticket":    e.ticket_number or "—",
            "Error":     (e.error_message[:60] + "…") if len(e.error_message) > 60 else e.error_message,
        })

    df = pd.DataFrame(rows)

    st.dataframe(df, use_container_width=True, hide_index=True)

    # Timeline chart
    if len(rows) > 1:
        st.subheader("Migration Timeline")
        df_chart = df.copy()
        df_chart["Timestamp"] = pd.to_datetime(df_chart["Timestamp"])
        df_chart = df_chart.sort_values("Timestamp")
        st.bar_chart(df_chart.set_index("Timestamp")["Rows"])

    with open(AUDIT_LOG_PATH, "r", encoding="utf-8") as f:
        raw = f.read()
    st.download_button(
        "Download Full Audit Log",
        data=raw,
        file_name="migration_audit.log",
        mime="text/plain",
    )


# ---------------------------------------------------------------------------
# Edit history
# ---------------------------------------------------------------------------

def _render_edit_history() -> None:
    st.subheader("Edit History")
    st.caption(
        "Every inline cell-edit Save is recorded here with full before/after diffs. "
        "Use the backup table name to restore manually if needed."
    )

    if not os.path.exists(EDIT_LOG_PATH):
        st.info("No edit history yet.")
        return

    entries = audit.read_edit_logs(AUDIT_LOG_DISPLAY_LIMIT)
    if not entries:
        st.info("Edit log is empty.")
        return

    # Summary table
    summary_rows = []
    for e in entries:
        summary_rows.append({
            "Timestamp":   e.timestamp,
            "Env":         ENV_LABELS.get(e.env, e.env),
            "Table":       e.table,
            "Client ID":   e.client_id,
            "Rows Edited": e.rows_changed,
            "Backup":      e.backup_table or "—",
        })

    df = pd.DataFrame(summary_rows)
    st.dataframe(df, use_container_width=True, hide_index=True)

    # Per-entry diff explorer
    st.markdown("---")
    st.markdown("#### Diff Explorer")
    st.caption("Select an entry to inspect exactly which columns changed.")

    labels = [
        f"{e.timestamp}  |  {e.table}  |  client {e.client_id}  |  {e.rows_changed} row(s)"
        for e in entries
    ]
    chosen_idx = st.selectbox("Select edit event", range(len(labels)),
                              format_func=lambda i: labels[i], key="edit_hist_sel")

    chosen = entries[chosen_idx]
    if not chosen.changes:
        st.info("No change detail recorded for this entry.")
    else:
        for i, ch in enumerate(chosen.changes, 1):
            pk_str = ", ".join(f"{k}={v}" for k, v in ch.get("pk", {}).items())
            with st.expander(f"Row {i}  —  PK: {pk_str or '(unknown)'}"):
                col_b, col_a = st.columns(2)
                col_b.markdown("**Before**")
                col_b.json(ch.get("before", {}))
                col_a.markdown("**After**")
                col_a.json(ch.get("after", {}))

        if chosen.backup_table:
            st.info(
                f"To roll back this edit, restore backup table **`{chosen.backup_table}`** "
                f"from the Backup Manager tab."
            )

    # Raw log download
    st.markdown("---")
    with open(EDIT_LOG_PATH, "r", encoding="utf-8") as f:
        raw = f.read()
    st.download_button(
        "Download Full Edit Log",
        data=raw,
        file_name="edit_audit.log",
        mime="text/plain",
    )


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def _render_export() -> None:
    st.subheader("Export Client Data")
    st.caption("Export a client's data as JSON or SQL INSERT statements.")

    env = st.selectbox("Source environment", ENV_ORDER, format_func=lambda e: ENV_LABELS[e], key="exp_env")
    client_id = st.number_input("Client ID", min_value=1, step=1, value=1, key="exp_cid")
    fmt = st.radio("Format", ["JSON", "SQL INSERTs"], horizontal=True, key="exp_fmt")

    if not st.button("Generate Export", type="primary", key="exp_run"):
        return

    try:
        conn = get_connection(env)
        tables = discover_related_tables(conn)
    except Exception as e:
        st.error(f"Connection error: {e}")
        return

    all_data: dict[str, list[dict]] = {}
    for info in tables:
        try:
            rows = read_client_data(info.name, info.client_id_column, client_id, conn)
            if rows:
                all_data[info.name] = rows
        except Exception:
            pass
    conn.close()

    if not all_data:
        st.warning(f"No data found for client {client_id} in {ENV_LABELS[env]}.")
        return

    total = sum(len(v) for v in all_data.values())
    st.success(f"Found {total} rows across {len(all_data)} tables.")

    if fmt == "JSON":
        output = json.dumps(all_data, default=str, indent=2)
        st.download_button(
            "Download JSON", data=output,
            file_name=f"client_{client_id}_{env}.json", mime="application/json",
        )
    else:
        lines = []
        for table, rows in all_data.items():
            lines.append(f"-- {table} ({len(rows)} rows)")
            for row in rows:
                cols = ", ".join(f"`{c}`" for c in row.keys())
                vals = ", ".join(_sql_lit(v) for v in row.values())
                lines.append(f"INSERT INTO `{table}` ({cols}) VALUES ({vals});")
            lines.append("")
        output = "\n".join(lines)
        st.download_button(
            "Download SQL", data=output,
            file_name=f"client_{client_id}_{env}.sql", mime="text/plain",
        )

    with st.expander("Preview (first 20 rows per table)"):
        for table, rows in all_data.items():
            st.markdown(f"**{table}**")
            st.dataframe(pd.DataFrame(rows[:20]), use_container_width=True, hide_index=True)


def _sql_lit(val) -> str:
    if val is None:
        return "NULL"
    if isinstance(val, (int, float)):
        return str(val)
    escaped = str(val).replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


def _safe_json(raw: str, default):
    if not raw or not raw.strip():
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return default
