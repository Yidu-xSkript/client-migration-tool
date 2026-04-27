# ui/migrate.py — Single-client migration tab (Dev→QA and QA→Prod)

from __future__ import annotations
import os
import time
import json
import pandas as pd
import streamlit as st

from db.connection import get_connection
from db.discovery import discover_related_tables
from db.operations import (
    search_clients, get_client_by_id, get_table_columns, update_rows,
    sample_client_data, get_row_count,
)
from migration.engine import dry_run, run_migration, DryRunResult
from migration.backup import create_backups
from migration.validation import (
    run_pre_checks, PRE_CHECKS, validate_row_filter,
)
from migration import audit
from config import ENV_LABELS, CLIENT_TABLE




def render_migration_tab(src_env: str, dst_env: str) -> None:
    src_label = ENV_LABELS[src_env]
    dst_label = ENV_LABELS[dst_env]
    is_prod = dst_env == "prod"
    kp = f"{src_env}_{dst_env}"   # Key prefix for all widget keys

    if is_prod:
        st.error(
            f"**Production Migration** — You are about to modify **{dst_label}**. "
            "This is irreversible without a backup. Proceed with care."
        )
    else:
        st.header(f"Migrate {src_label} → {dst_label}")
        st.caption(
            "All changes are atomic — any failure rolls back the entire migration."
        )

    # =========================================================================
    # Step 1 — Client selection
    # =========================================================================
    st.subheader("1. Select Client")
    client_id = _client_selector(src_env, kp)
    if client_id is None:
        return

    # =========================================================================
    # Step 2 — Table discovery
    # =========================================================================
    st.subheader("2. Discover Affected Tables")
    if st.button("Discover Tables", key=f"discover_{kp}"):
        _do_discover(client_id, src_env, kp)

    disc_key = f"discovered_{kp}"
    if disc_key not in st.session_state:
        st.info("Click **Discover Tables** to continue.")
        return

    tables_all = st.session_state[disc_key]
    table_names = [t.name for t in tables_all]

    selected_names = st.multiselect(
        "Tables to migrate (all selected by default)",
        options=table_names,
        default=table_names,
        key=f"sel_tables_{kp}",
    )
    tables_selected = [t for t in tables_all if t.name in selected_names]

    if not tables_selected:
        st.warning("Select at least one table.")
        return

    # =========================================================================
    # Step 3 — Options
    # =========================================================================
    st.subheader("3. Migration Options")

    col_o1, col_o2, col_o3, col_o4 = st.columns(4)
    dry_run_mode  = col_o1.checkbox("Preview only (Dry Run)", value=True, key=f"dry_{kp}")
    do_backup     = col_o2.checkbox("Create backup",          value=True, key=f"bkp_{kp}")
    delta_mode    = col_o3.checkbox("Delta mode",             value=False, key=f"delta_{kp}",
                                    help="Only migrate rows that changed since last sync.")
    post_validate = col_o4.checkbox("Post-migration check",  value=True, key=f"postval_{kp}",
                                    help="Verify row counts match after migration.")

    conflict_mode = st.radio(
        "Conflict resolution",
        options=["replace", "skip", "update"],
        format_func=lambda x: {
            "replace": "Replace (DELETE then INSERT)",
            "skip":    "Skip if exists (INSERT IGNORE)",
            "update":  "Update existing (UPSERT)",
        }[x],
        index=0,
        horizontal=True,
        key=f"conflict_{kp}",
        disabled=delta_mode,   # Delta handles conflicts internally
    )

    # -------------------------------------------------------------------------
    # Pre-migration validation rules
    # -------------------------------------------------------------------------
    with st.expander("Pre-Migration Validation Rules"):
        st.caption("Checks that must pass before the migration runs.")
        pre_check_names = st.multiselect(
            "Active checks",
            options=list(PRE_CHECKS.keys()),
            default=["source_has_data", "row_count_positive"],
            format_func=lambda k: PRE_CHECKS[k],
            key=f"pre_checks_{kp}",
        )
        custom_sql = st.text_input(
            "Custom SQL check (must return COUNT = 0 to pass)",
            key=f"custom_sql_{kp}",
            placeholder="SELECT COUNT(*) FROM ClientOrder WHERE ClientId = %s AND status = 'PENDING'",
        )

    # -------------------------------------------------------------------------
    # Column exclusions & row filters
    # -------------------------------------------------------------------------
    excluded_columns: dict[str, list[str]] = {}
    row_filters: dict[str, str] = {}

    with st.expander("Advanced: Column Exclusions & Row Filters"):
        st.warning(
            "⚠️ Admin use only. Row filters execute raw SQL WHERE conditions. "
            "Never enter user-supplied input here."
        )

        # Load column lists lazily from the source DB
        col_map = _load_column_map(tables_selected, src_env, kp)

        for info in tables_selected:
            col1, col2 = st.columns(2)

            available_cols = col_map.get(info.name, [])
            excl = col1.multiselect(
                f"`{info.name}` — exclude columns",
                options=available_cols,
                key=f"excl_{kp}_{info.name}",
            )
            if excl:
                excluded_columns[info.name] = excl

            row_filter = col2.text_input(
                f"`{info.name}` — row filter (SQL WHERE)",
                key=f"filter_{kp}_{info.name}",
                placeholder="is_deleted = 0",
            )
            if row_filter.strip():
                ok, err = validate_row_filter(row_filter)
                if not ok:
                    col2.error(err)
                else:
                    row_filters[info.name] = row_filter

    ticket = ""
    if is_prod:
        ticket = st.text_input(
            "Reference / Ticket number (optional)",
            key=f"ticket_{kp}",
            placeholder="JIRA-1234",
        )

    # =========================================================================
    # Step 4 — Pre-migration validation
    # =========================================================================
    if pre_check_names and st.button("Run Pre-Migration Checks", key=f"precheck_{kp}"):
        _run_pre_checks_ui(client_id, tables_selected, src_env, dst_env, pre_check_names, custom_sql)

    precheck_key = f"precheck_result_{kp}"
    if precheck_key in st.session_state:
        _render_validation_result(st.session_state[precheck_key])

    # Context bundle passed into preview cards so each table's Migrate button has everything it needs
    migration_ctx = {
        "client_id":       client_id,
        "tables_selected": tables_selected,
        "src_env":         src_env,
        "dst_env":         dst_env,
        "conflict_mode":   conflict_mode,
        "delta_mode":      delta_mode,
        "do_backup":       do_backup,
        "post_validate":   post_validate,
        "excluded_columns": excluded_columns,
        "row_filters":     row_filters,
        "ticket":          ticket,
        "kp":              kp,
        "is_prod":         is_prod,
    }

    # =========================================================================
    # Step 5 — Preview
    # =========================================================================
    st.subheader("4. Preview")
    if st.button("Preview Migration", key=f"preview_{kp}"):
        _do_dry_run(client_id, tables_selected, src_env, dst_env, conflict_mode, delta_mode, kp)

    preview_key = f"preview_{kp}_result"
    if preview_key in st.session_state:
        _render_dry_run(st.session_state[preview_key], delta_mode, migration_ctx)

    # =========================================================================
    # Step 6 — Confirmation
    # =========================================================================
    st.subheader("5. Confirm & Execute")
    confirmed = _render_confirmation(client_id, src_label, dst_label, is_prod, kp)

    btn_label = (
        f"DRY RUN — {src_label} → {dst_label}"
        if dry_run_mode
        else f"Migrate {src_label} → {dst_label}"
    )
    execute = st.button(
        btn_label,
        type="primary",
        disabled=not confirmed,
        use_container_width=True,
        key=f"exec_{kp}",
    )

    if not execute or not confirmed:
        return

    if dry_run_mode:
        _do_dry_run(client_id, tables_selected, src_env, dst_env, conflict_mode, delta_mode, kp)
        result = st.session_state.get(f"preview_{kp}_result")
        if result:
            st.subheader("Dry Run Results")
            _render_dry_run(result, delta_mode, migration_ctx)
        st.info("No data was written. Uncheck **Preview only (Dry Run)** above and click the button again to run the real migration.")
    else:
        full_payload = _do_migration(
            client_id=client_id,
            tables=tables_selected,
            src_env=src_env,
            dst_env=dst_env,
            conflict_mode=conflict_mode,
            delta_mode=delta_mode,
            do_backup=do_backup,
            post_validate=post_validate,
            excluded_columns=excluded_columns,
            row_filters=row_filters,
            ticket=ticket,
        )
        if full_payload is not None:
            full_result, full_backups = full_payload
            _render_migration_summary(full_result)
            if full_result.post_validation:
                st.subheader("Post-Migration Integrity Check")
                _render_validation_result(full_result.post_validation)
            if full_backups:
                dst_label_b = ENV_LABELS[dst_env]
                st.info(
                    f"**{len(full_backups)} backup file(s)** saved to `backups/{dst_env}/`.  "
                    "Find and restore them under **Settings → Backup Manager**."
                )
                with st.expander("Backup filenames", expanded=False):
                    for bt in full_backups:
                        st.code(os.path.basename(bt))


