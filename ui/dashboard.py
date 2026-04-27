# ui/dashboard.py — Sync Status (Drift) Dashboard
#
# Shows which clients are in sync vs drifted across environments.
# Uses a single UNION ALL query per environment for efficiency.

from __future__ import annotations
import pandas as pd
import streamlit as st

from db.connection import get_connection
from db.discovery import discover_related_tables
from db.operations import get_all_clients, get_client_totals_all
from config import ENV_LABELS, ENV_ORDER, DASHBOARD_CLIENT_LIMIT


def render_dashboard() -> None:
    st.header("Sync Status Dashboard")
    st.caption(
        "Compares total row counts for every client across all environments. "
        "Use this to identify which clients need migration — then batch-migrate them in one click."
    )

    # -------------------------------------------------------------------------
    # Controls
    # -------------------------------------------------------------------------
    col_ref, col_limit, col_run = st.columns([2, 2, 1])
    ref_env = col_ref.selectbox(
        "Reference environment",
        ENV_ORDER,
        format_func=lambda e: ENV_LABELS[e],
        index=0,
        key="dash_ref_env",
    )
    limit = col_limit.number_input(
        "Max clients to scan",
        min_value=10, max_value=DASHBOARD_CLIENT_LIMIT,
        value=200, step=50,
        key="dash_limit",
    )
    run = col_run.button("Refresh", type="primary", use_container_width=True, key="dash_run")

    if not run and "dash_results" not in st.session_state:
        st.info("Click **Refresh** to scan all environments and detect drift.")
        return

    if run:
        _run_dashboard(ref_env, int(limit))

    if "dash_results" not in st.session_state:
        return

    df: pd.DataFrame = st.session_state["dash_results"]
    meta: dict = st.session_state.get("dash_meta", {})

    # -------------------------------------------------------------------------
    # Summary metrics
    # -------------------------------------------------------------------------
    total = len(df)
    in_sync = (df["In Sync?"] == "✓").sum()
    drifted = total - in_sync

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Total Clients", total)
    m2.metric("In Sync", in_sync, delta=None)
    m3.metric("Drifted", drifted, delta=None,
              delta_color="inverse" if drifted > 0 else "normal")
    m4.metric("Scanned at", meta.get("scanned_at", "—"))

    if drifted == 0:
        st.success("All scanned clients are in sync across environments.")
    else:
        st.warning(f"{drifted} client(s) have different row counts across environments.")

    # -------------------------------------------------------------------------
    # Filter controls
    # -------------------------------------------------------------------------
    col_f1, col_f2 = st.columns(2)
    show_filter = col_f1.radio(
        "Show", ["All", "Drifted only", "In sync only"],
        horizontal=True, key="dash_filter"
    )
    search_id = col_f2.text_input("Filter by client ID or name", key="dash_search")

    view = df.copy()
    if show_filter == "Drifted only":
        view = view[view["In Sync?"] == "✗"]
    elif show_filter == "In sync only":
        view = view[view["In Sync?"] == "✓"]
    if search_id.strip():
        mask = view.apply(lambda row: search_id.strip().lower() in str(row).lower(), axis=1)
        view = view[mask]

    # -------------------------------------------------------------------------
    # Main table
    # -------------------------------------------------------------------------
    st.subheader(f"Client Sync Status ({len(view)} shown)")

    st.dataframe(view, use_container_width=True, hide_index=True)

    # -------------------------------------------------------------------------
    # Migrate drifted action
    # -------------------------------------------------------------------------
    if drifted > 0:
        st.divider()
        st.subheader("Migrate Drifted Clients")

        drifted_ids = df[df["In Sync?"] == "✗"]["Client ID"].tolist()
        ref_envs = [e for e in ENV_ORDER if e != ref_env]

        col_route, col_btn = st.columns([2, 1])
        route_dst = col_route.selectbox(
            "Migrate drifted clients to:",
            ref_envs,
            format_func=lambda e: f"{ENV_LABELS[ref_env]} → {ENV_LABELS[e]}",
            key="dash_route_dst",
        )
        migrate_all = col_btn.button(
            f"Batch Migrate {drifted} Drifted →",
            type="primary",
            use_container_width=True,
            key="dash_migrate_all",
        )

        if migrate_all:
            # Pre-populate batch tab state and redirect
            st.session_state["batch_prefill_ids"] = drifted_ids
            st.session_state["batch_prefill_src"] = ref_env
            st.session_state["batch_prefill_dst"] = route_dst
            st.toast(
                f"{len(drifted_ids)} client IDs loaded into the Batch Migration tab.",
                icon="📦",
            )
            st.info(
                f"Switch to the **📦 Batch Migration** tab to review and execute. "
                f"({len(drifted_ids)} clients pre-loaded)"
            )


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def _run_dashboard(ref_env: str, limit: int) -> None:
    errors: dict[str, str] = {}
    connections: dict[str, object] = {}

    with st.spinner("Connecting to all environments…"):
        for env in ENV_ORDER:
            try:
                connections[env] = get_connection(env)
            except Exception as e:
                errors[env] = str(e)

    for env, msg in errors.items():
        st.warning(f"Could not connect to {ENV_LABELS[env]}: {msg}")

    if ref_env not in connections:
        st.error(f"Reference environment ({ENV_LABELS[ref_env]}) is not available.")
        _close_all(connections)
        return

    # Discover tables once
    with st.spinner("Discovering schema…"):
        try:
            tables = discover_related_tables(connections[ref_env])
            st.session_state["last_discovered_tables"] = tables
        except Exception as e:
            st.error(f"Table discovery failed: {e}")
            _close_all(connections)
            return

    # Get all clients from reference env
    with st.spinner(f"Loading clients from {ENV_LABELS[ref_env]} (limit {limit})…"):
        try:
            clients = get_all_clients(connections[ref_env], limit=limit)
        except Exception as e:
            st.error(f"Could not load clients: {e}")
            _close_all(connections)
            return

    client_ids = [c.get("ClientId") for c in clients if c.get("ClientId")]

    # Get total row counts per client per environment (single query each)
    totals: dict[str, dict[int, int]] = {}
    for env, conn in connections.items():
        with st.spinner(f"Scanning {ENV_LABELS[env]}…"):
            try:
                totals[env] = get_client_totals_all(tables, conn)
            except Exception as e:
                st.warning(f"{ENV_LABELS[env]} scan failed: {e}")
                totals[env] = {}

    # Build result dataframe
    rows = []
    for client in clients:
        cid = client.get("ClientId")
        if not cid:
            continue

        # Try to get a display name
        display_name = (
            client.get("Name")
            or client.get("ClientName")
            or client.get("Company")
            or client.get("CompanyName")
            or "—"
        )

        row: dict = {"Client ID": cid, "Name": display_name}
        env_totals = []
        for env in ENV_ORDER:
            count = totals.get(env, {}).get(cid)
            row[ENV_LABELS[env]] = count if count is not None else "N/A"
            if isinstance(count, int):
                env_totals.append(count)

        row["In Sync?"] = "✓" if (len(set(env_totals)) <= 1 and env_totals) else "✗"
        rows.append(row)

    _close_all(connections)

    from datetime import datetime
    st.session_state["dash_results"] = pd.DataFrame(rows)
    st.session_state["dash_meta"] = {
        "scanned_at": datetime.now().strftime("%H:%M:%S"),
        "ref_env": ref_env,
    }


def _close_all(connections: dict) -> None:
    for conn in connections.values():
        try:
            conn.close()
        except Exception:
            pass
