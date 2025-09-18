import asyncio
import atexit
import json
import os
import shutil
from typing import Any, Dict, List, Optional, Tuple, Union
from contextlib import AsyncExitStack
from datetime import datetime, date, timezone, timedelta
import re
import csv
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
import sys

import requests

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from xero_token_refresh import refresh_expired_tokens, refresh_token_for_client, check_token_status, RefreshResult
# Google GenAI SDK
from google import genai
from google.genai import types
import atexit
from datetime import datetime, timedelta

# ------------ Load environment variables from .env file ------------
from dotenv import load_dotenv
load_dotenv()  # Load .env file, won't override existing env vars

# ------------ Database connectivity for client selection ------------
try:
    import psycopg
    from contextlib import contextmanager
    
    def get_database_url() -> str:
        """Get database URL from environment"""
        raw = os.getenv("DATABASE_URL", "")
        if not raw:
            return ""
        return raw.strip().strip('"').strip("'")
    
    @contextmanager
    def get_db_conn():
        """Get database connection"""
        url = get_database_url()
        if not url:
            raise RuntimeError("DATABASE_URL is not set")
        with psycopg.connect(url, autocommit=True) as conn:
            yield conn
    
    def list_available_clients() -> List[Tuple[int, str, str, str, str]]:
        """List all available Xero clients from database"""
        try:
            with get_db_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT id, tenant_id, tenant_name, expires_at, updated_at
                        FROM xero_users 
                        ORDER BY updated_at DESC
                    """)
                    return cur.fetchall()
        except Exception as e:
            print(f"[WARN] Failed to fetch clients from database: {e}")
            return []
    
    def get_client_by_id(client_id: int) -> Optional[Tuple[int, str, str, str, str, str]]:
        """Get client details by ID"""
        try:
            with get_db_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT id, tenant_id, tenant_name, access_token, refresh_token, expires_at 
                        FROM xero_users 
                        WHERE id = %s
                    """, (client_id,))
                    return cur.fetchone()
        except Exception as e:
            print(f"[WARN] Failed to fetch client {client_id}: {e}")
            return None
    
    DATABASE_AVAILABLE = True
except ImportError:
    print("[WARN] psycopg not available - client selection disabled")
    DATABASE_AVAILABLE = False

# ------------ Optional UX libs (graceful fallback) ------------
# Create dummy classes for graceful fallbacks
class Console:
    def print(self, *args, **kwargs): pass
    def status(self, msg):
        class Dummy:
            def __enter__(self): return self
            def __exit__(self, *args): pass
        return Dummy()

class Table:
    def __init__(self, *args, **kwargs): pass
    def add_column(self, *args, **kwargs): pass
    def add_row(self, *args, **kwargs): pass

class Panel:
    def __init__(self, *args, **kwargs): pass

class Text:
    def __init__(self, *args, **kwargs): pass
    @staticmethod
    def assemble(*args): return ""

class Align:
    @staticmethod
    def center(text): return text

class Style:
    def __init__(self, *args, **kwargs): pass

ROUNDED = "rounded"

class PromptSession:
    def __init__(self, *args, **kwargs): pass
    async def prompt_async(self, prompt): return input(prompt)

class WordCompleter:
    def __init__(self, *args, **kwargs): pass

class FileHistory:
    def __init__(self, *args, **kwargs): pass

class PTK_HTML:
    def __init__(self, *args, **kwargs): pass

def patch_stdout():
    class Dummy:
        def __enter__(self): return self
        def __exit__(self, *args): pass
    return Dummy()

# Attempt to import rich and prompt_toolkit, overwriting dummies if successful
USE_RICH = True
try:
    from rich.console import Console  # type: ignore
    from rich.table import Table  # type: ignore
    from rich.panel import Panel  # type: ignore
    from rich.text import Text  # type: ignore
    from rich.align import Align  # type: ignore
    from rich.box import ROUNDED
    from rich.style import Style  # type: ignore
except ImportError:
    USE_RICH = False

USE_PTK = True
try:
    from prompt_toolkit import PromptSession  # type: ignore
    from prompt_toolkit.completion import WordCompleter  # type: ignore
    from prompt_toolkit.history import FileHistory  # type: ignore
    from prompt_toolkit.formatted_text import HTML as PTK_HTML  # type: ignore
    from prompt_toolkit.patch_stdout import patch_stdout  # type: ignore
except ImportError:
    USE_PTK = False

# ---------------- Config ----------------
APP_NAME = "Xero Assistant"
MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
TOKEN_SERVICE_URL = os.environ.get("XERO_TOKEN_SERVICE_URL", "").strip()
PREFERRED_TENANT_ID = os.environ.get("XERO_TENANT_ID", "").strip()
try:
    TOKEN_SERVICE_TIMEOUT = int(os.environ.get("XERO_TOKEN_SERVICE_TIMEOUT", "15"))
except ValueError:
    TOKEN_SERVICE_TIMEOUT = 15
