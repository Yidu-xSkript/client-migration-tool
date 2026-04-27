"""
Connects to the production database and exports the full schema to schema.json.
Run once (or whenever the schema changes) to refresh the reference file.
"""
import json
import sys
import pymysql
import pymysql.cursors

HOST = "apsmart-main-db-mysql8.c6jssxf3a4wt.us-east-1.rds.amazonaws.com"
DB   = "APSHTML"
USER = "cloudx_dev"
PASS = "A$!%~J2E?|@4Xh6"
PORT = 3306

OUTPUT = "schema.json"


def fetch_schema():
    conn = pymysql.connect(
        host=HOST, port=PORT, user=USER, password=PASS,
        database=DB, cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=10,
    )
    schema = {"database": DB, "tables": {}}

    with conn:
        with conn.cursor() as cur:
            # ── 1. All tables ────────────────────────────────────────────────
            cur.execute("""
                SELECT TABLE_NAME, TABLE_COMMENT, ENGINE, TABLE_ROWS,
                       AUTO_INCREMENT, CREATE_TIME, UPDATE_TIME
                FROM information_schema.TABLES
                WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'BASE TABLE'
                ORDER BY TABLE_NAME
            """, (DB,))
            tables = cur.fetchall()

            for row in tables:
                tname = row["TABLE_NAME"]
                schema["tables"][tname] = {
                    "meta": {
                        "engine":       row["ENGINE"],
                        "approx_rows":  row["TABLE_ROWS"],
                        "auto_inc":     row["AUTO_INCREMENT"],
                        "comment":      row["TABLE_COMMENT"],
                    },
                    "columns":     [],
                    "primary_key": [],
                    "indexes":     [],
                    "foreign_keys_out": [],   # FK this table → other tables
                    "foreign_keys_in":  [],   # FK other tables → this table
                }

            # ── 2. Columns ───────────────────────────────────────────────────
            cur.execute("""
                SELECT TABLE_NAME, COLUMN_NAME, ORDINAL_POSITION,
                       COLUMN_DEFAULT, IS_NULLABLE, DATA_TYPE,
                       CHARACTER_MAXIMUM_LENGTH, NUMERIC_PRECISION,
                       NUMERIC_SCALE, COLUMN_TYPE, COLUMN_KEY,
                       EXTRA, COLUMN_COMMENT
                FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = %s
                ORDER BY TABLE_NAME, ORDINAL_POSITION
            """, (DB,))
            for col in cur.fetchall():
                t = col["TABLE_NAME"]
                if t not in schema["tables"]:
                    continue
                schema["tables"][t]["columns"].append({
                    "name":      col["COLUMN_NAME"],
                    "type":      col["COLUMN_TYPE"],
                    "nullable":  col["IS_NULLABLE"] == "YES",
                    "default":   col["COLUMN_DEFAULT"],
                    "key":       col["COLUMN_KEY"],
                    "extra":     col["EXTRA"],
                    "comment":   col["COLUMN_COMMENT"],
                })
                if col["COLUMN_KEY"] == "PRI":
                    schema["tables"][t]["primary_key"].append(col["COLUMN_NAME"])

            # ── 3. Indexes ───────────────────────────────────────────────────
            cur.execute("""
                SELECT TABLE_NAME, INDEX_NAME, NON_UNIQUE,
                       SEQ_IN_INDEX, COLUMN_NAME, INDEX_TYPE
                FROM information_schema.STATISTICS
                WHERE TABLE_SCHEMA = %s
                ORDER BY TABLE_NAME, INDEX_NAME, SEQ_IN_INDEX
            """, (DB,))
            idx_map = {}
            for row in cur.fetchall():
                t = row["TABLE_NAME"]
                if t not in schema["tables"]:
                    continue
                key = (t, row["INDEX_NAME"])
                if key not in idx_map:
                    idx_map[key] = {
                        "name":    row["INDEX_NAME"],
                        "unique":  not row["NON_UNIQUE"],
                        "type":    row["INDEX_TYPE"],
                        "columns": [],
                    }
                idx_map[key]["columns"].append(row["COLUMN_NAME"])
            for (t, _), idx in idx_map.items():
                schema["tables"][t]["indexes"].append(idx)

            # ── 4. Foreign keys ──────────────────────────────────────────────
            cur.execute("""
                SELECT
                    kcu.TABLE_NAME       AS src_table,
                    kcu.COLUMN_NAME      AS src_col,
                    kcu.CONSTRAINT_NAME  AS fk_name,
                    kcu.REFERENCED_TABLE_NAME  AS ref_table,
                    kcu.REFERENCED_COLUMN_NAME AS ref_col,
                    rc.UPDATE_RULE, rc.DELETE_RULE
                FROM information_schema.KEY_COLUMN_USAGE kcu
                JOIN information_schema.REFERENTIAL_CONSTRAINTS rc
                  ON rc.CONSTRAINT_NAME   = kcu.CONSTRAINT_NAME
                 AND rc.CONSTRAINT_SCHEMA = kcu.TABLE_SCHEMA
                WHERE kcu.TABLE_SCHEMA            = %s
                  AND kcu.REFERENCED_TABLE_NAME  IS NOT NULL
                ORDER BY kcu.TABLE_NAME, kcu.CONSTRAINT_NAME
            """, (DB,))
            for row in cur.fetchall():
                src, ref = row["src_table"], row["ref_table"]
                fk = {
                    "name":        row["fk_name"],
                    "column":      row["src_col"],
                    "ref_table":   ref,
                    "ref_column":  row["ref_col"],
                    "on_update":   row["UPDATE_RULE"],
                    "on_delete":   row["DELETE_RULE"],
                }
                if src in schema["tables"]:
                    schema["tables"][src]["foreign_keys_out"].append(fk)
                if ref in schema["tables"]:
                    schema["tables"][ref]["foreign_keys_in"].append({
                        "name":        row["fk_name"],
                        "from_table":  src,
                        "from_column": row["src_col"],
                        "ref_column":  row["ref_col"],
                        "on_update":   row["UPDATE_RULE"],
                        "on_delete":   row["DELETE_RULE"],
                    })

    return schema


if __name__ == "__main__":
    print(f"Connecting to {HOST}/{DB} ...")
    try:
        schema = fetch_schema()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    with open(OUTPUT, "w", encoding="utf-8") as f:
        json.dump(schema, f, indent=2, default=str)

    table_count = len(schema["tables"])
    col_count   = sum(len(t["columns"]) for t in schema["tables"].values())
    fk_count    = sum(len(t["foreign_keys_out"]) for t in schema["tables"].values())
    print(f"Done. {table_count} tables, {col_count} columns, {fk_count} foreign keys -> {OUTPUT}")
