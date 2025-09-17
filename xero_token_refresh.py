# xero_token_refresh.py
# ---------------------
# Refresh Xero OAuth tokens stored in Neon/Postgres and update DB.
# - Safely rotates refresh_token (Xero returns a new one on every refresh)
# - Handles concurrency with SELECT FOR UPDATE SKIP LOCKED
# - Adds a small grace window so we refresh a few minutes before expiry

from __future__ import annotations
import os
import base64
import time
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

import psycopg
import requests
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

XERO_TOKEN_URL = "https://identity.xero.com/connect/token"

# --- Config helpers ---------------------------------------------------------

def _get_env(name: str) -> str:
    val = os.getenv(name, "").strip().strip('"').strip("'")
    if not val:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return val


def _db_url() -> str:
    return _get_env("DATABASE_URL")


def _client_creds() -> Tuple[str, str]:
    return _get_env("XERO_CLIENT_ID"), _get_env("XERO_CLIENT_SECRET")


# --- DB helpers -------------------------------------------------------------

class Db:
    def __init__(self):
        self.url = _db_url()

    def conn(self, autocommit: bool = False):
        return psycopg.connect(self.url, autocommit=autocommit)


# --- Utility ----------------------------------------------------------------

def _mask(s: Optional[str], visible: int = 4) -> str:
    if not s:
        return ""
    s = s.strip()
    if len(s) <= visible:
        return "*" * len(s)
    return "*" * (len(s) - visible) + s[-visible:]


def _compute_expiry(expires_in_seconds: int) -> datetime:
    # Xero access tokens are ~30 min. Buffer by 60s to be safe.
    buf = max(0, expires_in_seconds - 60)
    return datetime.now(timezone.utc) + timedelta(seconds=buf)


# --- Xero token exchange ----------------------------------------------------

def _refresh_via_xero(refresh_token: str) -> dict:
    client_id, client_secret = _client_creds()

    # HTTP Basic: base64(client_id:client_secret)
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    headers = {
        "Authorization": f"Basic {basic}",
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
    }
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }

    resp = requests.post(XERO_TOKEN_URL, headers=headers, data=data, timeout=30)
    if resp.status_code != 200:
        # Summarize error body without leaking tokens
        snippet = resp.text.strip()
        if len(snippet) > 400:
            snippet = snippet[:400] + "..."
        raise RuntimeError(f"Xero token refresh failed: {resp.status_code} {snippet}")

    body = resp.json()
    if not body.get("access_token") or not body.get("refresh_token"):
        raise RuntimeError("Xero token refresh response missing tokens.")
    if not isinstance(body.get("expires_in"), int):
        raise RuntimeError("Xero token refresh response missing expires_in.")
    return body


# --- Public API -------------------------------------------------------------

def refresh_token_for_client(client_id: int) -> bool:
    """Refresh and rotate tokens for a single xero_users row by ID.
    Returns True on success, False on recoverable failure.
    Raises on configuration/connection issues.
    """
    db = Db()
    with db.conn(autocommit=False) as conn:
        try:
            with conn.cursor() as cur:
                # Lock the row so parallel processes don't double-refresh
                cur.execute(
                    """
                    SELECT id, tenant_name, access_token, refresh_token
                         , expires_at, updated_at
                    FROM xero_users
                    WHERE id = %s
                    FOR UPDATE SKIP LOCKED
                    """,
                    (client_id,),
                )
                row = cur.fetchone()
                if not row:
                    return False

                _id, tenant_name, access_token, refresh_token, expires_at, updated_at = row
                if not refresh_token:
                    # Nothing we can do without a refresh token
                    return False

                # Call Xero to exchange refresh→access
                body = _refresh_via_xero(refresh_token)
                new_access = body["access_token"]
                new_refresh = body["refresh_token"]
                expires_in = int(body["expires_in"])  # seconds
                new_expiry = _compute_expiry(expires_in)

                cur.execute(
                    """
                    UPDATE xero_users
                       SET access_token = %s,
                           refresh_token = %s,
                           expires_at = %s,
                           updated_at = NOW()
                     WHERE id = %s
                    """,
                    (new_access, new_refresh, new_expiry, _id),
                )
            conn.commit()
            print(
                f"[REFRESH] {tenant_name or _id}: access={_mask(new_access)} exp={new_expiry.isoformat()}"
            )
            return True
        except Exception as e:
            conn.rollback()
            print(f"[REFRESH][ERROR] client={client_id}: {e}")
            return False


def refresh_expired_tokens(grace_seconds: int = 300, limit: int = 50) -> int:
    """Refresh all rows expiring within N seconds (or already expired).
    Returns the count of successfully refreshed rows.
    """
    db = Db()
    refreshed = 0

    # We iterate IDs first to avoid long-held locks across the whole set
    with db.conn(autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id
                  FROM xero_users
                 WHERE expires_at IS NULL
                    OR expires_at <= NOW() + (%s::interval)
                 ORDER BY updated_at DESC NULLS LAST
                 LIMIT %s
                """,
                (f"{grace_seconds} seconds", limit),
            )
            ids = [r[0] for r in cur.fetchall()]

    for _id in ids:
        ok = refresh_token_for_client(_id)
        if ok:
            refreshed += 1
        # Small jitter to be polite in case of many rows
        time.sleep(0.25)

    if refreshed:
        print(f"[REFRESH] Completed: {refreshed}/{len(ids)} tokens refreshed.")
    else:
        print("[REFRESH] No tokens were refreshed.")
    return refreshed


if __name__ == "__main__":
    # Manual test: `python xero_token_refresh.py`
    # Will refresh any rows due within the next 5 minutes
    refresh_expired_tokens(grace_seconds=300)