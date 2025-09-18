import os
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
from pathlib import Path

import requests
import uvicorn
import sys
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from dotenv import load_dotenv

# Load .env file from parent directory if it exists
load_dotenv(dotenv_path=Path("../.env"))
load_dotenv()

try:
    from .config import CALLBACK_SUFFIX, get_database_url, get_env
    from .db import (
        init_db,
        pop_state,
        save_state,
        save_user,
        get_user_by_tenant_id,
        update_user_tokens,
        get_all_users,
        delete_all_users,
    )
except ImportError:
    PACKAGE_ROOT = Path(__file__).resolve().parent.parent
    if str(PACKAGE_ROOT) not in sys.path:
        sys.path.insert(0, str(PACKAGE_ROOT))
    from server.config import CALLBACK_SUFFIX, get_database_url, get_env
    from server.db import (
        init_db,
        pop_state,
        save_state,
        save_user,
        get_user_by_tenant_id,
        update_user_tokens,
        get_all_users,
        delete_all_users,
    )


APP = FastAPI(title="Xero OAuth Minimal Backend")


def _normalize_redirect_base(raw: str) -> str:
    """Return a https base URL with the callback suffix removed if present."""
    if not raw:
        return ""
    candidate = raw.strip().rstrip('/')
    lowered = candidate.lower()
    suffix = CALLBACK_SUFFIX.lower()
    if lowered.endswith(suffix):
        candidate = candidate[: -len(suffix)]
        candidate = candidate.rstrip('/')
    return candidate


# ---- Config (env) ----
CLIENT_ID = get_env("XERO_CLIENT_ID")
CLIENT_SECRET = get_env("XERO_CLIENT_SECRET")
RAW_REDIRECT_BASE_URL = get_env("REDIRECT_BASE_URL")
print(f"[DEBUG] RAW_REDIRECT_BASE_URL from env: '{RAW_REDIRECT_BASE_URL}'")
REDIRECT_BASE_URL = _normalize_redirect_base(RAW_REDIRECT_BASE_URL)  # e.g., https://<ngrok>.ngrok.io
print(f"[DEBUG] REDIRECT_BASE_URL after normalize: '{REDIRECT_BASE_URL}'")
DATABASE_URL = get_database_url()

if RAW_REDIRECT_BASE_URL and RAW_REDIRECT_BASE_URL != REDIRECT_BASE_URL:
    print("[INFO] Normalized REDIRECT_BASE_URL to", REDIRECT_BASE_URL)

# You can tweak scopes here as needed
XERO_SCOPES = get_env("XERO_SCOPES") or (
    "offline_access openid profile email accounting.settings accounting.reports.read accounting.transactions accounting.contacts"
)

# OAuth endpoints
AUTH_URL = "https://login.xero.com/identity/connect/authorize"
TOKEN_URL = "https://identity.xero.com/connect/token"
CONNECTIONS_URL = "https://api.xero.com/connections"


def require_base_redirect() -> str:
    if not REDIRECT_BASE_URL:
        raise HTTPException(status_code=500, detail="REDIRECT_BASE_URL is not set")
    # TEMPORARY: Allow HTTP for local testing - comment out for production
    # if not REDIRECT_BASE_URL.startswith("https://"):
    #     # Xero requires https; use ngrok or a local TLS cert
    #     raise HTTPException(status_code=500, detail="REDIRECT_BASE_URL must be https://")
    return REDIRECT_BASE_URL.rstrip("/")


@APP.on_event("startup")
def _startup():
    print("[INFO] Starting Xero OAuth Backend - Production Mode")
    if not CLIENT_ID or not CLIENT_SECRET:
        print("[WARN] XERO_CLIENT_ID/SECRET not set yet. /api/xero/authorize will fail.")
    if not DATABASE_URL:
        print("[WARN] DATABASE_URL not set. Set to your Neon Postgres URL.")
    else:
        print(f"[INFO] Database configured: {DATABASE_URL.split('@')[1].split('/')[0] if '@' in DATABASE_URL else 'Unknown'}")
    
    print("[INFO] Initializing database...")
    try:
        init_db()
        print("[SUCCESS] Database initialized successfully")
    except Exception as e:
        print(f"[ERROR] Database initialization failed: {e}")