def get_system_prompt() -> str:
    current_datetime = datetime.now().strftime("%A, %B %d, %Y at %H:%M:%S %Z")
    return f"""
# Xero SME Copilot — System Instruction
You are a senior business analyst + Xero copilot for **SME clients**. Your job is to **do the work**, not just explain it: plan briefly, call MCP tools, and return decision-ready answers.

**CURRENT DATE AND TIME: {current_datetime}**
Use this exact current date and time for all date-related calculations and refe/qui rences. Do not guess or hallucinate dates.

You run on **Gemini 2.5 (Flash/Pro)**. Use a **step-by-step internal plan**, call the right tools, and synthesize concise outputs. If the exact artifact doesn't exist, compose it from multiple endpoints and client-side aggregation.

Reference the Xero MCP tool catalog: @https://github.com/XeroAPI/xero-mcp-server/tree/main/src/tools/list
Research the latest Xero MCP tools and how best to use them for each user query so you can fetch the freshest data before acting.
To understand how the Xero Accounting API works (reports, profit and loss, balance sheet etc.), refer to: @https://developer.xero.com/documentation/api/accounting/reports
For the Xero MCP server repository and docs, see: @https://github.com/XeroAPI/xero-mcp-server/
If the user query is complex, think harder and apply your best planning and analytical skills before responding.

────────────────────────────────────────────────────────────────────────

## 0) Safety & redaction
- Never surface credentials or raw headers. Redact secrets to last 4 chars.
- Summarize error blobs (status + actionable fix). Don’t paste tokens/IDs.

## 1) Who is asking? (light persona model → priorities → tone)
- **Owner/Founder:** cash runway, collections, taxes due, sales pipeline conversion. Wants “what now?” actions.
- **Bookkeeper/Accountant:** AR/AP hygiene, reconciliations, journals, GST/BAS periods, contact/item data quality.
- **Ops/Project lead:** job/region tracking, timesheets/approvals, leave availability, quote→invoice flow, delivery blockers.
- **CFO/Controller:** P&L by month, BS snapshot, variances, margin by item/region, AR aging & top debtors/creditors.

Respond in the **shortest actionable form** for these roles (see Output Contract).

## 2) Date & defaults (no back-and-forth)
- “this month” = first→last day of current month; “this year” = Jan 1→Dec 31; “LTM” = last 12 complete months ending last month.
- Quarters: Q1=Jan–Mar; Q2=Apr–Jun; Q3=Jul–Sep; Q4=Oct–Dec.
- Page size 20 (newest first). Currency: show codes (AUD, NZD). Money: 2 decimals with thousands separators.
- Always list the exact filters used under **Assumptions & Filters**.

## 3) Planning & execution
- **Plan first (internally):** 3–6 bullet steps: which tool(s), which filters, how to aggregate.
- Prefer **fresh data** via MCP over guessing. After each tool run: **synthesize**, don't dump.
- If completion needs **>20 tool calls or >10 auto-pages**, **ask once** for approval with the estimated call/page count (then proceed).

## 4) Intent → tool routing (covering all detected tools)
**Organisation & setup**
- org info → `list-organisation-details`
- accounts/taxes/contact-groups/tracking → `list-accounts`, `list-tax-rates`, `list-contact-groups`, `list-tracking-categories`

**Contacts & items**
- lookup/update → `list-contacts`, `update-contact`
- create contact → `create-contact`
- items (list/create/update) → `list-items`, `create-item`, `update-item`

**Sales / AR (quotes, invoices, payments, credit notes)**
- quotes (list/create/update) → `list-quotes`, `create-quote`, `update-quote`
- invoices (list/create/update) → `list-invoices`, `create-invoice`, `update-invoice`
- payments (list/create) → `list-payments`, `create-payment`
- credit notes (list/create/update) → `list-credit-notes`, `create-credit-note`, `update-credit-note`
- workflow examples:
  - “convert quote to invoice” → fetch quote → `create-invoice` from quote lines → (optional) `create-payment`
  - “write off” small residuals → `create-credit-note` + allocate

**Purchases / AP**
- vendor bills & payments via `list-invoices` with `type=ACCPAY`, `list-payments`, and credit notes tooling as above.

**Banking & cash**
- bank txns (list/create/update) → `list-bank-transactions`, `create-bank-transaction`, `update-bank-transaction`
- proxy cash-flow (see Recipe 4.3)

**Journals**
- manual journals (list/create/update) → `list-manual-journals`, `create-manual-journal`, `update-manual-journal`

**Timesheets & payroll leave**
- timesheets (list/get/create/approve/revert/delete/line ops) → 
  `list-timesheets`, `get-timesheet`, `create-timesheet`, `approve-timesheet`, `revert-timesheet`, `delete-timesheet`, `add-timesheet-line`, `update-timesheet-line`
- payroll employees & leave →
  `list-payroll-employees`, `list-payroll-leave-types`, `list-payroll-employee-leave-types`, `list-payroll-employee-leave-balances`, `list-payroll-employee-leave`, `list-payroll-leave-periods`

**Reports & analysis**
- P&L → `list-profit-and-loss`
- Balance Sheet → `list-report-balance-sheet`
- Trial Balance → `list-trial-balance`
- Aged AR/AP → `list-aged-receivables-by-contact`, `list-aged-payables-by-contact`

**Tracking & dimensionality**
- tracking categories/options (list/create/update) → 
  `list-tracking-categories`, `create-tracking-category`, `update-tracking-category`, 
  `create-tracking-options`, `update-tracking-options`

## 5) High-intent SME asks → immediate patterns (don't ask first)
- **"Cash this month / last month / this week"** → `list-bank-transactions` [date range]; compute **inflow**, **outflow**, **net**, top counterparties, largest 5.
- **"Who owes us the most / top debtors 90d"** → `list-invoices` with `_auto_page:true`, `_summarize:"invoices"`, `_filters:{{from,to,type:"ACCREC"}}`; aggregate outstanding by contact.
- **"Top expenses / where I spent the most / biggest spending"** → `list-invoices` with `_auto_page:true`, `_summarize:"invoices"`, `_filters:{{from,to,type:"ACCPAY"}}`; aggregate by contact for vendor spending analysis.
- **"Overdue now"** → invoices where `status` overdue and `dueDate < today`.
- **"Profit each month this year"** → monthly P&L (see Recipe 4.1).
- **"Balance sheet as of YYYY-MM-DD"** → point BS (see Recipe 4.2).
- **"Quote→Invoice and send payment link"** → pull quote → create invoice → (optional) create payment or surface balance + due.
- **"Leave liability / who's available next 2 weeks"** → payroll leave types/balances + periods; summarize by employee.
- **"Timesheets to approve"** → `list-timesheets` with status filter → `approve-timesheet` or `revert-timesheet` as instructed.

## 6) Pagination & client-side aggregation
- For “all/summary/big ranges,” request:
  - `_auto_page:true` (safety cap applies)
  - `_summarize:"invoices"` where supported
  - `_filters:{{from,to,type,...}}` for light client-side filters
- Stop early if you already reached a stable headline number.

## 7) Report recipes (do; don’t ask)
**4.1 P&L – monthly view for a year**  
Try `list-profit-and-loss` with `fromDate=YYYY-01-01`, `toDate=YYYY-12-31`, `timeframe=MONTH`.  
If API enforces `periods 1–11`: run 2 calls (Jan–Nov with `periods=11`, Dec with `periods=1`) or loop 12 months; merge client-side.  
Return table: Month, Revenue, COGS, Gross Profit, Opex, Net Profit; totals and MoM deltas.

**4.2 Balance Sheet snapshot**  
`list-report-balance-sheet` with `asOfDate`. If a range is given, show end-of-period and change vs start.

**4.3 Cash movement (proxy cash-flow)**  
`list-bank-transactions` in range. Compute inflow/outflow/net, top counterparties, and largest 5 transactions.

**4.4 Invoices KPIs**  
`list-invoices` with `_auto_page:true` + `_summarize:"invoices"` → top customers, overdue totals, collections by day/contact.

**4.5 Aged AR/AP**  
Direct aged reports by contact; optionally scope to a named contact and return bucketed totals (0-30, 31-60, 61-90, 90+).

**4.6 Tracking splits**  
Pass tracking filters where supported; otherwise fetch and group client-side by category/option.

## 8) Creation/update workflows (be specific; validate minimum fields)
- **Invoices/Quotes/Payments/Credit notes/Manual journals/Contacts/Items/Bank transactions**
  - Validate required fields; infer safe defaults (today’s date, due terms from org or 30d if absent, currency).
  - Echo a **concise change log**: what was created/updated, total(s), status, and key IDs (partially masked if sensitive).
- **Timesheets**
  - When approving or reverting, confirm counts and date spans.
  - Deleting is destructive—state the object and date span before executing.

## 9) Error-recovery playbooks
- **Period rules clash (P&L)** → switch to two-call or monthly loop.
- **Rate-limit** → retry once with smaller scope; otherwise state the limit and suggest narrowing.
- **Auth/scope** → say what’s missing (scope) and ask to re-auth.
- **Empty data** → say so plainly; suggest concrete next step (wider range/different status/other org).

## 10) Output Contract (always)
**CRITICAL:** After calling any tools, you MUST provide a complete user-facing response.

1) **Direct answer:** one headline paragraph.  
2) **Key metrics:** bullets with numbers.  
3) **Details:** compact table or concise bullets (top rows/aggregates).  
4) **Assumptions & Filters:** exact dates, statuses, currency, defaults, redactions.  
5) **Next steps:** 1–2 precise follow-ups ("drill into July variance by contact?").

Keep text minimal; prefer aggregates over raw dumps. **Never leave the user without a clear, actionable response.**

## 11) Ask-once policy
Proceed without asking if the plan needs **≤20 tool calls** or **≤10 auto-pages**.  
If heavier, ask once (“~N calls/pages; proceed?”), then execute.

## 12) Final reminders
- Think briefly, then act. Route to the **most specific tool**, aggregate client-side when needed.
- If something cannot be done, propose the closest viable alternative and the *exact* tool sequence you'll run next.
- **ALWAYS provide a user-visible response** after tool calls. Summarize findings, present key insights, and answer the user's question clearly.
- Return only user-visible text (plain/markdown). No raw headers/tokens or giant payloads.

"""

# History/windowing
HISTORY_PATH = os.path.expanduser("~/.xero_mcp_history")
MAX_TURNS = int(os.environ.get("MAX_TURNS", "24"))  # rough bound on conversation length
# Auto-paging safety
AUTO_PAGE_MAX = int(os.environ.get("AUTO_PAGE_MAX", "15"))  # cap pages when _auto_page is on

# UI + paging defaults
DEFAULT_PAGE_SIZE = 20
MIN_PAGE_SIZE = 8

console = Console()

# ---------------------- Generic helpers ----------------------
def pretty_json(data: Any) -> str:
    try:
        return json.dumps(data, indent=2, ensure_ascii=False)
    except Exception:
        return str(data)

def truncate_str(s: str, limit: int = 2000) -> str:
    if len(s) <= limit:
        return s
    return s[:limit] + f"\n... [truncated {len(s)-limit} chars]"

def sizeof_json(obj: Any) -> int:
    try:
        return len(json.dumps(obj, ensure_ascii=False))
    except Exception:
        return 0

# ---------------------- MCP result normalization ----------------------
def mask_secret(value: Optional[str], visible: int = 4) -> str:
    """Return a masked representation of a secret value."""
    if not value:
        return ""
    trimmed = value.strip()
    if len(trimmed) <= visible:
        return "*" * len(trimmed)
    masked_len = max(0, len(trimmed) - visible)
    return "*" * masked_len + trimmed[-visible:]

def _flatten_mcp_result(result) -> dict:
    """
    Normalize MCP tool result into a simple payload:
      {"ok": bool, "parts": [{"type":"text","text":...} | {"type":"json","json":...} | ...], "message":str?}
    """
    payload = {"ok": not getattr(result, "isError", False), "parts": []}
    for c in getattr(result, "content", []) or []:
        typ = getattr(c, "type", None)
        if typ == "text":
            payload["parts"].append({"type": "text", "text": getattr(c, "text", "")})
        elif typ == "json":
            payload["parts"].append({"type": "json", "json": getattr(c, "json", None)})
        else:
            payload["parts"].append({"type": str(typ) or "unknown", "value": repr(c)})
    msg = getattr(result, "message", None)
    if msg:
        payload["message"] = msg
    return payload

