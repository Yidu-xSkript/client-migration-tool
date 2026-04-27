# Data Import Feature — Implementation Plan

## Overview

Port all 13 C# processors from `dataloadincloudx/DataentryFromExcel` into the Streamlit migration
tool as a new **"📥 Data Import"** tab. Each processor reads an Excel file and writes to specific
MySQL tables. The user selects target environment (Dev / QA / Prod) and Client ID before running
any import.

---

## Target Module Structure

```
migration/
  data_import/
    __init__.py          # read_excel_rows + read_excel_preview
    vendor.py
    department.py
    gl_code.py
    approval.py
    approver_gl_code.py
    approver_amount.py
    vendor_department.py
    vendor_gl_default.py
    user_registration.py
    invoice.py

ui/
  data_import.py         # Full Data Import tab UI
  components/
    column_mapper.py     # Reusable interactive column mapper widget
```

---

## Core Design: Interactive Column Mapper

Every client's Excel file has a different layout — different columns for the same field,
different numbers of header rows to skip. The column mapper solves this universally.

### How it works

After uploading a file, the user sees:

```
┌─────────────────────────────────────────────────────────────────┐
│  Sheet: [Sheet1 ▼]    Start Row: [4 ▲▼]                         │
│                                                                   │
│  Raw Preview (rows 1–8):                                         │
│  ┌────┬──────────────┬──────────────┬──────────────┬──────────┐  │
│  │ #  │   Col 1      │   Col 2      │   Col 3      │  Col 4   │  │
│  │ 1  │ Client Name  │ Code         │ Dept Name    │ Status   │  │
│  │ 2  │ (blank)      │ (blank)      │ (blank)      │ (blank)  │  │
│  │ 3  │ (blank)      │ (blank)      │ (blank)      │ (blank)  │  │
│  │ 4* │ Toyota       │ TOY          │ Accounting   │ Active   │  │ ← start row
│  │ 5  │ Toyota       │ TOY          │ Purchasing   │ Active   │  │
│  └────┴──────────────┴──────────────┴──────────────┴──────────┘  │
│  (* = first data row at current start row setting)               │
│                                                                   │
│  Field Mapping:                                                   │
│  ┌────────────────────┬──────────┬──────────────────────────┐    │
│  │ Field              │ Column # │ First value (preview)    │    │
│  ├────────────────────┼──────────┼──────────────────────────┤    │
│  │ Department Name    │  [3 ▲▼]  │  "Accounting"  ✓        │    │
│  └────────────────────┴──────────┴──────────────────────────┘    │
│                                                                   │
│  [  Run Import  ]    [ Dry Run ]                                  │
└─────────────────────────────────────────────────────────────────┘
```

Key behaviors:
- Raw preview always shows the first N rows with actual column numbers (1-based, matching Excel)
- Start row input highlights that row in the preview
- Each field mapping shows the live cell value at `(start_row, col_index)` immediately
- If the value looks wrong, the user adjusts the column index before running
- The Run button is disabled until all required fields have a non-zero column assignment

### Data flow

```
file_bytes
    │
    ▼
read_excel_preview(file_bytes, sheet_index)
    → list[list[any]]   # raw 2D grid, first 20 rows, all columns

column_map = {"dept_name": 3, "start_row": 4, "sheet_index": 0}
    │   ← set by user via render_column_mapper()
    ▼
read_excel_rows(file_bytes, column_map)
    → list[dict]   # [{"dept_name": "Accounting", ...}, ...]
    │   rows keyed by field name, not column index
    ▼
import_departments(conn, rows, client_id, dry_run)
    → {"inserted": 5, "skipped": 2, "errors": []}
```

All processor functions receive **named-key dicts** — they never touch column indices.

---

## Excel Reading API (`migration/data_import/__init__.py`)

