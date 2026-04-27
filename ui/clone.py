# ui/clone.py — "Copy from Client" tab

from __future__ import annotations
import pandas as pd
import streamlit as st

from db.connection import get_connection
from db.discovery import discover_related_tables
from db.operations import search_clients, get_client_by_id, read_client_data, update_rows
from migration.clone import (
    clone_client,
    delete_cloned_client,
    fetch_cloned_table_rows,
    get_outgoing_fks,
    get_pk_column,
    CLONE_CATALOG,
    CATALOG_BY_TABLE,
    CATALOG_TABLE_NAMES,
    CATALOG_GROUP_LABELS,
)
from config import ENV_LABELS, CLIENT_TABLE, CLIENT_ID_COLUMN


# ---------------------------------------------------------------------------
# Main render
# ---------------------------------------------------------------------------

def render_clone() -> None:
    st.header("Copy from Client")
    st.caption(
        "Duplicate a client's configuration to a new Client ID within Development.  "
        "Approval workflow, invoice config, and admin users are cloned by default.  "
        "Foreign-key records (e.g. Address) are automatically duplicated so the new "
        "client gets fully independent data."
    )

    if not _check_connection():
        return

    conn = get_connection("dev")

    # ── 1. Source client ──────────────────────────────────────────────────────
    st.subheader("1. Source Client")
    src_client_id = _pick_source_client(conn)

    if src_client_id is not None:
        src_row = get_client_by_id(src_client_id, conn)
        if not src_row:
            st.error(f"Client {src_client_id} not found.")
        else:
            # ── 2. New client details ─────────────────────────────────────────
            st.subheader("2. New Client Details")
            st.caption(
                f"**{CLIENT_ID_COLUMN}** is auto-assigned.  "
                "Fields marked *(FK — will be cloned)* create independent copies automatically."
            )
            try:
                client_fk_cols = {fk["col"] for fk in get_outgoing_fks(CLIENT_TABLE, conn)}
            except Exception:
                client_fk_cols = set()

            overrides = _render_client_form(src_row, client_fk_cols)

            # ── 3. Discover extra (non-catalog) tables ────────────────────────
            discover_key = "_clone_extra_tables"
            if discover_key not in st.session_state:
                with st.spinner("Discovering related tables…"):
                    try:
                        all_discovered = discover_related_tables(conn)
                        extras = [t for t in all_discovered if t.name not in CATALOG_TABLE_NAMES]
                        st.session_state[discover_key] = extras
                    except Exception as e:
                        st.error(f"Table discovery failed: {e}")
                        st.session_state[discover_key] = []

            extra_tables = st.session_state[discover_key]

            # ── 4. Table selector ─────────────────────────────────────────────
            st.subheader("3. Tables to Copy")
            enabled_catalog, enabled_extra = _render_table_selector(extra_tables)

            # ── 5. Confirm & execute ──────────────────────────────────────────
            st.subheader("4. Confirm & Copy")

            catalog_count = len(enabled_catalog)
            extra_count   = len(enabled_extra)
            total_on  = catalog_count + extra_count
            total_off = (len(CLONE_CATALOG) - 1) + len(extra_tables) - total_on
            st.markdown(f"**{total_on}** table(s) will be copied, **{total_off}** skipped.")

            confirmed = st.checkbox(
                f"Copy all selected data from client **{src_client_id}** to a new client in Development",
                key="clone_confirm",
            )

            if st.button("Copy Client", type="primary", disabled=not confirmed, key="clone_run"):
                # Clear any previous edit session before starting a new clone
                st.session_state.pop("clone_edit_result", None)
                _run_clone(
                    src_client_id        = src_client_id,
                    overrides            = overrides,
                    enabled_catalog      = enabled_catalog,
                    extra_tables         = extra_tables,
                    enabled_extra        = enabled_extra,
                    conn                 = conn,
                )

    # ── Post-clone edit section ───────────────────────────────────────────────
    if st.session_state.get("clone_edit_result") is not None:
        st.divider()
        _render_post_clone_edit(st.session_state["clone_edit_result"], conn)

    # ── Delete section — always visible once connected ────────────────────────
    st.divider()
    _render_delete_section(conn)