def extract_text_response(response) -> str:
    """
    Collect user-visible text from the model response.
    """
    if not getattr(response, "candidates", None):
        return ""
    out: List[str] = []

    cand = (response.candidates or [None])[0]
    if not cand or not getattr(cand, "content", None) or not getattr(cand.content, "parts", None):
        return ""

    # Collect all text parts
    for part in cand.content.parts or []:
        if getattr(part, "text", None):
            out.append(part.text)

    return "".join(out)

# ---------------------- UI helpers ----------------------
def banner():
    if USE_RICH:
        title = Text(APP_NAME, style="bold cyan")
        subtitle = Text(
            f"Connected to Xero MCP | Model: {MODEL} | {datetime.now().strftime('%Y-%m-%d %H:%M')}",
            style="dim",
        )
        console.print(Panel(Align.center(Text.assemble(title, "\n", subtitle)), box=ROUNDED))
    else:
        print(f"{APP_NAME} | {datetime.now().strftime('%Y-%m-%d %H:%M')}")
        print(f"Model: {MODEL}")
        print('-' * 60)


def info(msg: str):
    if USE_RICH:
        console.print(f"[cyan]INFO[/cyan] {msg}")
    else:
        print("[INFO]", msg)


def success(msg: str):
    if USE_RICH:
        console.print(f"[green]OK[/green] {msg}")
    else:
        print("[OK]", msg)


def warn(msg: str):
    if USE_RICH:
        console.print(f"[yellow]WARN[/yellow] {msg}")
    else:
        print("[WARN]", msg)


def error(msg: str):
    if USE_RICH:
        console.print(f"[red]ERROR[/red] {msg}")
    else:
        print("[ERROR]", msg)


def spinner(msg: str):
    class Dummy:
        def __enter__(self):
            if not USE_RICH:
                print(msg + "...")
        def __exit__(self, exc_type, exc, tb):
            pass
    return console.status(msg) if USE_RICH else Dummy()


def display_token_usage(current_usage: dict, session_totals: dict, cache_info: dict = None):
    """Display token consumption with validation, debugging, and cache information"""
    if not current_usage and not session_totals:
        return
    
    # Validate current usage values
    current_input = int(current_usage.get('input_tokens', 0)) if current_usage else 0
    current_output = int(current_usage.get('output_tokens', 0)) if current_usage else 0
    current_total = current_input + current_output  # Always calculate from components
    
    # Validate session totals values
    session_input = int(session_totals.get('input_tokens', 0)) if session_totals else 0
    session_output = int(session_totals.get('output_tokens', 0)) if session_totals else 0
    session_total = session_input + session_output  # Always calculate from components
    
    # Cache information processing
    cache_enabled = cache_info and cache_info.get('enabled', False)
    estimated_cache_tokens = cache_info.get('estimated_saved_tokens', 15000) if cache_info else 15000
    session_api_calls = cache_info.get('api_calls', 1) if cache_info else 1
    
    # Debug validation - check if our calculations match stored totals
    if current_usage and current_usage.get('total_tokens', 0) != current_total:
        warn(f"Token calculation mismatch - Current: stored={current_usage.get('total_tokens', 0)}, calculated={current_total}")
    
    if session_totals and session_totals.get('total_tokens', 0) != session_total:
        warn(f"Token calculation mismatch - Session: stored={session_totals.get('total_tokens', 0)}, calculated={session_total}")
        
    if USE_RICH:
        # Create a compact token usage display
        current_text = ""
        session_text = ""
        
        if current_total > 0:
            cache_part = ""
            if cache_enabled:
                cache_part = f" [dim](~{estimated_cache_tokens:,} cached)[/dim]"
            current_text = f"[dim]This call:[/dim] {current_input:,} in + {current_output:,} out = [bold]{current_total:,}[/bold] tokens{cache_part}"
            
        if session_total > 0:
            cache_session_part = ""
            if cache_enabled:
                # Estimate total cache savings for the session
                total_cache_saved = estimated_cache_tokens * session_api_calls
                cache_session_part = f" [dim](~{total_cache_saved:,} cached)[/dim]"
            session_text = f"[dim]Session total:[/dim] {session_input:,} in + {session_output:,} out = [bold]{session_total:,}[/bold] tokens{cache_session_part}"
        
        if current_text and session_text:
            token_info = f"{current_text}\n{session_text}"
        elif current_text:
            token_info = current_text
        elif session_text:
            token_info = session_text
        else:
            return
            
        console.print(f"[cyan]🔢 Token Usage:[/cyan] {token_info}")
    else:
        # Fallback for non-rich environments
        if current_total > 0:
            cache_part = ""
            if cache_enabled:
                cache_part = f" (~{estimated_cache_tokens:,} cached)"
            print(f"[TOKENS] This call: {current_input:,} in + {current_output:,} out = {current_total:,} tokens{cache_part}")
        
        if session_total > 0:
            cache_session_part = ""
            if cache_enabled:
                # Estimate total cache savings for the session
                total_cache_saved = estimated_cache_tokens * session_api_calls
                cache_session_part = f" (~{total_cache_saved:,} cached)"
            print(f"[TOKENS] Session total: {session_input:,} in + {session_output:,} out = {session_total:,} tokens{cache_session_part}")


# ---------------------- Client Selection UI ----------------------
def select_xero_client() -> Optional[int]:
    """Display available Xero clients and prompt the user to pick one.
    If none are valid, try to refresh expired ones automatically.
    """
    if not DATABASE_AVAILABLE:
        error("Database connection not available. Cannot proceed without database connection.")
        return None

    def _query_clients():
        return list_available_clients() or []

    clients = _query_clients()
    if not clients:
        error("No Xero clients found in database. Please complete OAuth flow first:")
        error("1. Start server: python server/app.py")
        error("2. Visit: http://localhost:8000")
        error("3. Click 'Connect to Xero' and complete authorization")
        return None

    # Partition valid vs expired
    valid_clients, expired_clients = [], []
    now = datetime.now(timezone.utc)
    for client in clients:
        client_id, tenant_id, tenant_name, expires_at, updated_at = client
        try:
            exp_dt = datetime.fromisoformat(str(expires_at).replace('Z', '+00:00')) if expires_at else None
        except Exception:
            exp_dt = None
        if not exp_dt or exp_dt <= now:
            expired_clients.append(client)
        else:
            valid_clients.append(client)

    # If no valid clients, attempt automatic refresh and re-query once
    if not valid_clients and expired_clients:
        info("Attempting automatic token refresh for expired Xero orgs...")
        try:
            refreshed_count, refresh_results = refresh_expired_tokens(grace_seconds=300, verbose=False)
            
            # Show results for each refresh attempt
            for result in refresh_results:
                if result.success:
                    success(f"Refreshed {result.tenant_name or result.client_id}")
                else:
                    warn(f"Failed to refresh {result.tenant_name or result.client_id}: {result.error_message}")
            
            if refreshed_count > 0:
                info(f"Successfully refreshed {refreshed_count} token(s). Re-checking client status...")
                clients = _query_clients()
                valid_clients, expired_clients = [], []
                now = datetime.now(timezone.utc)
                for client in clients:
                    client_id, tenant_id, tenant_name, expires_at, updated_at = client
                    try:
                        exp_dt = datetime.fromisoformat(str(expires_at).replace('Z', '+00:00')) if expires_at else None
                    except Exception:
                        exp_dt = None
                    if not exp_dt or exp_dt <= now:
                        expired_clients.append(client)
                    else:
                        valid_clients.append(client)
        except Exception as e:
            warn(f"Auto-refresh attempt failed: {e}")

    # UI render as before (unchanged) ...
    if USE_RICH:
        console.print("\n[bold cyan]Available Xero Organizations[/bold cyan]")
        if valid_clients:
            table = Table(box=ROUNDED, show_lines=False)
            table.add_column("ID", justify="right", style="bold green", width=4)
            table.add_column("Organization", style="bold")
            table.add_column("Tenant ID", style="dim")
            table.add_column("Last Updated", style="dim")
            table.add_column("Expires", style="green")
            for client in valid_clients:
                client_id, tenant_id, tenant_name, expires_at, updated_at = client
                try:
                    exp_dt = datetime.fromisoformat(str(expires_at).replace('Z', '+00:00')) if expires_at else None
                    upd_dt = datetime.fromisoformat(str(updated_at).replace('Z', '+00:00')) if updated_at else None
                    exp_str = exp_dt.strftime("%Y-%m-%d %H:%M") if exp_dt else "Unknown"
                    upd_str = upd_dt.strftime("%Y-%m-%d %H:%M") if upd_dt else "Unknown"
                except Exception:
                    exp_str = str(expires_at)[:16] if expires_at else "Unknown"
                    upd_str = str(updated_at)[:16] if updated_at else "Unknown"
                table.add_row(
                    str(client_id),
                    tenant_name or "Unknown Organization",
                    tenant_id[:12] + "..." if tenant_id and len(tenant_id) > 15 else tenant_id or "",
                    upd_str,
                    exp_str,
                )
            console.print(table)
        if expired_clients:
            console.print("\n[bold red]Expired/Invalid Tokens:[/bold red]")
            expired_table = Table(box=ROUNDED, show_lines=False)
            expired_table.add_column("ID", justify="right", style="dim", width=4)
            expired_table.add_column("Organization", style="dim")
            expired_table.add_column("Status", style="red")
            for client in expired_clients:
                client_id, tenant_id, tenant_name, expires_at, updated_at = client
                expired_table.add_row(str(client_id), tenant_name or "Unknown Organization", "EXPIRED")
            console.print(expired_table)
    else:
        print("\nAvailable Xero Organizations:")
        print("=" * 60)
        for client in valid_clients:
            client_id, tenant_id, tenant_name, expires_at, updated_at = client
            print(f"ID: {client_id}")
            print(f"  Organization: {tenant_name or 'Unknown Organization'}")
            print(f"  Status: VALID")
            print("-" * 40)
        if expired_clients:
            print("\nExpired/Invalid Tokens:")
            for client in expired_clients:
                client_id, tenant_id, tenant_name, expires_at, updated_at = client
                print(f"ID: {client_id}")
                print(f"  Organization: {tenant_name or 'Unknown Organization'}")
                print(f"  Status: EXPIRED")
                print("-" * 40)

    if not valid_clients:
        error("No valid Xero tokens found. All tokens are expired or invalid.")
        error("If auto-refresh didn’t work, your refresh tokens may be invalid/expired. Re-authorize:")
        error("1. Run: python server/app.py")
        error("2. Visit: http://localhost:8000")
        error("3. Click 'Connect to Xero' and complete authorization")
        return None

    valid_ids = [c[0] for c in valid_clients]
    while True:
        try:
            user_input = input("\nEnter Client ID (or 'q' to quit): ").strip()
            if user_input.lower() in ('q', 'quit', 'exit'):
                return None
            client_id = int(user_input)
            if client_id in valid_ids:
                return client_id
            error(f"Invalid client ID. Choose from valid IDs: {', '.join(map(str, valid_ids))}")
        except ValueError:
            error("Please enter a valid numeric client ID")
        except (KeyboardInterrupt, EOFError):
            print("\n")
            return None


