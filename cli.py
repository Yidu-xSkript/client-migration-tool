#!/usr/bin/env python
# cli.py — Command-line interface for the Client Migration Tool
#
# Usage:
#   python cli.py migrate --src dev --dst qa --client-id 42
#   python cli.py migrate --src qa --dst prod --client-id 42 --delta
#   python cli.py batch   --src dev --dst qa --client-ids 1,2,3
#   python cli.py compare --client-id 42
#   python cli.py run-profile "Nightly Dev→QA" --dry-run
#   python cli.py profiles list
#   python cli.py backups list --env qa
#   python cli.py backups restore <backup_table_name> --env qa
#   python cli.py backups cleanup --env qa --days 14
#
# Credentials are read from environment variables:
#   DEV_HOST, DEV_USER, DEV_PASSWORD, DEV_DATABASE, DEV_PORT
#   QA_HOST,  QA_USER,  QA_PASSWORD,  QA_DATABASE,  QA_PORT
#   PROD_HOST,PROD_USER,PROD_PASSWORD,PROD_DATABASE, PROD_PORT

import os
import sys
import json
from datetime import datetime

import click
import pymysql
import pymysql.cursors

from config import ENV_LABELS, CLIENT_ID_COLUMN, DEFAULT_PORT
from db.discovery import discover_related_tables
from db.operations import get_all_row_counts, get_client_by_id
from migration.engine import run_migration, dry_run
from migration.backup import list_backups, restore_backup, cleanup_old_backups, create_backups
from migration.profiles import load_all_profiles, get_profile
from migration.batch import run_batch
from migration import audit


# ---------------------------------------------------------------------------
# Credential loading (from env vars)
# ---------------------------------------------------------------------------

def _creds_from_env(env: str) -> dict:
    prefix = env.upper()
    return {
        "host":     os.environ.get(f"{prefix}_HOST", ""),
        "user":     os.environ.get(f"{prefix}_USER", ""),
        "password": os.environ.get(f"{prefix}_PASSWORD", ""),
        "database": os.environ.get(f"{prefix}_DATABASE", ""),
        "port":     int(os.environ.get(f"{prefix}_PORT", str(DEFAULT_PORT))),
    }


def _open_conn(env: str):
    """Open a pymysql connection using environment variables for credentials."""
    creds = _creds_from_env(env)
    missing = [k for k in ("host", "user", "database") if not creds.get(k)]
    if missing:
        raise click.ClickException(
            f"{ENV_LABELS.get(env, env)} credentials missing. "
            f"Set {env.upper()}_HOST, {env.upper()}_USER, {env.upper()}_PASSWORD, "
            f"{env.upper()}_DATABASE environment variables."
        )
    try:
        return pymysql.connect(
            host=creds["host"],
            user=creds["user"],
            password=creds["password"],
            database=creds["database"],
            port=creds["port"],
            cursorclass=pymysql.cursors.DictCursor,
            connect_timeout=10,
            autocommit=False,
            charset="utf8mb4",
        )
    except pymysql.Error as e:
        raise click.ClickException(f"Cannot connect to {ENV_LABELS.get(env, env)}: {e}")


# ---------------------------------------------------------------------------
# Shared options
# ---------------------------------------------------------------------------

def _src_dst_options(fn):
    fn = click.option("--src", required=True, type=click.Choice(["dev", "qa", "prod"]),
                      help="Source environment")(fn)
    fn = click.option("--dst", required=True, type=click.Choice(["dev", "qa", "prod"]),
                      help="Destination environment")(fn)
    return fn


# ---------------------------------------------------------------------------
# CLI groups
# ---------------------------------------------------------------------------

@click.group()
def cli():
    """Client Migration Tool — command-line interface."""
    pass


# ---------------------------------------------------------------------------
# migrate
# ---------------------------------------------------------------------------

@cli.command()
@_src_dst_options
@click.option("--client-id", "-c", required=True, type=int, multiple=True,
              help="Client ID(s) to migrate. Repeat for multiple: -c 1 -c 2")
@click.option("--dry-run", is_flag=True, help="Preview only, no changes written.")
@click.option("--delta", is_flag=True, help="Delta mode — only migrate changed rows.")
@click.option("--no-backup", is_flag=True, help="Skip backup creation.")
@click.option("--conflict", default="replace",
              type=click.Choice(["replace", "skip", "update"]),
              help="Conflict resolution strategy (default: replace).")