```python
def read_excel_preview(
    file_bytes: bytes,
    sheet_index: int = 0,
    max_rows: int = 20,
) -> tuple[list[str], list[list]]:
    """
    Returns (sheet_names, grid).
    sheet_names: list of all sheet names in the workbook.
    grid: first max_rows rows as list of lists, all columns included.
          Cell values are converted to str for display. Empty cells → "".
    """

def read_excel_rows(
    file_bytes: bytes,
    column_map: dict,        # {"field_key": col_index (1-based), ..., "start_row": int, "sheet_index": int}
    required_fields: list[str],
) -> list[dict]:
    """
    Reads the sheet from start_row onward.
    For each non-blank row, builds a dict keyed by field_key using the column_map.
    Skips rows where ALL required_fields cells are empty.
    Returns list of row dicts with field_key → cell value (raw Python type).
    """
```

---

## Column Mapper Component (`ui/components/column_mapper.py`)

```python
def render_column_mapper(
    file_bytes: bytes,
    fields: list[dict],
    widget_key: str,
    default_start_row: int = 2,
    default_sheet_index: int = 0,
) -> dict | None:
    """
    Renders the full interactive mapping UI.

    fields: list of field definitions, e.g.:
      [
        {"key": "dept_name",  "label": "Department Name", "required": True,  "default_col": 3},
        {"key": "client_code","label": "Client Code",     "required": False, "default_col": 0},
      ]

    Returns a column_map dict on success:
      {
        "sheet_index": 0,
        "start_row": 4,
        "dept_name": 3,
        "client_code": 2,
      }
    Returns None if any required field has col=0 (not yet mapped).

    Widget state is stored under st.session_state[widget_key + "_map"].
    """
```

### Render sequence inside `render_column_mapper`:

1. Call `read_excel_preview(file_bytes)` → get `(sheet_names, grid)`
2. Show sheet selector (`st.selectbox`) if len(sheet_names) > 1; else use index 0
3. Show `st.number_input("Start Row", min=1, value=default_start_row)` — call this `start_row`
4. Show the raw preview table using `st.dataframe`:
   - Columns labeled "Col 1", "Col 2", ... up to the max column count in grid
   - Row labels are actual row numbers (1-based)
   - Highlight `start_row` — do this by prepending a "→" marker to that row's first cell display
5. For each field in `fields`:
   - Show one row with three `st.columns([2, 1, 2])`:
     - Col A: field label (+ "required" badge if required)
     - Col B: `st.number_input(f"Col #", min=0, value=field["default_col"], key=widget_key+"_col_"+field["key"])`
     - Col C: if col > 0 and grid has enough columns → show `f'→ "{grid[start_row-1][col-1]}"'` in small text
              if col = 0 → show "not mapped" in grey
              if col > max_col → show "⚠ column out of range" in orange
6. Return the assembled column_map dict, or None if validation fails

---

## Implementation Prompts

---

### PROMPT 1 — Foundation: Module scaffold + Excel utilities

**Goal:** Create the directory scaffold, the Excel read API, and the column mapper component.
The column mapper is the most important piece — get it right before anything else.

**Files to create:**
```
migration/data_import/__init__.py    ← read_excel_preview + read_excel_rows
migration/data_import/vendor.py      ← empty stub: pass
migration/data_import/department.py  ← empty stub: pass
migration/data_import/gl_code.py     ← empty stub: pass
migration/data_import/approval.py    ← empty stub: pass
migration/data_import/approver_gl_code.py  ← empty stub
migration/data_import/approver_amount.py   ← empty stub
migration/data_import/vendor_department.py ← empty stub
migration/data_import/vendor_gl_default.py ← empty stub
migration/data_import/user_registration.py ← empty stub
migration/data_import/invoice.py           ← empty stub
ui/components/__init__.py            ← empty
ui/components/column_mapper.py       ← render_column_mapper (full implementation)
ui/data_import.py                    ← shell only
```

