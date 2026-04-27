# ui/sidebar.py — Connection configuration sidebar with saved credential support

import streamlit as st
from db.connection import test_connection
from config_store import save_credentials, load_credentials, delete_credentials, credentials_saved
from config import ENV_LABELS, ENV_ORDER, DEFAULT_PORT


def _env_color(env: str) -> str:
    return {"dev": "#4CAF50", "qa": "#FF9800", "prod": "#f44336"}.get(env, "#888")


def render_sidebar() -> None:
    """Render the connection configuration panel in the sidebar."""
    st.sidebar.title("Connection Config")

    # Initialise session state for connections
    if "connections" not in st.session_state:
        st.session_state["connections"] = {
            env: {"host": "", "user": "", "password": "", "database": "", "port": DEFAULT_PORT}
            for env in ENV_ORDER
        }

    # -------------------------------------------------------------------------
    # Load saved credentials button
    # -------------------------------------------------------------------------
    if credentials_saved():
        if st.sidebar.button("Load Saved Credentials", use_container_width=True):
            loaded = load_credentials()
            if loaded:
                for env in ENV_ORDER:
                    if env not in loaded:
                        continue
                    creds = loaded[env]
                    st.session_state["connections"][env].update(creds)
                    # Also write into the widget keys so inputs re-render with the new values
                    st.session_state[f"{env}_host"] = creds.get("host", "")
                    st.session_state[f"{env}_user"] = creds.get("user", "")
                    st.session_state[f"{env}_pass"] = creds.get("password", "")
                    st.session_state[f"{env}_db"]   = creds.get("database", "")
                    st.session_state[f"{env}_port"] = creds.get("port", DEFAULT_PORT)
                st.sidebar.success("Credentials loaded.")
                st.rerun()
            else:
                st.sidebar.error("Could not decrypt saved credentials.")
    else:
        st.sidebar.caption("No saved credentials on disk.")

    # -------------------------------------------------------------------------
    # Per-environment inputs
    # -------------------------------------------------------------------------
    for env in ENV_ORDER:
        label = ENV_LABELS[env]
        color = _env_color(env)
        st.sidebar.markdown(
            f"<span style='color:{color}; font-weight:700; font-size:1rem;'>● {label}</span>",
            unsafe_allow_html=True,
        )

        creds = st.session_state["connections"][env]

        with st.sidebar.expander(f"Configure {label}", expanded=(env == "dev")):
            creds["host"]     = st.text_input("Host",     value=creds["host"],     key=f"{env}_host")
            creds["user"]     = st.text_input("Username", value=creds["user"],     key=f"{env}_user")
            creds["password"] = st.text_input("Password", value=creds["password"], key=f"{env}_pass", type="password")
            creds["database"] = st.text_input("Database", value=creds["database"], key=f"{env}_db")
            creds["port"]     = st.number_input("Port",   value=creds["port"],     key=f"{env}_port", min_value=1, max_value=65535)

            if st.button(f"Test {label} Connection", key=f"{env}_test"):
                with st.spinner("Connecting…"):
                    ok, msg = test_connection(env)
                if ok:
                    st.success(f"Connected — {msg}")
                else:
                    st.error(msg)

        st.sidebar.markdown("---")

    # -------------------------------------------------------------------------
    # Save / clear buttons
    # -------------------------------------------------------------------------
    col_save, col_clear = st.sidebar.columns(2)
    if col_save.button("Save Credentials", use_container_width=True, help="Encrypt and save to disk"):
        save_credentials(st.session_state["connections"])
        st.sidebar.success("Saved (encrypted).")

    if col_clear.button("Clear Saved", use_container_width=True, help="Delete saved credentials file"):
        delete_credentials()
        st.sidebar.info("Saved credentials deleted.")

    # -------------------------------------------------------------------------
    # Quick status bar
    # -------------------------------------------------------------------------
    st.sidebar.markdown("**Connection Status**")
    for env in ENV_ORDER:
        label = ENV_LABELS[env]
        creds = st.session_state["connections"][env]
        if creds.get("host") and creds.get("user") and creds.get("database"):
            st.sidebar.caption(f"● {label}: configured")
        else:
            st.sidebar.caption(f"○ {label}: not configured")
