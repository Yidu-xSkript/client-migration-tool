# db/connection.py — Database connection management

import pymysql
import pymysql.cursors
import streamlit as st
from config import ENV_LABELS


def _get_creds(env: str) -> dict:
    """Pull credentials for the given env from session state."""
    conns = st.session_state.get("connections", {})
    creds = conns.get(env, {})
    return creds


def _cred_hash(creds: dict) -> tuple:
    return tuple(str(creds.get(k, "")) for k in ("host", "user", "password", "database", "port"))


class _PersistentConn:
    """
    Thin proxy around a pymysql connection that makes close() a no-op so the
    underlying socket is reused across Streamlit reruns instead of being torn
    down and rebuilt for every DB call (each new connection costs 50-200 ms).

    All other attributes delegate transparently to the real connection.
    """
    __slots__ = ("_conn",)

    def __init__(self, conn: pymysql.connections.Connection):
        object.__setattr__(self, "_conn", conn)

    # Keep close() as a deliberate no-op
    def close(self) -> None:
        pass

    def really_close(self) -> None:
        """Explicitly close when we know we want to discard this connection."""
        try:
            object.__getattribute__(self, "_conn").close()
        except Exception:
            pass

    def cursor(self, *args, **kwargs):
        return object.__getattribute__(self, "_conn").cursor(*args, **kwargs)

    def commit(self):
        return object.__getattribute__(self, "_conn").commit()

    def rollback(self):
        return object.__getattribute__(self, "_conn").rollback()

    def ping(self, *args, **kwargs):
        return object.__getattribute__(self, "_conn").ping(*args, **kwargs)

    def __getattr__(self, name: str):
        return getattr(object.__getattribute__(self, "_conn"), name)

    def __setattr__(self, name: str, value):
        if name == "_conn":
            object.__setattr__(self, name, value)
        else:
            setattr(object.__getattribute__(self, "_conn"), name, value)


def get_connection(env: str) -> _PersistentConn:
    """
    Return a persistent connection for the given environment, cached in session
    state.  The connection is kept alive across Streamlit reruns via ping/
    reconnect, so only the very first call per session pays the TCP handshake
    cost.  Credentials changes are detected via a hash and trigger a fresh
    connect automatically.

    Callers may call .close() freely — it is a no-op on the proxy.
    Raises RuntimeError if credentials are missing or the initial connect fails.
    """
    creds = _get_creds(env)
    required = ("host", "user", "password", "database")
    missing = [k for k in required if not creds.get(k)]
    if missing:
        label = ENV_LABELS.get(env, env)
        raise RuntimeError(
            f"{label} credentials incomplete. Missing: {', '.join(missing)}."
        )

    conn_key  = f"_db_conn_{env}"
    hash_key  = f"_db_hash_{env}"
    new_hash  = _cred_hash(creds)
    cached    = st.session_state.get(conn_key)

    if cached is not None and st.session_state.get(hash_key) == new_hash:
        try:
            cached.ping(reconnect=True)
            return cached
        except Exception:
            cached.really_close()

    # Build a fresh connection
    try:
        raw = pymysql.connect(
            host=creds["host"],
            user=creds["user"],
            password=creds["password"],
            database=creds["database"],
            port=int(creds.get("port", 3306)),
            cursorclass=pymysql.cursors.DictCursor,
            connect_timeout=10,
            autocommit=False,
            charset="utf8mb4",
        )
    except pymysql.Error as e:
        label = ENV_LABELS.get(env, env)
        raise RuntimeError(f"Could not connect to {label}: {e}") from e

    proxy = _PersistentConn(raw)
    st.session_state[conn_key] = proxy
    st.session_state[hash_key] = new_hash
    return proxy


def test_connection(env: str) -> tuple[bool, str]:
    """
    Test connectivity for the given environment.
    Returns (True, server_version) on success, (False, error_message) on failure.
    Uses the cached persistent connection so it's fast on repeat tests.
    """
    try:
        conn = get_connection(env)
        with conn.cursor() as cur:
            cur.execute("SELECT VERSION() AS v")
            row = cur.fetchone()
        version = row["v"] if row else "unknown"
        return True, f"MySQL {version}"
    except Exception as e:
        return False, str(e)