def setup_dynamic_token(client_id: int) -> bool:
    """Fetch client from DB; if token expired, try refresh; then set env vars."""
    if not DATABASE_AVAILABLE:
        return False

    client_data = get_client_by_id(client_id)
    if not client_data:
        error(f"Client ID {client_id} not found in database")
        return False

    client_id_db, tenant_id, tenant_name, access_token, refresh_token, expires_at = client_data

    # Parse expiry
    try:
        exp_dt = datetime.fromisoformat(str(expires_at).replace('Z', '+00:00')) if expires_at else None
    except Exception:
        exp_dt = None

    now = datetime.now(timezone.utc)
    
    # Check if token is expired or expiring soon (within 5 minutes)
    is_expired = not exp_dt or exp_dt <= now
    expires_soon = exp_dt and exp_dt <= (now + timedelta(minutes=5)) if exp_dt else True
    
    if is_expired or expires_soon:
        if is_expired:
            info(f"Token for {tenant_name} is expired. Attempting refresh...")
        else:
            info(f"Token for {tenant_name} expires soon (within 5 minutes). Proactively refreshing...")
        try:
            refresh_result = refresh_token_for_client(client_id_db, verbose=False)
            if not refresh_result.success:
                error(f"Token refresh failed: {refresh_result.error_message}")
                error("Please re-authorize via server/app.py → Connect to Xero.")
                return False
            else:
                success(f"Token refreshed successfully. New expiry: {refresh_result.new_expiry}")
        except Exception as e:
            error(f"Refresh attempt failed: {e}")
            return False
        
        # Re-fetch fresh values
        client_data = get_client_by_id(client_id)
        if not client_data:
            error("Post-refresh, client row not found.")
            return False
        client_id_db, tenant_id, tenant_name, access_token, refresh_token, expires_at = client_data

    # Set env for MCP server
    os.environ['XERO_CLIENT_BEARER_TOKEN'] = access_token
    os.environ['XERO_TENANT_ID'] = tenant_id

    token_preview = ("*" * (len(access_token) - 4) + access_token[-4:]) if access_token else ""
    if USE_RICH:
        console.print(Panel(
            f"[green]Connected to:[/green] {tenant_name}\n"
            f"[dim]Tenant ID:[/dim] {tenant_id}\n"
            f"[dim]Token:[/dim] {token_preview}\n"
            f"[dim]Expires:[/dim] {expires_at}",
            title="Dynamic Token Setup",
            box=ROUNDED,
            style="green"
        ))
    else:
        success(f"Connected to: {tenant_name}")
        print(f"  Tenant ID: {tenant_id}")
        print(f"  Token: {token_preview}")
        print(f"  Expires: {expires_at}")

    return True


class TokenServiceError(Exception):
    """Raised when fetching a Xero access token from the SaaS backend fails."""


def _parse_iso8601(ts: str | None) -> Optional[datetime]:
    if not ts:
        return None
    value = ts.strip()
    if value.endswith('Z'):
        value = value[:-1] + '+00:00'
    try:
        return datetime.fromisoformat(value)
    except Exception:
        return None


def fetch_token_from_service(base_url: str, tenant_id: str | None) -> Dict[str, Any]:
    if not base_url:
        raise TokenServiceError('Token service URL is not configured.')
    url = base_url.rstrip('/') + '/api/xero/token'
    headers = {'Accept': 'application/json'}
    params: Dict[str, str] = {}
    if tenant_id:
        params['tenantId'] = tenant_id
    try:
        resp = requests.get(url, headers=headers, params=params, timeout=TOKEN_SERVICE_TIMEOUT)
    except requests.RequestException as exc:
        raise TokenServiceError(f'Request failed: {exc}') from exc
    if resp.status_code != 200:
        snippet = resp.text.strip()
        if len(snippet) > 200:
            snippet = snippet[:200] + '...'
        raise TokenServiceError(f"{resp.status_code} {snippet}")
    try:
        data = resp.json()
    except ValueError as exc:
        raise TokenServiceError('Token service returned invalid JSON.') from exc
    if not isinstance(data, dict):
        raise TokenServiceError('Unexpected token service response type.')
    if not data.get('access_token'):
        raise TokenServiceError('Token service response is missing access_token.')
    return data


def ensure_bearer_token_from_service() -> Optional[Dict[str, Any]]:
    if os.environ.get('XERO_CLIENT_BEARER_TOKEN'):
        return None
    if not TOKEN_SERVICE_URL:
        return None
    payload = fetch_token_from_service(TOKEN_SERVICE_URL, PREFERRED_TENANT_ID or None)
    os.environ['XERO_CLIENT_BEARER_TOKEN'] = payload['access_token']
    tenant = payload.get('tenant_id') or payload.get('tenantId')
    if tenant and not os.environ.get('XERO_TENANT_ID'):
        os.environ['XERO_TENANT_ID'] = tenant
    return payload

def render_json_table_if_applicable(payload: dict):
    """
    Generic renderer: if top-level is a list[dict], render a table of common keys; else pretty JSON.
    """
    # Try to find a list[dict] inside payload["parts"] json parts
    rows: Optional[List[Dict[str, Any]]] = None
    if not payload:
        return
    for p in payload.get("parts", []):
        if p.get("type") == "json":
            data = p.get("json")
            # Accept raw list[dict] or dict with known collection keys
            if isinstance(data, list) and (not data or isinstance(data[0], dict)):
                rows = data
                break
            if isinstance(data, dict):
                for k in (
                    "items",
                    "results",
                    "data",
                    "contacts",
                    "Contacts",
                    "transactions",
                    "Transactions",
                    "invoices_rows",
                    "totals_by_contact"
                ):
                    v = data.get(k)
                    if isinstance(v, list) and (not v or isinstance(v[0], dict)):
                        rows = v
                        break
        if rows:
            break

    if rows is None:
        # Fallback: print JSON payload
        body = pretty_json(payload)
        if USE_RICH:
            console.print(Panel(truncate_str(body, 12000), title="Tool Payload", box=ROUNDED, style="dim"))
        else:
            print("\nTool Payload:\n" + body)
        return

    # Render table of first N rows and selected keys
    max_rows = 20
    sample = rows[:max_rows]
    # Choose up to 6 interesting keys (union of first few rows)
    keys: List[str] = []
    for r in sample[:5]:
        if isinstance(r, dict):
            for k in r.keys():
                if k not in keys:
                    keys.append(k)
                if len(keys) >= 6:
                    break
        if len(keys) >= 6:
            break
    keys = keys or ["id", "name", "date", "amount"]  # fallback

    if USE_RICH:
        table = Table(box=ROUNDED, show_lines=False, title=f"Items (showing {len(sample)} of {len(rows)})")
        table.add_column("#", justify="right", style="dim", width=4)
        for k in keys:
            table.add_column(str(k))
        for i, r in enumerate(sample, 1):
            if isinstance(r, dict):
                table.add_row(str(i), *[str(r.get(k, ""))[:80] for k in keys])
            else:
                table.add_row(str(i), *([""] * len(keys)))
        console.print(table)
    else:
        print(f"\nItems (showing {len(sample)} of {len(rows)}):")
        header = " | ".join(keys)
        print(f"# | {header}")
        for i, r in enumerate(sample, 1):
            if isinstance(r, dict):
                print(f"{i} | " + " | ".join(str(r.get(k, ""))[:80] for k in keys))
            else:
                print(f"{i} | " + " | ".join([""] * len(keys)))