**`migration/data_import/__init__.py`** — implement `read_excel_preview` and `read_excel_rows`
as specified in the Excel Reading API section above. Use `openpyxl` in read-only mode.

**`ui/components/column_mapper.py`** — implement `render_column_mapper` exactly as specified
in the Column Mapper Component section above.

**`ui/data_import.py` shell:**
```python
def render_data_import() -> None:
    st.header("📥 Data Import")
    # Environment + Client ID + CompanyId row
    col1, col2, col3 = st.columns([1, 1, 1])
    with col1:
        env = st.selectbox("Environment", ["dev", "qa", "prod"],
                           format_func=lambda e: {"dev":"Development","qa":"QA / Staging","prod":"Production"}[e],
                           key="di_env")
    with col2:
        client_id = st.number_input("Client ID", min_value=1, step=1, key="di_client_id")
    with col3:
        company_id = st.number_input("Client Company ID", min_value=1, step=1, key="di_company_id")

    if "connections" not in st.session_state or env not in st.session_state.get("connections", {}):
        st.warning(f"Not connected to {env}. Configure connections in the sidebar.")
        return

    tabs = st.tabs(["📦 Vendors", "🏢 Departments & GL Codes", "✅ Approvals", "👤 Users & Invoices"])
    with tabs[0]: st.info("Coming soon — Vendors")
    with tabs[1]: st.info("Coming soon — Departments & GL Codes")
    with tabs[2]: st.info("Coming soon — Approvals")
    with tabs[3]: st.info("Coming soon — Users & Invoices")
```

**`app.py` changes:**
- Add `from ui.data_import import render_data_import`
- Add `"📥 Data Import"` tab at position 1 (after Dashboard, before Compare)
- Wire `render_data_import()` in its block

**Check `requirements.txt`** — add `openpyxl` if not present.

---

### PROMPT 2 — Vendor Import (`migration/data_import/vendor.py`)

**Goal:** Port `VendorAddressVendorDeptMigration` from Program.cs.

**Field keys** this processor expects in each row dict:
```
vendor_no, vendor_name, street_name, city, state_short,
zipcode, phone, contact, vendor_type, address2, email
```

**Function signature:**
```python
def import_vendors(
    conn,
    rows: list[dict],         # field-keyed dicts from read_excel_rows
    client_id: int,
    client_company_id: int,
    company_code: str,
    dry_run: bool = False,
) -> tuple[dict, list[dict]]:
    # dict: {"inserted": int, "updated": int, "skipped": int, "errors": list[str]}
    # list[dict]: row-level log [{"row": int, "action": str, "vendor_no": str, "vendor_name": str, "detail": str}]
```

**Internal helpers (module-private):**
```python
def _get_state_id(conn, state_short: str) -> int | None
def _get_vendor(conn, vendor_no: str, client_id: int, client_company_id: int) -> tuple[int, int] | None
    # returns (vendor_id, address_id) or None
def _insert_address(conn, cur, row: dict, state_id: int) -> int
def _update_address(conn, cur, address_id: int, row: dict, state_id: int)
def _insert_vendor(conn, cur, row: dict, client_id: int, client_company_id: int, company_code: str, address_id: int) -> int
```

**Logic:**
- Pre-load states dict: `{state_short: state_id}` from State table once before the loop
- For each row: look up vendor → insert or update as described in the C# source
- Wrap all writes in one transaction; rollback on any exception
- Dry run: skip all writes, still compute what would happen
- Zipcode `"#VALUE!"` → store as empty string
- All SQL parameterized — no f-strings in queries

**Tables:** Address (write), Vendor (write), State (read)

**Default field values for INSERT Vendor:**
- IsActive = 1
- CompanyCode = company_code param
- ClientCompanyId = client_company_id param
- ClientId = client_id param

---

### PROMPT 3 — Department Loader (`migration/data_import/department.py`)

**Field key:** `dept_name`