# ---------------------------------------------------------------------------
# Client selector
# ---------------------------------------------------------------------------

def _client_selector(src_env: str, kp: str) -> int | None:
    method = st.radio(
        "Find client by:",
        ["Client ID", "Search (name / email / company)"],
        horizontal=True,
        key=f"method_{kp}",
    )

    if method == "Client ID":
        return int(st.number_input("Client ID", min_value=1, step=1, value=1, key=f"cid_{kp}"))

    query = st.text_input("Search term", key=f"search_{kp}", placeholder="Acme Corp")
    if not query or len(query) < 2:
        st.info("Enter at least 2 characters to search.")
        return None

    search_cache_key = f"_search_{src_env}_{query}"
    if search_cache_key not in st.session_state:
        try:
            conn = get_connection(src_env)
            st.session_state[search_cache_key] = search_clients(query, conn)
        except Exception as e:
            st.warning(f"Search failed: {e}")
            return None
    results = st.session_state[search_cache_key]

    if not results:
        st.warning("No clients found.")
        return None

    def label_row(row):
        parts = [str(row[k]) for k in ("Name", "ClientName", "Company", "Email") if row.get(k)]
        return f"[{row.get('ClientId', '?')}] " + " — ".join(parts[:2])

    opts = {label_row(r): r["ClientId"] for r in results}
    chosen = st.selectbox("Select client", list(opts.keys()), key=f"sel_{kp}")
    return opts.get(chosen)


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def _do_discover(client_id: int, src_env: str, kp: str) -> None:
    disc_key  = f"discovered_{kp}"
    # Share discovery across tabs for the same source env — INFORMATION_SCHEMA is slow
    share_key = f"_discovered_schema_{src_env}"
    try:
        with st.spinner("Discovering tables…"):
            if share_key not in st.session_state:
                conn = get_connection(src_env)
                st.session_state[share_key] = discover_related_tables(conn)
            tables = st.session_state[share_key]
        st.session_state[disc_key] = tables
        st.success(f"Discovered {len(tables)} related table(s).")
    except Exception as e:
        st.error(f"Discovery failed: {e}")