@click.option("--ticket", default="", help="Reference/ticket number for the audit log.")
def migrate(src, dst, client_id, dry_run, delta, no_backup, conflict, ticket):
    """Migrate one or more clients between environments."""
    client_ids = list(client_id)

    if dst == "prod" and not dry_run:
        click.confirm(
            f"⚠️  You are migrating {len(client_ids)} client(s) to PRODUCTION. Continue?",
            abort=True,
        )

    src_conn = _open_conn(src)
    click.echo(f"Discovering tables from {ENV_LABELS[src]}…")
    tables = discover_related_tables(src_conn)
    click.echo(f"Found {len(tables)} related tables.")

    for cid in client_ids:
        click.echo(f"\n{'─'*50}")
        click.echo(f"Client {cid}: {ENV_LABELS[src]} → {ENV_LABELS[dst]}")

        if dry_run:
            dst_conn = _open_conn(dst)
            result = dry_run(
                client_id=cid,
                tables=tables,
                src_conn=src_conn,
                dst_conn=dst_conn,
                source_env=src,
                target_env=dst,
                conflict_mode=conflict,
                delta_mode=delta,
            )
            dst_conn.close()
            click.echo(f"{'Table':<30} {'Src':>8} {'Dst':>8} {'Action':<25}")
            click.echo("─" * 75)
            for t in result.tables:
                if delta:
                    action = f"+{t.delta_insert} ~{t.delta_update} -{t.delta_delete}"
                else:
                    action = f"{conflict}: del {t.dst_rows} → ins {t.src_rows}"
                click.echo(f"{t.table:<30} {t.src_rows:>8} {t.dst_rows:>8} {action:<25}")
            continue

        dst_conn = _open_conn(dst)
        backup_tables = []
        if not no_backup:
            click.echo("  Creating backups…")
            backup_tables = create_backups(cid, tables, dst_conn)
            click.echo(f"  {len(backup_tables)} backup table(s) created.")

        def log(msg, level="info"):
            prefix = {"info": "  ℹ", "warning": "  ⚠", "error": "  ✗"}.get(level, "  •")
            click.echo(f"{prefix} {msg}")

        result = run_migration(
            client_id=cid,
            tables=tables,
            src_conn=src_conn,
            dst_conn=dst_conn,
            source_env=src,
            target_env=dst,
            conflict_mode=conflict,
            delta_mode=delta,
            progress_callback=log,
        )
        dst_conn.close()

        row_counts = {t.table: t.inserted for t in result.tables}
        audit.log_attempt(audit.make_entry(
            source_env=src, target_env=dst, client_id=cid,
            tables=tables, row_counts=row_counts,
            status="success" if result.success else "failure",
            error_message=result.error_message,
            ticket_number=ticket, backup_tables=backup_tables,
        ))

        if result.success:
            click.echo(f"  ✓ Success — {result.total_inserted} rows migrated.")
            if result.post_validation and not result.post_validation.passed:
                for chk in result.post_validation.failures:
                    click.echo(f"  ⚠ Post-check warning: {chk.message}")
        else:
            click.echo(f"  ✗ Failed: {result.error_message}", err=True)

    src_conn.close()


# ---------------------------------------------------------------------------
# batch
# ---------------------------------------------------------------------------

@cli.command()
@_src_dst_options
@click.option("--client-ids", required=True,
              help="Comma-separated list of client IDs, or @file.txt with one ID per line.")
@click.option("--dry-run", is_flag=True)
@click.option("--delta", is_flag=True)
@click.option("--no-backup", is_flag=True)
@click.option("--conflict", default="replace", type=click.Choice(["replace", "skip", "update"]))
@click.option("--ticket", default="")
def batch(src, dst, client_ids, dry_run, delta, no_backup, conflict, ticket):
    """Migrate a batch of clients."""
    # Parse client IDs
    if client_ids.startswith("@"):
        path = client_ids[1:]
        try:
            ids = [int(line.strip()) for line in open(path) if line.strip().isdigit()]
        except OSError as e:
            raise click.ClickException(str(e))
    else:
        ids = [int(x.strip()) for x in client_ids.split(",") if x.strip().isdigit()]

    if not ids:
        raise click.ClickException("No valid client IDs found.")

    click.echo(f"Batch migration: {len(ids)} client(s) — {ENV_LABELS[src]} → {ENV_LABELS[dst]}")

    if dst == "prod" and not dry_run:
        click.confirm("⚠️  Target is PRODUCTION. Continue?", abort=True)

    succeeded = failed = 0
    gen = run_batch(
        client_ids=ids,
        src_env=src,
        dst_env=dst,
        conflict_mode=conflict,
        delta_mode=delta,
        do_backup=not no_backup,
        ticket=ticket,
    )

    for r in gen:
        if r.success:
            succeeded += 1
            click.echo(f"  ✓ Client {r.client_id}: {r.rows_migrated} rows")
        else:
            failed += 1
            click.echo(f"  ✗ Client {r.client_id}: {r.error}", err=True)

    click.echo(f"\nBatch complete: {succeeded} succeeded, {failed} failed.")
    sys.exit(0 if failed == 0 else 1)