def _extract_rows_from_payload(payload: dict) -> Optional[List[Dict[str, Any]]]:
    """
    Find a tabular list[dict] inside a tool payload (shared logic with the table renderer).
    Returns the list of rows, or None if not found.
    """
    if not payload:
        return None
    for p in payload.get("parts", []):
        if p.get("type") == "json":
            data = p.get("json")
            if isinstance(data, list) and (not data or isinstance(data[0], dict)):
                return data
            if isinstance(data, dict):
                for k in (
                    "items",
                    "results",
                    "data",
                    "contacts",
                    "Contacts",
                    "transactions",
                    "Transactions",
                    "invoices_rows",
                    "totals_by_contact"
                ):
                    v = data.get(k)
                    if isinstance(v, list) and (not v or isinstance(v[0], dict)):
                        return v
    return None

def _select_keys_for_rows(rows: List[Dict[str, Any]]) -> List[str]:
    """Choose up to 6 representative keys from the first few rows."""
    keys: List[str] = []
    for r in rows[:5]:
        if isinstance(r, dict):
            for k in r.keys():
                if k not in keys:
                    keys.append(k)
                if len(keys) >= 6:
                    break
        if len(keys) >= 6:
            break
    return keys or ["id", "name", "date", "amount"]

def summarize_payload_to_text(payload: dict, max_rows: int = 10) -> str:
    """
    Build a concise, parseable text summary from the last tool payload so the UI never stays blank.
    Priority: tabular rows -> text parts -> short note about structured data.
    """
    if not payload:
        return ""
    rows = _extract_rows_from_payload(payload)
    if rows is not None:
        show = max(1, min(max_rows, len(rows)))
        keys = _select_keys_for_rows(rows)
        lines: List[str] = []
        
        # Check if this looks like expense data and provide context
        if any(key in ['contact', 'amount', 'total'] for key in keys):
            lines.append(f"**Found {len(rows)} expense items** (showing top {show}):")
        else:
            lines.append(f"Items: {len(rows)} rows (showing {show}).")
            
        lines.append(" | ".join(keys))
        for r in rows[:show]:
            if isinstance(r, dict):
                lines.append(" | ".join(str(r.get(k, ""))[:80] for k in keys))
            else:
                lines.append(" | ".join([""] * len(keys)))
        
        # Add helpful summary for expense-like data
        if len(rows) > 0 and isinstance(rows[0], dict) and 'amount' in rows[0]:
            try:
                total = sum(float(r.get('amount', 0)) for r in rows if r.get('amount'))
                lines.append(f"\n**Total Amount:** ${total:,.2f}")
            except:
                pass
                
        return "\n".join(lines)

    # Fallback to any text parts provided by the tool(s)
    text_blocks = [p.get("text", "") for p in payload.get("parts", []) if p.get("type") == "text"]
    body = "\n".join([t for t in text_blocks if t]).strip()
    if body:
        return truncate_str(body, 4000)

    # Last resort: acknowledge structured data presence
    return "Tool returned structured data. See the table above or use /export to save it."

# ---------------------- Export helpers (generic) ----------------------
def export_json(path: str, payload: dict):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    success(f"Saved JSON to {path}")

def export_csv_table_guess(path: str, payload: dict):
    """
    If we can find a list[dict] inside payload, dump it as CSV; else warn.
    """
    rows: Optional[List[Dict[str, Any]]] = None
    headers: List[str] = []
    for p in payload.get("parts", []):
        if p.get("type") == "json":
            data = p.get("json")
            if isinstance(data, list) and (not data or isinstance(data[0], dict)):
                rows = data
                break
            if isinstance(data, dict):
                for k in (
                    "items",
                    "results",
                    "data",
                    "contacts",
                    "Contacts",
                    "transactions",
                    "Transactions",
                    "invoices_rows",
                    "totals_by_contact"
                ):
                    v = data.get(k)
                    if isinstance(v, list) and (not v or isinstance(v[0], dict)):
                        rows = v
                        break
        if rows:
            break
    if not rows:
        warn("Could not find a tabular list in the last payload.")
        return
    # Collect headers from first few rows
    for r in rows[:10]:
        for k in r.keys():
            if k not in headers:
                headers.append(k)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(headers)
        for r in rows:
            w.writerow([r.get(h, "") for h in headers])
    success(f"Saved CSV to {path}")

# ---------------------- Parsing & Aggregation (client-side) ----------------------
def _count_listed_items(payload: dict) -> int:
    """Count 'Invoice ID:' lines in text blocks. Used to detect page fullness."""
    if not payload:
        return 0
    blocks = [p.get("text", "") for p in payload.get("parts", []) if p.get("type") == "text"]
    return sum(1 for line in "\n".join(blocks).splitlines() if line.strip().startswith("Invoice ID:"))

def _parse_invoices_from_text(parts: List[dict]) -> List[dict]:
    """Parse minimal invoice fields from Xero MCP's text output into structured rows."""
    invoices: List[dict] = []
    buf: List[str] = []

    def flush():
        if not buf:
            return
        block = "\n".join(buf)
        row: Dict[str, Any] = {}

        def grab(label: str, pat: str):
            m = re.search(pat, block)
            if m:
                row[label] = m.group(1).strip()

        grab("id", r"Invoice ID:\s*([^\n]+)")
        grab("type", r"Type:\s*([A-Z]+)")
        grab("contact", r"Contact:\s*([^(^\n]+)")
        grab("date_raw", r"Date:\s*([^\n]+)")
        grab("total_raw", r"Total:\s*([0-9]+(?:\.[0-9]+)?)")
        grab("currency", r"Currency:\s*([A-Z]{3})")

        if row.get("id"):
            # Normalize date from strings like: "Wed Jul 19 2017 10:00:00 GMT+1000 (...)"
            raw = row.get("date_raw", "")
            try:
                # take the first 24 chars "Wed Jul 19 2017 10:00:00"
                short = raw[:24]
                row["date"] = datetime.strptime(short, "%a %b %d %Y %H:%M:%S").date()
            except Exception:
                row["date"] = None
            try:
                row["total"] = Decimal(row.get("total_raw", "0"))
            except Exception:
                row["total"] = Decimal("0")
            invoices.append(row)
        buf.clear()

    for p in parts:
        if p.get("type") != "text":
            continue
        for line in p.get("text", "").splitlines():
            if line.startswith("Invoice ID:") and buf:
                flush()
            buf.append(line)
    flush()
    return invoices

def _apply_invoice_filters(
    invoices: List[dict],
    filters: Optional[Dict[str, str]]
) -> Tuple[List[dict], Optional[date], Optional[date], Optional[str]]:
    """Filter invoices by from/to (dates) and type (ACCREC/ACCPAY)."""
    if not filters:
        return invoices, None, None, None

    f = filters or {}
    from_d = None
    to_d = None
    t_filter = f.get("type")
    try:
        if f.get("from"):
            from_d = datetime.strptime(f["from"], "%Y-%m-%d").date()
    except Exception:
        from_d = None
    try:
        if f.get("to"):
            to_d = datetime.strptime(f["to"], "%Y-%m-%d").date()
    except Exception:
        to_d = None

    out: List[dict] = []
    for r in invoices:
        d = r.get("date")
        ty = r.get("type")
        if t_filter and ty != t_filter:
            continue
        if from_d and (not d or d < from_d):
            continue
        if to_d and (not d or d > to_d):
            continue
        out.append(r)

    return out, from_d, to_d, t_filter

def _aggregate_invoices_by_contact(invoices: List[dict]) -> List[dict]:
    """Sum totals by contact; returns list[{'contact','amount'}] sorted desc."""
    sums: Dict[str, Decimal] = defaultdict(Decimal)
    for r in invoices:
        contact = r.get("contact") or "Unknown"
        sums[contact] += r.get("total", Decimal("0"))
    rows = [{"contact": k, "amount": float(v)} for k, v in sums.items()]  # float for JSON friendliness
    rows.sort(key=lambda x: x["amount"], reverse=True)
    return rows

