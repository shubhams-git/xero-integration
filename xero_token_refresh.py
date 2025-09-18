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
from typing import Optional, Tuple, List

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

class RefreshResult:
    """Container for token refresh results with detailed status information."""
    def __init__(self, success: bool, client_id: Optional[int] = None, tenant_name: Optional[str] = None,
                 error_message: Optional[str] = None, new_expiry: Optional[datetime] = None):
        self.success = success
        self.client_id = client_id
        self.tenant_name = tenant_name
        self.error_message = error_message
        self.new_expiry = new_expiry
    
    def __bool__(self):
        return self.success
    
    def __str__(self):
        if self.success:
            return f"✓ {self.tenant_name or self.client_id} refreshed until {self.new_expiry}"
        else:
            return f"✗ {self.tenant_name or self.client_id}: {self.error_message}"

def refresh_token_for_client(client_id: int, verbose: bool = True) -> RefreshResult:
    """Refresh and rotate tokens for a single xero_users row by ID.
    Returns RefreshResult with detailed status information.
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
                    return RefreshResult(False, client_id, error_message="Client not found in database")

                _id, tenant_name, access_token, refresh_token, expires_at, updated_at = row
                if not refresh_token:
                    # Nothing we can do without a refresh token
                    return RefreshResult(False, client_id, tenant_name, "No refresh token available")

                # Call Xero to exchange refresh→access
                try:
                    body = _refresh_via_xero(refresh_token)
                except Exception as e:
                    return RefreshResult(False, client_id, tenant_name, f"Xero API error: {str(e)}")
                
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
            if verbose:
                print(
                    f"[REFRESH] {tenant_name or _id}: access={_mask(new_access)} exp={new_expiry.isoformat()}"
                )
            return RefreshResult(True, client_id, tenant_name, new_expiry=new_expiry)
        except Exception as e:
            conn.rollback()
            error_msg = f"Database error: {str(e)}"
            if verbose:
                print(f"[REFRESH][ERROR] client={client_id}: {error_msg}")
            return RefreshResult(False, client_id, error_message=error_msg)


def check_token_status(client_id: int) -> dict:
    """Check the status of a specific client's token.
    Returns dict with status information.
    """
    db = Db()
    try:
        with db.conn(autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, tenant_id, tenant_name, expires_at, updated_at
                    FROM xero_users
                    WHERE id = %s
                    """,
                    (client_id,),
                )
                row = cur.fetchone()
                if not row:
                    return {"exists": False, "error": "Client not found"}
                
                _id, tenant_id, tenant_name, expires_at, updated_at = row
                now = datetime.now(timezone.utc)
                
                if expires_at:
                    try:
                        exp_dt = datetime.fromisoformat(str(expires_at).replace('Z', '+00:00'))
                    except Exception:
                        exp_dt = None
                else:
                    exp_dt = None
                
                is_expired = not exp_dt or exp_dt <= now
                expires_soon = exp_dt and exp_dt <= (now + timedelta(minutes=5)) if exp_dt else True
                
                return {
                    "exists": True,
                    "client_id": _id,
                    "tenant_id": tenant_id,
                    "tenant_name": tenant_name,
                    "expires_at": exp_dt.isoformat() if exp_dt else None,
                    "is_expired": is_expired,
                    "expires_soon": expires_soon,
                    "minutes_until_expiry": int((exp_dt - now).total_seconds() / 60) if exp_dt and not is_expired else 0
                }
    except Exception as e:
        return {"exists": False, "error": f"Database error: {str(e)}"}

def refresh_expired_tokens(grace_seconds: int = 300, limit: int = 50, verbose: bool = True) -> Tuple[int, List[RefreshResult]]:
    """Refresh all rows expiring within N seconds (or already expired).
    Returns tuple of (count_refreshed, list_of_results).
    """
    db = Db()
    refreshed = 0
    results = []

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
        result = refresh_token_for_client(_id, verbose=verbose)
        results.append(result)
        if result.success:
            refreshed += 1
        # Small jitter to be polite in case of many rows
        time.sleep(0.25)

    if verbose:
        if refreshed:
            print(f"[REFRESH] Completed: {refreshed}/{len(ids)} tokens refreshed.")
        else:
            print("[REFRESH] No tokens were refreshed.")
    return refreshed, results


if __name__ == "__main__":
    import sys
    
    if len(sys.argv) > 1:
        # Check/refresh specific client: `python xero_token_refresh.py <client_id>`
        try:
            client_id = int(sys.argv[1])
            print(f"Checking status for client {client_id}...")
            status = check_token_status(client_id)
            if not status["exists"]:
                print(f"Error: {status['error']}")
                sys.exit(1)
            
            print(f"Client: {status['tenant_name']} ({status['client_id']})")
            print(f"Expires: {status['expires_at']}")
            print(f"Is expired: {status['is_expired']}")
            print(f"Expires soon: {status['expires_soon']}")
            
            if status['is_expired'] or status['expires_soon']:
                print("Attempting refresh...")
                result = refresh_token_for_client(client_id)
                if result.success:
                    print(f"✓ Success! New expiry: {result.new_expiry}")
                else:
                    print(f"✗ Failed: {result.error_message}")
            else:
                print("✓ Token is valid and not expiring soon")
                
        except ValueError:
            print("Usage: python xero_token_refresh.py [client_id]")
            sys.exit(1)
    else:
        # Manual test: `python xero_token_refresh.py`
        # Will refresh any rows due within the next 5 minutes
        print("Checking for expired tokens...")
        refresh_expired_tokens(grace_seconds=300)