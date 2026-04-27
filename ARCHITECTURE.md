# ARCHITECTURE.md — Client Migration Tool

## System Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                        Streamlit Frontend                        │
│  ┌──────────┐  ┌──────────┐  ┌────────────┐  ┌──────────────┐  │
│  │ Sidebar  │  │ Compare  │  │  Migrate   │  │   Settings   │  │
│  │(creds)   │  │  Tab     │  │  Tabs(2)   │  │    Tab       │  │
│  └────┬─────┘  └────┬─────┘  └─────┬──────┘  └──────┬───────┘  │
└───────┼─────────────┼──────────────┼─────────────────┼──────────┘
        │             │              │                 │
        ▼             ▼              ▼                 ▼
┌───────────────────────────────────────────────────────────────────┐
│                         Business Logic                            │
│  ┌──────────────────┐          ┌──────────────────────────────┐   │
│  │  db/connection   │          │     migration/engine         │   │
│  │  ConnectionMgr   │          │  MigrationEngine             │   │
│  └──────┬───────────┘          │  - dry_run()                 │   │
│         │                      │  - run()                     │   │
│  ┌──────▼───────────┐          │  - _migrate_table()          │   │
│  │  db/discovery    │          └──────────────┬───────────────┘   │
│  │  - discover_     │                         │                   │
│  │    related_tables│          ┌──────────────▼───────────────┐   │
│  │  - topo_sort     │          │     migration/backup         │   │
│  └──────┬───────────┘          │  - create_backup_table()     │   │
│         │                      └──────────────────────────────┘   │
│  ┌──────▼───────────┐                                             │
│  │  db/operations   │          ┌──────────────────────────────┐   │
│  │  - read_client   │          │     migration/audit          │   │
│  │  - batch_insert  │          │  - log_attempt()             │   │
│  │  - delete_client │          │  - read_log()                │   │
│  └──────────────────┘          └──────────────────────────────┘   │
└───────────────────────────────────────────────────────────────────┘
        │
        ▼
┌───────────────────────────────┐
│  MySQL (3 environments)       │
│  ┌───────┐ ┌──────┐ ┌──────┐  │
│  │  Dev  │ │  QA  │ │ Prod │  │
│  └───────┘ └──────┘ └──────┘  │
└───────────────────────────────┘
```

## Data Flow: Migration

```
User selects ClientId + options
        │
        ▼
discovery.discover_related_tables(client_id, src_conn)
        │ Returns: List[TableInfo] in topological order
        ▼
User selects subset of tables (multi-select)
        │
        ▼
engine.dry_run(client_id, tables, src_conn, dst_conn)   [if preview]
        │ Returns: Dict[table → {delete_count, insert_rows}]
        ▼
User confirms + (for Prod) types ID + "PROD"
        │
        ▼
backup.create_backup_tables(client_id, tables, dst_conn) [if enabled]
        │
        ▼
engine.run(client_id, tables, src_conn, dst_conn)
        │  Per table, in topo order:
        │  1. SET FOREIGN_KEY_CHECKS=0 (scoped to session)
        │  2. DELETE FROM dst WHERE ClientId = ?
        │  3. Batch INSERT from src
        │  4. SET FOREIGN_KEY_CHECKS=1
        │  5. Emit progress callback → UI progress bar
        │
        ▼
audit.log_attempt(...)
        │
        ▼
st.balloons() / st.toast() + result table
```

## Module Responsibilities

### `app.py`
- Streamlit page config, layout, tab creation
- Imports and calls render functions from `ui/`
- Zero business logic

### `config.py`
- `BATCH_SIZE = 1000`
- `ENV_LABELS = {"dev": "Development", "qa": "QA / Staging", "prod": "Production"}`
- `AUDIT_LOG_PATH = "migration_audit.log"`
- `DEFAULT_PORT = 3306`

### `db/connection.py`
- `ConnectionManager` class
- `get_connection(env: str) -> pymysql.Connection` — reads creds from session state
- `test_connection(env: str) -> tuple[bool, str]` — for health checks
- Connections are short-lived (open → use → close) — not pooled (single-user app)

### `db/discovery.py`
- `discover_related_tables(client_id, conn) -> list[TableInfo]`
  - Phase 1: FK scan via `INFORMATION_SCHEMA.KEY_COLUMN_USAGE` + `REFERENTIAL_CONSTRAINTS`
  - Phase 2: Column name fallback — find all tables with a `ClientId` column
  - Phase 3: Topological sort by FK dependencies (parents before children)
- `TableInfo` dataclass: `{name, client_id_column, row_count, fk_parents}`

### `db/operations.py`
- `get_client_row_counts(client_id, tables, conn) -> dict[str, int]`
- `read_client_data(client_id, table, column, conn) -> list[dict]`
- `batch_insert(rows, table, conn, batch_size) -> int`
- `delete_client_data(client_id, table, column, conn) -> int`
- `search_clients(query, conn) -> list[dict]` — searches name/email/company

### `migration/engine.py`
- `MigrationEngine(src_conn, dst_conn, client_id, tables, options)`
- `dry_run() -> DryRunResult` — reads source + target counts, no writes
- `run(progress_callback) -> MigrationResult` — full transactional migration
- Conflict modes: `replace` | `skip` | `update`

### `migration/backup.py`
- `create_backup(client_id, tables, conn) -> list[str]` — returns backup table names
- Uses `CREATE TABLE backup_name AS SELECT * FROM src WHERE ClientId = ?`

### `migration/audit.py`
- `log_attempt(entry: AuditEntry)` — appends JSON line to `migration_audit.log`
- `read_recent(n=50) -> list[AuditEntry]` — reads last N entries for display

## State Management

All mutable app state lives in `st.session_state`:

```python
st.session_state = {
    "connections": {
        "dev":  {"host": "", "user": "", "password": "", "database": "", "port": 3306},
        "qa":   {...},
        "prod": {...},
    },
    "compare_results": None,        # DataFrame from last comparison
    "migration_log": [],            # List of log lines for current session
    "last_discovered_tables": [],   # TableInfo list from last discovery
}
```

## Error Handling Strategy

| Layer | Strategy |
|-------|----------|
| DB connection | `try/except pymysql.Error` → return `(False, error_msg)` |
| Migration | Single try/except around full transaction, `conn.rollback()` on any error |
| Discovery | Gracefully skip tables with permission errors, log warning |
| UI | `st.error()` for user-facing errors, never show raw tracebacks |

## Security Considerations
- Credentials stored only in `st.session_state` (in-memory, session-scoped)
- All SQL uses parameterized queries (`%s` placeholders with pymysql)
- Table names in dynamic SQL are validated against the discovered table list (whitelist)
- No credentials written to disk, logs, or audit file
- Prod migration has multi-step confirmation gate