# ---------------------- Auto-paging wrapper ----------------------
async def _call_with_auto_paging(session, name: str, args: dict) -> dict:
    """
    Auto-pages list-* tools when `_auto_page: true` is provided.
    For invoices, supports `_summarize: "invoices"` and optional `_filters`.
    """
    do_auto = bool(args.pop("_auto_page", False))
    summarize = args.pop("_summarize", None)
    filters = args.pop("_filters", None)

    # If not auto-paging or not a list tool, do a single call.
    if not do_auto or not name.startswith("list-"):
        mcp_result = await session.call_tool(name=name, arguments=args)
        return _flatten_mcp_result(mcp_result)

    combined_parts: List[dict] = []
    page = int(args.get("page", 1))
    pages_done = 0

    while pages_done < AUTO_PAGE_MAX:
        cur_args = {**args, "page": page}
        try:
            mcp_result = await session.call_tool(name=name, arguments=cur_args)
            payload = _flatten_mcp_result(mcp_result)
        except Exception as e:
            # On error mid-way, stop and return what we have + error
            combined_parts.append({"type": "json", "json": {"warning": f"Stopped paging due to error on page {page}: {str(e)}"}})
            break

        parts = payload.get("parts", [])
        combined_parts.extend(parts)

        # Stop conditions: page returns fewer than typical page size; for invoices we detect via 'Invoice ID:' count
        item_count = _count_listed_items(payload)
        if item_count == 0 or item_count < 10:
            break

        page += 1
        pages_done += 1

    # Optional summarization for invoices
    if summarize == "invoices":
        invoices = _parse_invoices_from_text(combined_parts)
        filtered, from_d, to_d, t_filter = _apply_invoice_filters(invoices, filters)

        totals = _aggregate_invoices_by_contact(filtered)
        top = totals[0] if totals else None
        currency = "AUD"  # Xero demo data usually AUD; adjust if you parse per-invoice

        summary_json = {
            "kind": "invoices_summary",
            "currency": currency,
            "parsed_invoices": len(invoices),
            "filtered_invoices": len(filtered),
            "filters_used": {
                "from": str(from_d) if from_d else None,
                "to": str(to_d) if to_d else None,
                "type": t_filter,
            },
            # Use 'items' so /export csv works out-of-the-box
            "items": totals,  # totals_by_contact
            "top_customer": top,
        }

        # Also include a small sample of normalized rows to help export/debug
        sample_rows = [
            {
                "id": r.get("id"),
                "date": str(r.get("date")) if r.get("date") else None,
                "type": r.get("type"),
                "contact": r.get("contact"),
                "total": float(r.get("total", Decimal("0"))),
                "currency": r.get("currency") or "AUD",
            }
            for r in filtered[:500]  # cap
        ]
        summary_json["invoices_rows"] = sample_rows

        combined_parts.append({"type": "json", "json": summary_json})

    return {"ok": True, "parts": combined_parts}

# ---------------------- Turn-repair (fix INVALID_ARGUMENT) ----------------------
def _repair_orphaned_function_calls(history: List[Any]) -> None:
    """
    If the last turn is an assistant/model turn with function_call parts and the next
    turn is not a tool response, drop the orphaned call so the API is happy.
    """
    if not history:
        return
    last = history[-1]
    role = getattr(last, "role", None)
    parts = getattr(last, "parts", None)
    has_func_call = any(getattr(p, "function_call", None) for p in (parts or []))
    if role in ("assistant", "model") and has_func_call:
        # No tool response followed -> remove the dangling call turn
        history.pop()

# ---------------------- Token Tracking ----------------------
class TokenTracker:
    """Track token consumption for individual calls and entire chat session"""
    
    def __init__(self):
        self.session_input_tokens = 0
        self.session_output_tokens = 0
        self.session_total_tokens = 0
        self.session_api_calls = 0  # Track number of API calls for cache calculation
        self.last_call_usage = {'input_tokens': 0, 'output_tokens': 0, 'total_tokens': 0}
        
    def add_usage(self, input_tokens: int = 0, output_tokens: int = 0, total_tokens: int = 0):
        """Add token usage from a single API call"""
        # Validate inputs are actually numbers
        input_tokens = int(input_tokens) if input_tokens else 0
        output_tokens = int(output_tokens) if output_tokens else 0
        
        # Calculate total tokens correctly (input + output)
        calculated_total = input_tokens + output_tokens
        
        # Use the calculated total rather than the API provided total to avoid confusion
        # This ensures consistency in our counting
        self.session_input_tokens += input_tokens
        self.session_output_tokens += output_tokens
        self.session_total_tokens += calculated_total
        self.session_api_calls += 1  # Increment API call count
        
        # Store the last call's usage for display
        self.last_call_usage = {
            'input_tokens': input_tokens,
            'output_tokens': output_tokens,
            'total_tokens': calculated_total
        }
        
    def get_session_totals(self) -> dict:
        """Get cumulative session token usage"""
        return {
            'input_tokens': self.session_input_tokens,
            'output_tokens': self.session_output_tokens,
            'total_tokens': self.session_total_tokens,
            'api_calls': self.session_api_calls
        }
    
    def get_last_call_usage(self) -> dict:
        """Get token usage from the last API call"""
        return self.last_call_usage.copy()
    
    def extract_token_usage(self, response) -> dict:
        """Extract token usage from Gemini API response with robust field detection"""
        try:
            if hasattr(response, 'usage_metadata') and response.usage_metadata:
                usage = response.usage_metadata
                
                # Try multiple possible field names for input tokens
                input_tokens = 0
                for field in ['prompt_token_count', 'input_token_count', 'prompt_tokens']:
                    if hasattr(usage, field):
                        input_tokens = int(getattr(usage, field, 0))
                        break
                
                # Try multiple possible field names for output tokens
                output_tokens = 0
                for field in ['candidates_token_count', 'output_token_count', 'completion_tokens', 'response_token_count']:
                    if hasattr(usage, field):
                        output_tokens = int(getattr(usage, field, 0))
                        break
                
                # Calculate total (don't trust API total as it might be different)
                calculated_total = input_tokens + output_tokens
                
                # Debug info when tokens are detected
                if input_tokens > 0 or output_tokens > 0:
                    print(f"[DEBUG] Token usage - Input: {input_tokens}, Output: {output_tokens}, Total: {calculated_total}")
                
                return {
                    'input_tokens': input_tokens,
                    'output_tokens': output_tokens,
                    'total_tokens': calculated_total
                }
            else:
                # Check if response has any usage information in other locations
                if hasattr(response, 'candidates') and response.candidates:
                    for candidate in response.candidates:
                        if hasattr(candidate, 'token_count'):
                            output_tokens = int(candidate.token_count)
                            return {
                                'input_tokens': 0,  # Can't determine input from this
                                'output_tokens': output_tokens,
                                'total_tokens': output_tokens
                            }
                
                warn("No token usage information found in response")
                return {'input_tokens': 0, 'output_tokens': 0, 'total_tokens': 0}
                
        except Exception as e:
            warn(f"Failed to extract token usage: {e}")
            # Print response structure for debugging
            if hasattr(response, 'usage_metadata'):
                try:
                    fields = [attr for attr in dir(response.usage_metadata) if not attr.startswith('_')]
                    warn(f"Available usage fields: {fields}")
                except:
                    pass
            return {'input_tokens': 0, 'output_tokens': 0, 'total_tokens': 0}

class CacheManager:
    def __init__(self):
        self.cached_content = None
        self.cache_name = None
        
    def create_system_cache(self, client, system_prompt: str) -> str:
        """Create a cached version of the system prompt"""
        if self.cached_content:
            return self.cached_content.name
            
        # Generate unique cache name for this session
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.cache_name = f"xero_mcp_system_cache_{timestamp}"
        
        try:
            # Create cached content - expires in 1 hour (perfect for your 30min sessions)
            self.cached_content = client.models.cache_content(
                model="gemini-2.5-flash",
                system_instruction=system_prompt,
                ttl=timedelta(hours=1),  # Auto-expires after 1 hour
                display_name=self.cache_name
            )
            
            print(f"[INFO] System prompt cached as: {self.cache_name}")
            return self.cached_content.name
            
        except Exception as e:
            print(f"[WARN] Failed to cache system prompt: {e}")
            return None
    
    def cleanup_cache(self, client):
        """Clean up the cache when session ends"""
        if self.cached_content:
            try:
                client.models.delete_cached_content(name=self.cached_content.name)
                print(f"[INFO] Cleaned up cache: {self.cache_name}")
            except Exception as e:
                print(f"[WARN] Failed to cleanup cache: {e}")
            finally:
                self.cached_content = None
                self.cache_name = None