# ---------------------------------------------------------------------------
# Column map (cached per session)
# ---------------------------------------------------------------------------

def _load_column_map(tables, src_env: str, kp: str) -> dict[str, list[str]]:
    cache_key = f"col_map_{kp}"
    if cache_key not in st.session_state:
        col_map = {}
        try:
            conn = get_connection(src_env)
            for info in tables:
                col_map[info.name] = get_table_columns(info.name, conn)
            conn.close()
        except Exception:
            pass
        st.session_state[cache_key] = col_map
    return st.session_state[cache_key]


# ---------------------------------------------------------------------------
# Pre-checks
# ---------------------------------------------------------------------------

def _run_pre_checks_ui(client_id, tables, src_env, dst_env, check_names, custom_sql, kp=None) -> None:
    # kp is optional because this can also be called from the button handler
    # We store the result in a key derived from src/dst
    key_prefix = f"{src_env}_{dst_env}"
    precheck_key = f"precheck_result_{key_prefix}"
    try:
        with st.spinner("Running pre-migration checks…"):
            src_conn = get_connection(src_env)
            dst_conn = get_connection(dst_env)
            result = run_pre_checks(client_id, tables, src_conn, dst_conn, check_names, custom_sql)
            src_conn.close()
            dst_conn.close()
        st.session_state[precheck_key] = result
    except Exception as e:
        st.error(f"Pre-check error: {e}")


def _render_validation_result(result) -> None:
    for chk in result.checks:
        if chk.passed:
            st.success(f"✓ {chk.name}: {chk.message}")
        else:
            st.error(f"✗ {chk.name}: {chk.message}")


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------

def _do_dry_run(client_id, tables, src_env, dst_env, conflict_mode, delta_mode, kp) -> None:
    preview_key = f"preview_{kp}_result"
    try:
        with st.spinner("Running preview…"):
            src_conn = get_connection(src_env)
            dst_conn = get_connection(dst_env)
            result = dry_run(
                client_id=client_id, tables=tables,
                src_conn=src_conn, dst_conn=dst_conn,
                source_env=src_env, target_env=dst_env,
                conflict_mode=conflict_mode, delta_mode=delta_mode,
            )
            src_conn.close()
            dst_conn.close()
        st.session_state[preview_key] = result
    except Exception as e:
        st.error(f"Preview failed: {e}")


def _render_dry_run(result: DryRunResult, delta_mode: bool, migration_ctx: dict | None = None) -> None:
    from config import ENV_LABELS, PREVIEW_ROW_SAMPLE
    src_label = ENV_LABELS.get(result.source_env, result.source_env)
    dst_label = ENV_LABELS.get(result.target_env, result.target_env)

    normal  = [t for t in result.tables if not t.missing_in]
    missing = [t for t in result.tables if t.missing_in]

    # -------------------------------------------------------------------------
    # Summary bar
    # -------------------------------------------------------------------------
    if normal:
        total_src = sum(t.src_rows for t in normal)
        total_dst = sum(t.dst_rows for t in normal)
        c1, c2, c3 = st.columns(3)
        c1.metric(f"Tables to Migrate", len(normal))
        c2.metric(f"Rows in {src_label} (source)", total_src)
        c3.metric(f"Rows in {dst_label} (dest, current)", total_dst)

    if missing:
        st.warning(f"{len(missing)} table(s) will be **skipped** — see bottom of this preview.")

    if any(t.action == "skip" for t in normal):
        st.caption("Rows that already exist in the destination (by primary key) will be silently skipped.")

    # -------------------------------------------------------------------------
    # Per-table expandable cards
    # -------------------------------------------------------------------------
    for t in normal:
        _render_table_preview_card(t, src_label, dst_label, delta_mode, PREVIEW_ROW_SAMPLE, migration_ctx)

    # -------------------------------------------------------------------------
    # Missing tables section
    # -------------------------------------------------------------------------
    if missing:
        st.divider()
        st.markdown("#### ⚠️ Skipped Tables (schema mismatch between environments)")
        for t in missing:
            where = {"dst": dst_label, "src": src_label, "both": "both environments"}.get(t.missing_in, t.missing_in)
            st.error(f"**`{t.table}`** — does not exist in **{where}**. No data will be migrated for this table.")


def _fmt_num(n: int, *, red: bool = False) -> str:
    """Return n formatted with commas. Red when counts mismatch, bold-yellow when > 20."""
    s = f"{n:,}"
    if red:
        return f"<b style='color:#FF4B4B'>{s}</b>"
    if n > 20:
        return f"<b style='color:#F4C430'>{s}</b>"
    return s


