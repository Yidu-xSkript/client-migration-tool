# ui/batch.py — Batch Migration tab

from __future__ import annotations
import time
import io
import pandas as pd
import streamlit as st

from migration.batch import run_batch, BatchResult, BatchClientResult
from migration.profiles import load_all_profiles, get_profile
from config import ENV_LABELS, ENV_ORDER


def render_batch() -> None:
    st.header("Batch Migration")
    st.caption(
        "Migrate multiple clients in a single operation. "
        "Failed clients are skipped — they do not block the rest of the batch."
    )

    # -------------------------------------------------------------------------
    # 1. Route selection
    # -------------------------------------------------------------------------
    st.subheader("1. Route")
    col_src, col_arr, col_dst = st.columns([2, 1, 2])
    src_env = col_src.selectbox(
        "Source", ENV_ORDER, format_func=lambda e: ENV_LABELS[e], key="batch_src"
    )
    col_arr.markdown("<br><br>→", unsafe_allow_html=True)
    dst_env = col_dst.selectbox(
        "Destination",
        [e for e in ENV_ORDER if e != src_env],
        format_func=lambda e: ENV_LABELS[e],
        key="batch_dst",
    )

    is_prod = dst_env == "prod"
    if is_prod:
        st.error("**Production target selected.** Extra care required.")

    # -------------------------------------------------------------------------
    # 2. Client selection — profile, manual, or pre-filled from dashboard
    # -------------------------------------------------------------------------
    st.subheader("2. Clients")

    input_method = st.radio(
        "Select clients via:",
        ["Manual entry", "CSV / text upload", "Migration profile", "Pre-filled from Dashboard"],
        horizontal=True,
        key="batch_input_method",
    )

    client_ids: list[int] = []
    profile = None

    if input_method == "Manual entry":
        raw = st.text_area(
            "Client IDs (one per line, or comma-separated)",
            key="batch_manual_ids",
            height=120,
            placeholder="42\n107\n256",
        )
        client_ids = _parse_ids(raw)

    elif input_method == "CSV / text upload":
        uploaded = st.file_uploader(
            "Upload a .txt or .csv file with one Client ID per line",
            type=["txt", "csv"],
            key="batch_upload",
        )
        if uploaded:
            content = uploaded.read().decode("utf-8", errors="ignore")
            client_ids = _parse_ids(content)
            st.success(f"Loaded {len(client_ids)} client ID(s) from file.")

    elif input_method == "Migration profile":
        profiles = load_all_profiles()
        if not profiles:
            st.info("No profiles saved yet. Create one in the Settings tab.")
        else:
            profile_names = [p.name for p in profiles]
            chosen_name = st.selectbox("Select profile", profile_names, key="batch_profile_sel")
            profile = get_profile(chosen_name)
            if profile:
                client_ids = profile.client_ids or []
                if not client_ids:
                    raw_override = st.text_area(
                        "Profile has no client IDs — enter them here:",
                        key="batch_profile_ids",
                        height=80,
                    )
                    client_ids = _parse_ids(raw_override)
                else:
                    st.info(f"Profile contains {len(client_ids)} client ID(s).")
                    if st.checkbox("Override client list", key="batch_profile_override"):
                        raw_override = st.text_area(
                            "Replace with:", key="batch_profile_ids_override", height=80
                        )
                        client_ids = _parse_ids(raw_override)

    elif input_method == "Pre-filled from Dashboard":
        prefill = st.session_state.get("batch_prefill_ids", [])
        prefill_src = st.session_state.get("batch_prefill_src", src_env)
        prefill_dst = st.session_state.get("batch_prefill_dst", dst_env)
        if prefill:
            st.success(
                f"**{len(prefill)} client(s)** pre-loaded from Dashboard "
                f"({ENV_LABELS.get(prefill_src, prefill_src)} → {ENV_LABELS.get(prefill_dst, prefill_dst)})."
            )
            # Show editable list
            raw_prefill = "\n".join(str(i) for i in prefill)
            edited = st.text_area("Client IDs (edit as needed):", value=raw_prefill,
                                  height=120, key="batch_prefill_edit")
            client_ids = _parse_ids(edited)
        else:
            st.info("No clients pre-loaded from the Dashboard yet. Run a scan there first.")

    if client_ids:
        st.caption(f"{len(client_ids)} client(s) queued: {', '.join(str(i) for i in client_ids[:10])}"
                   + (" …" if len(client_ids) > 10 else ""))

    # -------------------------------------------------------------------------
    # 3. Options (loaded from profile if available, else defaults)
    # -------------------------------------------------------------------------
    st.subheader("3. Options")

    defaults = _defaults_from_profile(profile, src_env, dst_env)

    col_o1, col_o2, col_o3 = st.columns(3)
    conflict_mode = col_o1.radio(
        "Conflict resolution",
        ["replace", "skip", "update"],
        format_func=lambda x: {"replace": "Replace", "skip": "Skip existing", "update": "Upsert"}[x],
        index=["replace", "skip", "update"].index(defaults["conflict_mode"]),
        key="batch_conflict",
    )
    do_backup  = col_o2.checkbox("Create backups before migration", value=defaults["do_backup"], key="batch_backup")
    delta_mode = col_o3.checkbox("Delta mode (only changed rows)", value=defaults["delta_mode"], key="batch_delta")

    with st.expander("Advanced: Column Exclusions & Row Filters"):
        st.caption(
            "⚠️ Admin use only. Row filters are raw SQL WHERE expressions "
            "applied to each table in the source before reading."
        )
        excl_raw = st.text_area(
            "Column exclusions (JSON format: {\"TableName\": [\"col1\", \"col2\"]})",
            value=_dict_to_json(defaults["excluded_columns"]),
            height=80,
            key="batch_excl",
        )
        filter_raw = st.text_area(
            "Row filters (JSON format: {\"TableName\": \"is_deleted = 0\"})",
            value=_dict_to_json(defaults["row_filters"]),
            height=80,
            key="batch_filters",
        )
    excluded_columns = _parse_json_dict(excl_raw)
    row_filters = _parse_json_dict(filter_raw)

    ticket = ""
    if is_prod:
        ticket = st.text_input("Reference / Ticket number", key="batch_ticket",
                               placeholder="JIRA-1234")

    # -------------------------------------------------------------------------
    # 4. Confirmation
    # -------------------------------------------------------------------------
    st.subheader("4. Confirm & Execute")

    if not client_ids:
        st.warning("Add at least one client ID to enable migration.")
        return

    if is_prod:
        st.warning(f"**{len(client_ids)} clients will be migrated to Production.**")
        typed_prod = st.text_input('Type "PROD" to confirm:', key="batch_prod_confirm")
        final_check = st.checkbox(
            "I have verified all clients and understand this modifies Production.",
            key="batch_prod_final",
        )
        ready = typed_prod.strip() == "PROD" and final_check
    else:
        ready = st.checkbox(
            f"Confirm batch migration of {len(client_ids)} client(s): "
            f"{ENV_LABELS[src_env]} → {ENV_LABELS[dst_env]}",
            key="batch_confirm",
        )

    col_btn1, col_btn2 = st.columns(2)
    execute = col_btn1.button(
        f"Run Batch Migration ({len(client_ids)} clients)",
        type="primary",
        disabled=not ready,
        use_container_width=True,
        key="batch_exec",
    )

    if not execute:
        return

    # -------------------------------------------------------------------------
    # 5. Execution with live progress
    # -------------------------------------------------------------------------
    _run_batch_ui(
        client_ids=client_ids,
        src_env=src_env,
        dst_env=dst_env,
        conflict_mode=conflict_mode,
        delta_mode=delta_mode,
        do_backup=do_backup,
        excluded_columns=excluded_columns,
        row_filters=row_filters,
        ticket=ticket,
    )