# ---------------------------------------------------------------------------
# Connection guard
# ---------------------------------------------------------------------------

def _check_connection() -> bool:
    conns = st.session_state.get("connections", {})
    dev = conns.get("dev", {})
    if not dev.get("host"):
        st.warning("Configure and connect to the **Development** environment in the sidebar first.")
        return False
    return True


# ---------------------------------------------------------------------------
# Source client picker
# ---------------------------------------------------------------------------

def _pick_source_client(conn) -> int | None:
    col_search, col_or, col_id = st.columns([4, 1, 2])

    with col_search:
        query = st.text_input("Search by name / email / company", key="clone_search")
    col_or.markdown("<br>or", unsafe_allow_html=True)
    with col_id:
        manual_id = st.number_input("Enter Client ID directly", min_value=0, step=1,
                                    value=0, key="clone_manual_id")

    if manual_id:
        return int(manual_id)

    if not query or len(query) < 2:
        st.info("Type at least 2 characters to search, or enter a Client ID directly.")
        return None

    cache_key = f"_clone_search_{query}"
    if cache_key not in st.session_state:
        try:
            st.session_state[cache_key] = search_clients(query, conn)
        except Exception as e:
            st.error(f"Search failed: {e}")
            return None
    results = st.session_state[cache_key]

    if not results:
        st.warning("No clients found.")
        return None

    def label(row):
        parts = [str(row.get(k, "")) for k in
                 ("Name", "ClientName", "Company", "Email") if row.get(k)]
        return f"[{row.get(CLIENT_ID_COLUMN, '?')}]  " + "  ·  ".join(parts[:3])

    opts = {label(r): r[CLIENT_ID_COLUMN] for r in results}
    chosen = st.selectbox("Select source client", list(opts.keys()), key="clone_sel")
    return opts.get(chosen)


# ---------------------------------------------------------------------------
# Editable new-client form
# ---------------------------------------------------------------------------

def _render_client_form(src_row: dict, fk_cols: set[str]) -> dict:
    overrides: dict = {}
    skip_cols = {CLIENT_ID_COLUMN}

    cols_left, cols_right = st.columns(2)
    items = [(k, v) for k, v in src_row.items() if k not in skip_cols]
    half  = (len(items) + 1) // 2

    for idx, (col, val) in enumerate(items):
        container = cols_left if idx < half else cols_right
        with container:
            if col in fk_cols:
                st.text_input(
                    f"{col}  *(FK — will be cloned)*",
                    value=str(val) if val is not None else "",
                    disabled=True,
                    key=f"clone_field_{col}",
                )
            else:
                new_val = st.text_input(
                    col,
                    value=str(val) if val is not None else "",
                    key=f"clone_field_{col}",
                )
                if new_val != str(val if val is not None else ""):
                    if isinstance(val, int):
                        try:
                            overrides[col] = int(new_val)
                        except ValueError:
                            overrides[col] = new_val
                    elif isinstance(val, float):
                        try:
                            overrides[col] = float(new_val)
                        except ValueError:
                            overrides[col] = new_val
                    else:
                        overrides[col] = new_val

    return overrides


# ---------------------------------------------------------------------------
# Table selector
# ---------------------------------------------------------------------------