@st.fragment
def _render_table_preview_card(t, src_label: str, dst_label: str, delta_mode: bool, sample_limit: int, migration_ctx: dict | None = None) -> None:
    """Render one expandable card showing a full before/after breakdown for a single table."""

    mismatch = t.src_rows != t.dst_rows   # both numbers go red when counts differ

    if delta_mode:
        has_changes = bool(t.delta_insert or t.delta_update or t.delta_delete)
        icon = "✓" if not has_changes else "📋"
        changes = (
            f"+{_fmt_num(t.delta_insert, red=t.delta_insert > 0)} add  ·  "
            f"~{_fmt_num(t.delta_update, red=t.delta_update > 0)} change  ·  "
            f"-{_fmt_num(t.delta_delete, red=t.delta_delete > 0)} remove"
        )
        html_header = (
            f"{icon} <b><code>{t.table}</code></b>  —  {changes}  "
            f"(src: {_fmt_num(t.src_rows)} rows  ·  dest: {_fmt_num(t.dst_rows)} rows)"
        )
    elif t.action == "replace":
        icon = "📋"
        html_header = (
            f"{icon} <b><code>{t.table}</code></b>  —  "
            f"Delete {_fmt_num(t.dst_rows, red=mismatch)} current  →  "
            f"Insert {_fmt_num(t.src_rows, red=mismatch)} from source"
        )
    elif t.action == "skip":
        icon = "📋"
        html_header = (
            f"{icon} <b><code>{t.table}</code></b>  —  "
            f"INSERT IGNORE {_fmt_num(t.src_rows, red=mismatch)} rows  ·  "
            f"{_fmt_num(t.dst_rows, red=mismatch)} currently in dest"
        )
    elif t.action == "update":
        icon = "📋"
        html_header = (
            f"{icon} <b><code>{t.table}</code></b>  —  "
            f"Upsert {_fmt_num(t.src_rows, red=mismatch)} rows  ·  "
            f"{_fmt_num(t.dst_rows, red=mismatch)} currently in dest"
        )
    else:
        icon = "📋"
        html_header = (
            f"{icon} <b><code>{t.table}</code></b>  —  "
            f"{_fmt_num(t.src_rows, red=mismatch)} src rows  ·  "
            f"{_fmt_num(t.dst_rows, red=mismatch)} dest rows"
        )

    auto_expand = 0 < t.src_rows <= 5

    pending_key = f"pending_migrate_{migration_ctx['kp']}_{t.table}" if migration_ctx else ""
    is_pending  = bool(migration_ctx and st.session_state.get(pending_key))

    with st.container(border=True):
        if is_pending:
            # ── Inline confirmation banner (no popup needed) ───────────────────
            dst_label_ctx = ENV_LABELS.get(migration_ctx["dst_env"], migration_ctx["dst_env"])
            st.warning(
                f"Migrate **`{t.table}`** → **{dst_label_ctx}**?  "
                f"**{t.src_rows:,}** row(s) will be written. A backup is created first."
            )
            col_y, col_n, _ = st.columns([1, 1, 7])
            if col_y.button("✓ Confirm", type="primary",
                            key=f"confirm_yes_{pending_key}", use_container_width=True):
                st.session_state.pop(pending_key, None)
                st.session_state[f"migrate_ok_{migration_ctx['kp']}_{t.table}"] = True
            if col_n.button("✗ Cancel",
                            key=f"confirm_no_{pending_key}", use_container_width=True):
                st.session_state.pop(pending_key, None)
        else:
            # ── Normal header row ─────────────────────────────────────────────
            if migration_ctx:
                col_hdr, col_btn = st.columns([11, 1], gap="small")
                with col_hdr:
                    st.markdown(html_header, unsafe_allow_html=True)
                with col_btn:
                    _render_inline_migrate_button(t, migration_ctx)
            else:
                st.markdown(html_header, unsafe_allow_html=True)

        # Body expander always visible
        with st.expander("Data", expanded=auto_expand):
            if delta_mode:
                _render_delta_card_body(t, src_label, dst_label)
            else:
                _render_full_card_body(t, src_label, dst_label, sample_limit, migration_ctx)

    # ── Migration output renders here — full width, below the card ────────────
    if migration_ctx:
        _maybe_run_table_migration(t, migration_ctx)


def _nan_to_none(records: list[dict]) -> list[dict]:
    """Replace float NaN values (from st.data_editor) with None for MySQL compatibility."""
    import math
    return [
        {k: (None if isinstance(v, float) and math.isnan(v) else v) for k, v in row.items()}
        for row in records
    ]


def _do_save_inline(
    tbl:             str,
    env:             str,
    env_label:       str,
    client_id:       int,
    orig_records:    list[dict],
    edited_records:  list[dict],
    tables_selected: list,
    msg_key:         str,
) -> None:
    """
    Compare edited_records against orig_records, back up changed rows, UPDATE the DB,
    and append an entry to edit_audit.log.  Stores (level, message) in session_state[msg_key].
    """
    from migration.delta import get_primary_keys
    from migration.backup import create_backups

    # st.data_editor returns NaN for empty cells — MySQL only accepts None/NULL
    orig_records   = _nan_to_none(orig_records)
    edited_records = _nan_to_none(edited_records)

    changed_rows  = []
    audit_changes = []
    for orig, edited in zip(orig_records, edited_records):
        if orig == edited:
            continue
        changed_rows.append(edited)
        diff_cols = [k for k in edited if orig.get(k) != edited.get(k)]
        audit_changes.append({
            "before": {k: orig.get(k)   for k in diff_cols},
            "after":  {k: edited.get(k) for k in diff_cols},
        })

    if not changed_rows:
        st.session_state[msg_key] = ("info", "No changes detected.")
        return

    try:
        conn    = get_connection(env)
        pk_cols = get_primary_keys(tbl, conn)
        if not pk_cols:
            conn.close()
            st.session_state[msg_key] = (
                "error",
                f"Cannot save `{tbl}`: no primary key found.",
            )
            return

        # Backup before touching anything
        table_info = next((ti for ti in tables_selected if ti.name == tbl), None)
        if table_info:
            created     = create_backups(client_id, [table_info], conn, env=env)
            backup_name = created[0] if created else ""
        else:
            backup_name = ""

        # Attach PK values to audit entries
        for row, ac in zip(changed_rows, audit_changes):
            ac["pk"] = {k: row[k] for k in pk_cols if k in row}

        n = update_rows(tbl, changed_rows, pk_cols, conn)
        conn.close()

        audit.log_edit(
            env          = env,
            table        = tbl,
            client_id    = client_id,
            changes      = audit_changes,
            backup_table = backup_name,
        )

        backup_note = f" Backup: `{backup_name}`." if backup_name else ""
        st.session_state[msg_key] = (
            "success",
            f"Saved {n} row(s) to {env_label}.{backup_note} Edit logged.",
        )
    except Exception as e:
        st.session_state[msg_key] = ("error", f"Save failed: {e}")