# ---------------------------------------------------------------------------
# compare
# ---------------------------------------------------------------------------

@cli.command()
@click.option("--client-id", "-c", required=True, type=int)
@click.option("--envs", default="dev,qa,prod", help="Comma-separated environments to compare.")
def compare(client_id, envs):
    """Compare row counts for a client across environments."""
    env_list = [e.strip() for e in envs.split(",")]
    connections = {}
    for env in env_list:
        try:
            connections[env] = _open_conn(env)
        except click.ClickException as e:
            click.echo(f"Skipping {env}: {e}", err=True)

    if not connections:
        raise click.ClickException("No environments available.")

    ref_conn = connections[env_list[0]]
    tables = discover_related_tables(ref_conn)

    click.echo(f"\nClient {client_id} — Row Counts")
    click.echo(f"{'Table':<30}" + "".join(f" {ENV_LABELS.get(e, e):>10}" for e in env_list) + f"  {'Sync':>6}")
    click.echo("─" * (30 + 12 * len(env_list) + 8))

    for info in tables:
        counts = []
        for env in env_list:
            conn = connections.get(env)
            if conn:
                try:
                    from db.operations import get_row_count
                    counts.append(get_row_count(info.name, info.client_id_column, client_id, conn))
                except Exception:
                    counts.append(-1)
            else:
                counts.append(None)

        in_sync = len({c for c in counts if c is not None}) == 1
        sync_str = "✓" if in_sync else "✗"
        row = f"{info.name:<30}"
        for c in counts:
            row += f" {(str(c) if c is not None else 'N/A'):>10}"
        row += f"  {sync_str:>6}"
        click.echo(row)

    for conn in connections.values():
        conn.close()


# ---------------------------------------------------------------------------
# run-profile
# ---------------------------------------------------------------------------

@cli.command("run-profile")
@click.argument("profile_name")
@click.option("--dry-run", is_flag=True)
@click.option("--ticket", default="")
def run_profile(profile_name, dry_run, ticket):
    """Execute a saved migration profile."""
    profile = get_profile(profile_name)
    if not profile:
        raise click.ClickException(f"Profile '{profile_name}' not found.")

    click.echo(f"Running profile: {profile.name}")
    click.echo(f"Route: {ENV_LABELS[profile.src_env]} → {ENV_LABELS[profile.dst_env]}")
    click.echo(f"Clients: {profile.client_ids or 'from prompt'}")

    ids = profile.client_ids
    if not ids:
        raw = click.prompt("Enter client IDs (comma-separated)")
        ids = [int(x.strip()) for x in raw.split(",") if x.strip().isdigit()]

    gen = run_batch(
        client_ids=ids,
        src_env=profile.src_env,
        dst_env=profile.dst_env,
        conflict_mode=profile.conflict_mode,
        delta_mode=profile.delta_mode,
        do_backup=profile.do_backup,
        excluded_columns=profile.excluded_columns,
        row_filters=profile.row_filters,
        ticket=ticket or profile_name,
    )
    succeeded = failed = 0
    for r in gen:
        if r.success:
            succeeded += 1
            click.echo(f"  ✓ Client {r.client_id}: {r.rows_migrated} rows")
        else:
            failed += 1
            click.echo(f"  ✗ Client {r.client_id}: {r.error}", err=True)

    click.echo(f"\nProfile '{profile_name}' complete: {succeeded} succeeded, {failed} failed.")


# ---------------------------------------------------------------------------
# profiles list
# ---------------------------------------------------------------------------

@cli.group()
def profiles():
    """Manage migration profiles."""
    pass


