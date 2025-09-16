import os
from contextlib import contextmanager
from typing import Optional
import psycopg


from .config import get_database_url


DATABASE_URL = get_database_url()


def require_db_url() -> str:
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set. Point it to your Neon connection string.")
    return DATABASE_URL


def init_db():
    url = require_db_url()
    with psycopg.connect(url, autocommit=True) as conn:
        with conn.cursor() as cur:
            schema_path = os.path.join(os.path.dirname(__file__), "schema.sql")
            with open(schema_path, "r", encoding="utf-8") as f:
                cur.execute(f.read())


@contextmanager
def get_conn():
    url = require_db_url()
    with psycopg.connect(url, autocommit=True) as conn:
        yield conn


# Simple state management (using a temporary dict for OAuth states)
_oauth_states = {}

def save_state(state: str):
    """Save OAuth state temporarily"""
    _oauth_states[state] = True

def pop_state(state: str) -> bool:
    """Check and remove OAuth state"""
    return _oauth_states.pop(state, False)

def save_user(tenant_id: str, tenant_name: str, access_token: str, refresh_token: str, expires_at_iso: str):
    """Save or update user connection info"""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO xero_users (tenant_id, tenant_name, access_token, refresh_token, expires_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, NOW())
            ON CONFLICT (tenant_id) DO UPDATE SET
              tenant_name = EXCLUDED.tenant_name,
              access_token = EXCLUDED.access_token,
              refresh_token = EXCLUDED.refresh_token,
              expires_at = EXCLUDED.expires_at,
              updated_at = NOW()
            """,
            (tenant_id, tenant_name, access_token, refresh_token or "", expires_at_iso),
        )

def get_user_by_tenant_id(tenant_id: str = None) -> Optional[tuple]:
    """Get user by tenant_id, or get the most recent user if None"""
    with get_conn() as conn, conn.cursor() as cur:
        if tenant_id:
            cur.execute(
                "SELECT tenant_id, tenant_name, access_token, refresh_token, expires_at FROM xero_users WHERE tenant_id = %s",
                (tenant_id,)
            )
        else:
            cur.execute(
                "SELECT tenant_id, tenant_name, access_token, refresh_token, expires_at FROM xero_users ORDER BY updated_at DESC LIMIT 1"
            )
        return cur.fetchone()

def update_user_tokens(tenant_id: str, access_token: str, refresh_token: str, expires_at_iso: str):
    """Update just the tokens for a user"""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE xero_users 
            SET access_token = %s, refresh_token = %s, expires_at = %s, updated_at = NOW()
            WHERE tenant_id = %s
            """,
            (access_token, refresh_token or "", expires_at_iso, tenant_id)
        )