def _render_inline_migrate_button(t, ctx: dict) -> None:
    """Compact Migrate button — sets a pending flag so the card swaps to an inline confirmation."""
    kp          = ctx["kp"]
    is_prod     = ctx["is_prod"]
    tbl         = t.table
    btn_key     = f"migrate_tbl_{kp}_{tbl}"
    pending_key = f"pending_migrate_{kp}_{tbl}"

    st.markdown("<div style='margin-top:0.1rem'></div>", unsafe_allow_html=True)

    if is_prod:
        chk_key   = f"confirm_tbl_{kp}_{tbl}"
        confirmed = st.checkbox("✔", key=chk_key, help=f"Confirm migrating `{tbl}` to Production")
        clicked   = st.button("Migrate", key=btn_key, type="primary",
                              disabled=not confirmed, use_container_width=True)
    else:
        clicked = st.button("Migrate", key=btn_key, type="primary", use_container_width=True)

    if clicked:
        st.session_state[pending_key] = True


def _maybe_run_table_migration(t, ctx: dict) -> None:
    """
    Check whether the user confirmed a per-table migration (set by the dialog)
    and execute it at full page width — called OUTSIDE any column context so
    the status box, summary, and validation all render normally.
    After the migration finishes (success or failure), refresh the table's
    preview samples so the card immediately shows the current DB state.
    """
    tbl         = t.table
    kp          = ctx["kp"]
    confirm_key = f"migrate_ok_{kp}_{tbl}"

    if not st.session_state.pop(confirm_key, False):
        return

    table_info = next((ti for ti in ctx["tables_selected"] if ti.name == tbl), None)
    if table_info is None:
        st.error(f"TableInfo for `{tbl}` not found — re-run discovery and try again.")
        return

    result = _do_migration(
        client_id        = ctx["client_id"],
        tables           = [table_info],
        src_env          = ctx["src_env"],
        dst_env          = ctx["dst_env"],
        conflict_mode    = ctx["conflict_mode"],
        delta_mode       = ctx["delta_mode"],
        do_backup        = True,
        post_validate    = ctx["post_validate"],
        excluded_columns = ctx["excluded_columns"],
        row_filters      = ctx["row_filters"],
        ticket           = ctx["ticket"],
    )

    if result is not None:
        result_payload, backup_tables = result
        st.session_state[f"migration_result_{kp}_{tbl}"]  = result_payload
        st.session_state[f"migration_backups_{kp}_{tbl}"] = backup_tables
    else:
        result_payload = None
        backup_tables  = []

    # Refresh preview so the card shows the live post-migration state
    _refresh_table_preview(tbl, table_info, ctx)

    # ── Inline summary (no popup) ─────────────────────────────────────────────
    if result_payload is not None:
        _render_inline_migration_summary(result_payload, backup_tables, kp, tbl)


def _refresh_table_preview(tbl: str, table_info, ctx: dict) -> None:
    """
    Re-fetch src_sample, dst_sample, src_rows, dst_rows for one table and
    patch the cached DryRunResult in session state.  Also computes the diff
    between old and new dst_sample and stores it so _maybe_run_table_migration
    can display what actually changed.
    """
    from config import PREVIEW_ROW_SAMPLE

    kp          = ctx["kp"]
    preview_key = f"preview_{kp}_result"

    if preview_key not in st.session_state:
        return

    cached = st.session_state[preview_key]
    entry  = next((e for e in cached.tables if e.table == tbl), None)
    if entry is None:
        return

    try:
        src_conn = get_connection(ctx["src_env"])
        dst_conn = get_connection(ctx["dst_env"])
        col      = table_info.client_id_column
        cid      = ctx["client_id"]

        old_dst_sample = list(entry.dst_sample)

        entry.src_sample = sample_client_data(tbl, col, cid, src_conn, PREVIEW_ROW_SAMPLE)
        entry.dst_sample = sample_client_data(tbl, col, cid, dst_conn, PREVIEW_ROW_SAMPLE)
        entry.src_rows   = get_row_count(tbl, col, cid, src_conn)
        entry.dst_rows   = get_row_count(tbl, col, cid, dst_conn)
        entry.migrated   = True

        src_conn.close()
        dst_conn.close()

        # Compute row-level diff between old and new destination sample
        old_set = [json.dumps(r, sort_keys=True, default=str) for r in old_dst_sample]
        new_set = [json.dumps(r, sort_keys=True, default=str) for r in entry.dst_sample]
        old_keys = set(old_set)
        new_keys = set(new_set)

        added_rows   = [entry.dst_sample[i] for i, k in enumerate(new_set) if k not in old_keys]
        removed_rows = [old_dst_sample[i]   for i, k in enumerate(old_set) if k not in new_keys]

        delta_key = f"migration_delta_{kp}_{tbl}"
        st.session_state[delta_key] = {"added": added_rows, "removed": removed_rows}

    except Exception as e:
        st.warning(f"Could not refresh preview for `{tbl}`: {e}")