@profiles.command("list")
def profiles_list():
    """List all saved migration profiles."""
    all_profiles = load_all_profiles()
    if not all_profiles:
        click.echo("No profiles saved yet.")
        return
    click.echo(f"{'Name':<30} {'Route':<20} {'Clients':>8} {'Delta':>6} {'Backup':>7}")
    click.echo("─" * 75)
    for p in all_profiles:
        route = f"{ENV_LABELS.get(p.src_env, p.src_env)} → {ENV_LABELS.get(p.dst_env, p.dst_env)}"
        clients = str(len(p.client_ids)) if p.client_ids else "prompt"
        click.echo(f"{p.name:<30} {route:<20} {clients:>8} {'✓' if p.delta_mode else '✗':>6} {'✓' if p.do_backup else '✗':>7}")


# ---------------------------------------------------------------------------
# backups
# ---------------------------------------------------------------------------

@cli.group()
def backups():
    """Manage backup tables."""
    pass


@backups.command("list")
@click.option("--env", required=True, type=click.Choice(["dev", "qa", "prod"]))
@click.option("--client-id", type=int, default=None)
def backups_list(env, client_id):
    """List backup tables in an environment."""
    conn = _open_conn(env)
    infos = list_backups(conn, client_id=client_id)
    conn.close()
    if not infos:
        click.echo("No backup tables found.")
        return
    click.echo(f"{'Backup Table':<55} {'Client':>7} {'Original Table':<25} {'Age (days)':>10}")
    click.echo("─" * 100)
    for b in infos:
        click.echo(f"{b.backup_table:<55} {b.client_id:>7} {b.original_table:<25} {b.age_days:>10}")


@backups.command("restore")
@click.argument("backup_table")
@click.option("--env", required=True, type=click.Choice(["dev", "qa", "prod"]))
@click.option("--client-id-col", default=CLIENT_ID_COLUMN, help="Client ID column name in the table.")
@click.option("--yes", is_flag=True, help="Skip confirmation prompt.")
def backups_restore(backup_table, env, client_id_col, yes):
    """Restore a backup table into the original table."""
    from migration.backup import parse_backup_name
    info = parse_backup_name(backup_table)
    if not info:
        raise click.ClickException(f"Cannot parse backup table name: {backup_table}")

    if not yes:
        click.confirm(
            f"Restore `{info.backup_table}` → `{info.original_table}` "
            f"(client {info.client_id}) in {ENV_LABELS[env]}?",
            abort=True,
        )

    conn = _open_conn(env)
    restored = restore_backup(info, client_id_col, conn)
    conn.close()
    click.echo(f"✓ Restored {restored} rows into `{info.original_table}`.")


@backups.command("cleanup")
@click.option("--env", required=True, type=click.Choice(["dev", "qa", "prod"]))
@click.option("--days", default=30, show_default=True, help="Delete backups older than N days.")
@click.option("--yes", is_flag=True)
def backups_cleanup(env, days, yes):
    """Delete backup tables older than N days."""
    if not yes:
        click.confirm(f"Delete all backups older than {days} days in {ENV_LABELS[env]}?", abort=True)
    conn = _open_conn(env)
    dropped = cleanup_old_backups(conn, days=days)
    conn.close()
    click.echo(f"Dropped {len(dropped)} backup table(s).")
    for name in dropped:
        click.echo(f"  - {name}")


# ---------------------------------------------------------------------------
# audit log
# ---------------------------------------------------------------------------

@cli.command("audit-log")
@click.option("--limit", default=20, show_default=True)
@click.option("--format", "fmt", default="table", type=click.Choice(["table", "json"]))
def audit_log(limit, fmt):
    """Show recent migration audit log entries."""
    entries = audit.read_recent(limit)
    if not entries:
        click.echo("No audit log entries found.")
        return

    if fmt == "json":
        import dataclasses
        click.echo(json.dumps([dataclasses.asdict(e) for e in entries], indent=2))
        return

    click.echo(f"{'Timestamp':<22} {'Route':<22} {'Client':>7} {'Status':<10} {'Rows':>6}")
    click.echo("─" * 72)
    for e in entries:
        route = f"{ENV_LABELS.get(e.source_env, e.source_env)} → {ENV_LABELS.get(e.target_env, e.target_env)}"
        rows = sum(e.row_counts.values()) if e.row_counts else 0
        click.echo(f"{e.timestamp:<22} {route:<22} {e.client_id:>7} {e.status:<10} {rows:>6}")


if __name__ == "__main__":
    cli()