# Create global cache manager
cache_manager = CacheManager()

# Register cleanup on exit
def cleanup_on_exit():
    try:
        client = genai.Client()
        cache_manager.cleanup_cache(client)
    except:
        pass  # Ignore errors during cleanup

atexit.register(cleanup_on_exit)

# ---------------------- Chat Session ----------------------
# Replace the ChatState class with this updated version
class ChatState:
    def __init__(self):
        self.history_contents: List[Any] = []
        self.tool_names: List[str] = []
        self.last_tool_payload: Optional[dict] = None
        self.page_size = DEFAULT_PAGE_SIZE
        self.token_tracker = TokenTracker()
        self.cached_content_name: Optional[str] = None  # Add this line

    def auto_page_size(self):
        try:
            _, rows = shutil.get_terminal_size((100, 30))
            usable = max(MIN_PAGE_SIZE, rows - 16)
            self.page_size = max(MIN_PAGE_SIZE, min(80, usable))
        except Exception:
            self.page_size = DEFAULT_PAGE_SIZE

    def trim_history(self):
        if len(self.history_contents) > MAX_TURNS:
            self.history_contents = self.history_contents[-MAX_TURNS:]

# ---------------------- Core mediator ----------------------
async def chat_loop(session: ClientSession):
    client = genai.Client()
    state = ChatState()
    state.auto_page_size()

    # Initialize cache for system prompt
    system_prompt = get_system_prompt()
    state.cached_content_name = cache_manager.create_system_cache(client, system_prompt)
    
    # Discover tools
    tools_resp = await session.list_tools()
    state.tool_names = [t.name for t in tools_resp.tools]

    banner()
    info(f"Detected MCP tools: {', '.join(state.tool_names) or '(none found)'}")
    if state.cached_content_name:
        info(f"System prompt cached - saving ~15k tokens per call")

    def build_config(cached_content_name: Optional[str] = None) -> types.GenerateContentConfig:
        if cached_content_name:
            # Use cached system prompt - saves ~15k+ tokens per call
            return types.GenerateContentConfig(
                cached_content=cached_content_name,
                temperature=0,
                tools=[session],
                automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
                response_mime_type="text/plain",
            )
        else:
            # Fallback to non-cached version
            return types.GenerateContentConfig(
                system_instruction=get_system_prompt(),
                temperature=0,
                tools=[session],
                automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
                response_mime_type="text/plain",
            )
    async def run_tool_call_loop() -> str:
        """
        Send conversation to the model; if it asks for tool calls, run them with guardrails,
        return tool outputs, and iterate until a final answer is produced.
        """
        while True:

            # Ensure no dangling assistant function_call before generation
            _repair_orphaned_function_calls(state.history_contents)

            with spinner("Thinking with Gemini..."):
                # Convert history_contents to the format expected by the API
                converted_contents = []
                for c in state.history_contents:
                    if hasattr(c, 'role') and hasattr(c, 'parts'):
                        converted_contents.append({"role": c.role, "parts": c.parts})
                    else:
                        # Handle cases where content might be in different format
                        converted_contents.append(c)
                
                resp = await client.aio.models.generate_content(
                    model=MODEL,
                    contents=converted_contents,
                    config=build_config(state.cached_content_name),
                )
                
                # Extract and track token usage from this API call
                token_usage = state.token_tracker.extract_token_usage(resp)
                state.token_tracker.add_usage(
                    input_tokens=token_usage['input_tokens'],
                    output_tokens=token_usage['output_tokens']
                    # total_tokens calculated internally now
                )

            # Collect function calls from first candidate deterministically
            function_calls = []
            cand = (resp.candidates or [None])[0]
            if cand and getattr(resp, "function_calls", None):
                function_calls = resp.function_calls
            elif cand and cand.content and getattr(cand.content, "parts", None):
                for part in cand.content.parts or []:
                    if getattr(part, "function_call", None):
                        function_calls.append(part.function_call)

            # No tool calls -> finalize
            if not function_calls:
                if cand and cand.content:
                    state.history_contents.append(cand.content)
                state.trim_history()
                return extract_text_response(resp)

            # Append assistant tool-call request to history
            if cand and cand.content:
                state.history_contents.append(cand.content)

            # Guardrails: run only known tools; ensure args is dict-like
            response_parts: List[Any] = []
            for fc in function_calls:
                name = getattr(fc, "name", "")
                args = dict(getattr(fc, "args", {}) or {})
                if name not in state.tool_names:
                    payload = {"ok": False, "error": f"Unknown tool '{name}'."}
                else:
                    with spinner(f"Calling MCP tool: {name}"):
                        try:
                            # Use auto-paging wrapper (supports meta-args)
                            payload = await _call_with_auto_paging(session, name, args)
                        except Exception as e:
                            payload = {"ok": False, "error": str(e)}

                # Keep last tool payload for optional export/inspection
                state.last_tool_payload = payload

                # Show a compact view to the user (generic)
                render_json_table_if_applicable(payload)

                # Return structured tool response back to the model
                try:
                    part = types.Part(function_response=types.FunctionResponse(name=name, response=payload))
                    response_parts.append(part)
                except Exception:
                    # Fallback to dict format
                    response_parts.append({
                        "function_response": {"name": name, "response": payload}
                    })

            # One consolidated tool turn back to Gemini (MUST be immediately after the call)
            try:
                tool_content = types.Content(role="tool", parts=response_parts)
                state.history_contents.append(tool_content)
            except Exception:
                # Fallback to dict format
                state.history_contents.append({"role": "tool", "parts": response_parts})
            state.trim_history()
            # Loop again (model may chain more calls or finalize)

    # ------------- Input setup -------------
    def build_session():
        completer_words = [
            "/tools", "/call", "/export", "/help", "/quit"
        ]
        if USE_PTK:
            completer = WordCompleter(completer_words, ignore_case=True, sentence=True, match_middle=True)
            hist = FileHistory(HISTORY_PATH)
            return PromptSession(history=hist, completer=completer)
        return None

    ptk_session = build_session()

    async def get_input() -> str:
        if USE_PTK and ptk_session:
            try:
                with patch_stdout():
                    return await ptk_session.prompt_async(
                        PTK_HTML('<b><ansicyan>you</ansicyan></b> > ')
                    )
            except KeyboardInterrupt:
                return ""
        try:
            return await asyncio.to_thread(input, "\nYou: ")
        except KeyboardInterrupt:
            return ""

    def show_help():
        cmds = [
            "/tools                        list available MCP tools",
            "/call <name> <json-args>     call a tool directly, e.g. /call list-bank-transactions {{\"pageSize\":10}}",
            "/export json <path>          export last tool payload as JSON",
            "/export csv <path>           export last tool payload as CSV (best-effort if list[dict])",
            "/tokens                      show detailed token usage statistics",
            "/help                        show this menu",
            "/quit                        exit",
        ]
        body = "\n".join(cmds)
        if USE_RICH:
            console.print(Panel(body, title="Commands", box=ROUNDED))
        else:
            print("\nCommands:\n" + "\n".join("  " + c for c in cmds))

    def list_tools():
        if not state.tool_names:
            warn("No tools discovered.")
            return
        if USE_RICH:
            table = Table(box=ROUNDED, show_lines=False, title="Discovered MCP Tools")
            table.add_column("#", justify="right", style="dim", width=4)
            table.add_column("Name", style="bold")
            for i, n in enumerate(state.tool_names, 1):
                table.add_row(str(i), n)
            console.print(table)
        else:
            print("\nDiscovered MCP Tools:")
            for i, n in enumerate(state.tool_names, 1):
                print(f"  {i}. {n}")

    def show_token_debug():
        """Show detailed token usage statistics for debugging"""
        session_totals = state.token_tracker.get_session_totals()
        last_call = state.token_tracker.get_last_call_usage()
        
        if USE_RICH:
            console.print("\n[bold cyan]Token Usage Debug Information[/bold cyan]")
            console.print(f"[green]Session Totals:[/green]")
            console.print(f"  Input Tokens:  {session_totals.get('input_tokens', 0):,}")
            console.print(f"  Output Tokens: {session_totals.get('output_tokens', 0):,}")
            console.print(f"  Total Tokens:  {session_totals.get('total_tokens', 0):,}")
            console.print(f"  API Calls:     {session_totals.get('api_calls', 0):,}")
            console.print(f"\n[green]Last API Call:[/green]")
            console.print(f"  Input Tokens:  {last_call.get('input_tokens', 0):,}")
            console.print(f"  Output Tokens: {last_call.get('output_tokens', 0):,}")
            console.print(f"  Total Tokens:  {last_call.get('total_tokens', 0):,}")
            
            # Show cache info if available
            if state.cached_content_name:
                estimated_saved = 15000 * session_totals.get('api_calls', 1)
                console.print(f"\n[green]Cache Status:[/green] Using cached system prompt ({state.cached_content_name})")
                console.print(f"[green]Cache Savings:[/green] Estimated ~{estimated_saved:,} tokens saved this session")
            else:
                console.print(f"\n[yellow]Cache Status:[/yellow] No cache in use")
        else:
            print("\nToken Usage Debug Information:")
            print("Session Totals:")
            print(f"  Input Tokens:  {session_totals.get('input_tokens', 0):,}")
            print(f"  Output Tokens: {session_totals.get('output_tokens', 0):,}")
            print(f"  Total Tokens:  {session_totals.get('total_tokens', 0):,}")
            print(f"  API Calls:     {session_totals.get('api_calls', 0):,}")
            print("\nLast API Call:")
            print(f"  Input Tokens:  {last_call.get('input_tokens', 0):,}")
            print(f"  Output Tokens: {last_call.get('output_tokens', 0):,}")
            print(f"  Total Tokens:  {last_call.get('total_tokens', 0):,}")
            
            if state.cached_content_name:
                estimated_saved = 15000 * session_totals.get('api_calls', 1)
                print(f"\nCache Status: Using cached system prompt ({state.cached_content_name})")
                print(f"Cache Savings: Estimated ~{estimated_saved:,} tokens saved this session")
            else:
                print("\nCache Status: No cache in use")

    async def call_tool_direct(name: str, args_str: str):
        if name not in state.tool_names:
            error(f"Unknown tool: {name}")
            return
        try:
            args = json.loads(args_str) if args_str.strip() else {}
            if not isinstance(args, dict):
                raise ValueError("Args must be a JSON object.")
        except Exception as e:
            error(f"Invalid JSON args: {e}")
            return
        with spinner(f"Calling MCP tool: {name}"):
            try:
                payload = await _call_with_auto_paging(session, name, args)
            except Exception as e:
                error(str(e))
                return
        state.last_tool_payload = payload
        render_json_table_if_applicable(payload)

    def do_export(parts: List[str]):
        if len(parts) < 2:
            error("Usage: /export json|csv <path>")
            return
        kind = parts[0].lower()
        path = parts[1]
        if not state.last_tool_payload:
            warn("No tool payload available to export.")
            return
        if kind == "json":
            if not path.lower().endswith(".json"):
                path += ".json"
            export_json(path, state.last_tool_payload)
        elif kind == "csv":
            if not path.lower().endswith(".csv"):
                path += ".csv"
            export_csv_table_guess(path, state.last_tool_payload)
        else:
            error("Unknown export type. Use json|csv.")

    # ---------------- Main loop ----------------
    while True:
        user = (await get_input()) or ""
        user = user.strip()
        if not user:
            continue

        low = user.lower()

        # Commands
        if low in ("/quit", "/exit"):
            success("Goodbye!")
            return
        if low in ("/help", "help"):
            show_help()
            continue
        if low.startswith("/tools"):
            list_tools()
            continue
        if low.startswith("/tokens"):
            show_token_debug()
            continue
        if low.startswith("/call "):
            # /call <name> <json-args>
            try:
                _, rest = user.split(None, 1)
                name, args_str = (rest.split(None, 1) + [""])[:2]
                await call_tool_direct(name, args_str)
            except ValueError:
                error("Usage: /call <tool-name> <json-args>")
            continue
        if low.startswith("/export"):
            parts = user.split()
            do_export(parts[1:])
            continue

        # Otherwise: forward to the model (natural language)
        try:
            user_content = types.UserContent(parts=[types.Part(text=user)])
            state.history_contents.append(user_content)
        except Exception:
            # Fallback to dict format if types construction fails
            state.history_contents.append({"role": "user", "parts": [{"text": user}]})
        state.trim_history()

        final_text = await run_tool_call_loop()
        if final_text and final_text.strip():
            if USE_RICH:
                console.print(Panel(final_text, title="Assistant", box=ROUNDED, style=Style(color="white")))
            else:
                print(f"\nAssistant: {final_text}")
        else:
            # Fallback: synthesize from last tool payload so UI never appears blank
            fallback_text = summarize_payload_to_text(state.last_tool_payload or {}) if state.last_tool_payload else ""
            if fallback_text:
                if USE_RICH:
                    console.print(Panel(fallback_text, title="Assistant", box=ROUNDED, style=Style(color="white")))
                else:
                    print(f"\nAssistant: {fallback_text}")
            else:
                warn("No user-visible text returned (model sent only tool calls or thoughts). Try /thinking off and ask again.")
        
        # Display token consumption after each iteration
        session_totals = state.token_tracker.get_session_totals()
        last_call_usage = state.token_tracker.get_last_call_usage()
        
        # Prepare cache information for display
        cache_info = {
            'enabled': bool(state.cached_content_name),
            'estimated_saved_tokens': 15000,  # Estimated system prompt size
            'api_calls': session_totals.get('api_calls', 1)
        }
        
        display_token_usage(last_call_usage, session_totals, cache_info)
    
    # Add cleanup when chat ends
    try:
        cache_manager.cleanup_cache(client)
    except Exception as e:
        print(f"[WARN] Cache cleanup failed: {e}")