**Function signature:**
```python
def import_departments(
    conn,
    rows: list[dict],
    client_id: int,
    dry_run: bool = False,
) -> tuple[dict, list[dict]]:
    # dict: {"inserted": int, "skipped": int, "errors": list[str]}
    # list[dict]: [{"row": int, "action": str, "dept_name": str}]
```

**Logic:**
- For each row: check if `(DepartmentName, ClientId)` exists in Department
- If not exists → INSERT Department (DepartmentName, ClientId, IsActive=True)
- All INSERTs in one transaction
- Idempotent: duplicates are "skipped", not errors

**Tables:** Department

---

### PROMPT 4 — GL Code Loader (`migration/data_import/gl_code.py`)

**Two modes: flat and split.**

**Flat mode field keys:** `gl_code_name`, `description`

**Split mode field keys:** `parent_gl_name`, `description`, `child_gl_name`, `percentage`
(Parent rows have `parent_gl_name` set and `child_gl_name` empty; child rows are the reverse)

**Function signature:**
```python
def import_gl_codes(
    conn,
    rows: list[dict],
    client_id: int,
    client_company_id: int,
    mode: str = "flat",     # "flat" | "split"
    dry_run: bool = False,
) -> tuple[dict, list[dict]]:
    # dict: {"inserted": int, "updated": int, "skipped": int, "errors": list[str]}
```

**Flat logic:**
- For each row: GetExistingGLCodeId(ClientId, GLCodeName, ClientCompanyId)
  → update if found, insert if not

**Split logic (state machine):**
- Track `current_parent_id` across rows
- Row with `parent_gl_name` set: insert/update parent GLCode (no SourceGLCodeId, no Percentage)
  → set `current_parent_id`
- Row with `child_gl_name` set: insert/update child GLCode with SourceGLCodeId=current_parent_id,
  Percentage=percentage/100.0
- ClientCompanyId for split mode: accept as param (from UI) — do NOT derive from GL code name