def _scroll_sync_js(uid: str) -> str:
    """Return an HTML+JS snippet that syncs horizontal scroll between two dataframe iframes."""
    return f"""
    <script>
    (function() {{
        var srcId = 'scr-src-{uid}';
        var dstId = 'scr-dst-{uid}';

        function findNextIframe(markerId) {{
            var marker = window.parent.document.getElementById(markerId);
            if (!marker) return null;
            var iframes = window.parent.document.querySelectorAll('iframe');
            for (var i = 0; i < iframes.length; i++) {{
                if (marker.compareDocumentPosition(iframes[i]) & Node.DOCUMENT_POSITION_FOLLOWING)
                    return iframes[i];
            }}
            return null;
        }}

        function getScroller(iframe) {{
            try {{
                var doc = iframe.contentDocument || iframe.contentWindow.document;
                return doc.querySelector('.dvn-scroller') || doc.body;
            }} catch(e) {{ return null; }}
        }}

        var attempts = 0;
        function setup() {{
            var sf = findNextIframe(srcId);
            var df = findNextIframe(dstId);
            if (!sf || !df) {{ if (++attempts < 25) setTimeout(setup, 300); return; }}
            var ss = getScroller(sf);
            var ds = getScroller(df);
            if (!ss || !ds) {{ if (++attempts < 25) setTimeout(setup, 300); return; }}
            var busy = false;
            ss.addEventListener('scroll', function() {{
                if (busy) return; busy = true; ds.scrollLeft = ss.scrollLeft; busy = false;
            }});
            ds.addEventListener('scroll', function() {{
                if (busy) return; busy = true; ss.scrollLeft = ds.scrollLeft; busy = false;
            }});
        }}
        setup();
    }})();
    </script>
    """


def _render_full_card_body(
    t,
    src_label: str,
    dst_label: str,
    sample_limit: int,
    migration_ctx: dict | None = None,
) -> None:
    """
    Card body for replace / skip / update modes — two side-by-side tables whose
    horizontal scroll positions are kept in sync via a small JS snippet.
    When migration_ctx is present the tables are editable with Save buttons.
    """
    import streamlit.components.v1 as components

    action = t.action
    kp     = migration_ctx["kp"] if migration_ctx else "ro"
    tbl    = t.table
    uid    = f"{kp}_{tbl}".replace(" ", "_").replace("-", "_")

    if getattr(t, "migrated", False):
        # Post-migration: use neutral past-tense labels
        src_header = f"##### Source — {src_label} ({t.src_rows:,} rows)"
        dst_header = f"##### Destination — current state in {dst_label} ({t.dst_rows:,} rows)"
    else:
        HEADERS = {
            "replace": (
                f"##### ➕ Will write to dest ({t.src_rows} rows from {src_label})",
                f"##### 🗑️ Will delete from dest ({t.dst_rows} current rows in {dst_label})",
            ),
            "skip": (
                f"##### ➕ Will attempt to insert ({t.src_rows} rows from {src_label})",
                f"##### ⏭️ Currently in dest ({t.dst_rows} rows — kept as-is)",
            ),
            "update": (
                f"##### ✏️ Will upsert ({t.src_rows} rows from {src_label})",
                f"##### 📄 Currently in dest ({t.dst_rows} rows)",
            ),
        }
        src_header, dst_header = HEADERS.get(action, (
            f"##### Source ({t.src_rows} rows)",
            f"##### Dest ({t.dst_rows} rows)",
        ))

    col_src, col_dst = st.columns(2)

    # ── Source column ─────────────────────────────────────────────────────────
    with col_src:
        st.markdown(src_header)
        if action == "update":
            st.caption("Existing rows will be updated; new rows will be inserted.")
        # Anchor used by the scroll-sync script to locate the next iframe
        st.markdown(f'<span id="scr-src-{uid}"></span>', unsafe_allow_html=True)
        if t.src_sample:
            if migration_ctx:
                src_ed = st.data_editor(
                    pd.DataFrame(t.src_sample),
                    key=f"edit_{kp}_{tbl}_src",
                    use_container_width=True, hide_index=True, num_rows="fixed",
                )
                if t.src_rows > sample_limit:
                    st.caption(f"Showing {sample_limit} of {t.src_rows:,} rows.")
                if st.button("💾 Save Source", key=f"save_src_{kp}_{tbl}",
                             use_container_width=True):
                    _do_save_inline(tbl, migration_ctx["src_env"], src_label,
                                    migration_ctx["client_id"], t.src_sample,
                                    src_ed.to_dict("records"),
                                    migration_ctx["tables_selected"],
                                    f"save_msg_src_{kp}_{tbl}")
                msg_key = f"save_msg_src_{kp}_{tbl}"
                if msg_key in st.session_state:
                    lvl, txt = st.session_state[msg_key]; getattr(st, lvl)(txt)
            else:
                st.dataframe(pd.DataFrame(t.src_sample), use_container_width=True, hide_index=True)
                if t.src_rows > sample_limit:
                    st.caption(f"Showing {sample_limit} of {t.src_rows} rows.")
        else:
            st.info("No rows for this client in source.")

    # ── Destination column ────────────────────────────────────────────────────
    with col_dst:
        st.markdown(dst_header)
        st.markdown(f'<span id="scr-dst-{uid}"></span>', unsafe_allow_html=True)
        if t.dst_sample:
            if migration_ctx:
                dst_ed = st.data_editor(
                    pd.DataFrame(t.dst_sample),
                    key=f"edit_{kp}_{tbl}_dst",
                    use_container_width=True, hide_index=True, num_rows="fixed",
                )
                if t.dst_rows > sample_limit:
                    st.caption(f"Showing {sample_limit} of {t.dst_rows:,} rows.")
                if st.button("💾 Save Destination", key=f"save_dst_{kp}_{tbl}",
                             use_container_width=True):
                    _do_save_inline(tbl, migration_ctx["dst_env"], dst_label,
                                    migration_ctx["client_id"], t.dst_sample,
                                    dst_ed.to_dict("records"),
                                    migration_ctx["tables_selected"],
                                    f"save_msg_dst_{kp}_{tbl}")
                msg_key = f"save_msg_dst_{kp}_{tbl}"
                if msg_key in st.session_state:
                    lvl, txt = st.session_state[msg_key]; getattr(st, lvl)(txt)
            else:
                st.dataframe(pd.DataFrame(t.dst_sample), use_container_width=True, hide_index=True)
                if t.dst_rows > sample_limit:
                    st.caption(f"Showing {sample_limit} of {t.dst_rows} rows.")
        else:
            no_dst = {
                "replace": "Nothing to delete — destination is already empty for this client.",
                "skip":    "Destination is empty for this client.",
                "update":  "Destination is empty.",
            }
            st.info(no_dst.get(action, "Destination is empty."))

    # Inject scroll sync — invisible 0-height iframe that runs the JS
    components.html(_scroll_sync_js(uid), height=0)