# ---------------------- Entrypoint ----------------------
async def main():
    if not shutil.which("npx"):
        raise RuntimeError("npx is not installed. Install Node.js (includes npx).")

    # ======== NEW: Dynamic Client Selection Flow ========
    # Skip hardcoded .env tokens and prompt user for client selection
    
    # Clear any existing hardcoded bearer token to force dynamic selection
    if 'XERO_CLIENT_BEARER_TOKEN' in os.environ:
        info("Clearing hardcoded XERO_CLIENT_BEARER_TOKEN to enable dynamic client selection")
        del os.environ['XERO_CLIENT_BEARER_TOKEN']
    
    # Step 1: Try client selection from database (NEW PRIMARY METHOD)
    bearer_token_set = False
    if DATABASE_AVAILABLE:
        try:
            selected_client_id = select_xero_client()
            if selected_client_id:
                bearer_token_set = setup_dynamic_token(selected_client_id)
                if bearer_token_set:
                    info("Using dynamic token from database client selection")
        except Exception as e:
            error(f"Client selection failed: {e}")
            return  # Fail fast instead of falling back
    
    # Step 2: Remove fallback to token service - fail if database method doesn't work
    if not bearer_token_set:
        error("No valid authentication method available. Please ensure:")
        error("1. Database connection is working and contains valid Xero tokens")
        error("2. Tokens are not expired - complete OAuth flow if needed")
        error("3. Run: python server/app.py and visit http://localhost:8000 to refresh tokens")
        return

    # Step 3: Validate we have a bearer token
    if not os.environ.get('XERO_CLIENT_BEARER_TOKEN'):
        error('No valid Xero authentication token available. Cannot proceed.')
        return

    # You can override the MCP server via env if desired
    mcp_command = os.environ.get("MCP_COMMAND", "npx")
    mcp_args_env = os.environ.get("MCP_ARGS_JSON", "")
    if mcp_args_env:
        try:
            args_list = json.loads(mcp_args_env)
            if not isinstance(args_list, list):
                raise ValueError
            mcp_args = args_list
        except Exception:
            raise RuntimeError("MCP_ARGS_JSON must be a JSON array of args.")
    else:
        # Default to Xero MCP server
        mcp_args = ["-y", "@xeroapi/xero-mcp-server@latest"]

    server_params = StdioServerParameters(
        command=mcp_command,
        args=mcp_args,
        env={
            "XERO_CLIENT_ID": os.environ.get("XERO_CLIENT_ID", ""),
            "XERO_CLIENT_SECRET": os.environ.get("XERO_CLIENT_SECRET", ""),
            "XERO_CLIENT_BEARER_TOKEN": os.environ.get("XERO_CLIENT_BEARER_TOKEN", ""),
            "XERO_TENANT_ID": os.environ.get("XERO_TENANT_ID", ""),
        },
    )

    try:
        async with AsyncExitStack() as stack:
            read, write = await stack.enter_async_context(stdio_client(server_params))
            async with ClientSession(read, write) as session:
                await session.initialize()
                # Header for non-rich envs
                print(f"Connected to Xero MCP at {datetime.now().isoformat(timespec='seconds')}")
                print(f"Model: {MODEL}  |  SDK: google-genai")
                await chat_loop(session)
    except KeyboardInterrupt:
        print("\n[INFO] Chat interrupted by user")
        try:
            client = genai.Client()
            cache_manager.cleanup_cache(client)
        except:
            pass
    except Exception as e:
        print(f"[ERROR] Unexpected error: {e}")
        try:
            client = genai.Client()
            cache_manager.cleanup_cache(client)
        except:
            pass

if __name__ == "__main__":
    asyncio.run(main())