**Column name quirk:** GLCode table uses `Desccription` (two c's) — use exact spelling in SQL.

**Tables:** GLCode

---

### PROMPT 5 — Approval Sub-Step User Mappings (`migration/data_import/approval.py`)

**Four functions, each with its own field keys:**

**`import_approval_substep_users`**
- Field keys: `vendor_no`, `approval_one_email`, `approval_two_email`, `max_amount`
- Logic: for each unique email → GetExistingUserId → check exists → insert ApprovalSubStepUser
- Lookup: GetApprovalSubStepId(conn, client_id, company_code, substep_name)

**`import_approval_user_vendors`**
- Field keys: `vendor_no`, `user_email`, `approval_substep_id` (or pass as param)
- Logic: GetUserId → GetVendorId(VendorNo, ClientId) → insert if not exists

**`import_approval_user_departments`**
- Field keys: `dept_name`, `user_email`
- Logic: GetUserId → GetDepartmentId(DeptName, ClientId) → insert if not exists

**`import_approval_user_vendor_departments`**
- Field keys: `user_id` (guid string), `approval_substep_id`, `department_id`, `vendor_id`
- Logic: parse UserId as UUID → CheckExistence → insert if new

**All four function signatures:**
```python
def import_approval_substep_users(
    conn, rows: list[dict], client_id: int, company_code: str,
    substep_name: str, dry_run: bool = False
) -> tuple[dict, list[dict]]

def import_approval_user_vendors(
    conn, rows: list[dict], client_id: int, approval_substep_id: int,
    dry_run: bool = False
) -> tuple[dict, list[dict]]

def import_approval_user_departments(
    conn, rows: list[dict], client_id: int, approval_substep_id: int,
    dry_run: bool = False
) -> tuple[dict, list[dict]]

def import_approval_user_vendor_departments(
    conn, rows: list[dict], client_id: int,
    dry_run: bool = False
) -> tuple[dict, list[dict]]
```

**UserId handling:**
- MySQL stores UserId as binary(16). When reading: `uuid.UUID(bytes=row["UserId"]).hex`
- When querying by email: `SELECT UserId FROM User WHERE (UserName=%s OR Email=%s) AND ClientId=%s AND IsActive=1`

**Tables:** ApprovalSubStepUser, ApprovalSubStepUserVendor, ApprovalSubStepUserDepartment,
ApprovalSubStepUserVendorDepartment, User (read), Vendor (read), Department (read),
ApprovalSubStep + ApprovalStep (read), ClientCompany (read)

---

### PROMPT 6 — Approver GL Code (`migration/data_import/approver_gl_code.py`)

**Field keys:** `user_email`, `gl_code_name`, `action` (optional — "INSERT"/"DELETE", blank=INSERT)

**Function signature:**
```python
def import_approver_gl_codes(
    conn,
    rows: list[dict],
    client_id: int,
    mode: str = "process",    # "process" | "sync" | "insert"
    dry_run: bool = False,
) -> tuple[dict, list[dict]]:
    # dict: {"inserted": int, "deleted": int, "skipped": int, "errors": list[str]}
```

**Internal pre-loaders:**
```python
def _load_gl_code_lookup(conn, client_id) -> dict[str, int]       # name → GLCodeId
def _load_user_lookup(conn, client_id) -> dict[str, bytes]        # email/username → UserId bytes
def _load_existing_pairs(conn, client_id) -> set[tuple]           # {(gl_code_id, user_id_bytes)}
```

**Modes:**
- `process`: classify each row as INSERT or DELETE per `action` field; batch both
- `sync`: desired = set from Excel; current = loaded pairs; toInsert = desired-current; toDelete = current-desired
- `insert`: insert-only, skip existing

**Batch insert:** `INSERT IGNORE INTO ApproverGLCode (GLCodeId, UserId) VALUES (%s,%s),...` — 500 per chunk
**Batch delete:** `DELETE FROM ApproverGLCode WHERE (GLCodeId=%s AND UserId=%s) OR ...` — 500 per chunk

**Tables:** ApproverGLCode, GLCode (read), User (read)

---

### PROMPT 7 — Approver By Amount (`migration/data_import/approver_amount.py`)

**Field keys:** `first_approver_email`, `second_approver_email`, `max_amount`

**Function signature:**
```python
def import_approver_amounts(
    conn,
    rows: list[dict],
    client_id: int,
    client_company_id: int,
    dry_run: bool = False,
) -> tuple[dict, list[dict]]:
```

**Logic:**
- For each row: GetUserId(first_approver_email) → GetUserId(second_approver_email) → parse max_amount as Decimal
- Check if record exists (UserId, ClientCompanyId) in ApproverByAmount
- If not → INSERT (SecondApproverId, UserId, MaximumAllowedAmount, ClientCompanyId)

**Tables:** ApproverByAmount, User (read)

---

### PROMPT 8 — Vendor Department (`migration/data_import/vendor_department.py`)

**Field keys:** `vendor_no`, `vendor_name`, `dept_name`

**Function signature:**
```python
def import_vendor_departments(
    conn,
    rows: list[dict],
    client_id: int,
    dry_run: bool = False,
) -> tuple[dict, list[dict]]:
```

**Logic:**
- For each row: GetVendorId(VendorNo, VendorName, ClientId) + GetDepartmentId(DeptName, ClientId)
- If both found: `INSERT IGNORE INTO VendorDepartment (VendorId, DepartmentId) VALUES (%s, %s)`
- MySQL error 1062 → treat as skipped (already linked)

**Tables:** VendorDepartment, Vendor (read), Department (read)

---

### PROMPT 9 — Vendor GL Default (`migration/data_import/vendor_gl_default.py`)

**Field keys:** `vendor_no`, `vendor_name`, `gl_code_name`

**Function signature:**
```python
def import_vendor_gl_defaults(
    conn,
    rows: list[dict],
    client_id: int,
    dry_run: bool = False,
) -> tuple[dict, list[dict]]:
```

**Logic per row:**
1. GetGlCodeId(GLCodeName, ClientId) → skip row if not found
2. GetVendorId(VendorNo, VendorName, ClientId) → skip row if not found
3. VendorGlDefaultExists(ClientId, VendorId, GlCodeId) → INSERT VendorGlDefault if not exists
4. UPDATE Vendor SET DefaultGlCodeId=%s WHERE VendorId=%s AND ClientId=%s

**Tables:** VendorGlDefault, Vendor (write), GLCode (read)

---

### PROMPT 10 — User Registration (`migration/data_import/user_registration.py`)

**Field keys:** `first_name`, `last_name`, `email`

**Function signature:**
```python
def import_users(
    conn,
    rows: list[dict],
    client_id: int,
    default_address_id: int | None = None,
    dry_run: bool = False,
) -> tuple[dict, list[dict]]:
```

**Logic per row:**
- Check EmailExists(email, ClientId) → skip if already registered
- INSERT User:
  - UserId = `uuid.uuid4().bytes` (store as binary 16)
  - UserName = Email
  - Password = `b""` (empty — user sets it themselves post-import)
  - IsActive = 1
  - All notification flags = 0
  - RoleId = 1
  - AddressId = default_address_id or NULL
- INSERT UserRoles: (UserId, RoleId=1), (UserId, RoleId=5)
- Both inserts in one mini-transaction per user

**Tables:** User, UserRoles

---

### PROMPT 11 — Invoice Lookup (`migration/data_import/invoice.py`)

**Field keys:** `invoice_no`, `vendor_no`, `invoice_total`

**Function signature:**
```python
def lookup_invoices(
    conn,
    rows: list[dict],
    client_id: int,
    year: int,
) -> pd.DataFrame:
    # columns: InvoiceId, InvoiceNo, InvoiceTotal, ScannedDate, VendorName, VendorNo
```

**Logic:**
- For each row: query Invoice JOIN Vendor WHERE ClientId=%s AND InvoiceNo=%s AND VendorNo=%s
  AND YEAR(ScannedDate)=%s AND InvoiceType='Invoice'
- Collect all results into a single DataFrame (dedup by InvoiceId)
- Read-only — no writes

**Tables:** Invoice (read), Vendor (read)

---

### PROMPT 12 — Full UI (`ui/data_import.py`)

**Goal:** Replace the shell from Prompt 1 with the complete wired UI.
Every expander follows the same pattern:

```
[expander opens]
  st.file_uploader(.xlsx)
  if file uploaded:
    render_column_mapper(file_bytes, fields, widget_key=f"di_{proc}_mapper")
    if mapper returns a column_map (all required fields mapped):
      [additional config inputs specific to processor]
      dry_run_toggle
      run_button
      if run_button:
        rows = read_excel_rows(file_bytes, column_map, required_fields)
        summary, log = import_xxx(conn, rows, ...)
        show st.metric cols: Inserted / Updated / Skipped / Errors
        if log: show st.dataframe(log[:200])
        if summary["errors"]: show st.error for each
```

**Tab: "📦 Vendors"**
- Expander "Vendor Address Import"
  - Fields: vendor_no(req), vendor_name(req), street_name, city, state_short, zipcode, phone, contact, vendor_type, address2, email
  - Inputs: company_code (text), dry_run
  - Calls: `import_vendors`
- Expander "Vendor Department Links"
  - Fields: vendor_no(req), vendor_name(req), dept_name(req)
  - Calls: `import_vendor_departments`

**Tab: "🏢 Departments & GL Codes"**
- Expander "Department Import"
  - Fields: dept_name(req)
  - Calls: `import_departments`
- Expander "GL Code Import — Flat"
  - Fields: gl_code_name(req), description
  - Calls: `import_gl_codes(mode="flat")`
- Expander "GL Code Import — Split / Hierarchical"
  - Fields: parent_gl_name, description, child_gl_name, percentage
  - Calls: `import_gl_codes(mode="split")`
- Expander "Vendor GL Defaults"
  - Fields: vendor_no(req), vendor_name(req), gl_code_name(req)
  - Calls: `import_vendor_gl_defaults`

**Tab: "✅ Approvals"**
- Expander "Approval Sub-Step Users"
  - Fields: vendor_no, approval_one_email(req), approval_two_email, max_amount
  - Extra inputs: company_code, substep_name (text, default "Sub Step 1 ( PO)")
  - Calls: `import_approval_substep_users` + `import_approver_amounts` (same file, checkbox to also run amounts)
- Expander "Approval User → Vendor"
  - Fields: vendor_no(req), user_email(req)
  - Extra input: approval_substep_id (number)
  - Calls: `import_approval_user_vendors`
- Expander "Approval User → Department"
  - Fields: dept_name(req), user_email(req)
  - Extra input: approval_substep_id (number)
  - Calls: `import_approval_user_departments`
- Expander "Approval User → Vendor + Department (4-way)"
  - Fields: user_id(req), approval_substep_id(req), department_id(req), vendor_id(req)
  - Calls: `import_approval_user_vendor_departments`
- Expander "Approver GL Codes"
  - Fields: user_email(req), gl_code_name(req), action
  - Mode radio: Process / Sync / Insert
  - Calls: `import_approver_gl_codes`
- Expander "Approver By Amount"
  - Fields: first_approver_email(req), second_approver_email(req), max_amount(req)
  - Calls: `import_approver_amounts`

**Tab: "👤 Users & Invoices"**
- Expander "User Registration"
  - Fields: first_name(req), last_name(req), email(req)
  - Extra input: default_address_id (optional number)
  - Calls: `import_users`
- Expander "Invoice Lookup"
  - Fields: invoice_no(req), vendor_no(req), invoice_total
  - Extra input: year (number_input, default current year)
  - Read-only: no dry_run toggle
  - Calls: `lookup_invoices`
  - Shows: `st.dataframe` + `st.download_button` (CSV export)

**UI conventions:**
- `st.metric` for counts (Inserted / Updated / Skipped / Errors) in a 4-column row
- `st.dataframe` for row-level log, `height=300`, capped at 200 rows
- `st.success` / `st.error` / `st.warning` for top-level status
- Connection via `st.session_state["connections"][env]`
- No business logic — all calls go through `migration/data_import/` functions

---

## Dependency Order

| # | Prompt | Depends on |
|---|--------|-----------|
| 1 | Foundation + column mapper | — |
| 2–11 | Processors (any order) | 1 |
| 12 | Full UI | 1–11 |

Prompts 2–11 are independent of each other and can be implemented in any order.

---

## Schema Notes

Always verify column names in `schema.json` before writing SQL. Known quirks:
- `GLCode.Desccription` — two c's (typo baked into schema)
- `User.UserId` — `binary(16)`; read as bytes, convert with `uuid.UUID(bytes=val)`
- `ApprovalSubStepUserVendorDepartment` — no auto-increment PK, composite
- `VendorDepartment` — composite key (VendorId, DepartmentId)
- `UserRoles` — columns: UserId, RoleId

---

## What NOT to port

- Hardcoded ClientIds / ClientCompanyIds from the C# — all become UI inputs
- Hardcoded file paths — replaced by `st.file_uploader`
- Console.WriteLine — replaced by return dict + st.success/st.error
- Hardcoded password hash in UserRegistration — store empty bytes
- SQL injection in `InsertVendorDepartment` — all SQL parameterized
- Disk-export helpers — replaced by `st.download_button`
- `ApproverGLCode1.cs` — use optimized `ApproverGLCode.cs` only