def _render_delta_card_body(t, src_label: str, dst_label: str) -> None:
    """Card body for delta mode — shows exactly which rows change."""
    has_any = t.delta_insert or t.delta_update or t.delta_delete

    if not has_any:
        st.success("✓ No changes — source and destination are identical for this client.")
        return

    if t.delta_insert_rows:
        st.markdown(f"##### ➕ New rows to add ({t.delta_insert} rows)")
        st.dataframe(pd.DataFrame(t.delta_insert_rows), use_container_width=True, hide_index=True)
        if t.delta_insert > len(t.delta_insert_rows):
            st.caption(f"Showing {len(t.delta_insert_rows)} of {t.delta_insert} rows.")

    if t.delta_update_rows:
        st.markdown(f"##### ✏️ Changed rows to update ({t.delta_update} rows)")
        st.caption("These rows exist in both environments but their content differs.")
        st.dataframe(pd.DataFrame(t.delta_update_rows), use_container_width=True, hide_index=True)
        if t.delta_update > len(t.delta_update_rows):
            st.caption(f"Showing {len(t.delta_update_rows)} of {t.delta_update} rows.")

    if t.delta_delete_rows:
        st.markdown(f"##### 🗑️ Rows to remove from dest ({t.delta_delete} rows)")
        st.caption("These rows exist in the destination but no longer exist in the source.")
        # delta_delete_rows are PK tuples — show them as a simple list
        st.dataframe(
            pd.DataFrame(t.delta_delete_rows, columns=[f"PK ({i+1})" for i in range(len(t.delta_delete_rows[0]))])
            if t.delta_delete_rows and isinstance(t.delta_delete_rows[0], tuple)
            else pd.DataFrame({"Deleted PK": t.delta_delete_rows}),
            use_container_width=True, hide_index=True,
        )
        if t.delta_delete > len(t.delta_delete_rows):
            st.caption(f"Showing {len(t.delta_delete_rows)} of {t.delta_delete} rows.")


# ---------------------------------------------------------------------------
# Confirmation gate
# ---------------------------------------------------------------------------

def _render_confirmation(client_id, src_label, dst_label, is_prod, kp) -> bool:
    if not is_prod:
        return st.checkbox(
            f"Confirm migration of client **{client_id}**: {src_label} → {dst_label}",
            key=f"confirm_{kp}",
        )

    st.warning("**Three-step confirmation required for Production.**")
    typed_id   = st.text_input(f"Type the Client ID ({client_id}):", key=f"confirm_id_{kp}")
    typed_prod = st.text_input('Type "PROD":',                        key=f"confirm_prod_{kp}")
    final      = st.checkbox("I understand this modifies Production data.", key=f"confirm_final_{kp}")

    if typed_id and typed_id.strip() != str(client_id):
        st.error("Client ID does not match.")
    if typed_prod and typed_prod.strip() != "PROD":
        st.error('Must type "PROD" exactly.')

    return typed_id.strip() == str(client_id) and typed_prod.strip() == "PROD" and final


# ---------------------------------------------------------------------------
# Live migration
# ---------------------------------------------------------------------------

