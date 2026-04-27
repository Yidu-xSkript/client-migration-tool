# ui/compare.py — Compare Environments tab

from __future__ import annotations
import pandas as pd
import streamlit as st

from db.connection import get_connection
from db.discovery import discover_related_tables
from db.operations import get_all_row_counts, search_clients, get_client_by_id, get_column_schema
from config import ENV_LABELS, ENV_ORDER, CLIENT_TABLE


def render_compare() -> None:
    st.header("Compare Environments")
    st.caption(
        "View row counts for a client across Dev, QA, and Prod at a glance. "
        "Identifies what data exists where and whether environments are in sync."
    )

    client_id = _client_selector()
    if client_id is None:
        return

    col_run, col_drift = st.columns([1, 1])
    run_comparison = col_run.button("Run Full Comparison", type="primary", use_container_width=True)
    show_drift = col_drift.checkbox("Show schema drift", value=False)

    if run_comparison:
        _run_comparison(client_id, show_drift)


# ---------------------------------------------------------------------------
# Client selection helpers
# ---------------------------------------------------------------------------

def _client_selector() -> int | None:
    """
    Render client input widgets. Returns the selected ClientId or None.
    Tries to connect to Dev for client search; falls back to manual ID entry.
    """
    method = st.radio(
        "Find client by:",
        ["Client ID", "Search by name / email / company"],
        horizontal=True,
    )

    if method == "Client ID":
        client_id = st.number_input("Client ID", min_value=1, step=1, value=1)
        return int(client_id)

    # Search mode
    query = st.text_input("Search term", placeholder="e.g. Acme Corp, john@example.com")
    if not query:
        st.info("Enter a search term above.")
        return None

    try:
        conn = get_connection("dev")
        results = search_clients(query, conn)
        conn.close()
    except Exception as e:
        st.warning(f"Could not search Dev database: {e}")
        return None

    if not results:
        st.warning("No clients found matching that query.")
        return None

    # Build a display label for each result
    def label_row(row: dict) -> str:
        parts = []
        for key in ("Name", "ClientName", "Company", "CompanyName", "Email", "EmailAddress"):
            if row.get(key):
                parts.append(str(row[key]))
        id_val = row.get("ClientId", "?")
        return f"[{id_val}] " + " — ".join(parts[:3]) if parts else f"[{id_val}]"

    options = {label_row(r): r["ClientId"] for r in results}
    chosen = st.selectbox("Select a client", list(options.keys()))
    return options[chosen] if chosen else None


# ---------------------------------------------------------------------------
# Comparison logic
# ---------------------------------------------------------------------------

def _run_comparison(client_id: int, show_drift: bool) -> None:
    """
    Open connections to all three environments, discover tables, fetch row counts,
    and display a comparison table.
    """
    connections = {}
    errors = {}

    with st.spinner("Connecting to all environments…"):
        for env in ENV_ORDER:
            try:
                connections[env] = get_connection(env)
            except Exception as e:
                errors[env] = str(e)

    if errors:
        for env, msg in errors.items():
            st.error(f"{ENV_LABELS[env]}: {msg}")
        # If at least Dev connected, still try to discover
        if "dev" in errors:
            _close_all(connections)
            return

    # Discover tables using the first available connection
    discovery_conn = connections.get("dev") or next(iter(connections.values()), None)
    if discovery_conn is None:
        st.error("No database connections available.")
        return

    with st.spinner("Discovering related tables…"):
        try:
            tables = discover_related_tables(discovery_conn)
            st.session_state["last_discovered_tables"] = tables
        except Exception as e:
            st.error(f"Table discovery failed: {e}")
            _close_all(connections)
            return

    # Fetch row counts per environment
    counts: dict[str, dict[str, int]] = {}
    for env, conn in connections.items():
        with st.spinner(f"Fetching counts from {ENV_LABELS[env]}…"):
            counts[env] = get_all_row_counts(tables, client_id, conn)

    # Display client info banner
    _show_client_banner(client_id, connections)

    # Build and display the comparison DataFrame
    _render_count_table(tables, counts, client_id)

    # Schema drift
    if show_drift:
        _render_schema_drift(tables, connections)

    _close_all(connections)


def _show_client_banner(client_id: int, connections: dict) -> None:
    """Show a brief info line about the client."""
    for env in ENV_ORDER:
        conn = connections.get(env)
        if conn:
            try:
                row = get_client_by_id(client_id, conn)
                if row:
                    display_fields = []
                    for key in ("Name", "ClientName", "Email", "EmailAddress", "Company", "CompanyName"):
                        if row.get(key):
                            display_fields.append(f"**{key}:** {row[key]}")
                    if display_fields:
                        st.info(f"Client {client_id} — " + "  |  ".join(display_fields[:3]))
                    return
            except Exception:
                continue


def _render_count_table(tables, counts: dict, client_id: int) -> None:
    """Render the main row-count comparison table."""
    rows = []
    for info in tables:
        row = {"Table": info.name}
        env_vals = []
        for env in ENV_ORDER:
            cnt = counts.get(env, {}).get(info.name, None)
            row[ENV_LABELS[env]] = cnt if cnt is not None else "N/A"
            if isinstance(cnt, int):
                env_vals.append(cnt)
        # Match check — only among environments that responded
        row["In Sync?"] = "✓" if len(set(env_vals)) == 1 else "✗"
        rows.append(row)

    df = pd.DataFrame(rows)
    st.subheader(f"Row Counts for Client {client_id}")

    # Colour-code the sync column
    st.dataframe(df, use_container_width=True, hide_index=True)

    # Summary
    total = len(rows)
    in_sync = sum(1 for r in rows if r["In Sync?"] == "✓")
    if in_sync == total:
        st.success(f"All {total} tables are in sync across all environments.")
    else:
        st.warning(f"{total - in_sync} of {total} tables are out of sync.")


def _render_schema_drift(tables, connections: dict) -> None:
    """Compare column definitions across environments and highlight differences."""
    st.subheader("Schema Drift Detection")
    drift_found = False

    for info in tables:
        schemas: dict[str, list[dict]] = {}
        for env, conn in connections.items():
            try:
                schemas[env] = get_column_schema(info.name, conn)
            except Exception:
                schemas[env] = []

        # Compare Dev vs others
        ref = schemas.get("dev", [])
        ref_map = {r["COLUMN_NAME"]: r for r in ref}

        diffs = []
        for env, cols in schemas.items():
            if env == "dev":
                continue
            env_map = {c["COLUMN_NAME"]: c for c in cols}
            for col_name, ref_col in ref_map.items():
                env_col = env_map.get(col_name)
                if env_col is None:
                    diffs.append({
                        "Column": col_name,
                        "Dev": ref_col["DATA_TYPE"],
                        ENV_LABELS[env]: "MISSING",
                    })
                elif env_col["DATA_TYPE"] != ref_col["DATA_TYPE"]:
                    diffs.append({
                        "Column": col_name,
                        "Dev": ref_col["DATA_TYPE"],
                        ENV_LABELS[env]: env_col["DATA_TYPE"],
                    })

        if diffs:
            drift_found = True
            with st.expander(f"Drift in `{info.name}` ({len(diffs)} difference(s))", expanded=True):
                st.dataframe(pd.DataFrame(diffs), use_container_width=True, hide_index=True)

    if not drift_found:
        st.success("No schema differences detected across environments.")


def _close_all(connections: dict) -> None:
    for conn in connections.values():
        try:
            conn.close()
        except Exception:
            pass
