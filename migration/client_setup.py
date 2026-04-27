# migration/client_setup.py — Client onboarding setup workflow

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Callable

LogCallback = Callable[[str, str], None]


@dataclass
class SetupPreview:
    client_id: int
    customer_short_name: str
    user_id: str
    current_username: str
    new_username: str
    archive_reason_count: int
    client_param_count: int


@dataclass
class SetupResult:
    success: bool = False
    error: str = ""
    user_updated: bool = False
    archive_rows_inserted: int = 0
    param_rows_inserted: int = 0
    proc_called: bool = False
    proc_output: list[dict] = field(default_factory=list)
    proc_message: str = ""


def get_setup_preview(client_id: int, user_id: str, conn) -> SetupPreview:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT CustomerShortName FROM Client WHERE ClientId = %s",
            (client_id,),
        )
        client_row = cur.fetchone()

    if not client_row:
        raise ValueError(f"Client {client_id} not found.")
    short_name = client_row.get("CustomerShortName") or ""
    if not short_name.strip():
        raise ValueError(f"Client {client_id} has no CustomerShortName — set it before running setup.")

    with conn.cursor() as cur:
        cur.execute(
            "SELECT UserId, UserName, FirstName, LastName, Email FROM User WHERE UserId = %s",
            (user_id,),
        )
        user_row = cur.fetchone()

    if not user_row:
        raise ValueError(f"User '{user_id}' not found.")

    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS cnt FROM ArchiveReason WHERE ClientId = 0")
        ar_count = int((cur.fetchone() or {}).get("cnt", 0))

    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS cnt FROM _x_ClientParameters WHERE ClientId = 0")
        param_count = int((cur.fetchone() or {}).get("cnt", 0))

    return SetupPreview(
        client_id=client_id,
        customer_short_name=short_name.strip(),
        user_id=user_id,
        current_username=user_row.get("UserName") or "",
        new_username=f"Admin@{short_name.strip()}",
        archive_reason_count=ar_count,
        client_param_count=param_count,
    )


def run_client_setup(
    client_id: int,
    user_id: str,
    conn,
    progress_callback: LogCallback | None = None,
) -> SetupResult:
    result = SetupResult()

    def log(msg: str, level: str = "info"):
        if progress_callback:
            progress_callback(msg, level)

    try:
        # Resolve CustomerShortName fresh at execution time
        with conn.cursor() as cur:
            cur.execute(
                "SELECT CustomerShortName FROM Client WHERE ClientId = %s",
                (client_id,),
            )
            client_row = cur.fetchone()

        if not client_row or not (client_row.get("CustomerShortName") or "").strip():
            raise ValueError(f"Client {client_id} has no CustomerShortName.")

        short_name = client_row["CustomerShortName"].strip()
        new_username = f"Admin@{short_name}"

        # ── Steps 1–3: prep data (single transaction) ─────────────────────
        try:
            # 1. Update Admin user
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE User
                       SET isCloudXuser = 1,
                           FirstName    = 'Admin',
                           LastName     = 'CloudX',
                           UserName     = %s,
                           isCloudxUser = 1,
                           RoleId       = 0,
                           isActive     = 1
                       WHERE UserId = %s""",
                    (new_username, user_id),
                )
                if cur.rowcount == 0:
                    raise ValueError(f"User '{user_id}' not found.")
            result.user_updated = True
            log(f"Admin user updated — UserName set to '{new_username}'.")

            # 2. Copy ArchiveReason rows from ClientId=0 template
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO ArchiveReason (ArchiveReason, ClientId) "
                    "SELECT ArchiveReason, %s FROM ArchiveReason WHERE ClientId = 0",
                    (client_id,),
                )
                result.archive_rows_inserted = cur.rowcount
            log(f"Inserted {result.archive_rows_inserted} ArchiveReason row(s) from template.")

            # 3. Copy _x_ClientParameters rows from ClientId=0 template
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO _x_ClientParameters (ClientId, ParamKey, ParamValue) "
                    "SELECT %s, ParamKey, ParamValue FROM _x_ClientParameters WHERE ClientId = 0",
                    (client_id,),
                )
                result.param_rows_inserted = cur.rowcount
            log(f"Inserted {result.param_rows_inserted} _x_ClientParameters row(s) from template.")

            conn.commit()
            log("Preparation steps committed.")

        except Exception:
            conn.rollback()
            raise

        # ── Step 4: Call stored procedure ─────────────────────────────────
        # The proc manages its own commits internally (via sub-procedure calls).
        log(f"Calling _x_Utility_Migrate_Client({client_id}, NULL)…")
        with conn.cursor() as cur:
            cur.execute("CALL _x_Utility_Migrate_Client(%s, NULL)", (client_id,))
            rows = cur.fetchall() or []

            # Drain any additional result sets the proc returns
            try:
                while cur.nextset():
                    extra = cur.fetchall() or []
                    if extra:
                        rows = extra  # keep last meaningful result set
            except Exception:
                pass

        # Detect early-exit messages the proc emits via bare SELECT literals
        if rows and len(rows[0]) == 1:
            msg = str(list(rows[0].values())[0])
            result.proc_message = msg
            log(f"Stored procedure returned: {msg}", "warning")
        else:
            result.proc_output = list(rows)
            log("Stored procedure completed successfully.")

        conn.commit()
        result.proc_called = True
        result.success = True

    except Exception as e:
        result.success = False
        result.error = str(e)
        log(f"Setup failed: {e}", "error")
        try:
            conn.rollback()
        except Exception:
            pass

    return result
