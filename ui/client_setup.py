# ui/client_setup.py — Client Setup tab

from __future__ import annotations
import pandas as pd
import streamlit as st

from db.connection import get_connection
from migration.client_setup import get_setup_preview, run_client_setup

_ALLOWED_ENVS = {"dev": "Development", "qa": "QA / Staging"}


def render_client_setup() -> None:
    st.header("Client Setup")
    st.caption(
        "Prepare a newly cloned (or imported) client for use: updates the Admin user, "
        "seeds template data, and runs the migration utility stored procedure "
        "(`_x_Utility_Migrate_Client`). Available on Development and QA only."
    )

    # ── Environment selector ───────────────────────────────────────────────
    env = st.selectbox(
        "Target Environment",
        list(_ALLOWED_ENVS.keys()),
        format_func=lambda k: _ALLOWED_ENVS[k],
        key="setup_env",
    )

    conns = st.session_state.get("connections", {})
    if not conns.get(env, {}).get("host"):
        st.warning(
            f"Configure and connect to **{_ALLOWED_ENVS[env]}** in the sidebar first."
        )
        return

    conn = get_connection(env)

    # ── 1. Inputs ──────────────────────────────────────────────────────────
    st.subheader("1. Client & Admin User")

    col_cid, col_uid = st.columns([1, 2])
    with col_cid:
        client_id = st.number_input(
            "Client ID", min_value=1, step=1, value=1, key="setup_client_id"
        )
    with col_uid:
        user_id = st.text_input(
            "Admin User ID (UUID)",
            key="setup_user_id",
            placeholder="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
        )

    if st.button("Look Up", key="setup_lookup"):
        uid = user_id.strip()
        if not uid:
            st.error("Enter the Admin User ID.")
        else:
            try:
                preview = get_setup_preview(int(client_id), uid, conn)
                st.session_state["setup_preview"] = preview
            except Exception as exc:
                st.error(str(exc))
                st.session_state.pop("setup_preview", None)

    # Discard stale preview if the user changed the inputs
    preview = st.session_state.get("setup_preview")
    if preview and (
        preview.client_id != int(client_id)
        or preview.user_id != user_id.strip()
    ):
        st.session_state.pop("setup_preview", None)
        preview = None

    if not preview:
        return

    # ── 2. Preview ─────────────────────────────────────────────────────────
    st.subheader("2. Preview")

    col_c, col_u = st.columns(2)
    col_c.info(
        f"**Client {preview.client_id}** — CustomerShortName: `{preview.customer_short_name}`"
    )
    col_u.info(
        f"**Admin User** — current username: `{preview.current_username or '(none)'}` "
        f"→ new: `{preview.new_username}`"
    )

    steps_df = pd.DataFrame([
        {
            "Step": "1 — Update Admin User",
            "Detail": (
                f"UserName → `{preview.new_username}`, "
                "isCloudXuser = 1, RoleId = 0, isActive = 1, "
                "FirstName = 'Admin', LastName = 'CloudX'"
            ),
        },
        {
            "Step": "2 — Seed ArchiveReason",
            "Detail": (
                f"{preview.archive_reason_count} row(s) copied from ClientId = 0 template"
            ),
        },
        {
            "Step": "3 — Seed _x_ClientParameters",
            "Detail": (
                f"{preview.client_param_count} row(s) copied from ClientId = 0 template"
            ),
        },
        {
            "Step": "4 — Run stored procedure",
            "Detail": (
                f"CALL _x_Utility_Migrate_Client({preview.client_id}, NULL) — "
                "builds ClientRoles, ClientUserRoles, ClientRoleFunctions, and related setup"
            ),
        },
    ])
    st.dataframe(steps_df, use_container_width=True, hide_index=True)

    # ── 3. Confirm & execute ───────────────────────────────────────────────
    st.subheader("3. Confirm & Run")

    confirmed = st.checkbox(
        f"Run setup for Client **{preview.client_id}** ({preview.customer_short_name}) "
        f"on **{_ALLOWED_ENVS[env]}**",
        key="setup_confirm",
    )

    if st.button("Run Setup", type="primary", disabled=not confirmed, key="setup_run"):
        _execute_setup(preview.client_id, preview.user_id, conn)


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

def _execute_setup(client_id: int, user_id: str, conn) -> None:
    status = st.status("Running client setup…", expanded=True)

    def progress_cb(msg: str, level: str = "info"):
        icon = {"info": "ℹ️", "warning": "⚠️", "error": "❌"}.get(level, "•")
        status.write(f"{icon} {msg}")

    result = run_client_setup(client_id, user_id, conn, progress_cb)

    if not result.success:
        status.update(label="Setup failed.", state="error")
        st.error(f"Error: {result.error}")
        return

    status.update(label="Setup complete!", state="complete")
    st.success(f"Client **{client_id}** setup completed successfully.")

    c1, c2, c3 = st.columns(3)
    c1.metric("Admin User Updated", "Yes" if result.user_updated else "No")
    c2.metric("ArchiveReason Rows", result.archive_rows_inserted)
    c3.metric("Parameter Rows", result.param_rows_inserted)

    if result.proc_message:
        st.warning(f"Stored procedure message: {result.proc_message}")

    if result.proc_output:
        st.markdown("#### Admin users confirmed by stored procedure")
        st.dataframe(
            pd.DataFrame(result.proc_output),
            use_container_width=True,
            hide_index=True,
        )

    st.session_state.pop("setup_preview", None)