@APP.get("/", response_class=HTMLResponse)
def home():
    # Test database connection
    db_status = "unknown"
    try:
        from .db import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                db_status = "connected"
    except Exception as e:
        db_status = f"error: {str(e)[:50]}"
    
    client_id_status = 'yes' if CLIENT_ID else 'no'
    client_secret_status = 'yes' if CLIENT_SECRET else 'no'
    db_config_status = 'yes' if DATABASE_URL else 'no'
    redirect_url_display = REDIRECT_BASE_URL or '(unset)'
    
    html = f"""
    <html>
      <head><title>Xero OAuth Backend - Production</title></head>
      <body style='font-family: system-ui, -apple-system, Segoe UI, Roboto; padding: 20px;'>
        <h2>Xero OAuth Backend - Production Mode</h2>
        <div style='background: #f5f5f5; padding: 15px; border-radius: 5px; margin: 10px 0;'>
          <h3>System Status:</h3>
          <p>✅ Client ID configured: <code>{client_id_status}</code></p>
          <p>✅ Client Secret configured: <code>{client_secret_status}</code></p>
          <p>✅ Database configured: <code>{db_config_status}</code></p>
          <p>✅ Database connection: <code>{db_status}</code></p>
          <p>✅ Redirect base URL: <code>{redirect_url_display}</code></p>
        </div>
        <div style='margin: 20px 0;'>
          <h3>Test OAuth Flow:</h3>
          <a href="/api/xero/authorize" style='display: inline-block; background: #0066cc; color: white; padding: 10px 20px; text-decoration: none; border-radius: 5px; margin-right: 10px;'>🔗 Connect to Xero</a>
        </div>
        <div style='margin: 20px 0;'>
          <h3>Debug Endpoints:</h3>
          <a href="/api/xero/users" style='display: inline-block; background: #28a745; color: white; padding: 8px 16px; text-decoration: none; border-radius: 3px; margin-right: 10px;'>👥 View Stored Users</a>
          <button onclick="clearUsers()" style='background: #dc3545; color: white; padding: 8px 16px; border: none; border-radius: 3px; cursor: pointer;'>🗑️ Clear All Users</button>
        </div>
    """ + """
        <script>
          async function clearUsers() {
            if (confirm("Are you sure you want to delete all stored user connections?")) {
              try {
                const response = await fetch("/api/xero/users", { method: "DELETE" });
                const result = await response.json();
                alert(result.message || "Users cleared");
                location.reload();
              } catch (error) {
                alert("Error: " + error.message);
              }
            }
          }
        </script>
        <div style='background: #d1ecf1; padding: 15px; border-radius: 5px; margin: 10px 0;'>
          <h4>🔒 Production Mode Notes:</h4>
          <ul>
            <li>Tokens are securely stored in PostgreSQL database</li>
            <li>Automatic token refresh with 30-second safety buffer</li>
            <li>Ready for MCP client integration</li>
          </ul>
        </div>
      </body>
    </html>
    """
    return HTMLResponse(html)


@APP.get("/test/db")
def test_database():
    """Test database connection and show table status"""
    try:
        from .db import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                # Test basic connection
                cur.execute("SELECT current_database(), current_user, version()")
                db_info = cur.fetchone()
                
                # Check if our table exists
                cur.execute("""
                    SELECT table_name 
                    FROM information_schema.tables 
                    WHERE table_schema = 'public' 
                    AND table_name = 'xero_users'
                """)
                table_exists = cur.fetchone() is not None
                
                # Count users if table exists
                user_count = 0
                if table_exists:
                    cur.execute("SELECT COUNT(*) FROM xero_users")
                    user_count = cur.fetchone()[0]
                
                result = {
                    "status": "success",
                    "database": db_info[0] if db_info else "unknown",
                    "user": db_info[1] if db_info else "unknown",
                    "version": db_info[2][:50] if db_info and db_info[2] else "unknown",
                    "table_exists": table_exists,
                    "user_count": user_count,
                    "message": "Simple single-table setup for Xero users"
                }
                
                return JSONResponse(result)
    except Exception as e:
        return JSONResponse({
            "status": "error",
            "error": str(e),
            "message": "Database connection failed"
        }, status_code=500)


@APP.get("/api/xero/authorize")
def xero_authorize():
    base = require_base_redirect()
    redirect_uri = f"{base}/api/xero/callback"
    state = secrets.token_urlsafe(24)
    save_state(state)

    params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": redirect_uri,
        "scope": XERO_SCOPES,
        "state": state,
    }
    url = f"{AUTH_URL}?{urlencode(params)}"
    return RedirectResponse(url)


def _compute_expires_at(expires_in: int) -> datetime:
    # Subtract a minute for safety
    return datetime.now(timezone.utc) + timedelta(seconds=max(0, expires_in - 60))


