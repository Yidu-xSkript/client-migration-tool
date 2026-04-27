# config.py — App-wide constants and configuration

BATCH_SIZE = 1000           # Rows per batch insert
DELTA_CHECKSUM_BATCH = 5000 # Rows per batch when computing checksums

DEFAULT_PORT = 3306

AUDIT_LOG_PATH = "migration_audit.log"
EDIT_LOG_PATH  = "edit_audit.log"
PROFILES_FILE  = "profiles.json"
CREDS_FILE     = ".migration_creds.enc"
KEY_FILE       = ".migration.key"
BACKUP_DIR     = "backups"      # Root folder; subfolders per env (qa/, prod/)

ENV_LABELS = {
    "dev":  "Development",
    "qa":   "QA / Staging",
    "prod": "Production",
}

ENV_ORDER = ["dev", "qa", "prod"]

MIGRATION_ROUTES = [
    {"src": "dev",  "dst": "qa",   "label": "Dev → QA"},
    {"src": "qa",   "dst": "prod", "label": "QA → Prod"},
]

CLIENT_SEARCH_COLUMNS = [
    "Name", "ClientName",
    "Email", "EmailAddress",
    "Company", "CompanyName",
    "Phone", "PhoneNumber",
]

CLIENT_TABLE     = "Client"
CLIENT_ID_COLUMN = "ClientId"

AUDIT_LOG_DISPLAY_LIMIT = 50
PREVIEW_ROW_SAMPLE = 10    # Max rows fetched per table in dry run preview
DASHBOARD_CLIENT_LIMIT  = 500   # Max clients shown in Drift Dashboard
BACKUP_RETENTION_DAYS   = 30

# Row-filter keywords that are never permitted (regardless of context)
FORBIDDEN_FILTER_KEYWORDS = {
    "DROP", "DELETE", "INSERT", "UPDATE", "CREATE", "ALTER",
    "TRUNCATE", "EXEC", "EXECUTE", "GRANT", "REVOKE",
}