def _do_migration(
    client_id, tables, src_env, dst_env,
    conflict_mode, delta_mode, do_backup, post_validate,
    excluded_columns, row_filters, ticket,
):
    src_label = ENV_LABELS[src_env]
    dst_label = ENV_LABELS[dst_env]

    n_tables    = len(tables)
    # Progress bands: backup=10%, tables=80%, validation=10%
    HAS_BACKUP  = do_backup
    HAS_VALID   = post_validate
    BACKUP_BAND = 0.10 if HAS_BACKUP else 0.0
    TABLE_BAND  = 0.80
    VALID_BAND  = 0.10 if HAS_VALID  else 0.0
    # Remaining fraction fills table band
    TABLE_BAND  = 1.0 - BACKUP_BAND - VALID_BAND

    status_box   = st.status(
        f"Migrating client {client_id}: {src_label} → {dst_label}…",
        expanded=True,
    )
    progress_bar = st.progress(0, text="Starting…")

    backup_tables: list[str] = []
    result = None

    def _set_progress(pct: float, text: str) -> None:
        progress_bar.progress(min(max(pct, 0.0), 1.0), text=text)

    try:
        src_conn = get_connection(src_env)
        dst_conn = get_connection(dst_env)

        if do_backup:
            _set_progress(0.02, "Creating backups…")
            status_box.write("⏳ Creating backups…")
            backup_tables = create_backups(client_id, tables, dst_conn, env=dst_env)
            _set_progress(BACKUP_BAND, f"Backup done ({len(backup_tables)} file(s)). Starting migration…")

        def on_table_start(table_name: str, idx: int, total: int) -> None:
            pct  = BACKUP_BAND + (idx / total) * TABLE_BAND
            text = f"Table {idx + 1} / {total}:  {table_name}"
            _set_progress(pct, text)
            status_box.write(f"⏳ {text}")

        def progress_cb(msg: str, level: str = "info") -> None:
            icon = {"info": "ℹ️", "warning": "⚠️", "error": "❌"}.get(level, "•")
            status_box.write(f"{icon} {msg}")

        result = run_migration(
            client_id=client_id,
            tables=tables,
            src_conn=src_conn,
            dst_conn=dst_conn,
            source_env=src_env,
            target_env=dst_env,
            conflict_mode=conflict_mode,
            delta_mode=delta_mode,
            excluded_columns=excluded_columns,
            row_filters=row_filters,
            post_validate=post_validate,
            progress_callback=progress_cb,
            on_table_start=on_table_start,
        )

        _set_progress(BACKUP_BAND + TABLE_BAND, "Validating…" if HAS_VALID else "Finalising…")

        src_conn.close()
        dst_conn.close()

    except Exception as e:
        st.error(f"Migration failed: {e}")
        audit.log_attempt(audit.make_entry(
            source_env=src_env, target_env=dst_env, client_id=client_id,
            tables=tables, row_counts={}, status="failure",
            error_message=str(e), ticket_number=ticket, backup_tables=backup_tables,
        ))
        return None

    _set_progress(1.0, "Done.")

    if result.success:
        status_box.update(label="Migration complete!", state="complete")
        st.balloons()
        st.toast(
            f"Client {client_id} migrated: {result.total_inserted} rows inserted. "
            "Click 📊 to view the full summary.",
            icon="✅",
        )
    else:
        status_box.update(label="Migration failed — rolled back.", state="error")
        st.error(f"Error: {result.error_message}")

    # Audit
    row_counts = {t.table: t.inserted for t in result.tables}
    audit.log_attempt(audit.make_entry(
        source_env=src_env, target_env=dst_env, client_id=client_id,
        tables=tables, row_counts=row_counts,
        status="success" if result.success else "failure",
        error_message=result.error_message,
        ticket_number=ticket, backup_tables=backup_tables,
    ))

    return result, backup_tables


def _render_inline_migration_summary(result, backup_tables: list, kp: str, tbl: str) -> None:
    """Render the per-table migration result inline (no popup) in an auto-expanded section."""
    label = "✅ Migration Complete" if result.success else "❌ Migration Failed"
    with st.expander(label, expanded=True):
        _render_migration_summary(result)

        if result.post_validation:
            st.markdown("#### Post-Migration Check")
            _render_validation_result(result.post_validation)

        delta = st.session_state.get(f"migration_delta_{kp}_{tbl}", {})
        added   = delta.get("added",   [])
        removed = delta.get("removed", [])
        if added:
            st.markdown("#### Rows added to destination (sampled)")
            st.dataframe(pd.DataFrame(added), use_container_width=True, hide_index=True)
        if removed:
            st.markdown("#### Rows removed from destination (sampled)")
            st.dataframe(pd.DataFrame(removed), use_container_width=True, hide_index=True)

        if backup_tables:
            st.caption(
                "Backup files saved to `backups/{env}/` — restore via **Settings → Backup Manager**."
            )
            for bt in backup_tables:
                st.code(os.path.basename(bt))


def _render_migration_summary(result) -> None:
    migrated = [t for t in result.tables if t.status != "missing"]
    skipped  = [t for t in result.tables if t.status == "missing"]

    st.subheader("Migration Summary")

    if migrated:
        rows = []
        for t in migrated:
            rows.append({
                "Table":    t.table,
                "Mode":     t.mode,
                "Deleted":  t.deleted,
                "Inserted": t.inserted,
                "Updated":  t.updated,
                "Status":   "✓" if t.status == "ok" else f"✗ {t.error}",
            })
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

        col1, col2, col3 = st.columns(3)
        col1.metric("Rows Inserted", result.total_inserted)
        col2.metric("Rows Updated",  result.total_updated)
        col3.metric("Rows Deleted",  result.total_deleted)

    if skipped:
        st.warning(f"**{len(skipped)} table(s) skipped** — not found in destination:")
        st.dataframe(
            pd.DataFrame([
                {"Table": t.table, "Missing In": t.missing_in.capitalize(), "Reason": t.error}
                for t in skipped
            ]),
            use_container_width=True, hide_index=True,
        )