def _render_table_selector(
    extra_tables: list,
) -> tuple[set[str], set[str]]:
    """
    Two sections:
      A) Catalog tables — grouped, default states from CLONE_CATALOG.
      B) All other discovered tables — all off by default.

    Returns (enabled_catalog_tables, enabled_extra_tables).
    """
    st.caption(
        "Toggle which tables to include in the clone.  "
        "**Configuration tables** are on by default.  "
        "**Additional tables** (transactional data) are off by default."
    )

    enabled_catalog: set[str] = set()
    enabled_extra:   set[str] = set()

    # ── A. Catalog tables, grouped ────────────────────────────────────────
    catalog_by_group: dict[str, list] = {}
    for entry in CLONE_CATALOG:
        if entry.is_root:
            continue
        catalog_by_group.setdefault(entry.group, []).append(entry)

    group_order = ["config", "approval", "users", "email"]
    for group in group_order:
        entries = catalog_by_group.get(group, [])
        if not entries:
            continue
        group_label = CATALOG_GROUP_LABELS.get(group, group.title())
        st.markdown(f"**{group_label}**")
        cols = st.columns(3)
        for i, entry in enumerate(entries):
            checked = cols[i % 3].checkbox(
                f"{entry.label} (`{entry.table}`)",
                value=entry.default_enabled,
                key=f"clone_cat_{entry.table}",
            )
            if checked:
                enabled_catalog.add(entry.table)

    # ── B. Extra (discovered, not in catalog) — all off by default ────────
    if extra_tables:
        with st.expander(
            f"Additional Tables ({len(extra_tables)} discovered, all off by default)",
            expanded=False,
        ):
            st.caption(
                "These tables have a direct ClientId column but are not part of the "
                "standard clone set.  Enable individual tables if needed."
            )
            cols = st.columns(3)
            for i, info in enumerate(extra_tables):
                checked = cols[i % 3].checkbox(
                    info.name,
                    value=False,
                    key=f"clone_extra_{info.name}",
                )
                if checked:
                    enabled_extra.add(info.name)

    return enabled_catalog, enabled_extra


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

def _run_clone(
    src_client_id:   int,
    overrides:       dict,
    enabled_catalog: set[str],
    extra_tables:    list,
    enabled_extra:   set[str],
    conn,
) -> None:
    status = st.status("Cloning client…", expanded=True)

    def progress_cb(msg: str, level: str = "info"):
        icon = {"info": "ℹ️", "warning": "⚠️", "error": "❌"}.get(level, "•")
        status.write(f"{icon} {msg}")

    result = clone_client(
        src_client_id          = src_client_id,
        new_client_overrides   = overrides,
        enabled_catalog_tables = enabled_catalog,
        extra_table_infos      = extra_tables,
        enabled_extra_tables   = enabled_extra,
        conn                   = conn,
        progress_callback      = progress_cb,
    )

    if result.success:
        st.session_state["clone_edit_result"] = result
        status.update(label="Clone complete!", state="complete")
        st.balloons()
        st.success(
            f"New client created with **{CLIENT_ID_COLUMN} = {result.new_client_id}** "
            f"(copied from client {src_client_id})."
        )
    else:
        status.update(label="Clone failed — rolled back.", state="error")
        st.error(f"Error: {result.error}")
        return

    # ── Summary ───────────────────────────────────────────────────────────────
    c1, c2, c3 = st.columns(3)
    c1.metric("Tables Copied", len(result.tables_copied))
    c2.metric("Total Rows Copied", sum(result.rows_copied.values()))
    c3.metric("FK Records Cloned", sum(result.fk_cloned.values()))

    if result.rows_copied:
        st.markdown("#### Rows copied per table")
        df = pd.DataFrame([
            {"Table": tbl, "Rows Copied": n}
            for tbl, n in result.rows_copied.items()
        ])
        st.dataframe(df, use_container_width=True, hide_index=True)

    if result.fk_cloned:
        st.markdown("#### Shared records cloned (independent copies)")
        df_fk = pd.DataFrame([
            {"Shared Table": tbl, "Records Cloned": n}
            for tbl, n in result.fk_cloned.items()
        ])
        st.dataframe(df_fk, use_container_width=True, hide_index=True)

    if result.excluded_tables:
        st.caption("Skipped: " + ", ".join(f"`{t}`" for t in result.excluded_tables))

    st.info(
        f"To promote client **{result.new_client_id}** to QA, go to the "
        f"**Migrate Dev → QA** tab and search for this client ID."
    )


# ---------------------------------------------------------------------------
# Post-clone edit section
# ---------------------------------------------------------------------------