@APP.get("/api/xero/callback", response_class=HTMLResponse)
def xero_callback(code: str = Query(...), state: str = Query(...)):
    base = require_base_redirect()
    redirect_uri = f"{base}/api/xero/callback"

    if not pop_state(state):
        raise HTTPException(status_code=400, detail="Invalid or consumed state")

    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    tok = requests.post(TOKEN_URL, data=data, headers=headers, timeout=30)
    if tok.status_code != 200:
        raise HTTPException(status_code=tok.status_code, detail=f"Token exchange failed: {tok.text[:500]}")
    tj = tok.json()

    access_token = tj.get("access_token")
    refresh_token = tj.get("refresh_token")
    token_type = tj.get("token_type", "Bearer")
    scope = tj.get("scope", "")
    expires_in = int(tj.get("expires_in", 1800))
    expires_at = _compute_expires_at(expires_in)

    # Fetch connections to get tenant(s)
    connh = {"Authorization": f"Bearer {access_token}"}
    conns = requests.get(CONNECTIONS_URL, headers=connh, timeout=30)
    if conns.status_code != 200:
        raise HTTPException(status_code=conns.status_code, detail=f"Connections fetch failed: {conns.text[:500]}")
    arr = conns.json() or []

    # Save user connections (simplified) - with debugging
    saved_for = []
    print(f"[DEBUG] Found {len(arr)} Xero connections to save")
    for c in arr:
        tenant_id = c.get("tenantId")
        tenant_name = c.get("tenantName")
        print(f"[DEBUG] Processing connection: {tenant_name} (ID: {tenant_id})")
        if not tenant_id:
            print(f"[DEBUG] Skipping connection with no tenant_id: {c}")
            continue
        
        try:
            save_user(
                tenant_id,
                tenant_name or "Unknown Organization",
                access_token,
                refresh_token or "",
                expires_at.isoformat(),
            )
            print(f"[DEBUG] Successfully saved user: {tenant_name} ({tenant_id})")
            saved_for.append((tenant_id, tenant_name))
        except Exception as e:
            print(f"[ERROR] Failed to save user {tenant_name}: {e}")
            raise

    if not saved_for:
        return HTMLResponse("<h3>Connected, but no tenants found for this user.</h3>")

    # Simple success page
    options = "".join(
        [f"<li>{t} - <a href='/api/xero/token?tenantId={tid}'>token JSON</a></li>" for tid, t in saved_for]
    )
    html = f"""
    <html><body style='font-family: system-ui'>
      <h3>Connected to Xero</h3>
      <p>Saved tokens for:</p>
      <ul>{options}</ul>
      <p>Next: Configure your chat to call <code>/api/xero/token?tenantId=&lt;id&gt;</code> to fetch a valid access token.</p>
    </body></html>
    """
    return HTMLResponse(html)


def _refresh_token(refresh_token: str) -> dict:
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    r = requests.post(TOKEN_URL, data=data, headers=headers, timeout=30)
    if r.status_code != 200:
        raise HTTPException(status_code=r.status_code, detail=f"Refresh failed: {r.text[:500]}")
    return r.json()


@APP.get("/api/xero/users")
def list_users():
    """List all stored Xero connections for debugging"""
    try:
        users = get_all_users()
        result = []
        for row in users:
            result.append({
                "tenant_id": row[0],
                "tenant_name": row[1], 
                "expires_at": str(row[2]),
                "connected_at": str(row[3]),
                "updated_at": str(row[4])
            })
        
        return JSONResponse({
            "status": "success",
            "count": len(result),
            "users": result
        })
    except Exception as e:
        return JSONResponse({
            "status": "error",
            "error": str(e)
        }, status_code=500)


@APP.delete("/api/xero/users")
def clear_users():
    """Clear all stored Xero connections for testing"""
    try:
        deleted_count = delete_all_users()
        return JSONResponse({
            "status": "success",
            "message": f"Deleted {deleted_count} user connections",
            "deleted_count": deleted_count
        })
    except Exception as e:
        return JSONResponse({
            "status": "error", 
            "error": str(e)
        }, status_code=500)


@APP.get("/api/xero/token")
def xero_token(tenantId: str | None = Query(None)):
    # Get user by tenant ID or get most recent
    row = get_user_by_tenant_id(tenantId)
    if not row:
        raise HTTPException(status_code=404, detail="No stored tokens. Connect first.")

    tenant_id, tenant_name, access_token, refresh_token, expires_at = row
    now = datetime.now(timezone.utc)
    
    # Handle datetime parsing
    if isinstance(expires_at, str):
        try:
            expires_at = datetime.fromisoformat(expires_at)
        except Exception:
            expires_at = now

    # Check if token needs refresh
    if now >= (expires_at - timedelta(seconds=30)):
        if not refresh_token:
            raise HTTPException(status_code=401, detail="Token expired and no refresh_token stored. Reconnect.")
        
        tj = _refresh_token(refresh_token)
        access_token = tj.get("access_token")
        new_refresh = tj.get("refresh_token", refresh_token)
        expires_in = int(tj.get("expires_in", 1800))
        new_expires_at = _compute_expires_at(expires_in)
        
        update_user_tokens(tenant_id, access_token, new_refresh, new_expires_at.isoformat())
        expires_at = new_expires_at

    return JSONResponse(
        {
            "tenant_id": tenant_id,
            "tenant_name": tenant_name,
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_at": expires_at.isoformat() if isinstance(expires_at, datetime) else str(expires_at),
        }
    )




def run_app() -> None:
    """Entrypoint helper so `python app.py` starts Uvicorn."""
    host = get_env("APP_HOST") or "0.0.0.0"
    port_raw = get_env("APP_PORT") or "8000"
    try:
        port = int(port_raw)
    except ValueError:
        port = 8000
    reload_flag = (get_env("UVICORN_RELOAD") or "false").lower() in {"1", "true", "yes"}
    uvicorn.run("server.app:APP", host=host, port=port, reload=reload_flag)


if __name__ == "__main__":
    run_app()

