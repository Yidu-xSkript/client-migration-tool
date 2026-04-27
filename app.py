# app.py — Client Migration Tool entry point
# Tab routing only — no business logic lives here.

import streamlit as st

st.set_page_config(
    page_title="Client Migration Tool",
    page_icon="🔄",
    layout="wide",
    initial_sidebar_state="expanded",
)

from ui.sidebar import render_sidebar
from ui.dashboard import render_dashboard
from ui.compare import render_compare
from ui.migrate import render_migration_tab
from ui.batch import render_batch
from ui.clone import render_clone
from ui.client_setup import render_client_setup
from ui.settings import render_settings
from ui.data_import import render_data_import


def main() -> None:
    render_sidebar()

    st.title("🔄 Client Migration Tool")
    st.caption(
        "Migrate client data between Dev, QA, and Production MySQL environments. "
        "All migrations are atomic, audited, and reversible via backups."
    )

    tabs = st.tabs([
        "📊 Sync Dashboard",
        "🔍 Compare",
        "🚀 Migrate Dev → QA",
        "🚀 Migrate QA → Prod",
        "📦 Batch Migration",
        "📋 Copy from Client",
        "🛠️ Client Setup",
        "📥 Data Import",
        "⚙️ Settings",
    ])

    with tabs[0]:
        render_dashboard()

    with tabs[1]:
        render_compare()

    with tabs[2]:
        render_migration_tab(src_env="dev", dst_env="qa")

    with tabs[3]:
        render_migration_tab(src_env="qa", dst_env="prod")

    with tabs[4]:
        render_batch()

    with tabs[5]:
        render_clone()

    with tabs[6]:
        render_client_setup()

    with tabs[7]:
        render_data_import()

    with tabs[8]:
        render_settings()


if __name__ == "__main__":
    main()