def _render_post_clone_edit(result, conn) -> None:
    st.subheader("Edit Cloned Client Data")
    st.caption(
        f"Expand any table below to view and edit the rows cloned for "
        f"**{CLIENT_ID_COLUMN} = {result.new_client_id}**. "
        "Click **Save** within each table to write changes back to Development."
    )

    # ── Client root row ────────────────────────────────────────────────────
    with st.expander(f"`{CLIENT_TABLE}` — root record"):
        row = get_client_by_id(result.new_client_id, conn)
        _render_table_editor(
            table=CLIENT_TABLE,
            rows=[row] if row else [],
            pk_col=CLIENT_ID_COLUMN,
            locked_cols={CLIENT_ID_COLUMN},
            new_client_id=result.new_client_id,
            conn=conn,
        )

    # ── Catalog and extra tables ───────────────────────────────────────────
    for table in result.tables_copied:
        entry = CATALOG_BY_TABLE.get(table)
        label = entry.label if entry else table
        row_count = result.rows_copied.get(table, 0)

        with st.expander(f"`{table}` — {label} ({row_count} row(s))"):
            _fetch_and_render_table(table, entry, result.new_client_id, conn)


def _fetch_and_render_table(table: str, entry, new_client_id: int, conn) -> None:
    cache_key = f"_edit_rows_{table}_{new_client_id}"

    if cache_key not in st.session_state:
        try:
            if entry:
                rows = fetch_cloned_table_rows(entry, new_client_id, conn)
            else:
                extra_infos = st.session_state.get("_clone_extra_tables", [])
                info = next((i for i in extra_infos if i.name == table), None)
                if not info:
                    st.warning(f"Cannot load rows for `{table}` — table info unavailable.")
                    return
                rows = read_client_data(table, info.client_id_column, new_client_id, conn)
            st.session_state[cache_key] = rows
        except Exception as e:
            st.error(f"Could not load `{table}`: {e}")
            return

    rows = st.session_state.get(cache_key, [])

    # Determine PK and which columns to lock
    if entry:
        pk_col = entry.auto_pk or get_pk_column(table, conn)
        locked: set[str] = set()
        if pk_col:
            locked.add(pk_col)
        if entry.client_id_col:
            locked.add(entry.client_id_col)
        if entry.parent_join_col:
            locked.add(entry.parent_join_col)
    else:
        pk_col = get_pk_column(table, conn)
        locked = {pk_col} if pk_col else set()
        extra_infos = st.session_state.get("_clone_extra_tables", [])
        info = next((i for i in extra_infos if i.name == table), None)
        if info:
            locked.add(info.client_id_column)

    _render_table_editor(table, rows, pk_col, locked, new_client_id, conn)


def _render_table_editor(
    table: str,
    rows: list[dict],
    pk_col: str | None,
    locked_cols: set[str],
    new_client_id: int,
    conn,
) -> None:
    if not rows:
        st.info("No rows found for this table.")
        return

    df = pd.DataFrame(rows)

    col_cfg = {
        col: st.column_config.Column(disabled=True)
        for col in df.columns
        if col in locked_cols
    }

    edited_df = st.data_editor(
        df,
        key=f"editor_{table}_{new_client_id}",
        use_container_width=True,
        num_rows="fixed",
        column_config=col_cfg,
    )

    if st.button(f"Save `{table}`", key=f"save_{table}_{new_client_id}", type="primary"):
        if not pk_col:
            st.error(f"No primary key found for `{table}` — cannot save.")
            return
        try:
            cleaned = [_clean_row(r) for r in edited_df.to_dict("records")]
            n = update_rows(table, cleaned, [pk_col], conn)
            st.success(f"Saved {n} row(s) to `{table}`.")
            st.session_state.pop(f"_edit_rows_{table}_{new_client_id}", None)
        except Exception as e:
            st.error(f"Save failed: {e}")


