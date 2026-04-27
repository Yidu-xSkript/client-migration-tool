from __future__ import annotations
import streamlit as st
import pandas as pd
from migration.data_import import read_excel_preview


@st.cache_data(show_spinner=False)
def _cached_preview(file_bytes: bytes, sheet_index: int) -> tuple[list[str], list[list]]:
    return read_excel_preview(file_bytes, sheet_index=sheet_index)


def render_column_mapper(
    file_bytes: bytes,
    fields: list[dict],
    widget_key: str,
    default_start_row: int = 2,
    default_sheet_index: int = 0,
) -> dict | None:
    """
    Renders the interactive column mapping UI.

    fields: list of dicts with keys:
      key (str)          – field identifier used in returned column_map
      label (str)        – display label
      required (bool)    – if True, col must be > 0 to enable Run
      default_col (int)  – pre-filled column index suggestion (0 = not mapped)

    Returns a column_map dict when all required fields are mapped:
      {"sheet_index": 0, "start_row": 4, "field_key": col_index, ...}
    Returns None if any required field has col = 0.
    """
    # ── Get initial sheet names (always from sheet 0 for the list) ──
    sheet_names_0, _ = _cached_preview(file_bytes, 0)

    # ── Sheet selector ──────────────────────────────────────────────
    sheet_key = f"{widget_key}_sheet"
    if len(sheet_names_0) > 1:
        sheet_idx = st.selectbox(
            "Sheet",
            options=list(range(len(sheet_names_0))),
            format_func=lambda i: sheet_names_0[i],
            index=min(default_sheet_index, len(sheet_names_0) - 1),
            key=sheet_key,
        )
    else:
        sheet_idx = 0
        st.session_state[sheet_key] = 0

    # ── Load preview for selected sheet ────────────────────────────
    try:
        _, grid = _cached_preview(file_bytes, sheet_idx)
    except Exception as exc:
        st.error(f"Could not read file: {exc}")
        return None

    if not grid:
        st.warning("The uploaded file appears to be empty.")
        return None

    max_col = max((len(row) for row in grid), default=0)

    # ── Start row ──────────────────────────────────────────────────
    start_key = f"{widget_key}_start_row"
    c1, c2 = st.columns([1, 3])
    with c1:
        start_row = st.number_input(
            "Start Row",
            min_value=1,
            max_value=max(len(grid), 1),
            value=st.session_state.get(start_key, default_start_row),
            step=1,
            key=start_key,
            help="First data row (rows above this are treated as headers and skipped)",
        )
    with c2:
        st.caption(
            f"Preview shows {len(grid)} rows · {max_col} columns detected"
        )

    # ── Raw preview table ──────────────────────────────────────────
    with st.expander("Raw Preview", expanded=True):
        if grid and max_col > 0:
            display_cols = min(max_col, 50)
            col_labels = [f"Col {i + 1}" for i in range(display_cols)]
            padded = [
                (row + [""] * display_cols)[:display_cols] for row in grid
            ]
            df_preview = pd.DataFrame(
                padded,
                columns=col_labels,
                index=range(1, len(padded) + 1),
            )
            df_preview.index.name = "Row"

            def _highlight(row):
                if row.name == int(start_row):
                    return ["background-color: #d4edda; font-weight: bold"] * len(row)
                return [""] * len(row)

            st.dataframe(
                df_preview.style.apply(_highlight, axis=1),
                height=220,
                use_container_width=True,
            )

    # ── Field mapping ───────────────────────────────────────────────
    st.markdown("**Field Mapping** — enter the column number (1-based) for each field")

    h1, h2, h3 = st.columns([2, 1, 2])
    with h1:
        st.caption("Field")
    with h2:
        st.caption("Col #")
    with h3:
        st.caption("First value at start row")

    all_required_mapped = True
    first_data_row_idx = int(start_row) - 1  # 0-based index into grid

    for field in fields:
        key = field["key"]
        label = field["label"]
        required = field.get("required", False)
        default_col = field.get("default_col", 0)
        col_key = f"{widget_key}_col_{key}"

        c1, c2, c3 = st.columns([2, 1, 2])
        with c1:
            suffix = " \\*" if required else ""
            st.markdown(f"{label}{suffix}")
        with c2:
            col_val = st.number_input(
                "col",
                min_value=0,
                max_value=max_col if max_col > 0 else 9999,
                value=int(st.session_state.get(col_key, default_col)),
                step=1,
                key=col_key,
                label_visibility="collapsed",
            )
        with c3:
            if col_val == 0:
                st.caption("_(not mapped)_")
            elif col_val > max_col:
                st.warning(f"Col {col_val} > file width ({max_col})", icon="⚠️")
            elif 0 <= first_data_row_idx < len(grid):
                row_data = grid[first_data_row_idx]
                cell_val = row_data[col_val - 1] if col_val - 1 < len(row_data) else ""
                if cell_val:
                    st.markdown(f"`{cell_val[:60]}`")
                else:
                    st.caption("_(blank at this row)_")
            else:
                st.caption("_(row out of range)_")

        if required and int(st.session_state.get(col_key, default_col)) == 0:
            all_required_mapped = False

    if not all_required_mapped:
        st.info("Assign a column number to all required fields (\\*) to enable import.", icon="ℹ️")
        return None

    # ── Assemble and return column_map ──────────────────────────────
    column_map: dict = {
        "sheet_index": int(sheet_idx),
        "start_row": int(start_row),
    }
    for field in fields:
        k = field["key"]
        col_key = f"{widget_key}_col_{k}"
        v = int(st.session_state.get(col_key, field.get("default_col", 0)))
        if v > 0:
            column_map[k] = v

    return column_map