# ---------------------------------------------------------------------------
# Batch execution UI
# ---------------------------------------------------------------------------

def _run_batch_ui(
    client_ids, src_env, dst_env, conflict_mode, delta_mode,
    do_backup, excluded_columns, row_filters, ticket,
) -> None:
    total = len(client_ids)
    progress_bar = st.progress(0, text="Starting batch…")
    log_container = st.empty()
    results: list[BatchClientResult] = []

    gen = run_batch(
        client_ids=client_ids,
        src_env=src_env,
        dst_env=dst_env,
        conflict_mode=conflict_mode,
        delta_mode=delta_mode,
        do_backup=do_backup,
        excluded_columns=excluded_columns,
        row_filters=row_filters,
        ticket=ticket,
    )

    for i, r in enumerate(gen):
        results.append(r)
        pct = (i + 1) / total
        icon = "✓" if r.success else "✗"
        progress_bar.progress(
            pct,
            text=f"{icon} {i + 1}/{total} — Client {r.client_id}: "
                 + (f"{r.rows_migrated} rows" if r.success else r.error[:60]),
        )

    progress_bar.progress(1.0, text=f"Done — {total} clients processed.")

    # -------------------------------------------------------------------------
    # Summary
    # -------------------------------------------------------------------------
    succeeded = sum(1 for r in results if r.success)
    failed    = sum(1 for r in results if not r.success)
    total_rows = sum(r.rows_migrated for r in results)

    if failed == 0:
        st.balloons()
        st.success(f"Batch complete — {succeeded} clients, {total_rows} total rows migrated.")
    else:
        st.warning(
            f"Batch complete with errors — {succeeded} succeeded, {failed} failed, "
            f"{total_rows} rows migrated."
        )

    # Results table
    rows_df = []
    for r in results:
        rows_df.append({
            "Client ID":  r.client_id,
            "Status":     "✓ OK" if r.success else "✗ FAILED",
            "Tables":     r.tables_migrated,
            "Rows":       r.rows_migrated,
            "Error":      r.error[:80] if r.error else "",
        })
    df = pd.DataFrame(rows_df)

    st.dataframe(df, use_container_width=True, hide_index=True)

    # CSV export of results
    csv = df.to_csv(index=False)
    st.download_button(
        "Download Results CSV",
        data=csv,
        file_name=f"batch_result_{src_env}_to_{dst_env}.csv",
        mime="text/csv",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_ids(text: str) -> list[int]:
    """Parse a string of client IDs (comma or newline separated)."""
    ids = []
    for token in text.replace(",", "\n").split("\n"):
        t = token.strip()
        if t.isdigit():
            ids.append(int(t))
    return list(dict.fromkeys(ids))  # Deduplicate, preserve order


def _defaults_from_profile(profile, src_env: str, dst_env: str) -> dict:
    if profile:
        return {
            "conflict_mode":    profile.conflict_mode,
            "do_backup":        profile.do_backup,
            "delta_mode":       profile.delta_mode,
            "excluded_columns": profile.excluded_columns,
            "row_filters":      profile.row_filters,
        }
    return {
        "conflict_mode":    "replace",
        "do_backup":        True,
        "delta_mode":       False,
        "excluded_columns": {},
        "row_filters":      {},
    }


def _dict_to_json(d: dict) -> str:
    if not d:
        return ""
    import json
    return json.dumps(d, indent=2)


def _parse_json_dict(raw: str) -> dict:
    if not raw or not raw.strip():
        return {}
    import json
    try:
        val = json.loads(raw)
        return val if isinstance(val, dict) else {}
    except json.JSONDecodeError:
        return {}