def _clean_row(row: dict) -> dict:
    """Convert NaN floats (pandas null representation) back to None for MySQL."""
    result = {}
    for k, v in row.items():
        try:
            result[k] = None if pd.isna(v) else v
        except (TypeError, ValueError):
            result[k] = v
    return result


# ---------------------------------------------------------------------------
# Delete section
# ---------------------------------------------------------------------------

def _render_delete_section(conn) -> None:
    with st.expander("Undo — Delete Cloned Client", expanded=False):
        st.caption(
            "Permanently removes a cloned client and all of its associated rows "
            "from Development. Use this when a clone was created by mistake or is "
            "no longer needed. **This cannot be undone.**"
        )

        del_cid = st.number_input(
            "Client ID to delete", min_value=1, step=1, value=1, key="del_clone_cid"
        )

        if st.button("Look up client", key="del_clone_lookup"):
            row = get_client_by_id(int(del_cid), conn)
            st.session_state["del_clone_preview_id"]  = int(del_cid)
            st.session_state["del_clone_preview_row"] = row

        preview_id  = st.session_state.get("del_clone_preview_id")
        preview_row = st.session_state.get("del_clone_preview_row")

        # Clear stale preview if the user changed the ID without re-looking up
        if preview_id is not None and preview_id != int(del_cid):
            st.session_state.pop("del_clone_preview_id",  None)
            st.session_state.pop("del_clone_preview_row", None)
            preview_row = None

        if preview_id == int(del_cid):
            if preview_row is None:
                st.error(f"Client {int(del_cid)} not found in Development.")
            else:
                name_keys = [k for k in ("Name", "ClientName", "Company") if preview_row.get(k)]
                display   = " · ".join(str(preview_row[k]) for k in name_keys[:2]) or "(no name)"
                st.info(f"**{preview_row.get(CLIENT_ID_COLUMN)}** — {display}")

                typed = st.text_input(
                    f"Type **{int(del_cid)}** to confirm",
                    key="del_clone_typed",
                )
                confirmed = st.checkbox(
                    "I understand this permanently deletes all data for this client in Development",
                    key="del_clone_confirm",
                )

                ready = (typed.strip() == str(int(del_cid))) and confirmed

                if st.button(
                    "Permanently Delete Client",
                    type="primary",
                    disabled=not ready,
                    key="del_clone_run",
                ):
                    extra_tables = st.session_state.get("_clone_extra_tables", [])
                    _run_delete_clone(int(del_cid), extra_tables, conn)


def _run_delete_clone(client_id: int, extra_tables: list, conn) -> None:
    status = st.status(f"Deleting client {client_id}…", expanded=True)

    def progress_cb(msg: str, level: str = "info"):
        icon = {"info": "ℹ️", "warning": "⚠️", "error": "❌"}.get(level, "•")
        status.write(f"{icon} {msg}")

    result = delete_cloned_client(
        client_id         = client_id,
        extra_table_infos = extra_tables,
        conn              = conn,
        log_callback      = progress_cb,
    )

    if not result.success:
        status.update(label="Delete failed — rolled back.", state="error")
        st.error(f"Error: {result.error}")
        return

    status.update(label=f"Client {client_id} deleted.", state="complete")
    st.success(
        f"Client **{client_id}** and all associated data deleted from Development."
    )

    for key in ("del_clone_preview_id", "del_clone_preview_row"):
        st.session_state.pop(key, None)

    total_rows   = sum(result.rows_deleted.values())
    total_tables = len(result.tables_deleted)
    c1, c2 = st.columns(2)
    c1.metric("Tables Cleaned", total_tables)
    c2.metric("Total Rows Deleted", total_rows)

    if result.rows_deleted:
        df = pd.DataFrame([
            {"Table": tbl, "Rows Deleted": n}
            for tbl, n in result.rows_deleted.items()
        ])
        st.dataframe(df, use_container_width=True, hide_index=True)

    if result.fk_records_deleted:
        st.caption(
            "Shared FK records removed: "
            + ", ".join(f"`{t}` ({n})" for t, n in result.fk_records_deleted.items())
        )
